from dataclasses import replace

import pandas as pd
import pytest

from copybot.public_strategies import build_plans, donchian, ema_obv, macd_bb, variants
from copybot.research_data import ARCHIVE_END_HOUR
from copybot.research_models import Bar, MarketHistory


def prices(closes, volumes=1):
    close = pd.Series(closes, dtype=float)
    return pd.DataFrame({"close": close, "high": close + 1, "low": close - 1, "volume": volumes})


def test_macd_bb_needs_band_break_and_reversal_inside_prior_macd_direction():
    # The sharp fall first puts both MACD and its histogram below zero. As
    # the market stabilizes, the histogram turns before price rejoins the band.
    falling = prices([100] * 121 + [90, 80] + [70] * 15 + [68] * 6)
    rising = prices(200 - falling.close)
    longs, distance = macd_bb(falling)
    shorts, _ = macd_bb(rising)
    assert longs.iloc[:120].eq(0).all()
    assert longs.iloc[121] == 0
    assert longs.eq(1).any() and longs.ge(0).all()
    pd.testing.assert_series_equal(longs, -shorts)
    assert distance[longs.eq(1)].gt(0).all()


def test_ema_obv_confirms_both_crosses_and_zero_volume_does_not_confirm():
    series = prices([100] * 25 + [103, 98, 103], [1] * 27 + [0])
    entries, distance = ema_obv(series)
    assert entries.iloc[:-3].eq(0).all()
    assert list(entries.iloc[-3:]) == [1, -1, 0]
    assert distance.iloc[-2] > 0


def test_donchian_uses_strict_close_break_and_prior_channel():
    channel = prices([100] * 20 + [101, 103, 97])
    entries, excess = donchian(channel)
    assert entries.iloc[:20].eq(0).all()
    assert list(entries.iloc[-3:]) == [0, 1, -1]
    assert list(excess.iloc[-3:]) == [0, 1, 2]


def market(symbol, hour, closing, *, volume=5_000):
    candles = {stamp: Bar(100, 101, 99, 100, volume) for stamp in range(hour - 168, hour + 5)}
    candles[hour - 1] = Bar(100, closing + 1, 99, closing, volume)
    return MarketHistory(symbol, 2, False, None, candles, {})


def test_broad_plans_rank_and_include_all_liquid_candidates_without_future_bars():
    hour = 50_000
    stronger = market("ETH", hour, 110)
    weaker = market("SOL", hour, 105)
    noisy_future = replace(
        stronger, bars={**stronger.bars, hour: Bar(110, 2000, 50, 1500, 500_000)}
    )
    excluded = [market(coin, hour, 150) for coin in ("BTC", "wBTC", "dex:ALT")]
    quiet = market("QUIET", hour, 120, volume=1)
    assets = {row.coin: row for row in (noisy_future, weaker, quiet, *excluded)}
    planned = build_plans(assets, hour, hour + 1)
    maker_name, ioc_name = "freqtrade-ema-obv-1h-maker", "freqtrade-ema-obv-1h-ioc"
    for name in (maker_name, ioc_name):
        legs = [plan.legs[0] for plan in planned[name][hour]]
        assert [leg.coin for leg in legs] == ["ETH", "SOL"]
        assert all(leg.side == 1 for leg in legs)
    maker = planned[maker_name][hour][0].legs[0]
    ioc = planned[ioc_name][hour][0].legs[0]
    assert maker.limit == pytest.approx(110 - 0.20 * maker.atr)
    assert maker.ttl_hours == 4
    assert ioc.limit == pytest.approx(110 * 1.004)
    assert ioc.ttl_hours == 0
    assets["ETH"] = stronger
    assert planned == build_plans(assets, hour, hour + 1)


def test_missing_hour_prevents_channel_signal_and_cutoff_is_enforced():
    hour = 50_000
    hist = market("SOL", hour, 110)
    name = "donchian-20-4h-ioc"
    assert build_plans({"SOL": hist}, hour, hour + 1)[name][hour]
    candles = dict(hist.bars)
    del candles[hour - 3]
    assert not build_plans({"SOL": replace(hist, bars=candles)}, hour, hour + 1)[name]
    with pytest.raises(ValueError, match="development cutoff"):
        build_plans({}, ARCHIVE_END_HOUR - 1, ARCHIVE_END_HOUR + 1)
    assert len({variant.name for variant in variants()}) == 8
    assert any(variant.name == "donchian-20-24h-ioc" for variant in variants())


def test_daily_boundary_uses_only_closed_shifted_candles():
    boundary = 50_000
    result = market("SOL", boundary, 110)
    early = {hour: Bar(100, 101, 99, 100, 50_000) for hour in range(boundary - 528, boundary - 168)}
    result = replace(result, bars=early | result.bars)
    name = "donchian-20-24h-ioc"
    valid = build_plans({"SOL": result}, boundary, boundary + 1, daily_offset=8)
    assert valid[name][boundary]
    midnight = build_plans({"SOL": result}, boundary, boundary + 1, daily_offset=0)
    assert not midnight[name]
