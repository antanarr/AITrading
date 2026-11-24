from dataclasses import replace

import pytest

from copybot.intraday_move_study import build_candidates
from copybot.research_data import ARCHIVE_END_HOUR
from copybot.research_models import Bar, MarketHistory

HOUR = ARCHIVE_END_HOUR - 25


def books(aligned=6, side=1):
    histories = {}
    for n in range(8):
        coin = f"ALT{n}"
        bars = dict.fromkeys(range(HOUR - 25, HOUR + 3), Bar(100, 101, 99, 100, 1_000))
        move = 2.5 if n < aligned else -1
        close = 100 + side * move
        bars[HOUR - 1] = Bar(100, max(101, close), min(99, close), close, 1_000)
        histories[coin] = MarketHistory(coin, 2, False, None, bars, {})
    return histories


@pytest.mark.parametrize("side", [1, -1])
def test_breakout_uses_completed_hour_and_ranks_two_positions(side):
    histories = books(side=side)
    histories["BTC"] = replace(histories["ALT0"], coin="BTC")
    plans, info = build_candidates(histories, HOUR, HOUR + 1)
    assert info["orders"] == 2
    assert set(plans["ioc-target-6h"]) == {HOUR}
    legs = [plan.legs[0] for plan in plans["ioc-target-6h"][HOUR]]
    assert [(leg.coin, leg.side, leg.atr) for leg in legs] == [
        ("ALT0", side, 2),
        ("ALT1", side, 2),
    ]
    assert legs[0].limit == pytest.approx((100 + 2.5 * side) * (1 + 0.004 * side))
    maker = plans["maker-target-6h"][HOUR][0].legs[0]
    assert maker.limit == pytest.approx(100 + (2.5 - 0.4) * side)
    assert maker.ttl_hours == 2
    for history in histories.values():
        history.bars[HOUR] = Bar(100, 200, 1, 100 + 50 * side, 1_000_000)
    assert build_candidates(histories, HOUR, HOUR + 1) == (plans, info)


def test_breadth_gate_blocks_individual_impulses_and_reserve_is_rejected():
    plans, info = build_candidates(books(aligned=5), HOUR, HOUR + 1)
    assert info["broad_impulse_hours"] == 0
    assert all(not orders for orders in plans.values())
    with pytest.raises(ValueError, match="Development"):
        build_candidates(books(), HOUR, ARCHIVE_END_HOUR + 1)
