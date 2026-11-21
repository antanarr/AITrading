from dataclasses import replace

import numpy as np
import pytest

from copybot.public_pairs import (
    FIT_HOURS,
    PAIR_VARIANT,
    STOP_ATR,
    _weekly_fit,
    build_pair_plans,
    fit_spread,
    pair_exit_signal,
)
from copybot.research_data import ARCHIVE_END_HOUR
from copybot.research_models import Bar, MarketHistory

MONDAY = _weekly_fit(50_000)


def correlated_prices(hours):
    clock = np.arange(hours)
    hedge = 5 + clock * 0.001
    spread = 0.02 * np.sin(clock * 2 * np.pi / 40)
    return np.exp(2 + 0.7 * hedge + spread), np.exp(hedge)


def candle(price, volume=100_000):
    return Bar(price, price * 1.001, price * 0.999, price, volume)


def histories():
    asset, hedge = correlated_prices(FIT_HOURS + 10)
    asset[FIT_HOURS] *= 1.04
    return {
        coin: MarketHistory(
            coin,
            4,
            False,
            None,
            {
                hour: candle(float(price))
                for hour, price in zip(range(MONDAY - FIT_HOURS, MONDAY + 10), prices, strict=True)
            },
            {},
        )
        for coin, prices in (("SOL", asset), ("ETH", hedge))
    }


def test_fit_has_positive_log_beta_and_an_in_range_residual_half_life():
    asset, hedge = correlated_prices(FIT_HOURS)
    model, reason = fit_spread(asset, hedge)
    assert reason is None
    assert model.beta == pytest.approx(0.7, abs=0.01)
    assert 30 < model.half_life < 80
    assert fit_spread(1 / asset, hedge)[1] == "invalid_beta"
    assert fit_spread(asset[:-1], hedge[:-1])[1] == "incomplete_fit_prices"
    assert fit_spread(hedge, hedge)[1] == "degenerate_residual"


def test_pair_signal_uses_only_prior_closes_and_does_not_refit_midweek():
    markets = histories()
    plans, audit = build_pair_plans(markets, MONDAY, MONDAY + 10)
    assert list(plans) == [MONDAY + 1]
    assert audit["pair_coverage"]["SOL/ETH"]["accepted_fits"] == 1
    assert audit["pair_coverage"]["SOL/ETH"]["entry_plans"] == 1
    assert audit["pair_coverage"]["AVAX/SOL"]["missing_markets"] == ["AVAX"]
    trade = plans[MONDAY + 1][0]
    assert trade.label == PAIR_VARIANT
    own, hedge = trade.legs
    assert (own.coin, own.side, hedge.coin, hedge.side) == ("SOL", -1, "ETH", 1)
    assert (own.mode, hedge.mode) == ("ioc", "ioc")
    assert own.stop_atr == STOP_ATR
    assert hedge.weight / own.weight == pytest.approx(audit["fits"][0]["beta"])
    assert own.limit == pytest.approx(markets["SOL"].bars[MONDAY].close * 0.996)

    changed = markets["SOL"]
    changed = replace(changed, bars={**changed.bars, MONDAY + 1: candle(100_000)})
    new_plans, new_audit = build_pair_plans({**markets, "SOL": changed}, MONDAY, MONDAY + 10)
    assert new_plans[MONDAY + 1] == plans[MONDAY + 1]
    assert new_audit["fits"] == audit["fits"]


def test_exit_requires_traded_prior_closes_and_original_weekly_fit():
    markets = histories()
    plans, audit = build_pair_plans(markets, MONDAY, MONDAY + 10)
    trade = plans[MONDAY + 1][0]
    exit_signal = pair_exit_signal(markets, audit)
    assert exit_signal(trade, MONDAY + 1, MONDAY + 1) is None
    assert exit_signal(trade, MONDAY + 1, MONDAY + 2) == "spread reversion"
    final = markets["SOL"].bars[MONDAY + 1]
    halted = replace(
        markets["SOL"], bars={**markets["SOL"].bars, MONDAY + 1: replace(final, volume=0)}
    )
    assert (
        pair_exit_signal({**markets, "SOL": halted}, audit)(trade, MONDAY + 1, MONDAY + 2) is None
    )
    further_out = replace(
        markets["SOL"], bars={**markets["SOL"].bars, MONDAY + 1: candle(final.close * 1.2)}
    )
    assert (
        pair_exit_signal({**markets, "SOL": further_out}, audit)(trade, MONDAY + 1, MONDAY + 2)
        == "spread divergence"
    )


def test_pair_liquidity_and_archive_end_are_required():
    markets = histories()
    inactive = replace(
        markets["ETH"],
        bars={hour: replace(bar, volume=0) for hour, bar in markets["ETH"].bars.items()},
    )
    plans, audit = build_pair_plans({**markets, "ETH": inactive}, MONDAY, MONDAY + 10)
    assert not plans
    assert audit["pair_coverage"]["SOL/ETH"]["inactive_fit_candles"] == 1
    plans, audit = build_pair_plans(markets, MONDAY, MONDAY + 1)
    assert not plans
    assert audit["pair_coverage"]["SOL/ETH"]["accepted_fits"] == 1
    with pytest.raises(ValueError, match="development cutoff"):
        build_pair_plans({}, ARCHIVE_END_HOUR - 1, ARCHIVE_END_HOUR + 1)
