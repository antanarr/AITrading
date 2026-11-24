from dataclasses import replace

import pytest

from copybot.kraken_venue_study import ENTRY_CAP, _candle_page, _kraken_funding, _utc, _venue_plans
from copybot.research_models import HOUR_MS, Bar, MarketHistory, OrderPlan


def test_kraken_funding_uses_completed_hours_at_actual_cash_per_contract():
    first = 490000
    bars = {first: Bar(30, 32, 29, 31, 50)}
    funding = _kraken_funding(
        [
            {"timestamp": _utc(first - 1)},
            {"timestamp": _utc(first), "fundingRate": -0.001},
            {"timestamp": _utc(first + 1)},
            {"timestamp": _utc(first + 2)},
        ],
        bars,
        first,
        first + 2,
    )
    assert list(funding) == [first + 1]
    assert funding[first + 1].time_ms == (first + 1) * HOUR_MS
    assert funding[first + 1].rate * bars[first].close == pytest.approx(-0.001)


def test_kraken_candle_slice_filters_before_reading_unrelated_prices():
    row = dict(time=HOUR_MS, open="10", high="11", low="9", close="10.5", volume="7")
    rows = [{"time": 0}, row, {"time": 2 * HOUR_MS}]

    assert _candle_page(rows, 1, 2) == {1: Bar(10, 11, 9, 10.5, 7)}
    with pytest.raises(ValueError, match="conflicting candles"):
        _candle_page([row, {**row, "close": "10.8"}], 1, 2)


def test_venue_plans_use_common_opportunities_and_preserve_relative_risk():
    order = OrderPlan("SOL", 1, 100, 5, 2, None, 100, 0, "ioc", 2)
    native_bar = Bar(100, 100, 100, 100, 10)
    kraken_bar = Bar(120, 120, 120, 120, 10)
    native = MarketHistory("SOL", 2, False, None, {9: native_bar, 10: native_bar}, {})
    kraken = replace(native, bars={9: kraken_bar})
    common = _venue_plans(
        {10: [order], 11: [order]},
        {"SOL": native},
        {"hl": {"SOL": native}, "kraken": {"SOL": kraken}},
    )
    assert common["hl"].keys() == common["kraken"].keys() == {10}
    native_leg = common["hl"][10][0].legs[0]
    kraken_leg = common["kraken"][10][0].legs[0]
    assert native_leg.limit / native_bar.close - 1 == pytest.approx(float(ENTRY_CAP))
    assert kraken_leg.limit / kraken_bar.close - 1 == pytest.approx(float(ENTRY_CAP))
    assert kraken_leg.atr / kraken_bar.close == pytest.approx(native_leg.atr / native_bar.close)
