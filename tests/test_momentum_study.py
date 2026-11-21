from dataclasses import replace

import pytest

from copybot.momentum_study import AGREEMENT, HOLD_HOURS, MOMENTUM, build_momentum_plans
from copybot.research_data import ARCHIVE_END_HOUR
from copybot.research_models import DAY_HOURS, Bar, MarketHistory

MONDAY = 168 * 300 - 72


def market(coin, rising, confirms):
    bars = {}
    for day in range(-32, 10):
        price = 100 + (day + 32) + (2 if day % 3 == 0 else 0)
        if not confirms and -7 <= day < 0:
            price = 125 - (day + 7)
        if not rising:
            price = 250 - price
        for hour in range(MONDAY + day * DAY_HOURS, MONDAY + (day + 1) * DAY_HOURS):
            bars[hour] = Bar(price, price + 1, price - 1, price, 10_000)
    return MarketHistory(coin, 3, False, None, bars, {})


def test_own_return_sign_and_short_return_agreement_are_distinct():
    assets = [market("ETH", True, False), market("SOL", False, True), market("wBTC", True, True)]
    histories = {item.coin: item for item in assets}
    picks = build_momentum_plans(histories, MONDAY, MONDAY + 24, "monday")
    left = {plan.legs[0].coin: plan.legs[0].side for plan in picks[MOMENTUM][MONDAY]}
    right = {plan.legs[0].coin: plan.legs[0].side for plan in picks[AGREEMENT][MONDAY]}
    assert left == {"ETH": 1, "SOL": -1}
    assert right == {"SOL": -1}
    assert all(plan.legs[0].hold_hours == HOLD_HOURS for plan in picks[MOMENTUM][MONDAY])


def test_completed_day_and_clock_are_strictly_causal():
    asset = market("ETH", True, True)
    initial = build_momentum_plans({"ETH": asset}, MONDAY, MONDAY + 24, "monday")
    edited = replace(asset, bars={**asset.bars, MONDAY: Bar(1, 700, 1, 700, 70_000)})
    checked = build_momentum_plans({"ETH": edited}, MONDAY, MONDAY + 24, "monday")
    assert initial == checked
    alternate = build_momentum_plans({"ETH": asset}, MONDAY, MONDAY + 4 * 24, "thursday")
    assert list(alternate[MOMENTUM]) == [MONDAY + 3 * 24]


def test_missing_warmup_candle_blocks_signal_and_cutoff_precedes_reserve():
    asset = market("ETH", True, True)
    candles = dict(asset.bars)
    del candles[MONDAY - 8 * 24]
    picks = build_momentum_plans(
        {"ETH": replace(asset, bars=candles)}, MONDAY, MONDAY + 24, "monday"
    )
    assert not picks[MOMENTUM] and not picks[AGREEMENT]
    with pytest.raises(ValueError, match="development cutoff"):
        build_momentum_plans({}, ARCHIVE_END_HOUR - 1, ARCHIVE_END_HOUR + 1, "monday")
