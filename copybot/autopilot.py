"""Scan and trade Hyperliquid perpetuals using a bound API wallet."""

import argparse
import fcntl
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal, DecimalException
from pathlib import Path
from time import sleep
from uuid import uuid4

import httpx
import yaml
from pydantic import Field

from .hyperliquid import AccountGateway, TradingError, load_signer
from .models import Model, Positive
from .perps import BPS, Feed, now_ms, number, round_price

MIN_NOTIONAL = Decimal(10)
ACTION_TTL_MS = 10_000
RECONCILIATION_DELAY_MS = 5_000
CLOSED = {
    "filled",
    "canceled",
    "marginCanceled",
    "reduceOnlyCanceled",
    "siblingFilledCanceled",
    "delistedCanceled",
    "liquidatedCanceled",
    "scheduledCancel",
    "triggered",
    "openInterestCapCanceled",
    "selfTradeCanceled",
    "vaultWithdrawalCanceled",
}


class Settings(Model):
    owner: str | None = None
    markets: list[str] = Field(min_length=1, max_length=12)
    max_order_usd: Positive = Field(ge=10)
    max_entries_per_day: int = Field(default=3, ge=1, le=24)
    min_volume_usd: Positive = Decimal(10_000_000)
    max_funding_bps: Positive = Decimal(2)
    max_spread_bps: Positive = Decimal(8)
    entry_slippage_bps: Positive = Field(default=Decimal(5), le=25)
    stop_bps: Positive = Field(default=Decimal(75), le=500)
    target_bps: Positive = Field(default=Decimal(150), le=2000)
    max_hold_minutes: int = Field(default=90, ge=5, le=1440)


def utc_day():
    return datetime.now(UTC).date().isoformat()


def order_id():
    return f"0x{uuid4().hex}"


def terminal(status):
    return status in CLOSED or status == "rejected" or status.endswith("Rejected")


