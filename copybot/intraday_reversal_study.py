"""Backtest liquid-perp reversal after a volume-backed failed hourly breakout."""

import argparse
from collections import defaultdict
from itertools import pairwise
from pathlib import Path
from statistics import fmean, median

from .broad_study import benchmark, run_book, usable_histories
from .liquid_trend_leverage import WINDOWS
from .meme_study import hour_date, qualification, write_json
from .research_data import ARCHIVE_END_HOUR, load_archive
from .research_execution import RiskLimits
from .research_models import OrderPlan, TradePlan, excluded_symbol
from .research_signals import _true_range

TURNOVER_FLOOR = 1_000_000
CAPITAL = 500
LOOKBACKS = (12, 24)
HOLD_HOURS = 6
IOC_CAP_BPS = 40


def build_reversal_plans(histories, start, end):
    if not 0 < start < end <= ARCHIVE_END_HOUR:
        raise ValueError("Use increasing UTC hours inside the Development cutoff")
    variants = {f"failed-breakout-{lookback}h": defaultdict(list) for lookback in LOOKBACKS}
    for coin, history in histories.items():
        if excluded_symbol(coin) or ":" in coin:
            continue
        for hour in range(start, end):
            latest = history.bars.get(hour - 1)
            prior = [history.bars.get(stamp) for stamp in range(hour - 25, hour - 1)]
            if latest is None or any(bar is None for bar in prior):
                continue
            current_volume = latest.close * latest.volume
            prior_volumes = [bar.close * bar.volume for bar in prior]
            usual_volume = median(prior_volumes)
            if current_volume <= 0 or usual_volume <= 0 or current_volume < 2 * usual_volume:
                continue
            active = [amount for amount in prior_volumes[1:] if amount > 0] + [current_volume]
            if (
                len(active) < 9
                or sum(active) < TURNOVER_FLOOR
                or median(active) < TURNOVER_FLOOR / 72
            ):
                continue
            atr = fmean(_true_range(bar, previous) for previous, bar in pairwise(prior[-15:]))
            if atr <= 0:
                continue
            for lookback in LOOKBACKS:
                high = max(bar.high for bar in prior[-lookback:])
                low = min(bar.low for bar in prior[-lookback:])
                above = latest.high > high + 0.5 * atr
                below = latest.low < low - 0.5 * atr
                if above and not below and latest.close < high - 0.1 * atr:
                    side, rejection = -1, high - latest.close
                elif below and not above and latest.close > low + 0.1 * atr:
                    side, rejection = 1, latest.close - low
                else:
                    continue
                name = f"failed-breakout-{lookback}h"
                order = OrderPlan(
                    coin,
                    side,
                    latest.close * (1 + side * IOC_CAP_BPS / 10_000),
                    atr,
                    1.5,
                    1.5,
                    HOLD_HOURS,
                    0,
                    mode="ioc",
                    signal_strength=rejection / atr,
                )
                variants[name][hour].append(TradePlan((order,), name))
    return {
        name: {
            hour: sorted(picks, key=lambda plan: (-plan.legs[0].signal_strength, plan.legs[0].coin))
            for hour, picks in sorted(rows.items())
        }
        for name, rows in variants.items()
    }


def evaluate(archive):
    study = {
        "scope": "Development data only. Tests a failed hourly breakout as a reversal.",
        "parameters": {
            "capital_usdc": CAPITAL,
            "prior_range_hours": LOOKBACKS,
            "hold_hours": HOLD_HOURS,
            "previous_atr_hours": 14,
            "excursion_atr": 0.5,
            "rejection_atr": 0.1,
            "volume_multiple": 2,
            "stop_atr": 1.5,
            "maker_target_atr": 1.5,
        },
        "periods": {},
    }
    for period, (start, end) in WINDOWS.items():
        all_markets, coverage = load_archive(archive, end, include_quarters=True)
        histories, excluded = usable_histories(all_markets, coverage)
        if not histories:
            raise ValueError("No markets with usable candle and funding history")
        cases = {}
        limits = RiskLimits(initial_equity=CAPITAL, max_leg_fraction=0.25)
        for name, plans in build_reversal_plans(histories, start, end).items():
            cases[name] = run_book(histories, plans, start, end, limits, profit_lock=0.25)
            stress = cases[name]["stress"]
            cases[name]["track_a"] = qualification("A", stress)
            print(period, name, f"net={stress['net']:.2f}", f"trades={stress['count']}", flush=True)
        study["periods"][period] = {
            "start": hour_date(start),
            "end_exclusive": hour_date(end),
            "markets": sorted(histories),
            "excluded": excluded,
            "strategies": cases,
            "benchmarks": benchmark(histories, start, end, CAPITAL, TURNOVER_FLOOR),
        }
    return study


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    write_json(args.output, evaluate(args.archive))


if __name__ == "__main__":
    main()
