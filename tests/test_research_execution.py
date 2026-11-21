import json
from dataclasses import replace

import pytest

from copybot.research_execution import BASE, STRESS, CostModel, RiskLimits, replay
from copybot.research_models import HOUR_MS, Bar, Funding, MarketHistory, OrderPlan, TradePlan

COSTLESS = CostModel("costless", 0, 0, 0, maker_fee_bps=0, taker_fee_bps=0)


def bar(open_=100, high=100, low=100, close=100, *, volume=1_000):
    return Bar(open_, high, low, close, volume)


def history(bars, coin="DOGE", *, decimals=2, funding=None, quarters=None):
    return MarketHistory(coin, decimals, True, None, bars, funding or {}, quarters or {})


def order(coin="DOGE", side=1, limit=100, **changes):
    plan = OrderPlan(coin, side, limit, 1, 2, 2, 12, 2)
    return replace(plan, **changes)


def run(market, plan=None, *, end=4, costs=BASE, limits=None):
    bet = TradePlan((plan or order(),), "one")
    return replay(
        {market.coin: market}, {0: [bet]}, 0, end, costs=costs, limits=limits or RiskLimits()
    )


def test_adaptive_sizing_uses_equity_and_withholds_profit_highs():
    market = history(
        {0: bar(high=105), 1: bar(low=95), 2: bar(high=105)},
        quarters=quarter_hour(0, [bar(), bar(high=105), bar(), bar()]),
    )
    plan = TradePlan((order(mode="ioc"),), "directional")
    signals = {hour: [plan] for hour in range(3)}
    scores = [
        replay(
            {market.coin: market},
            signals,
            0,
            3,
            costs=COSTLESS,
            limits=RiskLimits(initial_equity=equity, max_leg_fraction=0.25),
            profit_lock_fraction=0.25,
        )
        for equity in (300, 500)
    ]
    assert [score["trades"][0]["legs"][0]["size"] for score in scores] == [0.75, 1.25]
    for score, equity in zip(scores, (300, 500), strict=True):
        first, second, third = score["trades"]
        assert second["sizing"]["equity_at_submission"] == pytest.approx(
            equity + first["net"] * 0.75
        )
        assert third["sizing"]["equity_at_submission"] < second["sizing"]["equity_at_submission"]
        assert third["sizing"]["profit_excluded_from_sizing"] == pytest.approx(first["net"] * 0.25)


def test_shared_slots_keep_a_strategy_from_taking_every_position():
    markets = {coin: history({0: bar(), 1: bar()}, coin) for coin in ("DOGE", "SOL", "ETH")}
    ideas = {
        0: [
            TradePlan((order(coin=coin, mode="ioc"),), label)
            for coin, label in zip(markets, ("trend", "trend", "reversion"), strict=True)
        ]
    }
    score = replay(
        markets,
        ideas,
        0,
        2,
        costs=COSTLESS,
        limits=RiskLimits(initial_equity=500, max_positions=2, max_leg_fraction=0.25),
        slots_by_strategy={"trend": 1, "reversion": 1},
    )
    assert [trade["label"] for trade in score["trades"]] == ["trend", "reversion"]
    assert score["submission_diagnostics"]["replay"]["rejected"]["slot_limit"] == 1


def test_gap_proxy_is_labeled_and_signal_exit_uses_next_open():
    market = history({0: bar(), 1: bar(101, 101, 101, 101)})
    signals = {0: [TradePlan((order(mode="ioc"),), "test")]}
    proxy = replay(
        {market.coin: market},
        signals,
        0,
        3,
        costs=COSTLESS,
        data_gap_proxy=True,
        limits=RiskLimits(initial_equity=500),
    )
    assert proxy["terminal_proxy_exits"] == 1
    assert proxy["trades"][0]["legs"][0]["reason"] == "data gap"
    decisions = []

    def exit_signal(plan, submitted, current):
        decisions.append((plan.label, submitted, current))
        return "spread reversion"

    closed = replay(
        {market.coin: market},
        signals,
        0,
        2,
        costs=COSTLESS,
        exit_signal=exit_signal,
        limits=RiskLimits(initial_equity=500),
    )
    assert decisions == [("test", 0, 1)]
    assert closed["trades"][0]["legs"][0]["exit"] == 101
    assert closed["trades"][0]["legs"][0]["reason"] == "spread reversion"


