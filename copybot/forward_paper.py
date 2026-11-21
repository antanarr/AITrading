"""Prospective, public-data-only 1× and 3× paper portfolios."""

import argparse
import fcntl
import json
import signal
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from pathlib import Path

import httpx

from .forward_market import TAKER_FEE_RATE, PublicMarket, sweep_ioc
from .forward_signals import SignalLoader
from .perps import now_ms, number, round_price
from .research_models import DAY_HOURS, HOUR_MS, excluded_symbol

DAY_MS = DAY_HOURS * HOUR_MS
POLL_SECONDS = 10
DECISION_WINDOW_MS = 15 * 60_000
MAX_ORACLE_AGE_MS = 60_000
MAX_MARK_GAP_MS = 45_000
CAPITAL = Decimal(500)
MIN_NOTIONAL = Decimal(10)
ZERO = Decimal(0)
SCHEMA = "donchian-top20-sma50-v1"
MARKET_ERRORS = (httpx.HTTPError, ValueError, KeyError)


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, default=str, allow_nan=False), flush=True)


@dataclass
class Position:
    coin: str
    side: int
    decimals: int
    maximum_leverage: int
    quantity: Decimal
    entry: Decimal
    stop: Decimal
    entered_ms: int
    funding_through: int
    entry_fee: Decimal
    mark: Decimal
    oracle: Decimal
    mark_ms: int
    funding_open: Decimal = ZERO
    exit_reason: str | None = None
    exit_book_ms: int = 0

    def value(self):
        return self.side * self.quantity * (self.mark - self.entry)

    def collateral(self, leverage):
        return self.quantity * self.entry / leverage

    def liquidatable(self, leverage):
        maintenance = self.quantity * self.mark / (2 * self.maximum_leverage)
        return self.collateral(leverage) + self.funding_open + self.value() <= maintenance


@dataclass
class Portfolio:
    leverage: int
    positions: dict[str, Position] = field(default_factory=dict)
    net: Decimal = ZERO
    closed_net: Decimal = ZERO
    locked: Decimal = ZERO
    peak: Decimal = CAPITAL
    drawdown: Decimal = ZERO

    def equity(self):
        return CAPITAL + self.net + sum(position.value() for position in self.positions.values())

    def mark_drawdown(self):
        equity = self.equity()
        self.peak = max(self.peak, equity)
        self.drawdown = max(self.drawdown, self.peak - equity)

    def requested_quantity(self, side, decimals, price, atr):
        equity = self.equity() - self.locked
        free = CAPITAL + self.net - self.locked
        posted = ZERO
        same_side = ZERO
        for position in self.positions.values():
            collateral = position.collateral(self.leverage)
            free -= collateral + position.funding_open
            posted += collateral
            same_side += collateral if position.side == side else ZERO
        per_unit_risk = 2 * atr + price * (2 * TAKER_FEE_RATE + Decimal("0.0028"))
        collateral = min(equity - posted, equity * Decimal("0.60") - same_side)
        quantity = min(
            collateral * self.leverage / price,
            equity * Decimal("0.25") * self.leverage / price,
            equity * Decimal("0.01") * self.leverage / per_unit_risk,
            free / (price * (1 / Decimal(self.leverage) + TAKER_FEE_RATE)),
        )
        lot = Decimal(1).scaleb(-decimals)
        return max(ZERO, quantity).quantize(lot, rounding=ROUND_DOWN)

    def close_pnl(self, net, fees_already_paid, released_funding):
        self.net += net
        self.closed_net += net - fees_already_paid + released_funding
        self.locked = max(self.locked, self.closed_net / 4)


def restore_portfolio(row):
    for key in ("net", "closed_net", "locked", "peak", "drawdown"):
        row[key] = number(row[key])
    for coin, fields in row["positions"].items():
        for key in ("quantity", "entry", "stop", "entry_fee", "mark", "oracle", "funding_open"):
            fields[key] = number(fields[key])
        row["positions"][coin] = Position(**fields)
    return Portfolio(**row)


