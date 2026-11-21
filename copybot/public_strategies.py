"""Offline adaptations of public directional entry rules for Hyperliquid."""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite

import numpy as np
import pandas as pd

from .directional_features import _aggregate, _bars
from .research_data import ARCHIVE_END_HOUR
from .research_models import MarketHistory, OrderPlan, TradePlan, excluded_symbol

HUMMINGBOT_SOURCE = (
    "https://github.com/hummingbot/hummingbot/blob/02b87048d9c576d6ce2a017ecaa6a6f00cc2e5ea"
    "/controllers/directional_trading/macd_bb_v1.py"
)
FREQTRADE_SOURCE = (
    "https://github.com/freqtrade/freqtrade-strategies/blob/0727df98acc0b49497b1988da3dc37892be5503c"
    "/user_data/strategies/futures/TrendFollowingStrategy.py"
)
DONCHIAN_SOURCE = (
    "https://github.com/QuantConnect/Lean/blob/ed551458f5f91bedd1409fe5dd8df373f1f1d8ca"
    "/Indicators/DonchianChannel.cs"
)

WARMUP_HOURS = 22 * 24
LIQUIDITY_LOOKBACK_HOURS = 30 * 24
MINIMUM_ACTIVE_HOURS = 9
ATR_BARS = 14
IOC_CAP_BPS = 40
MAKER_OFFSET_ATR = 0.20
MAKER_TTL_HOURS = 4


@dataclass(frozen=True)
class PublicVariant:
    name: str
    family: str
    resolution_hours: int
    mode: str
    hold_hours: int
    stop_atr: float
    target_atr: float | None
    trailing_atr: float | None
    source: str


def variants() -> tuple[PublicVariant, ...]:
    """Fixed entry rules and the common Hyperliquid exit assumptions.

    MACD-BB uses a bounded mean-reversion exit. EMA/OBV and channel
    breakouts use trailing exits so that a trend can continue past 2R.
    The archives support 1h and synthetic 4h research frequencies.
    """
    definitions = (
        ("hummingbot-macd-bb", 1, 24, 1.5, 3.0, None, HUMMINGBOT_SOURCE),
        ("freqtrade-ema-obv", 1, 36, 1.75, None, 2.5, FREQTRADE_SOURCE),
        ("donchian-20", 4, 96, 2.0, None, 3.0, DONCHIAN_SOURCE),
        ("donchian-20", 24, 240, 2.0, None, 3.0, DONCHIAN_SOURCE),
    )
    return tuple(
        PublicVariant(
            f"{family}-{resolution}h-{mode}",
            family,
            resolution,
            mode,
            hold,
            stop,
            target,
            trail,
            source,
        )
        for family, resolution, hold, stop, target, trail, source in definitions
        for mode in ("ioc", "maker")
    )


def _ema(values: pd.Series, period: int) -> pd.Series:
    # Missing candles reset the indicator rather than joining separate histories.
    return values.groupby(values.isna().cumsum()).transform(
        lambda segment: segment.ewm(span=period, adjust=False, min_periods=period).mean()
    )