def test_maker_requires_later_trade_through_and_expires_without_cost():
    market = history({0: bar(low=99), 1: bar(low=100), 2: bar(low=99), 3: bar()})
    for ttl in (0, 1):
        result = run(market, order(ttl_hours=ttl))
        assert result["count"] == 0
        assert result["fees"] == result["slippage"] == result["net"] == 0
        assert result["maker_fill_rate"] == 0
    result = run(market)
    assert result["count"] == 1
    assert result["trades"][0]["entered_ms"] == 3 * HOUR_MS - 1

    immediate_expiry = run(market, order(ttl_hours=0))
    assert immediate_expiry["maker_orders"] == 0
    assert (
        immediate_expiry["submission_diagnostics"]["replay"]["rejected"]["expired_on_submission"]
        == 1
    )


@pytest.mark.parametrize("side, limit", [(1, 100.03), (-1, 99.97)])
def test_post_only_rejects_order_at_opening_executable_quote(side, limit):
    market = history({0: bar(), 1: bar(high=101, low=99), 2: bar(), 3: bar()})
    result = run(market, order(side=side, limit=limit))
    assert result["count"] == 0
    assert result["maker_orders"] == 0
    diagnostics = result["submission_diagnostics"]
    assert diagnostics["replay"]["rejected"]["post_only_cross"] == 1
    assert diagnostics["empty_book"]["1000"]["rejected"]["post_only_cross"] == 1


def test_profit_limit_gets_maker_fee_and_requires_sequence_after_fill():
    market = history({0: bar(), 1: bar(high=103, low=99), 2: bar(high=102), 3: bar(high=103)})
    result = run(market)
    leg = result["trades"][0]["legs"][0]
    assert leg["exited_ms"] == 4 * HOUR_MS - 1
    assert leg["entry"] == 100
    assert leg["exit"] == 102
    assert result["slippage"] == 0
    assert result["fees"] == pytest.approx(202 * leg["size"] * 0.00015)
    assert result["net"] == pytest.approx(leg["size"] * 2 - result["fees"])


def test_fill_candle_can_stop_and_adverse_gaps_get_taker_costs():
    both = history({0: bar(), 1: bar(high=103, low=97), 2: bar(), 3: bar()})
    leg = run(both)["trades"][0]["legs"][0]
    assert leg["entered_ms"] == leg["exited_ms"]
    assert leg["reason"] == "stop"
    assert leg["exit_mode"] == "taker"
    gap = history({0: bar(), 1: bar(low=99), 2: bar(97, 102, 96, 100), 3: bar()})
    for costs in (BASE, STRESS):
        result = run(gap, costs=costs)
        trade = result["trades"][0]["legs"][0]
        assert trade["reason"] == "stop gap"
        assert trade["exit"] == pytest.approx(
            97 * (1 - (costs.half_spread_bps + costs.exit_impact_bps) / 10_000)
        )
        assert trade["slippage"] > trade["size"]
        assert result["intrabar_drawdown"] >= result["realized_drawdown"] > 0


def test_complete_quarter_bars_allow_a_later_target_in_the_fill_hour():
    quarter = HOUR_MS // 4
    rows = [bar(low=99), bar(high=103), bar(low=97), bar()]
    market = history(
        {0: bar(), 1: bar(high=103, low=97), 2: bar(), 3: bar()},
        quarters={HOUR_MS + offset * quarter: row for offset, row in enumerate(rows)},
    )
    leg = run(market)["trades"][0]["legs"][0]
    assert leg["entered_ms"] == HOUR_MS + quarter - 1
    assert leg["exited_ms"] == HOUR_MS + 2 * quarter - 1
    assert leg["exit"] == 102


