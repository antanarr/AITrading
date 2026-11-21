"""Audit prospective maker markouts from an offline public recorder archive."""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
import zlib
from bisect import bisect_right
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from heapq import merge
from pathlib import Path
from statistics import mean

from .research_models import excluded_symbol

PROSPECTIVE_START = date(2026, 11, 22)
MIN_DAYS = 7
MIN_ANCHORS = 500
MINUTE_MS = 60_000
DAY_MS = 1_440 * MINUTE_MS
ANCHOR_MS = 16 * MINUTE_MS
TTL_MS = MINUTE_MS
ACTIVATION_MS = 250
BOOK_AGE_MS = 10_000
MAX_BOOK_GAP_MS = 20_000
COVERAGE_SLOT_MS = 5 * MINUTE_MS
COVERAGE_EDGE_MS = 2 * MINUTE_MS
HORIZONS = (1, 5, 15)
PRIMARY_HORIZON = 5
COVERAGE_KINDS = ("meta", "health", "book")
MAKER_BPS = Decimal("1.5")
TAKER_BPS = Decimal("4.5")
QUOTE_USD = Decimal(10)
META_AGE_MS = 75_000
OI_LOOKBACK_MS = 5 * MINUTE_MS
FLOW_IMBALANCE = Decimal("0.50")
FLOW_NOTIONAL = Decimal(2500)
DEPTH_IMBALANCE = Decimal("0.20")
FLUSH_RETURN = Decimal("-0.0175")
FLUSH_OI_CHANGE = Decimal("-0.03")
FLUSH_PREMIUM_BPS = Decimal(-10)
FADE_PREMIUM_BPS = Decimal(20)
CONDITIONAL_MEAN_BPS = 3
CONDITIONAL_MARKETS = 4
CONDITIONAL_SIGNALS = ("taker_flow_depth", "oi_flush", "mark_oracle_fade")
PART_NAME = re.compile(r"market-[0-9]{13}-[0-9a-f]{8}\.sqlite3")
PARTITION_QUERY = """
    SELECT CASE
               WHEN kind = 'book' AND (
                   typeof(exchange_ms) <> 'integer' OR exchange_ms <= 0 OR exchange_ms > received_ms
               ) THEN 'invalid_book'
               ELSE kind
           END AS source, received_ms / :day AS day, received_ms / :slot AS slot,
           MIN(received_ms), MAX(received_ms)
    FROM observations
    WHERE received_ms >= :start AND received_ms < :end
      AND kind IN ('meta', 'health', 'book', 'trade')
    GROUP BY source, day, slot
"""


class StudyError(ValueError):
    """The supplied archive or dates cannot support the requested study."""


def day_ms(day):
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) * 1000


def utc_today():
    return datetime.now(UTC).date()


