import json
from dataclasses import replace
from pathlib import Path

import pytest

from copybot import llm_trade_inputs
from copybot.liquid_trend import ENHANCED_VARIANT
from copybot.research_data import ARCHIVE_END_HOUR
from copybot.research_models import (
    DAY_HOURS,
    HOUR_MS,
    Bar,
    Funding,
    MarketHistory,
    OrderPlan,
    TradePlan,
)

HOUR = 50_000 // DAY_HOURS * DAY_HOURS


def market(coin, multiplier=1):
    bars = {}
    for hour in range(HOUR - 52 * DAY_HOURS, HOUR + 4):
        price = (110 if hour == HOUR - 1 else 100) * multiplier
        bars[hour] = Bar(price, price + multiplier, price - multiplier, price, 10_000)
    funding = {
        hour: Funding(hour * HOUR_MS + 12, 0.0001, 0.3)
        for hour in range(HOUR - DAY_HOURS, HOUR + 4)
    }
    return MarketHistory(coin, 2, False, HOUR - 60 * DAY_HOURS, bars, funding)


def order(coin, side=1, multiplier=1):
    leg = OrderPlan(
        coin, side, 110.4 * multiplier, 4 * multiplier, 2, None, 240, 0, "ioc", signal_strength=2.25
    )
    return TradePlan((leg,), ENHANCED_VARIANT)


def test_packets_omit_identity_and_keep_the_exact_private_mapping():
    coins = ["ETH", "SOL", "HYPE", "XRP", "BNB"]
    plans = [order("ETH"), order("SOL", -1)]
    histories = {coin: market(coin) for coin in coins}
    packets, private = llm_trade_inputs.build_packets(histories, {HOUR: plans})
    packet = packets[HOUR]
    data = json.dumps(packet, allow_nan=False)
    assert all(coin not in data for coin in coins)
    assert str(HOUR) not in data and "110.4" not in data
    assert set(packet) == {"schema", "broad_market", "candidates"}
    assert packet["broad_market"]["positive_24h_share_pct"] == 100
    assert set(private[HOUR].values()) == set(plans)
    for candidate in packet["candidates"]:
        leg = private[HOUR][candidate["id"]].legs[0]
        assert candidate["age_estimate_bucket"] == "under_90_days"
        assert candidate["chart"]["return_1h_pct"] == pytest.approx(10)
        assert candidate["chart"]["trade_direction_sma50_gap_atr"] == pytest.approx(2.5 * leg.side)
        assert candidate["funding"]["prior_24h_mean_bps_paid_per_hour"] == leg.side


def test_packet_contains_no_absolute_price_and_ignores_same_hour_future_labels():
    original = market("ETH")
    scaled = market("ETH", multiplier=10)
    downstream = replace(
        scaled,
        bars={**scaled.bars, HOUR: Bar(1, 70000, 1, 70000, 9_000_000)},
        funding={**scaled.funding, HOUR: Funding(HOUR * HOUR_MS, 0.25, -0.4)},
        listing_hour=original.listing_hour,
    )
    before, _ = llm_trade_inputs.build_packets({"ETH": original}, {HOUR: [order("ETH")]})
    after, _ = llm_trade_inputs.build_packets(
        {"ETH": downstream}, {HOUR: [order("ETH", multiplier=10)]}
    )
    assert before == after


def test_missing_older_bar_or_late_funding_is_unavailable():
    history = market("ETH")
    bars = dict(history.bars)
    del bars[HOUR - 25]
    late = {**history.funding, HOUR - 2: Funding(HOUR * HOUR_MS, 0.1)}
    changed = replace(history, bars=bars, funding=late)
    packets, _ = llm_trade_inputs.build_packets({"ETH": changed}, {HOUR: [order("ETH")]})
    row = packets[HOUR]["candidates"][0]
    assert row["chart"]["trade_direction_sma50_gap_atr"] is None
    assert row["chart"]["return_7d_pct"] is None
    assert row["funding"]["prior_1h_mean_bps_paid_per_hour"] == 1
    assert row["funding"]["prior_6h_mean_bps_paid_per_hour"] is None


def test_rejects_reserve_before_loading_and_blocks_btc_inputs(monkeypatch):
    monkeypatch.setattr(
        llm_trade_inputs, "load_archive", lambda *a, **kw: pytest.fail("archive accessed")
    )
    with pytest.raises(ValueError, match="development cutoff"):
        llm_trade_inputs.build_inputs(Path("unread"), ARCHIVE_END_HOUR - 1, ARCHIVE_END_HOUR + 1)
    with pytest.raises(ValueError, match="non-BTC"):
        llm_trade_inputs.build_packets({}, {HOUR: [order("wBTC")]})
