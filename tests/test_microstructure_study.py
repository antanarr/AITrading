import json
import sqlite3
import zlib
from datetime import timedelta
from decimal import Decimal

import pytest

from copybot import microstructure_study as study

START = study.PROSPECTIVE_START
END = START + timedelta(days=study.MIN_DAYS)
STAMP = study.day_ms(START)
HEALTHY = '{"event":"recorder_heartbeat","ws_ready":true}'


def archive_part(directory, start, records):
    name = directory / f"market-{start:013d}-abcde123.sqlite3"
    with sqlite3.connect(name) as connection:
        connection.executescript("""
            CREATE TABLE observations (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                received_ms INTEGER NOT NULL,
                coin TEXT,
                exchange_ms INTEGER,
                trade_id INTEGER,
                payload BLOB NOT NULL
            );
        """)
        connection.executemany(
            "INSERT INTO observations (kind, received_ms, coin, exchange_ms, trade_id, payload)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            records,
        )
    return name


def book_payload(bid="100", ask="102", bid_size="1", ask_size="1"):
    return zlib.compress(
        json.dumps(
            {
                "bids": [{"px": bid, "sz": bid_size}],
                "asks": [{"px": ask, "sz": ask_size}],
            }
        ).encode()
    )


def ladder_payload(bid_size, ask_size):
    return zlib.compress(
        json.dumps(
            {
                "bids": [{"px": str(100 - level), "sz": bid_size} for level in range(5)],
                "asks": [{"px": str(102 + level), "sz": ask_size} for level in range(5)],
            }
        ).encode()
    )


def market_payload(mark, oracle, oi, extra=None):
    return json.dumps(
        {
            "markets": {
                "DOGE": {"markPx": mark, "oraclePx": oracle, "openInterest": oi},
                **(extra or {}),
            }
        }
    )


def baseline_timestamps(start, book=b"invalid payload"):
    # Required UTC source coverage, with edges inside the two-minute tolerance.
    offsets = [*range(0, study.DAY_MS, study.COVERAGE_SLOT_MS), study.DAY_MS - study.MINUTE_MS]
    for offset in offsets:
        stamp = start + offset
        yield "health", stamp, None, None, None, HEALTHY
        yield "meta", stamp, None, None, None, b"invalid payload"
        yield "book", stamp, "DOGE", stamp, None, book


def quote_day(start, fills=True):
    yield from baseline_timestamps(start, book_payload())
    for offset in range(0, study.DAY_MS, study.ANCHOR_MS):
        anchor = start + offset
        yield "meta", anchor, None, None, None, market_payload("99", "100", "1000")
        for arrival_offset in range(0, 60_001, 10_000):
            stamp = anchor + arrival_offset
            yield "book", stamp, "DOGE", stamp, None, book_payload()
        if fills:
            fill_ms = anchor + 5_000
            yield "trade", fill_ms + 5, "DOGE", fill_ms, 4, '{"side":"sell","px":"99","sz":"1"}'
            for horizon in study.HORIZONS:
                stamp = fill_ms + horizon * study.MINUTE_MS
                yield "book", stamp, "DOGE", stamp, None, book_payload(bid="101", ask="103")


def arguments(directory, *extra):
    return [str(directory), "--start", START.isoformat(), "--end", END.isoformat(), *extra]


def test_timestamp_gate_does_not_load_payloads_and_reports_incomplete_days(tmp_path, monkeypatch):
    for day in range(6):
        start = STAMP + day * study.DAY_MS
        archive_part(tmp_path, start, baseline_timestamps(start))
    last = STAMP + 6 * study.DAY_MS
    archive_part(tmp_path, last, [("meta", last, None, None, None, b"invalid payload")])
    monkeypatch.setattr(study, "decoded", lambda _payload: pytest.fail("Outcome data was read"))
    monkeypatch.setattr(study, "utc_today", lambda: END)
    output = tmp_path.parent / f"{tmp_path.name}.json"
    assert study.main(arguments(tmp_path, "--output", str(output))) == 2
    report = json.loads(output.read_text())
    assert report["status"] == "coverage_blocked"
    assert report["complete_days"] == 6
    assert (
        report["days"][(END - timedelta(days=1)).isoformat()]["book"]["five_minute_bins_present"]
        == 0
    )
    assert "overall" not in report


