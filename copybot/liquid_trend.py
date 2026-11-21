"""Offline SMA-filtered daily Donchian plans with opposite-channel exits."""

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import replace

from .directional_features import _aggregate, _bars
from .research_data import ARCHIVE_END_HOUR
from .research_models import DAY_HOURS, MarketHistory, TradePlan, excluded_symbol

BASELINE_VARIANT = "donchian-20-24h-ioc"
ENHANCED_VARIANT = "donchian-20-24h-ioc-top20-sma50-channel10"
SMA_DAYS = 50
EXIT_DAYS = 10


def _completed_days(history, first, end, offset):
    hourly = _bars(history.bars, first, end)
    daily = _aggregate(hourly.set_axis(hourly.index - offset), DAY_HOURS)
    daily.index += offset
    # The last archived hourly candle must have traded for a fresh daily close.
    fresh = hourly.volume.reindex(daily.index - 1).gt(0).set_axis(daily.index)
    valid = daily.volume.gt(0)
    close = daily.close.where(valid)
    lower = daily.low.where(valid).rolling(EXIT_DAYS).min().shift()
    upper = daily.high.where(valid).rolling(EXIT_DAYS).max().shift()
    return daily.assign(
        sma=close.rolling(SMA_DAYS).mean().shift(),
        fresh=fresh,
        long_exit=fresh & close.lt(lower),
        short_exit=fresh & close.gt(upper),
    )


def build_liquid_trend(
    histories: Mapping[str, MarketHistory],
    top20_daily_ioc_plans: Mapping[int, list[TradePlan]],
    start: int,
    end: int,
    *,
    daily_offset: int = 0,
):
    """Filter already ranked daily entries and return their causal exit callback.

    Previous means 50 or 10 complete daily bars before the evaluated daily bar.
    The callback uses the newly completed daily close at its decision boundary.
    It keeps the source plan's protective stop and maximum holding period.
    """
    if type(start) is not int or type(end) is not int or not 0 < start < end <= ARCHIVE_END_HOUR:
        raise ValueError("Expected increasing UTC epoch hours inside the development cutoff")
    if type(daily_offset) is not int or not 0 <= daily_offset < DAY_HOURS:
        raise ValueError("Daily candle offset must be an integer UTC hour")
    first = max(0, start - (SMA_DAYS + 2) * DAY_HOURS)
    days = {}
    candidates = defaultdict(list)
    rejections = Counter()
    for hour, plans in sorted(top20_daily_ioc_plans.items()):
        if not start <= hour < end:
            continue
        if (hour - daily_offset) % DAY_HOURS:
            raise ValueError("Expected a completed daily boundary for a Donchian order")
        for plan in plans:
            if len(plan.legs) != 1 or plan.legs[0].mode != "ioc":
                raise ValueError("Expected a single-leg daily Donchian IOC order")
            leg = plan.legs[0]
            if excluded_symbol(leg.coin) or ":" in leg.coin:
                raise ValueError("Expected a native non-BTC market")
            if leg.coin not in days:
                days[leg.coin] = _completed_days(histories[leg.coin], first, end, daily_offset)
            candle = days[leg.coin].loc[hour]
            if not candle.fresh or not (candle.close > 0 and candle.sma > 0):
                rejections["insufficient_daily_history"] += 1
                continue
            if (candle.close - candle.sma) * leg.side <= 0:
                rejections["opposite_50_day_trend"] += 1
                continue
            # Keep the source stop, sizing and maximum hold. The daily channel
            # replaces only the baseline's ATR trailing exit in this variant.
            candidates[hour].append(TradePlan((replace(leg, trailing_atr=None),), ENHANCED_VARIANT))

    def exit_signal(plan: TradePlan, submitted: int, hour: int) -> str | None:
        if plan.label != ENHANCED_VARIANT or hour <= submitted or hour >= end:
            return None
        if (hour - daily_offset) % DAY_HOURS:
            return None
        leg = plan.legs[0]
        frame = days[leg.coin]
        signal = "long_exit" if leg.side > 0 else "short_exit"
        return "10-day channel exit" if frame.at[hour, signal] else None

    kept = sum(len(plans) for plans in candidates.values())
    diagnostics = {
        "accepted_sma_filter": kept,
        "rejected_insufficient_daily_history": rejections["insufficient_daily_history"],
        "rejected_opposite_50_day_trend": rejections["opposite_50_day_trend"],
        "input_plans": kept + sum(rejections.values()),
        "opening_market_count": len(days),
    }
    return dict(candidates), exit_signal, diagnostics
