from decimal import Decimal
from unittest.mock import Mock

import pytest

from copybot.backrun import HOUR, Scenario, exit_signal
from copybot.strategy_study import CONFIG, Momentum, RangeReentry, closed_ranges


def test_four_hour_ranges_only_use_aligned_closed_candles():
    tape = Mock(signal_step=HOUR)
    tape.previous_bars.return_value = [
        {"t": i * HOUR, "h": str(104 + i), "l": str(99 - i), "c": str(101 + i)} for i in range(4)
    ]
    assert closed_ranges(tape, "BTC", 4 * HOUR, 4 * HOUR, 1) == [(107, 96, 104)]
    tape.previous_bars.assert_called_once_with("BTC", 4 * HOUR, 4)
    with pytest.raises(ValueError, match="aligned"):
        closed_ranges(tape, "BTC", 5 * HOUR, 4 * HOUR, 1)


def test_range_reentry_requires_a_prior_excursion_and_current_confirmation():
    rule = RangeReentry(Mock(), HOUR, lookback=51, hold_bars=12)
    range_bars = [(Decimal(102), Decimal(98), Decimal(100 + i % 2)) for i in range(49)]
    excursion = (Decimal(101), Decimal(97), Decimal(98))
    recovered = (Decimal(101), Decimal(98), Decimal(100))
    assert rule.setup(range_bars + [excursion, recovered]) == (True, Decimal("99.5"))
    assert rule.setup(range_bars + [excursion, excursion]) is None
    # A move of several prior hourly ranges disqualifies the fade.
    trending = [(Decimal(119), Decimal(115), Decimal(117)), *range_bars[1:]]
    assert rule.setup(trending + [excursion, recovered]) is None


@pytest.mark.parametrize("side,closes,expected_stop", [(1, (104, 102), 102), (-1, (96, 98), 98)])
def test_trailing_stop_only_tightens_and_can_exit_at_an_adverse_gap(side, closes, expected_stop):
    tape = Mock(signal_step=HOUR, decimals={"BTC": 5})
    tape.previous_bars.side_effect = [
        [{"t": i * HOUR, "h": str(close + 1), "l": str(close - 1), "c": str(close)}]
        for i, close in enumerate(closes)
    ]
    rule = Momentum(tape, HOUR, lookback=49, hold_bars=36, channel=20)
    position = {
        "coin": "BTC",
        "side": side,
        "entry": Decimal(100),
        "entered": 0,
        "best_close": Decimal(100),
        "stop_distance": Decimal(2),
        "stop": Decimal(100 - side * 2),
        "target": None,
    }
    rule.update_stop(tape, position, HOUR)
    rule.update_stop(tape, position, 2 * HOUR)
    assert position["stop"] == expected_stop
    tape.open_bar.return_value = {"o": "100"}
    assert exit_signal(tape, position, 2 * HOUR, CONFIG, Scenario("test"), intrabar=False) == (
        "stop gap",
        Decimal(100),
    )
