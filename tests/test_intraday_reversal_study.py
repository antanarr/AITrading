from dataclasses import replace

from copybot.intraday_reversal_study import build_reversal_plans
from copybot.research_models import Bar, MarketHistory


def market(impulse, coin="SOL"):
    standard = Bar(100, 101, 99, 100, 1_000)
    return MarketHistory(
        coin, 2, False, None, {**dict.fromkeys(range(40, 76), standard), 70: impulse}, {}
    )


def test_failed_range_break_only_signals_after_the_hour_closes():
    history = market(Bar(100, 104, 100, 100, 5_000))
    result = build_reversal_plans({"SOL": history}, 70, 73)
    for rows in result.values():
        assert 70 not in rows
        leg = rows[71][0].legs[0]
        assert leg.side == -1
        assert leg.limit == 99.6
        assert leg.atr == 2
        assert leg.stop_atr == leg.target_atr == 1.5
        assert leg.hold_hours == 6

    changed_future = replace(history, bars={**history.bars, 71: Bar(110, 130, 90, 120, 5_000)})
    changed = build_reversal_plans({"SOL": changed_future}, 70, 73)
    assert all(changed[name][71] == rows[71] for name, rows in result.items())


def test_thin_volume_and_two_sided_excursions_are_skipped():
    for impulse in (Bar(100, 104, 100, 100, 1_000), Bar(100, 104, 96, 100, 5_000)):
        result = build_reversal_plans({"SOL": market(impulse)}, 70, 73)
        assert all(71 not in rows for rows in result.values())
