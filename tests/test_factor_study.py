import json
import sys
from dataclasses import replace
from datetime import date
from math import exp

import pytest

from copybot import factor_study
from copybot.factor_study import _Features, _features, _Portfolio, _weights, replay_factor
from copybot.research_execution import BASE, STRESS
from copybot.research_models import HOUR_MS, Bar, Funding, MarketHistory


def flat_bar(price=100, volume=1_000):
    return Bar(price, price, price, price, volume)


def market(coin, bars, funding):
    return MarketHistory(coin, 3, True, None, bars, funding)


def ranking(coins):
    return {coin: _Features(1.0, {"funding-24h": -rank}) for rank, coin in enumerate(coins)}


def test_factors_use_only_contiguous_prices_and_funding_strictly_before_decision():
    decision = 168
    bars = {
        hour: flat_bar(100 * exp(hour * 0.001), volume=250_000) for hour in range(-1, decision + 1)
    }
    funding = {hour: Funding(hour * HOUR_MS + 1, -0.002) for hour in range(decision - 24, decision)}
    record = market("A", bars, funding)
    before = _features(record, decision, 1_000_000)
    assert before.scores["funding-24h"] == pytest.approx(0.002)
    assert before.scores["momentum-168h"] > 0
    assert before.scores["reversal-24h"] < 0
    future = replace(
        record,
        bars={**bars, decision: flat_bar(1_000_000)},
        funding={**funding, decision: Funding(decision * HOUR_MS, 0.9)},
    )
    assert _features(future, decision, 1_000_000) == before
    settlement_at_boundary = replace(
        record, funding={**funding, decision - 1: Funding(decision * HOUR_MS, 0.9)}
    )
    assert "funding-24h" not in _features(settlement_at_boundary, decision, 1_000_000).scores
    missing = replace(record, bars={hour: bar for hour, bar in bars.items() if hour != 20})
    assert _features(missing, decision, 1_000_000) is None


def test_rotation_preserves_rank_six_incumbents_and_never_selects_the_same_coin_twice():
    features = ranking(["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"])
    features["D"] = _Features(2.0, features["D"].scores)
    held = {"D": 1, "F": 1, "H": 1, "E": -1, "G": -1, "B": -1}
    weights, eligible = _weights(features, "funding-24h", held)
    assert eligible == 10
    assert {coin for coin, weight in weights.items() if weight > 0} == {"A", "D", "F"}
    assert {coin for coin, weight in weights.items() if weight < 0} == {"E", "G", "J"}
    assert weights["D"] == pytest.approx(weights["A"] / 2)

    six = ranking(["A", "B", "C", "D", "E", "F"])
    weights, _ = _weights(six, "funding-24h", {"A": -1, "E": 1, "F": 1})
    assert len(weights) == 6
    assert sum(weight for weight in weights.values() if weight > 0) == pytest.approx(0.5)
    assert sum(weight for weight in weights.values() if weight < 0) == pytest.approx(-0.5)
    assert weights["A"] < 0 and weights["E"] > 0 and weights["F"] > 0
    assert _weights(ranking(["A", "B", "C", "D", "E"]), "funding-24h", {}) == ({}, 5)


def test_equal_weight_eight_leg_variant_retains_incumbents_through_rank_eight():
    coins = list("ABCDEFGHIJKLMNOP")
    features = ranking(coins)
    features["H"] = _Features(2.0, features["H"].scores)
    weights, eligible = _weights(
        features, "funding-24h", {"H": 1, "I": 1, "J": -1}, legs_per_side=4, weighting="equal"
    )
    assert eligible == 16
    assert len(weights) == 8
    assert weights["H"] == 0.125
    assert weights["J"] == -0.125
    assert "I" not in weights
    assert set(map(abs, weights.values())) == {0.125}