def quarter_hour(hour, rows):
    return {hour * HOUR_MS + offset * (HOUR_MS // 4): row for offset, row in enumerate(rows)}


def test_zero_volume_trade_through_does_not_fill_resting_entry():
    stale = bar(99, 99, 99, 99, volume=0)
    market = history(
        {0: bar(low=99, close=99), 1: bar(99, 100, 99, 100), 2: bar(), 3: bar()},
        quarters=quarter_hour(1, [stale, bar(), bar(), bar()]),
    )
    result = run(market, order(ttl_hours=1))
    assert result["count"] == result["maker_fills"] == result["fees"] == 0
    assert result["maker_orders"] == 1


@pytest.mark.parametrize("stale_price", [97, 103])
def test_zero_volume_prices_do_not_trigger_stop_or_profit_brackets(stale_price):
    stale = bar(*([stale_price] * 4), volume=0)
    market = history(
        {0: bar(), 1: bar(high=103, low=97), 2: bar(high=103), 3: bar()},
        quarters=quarter_hour(1, [bar(low=99), stale, bar(), bar()]),
    )
    (leg,) = run(market)["trades"][0]["legs"]
    assert leg["reason"] == "target"
    assert leg["exited_ms"] == 3 * HOUR_MS - 1


def test_time_exit_waits_for_trading_and_counts_terminal_price_proxies():
    stale = bar(97, 97, 97, 97, volume=0)
    bars = {0: bar(), 1: bar(97, 101, 97, 101), 2: bar(), 3: bar()}
    market = history(bars, quarters=quarter_hour(1, [stale, bar(), bar(), bar()]))
    plan = order(limit=101, mode="ioc", hold_hours=1)
    deferred = run(market, plan)
    (leg,) = deferred["trades"][0]["legs"]
    assert leg["reason"] == "time"
    assert leg["exited_ms"] == HOUR_MS + HOUR_MS // 4
    assert deferred["deferred_exit_slices"] == 1
    assert deferred["terminal_proxy_exits"] == 0

    no_trades = history(bars, quarters=quarter_hour(1, [stale] * 4))
    terminal = run(no_trades, plan, end=2)
    (leg,) = terminal["trades"][0]["legs"]
    assert leg["exit"] == pytest.approx(100 * (1 - 0.0007))
    assert leg["terminal_proxy"]
    assert terminal["terminal_proxy_exits"] == 1


def test_ioc_does_not_use_stale_opening_quote():
    market = history(
        {0: bar(), 1: bar(), 2: bar(), 3: bar()},
        quarters=quarter_hour(0, [bar(volume=0), bar(), bar(), bar()]),
    )
    result = run(market, order(limit=101, mode="ioc"))
    assert result["count"] == 0
    assert result["zero_volume_submissions"] == 1


def test_ioc_uses_executable_limit_and_charges_only_funding_during_exposure():
    market = history(
        {-1: bar(close=99), 0: bar(), 1: bar(), 2: bar(), 3: bar()},
        funding={
            0: Funding(1, 0.001),
            1: Funding(HOUR_MS + 1, 0.002),
        },
    )
    assert run(market, order(mode="ioc"))["count"] == 0
    ioc = order(mode="ioc", limit=101, hold_hours=1)
    result = run(market, ioc)
    leg = result["trades"][0]["legs"][0]
    assert leg["entry"] == pytest.approx(100.05)
    assert leg["exited_ms"] == HOUR_MS
    assert leg["funding"] == pytest.approx(-leg["size"] * 99 * 0.001)
    assert leg["fees"] == pytest.approx((leg["entry"] + leg["exit"]) * leg["size"] * 0.00045)
    assert result["hours_in_market_pct"] == 25


def test_four_leg_ioc_rejects_the_whole_plan_if_one_opening_quote_exceeds_its_cap():
    markets = {
        coin: history({0: bar(), 1: bar()}, coin) for coin in ("DOGE", "SPX", "PEPE", "PENGU")
    }
    plan = TradePlan(
        (
            order(limit=101, mode="ioc", weight=0.6),
            order("SPX", 1, 101, mode="ioc", weight=0.4),
            order("PEPE", -1, 99, mode="ioc", weight=0.6),
            order("PENGU", -1, 99, mode="ioc", weight=0.4),
        ),
        "four-leg basket",
    )
    limits = RiskLimits(initial_equity=1_000)
    matching = replay(markets, {0: [plan]}, 0, 2, limits=limits)
    (trade,) = matching["trades"]
    assert len(trade["legs"]) == 4
    assert all(leg["entered_ms"] == 0 and leg["entry_mode"] == "ioc" for leg in trade["legs"])
    net_notional = sum(leg["side"] * leg["entry"] * leg["size"] for leg in trade["legs"])
    assert abs(net_notional) < 2.01  # Below the sum of two quantity lots on either side.
    assert matching["fees"] > 0

    opening_quarters = quarter_hour(0, [bar(98, 98, 98, 98)] * 4)
    markets["PENGU"] = replace(markets["PENGU"], quarter_bars=opening_quarters)
    missing_cap = replay(markets, {0: [plan]}, 0, 2, limits=limits)
    assert missing_cap["count"] == missing_cap["fees"] == missing_cap["orphans"] == 0
    diagnostics = missing_cap["submission_diagnostics"]
    assert diagnostics["replay"]["rejected"]["entry_price_cap"] == 1
    assert diagnostics["empty_book"]["1000"]["rejected"]["entry_price_cap"] == 1


def test_trailing_stop_uses_completed_close():
    market = history(
        {0: bar(), 1: bar(high=103, low=99, close=103), 2: bar(103, 103, 101, 102), 3: bar()}
    )
    leg = run(market, order(target_atr=None, trailing_atr=1))["trades"][0]["legs"][0]
    assert leg["exited_ms"] == 3 * HOUR_MS - 1
    assert leg["exit"] == pytest.approx(102 * (1 - 0.0007))


def test_basket_independently_fills_legs_and_closes_as_one_bet():
    long = history({0: bar(), 1: bar(low=99), 2: bar(high=103), 3: bar()})
    short = history({0: bar(), 1: bar(high=101), 2: bar(close=100), 3: bar(99, 99, 99, 99)}, "SPX")
    basket = TradePlan((order(), order("SPX", -1, target_atr=None)), "pair")
    result = replay({leg.coin: leg for leg in (long, short)}, {0: [basket]}, 0, 4)
    assert result["count"] == result["wins"] == 1
    assert result["orphans"] == 0
    left, right = result["trades"][0]["legs"]
    assert left["size"] == right["size"]
    assert left["exited_ms"] == 3 * HOUR_MS - 1
    assert right["exited_ms"] == 3 * HOUR_MS
    assert right["reason"] == "basket unwind"
    assert right["exit_mode"] == "taker"
    assert result["net"] == pytest.approx(sum(result["by_coin"].values()))
    json.dumps(result, allow_nan=False)


def test_basket_expires_unfilled_sibling_and_unwinds_orphan_at_market():
    long = history({0: bar(), 1: bar(low=99), 2: bar(), 3: bar()})
    short = history({0: bar(), 1: bar(), 2: bar(), 3: bar(high=101)}, "SPX")
    basket = TradePlan((order(), order("SPX", -1)), "pair")
    result = replay({leg.coin: leg for leg in (long, short)}, {0: [basket]}, 0, 4)
    assert result["count"] == result["orphans"] == 1
    assert result["net"] < 0
    assert result["maker_fill_rate"] == 0.5
    (leg,) = result["trades"][0]["legs"]
    assert leg["exited_ms"] == 3 * HOUR_MS
    assert leg["reason"] == "orphan unwind"


def test_risk_caps_and_exchange_minimum_apply_to_lot_rounded_size():
    market = history({0: bar(), 1: bar(low=99), 2: bar(), 3: bar()})
    normal = run(market)
    size = normal["trades"][0]["legs"][0]["size"]
    assert 11 < size * 100 <= 0.5 * 47
    assert size == round(size, market.decimals)
    small = run(market, limits=RiskLimits(max_leg_fraction=0.2))
    assert small["count"] == small["maker_orders"] == 0
    other = replace(market, coin="SPX")
    plans = [TradePlan((order(coin),), coin) for coin in (market.coin, other.coin)]
    result = replay(
        {leg.coin: leg for leg in (market, other)},
        {0: plans},
        0,
        4,
        limits=RiskLimits(max_net_fraction=0.60),
    )
    assert result["count"] == 1


def test_empty_book_threshold_uses_the_smallest_whole_lot_above_the_exchange_minimum():
    market = history({0: bar(6.1, 6.1, 6.1, 6.1), 1: bar(6, 6, 5.5, 6)}, decimals=0)
    plan = order(limit=6, stop_atr=1, target_atr=None)

    below = run(market, plan, end=2, limits=RiskLimits(initial_equity=199.99), costs=COSTLESS)
    at_minimum = run(market, plan, end=2, limits=RiskLimits(initial_equity=200), costs=COSTLESS)
    assert below["count"] == 0
    assert below["submission_diagnostics"]["replay"]["rejected"]["MIN_NOTIONAL"] == 1

    diagnostics = at_minimum["submission_diagnostics"]
    assert below["submission_diagnostics"]["empty_book"] == diagnostics["empty_book"]
    assert diagnostics["empty_book"]["47"]["rejected"]["MIN_NOTIONAL"] == 1
    assert diagnostics["empty_book"]["1000"]["accepted"] == 1
    assert below["submission_diagnostics"]["executable_plans"] == diagnostics["executable_plans"]
    (candidate,) = diagnostics["executable_plans"]
    assert candidate == {
        "hour": 0,
        "candidate_number": 1,
        "label": "one",
        "coins": ["DOGE"],
        "minimum_equity": {"1pct": 200, "2pct": 100},
    }
    (trade,) = at_minimum["trades"]
    assert trade["legs"][0]["size"] == 2  # Two $6 lots are required to clear $10.
    for scope in ("empty_book", "at_submission_occupancy"):
        assert trade["sizing"][f"minimum_equity_{scope}"] == {"1pct": 200, "2pct": 100}
    assert diagnostics["minimum_equity"]["filled_at_submission_occupancy"]["1pct"] == {
        "count": 1,
        "p50": 200,
        "p90": 200,
        "p95": 200,
        "max": 200,
    }
    tighter_risk = run(
        market,
        plan,
        end=2,
        limits=RiskLimits(initial_equity=100, risk_fraction=0.02),
        costs=COSTLESS,
    )
    assert tighter_risk["trades"][0]["legs"][0]["size"] == 2


def test_weighted_basket_minimum_uses_the_gross_cap_after_each_leg_clears_the_floor():
    markets = {
        "DOGE": history({0: bar(10.1, 10.1, 10.1, 10.1), 1: bar(10, 10, 9.95, 10)}),
        "SPX": history({0: bar(9.9, 9.9, 9.9, 9.9), 1: bar(10, 10.05, 10, 10)}, "SPX"),
    }
    plan = TradePlan(
        (
            order(limit=10, atr=0.1, stop_atr=1, target_atr=None, weight=0.75),
            order("SPX", -1, 10, atr=0.1, stop_atr=1, target_atr=None, weight=0.25),
        ),
        "weighted",
    )
    limits = RiskLimits(initial_equity=200, max_gross_fraction=0.20)
    result = replay(markets, {0: [plan]}, 0, 2, limits=limits, costs=COSTLESS)
    (trade,) = result["trades"]
    assert [leg["size"] for leg in trade["legs"]] == [3, 1]
    assert trade["sizing"]["minimum_equity_empty_book"] == {"1pct": 200, "2pct": 200}


@pytest.mark.parametrize(("slots", "rejection"), [(4, "budget"), (2, "slot_limit")])
def test_submission_diagnostics_distinguish_occupied_budgets_and_slots(slots, rejection):
    bars = {0: bar(50.1, 50.1, 50.1, 50.1), 1: bar(50, 50, 49.5, 50)}
    markets = {coin: history(bars, coin, decimals=1) for coin in ("DOGE", "SPX", "PEPE")}
    plans = [
        TradePlan((order(coin, limit=50, stop_atr=1, target_atr=None),), coin) for coin in markets
    ]
    ideas = {0: [*plans, plans[0]], 2: plans}
    limits = RiskLimits(
        initial_equity=200, max_positions=slots, max_leg_fraction=0.20, max_net_fraction=0.30
    )
    result = replay(markets, ideas, 0, 2, costs=COSTLESS, limits=limits)
    diagnostics = result["submission_diagnostics"]
    actual = diagnostics["replay"]
    assert actual["candidates"] == actual["accepted"] + sum(actual["rejected"].values()) == 4
    assert actual["accepted"] == result["count"] == 2
    assert actual["rejected"]["overlap"] == actual["rejected"][rejection] == 1
    assert diagnostics["empty_book"]["1000"]["accepted"] == 4
    assert diagnostics["empty_book"]["47"]["rejected"]["MIN_NOTIONAL"] == 4

    first, second = result["trades"]
    assert first["sizing"]["reserved_positions"] == 0
    assert second["sizing"]["reserved_positions"] == 1
    assert second["sizing"]["reserved_long_notional"] == 40
    assert second["sizing"]["reserved_short_notional"] == 0
    assert second["sizing"]["minimum_equity_empty_book"] == {"1pct": 50, "2pct": 50}
    assert second["sizing"]["minimum_equity_at_submission_occupancy"] == pytest.approx(
        {"1pct": (40 + 10) / 0.30, "2pct": (40 + 10) / 0.30}
    )


def test_data_gaps_cancel_unfilled_orders_and_raise_for_unpriced_exposure():
    missing_opening = run(history({1: bar()}), end=2)
    assert missing_opening["submission_diagnostics"]["replay"]["rejected"]["data_gap"] == 1
    result = run(history({0: bar(), 2: bar(low=99), 3: bar()}))
    assert result["count"] == result["fees"] == 0
    assert result["pending_data_gaps"] == 1
    with pytest.raises(ValueError, match="Missing execution candle for open positions"):
        run(history({0: bar(), 1: bar(low=99), 3: bar()}))
