from dataclasses import replace

import pandas as pd
import pytest

from copybot.directional_features import QUARTER_MS, build_frame
from copybot.research_models import HOUR_MS, Bar, Funding, MarketHistory

HOUR = 48_000


def history(coin="MEME", *, is_meme=True, listing=HOUR - 500, step=0.02, quarters=False):
    bars, funding, quarter_bars = {}, {}, {}
    first = max(HOUR - 500, listing or 0)
    for hour in range(first, HOUR + 26):
        price = 100 + step * (hour - HOUR)
        bars[hour] = Bar(price, price + 1, price - 1, price + 0.5, 1_000)
        funding[hour] = Funding(hour * HOUR_MS + 123, 0.0001, -0.0002)
        if quarters and HOUR - 48 <= hour:
            for subbar in range(4):
                stamp = hour * HOUR_MS + subbar * QUARTER_MS
                quarter_bars[stamp] = bars[hour]
    return MarketHistory(coin, 2, is_meme, listing, bars, funding, quarter_bars)


def frame(*histories, start=HOUR, end=HOUR + 20):
    return build_frame({item.coin: item for item in histories}, start, end)


def test_features_use_only_completed_aligned_bars_and_predecision_funding():
    market = history(quarters=True)
    first = frame(market).iloc[0]
    bars = {**market.bars, HOUR: Bar(101, 302, 100, 301, 200_000)}
    quarters = {**market.quarter_bars, HOUR * HOUR_MS: bars[HOUR]}
    funding = {**market.funding, HOUR: Funding(HOUR * HOUR_MS, 0.05, 0.1)}
    altered = replace(market, bars=bars, funding=funding, quarter_bars=quarters)
    second = frame(altered).iloc[0]
    feature_cols = first.index.str.startswith(("tech_", "market_", "carry_"))
    pd.testing.assert_series_equal(first[feature_cols], second[feature_cols])
    assert first["carry_funding_bps"] == 1
    assert first["tech_15m_return_4"] == pytest.approx(first["tech_1h_return_1"])
    lagged = frame(altered).iloc[1]
    assert lagged["carry_funding_bps"] == 500
    assert lagged["tech_1h_return_1"] != first["tech_1h_return_1"]
    assert lagged["tech_4h_return_1"] == first["tech_4h_return_1"]
    assert lagged["tech_1d_return_1"] == first["tech_1d_return_1"]


def test_no_quarter_data_or_complete_higher_bar_remains_missing():
    market = history()
    bars = dict(market.bars)
    del bars[HOUR - 30]
    decision = frame(replace(market, bars=bars)).iloc[0]
    assert pd.isna(decision["tech_15m_return_1"])
    assert pd.isna(decision["tech_1d_return_1"])
    assert pd.notna(decision["tech_1h_return_6"])


def test_returns_use_next_hour_entry_actual_funding_sign_and_exclusive_cutoff():
    market = history()
    funding = {**market.funding}
    funding[HOUR + 1] = Funding((HOUR + 1) * HOUR_MS, 0.05)
    funding[HOUR + 5] = Funding((HOUR + 5) * HOUR_MS, -0.0002)
    result = frame(replace(market, funding=funding), end=HOUR + 6)
    first = result.iloc[0]
    assert first["entry_hour"] == HOUR + 1
    assert first["exit_hour_4"] == HOUR + 5
    assert first["price_return_4"] == pytest.approx(
        market.bars[HOUR + 5].open / market.bars[HOUR + 1].open - 1
    )
    expected = (
        -0.0001 * sum(market.bars[hour - 1].close for hour in range(HOUR + 2, HOUR + 5))
        + 0.0002 * market.bars[HOUR + 4].close
    ) / first["entry_price"]
    assert first["funding_return_4"] == pytest.approx(expected)
    assert result["price_return_4"].notna().sum() == 1
    assert result["price_return_12"].isna().all()


def test_missing_execution_data_does_not_change_point_in_time_universe():
    market = history()
    bars = {**market.bars, HOUR + 1: replace(market.bars[HOUR + 1], volume=0)}
    decision = frame(replace(market, bars=bars)).iloc[0]
    assert decision["hour"] == HOUR
    assert pd.isna(decision["price_return_4"])
    assert pd.isna(decision["funding_return_4"])
    funding = {hour: value for hour, value in market.funding.items() if hour != HOUR + 5}
    decision = frame(replace(market, funding=funding)).iloc[0]
    assert pd.notna(decision["price_return_4"])
    assert pd.isna(decision["funding_return_4"])


def test_only_liquid_meme_and_recent_markets_enter_peer_features():
    active_memes = [history(f"MEME{index}", step=0.01 * (index + 1)) for index in range(5)]
    quiet = history("QUIET", step=-0.03)
    quiet = replace(quiet, bars={key: replace(row, volume=1) for key, row in quiet.bars.items()})
    recent = history("NEW", is_meme=False, listing=HOUR - 12)
    other = history("OTHER", is_meme=False)
    result = frame(*active_memes, recent, other, quiet, history("wBTC"), history("dex:ALT"))
    first = result[result["hour"].eq(HOUR)]
    assert set(first["coin"]) == {market.coin for market in active_memes} | {"NEW"}
    assert first["market_meme_count"].eq(5).all()
    assert first["market_meme_breadth_6h"].eq(1).all()
    assert first[first["coin"].eq("NEW")]["group"].item() == "recent"
    assert first[first["coin"].eq("MEME4")]["market_return_rank_72h"].item() == 1.0


def test_broad_universe_uses_liquid_majors_and_excludes_btc_aliases():
    markets = {
        name: history(name, is_meme=False, step=step)
        for name, step in {
            "ETH": 0.01,
            "SOL": 0.02,
            "SUI": -0.01,
            "HYPE": -0.02,
            "AAVE": -0.03,
            "wBTC": 0.5,
        }.items()
    }
    first = build_frame(markets, HOUR, HOUR + 20, broad=True).query("hour == @HOUR")
    assert set(first.coin) == {"ETH", "SOL", "SUI", "HYPE", "AAVE"}
    assert first.market_universe_count.eq(5).all()
    assert first.market_universe_breadth_24h.eq(0.4).all()