def macd_bb(bars: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Adapt the Hummingbot default 100/2 Bollinger and 21/42/9 MACD thresholds."""
    close = bars.close
    macd = _ema(close, 21) - _ema(close, 42)
    histogram = macd - _ema(macd, 9)
    bands = close.rolling(100)
    center = bands.mean()
    radius = 2 * bands.std(ddof=0)
    long = close.lt(center - radius) & histogram.gt(0) & macd.lt(0)
    short = close.gt(center + radius) & histogram.lt(0) & macd.gt(0)
    band_excess = ((close - center).abs() - radius).clip(lower=0)
    return pd.Series(np.select([long, short], [1, -1]), index=bars.index), band_excess


def ema_obv(bars: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Adapt the Freqtrade community EMA20 cross with confirmatory OBV change."""
    close = bars.close
    gap = close - _ema(close, 20)
    previous_gap = gap.shift()
    # A single OBV increment equals signed candle volume. No cumulative level is needed.
    obv_change = np.sign(close.diff()) * bars.volume
    long = gap.gt(0) & previous_gap.le(0) & obv_change.gt(0)
    short = gap.lt(0) & previous_gap.ge(0) & obv_change.lt(0)
    return pd.Series(np.select([long, short], [1, -1]), index=bars.index), gap.abs()


def donchian(bars: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Test a close beyond the extremes of the preceding 20 completed bars."""
    upper = bars.high.shift().rolling(20).max()
    lower = bars.low.shift().rolling(20).min()
    long = bars.close.gt(upper)
    short = bars.close.lt(lower)
    excess = (bars.close - upper).where(long, (lower - bars.close).where(short, 0))
    return pd.Series(np.select([long, short], [1, -1]), index=bars.index), excess


def _atr(bars: pd.DataFrame) -> pd.Series:
    previous = bars.close.shift()
    true_range = pd.concat(
        [bars.high - bars.low, (bars.high - previous).abs(), (bars.low - previous).abs()], axis=1
    ).max(axis=1, skipna=False)
    return true_range.rolling(ATR_BARS).mean()


def _liquid(closed_hourly: pd.DataFrame, minimum_turnover: float) -> pd.Series:
    notional = closed_hourly.close * closed_hourly.volume
    active = notional.gt(0).rolling(24).sum()
    median = notional.where(notional.gt(0)).rolling(24, min_periods=MINIMUM_ACTIVE_HOURS).median()
    return (
        notional.gt(0)
        & notional.rolling(24).sum().ge(minimum_turnover)
        & active.ge(MINIMUM_ACTIVE_HOURS)
        & median.ge(minimum_turnover / 72)
    )


def _order(candidate, coin: str, config: PublicVariant) -> OrderPlan:
    if config.mode == "maker":
        limit = candidate.close - candidate.side * candidate.atr * MAKER_OFFSET_ATR
        ttl = MAKER_TTL_HOURS
    else:
        limit = candidate.close * (1 + candidate.side * IOC_CAP_BPS / 10_000)
        ttl = 0
    return OrderPlan(
        coin=coin,
        side=int(candidate.side),
        limit=float(limit),
        atr=float(candidate.atr),
        stop_atr=config.stop_atr,
        target_atr=config.target_atr,
        hold_hours=config.hold_hours,
        ttl_hours=ttl,
        mode=config.mode,
        trailing_atr=config.trailing_atr,
        signal_strength=float(candidate.strength),
    )


def build_plans(
    histories: Mapping[str, MarketHistory],
    start: int,
    end: int,
    minimum_turnover: float = 1_000_000,
    *,
    daily_offset: int = 0,
    liquidity_top_n: int | None = None,
) -> dict[str, dict[int, list[TradePlan]]]:
    """Rank every eligible public entry by signal distance in units of ATR.

    Key clocks are native UTC decision hours. All features are formed on
    candles completed before the decision. The archived replay submits
    limits at decision hours and operates on the remaining bars.
    """
    if type(start) is not int or type(end) is not int or not 0 < start < end <= ARCHIVE_END_HOUR:
        raise ValueError("Expected increasing UTC epoch hours inside the development cutoff")
    if not isfinite(minimum_turnover) or minimum_turnover <= 0:
        raise ValueError("Expected finite positive minimum turnover")
    if type(daily_offset) is not int or not 0 <= daily_offset < 24:
        raise ValueError("Daily candle offset must be an integer UTC hour")
    if liquidity_top_n is not None and (type(liquidity_top_n) is not int or liquidity_top_n <= 0):
        raise ValueError("The liquid universe count must be a positive integer")
    rules = {"hummingbot-macd-bb": macd_bb, "freqtrade-ema-obv": ema_obv, "donchian-20": donchian}
    configs = variants()
    ranked = {config.name: defaultdict(list) for config in configs}
    first = max(0, start - (LIQUIDITY_LOOKBACK_HOURS if liquidity_top_n else WARMUP_HOURS))
    turnover = {}
    for history in histories.values():
        coin = history.coin
        if excluded_symbol(coin) or ":" in coin or not history.bars:
            continue
        hourly = _bars(history.bars, first, end)
        four_hour = _aggregate(hourly, 4)
        daily = _aggregate(hourly.set_axis(hourly.index - daily_offset), 24)
        daily.index += daily_offset
        hourly.index += 1
        eligible = _liquid(hourly, minimum_turnover)
        if liquidity_top_n is not None:
            turnover[coin] = (
                (hourly.close * hourly.volume)
                .rolling(LIQUIDITY_LOOKBACK_HOURS)
                .sum()
                .where(eligible)
            )
        resolutions = {1: hourly, 4: four_hour, 24: daily}
        candles = {period: bars.assign(atr=_atr(bars)) for period, bars in resolutions.items()}
        for family, resolution in sorted({(cfg.family, cfg.resolution_hours) for cfg in configs}):
            strategies = [
                item
                for item in configs
                if (item.family, item.resolution_hours) == (family, resolution)
            ]
            bars = candles[resolution]
            side, strength = rules[family](bars)
            valid = (
                side.ne(0)
                & bars.atr.gt(0)
                & eligible.reindex(bars.index, fill_value=False)
                & (bars.index >= start)
                & (bars.index < end)
            )
            signals = bars.loc[valid, ["close", "atr"]].assign(
                side=side.loc[valid], strength=(strength / bars.atr).loc[valid]
            )
            for signal in signals.itertuples():
                for config in strategies:
                    leg = _order(signal, coin, config)
                    if leg.limit > 0:
                        ranked[config.name][int(signal.Index)].append((signal.strength, leg))

    if turnover:
        ranks = (
            pd.DataFrame(turnover)
            .sort_index(axis=1)
            .rank(
                axis=1,
                ascending=False,
                method="first",
            )
        )
        for decisions in ranked.values():
            for hour, entries in decisions.items():
                decisions[hour] = [
                    item for item in entries if ranks.at[hour, item[1].coin] <= liquidity_top_n
                ]
    return {
        name: {
            hour: [
                TradePlan((leg,), name)
                for _, leg in sorted(entries, key=lambda item: (-item[0], item[1].coin))
            ]
            for hour, entries in sorted(decisions.items())
        }
        for name, decisions in ranked.items()
    }
