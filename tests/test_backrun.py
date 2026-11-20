from decimal import Decimal
from unittest.mock import Mock

from copybot.autopilot import Settings
from copybot.backrun import BAR_MS, STEP, Scenario, combine_15m, exit_signal


def test_candle_combination_preserves_boundaries_and_volume():
    candles = [
        {
            "t": i * STEP,
            "T": (i + 1) * STEP - 1,
            "s": "BTC",
            "o": str(100 + i),
            "c": str(101 + i),
            "h": str(103 + i),
            "l": str(99 + i),
            "v": "5",
        }
        for i in range(3)
    ]
    assert combine_15m(candles) == [
        {
            "t": 0,
            "T": BAR_MS - 1,
            "s": "BTC",
            "i": "15m",
            "o": "100",
            "c": "103",
            "h": "105",
            "l": "99",
            "v": "15",
        }
    ]


def test_ambiguous_candle_uses_the_adverse_exit_and_reversal_needs_closed_bar():
    tape = Mock()
    tape.open_bar.return_value = {"o": "100", "h": "102", "l": "98"}
    tape.prior_close.return_value = Decimal("99")
    cfg = Settings(markets=["BTC"], max_order_usd="11")
    trade = {
        "coin": "BTC",
        "side": 1,
        "entered": BAR_MS,
        "entry": Decimal("100"),
        "stop": Decimal("99"),
        "breakout": Decimal("99.50"),
    }
    assert exit_signal(tape, trade, BAR_MS, cfg, Scenario("test"), intrabar=True) == (
        "stop",
        Decimal(99),
    )
    assert exit_signal(tape, trade, BAR_MS + STEP, cfg, Scenario("test"), intrabar=False) is None
    tape.prior_close.assert_not_called()
    assert exit_signal(tape, trade, 2 * BAR_MS, cfg, Scenario("test"), intrabar=False) == (
        "failed breakout",
        Decimal(100),
    )
