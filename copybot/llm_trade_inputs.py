"""Construct anonymous decision-time packets for a frozen Donchian replay."""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from math import isfinite
from pathlib import Path
from statistics import fmean, median

from .broad_study import usable_histories
from .liquid_trend import BASELINE_VARIANT, ENHANCED_VARIANT, build_liquid_trend
from .public_strategies import MINIMUM_ACTIVE_HOURS, build_plans
from .research_data import ARCHIVE_END_HOUR, load_archive
from .research_models import DAY_HOURS, HOUR_MS, MarketHistory, TradePlan, excluded_symbol

DEVELOPMENT_WINDOWS = (
    ("discovery", date(2026, 5, 25), date(2026, 9, 26)),
    ("validation", date(2026, 9, 26), date(2026, 11, 21)),
)
MINIMUM_TURNOVER = 1_000_000
TOP_MARKETS = 20
BREAKOUT_SCHEMA = 1


@dataclass(frozen=True)
class TradeInputs:
    histories: dict[str, MarketHistory]
    plans: dict[int, list[TradePlan]]
    exit_signal: Callable[[TradePlan, int, int], str | None]
    packets: dict[int, dict]
    candidates: dict[int, dict[str, TradePlan]]
    coverage: dict
    trend_diagnostics: dict
    statistics: dict


def _round(value):
    return round(value, 4) if value is not None and isfinite(value) else None


def _bars(history, first, end):
    bars = [history.bars.get(stamp) for stamp in range(first, end)]
    return bars if all(bar is not None for bar in bars) else None


def _close_return(history, hour, span):
    bars = _bars(history, hour - span - 1, hour)
    if bars is None or bars[0].close <= 0 or bars[-1].close <= 0:
        return None
    return 100 * (bars[-1].close / bars[0].close - 1)


def _funding_window(history, hour, span):
    rows = [history.funding.get(stamp) for stamp in range(hour - span, hour)]
    if any(row is None or row.time_ms >= hour * HOUR_MS for row in rows):
        return None
    return rows


def _paid_bps(history, hour, side, span):
    rows = _funding_window(history, hour, span)
    return _round(10_000 * side * fmean(row.rate for row in rows)) if rows else None


def _age_bucket(history, hour):
    if history.listing_hour is None or history.listing_hour >= hour:
        return "unknown"
    days = (hour - history.listing_hour) // DAY_HOURS
    if days < 90:
        return "under_90_days"
    return "90_to_364_days" if days < 365 else "at_least_365_days"


def _prior_sma(history, hour):
    prior = _bars(history, hour - 51 * DAY_HOURS, hour - DAY_HOURS)
    if prior is None:
        return None
    closes = []
    for first in range(0, len(prior), DAY_HOURS):
        day = prior[first : first + DAY_HOURS]
        if not any(bar.volume > 0 for bar in day):
            return None
        closes.append(day[-1].close)
    return fmean(closes)


def _notional(bars):
    return sum(bar.close * bar.volume for bar in bars)


def _chart(history, hour, leg):
    current = _bars(history, hour - DAY_HOURS, hour)
    previous = _bars(history, hour - 8 * DAY_HOURS, hour - DAY_HOURS)
    average_prior = _notional(previous) / 7 if previous is not None else 0
    notional_ratio = _notional(current) / average_prior if current and average_prior > 0 else None
    sma = _prior_sma(history, hour)
    close = current[-1].close if current else None
    prior_range = _bars(history, hour - 2 * DAY_HOURS, hour - DAY_HOURS)
    if current and prior_range:
        high, low = max(bar.high for bar in current), min(bar.low for bar in current)
        true_range = max(
            high - low, abs(high - prior_range[-1].close), abs(low - prior_range[-1].close)
        )
    else:
        true_range = None
    return {
        "breakout_excess_atr": _round(leg.signal_strength),
        "trade_direction_sma50_gap_atr": _round(leg.side * (close - sma) / leg.atr)
        if close is not None and sma is not None
        else None,
        "return_1h_pct": _round(_close_return(history, hour, 1)),
        "return_4h_pct": _round(_close_return(history, hour, 4)),
        "return_24h_pct": _round(_close_return(history, hour, DAY_HOURS)),
        "return_7d_pct": _round(_close_return(history, hour, 7 * DAY_HOURS)),
        "return_28d_pct": _round(_close_return(history, hour, 28 * DAY_HOURS)),
        "atr_14d_pct_of_close": _round(100 * leg.atr / close) if close else None,
        "last_day_true_range_vs_atr": _round(true_range / leg.atr)
        if true_range is not None
        else None,
        "last_day_turnover_vs_prior_7d_average": _round(notional_ratio),
    }


