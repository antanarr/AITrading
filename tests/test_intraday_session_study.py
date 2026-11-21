from datetime import UTC, date, datetime

import pytest

from copybot import intraday_session_study as study
from copybot.research_models import HOUR_MS, Bar, MarketHistory


def market(coin, signal, direction=1, turnover=20_000):
    normal = Bar(100, 101, 99, 100, turnover)
    move = Bar(100, 102, 98, 100 + direction * 1.5, turnover)
    bars = {hour: normal for hour in range(signal - 30, signal + 10)}
    bars[signal] = move
    return MarketHistory(coin, 2, False, None, bars, {})


def test_cash_clock_respects_new_york_dst_and_holiday():
    hours = list(study.session_hours(date(2026, 10, 30), date(2026, 11, 4), "cash"))
    assert [day for day, *_ in hours] == [date(2026, 10, 30), date(2026, 11, 2), date(2026, 11, 3)]
    assert [datetime.fromtimestamp(observed * 3600, UTC).hour for _, observed, _ in hours] == [
        14,
        15,
        15,
    ]
    assert all(submit == observed + 5 for _, observed, submit in hours)
    holidays = study.session_hours(date(2026, 6, 19), date(2026, 6, 20), "cash")
    assert list(holidays) == []


def test_rank_and_sides_use_completed_window_and_ignore_btc(monkeypatch):
    day, signal = date(2026, 6, 1), 1_000
    enter = signal + 5
    monkeypatch.setattr(study, "session_hours", lambda *_: [(day, signal, enter)])
    assets = {f"ALT{i}": market(f"ALT{i}", signal, turnover=30_000 - i) for i in range(6)}
    assets["BTC"] = market("BTC", signal, -1, turnover=1_000_000)
    paths, notes, calendar = study.build_signals(assets, day, day, "cash")
    assert calendar[enter]["coins"] == ["ALT0", "ALT1"]
    assert notes["impulse_sessions"] == 1
    assert [trade.legs[0].side for trade in paths["follow"][enter]] == [1, 1]
    assert [trade.legs[0].side for trade in paths["fade"][enter]] == [-1, -1]
    assert all(trade.legs[0].hold_hours == 1 for trade in paths["follow"][enter])
    assert paths["follow"][enter][0].legs[0].limit == pytest.approx(100.15)
    for item in assets.values():
        item.bars[enter] = Bar(500, 999, 1, 14, 0)
        item.bars[enter + 1] = Bar(500, 999, 1, 14, 0)
    assert study.build_signals(assets, day, day, "cash") == (paths, notes, calendar)


def test_session_diagnostic_clusters_two_fills_together():
    calendar = {1: {"submitted_hour": 1, "session_date": "2026-05-26"}}
    scores = {
        "trades": [
            {"entered_ms": HOUR_MS, "net": -4},
            {"entered_ms": HOUR_MS + 500, "net": 5},
        ]
    }
    summary = study.by_session(scores, calendar)
    assert summary["sessions_with_fills"] == summary["positive_sessions"] == 1
    assert summary["net_by_session_date"] == {"2026-05-26": 1}
    assert summary["session_bootstrap_95pct_mean_pnl_interval"] is None


def test_preflight_refuses_reserve_before_reading_inputs(tmp_path):
    with pytest.raises(ValueError, match="exclusive of Reserve"):
        study.run(tmp_path / "unavailable", study.START, study.VALIDATION, date(2026, 11, 22))
