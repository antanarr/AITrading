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
from pathlib import Path
from statistics import mean

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


@dataclass(frozen=True, slots=True)
class Book:
    exchange_ms: int
    received_ms: int
    bid: Decimal
    bid_size: Decimal
    ask: Decimal
    ask_size: Decimal

    @property
    def mid(self):
        return (self.bid + self.ask) / 2


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
        end = anchor + TTL_MS
        previous = initial.received_ms
        for index in range(bisect_right(self.times, anchor), bisect_right(self.times, end)):
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
    bid = max(
        ((positive(level["px"]), positive(level["sz"])) for level in values["bids"]),
        key=lambda level: level[0],
    )
    ask = min(
        ((positive(level["px"]), positive(level["sz"])) for level in values["asks"]),
        key=lambda level: level[0],
    )
    if bid[0] >= ask[0]:
        raise ValueError("Expected a two-sided book with a positive spread.")
    return Book(exchange, received, *bid, *ask)


@dataclass(slots=True)
class Window:
    anchor: int
    coin: str
    book: Book
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
        if direction and (
            direction not in self.hits or (exchange, received) < self.hits[direction]
        ):
            self.hits[direction] = (exchange, received)


@dataclass(slots=True)
class Outcome:
    direction: int
    markouts: dict[int, float]
    roundtrips: dict[int, float]


def outcome(window, direction, books):
    entry_price = window.book.bid if direction == 1 else window.book.ask
    quantity = QUOTE_USD / entry_price
    fill_ms, _ = window.hits[direction]
    markouts, roundtrips = {}, {}
    for horizon in HORIZONS:
        end = books.at(fill_ms + horizon * MINUTE_MS)
        if end is None:
            continue
        markouts[horizon] = float(direction * (end.mid / entry_price - 1) * 10_000 - MAKER_BPS)
        exit_price, exit_size = (
            (end.bid, end.bid_size) if direction == 1 else (end.ask, end.ask_size)
        )
        if exit_size >= quantity:
            roundtrips[horizon] = float(
                direction * (exit_price / entry_price - 1) * 10_000 - MAKER_BPS - TAKER_BPS
            )
    return Outcome(direction, markouts, roundtrips)


def read_slice(parts, left, right, kind):
    query = """
        SELECT coin, exchange_ms, received_ms, trade_id, payload
        FROM observations
        WHERE kind = :kind AND received_ms >= :left AND received_ms < :right
    """
    if kind == "trade":
        query += " AND exchange_ms % :anchor BETWEEN :activation + 1 AND :ttl - 1"
    for part in parts:
        with open_readonly(part) as db:
            yield from db.execute(
                query,
                {
                    "kind": kind,
                    "left": left,
                    "right": right,
                    "anchor": ANCHOR_MS,
                    "ttl": TTL_MS,
                    "activation": ACTIVATION_MS,
                },
            )


def book_series(rows, counters):
    by_coin = defaultdict(list)
    for coin, exchange, received, _, payload in rows:
        if not coin or coin == "BTC":
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


def eligible_windows(start, books, gaps, counters):
    end = start + DAY_MS
    windows = {}
    for anchor in range(start, end, ANCHOR_MS):
        if any(left < anchor + TTL_MS and right >= anchor for left, right in gaps):
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


def observe_trades(rows, windows, counters):
    identity = {}
    for coin, exchange, received, trade_id, payload in rows:
        anchor = exchange // ANCHOR_MS * ANCHOR_MS
        window = windows.get((coin, anchor))
        if window is None:
            continue
        if exchange > received or type(trade_id) is not int or trade_id < 0:
            window.invalid_trade = True
            counters["invalid_trade_rows"] += 1
            continue
        if received >= anchor + TTL_MS:
            window.invalid_trade = True
            counters["late_trades_in_fill_window"] += 1
            continue
        try:
            trade = decoded(payload)
            side = trade["side"]
            if side not in {"buy", "sell"}:
                raise ValueError("Unknown aggressor.")
            price = positive(trade["px"])
            size = positive(trade["sz"])
        except (ValueError, TypeError, KeyError, InvalidOperation, zlib.error):
            window.invalid_trade = True
            counters["invalid_trade_rows"] += 1
            continue
        key = (coin, exchange, trade_id)
        if key in identity and identity[key] != (side, price, size):
            window.invalid_trade = True
            counters["conflicting_trade_ids"] += 1
            continue
        identity[key] = (side, price, size)
        window.consider(exchange, received, side, price)