def test_check_only_and_date_gate_preserve_prospective_cutoff(tmp_path, monkeypatch, capsys):
    with pytest.raises(study.StudyError, match=START.isoformat()):
        study.coverage(tmp_path, START - timedelta(days=1), END, END)
    with pytest.raises(study.StudyError, match="Exclusive end"):
        study.coverage(tmp_path, START, END, END - timedelta(days=1))
    with pytest.raises(study.StudyError, match="at least 7"):
        study.coverage(tmp_path, START, END - timedelta(days=1), END)
    for day in range(7):
        start = STAMP + day * study.DAY_MS
        archive_part(tmp_path, start, baseline_timestamps(start))
    monkeypatch.setattr(study, "decoded", lambda _payload: pytest.fail("Outcome data was read"))
    monkeypatch.setattr(study, "utc_today", lambda: END)
    assert study.main(arguments(tmp_path, "--check-only")) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["complete_days"] == 7
    assert report["status"] == "coverage_ready"


def test_quotes_require_strict_through_opposite_side_and_activation():
    initial = study.read_book(STAMP - 250, STAMP - 160, book_payload())
    window = study.Window(STAMP, "DOGE", initial)
    price = Decimal
    window.consider(STAMP + 250, STAMP + 260, "sell", price(99))
    window.consider(STAMP + 251, STAMP + 245, "sell", price(99))
    window.consider(STAMP + 300, STAMP + 300, "sell", price(100))
    window.consider(STAMP + 300, STAMP + 300, "buy", price(102))
    window.consider(STAMP + 300, STAMP + 300, "buy", price(99))
    window.consider(STAMP + 300, STAMP + 300, "sell", price(103))
    window.consider(STAMP + 59_990, STAMP + 60_000, "sell", price(99))
    assert not window.hits
    window.consider(STAMP + 320, STAMP + 321, "sell", price(99))
    window.consider(STAMP + 310, STAMP + 330, "sell", price(99))
    window.consider(STAMP + 320, STAMP + 325, "buy", price(103))
    assert window.hits == {1: (STAMP + 310, STAMP + 330), -1: (STAMP + 320, STAMP + 325)}


def test_quote_lookup_respects_local_availability_and_requires_full_exit_depth():
    initial = study.read_book(STAMP, STAMP, book_payload())
    window = study.Window(STAMP, "DOGE", initial)
    fill_time = STAMP + 500
    window.consider(fill_time, fill_time + 25, "sell", Decimal("99"))
    first_mark = fill_time + study.MINUTE_MS
    later_book = study.read_book(first_mark - 5, first_mark + 1, book_payload(bid="110", ask="112"))
    five_mark = fill_time + 5 * study.MINUTE_MS
    shallow = study.read_book(
        five_mark - 1,
        five_mark,
        book_payload(
            bid="100",
            ask="102",
            bid_size="0.09",
        ),
    )
    fifteen_mark = fill_time + 15 * study.MINUTE_MS
    deep = study.read_book(
        fifteen_mark - 10_000,
        fifteen_mark,
        book_payload(
            bid="101",
            ask="103",
        ),
    )
    series = study.Books(
        [item.received_ms for item in (initial, later_book, shallow, deep)],
        [initial, later_book, shallow, deep],
    )
    result = study.outcome(window, 1, series)
    assert result.markouts == {5: 98.5, 15: 198.5}
    assert result.roundtrips == {15: 93.955}