def open_readonly(path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return closing(connection)


def validate_range(start, end, today):
    if start < PROSPECTIVE_START:
        raise StudyError(f"Start must be on or after {PROSPECTIVE_START.isoformat()} UTC.")
    if end > today:
        raise StudyError(f"Exclusive end must be on or before {today.isoformat()} UTC.")
    if (end - start).days < MIN_DAYS:
        raise StudyError(f"Select at least {MIN_DAYS} complete UTC days using the exclusive end.")


def coverage(directory, start, end, today):
    """Use recording timestamps only. Price, size, and health payloads stay unread."""
    validate_range(start, end, today)
    if not directory.is_dir():
        raise StudyError(f"The recorder archive directory does not exist: {directory}")
    parts = sorted(
        part
        for part in directory.iterdir()
        if PART_NAME.fullmatch(part.name) and part.is_file() and not part.is_symlink()
    )
    slots = defaultdict(set)
    edges = {}
    by_day = defaultdict(list)
    parameters = {
        "start": day_ms(start),
        "end": day_ms(end),
        "day": DAY_MS,
        "slot": COVERAGE_SLOT_MS,
    }
    for part in parts:
        part_days = set()
        with open_readonly(part) as db:
            for kind, day, slot, first, last in db.execute(PARTITION_QUERY, parameters):
                part_days.add(day)
                if kind not in COVERAGE_KINDS:
                    continue
                key = (day, kind)
                slots[key].add(slot)
                previous = edges.get(key, (first, last))
                edges[key] = (min(previous[0], first), max(previous[1], last))
        for day in part_days:
            by_day[day].append(part)
    days = {}
    expected_slots = DAY_MS // COVERAGE_SLOT_MS
    for day in range(parameters["start"] // DAY_MS, parameters["end"] // DAY_MS):
        first_ms, end_ms = day * DAY_MS, (day + 1) * DAY_MS
        date_label = datetime.fromtimestamp(first_ms // 1000, UTC).date().isoformat()
        day_details = {}
        for kind in COVERAGE_KINDS:
            first, last = edges.get((day, kind), (None, None))
            populated = len(slots[(day, kind)])
            day_details[kind] = {
                "five_minute_bins_present": populated,
                "first_received_ms": first,
                "last_received_ms": last,
                "complete": (
                    populated == expected_slots
                    and first - first_ms <= COVERAGE_EDGE_MS
                    and end_ms - last <= COVERAGE_EDGE_MS
                ),
            }
        days[date_label] = {
            "complete": all(item["complete"] for item in day_details.values()),
            **day_details,
        }
    complete = sum(day["complete"] for day in days.values())
    report = {
        "status": "coverage_ready" if complete == len(days) else "coverage_blocked",
        "utc_start": start.isoformat(),
        "utc_end_exclusive": end.isoformat(),
        "required_complete_days": max(MIN_DAYS, len(days)),
        "complete_days": complete,
        "archive_parts": len(parts),
        "required_five_minute_bins_per_day": expected_slots,
        "required_edge_proximity_ms": COVERAGE_EDGE_MS,
        "days": days,
    }
    return report, by_day


def decoded(payload):
    return json.loads(zlib.decompress(payload) if isinstance(payload, bytes) else payload)


def positive(value):
    if not isinstance(value, str):
        raise ValueError("Expected a positive decimal string.")
    number = Decimal(value)
    if not number.is_finite() or number <= 0:
        raise ValueError("Expected a positive finite value.")
    return number


def book_levels(values, side):
    return sorted(
        ((positive(level["px"]), level["sz"]) for level in values[side]),
        key=lambda level: level[0],
        reverse=side == "bids",
    )


@dataclass(frozen=True, slots=True)
class Book:
    exchange_ms: int
    received_ms: int
    bid: Decimal
    bid_size: Decimal
    ask: Decimal
    ask_size: Decimal
    raw: bytes | str

    @property
    def mid(self):
        return (self.bid + self.ask) / 2

    def depth_imbalance(self):
        values = decoded(self.raw)
        bids = book_levels(values, "bids")[:5]
        asks = book_levels(values, "asks")[:5]
        if len(bids) < 5 or len(asks) < 5:
            return None
        bid_depth = sum(price * positive(size) for price, size in bids)
        ask_depth = sum(price * positive(size) for price, size in asks)
        return (bid_depth - ask_depth) / (bid_depth + ask_depth)

    def exit_vwap(self, direction, quantity):
        side = "bids" if direction == 1 else "asks"
        remaining = quantity
        notional = Decimal(0)
        for price, raw_size in book_levels(decoded(self.raw), side):
            size = min(positive(raw_size), remaining)
            notional += price * size
            remaining -= size
            if not remaining:
                return notional / quantity
        return None


@dataclass(slots=True)
class Books:
    times: list[int]
    rows: list[Book | None]

    def at(self, target):
        position = bisect_right(self.times, target) - 1
        if position < 0:
            return None
        book = self.rows[position]
        if book is None or not (0 <= target - book.exchange_ms <= BOOK_AGE_MS):
            return None
        return book

    def covers_ttl(self, anchor, initial):
        return self.covers(anchor, anchor + TTL_MS, initial)

    def covers(self, start, end, initial):
        previous = initial.received_ms
        for index in range(bisect_right(self.times, start), bisect_right(self.times, end)):
            book = self.rows[index]
            if book is None or book.received_ms - previous > MAX_BOOK_GAP_MS:
                return False
            if book.received_ms - book.exchange_ms > BOOK_AGE_MS:
                return False
            previous = book.received_ms
        return end - previous <= MAX_BOOK_GAP_MS


def read_book(exchange, received, payload):
    if not 0 < exchange <= received:
        raise ValueError("Expected exchange time at or before receipt.")
    values = decoded(payload)
    bids = book_levels(values, "bids")
    asks = book_levels(values, "asks")
    if not bids or not asks or bids[0][0] >= asks[0][0]:
        raise ValueError("Expected a two-sided book with a positive spread.")
    # Reject a corrupted deeper level before it can affect a flow signal or an exit.
    for _, size in (*bids, *asks):
        positive(size)
    bid, bid_size = bids[0]
    ask, ask_size = asks[0]
    return Book(exchange, received, bid, positive(bid_size), ask, positive(ask_size), payload)


@dataclass(slots=True)
class Window:
    anchor: int
    coin: str
    book: Book
    directions: tuple[int, ...] = (1, -1)
    hits: dict[int, tuple[int, int]] = field(default_factory=dict)
    invalid_trade: bool = False

    def consider(self, exchange, received, aggressor, price):
        if not self.anchor + ACTIVATION_MS < exchange <= received < self.anchor + TTL_MS:
            return
        direction = 0
        if aggressor == "sell" and price < self.book.bid:
            direction = 1
        elif aggressor == "buy" and price > self.book.ask:
            direction = -1
        if direction in self.directions and (
            direction not in self.hits or (exchange, received) < self.hits[direction]
        ):
            self.hits[direction] = (exchange, received)


@dataclass(slots=True)
class Outcome:
    direction: int
    markouts: dict[int, float]
    roundtrips: dict[int, float]


def outcome(window, direction, books, gaps=()):
    entry_price = window.book.bid if direction == 1 else window.book.ask
    quantity = QUOTE_USD / entry_price
    fill_ms, _ = window.hits[direction]
    markouts, roundtrips = {}, {}
    for horizon in HORIZONS:
        target = fill_ms + horizon * MINUTE_MS
        end = books.at(target)
        if end is None or overlaps_gap(gaps, target, target + 1):
            continue
        markouts[horizon] = float(direction * (end.mid / entry_price - 1) * 10_000 - MAKER_BPS)
        exit_price = end.exit_vwap(direction, quantity)
        if exit_price is not None:
            price_ratio = exit_price / entry_price
            roundtrips[horizon] = float(
                direction * (price_ratio - 1) * 10_000 - MAKER_BPS - TAKER_BPS * price_ratio
            )
    return Outcome(direction, markouts, roundtrips)


def read_slice(parts, left, right, kind, *, fills_only=False, ordered=False):
    query = """
        SELECT coin, exchange_ms, received_ms, trade_id, payload
        FROM observations
        WHERE kind = :kind AND received_ms >= :left AND received_ms < :right
    """
    if fills_only:
        query += """
            AND (exchange_ms % :anchor BETWEEN :activation + 1 AND :ttl - 1
                 OR typeof(exchange_ms) <> 'integer' OR exchange_ms <= 0)
        """
    if ordered:
        query += " ORDER BY received_ms"

    def rows(part):
        with open_readonly(part) as db:
            yield from db.execute(
                query,
                {
                    "kind": kind,
                    "left": left,
                    "right": right,
                    "anchor": MINUTE_MS,
                    "ttl": TTL_MS,
                    "activation": ACTIVATION_MS,
                },
            )

    if ordered:
        yield from merge(*(rows(part) for part in parts), key=lambda row: row[2])
    else:
        for part in parts:
            yield from rows(part)


def book_series(rows, counters):
    by_coin = defaultdict(list)
    for coin, exchange, received, _, payload in rows:
        if not isinstance(coin, str) or not coin or ":" in coin or excluded_symbol(coin):
            continue
        try:
            book = read_book(exchange, received, payload)
        except (ValueError, TypeError, KeyError, InvalidOperation, zlib.error):
            counters["invalid_or_one_sided_books"] += 1
            book = None
        by_coin[coin].append((received, book))
    result = {}
    for coin, series in by_coin.items():
        series.sort(key=lambda pair: pair[0])
        times, books = zip(*series, strict=True)
        result[coin] = Books(list(times), list(books))
    return result


def source_gaps(rows, day_end, counters):
    """Capture feed disconnects and recording restarts, with sampled readiness evidence."""
    intervals = []
    opened = None
    for _, _, stamp, _, raw in sorted(rows, key=lambda row: row[2]):
        try:
            event = decoded(raw)
            kind = event["event"]
        except (ValueError, TypeError, KeyError, zlib.error):
            counters["invalid_health_events"] += 1
            intervals.append((stamp - COVERAGE_SLOT_MS, stamp + COVERAGE_SLOT_MS))
            continue
        if kind in {"ws_disconnected", "recorder_started", "recorder_stopped"}:
            start = event.get("gap_start_ms", stamp)
            if type(start) is not int or start > stamp:
                start = stamp
            opened = min(opened, start) if opened is not None else start
        elif kind == "ws_recovered":
            start = event.get("gap_start_ms", stamp)
            if type(start) is not int or start > stamp:
                start = stamp
            intervals.append((min(opened, start) if opened is not None else start, stamp))
            opened = None
        elif kind == "recorder_heartbeat":
            open_gap = event.get("gap_open_ms")
            if type(open_gap) is int and open_gap <= stamp:
                opened = min(opened, open_gap) if opened is not None else open_gap
            if event.get("ws_ready") is True and opened is not None:
                intervals.append((opened, stamp))
                opened = None
            elif event.get("ws_ready") is False and opened is None:
                opened = stamp
    if opened is not None:
        intervals.append((opened, day_end))
    return intervals


def overlaps_gap(gaps, start, end):
    return any(left < end and right >= start for left, right in gaps)


def eligible_windows(start, books, gaps, counters):
    end = start + DAY_MS
    windows = {}
    for anchor in range(start, end, ANCHOR_MS):
        if overlaps_gap(gaps, anchor, anchor + TTL_MS):
            counters["clock_anchors_with_source_gap"] += 1
            continue
        for coin, series in books.items():
            book = series.at(anchor)
            if book is None:
                counters["market_windows_missing_fresh_quote"] += 1
            elif min(book.bid * book.bid_size, book.ask * book.ask_size) < QUOTE_USD:
                counters["market_windows_insufficient_entry_depth"] += 1
            elif not series.covers_ttl(anchor, book):
                counters["market_windows_incomplete_fill_coverage"] += 1
            else:
                windows[(coin, anchor)] = Window(anchor, coin, book)
    return windows


def trade_values(payload):
    trade = decoded(payload)
    side = trade["side"]
    if side not in {"buy", "sell"}:
        raise ValueError("Unknown aggressor.")
    return side, positive(trade["px"]), positive(trade["sz"])


def flow_series(rows, coins, counters):
    """Aggregate the last exchange minute using only trades received by its end."""
    result = {}
    totals = defaultdict(lambda: [Decimal(0), Decimal(0)])
    invalid = set()
    seen = {}
    active_anchor = None

    def publish():
        for coin, (signed, gross) in totals.items():
            if coin not in invalid:
                result[(coin, active_anchor)] = (signed, gross)
        totals.clear()
        seen.clear()
        invalid.clear()

    for coin, exchange, received, trade_id, payload in rows:
        if coin not in coins:
            continue
        if type(received) is not int:
            counters["invalid_flow_trade_rows"] += 1
            continue
        anchor = (received + MINUTE_MS - 1) // MINUTE_MS * MINUTE_MS
        if anchor != active_anchor:
            publish()
            active_anchor = anchor
        if type(exchange) is int and 0 < exchange <= anchor - MINUTE_MS:
            continue
        try:
            if type(exchange) is not int or not 0 < exchange <= received:
                raise ValueError("Invalid trade clocks.")
            if type(trade_id) is not int or trade_id < 0:
                raise ValueError("Invalid trade identity.")
            side, price, size = trade_values(payload)
            key = (coin, exchange, trade_id)
            values = (side, price, size)
            if key in seen:
                if seen[key] != values:
                    raise ValueError("Conflicting trade identity.")
                continue
            seen[key] = values
        except (ValueError, TypeError, KeyError, InvalidOperation, zlib.error):
            invalid.add(coin)
            counters["invalid_flow_trade_rows"] += 1
            continue
        value = price * size
        totals[coin][0] += value if side == "buy" else -value
        totals[coin][1] += value
    publish()
    return result


@dataclass(frozen=True, slots=True)
class MarketState:
    mark: Decimal
    oracle: Decimal | None
    oi: Decimal | None

    @property
    def premium_bps(self):
        return (self.mark / self.oracle - 1) * 10_000 if self.oracle is not None else None


@dataclass(slots=True)
class MarketStates:
    times: list[int]
    source_times: list[int]
    snapshots: list[dict[str, MarketState]]

    def at(self, coin, target):
        index = bisect_right(self.times, target) - 1
        if index < 0 or target - self.source_times[index] > META_AGE_MS:
            return None
        return self.snapshots[index].get(coin)


def optional_decimal(value, *, allow_zero=False):
    try:
        if allow_zero and isinstance(value, str) and value:
            number = Decimal(value)
            if number.is_finite() and number == 0:
                return number
        return positive(value)
    except (ValueError, InvalidOperation):
        return None


def market_states(rows, coins, counters):
    times, source_times, snapshots = [], [], []
    for _, exchange, received, _, raw in sorted(rows, key=lambda row: row[2]):
        states = {}
        try:
            if exchange is not None and (type(exchange) is not int or not 0 < exchange <= received):
                raise ValueError("Invalid context clocks.")
            markets = decoded(raw)["markets"]
            if not isinstance(markets, dict):
                raise ValueError("Expected an asset context map.")
        except (ValueError, TypeError, KeyError, zlib.error):
            counters["invalid_meta_rows"] += 1
            markets = {}
        for coin in coins & markets.keys():
            values = markets[coin]
            if not isinstance(values, dict):
                counters["invalid_meta_market_rows"] += 1
                continue
            if values.get("isDelisted"):
                continue
            mark = optional_decimal(values.get("markPx"))
            if mark is None:
                continue
            states[coin] = MarketState(
                mark,
                optional_decimal(values.get("oraclePx")),
                optional_decimal(values.get("openInterest"), allow_zero=True),
            )
        times.append(received)
        source_times.append(exchange if states and exchange is not None else received)
        snapshots.append(states)
    return MarketStates(times, source_times, snapshots)


def signal_directions(coin, anchor, book, series, flows, states, counters):
    directions = {}
    signed, gross = flows.get((coin, anchor), (0, 0))
    if gross >= FLOW_NOTIONAL:
        initial = series.at(anchor - MINUTE_MS)
        imbalance = book.depth_imbalance()
        if (
            initial is None
            or imbalance is None
            or not series.covers(anchor - MINUTE_MS, anchor, initial)
        ):
            counters["flow_depth_context_unavailable"] += 1
        elif signed / gross >= FLOW_IMBALANCE and imbalance >= DEPTH_IMBALANCE:
            directions["taker_flow_depth"] = 1
        elif signed / gross <= -FLOW_IMBALANCE and imbalance <= -DEPTH_IMBALANCE:
            directions["taker_flow_depth"] = -1
    now = states.at(coin, anchor)
    if now is None or now.premium_bps is None:
        counters["fresh_mark_oracle_unavailable"] += 1
        return directions
    premium = now.premium_bps
    if abs(premium) >= FADE_PREMIUM_BPS:
        directions["mark_oracle_fade"] = -1 if premium > 0 else 1
    past = states.at(coin, anchor - OI_LOOKBACK_MS)
    if past is None or not past.oi or now.oi is None:
        counters["fresh_oi_history_unavailable"] += 1
    elif (
        now.mark / past.mark - 1 <= FLUSH_RETURN
        and now.oi / past.oi - 1 <= FLUSH_OI_CHANGE
        and premium <= FLUSH_PREMIUM_BPS
    ):
        directions["oi_flush"] = 1
    return directions


def conditional_windows(start, books, gaps, flows, states, cooldown, diagnostics):
    windows = {name: {} for name in CONDITIONAL_SIGNALS}
    for anchor in range(start, start + DAY_MS, MINUTE_MS):
        for coin, series in books.items():
            book = series.at(anchor)
            if book is None:
                continue
            signals = signal_directions(
                coin,
                anchor,
                book,
                series,
                flows,
                states,
                diagnostics["data"],
            )
            for name, direction in signals.items():
                counts = diagnostics[name]
                counts["signal_hits"] += 1
                if anchor < cooldown.get((coin, name), 0):
                    counts["hits_in_cooldown"] += 1
                    continue
                touch = book.bid * book.bid_size if direction == 1 else book.ask * book.ask_size
                if touch < QUOTE_USD:
                    counts["insufficient_entry_depth"] += 1
                    continue
                # Once posted, a quote consumes cooldown even if it later proves unobservable.
                cooldown[(coin, name)] = anchor + ANCHOR_MS
                counts["quote_attempts_before_quality_filter"] += 1
                has_gap = overlaps_gap(gaps, anchor, anchor + TTL_MS)
                if name == "taker_flow_depth":
                    has_gap |= overlaps_gap(gaps, anchor - MINUTE_MS, anchor)
                if has_gap or not series.covers_ttl(anchor, book):
                    counts["incomplete_source_coverage"] += 1
                    continue
                windows[name][(coin, anchor)] = Window(anchor, coin, book, (direction,))
    return windows


def observe_trades(rows, windows, counters):
    identity = {}
    for coin, exchange, received, trade_id, payload in rows:
        if type(received) is not int or type(exchange) is not int or exchange <= 0:
            counters["invalid_trade_clocks"] += 1
            if type(received) is int:
                for window in windows.get((coin, received // MINUTE_MS * MINUTE_MS), ()):
                    window.invalid_trade = True
            continue
        anchor = exchange // MINUTE_MS * MINUTE_MS
        candidates = windows.get((coin, anchor))
        if not candidates:
            continue
        if exchange > received or type(trade_id) is not int or trade_id < 0:
            for window in candidates:
                window.invalid_trade = True
            counters["invalid_trade_rows"] += 1
            continue
        if received >= anchor + TTL_MS:
            for window in candidates:
                window.invalid_trade = True
            counters["late_trades_in_fill_window"] += 1
            continue
        try:
            side, price, size = trade_values(payload)
        except (ValueError, TypeError, KeyError, InvalidOperation, zlib.error):
            for window in candidates:
                window.invalid_trade = True
            counters["invalid_trade_rows"] += 1
            continue
        key = (coin, exchange, trade_id)
        if key in identity and identity[key] != (side, price, size):
            for window in candidates:
                window.invalid_trade = True
            counters["conflicting_trade_ids"] += 1
            continue
        identity[key] = (side, price, size)
        for window in candidates:
            window.consider(exchange, received, side, price)


@dataclass(slots=True)
class Group:
    eligible: int = 0
    quotes: int = 0
    outcomes: list[Outcome] = field(default_factory=list)

    def details(self, include_metrics):
        result = {
            "eligible_market_windows": self.eligible,
            "maker_quotes": self.quotes,
            "strict_through_proxy_fills": len(self.outcomes),
            "proxy_fills_by_maker_side": {
                "buy": sum(row.direction == 1 for row in self.outcomes),
                "sell": sum(row.direction == -1 for row in self.outcomes),
            },
        }
        result["proxy_fill_fraction"] = (
            round(len(self.outcomes) / self.quotes, 6) if self.quotes else None
        )
        result["horizons"] = {}
        for horizon in HORIZONS:
            marks = [row.markouts[horizon] for row in self.outcomes if horizon in row.markouts]
            exits = [row.roundtrips[horizon] for row in self.outcomes if horizon in row.roundtrips]
            result["horizons"][str(horizon)] = {
                "markout_observations": len(marks),
                "avg_markout_after_entry_fee_bps": (
                    round(mean(marks), 6) if include_metrics and marks else None
                ),
                "executable_exit_observations": len(exits),
                "executable_exit_fraction": (
                    round(len(exits) / len(self.outcomes), 6) if self.outcomes else None
                ),
                "avg_executable_roundtrip_net_bps": (
                    round(mean(exits), 6) if include_metrics and exits else None
                ),
            }
        return result


def primary_returns(group):
    return [
        item.roundtrips[PRIMARY_HORIZON]
        for item in group.outcomes
        if PRIMARY_HORIZON in item.roundtrips
    ]


def cluster_interval(groups):
    clusters = []
    for group in groups:
        marks = primary_returns(group)
        if marks:
            clusters.append((sum(marks), len(marks)))
    result = {"represented_utc_days": len(clusters), "ci95_bps": None}
    if len(clusters) < MIN_DAYS:
        return result
    samples = []
    rng = random.Random(31)
    for _ in range(5000):
        draw = rng.choices(clusters, k=len(clusters))
        samples.append(sum(total for total, _ in draw) / sum(count for _, count in draw))
    samples.sort()
    result["ci95_bps"] = [round(samples[125], 6), round(samples[4874], 6)]
    return result


@dataclass(slots=True)
class Cohort:
    day_groups: dict[str, Group] = field(default_factory=dict)
    market_groups: dict[str, Group] = field(default_factory=lambda: defaultdict(Group))
    anchors: set[int] = field(default_factory=set)

    def add_day(self, day, windows, books, gaps, counters):
        day_group = self.day_groups[day] = Group()
        for coin in books:
            self.market_groups.setdefault(coin, Group())
        for window in windows.values():
            if window.invalid_trade:
                counters["market_windows_excluded_for_trade_quality"] += 1
                continue
            self.anchors.add(window.anchor)
            market_group = self.market_groups[window.coin]
            fills = [outcome(window, side, books[window.coin], gaps) for side in window.hits]
            for group in (day_group, market_group):
                group.eligible += 1
                group.quotes += len(window.directions)
                group.outcomes.extend(fills)

    def details(self, *, conditional=False):
        total = Group(
            eligible=sum(group.eligible for group in self.day_groups.values()),
            quotes=sum(group.quotes for group in self.day_groups.values()),
            outcomes=[row for group in self.day_groups.values() for row in group.outcomes],
        )
        enough_anchors = len(self.anchors) >= MIN_ANCHORS
        primary = primary_returns(total)
        clustered = cluster_interval(self.day_groups.values()) if enough_anchors else None
        markets = {coin: primary_returns(group) for coin, group in self.market_groups.items()}
        positive_markets = sorted(
            coin for coin, marks in markets.items() if marks and mean(marks) > 0
        )
        if not enough_anchors:
            status = "insufficient_eligible_windows"
        elif not total.outcomes:
            status = "zero_proxy_fills"
        elif clustered["ci95_bps"] is None:
            status = "insufficient_executable_exit_days"
        elif clustered["ci95_bps"][0] <= 0 or (
            conditional and mean(primary) <= CONDITIONAL_MEAN_BPS
        ):
            status = "no_positive_candidate"
        elif conditional and len(positive_markets) < CONDITIONAL_MARKETS:
            status = "insufficient_positive_market_support"
        else:
            status = "candidate_for_further_research"
        market_breakdown = {
            coin: group.details(enough_anchors)
            for coin, group in sorted(self.market_groups.items())
        }
        if enough_anchors:
            for coin, breakdown in market_breakdown.items():
                marks = markets[coin]
                breakdown["primary_exit_share"] = (
                    round(len(marks) / len(primary), 6) if primary else None
                )
                breakdown["contribution_to_overall_primary_mean_bps"] = (
                    round(sum(marks) / len(primary), 6) if primary else None
                )
        return {
            "status": status,
            "distinct_eligible_clock_anchors": len(self.anchors),
            "required_distinct_anchors": MIN_ANCHORS,
            "overall": total.details(enough_anchors),
            "by_day": {
                day: group.details(enough_anchors) for day, group in self.day_groups.items()
            },
            "by_market": market_breakdown,
            "positive_primary_markets": positive_markets if enough_anchors else None,
            "primary_day_clustered_statistic": clustered,
        }


def analyze(coverage_report, by_day, start, end):
    if coverage_report["status"] != "coverage_ready":
        raise StudyError("Outcome review requires complete timestamp coverage.")
    baseline = Cohort()
    conditional = {name: Cohort() for name in CONDITIONAL_SIGNALS}
    counters = Counter()
    diagnostics = defaultdict(Counter)
    cooldown = {}
    first_ms, last_ms = day_ms(start), day_ms(end)
    for offset in range((end - start).days):
        calendar_day = start + timedelta(days=offset)
        day_start = day_ms(calendar_day)
        day_index = day_start // DAY_MS
        day_end = day_start + DAY_MS
        parts = by_day[day_index]
        previous = by_day.get(day_index - 1, [])
        following = by_day.get(day_index + 1, [])
        prior_parts = list(dict.fromkeys([*previous, *parts]))
        outcome_parts = list(dict.fromkeys([*parts, *following]))
        book_parts = list(dict.fromkeys([*prior_parts, *following]))
        # Data from the next UTC day is only used for exits and late delivery checks.
        overlap_start = max(first_ms, day_start - OI_LOOKBACK_MS - META_AGE_MS)
        book_end = min(day_end + ANCHOR_MS, last_ms)
        books = book_series(read_slice(book_parts, overlap_start, book_end, "book"), counters)
        gaps = source_gaps(
            read_slice(book_parts, overlap_start, book_end, "health"), book_end, counters
        )
        flows = flow_series(
            read_slice(
                prior_parts, max(first_ms, day_start - MINUTE_MS), day_end, "trade", ordered=True
            ),
            books,
            diagnostics["data"],
        )
        states = market_states(
            read_slice(prior_parts, overlap_start, day_end, "meta"),
            books.keys(),
            diagnostics["data"],
        )
        windows = eligible_windows(day_start, books, gaps, counters)
        signals = conditional_windows(day_start, books, gaps, flows, states, cooldown, diagnostics)
        active_windows = defaultdict(list)
        for cohort_windows in (windows, *signals.values()):
            for key, window in cohort_windows.items():
                active_windows[key].append(window)
        observe_trades(
            read_slice(
                outcome_parts, day_start, min(day_end + TTL_MS, last_ms), "trade", fills_only=True
            ),
            active_windows,
            counters,
        )
        label = calendar_day.isoformat()
        baseline.add_day(label, windows, books, gaps, counters)
        for name, cohort in conditional.items():
            cohort.add_day(label, signals[name], books, gaps, diagnostics[name])

    signal_reports = {
        name: {**cohort.details(conditional=True), "diagnostics": dict(diagnostics[name])}
        for name, cohort in conditional.items()
    }
    return {
        **baseline.details(),
        "coverage": coverage_report,
        "method": {
            "clock_grid_minutes": ANCHOR_MS // MINUTE_MS,
            "fill_ttl_seconds": TTL_MS // 1000,
            "modeled_activation_delay_ms": ACTIVATION_MS,
            "max_book_age_seconds": BOOK_AGE_MS // 1000,
            "max_gap_inside_fill_ttl_seconds": MAX_BOOK_GAP_MS // 1000,
            "maker_fee_bps": float(MAKER_BPS),
            "exit_taker_fee_bps": float(TAKER_BPS),
            "order_notional_usd": float(QUOTE_USD),
            "fill_rule": "Opposite aggressor strictly through after activation on both clocks",
            "exit_rule": "Walk recorded bid or ask levels to cover the base quantity, pricing exit fee on actual exit notional",
            "primary_outcome": "5-minute executable round-trip net with UTC-day block bootstrap",
            "limitations": "Public trade and sampled quote proxies. Queue position, funding, latency and actual fills are unobserved.",
        },
        "diagnostics": dict(counters),
        "conditional_signals": signal_reports,
        "conditional_method": {
            "clock_grid_minutes": 1,
            "cooldown_minutes_per_market_per_signal": ANCHOR_MS // MINUTE_MS,
            "sided_quotes_per_trigger": 1,
            "flow_lookback_seconds": MINUTE_MS // 1000,
            "flow_minimum_total_usd": float(FLOW_NOTIONAL),
            "flow_signed_imbalance_threshold": float(FLOW_IMBALANCE),
            "depth_top_levels_per_side": 5,
            "depth_usd_notional_imbalance_threshold": float(DEPTH_IMBALANCE),
            "context_freshness_seconds": META_AGE_MS // 1000,
            "context_clock": "Recorder received time for REST meta, which supplies no exchange timestamp",
            "oi_flush_lookback_minutes": OI_LOOKBACK_MS // MINUTE_MS,
            "oi_flush_mark_return_cutoff": float(FLUSH_RETURN),
            "oi_flush_oi_change_cutoff": float(FLUSH_OI_CHANGE),
            "oi_flush_max_mark_oracle_premium_bps": float(FLUSH_PREMIUM_BPS),
            "fade_absolute_mark_oracle_premium_bps": float(FADE_PREMIUM_BPS),
            "promotion_mean_must_exceed_bps": CONDITIONAL_MEAN_BPS,
            "promotion_minimum_positive_markets": CONDITIONAL_MARKETS,
            "promotion_clustered_lower_bound_must_exceed_bps": 0,
            "diagnostics": dict(diagnostics["data"]),
        },
    }


def iso_date(raw):
    parsed = date.fromisoformat(raw)
    if parsed.isoformat() != raw:
        raise ValueError("Expected YYYY-MM-DD.")
    return parsed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory", type=Path, help="Local archive exported from the public recorder"
    )
    parser.add_argument("--start", required=True, type=iso_date, help="First UTC day")
    parser.add_argument("--end", required=True, type=iso_date, help="Exclusive UTC end day")
    parser.add_argument("--check-only", action="store_true", help="Verify timestamp coverage only")
    parser.add_argument("--output", type=Path, help="Write the JSON report to this path")
    options = parser.parse_args(argv)
    try:
        if options.output and options.output.resolve().is_relative_to(options.directory.resolve()):
            raise StudyError("Write the report outside the input archive directory.")
        report, parts = coverage(options.directory, options.start, options.end, utc_today())
        blocked = report["status"] == "coverage_blocked"
        if not blocked and not options.check_only:
            report = analyze(report, parts, options.start, options.end)
        output = json.dumps(report, allow_nan=False, indent=2) + "\n"
        if options.output:
            with options.output.open("x", encoding="utf-8") as destination:
                destination.write(output)
        else:
            print(output, end="")
        return 2 if blocked else 0
    except (StudyError, OSError, sqlite3.Error) as exc:
        print(f"Microstructure study unavailable. {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