def _broad_context(histories, hour):
    histories = [
        history
        for history in histories.values()
        if not excluded_symbol(history.coin) and ":" not in history.coin
    ]
    returns = {DAY_HOURS: [], 7 * DAY_HOURS: []}
    for history in histories:
        bars = _bars(history, hour - DAY_HOURS, hour)
        if not bars or bars[-1].volume <= 0:
            continue
        active = [bar.close * bar.volume for bar in bars if bar.volume > 0]
        if (
            len(active) < MINIMUM_ACTIVE_HOURS
            or sum(active) < MINIMUM_TURNOVER
            or median(active) < MINIMUM_TURNOVER / 72
        ):
            continue
        for span, values in returns.items():
            change = _close_return(history, hour, span)
            if change is not None:
                values.append(change)
    output = {}
    for span, values in returns.items():
        name = "24h" if span == DAY_HOURS else "7d"
        output[f"positive_{name}_share_pct"] = (
            _round(100 * fmean(value > 0 for value in values)) if len(values) >= 5 else None
        )
        output[f"median_{name}_return_pct"] = _round(median(values)) if len(values) >= 5 else None
    return output


def _packet_row(history, hour, token, leg):
    return {
        "id": token,
        "side": "long" if leg.side > 0 else "short",
        "age_estimate_bucket": _age_bucket(history, hour),
        "chart": _chart(history, hour, leg),
        "funding": {
            f"prior_{span}h_mean_bps_paid_per_hour": _paid_bps(history, hour, leg.side, span)
            for span in (1, 6, 24)
        },
    }


def build_packets(histories: Mapping[str, MarketHistory], plans: Mapping[int, list[TradePlan]]):
    """Return shareable packets and a separate private map to the original plans."""
    packets, linkage = {}, {}
    for hour, baseline in sorted(plans.items()):
        if not baseline:
            continue
        if type(hour) is not int or hour % DAY_HOURS or hour >= ARCHIVE_END_HOUR or hour <= 0:
            raise ValueError("Expected a development-only midnight signal hour")
        if any(len(plan.legs) != 1 or plan.label != ENHANCED_VARIANT for plan in baseline):
            raise ValueError("Expected the fixed enhanced Donchian entry plans")
        coins = [plan.legs[0].coin for plan in baseline]
        if len(coins) != len(set(coins)) or any(
            excluded_symbol(coin) or ":" in coin for coin in coins
        ):
            raise ValueError("Expected distinct native non-BTC candidates")

        # Shuffle the packet so its order does not disclose the baseline ranking.
        def order_key(plan, hour=hour):
            return hashlib.blake2s(f"llm-packet-v1/{hour}/{plan.legs[0].coin}".encode()).digest()

        reordered = sorted(baseline, key=order_key)
        mapping = {f"C{index}": plan for index, plan in enumerate(reordered, 1)}
        packets[hour] = {
            "schema": BREAKOUT_SCHEMA,
            "broad_market": _broad_context(histories, hour),
            "candidates": [
                _packet_row(histories[plan.legs[0].coin], hour, token, plan.legs[0])
                for token, plan in mapping.items()
            ],
        }
        linkage[hour] = mapping
    return packets, linkage


def build_inputs(archive: Path, start: int, end: int) -> TradeInputs:
    """Rebuild the existing fixed midnight rule and package only its eligible decisions."""
    if type(start) is not int or type(end) is not int or not 0 < start < end <= ARCHIVE_END_HOUR:
        raise ValueError("Expected increasing epoch hours inside the development cutoff")
    all_histories, coverage = load_archive(archive, end, include_quarters=True)
    histories, rejected = usable_histories(all_histories, coverage)
    if not histories:
        raise ValueError("No markets with usable price and funding history")
    base = build_plans(
        histories, start, end, MINIMUM_TURNOVER, daily_offset=0, liquidity_top_n=TOP_MARKETS
    )[BASELINE_VARIANT]
    plans, exit_signal, trend_diagnostics = build_liquid_trend(histories, base, start, end)
    packets, linkage = build_packets(histories, plans)
    candidate_counts = [len(packet["candidates"]) for packet in packets.values()]
    packet_sizes = [
        len(json.dumps(packet, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        for packet in packets.values()
    ]
    return TradeInputs(
        histories,
        plans,
        exit_signal,
        packets,
        linkage,
        {
            "archived_markets": len(all_histories),
            "tested_markets": sorted(histories),
            "excluded_market_data": rejected,
        },
        trend_diagnostics,
        {
            "decision_count": len(packets),
            "candidate_count": sum(candidate_counts),
            "maximum_candidates_per_packet": max(candidate_counts, default=0),
            "maximum_packet_bytes": max(packet_sizes, default=0),
        },
    )