def test_gap_and_bad_trade_cannot_turn_into_clean_unfilled_windows():
    initial = study.read_book(STAMP, STAMP, book_payload())
    distant = study.read_book(STAMP + 40_000, STAMP + 40_000, book_payload())
    series = study.Books([initial.received_ms, distant.received_ms], [initial, distant])
    assert not series.covers_ttl(STAMP, initial)
    result = study.source_gaps(
        [
            (None, None, STAMP, None, '{"event":"ws_disconnected"}'),
            (None, None, STAMP + 30_000, None, '{"event":"ws_recovered"}'),
        ],
        STAMP + study.DAY_MS,
        study.Counter(),
    )
    assert result == [(STAMP, STAMP + 30_000)]
    window = study.Window(STAMP, "DOGE", initial)
    counters = study.Counter()
    study.observe_trades(
        [
            ("DOGE", STAMP + 500, STAMP + 60_000, 4, '{"side":"sell","px":"99","sz":"1"}'),
        ],
        {("DOGE", STAMP): [window]},
        counters,
    )
    assert window.invalid_trade
    assert counters["late_trades_in_fill_window"] == 1
    study.observe_trades(
        [("DOGE", None, STAMP + 1_000, 5, "{}")],
        {("DOGE", STAMP): [window]},
        counters,
    )
    assert counters["invalid_trade_clocks"] == 1


@pytest.mark.parametrize("fills", (True, False))
def test_report_counts_nonoverlapping_clock_anchors_and_primary_executable_exit(
    tmp_path,
    monkeypatch,
    capsys,
    fills,
):
    for day in range(7):
        start = STAMP + day * study.DAY_MS
        archive_part(tmp_path, start, quote_day(start, fills=fills))
    before = {part.name: part.stat().st_mtime_ns for part in tmp_path.iterdir()}
    monkeypatch.setattr(study, "utc_today", lambda: END)
    assert study.main(arguments(tmp_path)) == 0
    report = json.loads(capsys.readouterr().out)
    assert {part.name: part.stat().st_mtime_ns for part in tmp_path.iterdir()} == before
    assert report["distinct_eligible_clock_anchors"] == 630
    assert report["overall"]["maker_quotes"] == 1_260
    if not fills:
        assert report["status"] == "zero_proxy_fills"
        assert report["overall"]["proxy_fill_fraction"] == 0
        assert report["overall"]["horizons"]["5"]["avg_executable_roundtrip_net_bps"] is None
        return
    assert report["status"] == "candidate_for_further_research"
    assert report["overall"]["proxy_fill_fraction"] == 0.5
    assert report["overall"]["proxy_fills_by_maker_side"] == {"buy": 630, "sell": 0}
    assert report["overall"]["horizons"]["5"]["avg_executable_roundtrip_net_bps"] == 93.955
    assert report["primary_day_clustered_statistic"] == {
        "represented_utc_days": 7,
        "ci95_bps": [93.955, 93.955],
    }
    assert report["by_market"]["DOGE"]["contribution_to_overall_primary_mean_bps"] == 93.955
    fade = report["conditional_signals"]["mark_oracle_fade"]
    assert fade["status"] == "insufficient_positive_market_support"
    assert fade["distinct_eligible_clock_anchors"] == 630
    assert fade["overall"]["maker_quotes"] == 630
    assert fade["overall"]["proxy_fill_fraction"] == 1.0
    assert fade["positive_primary_markets"] == ["DOGE"]


def test_full_coverage_can_have_insufficient_eligible_anchors(tmp_path):
    for day in range(7):
        start = STAMP + day * study.DAY_MS
        archive_part(tmp_path, start, baseline_timestamps(start, book_payload()))
    coverage, days = study.coverage(tmp_path, START, END, END)
    report = study.analyze(coverage, days, START, END)
    assert report["status"] == "insufficient_eligible_windows"
    assert report["distinct_eligible_clock_anchors"] == 0
    assert report["primary_day_clustered_statistic"] is None
    assert report["by_market"]["DOGE"]["strict_through_proxy_fills"] == 0