@pytest.mark.parametrize("costs", [BASE, STRESS])
def test_unchanged_incumbents_only_pay_open_and_close_friction(costs):
    coins = ["A", "B", "C", "D", "E", "F"]
    start = 240
    end = start + 48
    histories = {
        coin: market(
            coin,
            {hour: flat_bar() for hour in range(start - 1, end)},
            {hour: Funding(hour * HOUR_MS + 1, 0.0) for hour in range(start, end)},
        )
        for coin in coins
    }
    frames = {start: ranking(coins), start + 24: ranking(coins)}
    row = replay_factor(histories, frames, "funding-24h", 24, start, end, costs=costs)
    assert row["status"] == "complete"
    (opening, retention) = row["rebalances"]
    assert opening["orders"] == 6
    assert opening["minimum_equity"] == pytest.approx(60.6)
    assert opening["net_imbalance"] == pytest.approx(0)
    assert retention["orders"] == 0 and retention["buffered_incumbents"] == coins
    assert row["activity"]["taker_orders"] == 12
    assert row["gross_price"] == row["funding"] == row["maker_rebate"] == 0
    assert row["fees"] == pytest.approx(2 * 999.6 * costs.taker_fee_bps / 10_000)
    assert row["slippage"] == pytest.approx(
        999.6
        * (2 * costs.half_spread_bps + costs.entry_impact_bps + costs.exit_impact_bps)
        / 10_000
    )
    assert row["net"] == pytest.approx(-row["fees"] - row["slippage"])
    assert row["net"] == pytest.approx(sum(row["by_coin"].values()))
    assert row["daily_drawdown"] == pytest.approx(-row["net"])
    assert row["average_daily_turnover_pct"] > 99


def test_funding_uses_the_exposed_book_at_the_settlement_boundary():
    coins = ["A", "B", "C", "D", "E", "F"]
    start = 240
    hours = range(start, start + 2)
    bars = {hour: flat_bar() for hour in range(start - 1, start + 2)}
    funding = {
        start: Funding(start * HOUR_MS, 0.71),
        start + 1: Funding((start + 1) * HOUR_MS + 1, 0.002),
    }
    histories = {
        coin: market(
            coin,
            bars,
            funding if coin == "A" else {hour: Funding(hour * HOUR_MS, 0.0) for hour in hours},
        )
        for coin in coins
    }
    row = replay_factor(histories, {start: ranking(coins)}, "funding-24h", 24, start, start + 2)
    assert row["funding"] == pytest.approx(-1.666 * 100 * 0.002)
    assert row["funding_paid"] == pytest.approx(-row["funding"])
    assert row["funding_received"] == 0
    assert row["by_sleeve"]["long"]["funding"] == row["funding"]
    assert row["by_sleeve"]["short"]["funding"] == 0


@pytest.mark.parametrize(("prior_lots", "skip_delta"), [(260, True), (500, False)])
def test_drift_resizes_only_when_the_delta_clears_the_minimum(prior_lots, skip_delta):
    coins = ["A", "B", "C", "D", "E", "F"]
    histories = {coin: market(coin, {240: flat_bar()}, {}) for coin in coins}
    portfolio = _Portfolio(histories, STRESS, 1_000)
    portfolio.positions["A"] = prior_lots
    portfolio.prices["A"] = 100
    features = ranking(coins)
    features["A"] = _Features(12.0, features["A"].scores)

    portfolio.rebalance("funding-24h", features, 240)
    (event,) = portfolio.rebalances
    assert event["buffered_incumbents"] == []
    if skip_delta:
        assert event["skipped_small_deltas"] == ["A"]
        assert portfolio.positions["A"] == prior_lots
        assert "A" not in portfolio.by_coin or portfolio.by_coin["A"] == 0
    else:
        assert event["skipped_small_deltas"] == []
        assert portfolio.positions["A"] * 0.1 == pytest.approx(20, abs=0.1)
        sold = (prior_lots - portfolio.positions["A"]) * 0.001
        expected_cost = sold * 100 * (STRESS.half_spread_bps + STRESS.exit_impact_bps) / 10_000
        expected_cost += sold * 99.72 * STRESS.taker_fee_bps / 10_000
        assert portfolio.by_coin["A"] == pytest.approx(-expected_cost)


@pytest.mark.parametrize("equity", [1_000, 200])
def test_neutrality_override_uses_deltas_and_reports_residual_blocked_by_minimum(equity):
    coins = list("ABCD")
    histories = {coin: market(coin, {240: flat_bar()}, {}) for coin in coins}
    portfolio = _Portfolio(histories, BASE, equity, legs_per_side=2, neutrality_band=0.05)
    target = int(equity * 2.5)
    shift = 300 if equity == 1_000 else 60
    portfolio.positions = {
        "A": target + shift,
        "B": target + shift,
        "C": shift - target,
        "D": shift - target,
    }
    portfolio.prices = dict.fromkeys(coins, 100)

    portfolio.rebalance("funding-24h", ranking(coins), 240)
    (event,) = portfolio.rebalances
    assert event["buffered_projected_imbalance_pct_gross"] == pytest.approx(12)
    assert set(portfolio.positions) == set(coins)
    if equity == 1_000:
        assert event["orders"] == 4
        assert event["neutrality_correction"] == "within_band"
        assert event["neutrality_full_rebalance_used"]
        assert event["net_imbalance_pct_gross"] == pytest.approx(0)
        assert portfolio.day_turnover == pytest.approx(120, abs=0.10)
    else:
        assert event["orders"] == 0
        assert event["neutrality_correction"] == "delta_below_minimum"
        assert not event["neutrality_full_rebalance_used"]
        assert event["neutrality_blocked_markets"] == coins
        assert event["net_imbalance_pct_gross"] == pytest.approx(12)
        assert portfolio.day_turnover == 0


