"""Record public Hyperliquid market observations in rotating SQLite files."""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import signal
import sqlite3
import time
import zlib
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

import httpx
import websocket

INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"
TAXONOMY_PATH = Path(__file__).with_name("research_taxonomy.json")
DAY_MS = 86_400_000
MIB = 1024 * 1024
LISTING_DAYS = 30
LISTING_LOOKBACK_DAYS = 90
LISTING_REQUEST_INTERVAL = 15
LISTING_RETRY_MS = 6 * 60 * 60_000
META_INTERVAL = 60
FLUSH_INTERVAL = 2
HEARTBEAT_INTERVAL = 30
PING_INTERVAL = 20
PONG_TIMEOUT = 45
ACK_TIMEOUT = 15
PART_BYTES = 16 * MIB
PART_NAME = re.compile(r"market-([0-9]{13})-[0-9a-f]{8}\.sqlite3")
DEPTH_BANDS = (5, 10, 25)
CHANNELS = ("trades", "l2Book")
SIDES = {"B": "buy", "A": "sell"}
CONTEXT_FIELDS = (
    "openInterest",
    "oraclePx",
    "markPx",
    "premium",
    "funding",
    "impactPxs",
    "dayNtlVlm",
    "midPx",
    "prevDayPx",
    "dayBaseVlm",
)


def now_ms():
    return time.time_ns() // 1_000_000


def json_text(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False)


def encode_payload(event):
    payload = json_text(event.payload)
    if event.kind in {"meta", "book"}:
        return zlib.compress(payload.encode())
    return payload


def decode_payload(payload):
    """Decode a payload returned by SQLite, including compressed market and book rows."""
    return json.loads(zlib.decompress(payload) if isinstance(payload, bytes) else payload)


def log(event, **details):
    print(json_text({"event": event, "received_ms": now_ms(), **details}), flush=True)


def decimal(value):
    if not isinstance(value, str):
        raise ValueError("Expected a decimal string")
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError("Expected a finite decimal")
    return number


def integer(value):
    if type(value) is not int or value < 0:
        raise ValueError("Expected a nonnegative integer")
    return value


@dataclass(frozen=True)
class Event:
    kind: str
    received_ms: int
    payload: dict
    coin: str | None = None
    exchange_ms: int | None = None
    trade_id: int | None = None


