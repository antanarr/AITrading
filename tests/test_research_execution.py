import json
from dataclasses import replace

import pytest

from copybot.research_execution import BASE, STRESS, RiskLimits, replay
from copybot.research_models import HOUR_MS, Bar, Funding, MarketHistory, OrderPlan, TradePlan


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


@pytest.mark.parametrize("side, limit", [(1, 100.03), (-1, 99.97)])
def test_post_only_rejects_order_at_opening_executable_quote(side, limit):
    market = history({0: bar(), 1: bar(high=101, low=99), 2: bar(), 3: bar()})
    result = run(market, order(side=side, limit=limit))
    assert result["count"] == 0
    assert result["maker_orders"] == 0


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


def test_data_gaps_cancel_unfilled_orders_and_raise_for_unpriced_exposure():
    result = run(history({0: bar(), 2: bar(low=99), 3: bar()}))
    assert result["count"] == result["fees"] == 0
    assert result["pending_data_gaps"] == 1
    with pytest.raises(ValueError, match="Missing execution candle for open positions"):
        run(history({0: bar(), 1: bar(low=99), 3: bar()}))
