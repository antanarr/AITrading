from dataclasses import replace

import pytest

from copybot.liquid_trend import BASELINE_VARIANT, ENHANCED_VARIANT, build_liquid_trend
from copybot.research_data import ARCHIVE_END_HOUR
from copybot.research_execution import STRESS, RiskLimits, replay
from copybot.research_models import DAY_HOURS, Bar, MarketHistory, OrderPlan, TradePlan

DAY = 50_000 // DAY_HOURS * DAY_HOURS


def fresh_bar(price, volume=10_000):
    return Bar(price, price + 1, price - 1, price, volume)


def market(coin="ETH", *, day=DAY, signal=110, following=97):
    prices = {hour: fresh_bar(100) for hour in range(day - 52 * DAY_HOURS, day + 2 * DAY_HOURS)}
    prices[day - 1] = fresh_bar(signal)
    prices[day + DAY_HOURS - 1] = fresh_bar(following)
    return MarketHistory(coin, 3, False, None, prices, {})


def order(coin="ETH", side=1):
    return TradePlan(
        (OrderPlan(coin, side, 110.4, 4, 2, None, 240, 0, "ioc", 3),), BASELINE_VARIANT
    )


def test_uses_previous_daily_sma_and_clones_only_the_exit_rule():
    source = order()
    rising, falling = market(), market("SOL", signal=90)
    inputs = {DAY: [source, order("SOL"), order("SOL", -1)]}
    histories = {row.coin: row for row in (rising, falling)}
    picks, callback, audit = build_liquid_trend(histories, inputs, DAY, DAY + 48)
    assert [plan.legs[0].side for plan in picks[DAY]] == [1, -1]
    assert picks[DAY][0] == TradePlan(
        (replace(source.legs[0], trailing_atr=None),), ENHANCED_VARIANT
    )
    assert audit == {
        "accepted_sma_filter": 2,
        "rejected_insufficient_daily_history": 0,
        "rejected_opposite_50_day_trend": 1,
        "input_plans": 3,
        "opening_market_count": 2,
    }
    assert callback(source, DAY, DAY + DAY_HOURS) is None
    assert source.legs[0].trailing_atr == 3


def test_missing_daily_input_does_not_shorten_the_50_day_lookback_or_emit_entry():
    history = market()
    bars = dict(history.bars)
    del bars[DAY - 30 * DAY_HOURS]
    picks, _, audit = build_liquid_trend(
        {"ETH": replace(history, bars=bars)}, {DAY: [order()]}, DAY, DAY + 24
    )
    assert not picks
    assert audit["rejected_insufficient_daily_history"] == 1
    bars = dict(history.bars)
    del bars[DAY - 10]
    picks, _, audit = build_liquid_trend(
        {"ETH": replace(history, bars=bars)}, {DAY: [order()]}, DAY, DAY + 24
    )
    assert not picks
    assert audit["rejected_insufficient_daily_history"] == 1


def test_opposite_channel_uses_only_closed_days_and_requires_a_strict_break():
    decision = DAY + 8
    end = decision + 48
    for next_close, expected in ((99, None), (97, "10-day channel exit")):
        history = market(day=decision, following=next_close)
        picks, callback, _ = build_liquid_trend(
            {"ETH": history}, {decision: [order()]}, decision, end, daily_offset=8
        )
        plan = picks[decision][0]
        assert callback(plan, decision, decision + 23) is None
        assert callback(plan, decision, decision + 24) == expected
        later = replace(history, bars={**history.bars, decision + 24: fresh_bar(10)})
        second, after, _ = build_liquid_trend(
            {"ETH": later}, {decision: [order()]}, decision, end, daily_offset=8
        )
        assert second == picks
        assert after(plan, decision, decision + 24) == expected


def test_rejects_btc_inputs_and_future_cutoffs():
    with pytest.raises(ValueError, match="non-BTC"):
        build_liquid_trend({}, {DAY: [order("wBTC")]}, DAY, DAY + 24)
    with pytest.raises(ValueError, match="development cutoff"):
        build_liquid_trend({}, {}, DAY, ARCHIVE_END_HOUR + 1)


def test_shared_replay_executes_channel_exit_at_the_daily_boundary():
    histories = {"ETH": market()}
    plans, callback, _ = build_liquid_trend(histories, {DAY: [order()]}, DAY, DAY + 48)
    result = replay(
        histories,
        plans,
        DAY,
        DAY + 48,
        exit_signal=callback,
        costs=STRESS,
        limits=RiskLimits(initial_equity=300),
    )
    assert result["count"] == 1
    trade = result["trades"][0]
    assert trade["legs"][0]["reason"] == "10-day channel exit"