def test_exit_walks_recorded_levels_charges_both_fees_and_rejects_missing_depth():
    initial = study.read_book(STAMP, STAMP, book_payload())
    window = study.Window(STAMP, "DOGE", initial)
    window.consider(STAMP + 500, STAMP + 510, "sell", Decimal(99))
    target = STAMP + 500 + 5 * study.MINUTE_MS
    exit_book = study.read_book(target, target, ladder_payload("0.06", "0.05"))
    series = study.Books([target], [exit_book])
    assert exit_book.exit_vwap(1, Decimal("0.1")) == Decimal("99.6")
    assert exit_book.exit_vwap(-1, Decimal("0.08")) == Decimal("102.375")
    assert exit_book.exit_vwap(1, Decimal("0.301")) is None
    result = study.outcome(window, 1, series)
    assert result.roundtrips == {5: -45.982}
    assert study.outcome(window, 1, series, [(target - 1, target + 1)]).roundtrips == {}


def test_flow_signal_uses_only_arrived_trade_notional_and_usd_depth():
    anchor = STAMP + study.MINUTE_MS
    payload = ladder_payload("4", "2")
    series = study.book_series(
        [("DOGE", ms, ms, None, payload) for ms in range(STAMP, anchor + 1, 10_000)],
        study.Counter(),
    )["DOGE"]
    rows = [
        ("DOGE", anchor - 500, anchor - 490, 1, '{"side":"buy","px":"100","sz":"20"}'),
        ("DOGE", anchor - 500, anchor - 489, 1, '{"side":"buy","px":"100","sz":"20"}'),
        ("DOGE", anchor - 450, anchor, 2, '{"side":"sell","px":"100","sz":"5"}'),
        ("DOGE", anchor - 400, anchor + 1, 3, '{"side":"sell","px":"100","sz":"250"}'),
    ]
    counts = study.Counter()
    flows = study.flow_series(rows, {"DOGE"}, counts)
    assert flows == {("DOGE", anchor): (Decimal(1500), Decimal(2500))}
    state = study.market_states([], {"DOGE"}, counts)
    book = series.at(anchor)
    assert study.signal_directions("DOGE", anchor, book, series, flows, state, counts) == {
        "taker_flow_depth": 1,
    }
    thinner = study.read_book(anchor, anchor, ladder_payload("3", "2"))
    assert thinner.depth_imbalance() < Decimal("0.20")
    assert study.signal_directions("DOGE", anchor, thinner, series, flows, state, counts) == {}


def test_flush_and_premium_fade_enforce_inclusive_boundaries_and_fresh_receive_time():
    anchor = STAMP + 7 * study.MINUTE_MS
    past = anchor - study.OI_LOOKBACK_MS - study.META_AGE_MS
    counts = study.Counter()
    rows = [
        (None, None, past, None, market_payload("100", "100", "1000")),
        (
            None,
            None,
            anchor - 100,
            None,
            market_payload(
                "98.25",
                "100",
                "970",
                {
                    "WIF": "broken",
                    "HYPE": {
                        "markPx": "110",
                        "oraclePx": "100",
                        "openInterest": "broken",
                    },
                },
            ),
        ),
        (None, anchor - 10, anchor + 1, None, market_payload("105", "100", "1000")),
    ]
    states = study.market_states(rows, {"DOGE", "WIF", "HYPE"}, counts)
    initial = study.read_book(anchor, anchor, book_payload())
    assert study.signal_directions("DOGE", anchor, initial, None, {}, states, counts) == {
        "mark_oracle_fade": 1,
        "oi_flush": 1,
    }
    assert study.signal_directions("DOGE", anchor + 1, initial, None, {}, states, counts) == {
        "mark_oracle_fade": -1,
    }
    assert study.signal_directions("HYPE", anchor, initial, None, {}, states, counts) == {
        "mark_oracle_fade": -1,
    }
    assert states.at("DOGE", anchor + 1 + study.META_AGE_MS) is None
    assert states.at("WIF", anchor) is None
    assert counts["invalid_meta_market_rows"] == 1