@dataclass(slots=True)
class Group:
    eligible: int = 0
    outcomes: list[Outcome] = field(default_factory=list)

    def details(self, include_metrics):
        result = {
            "eligible_market_windows": self.eligible,
            "maker_quotes": 2 * self.eligible,
            "strict_through_proxy_fills": len(self.outcomes),
            "proxy_fills_by_maker_side": {
                "buy": sum(row.direction == 1 for row in self.outcomes),
                "sell": sum(row.direction == -1 for row in self.outcomes),
            },
        }
        if not include_metrics:
            return result
        result["proxy_fill_fraction"] = (
            round(len(self.outcomes) / (2 * self.eligible), 6) if self.eligible else None
        )
        result["horizons"] = {}
        for horizon in HORIZONS:
            marks = [row.markouts[horizon] for row in self.outcomes if horizon in row.markouts]
            exits = [row.roundtrips[horizon] for row in self.outcomes if horizon in row.roundtrips]
            result["horizons"][str(horizon)] = {
                "markout_observations": len(marks),
                "avg_markout_after_entry_fee_bps": round(mean(marks), 6) if marks else None,
                "executable_exit_observations": len(exits),
                "executable_exit_fraction": (
                    round(len(exits) / len(self.outcomes), 6) if self.outcomes else None
                ),
                "avg_executable_roundtrip_net_bps": round(mean(exits), 6) if exits else None,
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


def analyze(coverage_report, by_day, start, end):
    if coverage_report["status"] != "coverage_ready":
        raise StudyError("Outcome review requires complete timestamp coverage.")
    day_groups = {}
    market_groups = defaultdict(Group)
    counters = Counter()
    anchors = set()
    for offset in range((end - start).days):
        calendar_day = start + timedelta(days=offset)
        day_start = day_ms(calendar_day)
        day_index = day_start // DAY_MS
        day_end = day_start + DAY_MS
        day_group = day_groups[calendar_day.isoformat()] = Group()
        parts = by_day[day_index]
        previous = by_day[day_index - 1] if offset else []
        overlap = list(dict.fromkeys([*previous, *parts]))
        # Earlier observations can establish prior receipt or an open socket interruption.
        overlap_start = day_start - BOOK_AGE_MS if offset else day_start
        books = book_series(read_slice(overlap, overlap_start, day_end, "book"), counters)
        for coin in books:
            market_groups.setdefault(coin, Group())
        health_start = day_start - COVERAGE_SLOT_MS if offset else day_start
        gaps = source_gaps(read_slice(overlap, health_start, day_end, "health"), day_end, counters)
        windows = eligible_windows(day_start, books, gaps, counters)
        observe_trades(read_slice(parts, day_start, day_end, "trade"), windows, counters)
        for window in windows.values():
            if window.invalid_trade:
                counters["market_windows_excluded_for_trade_quality"] += 1
                continue
            anchors.add(window.anchor)
            market_group = market_groups[window.coin]
            fills = [outcome(window, side, books[window.coin]) for side in window.hits]
            for group in (day_group, market_group):
                group.eligible += 1
                group.outcomes.extend(fills)
    total = Group(
        eligible=sum(group.eligible for group in day_groups.values()),
        outcomes=[row for group in day_groups.values() for row in group.outcomes],
    )
    enough_anchors = len(anchors) >= MIN_ANCHORS
    clustered = cluster_interval(day_groups.values()) if enough_anchors else None
    if not enough_anchors:
        status = "insufficient_eligible_windows"
    elif not total.outcomes:
        status = "zero_proxy_fills"
    elif clustered["ci95_bps"] is None:
        status = "insufficient_executable_exit_days"
    elif clustered["ci95_bps"][0] > 0:
        status = "candidate_for_further_research"
    else:
        status = "no_positive_candidate"
    market_breakdown = {
        coin: group.details(enough_anchors) for coin, group in sorted(market_groups.items())
    }
    if enough_anchors:
        population = len(primary_returns(total))
        for coin, breakdown in market_breakdown.items():
            marks = primary_returns(market_groups[coin])
            breakdown["primary_exit_share"] = (
                round(len(marks) / population, 6) if population else None
            )
            breakdown["contribution_to_overall_primary_mean_bps"] = (
                round(sum(marks) / population, 6) if population else None
            )
    return {
        "status": status,
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
            "exit_rule": "Displayed executable touch at fill timestamp plus horizon with sufficient base size",
            "primary_outcome": "5-minute executable round-trip net with UTC-day block bootstrap",
            "limitations": "Public trade and sampled quote proxies. Queue position, funding, latency and actual fills are unobserved.",
        },
        "distinct_eligible_clock_anchors": len(anchors),
        "required_distinct_anchors": MIN_ANCHORS,
        "diagnostics": dict(counters),
        "overall": total.details(enough_anchors),
        "by_day": {day: group.details(enough_anchors) for day, group in day_groups.items()},
        "by_market": market_breakdown,
        "primary_day_clustered_statistic": clustered,
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
