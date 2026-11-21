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
    assert result.roundtrips == {15: 94.0}


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
        {("DOGE", STAMP): window},
        counters,
    )
    assert window.invalid_trade
    assert counters["late_trades_in_fill_window"] == 1


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
    assert report["overall"]["horizons"]["5"]["avg_executable_roundtrip_net_bps"] == 94.0
    assert report["primary_day_clustered_statistic"] == {
        "represented_utc_days": 7,
        "ci95_bps": [94.0, 94.0],
    }
    assert report["by_market"]["DOGE"]["contribution_to_overall_primary_mean_bps"] == 94.0


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