def test_cooldown_carries_through_midnight_and_does_not_use_future_quality(monkeypatch):
    boundary = STAMP + study.DAY_MS
    initial = study.read_book(
        boundary - study.MINUTE_MS, boundary - study.MINUTE_MS, book_payload()
    )
    later = study.read_book(boundary, boundary, book_payload())
    observations = study.Books([initial.received_ms, later.received_ms], [initial, later])
    monkeypatch.setattr(study, "signal_directions", lambda *_: {"mark_oracle_fade": -1})
    diagnostics = study.defaultdict(study.Counter)
    cooldown = {}
    first = study.conditional_windows(
        STAMP,
        {"DOGE": observations},
        [(initial.received_ms, boundary)],
        {},
        None,
        cooldown,
        diagnostics,
    )
    assert not first["mark_oracle_fade"]
    assert cooldown[("DOGE", "mark_oracle_fade")] == initial.received_ms + study.ANCHOR_MS
    study.conditional_windows(
        boundary,
        {"DOGE": observations},
        [],
        {},
        None,
        cooldown,
        diagnostics,
    )
    assert diagnostics["mark_oracle_fade"]["hits_in_cooldown"] == 1


def test_conditional_promotion_requires_mean_above_three_and_four_positive_markets():
    def cohort(net_bps, count):
        outcome = study.Outcome(1, {5: net_bps}, {5: net_bps})
        return study.Cohort(
            day_groups={str(day): study.Group(1, 1, [outcome]) for day in range(7)},
            market_groups={str(coin): study.Group(1, 1, [outcome]) for coin in range(count)},
            anchors=set(range(500)),
        ).details(conditional=True)

    assert cohort(3, 4)["status"] == "no_positive_candidate"
    assert cohort(3.1, 3)["status"] == "insufficient_positive_market_support"
    assert cohort(3.1, 4)["status"] == "candidate_for_further_research"


def test_books_exclude_bitcoin_aliases_and_non_native_symbols():
    rows = [
        (coin, STAMP, STAMP, None, book_payload())
        for coin in ("DOGE", "BTC", "wbtc", "XBT", "kBTC", "other:DOGE")
    ]
    assert set(study.book_series(rows, study.Counter())) == {"DOGE"}


@pytest.mark.parametrize("late_trade", (False, True))
def test_conditional_exit_can_cross_utc_midnight_inside_the_requested_range(tmp_path, late_trade):
    for day in range(8):
        start = STAMP + day * study.DAY_MS
        rows = list(baseline_timestamps(start, book_payload()))
        if day in {0, 6}:
            anchor = start + study.DAY_MS - study.MINUTE_MS
            rows.append(("meta", anchor, None, None, None, market_payload("99", "100", "1000")))
            for offset in range(0, 60_000, 10_000):
                stamp = anchor + offset
                rows.append(("book", stamp, "DOGE", stamp, None, book_payload()))
            rows.append(
                (
                    "trade",
                    anchor + 500,
                    "DOGE",
                    anchor + 500,
                    1,
                    '{"side":"sell","px":"99","sz":"1"}',
                )
            )
        if day in {1, 7}:
            stamp = start + 4 * study.MINUTE_MS + 500
            rows.append(("book", stamp, "DOGE", stamp, None, book_payload(bid="101", ask="103")))
        if day == 1 and late_trade:
            rows.append(
                ("trade", start + 1, "DOGE", start - 500, 2, '{"side":"sell","px":"99","sz":"1"}')
            )
        archive_part(tmp_path, start, rows)
    coverage, days = study.coverage(tmp_path, START, END, END)
    report = study.analyze(coverage, days, START, END)
    fade = report["conditional_signals"]["mark_oracle_fade"]
    assert fade["distinct_eligible_clock_anchors"] == (1 if late_trade else 2)
    assert fade["overall"]["strict_through_proxy_fills"] == (1 if late_trade else 2)
    assert fade["overall"]["horizons"]["5"]["executable_exit_observations"] == (
        0 if late_trade else 1
    )
