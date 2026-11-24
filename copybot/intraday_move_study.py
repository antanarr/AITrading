"""Replay a synchronized hourly breakout with bounded intraday holding periods."""

import argparse
from collections import defaultdict
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path
from statistics import mean, median

from .broad_study import benchmark, run_book, usable_histories
from .liquid_trend_leverage import WINDOWS
from .meme_study import qualification, write_json
from .research_data import ARCHIVE_END_HOUR, load_archive
from .research_execution import RiskLimits
from .research_models import OrderPlan, TradePlan, excluded_symbol
from .research_signals import _true_range

MINIMUM_TURNOVER = 1_000_000
MINIMUM_ACTIVE_HOURS = 9
TOP_MARKETS = 20
MIN_PEERS = 8
CHANNEL_HOURS = 6
ATR_HOURS = 14
MIN_MEDIAN_IMPULSE = 0.75
MIN_COIN_IMPULSE = 1
MAX_ORDERS = 2
STOP_ATR = 1.25
IOC_CAP_BPS = 40
PULLBACK_ATR = 0.20
PULLBACK_TTL_HOURS = 2


@dataclass(frozen=True, slots=True)
class ExitPreset:
    mode: str
    hold_hours: int
    target_atr: float | None = None
    trailing_atr: float | None = None


PRESETS = {
    "ioc-target-6h": ExitPreset("ioc", 6, target_atr=2.5),
    "maker-target-6h": ExitPreset("maker", 6, target_atr=2.5),
    "ioc-trailing-12h": ExitPreset("ioc", 12, trailing_atr=1.5),
}


@dataclass(frozen=True, slots=True)
class Cue:
    coin: str
    turnover: float
    impulse: float
    close: float
    atr: float
    high: float
    low: float


def market_cues(history, start, end):
    for hour in range(start, end):
        latest = history.bars.get(hour - 1)
        if latest is None or latest.volume <= 0:
            continue
        prior = [history.bars.get(stamp) for stamp in range(hour - 25, hour - 1)]
        if not all(prior) or prior[-1].volume <= 0:
            continue
        active = [bar.close * bar.volume for bar in prior if bar.volume > 0]
        turnover = sum(active)
        if (
            turnover < MINIMUM_TURNOVER
            or len(active) < MINIMUM_ACTIVE_HOURS
            or median(active) < MINIMUM_TURNOVER / 72
        ):
            continue
        atr = mean(_true_range(bar, last) for last, bar in pairwise(prior[-ATR_HOURS - 1 :]))
        if atr <= 0:
            continue
        channel = prior[-CHANNEL_HOURS:]
        yield (
            hour,
            Cue(
                history.coin,
                turnover,
                (latest.close - latest.open) / atr,
                latest.close,
                atr,
                max(bar.high for bar in channel),
                min(bar.low for bar in channel),
            ),
        )


def build_candidates(histories, start, end):
    if not 25 < start < end <= ARCHIVE_END_HOUR:
        raise ValueError("Use only chronological Development data")
    cues = defaultdict(list)
    for coin, history in sorted(histories.items()):
        if excluded_symbol(coin) or ":" in coin:
            continue
        for hour, cue in market_cues(history, start, end):
            cues[hour].append(cue)
    plans = {name: {} for name in PRESETS}
    covered = qualified = 0
    for hour, rows in sorted(cues.items()):
        peers = sorted(rows, key=lambda row: (-row.turnover, row.coin))[:TOP_MARKETS]
        if len(peers) < MIN_PEERS:
            continue
        covered += 1
        impulse = median(row.impulse for row in peers)
        side = 1 if impulse > 0 else -1
        if abs(impulse) < MIN_MEDIAN_IMPULSE or 3 * sum(
            side * row.impulse > 0 for row in peers
        ) < 2 * len(peers):
            continue
        qualified += 1
        candidates = [
            row
            for row in peers
            if side * row.impulse >= MIN_COIN_IMPULSE
            and side * (row.close - (row.high if side > 0 else row.low)) > 0
        ]
        top = sorted(candidates, key=lambda row: (-side * row.impulse, row.coin))[:MAX_ORDERS]
        for row in top:
            for name, preset in PRESETS.items():
                limit = (
                    row.close - side * PULLBACK_ATR * row.atr
                    if preset.mode == "maker"
                    else row.close * (1 + side * IOC_CAP_BPS / 10_000)
                )
                order = OrderPlan(
                    coin=row.coin,
                    side=side,
                    limit=limit,
                    atr=row.atr,
                    stop_atr=STOP_ATR,
                    target_atr=preset.target_atr,
                    hold_hours=preset.hold_hours,
                    ttl_hours=PULLBACK_TTL_HOURS if preset.mode == "maker" else 0,
                    mode=preset.mode,
                    trailing_atr=preset.trailing_atr,
                )
                plans[name].setdefault(hour, []).append(TradePlan((order,), name))
    diagnostics = {
        "hours_with_enough_peers": covered,
        "broad_impulse_hours": qualified,
        "signal_hours": len(plans["ioc-target-6h"]),
        "orders": sum(map(len, plans["ioc-target-6h"].values())),
    }
    return plans, diagnostics


def run(archive):
    cutoff = WINDOWS["validation"][1]
    available, coverage = load_archive(archive, cutoff, include_quarters=True)
    histories, excluded = usable_histories(available, coverage)
    limits = RiskLimits(initial_equity=500, max_leg_fraction=0.25)
    results = {}
    for name, (start, end) in WINDOWS.items():
        print(f"Replaying {name}", flush=True)
        plans, diagnostics = build_candidates(histories, start, end)
        results[name] = {
            "diagnostics": diagnostics,
            "flat_net": 0,
            "buy_hold": benchmark(histories, start, end, limits.initial_equity, MINIMUM_TURNOVER),
            "presets": {
                preset: run_book(histories, orders, start, end, limits, profit_lock=0.25)
                for preset, orders in plans.items()
            },
        }

    for row in results.values():
        for summary in row["presets"].values():
            summary["track_b"] = qualification("B", summary["stress"])
    return {
        "calendar": {period: list(bounds) for period, bounds in WINDOWS.items()},
        "parameters": {
            "minimum_turnover": MINIMUM_TURNOVER,
            "top_markets": TOP_MARKETS,
            "minimum_peers": MIN_PEERS,
            "channel_hours": CHANNEL_HOURS,
            "minimum_breadth": "2/3",
            "minimum_median_impulse_atr": MIN_MEDIAN_IMPULSE,
            "minimum_coin_impulse_atr": MIN_COIN_IMPULSE,
            "stop_atr": STOP_ATR,
            "maximum_orders_per_hour": MAX_ORDERS,
            "ioc_cap_bps": IOC_CAP_BPS,
            "pullback_atr": PULLBACK_ATR,
            "pullback_ttl_hours": PULLBACK_TTL_HOURS,
            "risk_limits": asdict(limits),
            "presets": {name: asdict(preset) for name, preset in PRESETS.items()},
        },
        "markets": sorted(histories),
        "excluded": excluded,
        "results": results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    options = parser.parse_args(argv)
    write_json(options.output, run(options.archive))


if __name__ == "__main__":
    main()