class Archive:
    """One writer, full SQLite transactions, and retention by whole closed file."""

    def __init__(self, directory, max_bytes, retention_days):
        self.directory = directory
        self.max_bytes = max_bytes
        self.retention_days = retention_days
        self.connection = None
        self.active_path = None
        self.active_day = None
        self.pending = []
        self.counts = Counter()
        self.size_bytes = 0
        self.last_commit_ms = None
        self.cache_path = directory / "listing-cache.json"
        self.lock = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = (self.directory / ".recorder.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock.close()
            raise RuntimeError("Another recorder owns this output directory") from exc
        return self

    def __exit__(self, error_type, error, traceback):
        try:
            if error_type is None:
                self.flush()
        finally:
            if self.connection is not None:
                self.connection.close()
            self.lock.close()

    def add(self, event):
        self.pending.append(event)

    def files(self):
        return sorted(
            path
            for path in self.directory.glob("market-*.sqlite3")
            if PART_NAME.fullmatch(path.name)
        )

    def open_part(self, stamp):
        day = stamp // DAY_MS
        if self.connection is not None:
            if self.active_day == day and self.active_path.stat().st_size < PART_BYTES:
                return
            self.connection.close()
            self.connection = None
        files = self.files()
        latest = files[-1] if files else None
        if (
            latest is not None
            and self.part_time(latest) // DAY_MS == day
            and latest.stat().st_size < PART_BYTES
        ):
            self.active_path = latest
        else:
            self.active_path = self.directory / f"market-{stamp:013d}-{uuid4().hex[:8]}.sqlite3"
        self.active_day = day
        self.connection = sqlite3.connect(self.active_path)
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                received_ms INTEGER NOT NULL,
                coin TEXT,
                exchange_ms INTEGER,
                trade_id INTEGER,
                payload BLOB NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS trade_identity
              ON observations (coin, exchange_ms, trade_id) WHERE trade_id IS NOT NULL;
            PRAGMA user_version = 2;
            """
        )

    @staticmethod
    def part_time(path):
        return int(path.name.split("-")[1])

    def prune(self, stamp):
        parts = self.files()
        size = sum(part.stat().st_size for part in parts)
        # The newest write to a partition can occur late in its first UTC day.
        cutoff_day = stamp // DAY_MS - self.retention_days
        for part in parts:
            if part == self.active_path:
                continue
            expired = self.part_time(part) // DAY_MS <= cutoff_day
            if size <= self.max_bytes and not expired:
                continue
            removed_bytes = part.stat().st_size
            part.unlink()
            size -= removed_bytes
            self.counts["pruned_parts"] += 1
            self.counts["pruned_bytes"] += removed_bytes
            log("archive_part_pruned", file=part.name, bytes=removed_bytes, expired=expired)
        self.size_bytes = size

    def flush(self, stamp=None):
        if not self.pending:
            return
        stamp = now_ms() if stamp is None else stamp
        self.open_part(stamp)
        written = Counter()
        with self.connection:
            for event in self.pending:
                cursor = self.connection.execute(
                    """
                    INSERT INTO observations (kind, received_ms, coin, exchange_ms, trade_id, payload)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (coin, exchange_ms, trade_id)
                      WHERE trade_id IS NOT NULL DO NOTHING
                    """,
                    (
                        event.kind,
                        event.received_ms,
                        event.coin,
                        event.exchange_ms,
                        event.trade_id,
                        encode_payload(event),
                    ),
                )
                written[event.kind if cursor.rowcount else "duplicate_trades_on_disk"] += 1
        self.counts.update(written)
        self.pending.clear()
        self.last_commit_ms = stamp
        self.prune(stamp)

    def load_listings(self):
        if not self.cache_path.is_file():
            return {}
        try:
            with self.cache_path.open(encoding="utf-8") as stream:
                return json.load(stream)
        except json.JSONDecodeError:
            log("listing_cache_unreadable")
            return {}

    def save_listings(self, listings):
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json_text(listings), encoding="utf-8")
        temporary.replace(self.cache_path)


def market_rows(response, taxonomy, listings, stamp):
    metadata, contexts = response
    assets = metadata["universe"]
    if not assets or len(assets) != len(contexts):
        raise ValueError("Native metadata and asset contexts do not align")
    markets = {}
    for asset, context in zip(assets, contexts, strict=True):
        coin = asset["name"]
        if not isinstance(coin, str) or not coin or ":" in coin or coin in markets:
            raise ValueError("Unexpected native market name")
        delisted = asset.get("isDelisted", False)
        if type(delisted) is not bool:
            raise ValueError("Unexpected delisting flag")
        if not isinstance(context, dict):
            raise ValueError("Unexpected asset context")
        identity = taxonomy.get(coin, {})
        listing = listings.get(coin)
        first = listing["first_daily_candle_ms"] if listing else None
        recent = None if first is None else 0 <= stamp - first < LISTING_DAYS * DAY_MS
        markets[coin] = {
            **{field: context.get(field) for field in CONTEXT_FIELDS},
            "isDelisted": delisted,
            "szDecimals": integer(asset["szDecimals"]),
            "category": identity.get("category", "unknown"),
            "category_provider_id": identity.get("provider_id"),
            "first_daily_candle_ms": first,
            "listing_history_status": listing["status"] if listing else "unchecked",
            "recent_listing_estimate": recent,
        }
    return markets


def volume_order(markets):
    candidates = []
    for coin, row in markets.items():
        if coin == "BTC" or row["isDelisted"]:
            continue
        volume = decimal(row["dayNtlVlm"])
        if volume < 0:
            raise ValueError("Unexpected negative daily notional")
        candidates.append((-volume, coin))
    return [coin for _, coin in sorted(candidates)]


def tracked_symbols(markets, limit):
    return tuple(
        coin
        for coin in volume_order(markets)
        if markets[coin]["category"] == "meme" or markets[coin]["recent_listing_estimate"]
    )[:limit]


def listing_evidence(response, stamp, coin):
    if not isinstance(response, list):
        raise ValueError("Expected a daily candle list")
    candles = []
    for row in response:
        opened = integer(row["t"])
        if row["s"] != coin or row["i"] != "1d" or opened % DAY_MS or opened > stamp:
            raise ValueError("Unexpected daily candle identity or timestamp")
        candles.append(opened)
    return {
        "first_daily_candle_ms": min(candles, default=None),
        "checked_ms": stamp,
        "lookback_days": LISTING_LOOKBACK_DAYS,
        "status": "daily_candle_estimate" if candles else "no_daily_candles",
    }


def trade_event(row, stamp):
    side = SIDES[row["side"]]
    if decimal(row["px"]) <= 0 or decimal(row["sz"]) <= 0:
        raise ValueError("Expected positive trade price and size")
    return Event(
        "trade",
        stamp,
        {"side": side, "px": row["px"], "sz": row["sz"]},
        row["coin"],
        integer(row["time"]),
        integer(row["tid"]),
    )


def book_event(row, stamp):
    book = row["levels"]
    if not isinstance(book, list) or len(book) != 2:
        raise ValueError("Expected two book sides")
    levels = []
    raw_levels = []
    for side in book:
        values = []
        raw_side = []
        for level in side:
            price, size = decimal(level["px"]), decimal(level["sz"])
            if price <= 0 or size <= 0:
                raise ValueError("Expected positive book price and size")
            values.append((price, size))
            raw_side.append({"px": level["px"], "sz": level["sz"], "n": integer(level["n"])})
        levels.append(values)
        raw_levels.append(raw_side)
    bids, asks = levels
    best_bid = max((price for price, _ in bids), default=None)
    best_ask = min((price for price, _ in asks), default=None)
    payload = {
        "bids": raw_levels[0],
        "asks": raw_levels[1],
        "mid_px": None,
        "spread_px": None,
        "spread_bps": None,
        "observed_depth": None,
    }
    if best_bid is not None and best_ask is not None:
        if best_bid > best_ask:
            raise ValueError("Crossed book")
        mid = (best_bid + best_ask) / 2
        spread = best_ask - best_bid
        bands = {}
        for band in DEPTH_BANDS:
            sides = {}
            max_distance = mid * band / 10_000
            for name, side in zip(("bid", "ask"), levels, strict=True):
                inside = [(px, sz) for px, sz in side if abs(px - mid) <= max_distance]
                sides[name] = {
                    "base_sz": str(sum(sz for _, sz in inside)),
                    "usd_ntl": str(sum(px * sz for px, sz in inside)),
                    "outermost_level_reaches_band": any(
                        abs(px - mid) >= max_distance for px, _ in side
                    ),
                }
            bands[str(band)] = sides
        payload.update(
            mid_px=str(mid),
            spread_px=str(spread),
            spread_bps=str(spread / mid * 10_000),
            observed_depth=bands,
        )
    return Event("book", stamp, payload, row["coin"], integer(row["time"]))


class PublicFeed:
    def __init__(self, recorder, book_interval):
        self.recorder = recorder
        self.book_bucket_ms = book_interval * 1000
        self.desired = set()
        self.subscribed = set()
        self.pending_acks = {}
        self.socket = None
        self.last_ping = 0.0
        self.last_pong = 0.0
        self.last_packet_ms = None
        self.reconnect_at = 0.0
        self.reconnect_delay = 1
        self.disconnected_since_ms = None
        self.trade_ids = OrderedDict()
        self.book_buckets = {}
        self.last_book_ms = {}
        self.last_trade_ms = {}

    def set_symbols(self, symbols):
        self.desired = {(channel, coin) for channel in CHANNELS for coin in symbols}

    def ready(self):
        return self.socket is not None and self.subscribed == self.desired and not self.pending_acks

    def close(self):
        if self.socket is not None:
            self.socket.shutdown()
            self.socket = None

    def disconnect(self, reason):
        self.close()
        self.subscribed.clear()
        self.pending_acks.clear()
        self.reconnect_at = time.monotonic() + self.reconnect_delay
        self.reconnect_delay = min(30, self.reconnect_delay * 2)
        self.recorder.counters["ws_disconnects"] += 1
        self.disconnected_since_ms = self.disconnected_since_ms or self.last_packet_ms or now_ms()
        self.recorder.health(
            "ws_disconnected",
            reason=reason,
            gap_start_ms=self.disconnected_since_ms,
            symbols=sorted({coin for _, coin in self.desired}),
        )

    def connect(self):
        self.socket = websocket.create_connection(WS_URL, timeout=5)
        self.socket.settimeout(0.5)
        self.last_pong = self.last_ping = time.monotonic()
        self.recorder.counters["ws_connections"] += 1
        self.recorder.health("ws_connected")

    def synchronize(self, monotonic):
        changes = [
            ("unsubscribe", self.subscribed - self.desired),
            ("subscribe", self.desired - self.subscribed),
        ]
        for method, subscriptions in changes:
            for channel, coin in sorted(subscriptions):
                self.socket.send(
                    json_text(
                        {
                            "method": method,
                            "subscription": {"type": channel, "coin": coin},
                        }
                    )
                )
                self.pending_acks[(method, channel, coin)] = monotonic
        self.subscribed = self.desired.copy()

    def message(self, raw, stamp):
        packet = json.loads(raw)
        channel = packet["channel"]
        if channel == "pong":
            self.last_pong = time.monotonic()
        elif channel == "subscriptionResponse":
            data = packet["data"]
            sub = data["subscription"]
            self.pending_acks.pop((data["method"], sub["type"], sub["coin"]), None)
        elif channel == "trades":
            if not isinstance(packet["data"], list):
                raise ValueError("Expected a trade array")
            for row in packet["data"]:
                try:
                    self.receive_trade(row, stamp)
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    self.recorder.counters["invalid_trade_rows"] += 1
        elif channel == "l2Book":
            self.receive_book(packet["data"], stamp)
        elif channel == "error":
            self.disconnect("provider_error")
        else:
            self.recorder.counters["unexpected_ws_channels"] += 1

    def receive_trade(self, row, stamp):
        if ("trades", row["coin"]) not in self.desired:
            self.recorder.counters["untracked_trades"] += 1
            return
        event = trade_event(row, stamp)
        key = (event.coin, event.exchange_ms, event.trade_id)
        self.last_trade_ms[event.coin] = stamp
        if key in self.trade_ids:
            self.trade_ids.move_to_end(key)
            self.recorder.counters["duplicate_trades_in_memory"] += 1
            return
        self.trade_ids[key] = None
        if len(self.trade_ids) > 100_000:
            self.trade_ids.popitem(last=False)
        self.recorder.archive.add(event)

    def receive_book(self, row, stamp):
        coin = row["coin"]
        if ("l2Book", coin) not in self.desired:
            self.recorder.counters["untracked_books"] += 1
            return
        bucket = integer(row["time"]) // self.book_bucket_ms
        previous = self.book_buckets.get(coin, -1)
        if bucket <= previous:
            counter = "downsampled_books" if bucket == previous else "out_of_order_books"
            self.recorder.counters[counter] += 1
            return
        self.recorder.archive.add(book_event(row, stamp))
        self.book_buckets[coin] = bucket
        self.last_book_ms[coin] = stamp

    def poll(self):
        monotonic = time.monotonic()
        try:
            if self.socket is None:
                if not self.desired or monotonic < self.reconnect_at:
                    return False
                self.connect()
                monotonic = time.monotonic()
            self.synchronize(monotonic)
            if monotonic - self.last_pong > PONG_TIMEOUT:
                self.disconnect("pong_timeout")
                return False
            if self.pending_acks and monotonic - min(self.pending_acks.values()) > ACK_TIMEOUT:
                self.disconnect("subscription_ack_timeout")
                return False
            if monotonic - self.last_ping >= PING_INTERVAL:
                self.socket.send(json_text({"method": "ping"}))
                self.last_ping = monotonic
            try:
                raw = self.socket.recv()
            except websocket.WebSocketTimeoutException:
                return False
            if not raw:
                self.disconnect("peer_closed")
                return False
            self.last_packet_ms = now_ms()
            self.message(raw, self.last_packet_ms)
            if self.ready():
                self.reconnect_delay = 1
                if self.disconnected_since_ms is not None:
                    self.recorder.health(
                        "ws_recovered",
                        gap_start_ms=self.disconnected_since_ms,
                        gap_end_ms=now_ms(),
                        replay_complete=False,
                    )
                    self.disconnected_since_ms = None
            return True
        except (OSError, websocket.WebSocketException) as exc:
            self.disconnect(type(exc).__name__)
            return False
        except (KeyError, TypeError, ValueError, InvalidOperation):
            self.recorder.counters["invalid_ws_packets"] += 1
            return True


class Recorder:
    def __init__(self, archive, top_n, book_interval):
        self.archive = archive
        self.top_n = top_n
        self.taxonomy = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
        self.listings = archive.load_listings()
        self.watched = ()
        self.market_names = set()
        self.new_markets = set()
        self.latest_meta = None
        self.last_meta_ms = None
        self.counters = Counter()
        self.feed = PublicFeed(self, book_interval)
        self.session = uuid4().hex
        self.stop = False

    def health(self, event, **details):
        log(event, session=self.session, **details)
        self.archive.add(
            Event("health", now_ms(), {"event": event, "session": self.session, **details})
        )

    def meta(self, response, stamp):
        markets = market_rows(response, self.taxonomy["assets"], self.listings, stamp)
        selected = tracked_symbols(markets, self.top_n)
        if self.market_names:
            added = markets.keys() - self.market_names
            removed = self.market_names - markets.keys()
            self.new_markets.update(added)
            if added or removed:
                self.health("native_universe_changed", added=sorted(added), removed=sorted(removed))
        self.market_names = set(markets)
        self.latest_meta = response
        self.last_meta_ms = stamp
        self.archive.add(
            Event(
                "meta",
                stamp,
                {
                    "type": "metaAndAssetCtxs",
                    "dex": "",
                    "markets": markets,
                    "tracked": selected,
                    "listing_recent_days": LISTING_DAYS,
                    "taxonomy_as_of": self.taxonomy["captured_at_utc"],
                },
            )
        )
        self.select(selected)

    def select(self, selected):
        if selected == self.watched:
            return
        self.watched = selected
        self.feed.set_symbols(selected)
        self.health("tracked_markets", symbols=selected)

    def listing_candidate(self, stamp):
        if self.latest_meta is None:
            return None
        markets = market_rows(self.latest_meta, self.taxonomy["assets"], self.listings, stamp)
        ranked = volume_order(markets)
        eligible = [
            coin
            for coin in ranked
            if coin not in self.listings
            or (
                self.listings[coin]["first_daily_candle_ms"] is None
                and stamp - self.listings[coin]["checked_ms"] >= LISTING_RETRY_MS
            )
        ]
        return next(
            (coin for coin in eligible if coin in self.new_markets),
            eligible[0] if eligible else None,
        )

    def listing(self, response, stamp, coin):
        evidence = listing_evidence(response, stamp, coin)
        self.listings[coin] = evidence
        self.new_markets.discard(coin)
        self.archive.add(Event("listing", stamp, evidence, coin))
        self.archive.flush()
        self.archive.save_listings(self.listings)
        markets = market_rows(self.latest_meta, self.taxonomy["assets"], self.listings, stamp)
        self.select(tracked_symbols(markets, self.top_n))

    @staticmethod
    def request(client, coin=None):
        parameters = {"type": "metaAndAssetCtxs", "dex": ""}
        if coin is not None:
            stamp = now_ms()
            parameters = {
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": "1d",
                    "startTime": stamp - LISTING_LOOKBACK_DAYS * DAY_MS,
                    "endTime": stamp,
                },
            }
        response = client.post(INFO_URL, json=parameters)
        stamp = now_ms()
        response.raise_for_status()
        return response.json(), stamp

    def capture(self, request, coin=None):
        try:
            payload, stamp = request()
            if coin is None:
                self.meta(payload, stamp)
            else:
                self.listing(payload, stamp, coin)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
            kind = "meta" if coin is None else "listing"
            self.counters[f"{kind}_errors"] += 1
            details = {"source": kind, "coin": coin, "reason": type(exc).__name__}
            if isinstance(exc, httpx.HTTPStatusError):
                details["status_code"] = exc.response.status_code
            self.health("public_http_error", **details)
            if coin is not None:
                self.listings[coin] = {
                    "first_daily_candle_ms": None,
                    "checked_ms": now_ms(),
                    "status": "request_error",
                }
            return False
        return True

    def heartbeat(self):
        self.archive.flush()
        self.health(
            "recorder_heartbeat",
            tracked=self.watched,
            ws_ready=self.feed.ready(),
            last_meta_ms=self.last_meta_ms,
            last_commit_ms=self.archive.last_commit_ms,
            ws_subscriptions_waiting=len(self.feed.pending_acks),
            gap_open_ms=self.feed.disconnected_since_ms,
            last_ws_packet_ms=self.feed.last_packet_ms,
            last_trade_ms={coin: self.feed.last_trade_ms.get(coin) for coin in self.watched},
            last_book_ms={coin: self.feed.last_book_ms.get(coin) for coin in self.watched},
            archive_bytes=self.archive.size_bytes,
            written=dict(self.archive.counts),
            counters=dict(self.counters),
        )

    def run(self, once, run_seconds):
        self.health(
            "recorder_started",
            output=str(self.archive.directory),
            top_n=self.top_n,
            listing_lookback_days=LISTING_LOOKBACK_DAYS,
            flush_interval_sec=FLUSH_INTERVAL,
        )
        with httpx.Client(timeout=5, trust_env=False) as client:
            if once:
                succeeded = self.capture(lambda: self.request(client))
                self.heartbeat()
                return 0 if succeeded else 1
            # HTTP runs separately so historical lookups do not block websocket receive.
            with ThreadPoolExecutor(max_workers=1) as http:
                try:
                    self.run_loop(http, client, run_seconds)
                finally:
                    self.feed.close()
        self.heartbeat()
        self.health("recorder_stopped")
        return 0

    def run_loop(self, http, client, run_seconds):
        started = time.monotonic()
        next_meta = next_listing = started
        next_flush = started + FLUSH_INTERVAL
        next_heartbeat = started + HEARTBEAT_INTERVAL
        pending = None
        coin = None
        while not self.stop:
            current = time.monotonic()
            if run_seconds is not None and current - started >= run_seconds:
                break
            if pending is not None and pending.done():
                self.capture(pending.result, coin)
                pending = None
            if pending is None:
                if current >= next_meta:
                    next_meta = current + META_INTERVAL
                    coin = None
                    pending = http.submit(self.request, client)
                elif current >= next_listing:
                    next_listing = current + LISTING_REQUEST_INTERVAL
                    coin = self.listing_candidate(now_ms())
                    if coin is not None:
                        pending = http.submit(self.request, client, coin)
            if current >= next_heartbeat:
                self.heartbeat()
                next_heartbeat = current + HEARTBEAT_INTERVAL
            if current >= next_flush or len(self.archive.pending) >= 5000:
                self.archive.flush()
                next_flush = current + FLUSH_INTERVAL
            if not self.feed.poll():
                time.sleep(0.05)
        if pending is not None:
            self.capture(pending.result, coin)


def bounded_int(minimum, maximum):
    def parse(raw):
        value = int(raw)
        if not minimum <= value <= maximum:
            raise argparse.ArgumentTypeError(f"Expected an integer from {minimum} to {maximum}")
        return value

    return parse


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", required=True, type=Path, help="Dedicated recorder data directory"
    )
    parser.add_argument(
        "--once", action="store_true", help="Persist one HTTP snapshot without WebSocket"
    )
    parser.add_argument(
        "--run-seconds", type=bounded_int(1, 3600), help="Stop after a bounded smoke run"
    )
    parser.add_argument(
        "--top-n", type=bounded_int(1, 25), default=8, help="Maximum tracked markets"
    )
    parser.add_argument(
        "--book-seconds", type=bounded_int(1, 60), default=5, help="Order-book sample interval"
    )
    parser.add_argument(
        "--max-mib", type=bounded_int(32, 100_000), default=384, help="Archive size limit"
    )
    parser.add_argument(
        "--retention-days", type=bounded_int(1, 3650), default=30, help="Archive retention"
    )
    args = parser.parse_args(argv)
    with Archive(args.output, args.max_mib * MIB, args.retention_days) as archive:
        recorder = Recorder(archive, args.top_n, args.book_seconds)

        def stop(*_args):
            recorder.stop = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        return recorder.run(args.once, args.run_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