class PaperJournal:
    """One transaction publishes paper book state and its audit events together."""

    def __init__(self, path, stamp):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = path.with_suffix(".lock").open("a")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.db = sqlite3.connect(path)
        tables = {
            name for (name,) in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if tables - {"paper_state", "paper_events"}:
            self.close()
            raise ValueError("Use an independent paper journal")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                state TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_events (
                id INTEGER PRIMARY KEY,
                time_ms INTEGER NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            """
        )
        row = self.db.execute("SELECT state FROM paper_state WHERE singleton = 1").fetchone()
        if row:
            saved = json.loads(row[0])
            if saved["schema"] != SCHEMA:
                raise ValueError("Paper journal belongs to a different strategy version")
            self.next_boundary = saved["next_boundary"]
            self.books = [restore_portfolio(book) for book in saved["books"]]
        else:
            self.next_boundary = (stamp // DAY_MS + 1) * DAY_MS
            self.books = [Portfolio(1), Portfolio(3)]
            self.save(stamp, [("enrolled", {"first_boundary_ms": self.next_boundary})])

    def save(self, stamp, events):
        state = {
            "schema": SCHEMA,
            "next_boundary": self.next_boundary,
            "books": [asdict(book) for book in self.books],
        }
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO paper_state VALUES (1, ?)",
                (json.dumps(state, default=str),),
            )
            self.db.executemany(
                "INSERT INTO paper_events (time_ms,kind,payload) VALUES (?, ?, ?)",
                [(stamp, kind, json.dumps(data, default=str)) for kind, data in events],
            )
        for kind, data in events:
            emit(f"paper_{kind}", time_ms=stamp, **data)

    def close(self):
        self.db.close()
        self.lock.close()


def details(fill, book, reference):
    return {
        "requested": fill.requested_quantity,
        "filled": fill.quantity,
        "remaining": fill.remaining,
        "notional": fill.notional,
        "price": fill.average_price,
        "fee": fill.fee,
        "limit": fill.limit_price,
        "reference": reference,
        "l2_exchange_ms": book.exchange_ms,
        "l2_received_ms": book.received_ms,
        "levels": [asdict(level) for level in fill.fills],
    }


def entry(portfolio, order, decision, context, order_book, events):
    coin = order.coin
    leverage = portfolio.leverage
    if coin in portfolio.positions or len(portfolio.positions) >= 4:
        events.append(("entry_skipped", {"coin": coin, "leverage": leverage, "reason": "slots"}))
        return
    if (
        context.max_leverage is None
        or leverage > min(decision.market.max_leverage, context.max_leverage)
        or context.size_decimals != decision.market.decimals
    ):
        events.append(("entry_skipped", {"coin": coin, "leverage": leverage, "reason": "contract"}))
        return
    decimals = context.size_decimals
    side = order.side
    cap = number(order.limit)
    atr = number(order.atr)
    levels = order_book.asks if side > 0 else order_book.bids
    if not levels:
        events.append(("entry_skipped", {"coin": coin, "leverage": leverage, "reason": "empty L2"}))
        return
    budget_price = cap if side > 0 else max(cap, levels[0].price)
    quantity = portfolio.requested_quantity(side, decimals, budget_price, atr)
    if quantity * min(cap, levels[0].price) < MIN_NOTIONAL:
        events.append(("entry_skipped", {"coin": coin, "leverage": leverage, "reason": "minimum"}))
        return
    fill = sweep_ioc(
        order_book, buy=side > 0, quantity=quantity, limit_price=cap, size_decimals=decimals
    )
    record = {
        "coin": coin,
        "leverage": leverage,
        "side": side,
        **details(fill, order_book, number(decision.close)),
    }
    if not fill.quantity:
        events.append(("entry_unfilled", record))
        return
    price = fill.average_price
    stop = round_price(price - side * 2 * atr, decimals, ROUND_CEILING if side > 0 else ROUND_DOWN)
    if stop <= 0 or (price - stop) * side <= 0:
        events.append(("entry_skipped", {**record, "reason": "invalid stop"}))
        return
    stamp = order_book.received_ms
    portfolio.positions[coin] = Position(
        coin,
        side,
        decimals,
        context.max_leverage,
        fill.quantity,
        price,
        stop,
        stamp,
        stamp // HOUR_MS * HOUR_MS,
        fill.fee,
        context.mark,
        context.oracle,
        context.received_ms,
    )
    portfolio.net -= fill.fee
    events.append(("entry", {**record, "stop": stop, "collateral": fill.notional / leverage}))


def settle(portfolio, position, context, funding, events):
    boundary = position.funding_through + HOUR_MS
    while boundary < context.received_ms:
        candidates = [
            item for item in funding if boundary <= item.timestamp_ms < boundary + MAX_ORACLE_AGE_MS
        ]
        if not candidates:
            return False
        if len(candidates) != 1:
            raise ValueError(f"Unexpected funding interval for {position.coin}")
        observation = candidates[0]
        stamp = observation.timestamp_ms
        if stamp > context.received_ms:
            return False
        oracle, clock = min(
            [(position.oracle, position.mark_ms), (context.oracle, context.received_ms)],
            key=lambda sample: abs(sample[1] - stamp),
        )
        amount = -position.side * position.quantity * oracle * observation.rate
        if stamp > position.entered_ms:
            portfolio.net += amount
            position.funding_open += amount
            events.append(
                (
                    "funding",
                    {
                        "coin": position.coin,
                        "leverage": portfolio.leverage,
                        "funding_ms": stamp,
                        "oracle": oracle,
                        "oracle_age_ms": abs(clock - stamp),
                        "rate": observation.rate,
                        "amount": amount,
                        "coverage_gap": abs(clock - stamp) > MAX_ORACLE_AGE_MS,
                    },
                )
            )
        position.funding_through = boundary
        boundary += HOUR_MS
    return True


def exit_position(portfolio, position, order_book, events):
    if order_book.exchange_ms <= position.exit_book_ms:
        return
    position.exit_book_ms = order_book.exchange_ms
    side = position.side
    cap = position.mark * (1 - side * Decimal("0.10"))
    fill = sweep_ioc(
        order_book,
        buy=side < 0,
        quantity=position.quantity,
        limit_price=cap,
        size_decimals=position.decimals,
    )
    record = {
        "coin": position.coin,
        "leverage": portfolio.leverage,
        "reason": position.exit_reason,
        "side": side,
        **details(fill, order_book, position.mark),
    }
    if not fill.quantity:
        events.append(("exit_unfilled", record))
        return
    fraction = fill.quantity / position.quantity
    fees_allocated = position.entry_fee * fraction
    funding_allocated = position.funding_open * fraction
    gross = side * (fill.notional - fill.quantity * position.entry)
    portfolio.close_pnl(gross - fill.fee, fees_allocated, funding_allocated)
    position.quantity -= fill.quantity
    position.entry_fee -= fees_allocated
    position.funding_open -= funding_allocated
    if not position.quantity:
        del portfolio.positions[position.coin]
    events.append(
        (
            "exit",
            {
                **record,
                "gross": gross,
                "funding": funding_allocated,
                "closed_net": gross + funding_allocated - fill.fee - fees_allocated,
            },
        )
    )


class PaperWorker:
    def __init__(self, journal, market, loader, pool):
        self.journal = journal
        self.market = market
        self.loader = loader
        self.pool = pool
        self.job = None
        self.prior_universe = None
        self.last_heartbeat = 0
        self.last_funding_reads = {}

    def tick(self):
        contexts = self.market.contexts()
        stamp = now_ms()
        events = []
        boundary = self.journal.next_boundary
        books = self.journal.books
        held = {coin for book in books for coin in book.positions}
        if stamp < boundary and boundary - stamp <= 5 * 60_000 and self.prior_universe is None:
            try:
                self.prior_universe = self.loader.capture_universe()
            except MARKET_ERRORS as error:
                events.append(("census_gap", {"error": str(error)}))
        if self.job is None and boundary <= stamp < boundary + DECISION_WINDOW_MS:
            self.job = self.pool.submit(
                self.loader.load,
                boundary // HOUR_MS,
                open_symbols=held,
                prior_universe=self.prior_universe,
            )
        frame = None
        if stamp >= boundary + DECISION_WINDOW_MS or (self.job and self.job.done()):
            if self.job and not self.job.done():
                self.job.cancel()
            elif self.job:
                try:
                    frame = self.job.result()
                except MARKET_ERRORS as error:
                    events.append(("signal_error", {"error": str(error), "boundary_ms": boundary}))
            self.job = None
            if stamp >= boundary + DECISION_WINDOW_MS:
                frame = None
            events.append(
                (
                    "decision",
                    {
                        "boundary_ms": boundary,
                        "lag_ms": stamp - boundary,
                        "complete": bool(frame and frame.complete),
                        "roster": list(frame.roster) if frame else [],
                        "problems": dict(frame.problems)
                        if frame
                        else {"clock": "decision unavailable within window"},
                        "signals": [name for name, item in frame.decisions.items() if item.plan]
                        if frame
                        else [],
                        "signal_details": [asdict(item) for item in frame.decisions.values()]
                        if frame
                        else [],
                        "census": frame.census_basis if frame else None,
                    },
                )
            )
            self.journal.next_boundary = (stamp // DAY_MS + 1) * DAY_MS
            self.prior_universe = None

        depth = {}
        funding_cache = {}

        def order_book(coin):
            if coin not in depth:
                try:
                    depth[coin] = self.market.book(coin)
                except MARKET_ERRORS as error:
                    events.append(("book_gap", {"coin": coin, "error": str(error)}))
                    depth[coin] = None
            return depth[coin]

        for book in books:
            for coin, position in list(book.positions.items()):
                context = contexts.get(coin)
                if context is None:
                    events.append(("mark_gap", {"coin": coin, "reason": "market unavailable"}))
                    continue
                if context.received_ms - position.mark_ms > MAX_MARK_GAP_MS:
                    events.append(
                        (
                            "mark_gap",
                            {"coin": coin, "age_ms": context.received_ms - position.mark_ms},
                        )
                    )
                due = position.funding_through + HOUR_MS < context.received_ms
                synced = True
                if due:
                    key = (coin, position.funding_through)
                    if key not in funding_cache:
                        funding_cache[key] = ()
                        if stamp - self.last_funding_reads.get(coin, 0) >= 30_000:
                            self.last_funding_reads[coin] = stamp
                            try:
                                funding_cache[key] = self.market.funding(
                                    coin, position.funding_through + HOUR_MS, context.received_ms
                                )
                            except MARKET_ERRORS as error:
                                events.append(("funding_gap", {"coin": coin, "error": str(error)}))
                    synced = settle(book, position, context, funding_cache[key], events)
                position.mark = context.mark
                if context.max_leverage is not None:
                    position.maximum_leverage = context.max_leverage
                choice = frame.decisions.get(coin) if frame else None
                if (position.mark - position.stop) * position.side <= 0:
                    position.exit_reason = "protective stop"
                elif choice and (choice.exit_long if position.side > 0 else choice.exit_short):
                    position.exit_reason = position.exit_reason or "10-day channel"
                elif stamp >= position.entered_ms + 10 * DAY_MS:
                    position.exit_reason = position.exit_reason or "10-day timeout"
                if synced:
                    position.oracle, position.mark_ms = context.oracle, context.received_ms
                else:
                    # Keep the last oracle until the funding amount can be reconciled.
                    if context.received_ms - position.funding_through - HOUR_MS > MAX_ORACLE_AGE_MS:
                        events.append(
                            ("funding_gap", {"coin": coin, "reason": "settlement delayed"})
                        )
                    if position.exit_reason or position.liquidatable(book.leverage):
                        events.append(
                            ("funding_gap", {"coin": coin, "reason": "exit without a settled rate"})
                        )
                if position.liquidatable(book.leverage):
                    loss = -position.collateral(book.leverage)
                    book.close_pnl(
                        loss - position.funding_open, position.entry_fee, position.funding_open
                    )
                    del book.positions[coin]
                    events.append(
                        (
                            "liquidation_model",
                            {
                                "coin": coin,
                                "leverage": book.leverage,
                                "mark": position.mark,
                                "remaining_collateral_loss": loss,
                            },
                        )
                    )
                elif position.exit_reason and (observed_book := order_book(coin)) is not None:
                    exit_position(book, position, observed_book, events)
                events.append(
                    (
                        "mark",
                        {
                            "coin": coin,
                            "leverage": book.leverage,
                            "mark": context.mark,
                            "oracle": context.oracle,
                            "sample_ms": context.received_ms,
                        },
                    )
                )

        if frame and frame.complete:
            choices = sorted(
                (item for item in frame.decisions.values() if item.plan),
                key=lambda item: (-item.plan.signal_strength, item.market.coin),
            )
            for choice in choices:
                coin = choice.market.coin
                if excluded_symbol(coin) or ":" in coin:
                    raise ValueError(f"Unexpected paper market {coin}")
                context = contexts.get(coin)
                if context is None or now_ms() >= boundary + DECISION_WINDOW_MS:
                    events.append(("entry_skipped", {"coin": coin, "reason": "market or deadline"}))
                    continue
                if (observed_book := order_book(coin)) is not None:
                    if observed_book.received_ms >= boundary + DECISION_WINDOW_MS:
                        events.append(
                            ("entry_skipped", {"coin": coin, "reason": "book after deadline"})
                        )
                        continue
                    for book in books:
                        entry(book, choice.plan, choice, context, observed_book, events)

        for book in books:
            book.mark_drawdown()
        if stamp - self.last_heartbeat >= MAX_ORACLE_AGE_MS:
            self.last_heartbeat = stamp
            events.append(
                (
                    "heartbeat",
                    {
                        "mode": "public_forward_paper",
                        "markets": len(contexts),
                        "next_boundary_ms": self.journal.next_boundary,
                        "portfolios": [
                            {
                                "leverage": book.leverage,
                                "equity": book.equity(),
                                "open": len(book.positions),
                                "drawdown": book.drawdown,
                            }
                            for book in books
                        ],
                    },
                )
            )
        if events or held:
            self.journal.save(stamp, events)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--probe", action="store_true", help="Read current inputs and exit")
    args = parser.parse_args(argv)

    with httpx.Client(timeout=10, follow_redirects=False) as client, ThreadPoolExecutor(1) as pool:
        market = PublicMarket(client)
        loader = SignalLoader(market.info, rate_weight_per_minute=720)
        journal = PaperJournal(args.journal, now_ms())
        worker = PaperWorker(journal, market, loader, pool)
        stopping = False

        def stop(*_):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            probe = market.book("SOL")
            emit(
                "paper_ready",
                exchange_book_ms=probe.exchange_ms,
                book_bids=len(probe.bids),
                book_asks=len(probe.asks),
                base_taker_fee=str(TAKER_FEE_RATE),
                first_boundary_ms=journal.next_boundary,
            )
            while not stopping:
                try:
                    worker.tick()
                except MARKET_ERRORS as error:
                    if args.probe:
                        raise
                    emit("paper_public_data_error", error=str(error))
                if args.probe:
                    break
                time.sleep(POLL_SECONDS)
        finally:
            journal.close()


if __name__ == "__main__":
    main()