def test_four_leg_rotation_frees_slots_before_opening_replacements(monkeypatch):
    coins = list("ABCDEFGHIJKL")
    histories = {coin: market(coin, {240: flat_bar()}, {}) for coin in coins}
    portfolio = _Portfolio(histories, BASE, 1_000, legs_per_side=2)
    portfolio.positions = {"E": 2500, "F": 2500, "G": -2500, "H": -2500}
    portfolio.prices = dict.fromkeys(portfolio.positions, 100)
    position_counts = []
    trade = portfolio.trade

    def count_trade(*args):
        trade(*args)
        position_counts.append(len(portfolio.positions))

    monkeypatch.setattr(portfolio, "trade", count_trade)
    portfolio.rebalance("funding-24h", ranking(coins), 240)
    assert set(portfolio.positions) == {"A", "B", "K", "L"}
    assert position_counts == [3, 2, 1, 0, 1, 2, 3, 4]


@pytest.mark.parametrize("offset", [0, 1, 2])
def test_72_hour_offsets_change_the_rebalance_clock_and_preserve_calendar_days(offset):
    start = 240
    end = start + 168
    row = replay_factor({}, {}, "funding-24h", 72, start, end, clock_offset_days=offset)
    assert [event["hour"] for event in row["rebalances"]] == list(
        range(start + offset * 24, end, 72)
    )
    assert len(row["daily"]) == 7
    assert all(day["return"] == 0 for day in row["daily"])


def test_cli_records_and_enforces_explicit_coin_exclusions(tmp_path, monkeypatch):
    start = factor_study.day_hour(date(2026, 5, 25))
    end = factor_study.ARCHIVE_END_HOUR
    bars = {start: flat_bar(), end - 1: flat_bar()}
    histories = {coin: market(coin, bars, {}) for coin in ("SKR", "PONS", "DOGE", "BTC")}
    monkeypatch.setattr(factor_study, "load_archive", lambda _archive, _end: (histories, {}))
    partitions = []

    def record_period(scoped, starts, ends, turnover, **settings):
        partitions.append((set(scoped), settings))
        return {"start": starts, "end": ends, "results": []}

    monkeypatch.setattr(factor_study, "study_period", record_period)
    output = tmp_path / "result.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "factor_study",
            str(tmp_path),
            str(output),
            "--funding-72-only",
            "--exclude-coin",
            "skr",
            "--exclude-coin",
            "PONS",
            "--legs-per-side",
            "4",
            "--weighting",
            "equal",
            "--clock-offset-days",
            "2",
        ],
    )
    factor_study.main()
    assert len(partitions) == 2
    assert all(universe == {"DOGE"} for universe, _ in partitions)
    assert all(
        settings["funding_72_only"] and settings["clock_offset_days"] == 2
        for _, settings in partitions
    )
    assert all(
        settings["legs_per_side"] == 4 and settings["weighting"] == "equal"
        for _, settings in partitions
    )
    assert json.loads(output.read_text())["excluded_coins"] == ["PONS", "SKR"]


@pytest.mark.parametrize("missing", ["held market", "settlement"])
def test_missing_held_data_is_reported_on_the_affected_cohort(missing):
    coins = ["A", "B", "C", "D", "E", "F"]
    start = 240
    bars = {hour: flat_bar() for hour in range(start - 1, start + 2)}
    funding = {hour: Funding(hour * HOUR_MS + 1, 0.0) for hour in (start, start + 1)}
    histories = {coin: market(coin, bars, funding) for coin in coins}
    if missing == "held market":
        histories["A"] = replace(
            histories["A"], bars={hour: bar for hour, bar in bars.items() if hour <= start}
        )
    else:
        histories["A"] = replace(histories["A"], funding={start: funding[start]})

    row = replay_factor(histories, {start: ranking(coins)}, "funding-24h", 24, start, start + 2)
    assert row["status"] == "incomplete"
    assert row["stopped_hour"] == start + 1
    assert (
        "archived candle series ended" if missing == "held market" else "missing funding"
    ) in row["incomplete_reason"]