class Journal:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, time_ms INTEGER NOT NULL,
                event TEXT NOT NULL, details TEXT NOT NULL);
        """)

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def record(self, event, details):
        self.db.execute(
            "INSERT INTO audit (time_ms,event,details) VALUES (?,?,?)",
            (now_ms(), event, json.dumps(details, default=str)),
        )

    def change_trade(self, trade, event):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.put("trade", trade)
            self.record(event, trade)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def stage_entry(self, trade, day, daily_limit):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            counts = self.get("entries", {})
            count = counts.get(day, 0)
            allowed = (
                not self.get("paused", False)
                and not self.get("trade")
                and count < daily_limit
                and trade["bar_end"] > self.get("last_bar", 0)
            )
            if allowed:
                self.put("trade", trade)
                self.put("entries", {day: count + 1})
                self.put("last_bar", trade["bar_end"])
                self.record("entry_intent", trade)
            self.db.commit()
            return allowed
        except BaseException:
            self.db.rollback()
            raise

    def status(self):
        events = self.db.execute(
            "SELECT time_ms,event,details FROM audit ORDER BY id DESC LIMIT 20"
        )
        return {
            "paused": self.get("paused", False),
            "entries_today": self.get("entries", {}).get(utc_day(), 0),
            "trade": self.get("trade"),
            "last_cycle": self.get("last_cycle"),
            "recent_events": [
                {"time_ms": ts, "event": event, "details": json.loads(details)}
                for ts, event, details in events
            ],
        }


def entry_order(signal, book, cfg):
    price = book.ask if signal.long else book.bid
    size_at_top = book.ask_size if signal.long else book.bid_size
    direction = 1 if signal.long else -1
    if (
        book.spread_bps > cfg.max_spread_bps
        or (price - signal.trigger) * direction <= 0
        or (price / signal.close - 1) * direction * BPS > 35
    ):
        return None
    if (
        signal.market.funding * direction * BPS > cfg.max_funding_bps
        or signal.market.mark <= 0
        or abs(price / signal.market.mark - 1) * BPS > 25
    ):
        return None
    if signal.long:
        limit = round_price(price * (1 + cfg.entry_slippage_bps / BPS), signal.market.decimals)
    else:
        limit = round_price(
            price * (1 - cfg.entry_slippage_bps / BPS), signal.market.decimals, ROUND_CEILING
        )
    # A short IOC can fill above its limit. Size against the higher of book and observed mark.
    cap_price = max(price, limit, signal.market.mark)
    increment = Decimal(1).scaleb(-signal.market.decimals)
    size = (MIN_NOTIONAL / min(price, limit)).quantize(increment, rounding=ROUND_CEILING)
    if size > size_at_top or size * cap_price > cfg.max_order_usd:
        return None
    return size, limit


class Runner:
    def __init__(self, feed, gateway, journal, settings):
        self.feed = feed
        self.gateway = gateway
        self.journal = journal
        self.settings = settings

    def lookup(self, coin, cloid):
        try:
            result = self.gateway.lookup(coin, cloid)
        except httpx.HTTPError:
            result = None
        if result is not None:
            return result
        try:
            return self.gateway.historical_order(coin, cloid)
        except httpx.HTTPError:
            return None

    def settled(self, trade, leg):
        field = f"{leg}_expires"
        if field not in trade:
            # Older journals have no signing deadline. Allow a fresh observation window.
            trade[field] = now_ms() + ACTION_TTL_MS
            self.journal.change_trade(trade, f"{leg}_reconciliation_started")
            return False
        return now_ms() >= trade[field] + RECONCILIATION_DELAY_MS

    def entry_size(self, trade):
        entry = self.lookup(trade["coin"], trade["entry_id"])
        if entry is None:
            return Decimal(0) if trade.get("entry_rejected") else None
        status = entry["status"]
        if not terminal(status):
            return None
        if status == "rejected" or status.endswith("Rejected"):
            return Decimal(0)
        original = number(entry["order"]["origSz"])
        return original if status == "filled" else original - number(entry["order"]["sz"])

    def confirmed_flat(self, trade):
        if not trade.get("flat_seen"):
            trade["flat_seen"] = True
            self.journal.change_trade(trade, "flat_observed")
            return False
        return True

    def step(self):
        trade = self.journal.get("trade")
        if trade:
            return self.manage(trade)
        if self.journal.get("paused", False):
            return "Entries paused"
        cfg = self.settings
        day = utc_day()
        if self.journal.get("entries", {}).get(day, 0) >= cfg.max_entries_per_day:
            return "Daily entry limit reached"
        signals, counts = self.feed.scan(
            cfg.markets, min_volume=cfg.min_volume_usd, max_funding_bps=cfg.max_funding_bps
        )
        for signal in signals:
            if signal.bar_end <= self.journal.get("last_bar", 0):
                continue
            coin = signal.market.coin
            if not self.gateway.flat_market(coin):
                continue
            if entry_order(signal, self.feed.book(coin), cfg) is None:
                continue
            self.gateway.prepare_margin(coin, signal.long, cfg.max_order_usd)
            if not self.gateway.flat_market(coin):
                continue
            book = self.feed.book(coin)
            proposed = entry_order(signal, book, cfg)
            if not proposed or now_ms() - signal.bar_end > 5 * 60_000:
                continue
            size, limit = proposed
            opened = now_ms()
            trade = {
                "coin": coin,
                "long": signal.long,
                "decimals": signal.market.decimals,
                "entry_id": order_id(),
                "bar_end": signal.bar_end,
                "opened": opened,
                "entry_expires": opened + ACTION_TTL_MS,
                "requested_size": str(size),
                "limit": str(limit),
                "stop_bps": str(cfg.stop_bps),
                "target_bps": str(cfg.target_bps),
                "max_hold_minutes": cfg.max_hold_minutes,
                "signal": signal.evidence(),
                "market_context": counts.get("market_context", {}),
            }
            if not self.journal.stage_entry(trade, utc_day(), cfg.max_entries_per_day):
                return "Entry skipped because the journal changed"
            rejected = self.gateway.order(
                coin, signal.long, size, limit, trade["entry_id"], expires_at=trade["entry_expires"]
            )
            if rejected:
                trade["entry_rejected"] = rejected
                self.journal.change_trade(trade, "entry_rejected")
            return self.manage(trade)
        return {"result": "No eligible entry", **counts, "signals": len(signals)}

    def manage(self, trade):
        coin, long = trade["coin"], trade["long"]
        position = self.gateway.market_state(coin)
        signed_size = number(position["szi"]) if position else Decimal(0)
        size = abs(signed_size)
        if size and (signed_size > 0) != long:
            raise TradingError(f"{coin} position changed direction outside autopilot")
        if size and trade.pop("flat_seen", False):
            self.journal.change_trade(trade, "position_visible_again")

        if "owned_size" not in trade:
            filled = self.entry_size(trade)
            requested = number(trade["requested_size"])
            if size > requested or (filled is not None and (filled < size or filled > requested)):
                raise TradingError(f"{coin} position could not be attributed to the entry order")
            if not size:
                if filled == 0 and self.gateway.flat_market(coin):
                    self.journal.change_trade(None, "entry_unfilled")
                    return f"No fill on {coin}"
                if (
                    filled is None
                    and self.settled(trade, "entry")
                    and self.gateway.flat_market(coin)
                    and not self.gateway.has_market_fills(coin, trade["opened"])
                    and self.confirmed_flat(trade)
                ):
                    self.journal.record("entry_reconciled_flat", trade)
                    self.journal.change_trade(None, "trade_complete")
                    return f"Confirmed flat on {coin} after entry deadline"
                return f"Reconciling entry status and position on {coin}"
            if filled is None:
                raise TradingError(
                    f"{coin} has open exposure without a confirmed entry; stop protection is unverified"
                )
            trade["owned_size"] = str(filled)
            trade["entry_price"] = position["entryPx"]
            self.journal.change_trade(trade, "entry_confirmed")

        if size > number(trade["owned_size"]):
            raise TradingError(f"{coin} position was increased outside autopilot")
        stop = self.lookup(coin, trade["stop_id"]) if "stop_id" in trade else None
        if not size:
            if not self.confirmed_flat(trade):
                return f"Rechecking flat position on {coin}"
            return self.finish(trade, stop)

        if trade.get("exit_id"):
            exit_order = self.lookup(coin, trade["exit_id"])
            if (
                not trade.get("exit_rejected")
                and not (exit_order and terminal(exit_order["status"]))
                and not self.settled(trade, "exit")
            ):
                return f"Waiting for the previous reduce-only exit on {coin} to settle"
            if trade.get("entry_price") and number(position["entryPx"]) != number(
                trade["entry_price"]
            ):
                raise TradingError(f"{coin} entry price changed outside autopilot")
            return self.exit(trade, size, trade["exit_reason"])

        decimals = trade["decimals"]
        entry_price = number(position["entryPx"])
        if trade.get("entry_price") and entry_price != number(trade["entry_price"]):
            raise TradingError(f"{coin} entry price changed outside autopilot")
        direction = 1 if long else -1
        trigger = round_price(
            entry_price * (1 - direction * number(trade["stop_bps"]) / BPS),
            decimals,
            ROUND_CEILING if long else ROUND_DOWN,
        )
        if "stop_id" not in trade:
            trade["stop_id"] = order_id()
            trade["stop_expires"] = now_ms() + ACTION_TTL_MS
            self.journal.change_trade(trade, "stop_intent")
            limit = round_price(
                trigger * (1 - direction * Decimal("0.03")),
                decimals,
                ROUND_DOWN if long else ROUND_CEILING,
            )
            result = self.gateway.order(
                coin,
                not long,
                size,
                limit,
                trade["stop_id"],
                reduce_only=True,
                stop=trigger,
                expires_at=trade["stop_expires"],
            )
            if result:
                trade["stop_rejected"] = result
                self.journal.change_trade(trade, "stop_rejected")
                return self.exit(trade, size, "protective stop rejected")
            return self.manage(trade)

        if not stop and not trade.get("stop_rejected") and not self.settled(trade, "stop"):
            return f"Waiting to verify protective stop for {coin}"
        if not stop or stop["status"] != "open":
            return self.exit(trade, size, "protective stop unavailable or triggered")
        details = stop["order"]
        if (
            not details["reduceOnly"]
            or not details["isTrigger"]
            or details["side"] != ("A" if long else "B")
            or number(details["triggerPx"]) != trigger
            or number(details["origSz"]) < size
        ):
            return self.exit(trade, size, "protective stop does not cover the open position")

        if now_ms() - trade["opened"] >= trade["max_hold_minutes"] * 60_000:
            return self.exit(trade, size, "maximum holding time")
        executable = self.feed.exit_price(coin, sell=long)
        move = (executable / entry_price - 1) * direction * BPS
        if move >= number(trade["target_bps"]):
            return self.exit(trade, size, "take profit", executable)
        return f"Monitoring protected {coin} position"

    def exit(self, trade, size, reason, executable=None):
        coin, long = trade["coin"], trade["long"]
        quote = executable if executable is not None else self.feed.exit_price(coin, sell=long)
        price = quote * (Decimal("0.995") if long else Decimal("1.005"))
        limit = round_price(price, trade["decimals"], ROUND_DOWN if long else ROUND_CEILING)
        trade.update(
            exit_id=order_id(),
            exit_reason=reason,
            exit_rejected=None,
            exit_expires=now_ms() + ACTION_TTL_MS,
        )
        self.journal.change_trade(trade, "exit_intent")
        rejected = self.gateway.order(
            coin,
            not long,
            size,
            limit,
            trade["exit_id"],
            reduce_only=True,
            expires_at=trade["exit_expires"],
        )
        if rejected:
            trade["exit_rejected"] = rejected
            self.journal.change_trade(trade, "exit_rejected")
        return f"Reduce-only exit requested for {coin}: {reason}"

    def finish(self, trade, stop):
        coin = trade["coin"]
        if stop and stop["status"] == "open":
            self.gateway.cancel(coin, trade["stop_id"])
            return f"Canceling leftover {coin} protective stop"
        if (
            "stop_id" in trade
            and not trade.get("stop_rejected")
            and (not stop or not terminal(stop["status"]))
            and not self.settled(trade, "stop")
        ):
            raise TradingError(f"Flat on {coin}, but protective stop status is unconfirmed")
        if trade.get("exit_id") and not trade.get("exit_rejected"):
            exit_order = self.lookup(coin, trade["exit_id"])
            if (not exit_order or not terminal(exit_order["status"])) and not self.settled(
                trade, "exit"
            ):
                raise TradingError(f"Flat on {coin}, but exit order status is unconfirmed")
        if not self.gateway.flat_market(coin):
            raise TradingError(f"{coin} still has a position or open order")
        self.journal.record("position_flat", trade)
        self.journal.change_trade(None, "trade_complete")
        return f"Confirmed flat on {coin}"


def emit(result):
    print(json.dumps(result, default=str), flush=True)


def scan_result(feed):
    signals, counts = feed.scan()
    return {
        "mode": "market scan",
        **counts,
        "signals": [
            {
                "market": row.market.coin,
                "side": "long" if row.long else "short",
                "bar_end_ms": row.bar_end,
                "breakout_price": row.trigger,
                "close": row.close,
                **row.evidence(),
            }
            for row in signals
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("state/hyperliquid-live.sqlite"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--agent-key-file", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="Scan public markets without submitting orders")
    scan.add_argument("--interval", type=int, choices=range(30, 3601), metavar="30..3600")
    commands.add_parser("check", help="Verify API wallet authorization without signing an order")
    for name in ("status", "pause", "resume"):
        commands.add_parser(name)
    live = commands.add_parser("run", help="Start live trading under the supplied config")
    live.add_argument("--live", action="store_true", required=True)
    live.add_argument("--interval", type=int, choices=range(30, 301), default=60, metavar="30..300")
    args = parser.parse_args(argv)

    import requests
    from hyperliquid.utils.error import ClientError, ServerError
    from keyring.errors import KeyringError

    failures = (
        TradingError,
        ValueError,
        OSError,
        httpx.HTTPError,
        sqlite3.Error,
        KeyError,
        TypeError,
        DecimalException,
        requests.RequestException,
        ClientError,
        ServerError,
        KeyringError,
        yaml.YAMLError,
    )
    journal = None
    try:
        if args.command in {"status", "pause", "resume"}:
            journal = Journal(args.db)
            if args.command != "status":
                journal.put("paused", args.command == "pause")
            emit(journal.status())
            return 0

        with httpx.Client(timeout=8, follow_redirects=False) as client:
            feed = Feed(client)
            if args.command == "scan":
                while True:
                    try:
                        emit(scan_result(feed))
                    except failures as error:
                        print(
                            f"Market scan failed ({type(error).__name__})",
                            file=sys.stderr,
                            flush=True,
                        )
                        if not args.interval:
                            return 1
                    if not args.interval:
                        return 0
                    sleep(args.interval)

            cfg = (
                Settings.model_validate(yaml.safe_load(args.config.read_text()))
                if args.config
                else None
            )
            if args.command == "run" and cfg is None:
                raise TradingError("Set --config to a file with the live markets and order limit")
            owner, signer, name = load_signer(cfg.owner if cfg else None, args.agent_key_file)
            gateway = AccountGateway(feed, owner, signer, name)
            valid_until = gateway.verify(force=True)
            if args.command == "check":
                emit({"status": "API wallet authorized", "approval_expires_ms": valid_until})
                return 0

            lock_dir = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local/state")) / "aitrading"
            lock_dir.mkdir(parents=True, exist_ok=True)
            with (lock_dir / f"{owner.lower()}.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                journal = Journal(args.db)
                if journal.get("owner", owner.lower()) != owner.lower():
                    raise TradingError("This journal belongs to a different Hyperliquid account")
                journal.put("owner", owner.lower())
                runner = Runner(feed, gateway, journal, cfg)
                while True:
                    try:
                        result = runner.step()
                    except failures as error:
                        result = {
                            "error": str(error)
                            if isinstance(error, TradingError)
                            else type(error).__name__
                        }
                    journal.put("last_cycle", {"time_ms": now_ms(), "result": result})
                    emit({"mode": "live", "result": result})
                    sleep(5 if journal.get("trade") else args.interval)
    except failures as error:
        message = str(error) if isinstance(error, TradingError) else type(error).__name__
        print(f"Hyperliquid autopilot: {message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if journal:
            journal.db.close()


if __name__ == "__main__":
    raise SystemExit(main())
