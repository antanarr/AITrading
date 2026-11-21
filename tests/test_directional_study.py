import argparse
from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from copybot import directional_study as study
from copybot.research_models import Bar, MarketHistory


def test_training_uses_only_outcomes_already_observed_at_fold_start():
    frame = pd.DataFrame(
        {
            "hour": [2, 3, 4, 5, 6, 7],
            "exit_hour_4": [5, 6, 7, 8, 6, 9],
            "price_return_4": [0.1, 0.2, -0.1, 0, 0.1, np.nan],
            "funding_return_4": [0, 0, 0, 0, 0, 0],
        }
    )
    fold = study.Fold(3, 7, 12, "validation")
    assert list(study.fit_rows(frame, fold, 4).hour) == [3, 6]


def test_hybrid_uses_model_for_disagreement_and_preserves_decision_with_unknown_exit():
    test = pd.DataFrame(
        {
            "hour": [4, 8, 8, 12],
            "coin": ["A", "A", "B", "A"],
            "tech_1h_return_24": [0.10, 0.10, 0.10, 0.10],
            "market_meme_breadth_24h": [0.60, 0.35, 0.35, 0.35],
            "boost": [0.15, 0.20, 0.30, 0.52],
            "price_return_4": [0.1, np.nan, 0.5, 0.1],
        }
    )
    picks = study.choose_signals(test, 4, "hybrid")
    assert list(zip(picks.hour, picks.coin, picks.side, strict=True)) == [(4, "A", 1), (8, "A", -1)]
    assert np.isnan(picks.iloc[1].price_return_4)


def test_taker_replay_signs_funding_and_conserves_pnl():
    row = SimpleNamespace(
        hour=0,
        entry_hour=1,
        exit_hour_4=5,
        coin="DOGE",
        group="meme",
        entry_price=10,
        price_return_4=0.03,
        funding_return_4=-0.001,
        side=-1,
        market_meme_breadth_24h=0.25,
    )
    hist = MarketHistory(
        "DOGE", 1, True, None, {h: Bar(10, 10.4, 10, 10, 1) for h in range(1, 5)}, {}
    )
    parts = study.trade_components(row, hist, 4, study.STRESS, 100)
    assert parts["gross"] < 0 < parts["funding"]
    assert parts["net"] == pytest.approx(
        parts["gross"] + parts["funding"] - parts["slippage"] - parts["fees"]
    )
    assert parts["worst_open_pnl"] < parts["net"]


def test_historical_cutoff_blocks_access_before_archive_loading(monkeypatch):
    def should_not_load(*args, **kwargs):
        pytest.fail("archive load happened before cutoff check")

    monkeypatch.setattr(study, "load_archive", should_not_load)
    args = argparse.Namespace(
        start=date(2026, 5, 25), validation_start=date(2026, 9, 26), end=date(2026, 11, 22)
    )
    with pytest.raises(ValueError, match="development archive cutoff"):
        study.run(args)


def test_equal_side_basket_selects_disjoint_legs_and_can_abstain():
    test = pd.DataFrame(
        {
            "hour": [12] * 5,
            "coin": list("ABCDE"),
            "relative": [-0.02, -0.01, 0.0, 0.02, 0.03],
        }
    )
    basket = study.choose_baskets(test, 12, 0, gated=True)
    assert dict(zip(basket.coin, basket.side, strict=True)) == {"A": -1, "B": -1, "D": 1, "E": 1}
    test["relative"] *= 0.1
    assert study.choose_baskets(test, 12, 0, gated=True).empty


def test_only_exploratory_variant_reads_retrospective_headlines():
    features = pd.DataFrame({"tech_momentum": [0.1], "news_fed": [1], "news_media_tone": [0.25]})
    assert list(study.market_data(features, "context")) == ["tech_momentum", "news_fed"]
    assert list(study.market_data(features, "media")) == list(features)


def test_inverse_volatility_basket_preserves_half_budget_per_side():
    test = pd.DataFrame(
        {
            "hour": [12] * 5,
            "coin": list("ABCDE"),
            "relative": [-2, -1, 0, 1, 2],
            "tech_1h_realized_vol": [0.001, 0.20, 0.003, 0.01, 0.03],
        }
    )
    basket = study.choose_baskets(test, 12, 0, gated=False, relative="volatility")
    assert basket.groupby("side").weight.sum().to_dict() == pytest.approx({-1: 0.5, 1: 0.5})
    assert basket.weight.between(0.15, 0.35).all()
