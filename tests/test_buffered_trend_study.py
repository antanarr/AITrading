from dataclasses import replace
from math import exp, sin

from copybot.buffered_trend_study import build_plans, make_exit
from copybot.research_models import DAY_HOURS, Bar, MarketHistory


def history(coin, start, end):
    bars = {}
    for hour in range(start, end):
        day = (hour - start) // DAY_HOURS
        price = 100 * exp(0.01 * day + 0.005 * sin(day))
        bars[hour] = Bar(price, price + 1, price - 1, price, 1000)
    return MarketHistory(coin, 2, False, None, bars, {})


def test_entry_uses_completed_days_and_requires_full_warmup():
    start = 60 * DAY_HOURS
    source = history("SOL", DAY_HOURS, start + DAY_HOURS + 1)
    native_btc = replace(source, coin="BTC")
    plans, observations = build_plans({"SOL": source, "BTC": native_btc}, start, start + 1, 0)
    assert list(plans) == [start]
    assert [(plan.legs[0].coin, plan.legs[0].side) for plan in plans[start]] == [("SOL", 1)]
    expected = plans[start][0]
    changed = replace(source, bars={**source.bars, start: Bar(200, 350, 90, 330, 40_000)})
    assert build_plans({"SOL": changed}, start, start + 1, 0)[0][start] == [expected]
    assert len(observations) == 1
    incomplete = replace(
        source,
        bars={hour: bar for hour, bar in source.bars.items() if hour != start - 4 * DAY_HOURS},
    )
    assert build_plans({"SOL": incomplete}, start, start + 1, 0)[0] == {}


def test_buffered_exit_waits_for_a_larger_opposing_trend():
    start = 60 * DAY_HOURS
    plans, _ = build_plans({"SOL": history("SOL", DAY_HOURS, start)}, start, start + 1, 0)
    [order] = plans[start]
    scores = {(start + 24, "SOL"): -0.1, (start + 48, "SOL"): -0.6}
    assert make_exit(scores, 0)(order, start, start + 24) == "trend reversal"
    assert make_exit(scores, 0.5)(order, start, start + 24) is None
    assert make_exit(scores, 0.5)(order, start, start + 48) == "trend reversal"
