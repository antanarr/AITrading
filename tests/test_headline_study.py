import json
from dataclasses import replace

import pytest

from copybot.headline_study import (
    BREAKOUT_WINDOW,
    CUTOFF,
    HOLD_TIME,
    MINUTE_MS,
    RELEASES,
    bounds,
    choose_signal,
    current_availability,
    exit_trade,
    simulate,
)
from copybot.research_execution import QUARTER_MS, STRESS
from copybot.research_models import HOUR_MS, Bar

QUIET = Bar(100, 101, 99, 100, 10)


def test_headline_cannot_use_signal_that_closed_before_receipt():
    step = 5 * MINUTE_MS
    anchor = CUTOFF - 12 * HOUR_MS
    candles = dict.fromkeys(
        range(anchor - BREAKOUT_WINDOW - step, anchor + HOUR_MS * 2, step), QUIET
    )
    candles[anchor] = Bar(100, 104, 99, 104, 10)
    published = anchor + 3 * MINUTE_MS
    fast = choose_signal(candles, published, published + MINUTE_MS, step=step)
    slow = choose_signal(candles, published, published + 5 * MINUTE_MS, step=step)
    assert fast["time_ms"] == anchor + step
    assert slow is None


def test_current_feed_tracks_the_source_not_the_last_active_rate(tmp_path):
    cpi, fomc, *_ = RELEASES
    hour = fomc.published // HOUR_MS
    archive = tmp_path / "context.jsonl"
    older = {
        "available_hour": hour,
        "source": "older decision",
        "features": {"news_fomc_rate_direction": 0},
    }
    new = {**older, "available_hour": hour + 1, "source": fomc.source}
    archive.write_text(json.dumps(older) + "\n" + json.dumps(new) + "\n")
    assert current_availability(cpi, archive) is None
    assert current_availability(fomc, archive) == (hour + 1) * HOUR_MS
    archive.write_text(json.dumps(older) + "\n")
    with pytest.raises(ValueError, match="No archived rate context"):
        current_availability(fomc, archive)


def test_snapshot_bounds_reject_post_development_tape():
    with pytest.raises(ValueError, match="Development"):
        bounds(replace(RELEASES[-1], eastern="2026-11-21T08:00"))


def test_ioc_rejects_a_gap_past_the_cap():
    stamp = CUTOFF - HOUR_MS
    result = simulate(
        {stamp: Bar(102, 103, 101, 102, 10)},
        {},
        3,
        {"time_ms": stamp, "side": 1, "atr": 1, "reference": 100},
        STRESS,
        1,
    )
    assert result["status"] == "ioc_unfilled"
    assert result["net"] == 0


def test_resting_target_requires_trade_through_after_entry_and_yields_to_stop():
    step = QUARTER_MS
    start = CUTOFF - 4 * HOUR_MS
    candles = dict.fromkeys(range(start, start + HOLD_TIME + step, step), QUIET)
    candles[start] = Bar(100, 104, 99, 100, 10)
    candles[start + step] = Bar(100, 103, 99, 100, 10)
    candles[start + 2 * step] = Bar(100, 104, 99, 100, 10)
    assert exit_trade(candles, start, step, 1, 98, 103) == (start + 3 * step, 103, "maker_target")
    candles[start + 2 * step] = Bar(100, 104, 97, 100, 10)
    assert exit_trade(candles, start, step, 1, 98, 103) == (start + 3 * step, 98, "stop")
    candles[start + 2 * step] = Bar(96, 104, 95, 100, 10)
    assert exit_trade(candles, start, step, 1, 98, 103) == (start + 2 * step, 96, "gap_stop")


def test_leverage_multiplies_notional_with_the_same_collateral_and_stop():
    start = CUTOFF - HOUR_MS + QUARTER_MS
    candles = {start: Bar(100, 101, 97, 99, 10)}
    signal = {"time_ms": start, "side": 1, "atr": 1, "reference": 100}
    baseline = simulate(candles, {}, 3, signal, STRESS, 1, max_leverage=10)
    levered = simulate(candles, {}, 3, signal, STRESS, 1, leverage=5, max_leverage=10)
    assert levered["entry_notional"] == pytest.approx(baseline["entry_notional"] * 5)
    assert levered["collateral"] == pytest.approx(baseline["collateral"])
    assert levered["initial_stop"] == baseline["initial_stop"]
    assert levered["net"] == pytest.approx(baseline["net"] * 5)


def test_liquidation_takes_priority_when_the_stop_and_liquidation_cross_in_one_bar():
    start = CUTOFF - HOUR_MS + QUARTER_MS
    candles = {start: Bar(100, 101, 80, 99, 10)}
    signal = {"time_ms": start, "side": 1, "atr": 1, "reference": 100}
    baseline = simulate(candles, {}, 3, signal, STRESS, 1, max_leverage=10)
    levered = simulate(candles, {}, 3, signal, STRESS, 1, leverage=5, max_leverage=10)
    assert baseline["exit_reason"] == "stop"
    assert levered["exit_reason"] == "liquidation_intrabar"
    fee = levered["entry_notional"] * STRESS.taker_fee_bps / 10_000
    assert levered["net"] == pytest.approx(-levered["collateral"] - fee)
