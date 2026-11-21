"""Rerun the unchanged paper breakout across calendar windows and fill assumptions."""

import argparse
from dataclasses import replace
from datetime import date
from functools import partial
from math import isfinite
from pathlib import Path

from .binance_regime_archive import END as REFERENCE_END
from .binance_regime_archive import START as REFERENCE_START
from .binance_regime_archive import inspect_binance_regime
from .broad_study import benchmark, run_book, usable_histories
from .liquid_trend import BASELINE_VARIANT, build_liquid_trend
from .liquid_trend_leverage import CASCADE, CASCADE_MARK_BPS, WINDOWS, diagnostics, policy_leverage
from .meme_study import COSTS, day_hour, hour_date, qualification, write_json
from .public_strategies import build_plans
from .research_data import ARCHIVE_END_HOUR, load_archive
from .research_execution import IsolatedMargin, RiskLimits

PROFILES = {
    "midnight": (0, 0),
    "delayed_one_hour": (0, 1),
    "08_utc": (8, 0),
    "16_utc": (16, 0),
}
LEVERAGES = ("1x", "3x")


def delay_execution(plans, exit_signal, end, lag):
    """Shift scheduled entries and channel orders, keeping their information cutoff."""
    if not lag:
        return plans, exit_signal

    def shifted_exit(plan, submitted, current):
        return exit_signal(plan, submitted - lag, current - lag)

    return {hour + lag: orders for hour, orders in plans.items() if hour + lag < end}, shifted_exit


def calendar_windows(start, end, step):
    boundaries = [start]
    cursor = start
    while True:
        year, month = divmod(cursor.year * 12 + cursor.month - 1 + step, 12)
        cursor = date(year, month + 1, 1)
        if cursor >= end:
            break
        boundaries.append(cursor)
    boundaries.append(end)
    return {
        f"reset_{first:%Y-%m}": (day_hour(first), day_hour(last))
        for first, last in zip(boundaries[:-1], boundaries[1:], strict=True)
    }


def compact_score(score):
    diagnostics(score)
    submission = score["submission_diagnostics"]["replay"]
    return {
        **{
            key: value
            for key, value in score.items()
            if key not in {"trades", "submission_diagnostics"}
        },
        "orders": submission,
        "track_b": qualification("B", score),
    }


def run_scenarios(histories, windows, profiles, caps, capital, taker_bps):
    start = min(window[0] for window in windows.values())
    end = max(window[1] for window in windows.values())
    if not 0 < start < end <= ARCHIVE_END_HOUR:
        raise ValueError("Use development data only")
    if not histories or any(type(cap) is not int or cap <= 0 for cap in caps.values()):
        raise ValueError("Replay requires histories and a valid maintenance basis")
    limits = RiskLimits(initial_equity=capital, max_leg_fraction=0.25)
    costs = {name: replace(cost, taker_fee_bps=taker_bps) for name, cost in COSTS.items()}
    cascade = replace(CASCADE, taker_fee_bps=taker_bps)
    indicators = {}
    results = {
        name: {
            "start": hour_date(left),
            "end_exclusive": hour_date(right),
            "benchmarks": benchmark(histories, left, right, capital, 1_000_000),
            "profiles": {},
        }
        for name, (left, right) in windows.items()
    }
    for profile in profiles:
        offset, lag = PROFILES[profile]
        if offset not in indicators:
            entries = build_plans(histories, start, end, daily_offset=offset, liquidity_top_n=20)
            indicators[offset] = build_liquid_trend(
                histories, entries[BASELINE_VARIANT], start, end, daily_offset=offset
            )
        plans, exits, _ = indicators[offset]
        for name, (left, right) in windows.items():
            local = {hour: orders for hour, orders in plans.items() if left <= hour < right}
            points, exit_signal = delay_execution(local, exits, right, lag)
            policies = {}
            for leverage in LEVERAGES:
                margin_policy = IsolatedMargin(caps, partial(policy_leverage, leverage, caps))
                common = {
                    "exit_signal": exit_signal,
                    "profit_lock": 0.25,
                }
                regular = run_book(
                    histories,
                    points,
                    left,
                    right,
                    limits,
                    **common,
                    cost_models=costs,
                    isolated_margin=margin_policy,
                )
                shock = run_book(
                    histories,
                    points,
                    left,
                    right,
                    limits,
                    **common,
                    cost_models={"cascade": cascade},
                    isolated_margin=replace(margin_policy, adverse_mark_bps=CASCADE_MARK_BPS),
                )
                policies[leverage] = {
                    name: compact_score(score) for name, score in {**regular, **shock}.items()
                }
            results[name]["profiles"][profile] = policies
        print(f"Finished {profile} across {len(windows)} windows", flush=True)
    return results


def evaluate(native, reference, capital):
    beginning = WINDOWS["discovery"][0]
    all_native, coverage = load_archive(native, ARCHIVE_END_HOUR, include_quarters=True)
    histories, excluded = usable_histories(all_native, coverage)
    caps = {coin: coverage["markets"][coin]["census_max_leverage"] for coin in histories}
    native_windows = {
        "continuous": (beginning, ARCHIVE_END_HOUR),
        **WINDOWS,
        **calendar_windows(date(2026, 6, 1), date(2026, 11, 21), 1),
    }
    results = {
        "capital": capital,
        "clock_profiles_utc": {name: list(settings) for name, settings in PROFILES.items()},
        "model": {
            "rules": "Unchanged top-20 20-day Donchian, SMA50, 2 ATR stop, opposite 10-day channel, 10-day timeout.",
            "scope": "Development replays; calendar windows are disjoint within a venue, related scenarios overlap.",
            "execution": "Archived candle execution proxies, published funding rates with a preceding-close price proxy.",
            "delay": "One-hour stress for entry and channel exit; original signal timestamp and price cap are unchanged.",
            "reset": "Each named window starts with the same collateral and no holdings; closes at its last candle.",
            "cascade": "3% extra protective-stop slippage plus 1% adverse displacement of the mark proxy.",
            "capital_controls": "1% collateral-normalized stop risk per leg; 4 slots and 25% leg maximum.",
        },
        "hyperliquid": {
            "markets": sorted(histories),
            "excluded": excluded,
            "windows": run_scenarios(histories, native_windows, PROFILES, caps, capital, 4.5),
        },
    }
    external, coverage = inspect_binance_regime(reference)
    reference_windows = {
        "continuous": (day_hour(REFERENCE_START), day_hour(REFERENCE_END)),
        **calendar_windows(REFERENCE_START, REFERENCE_END, 3),
    }
    results["binance_reference"] = {
        "markets": sorted(external),
        "coverage": coverage,
        "assumptions": (
            "Earlier cross-venue stress reference. Uses 5 bps taker each side, "
            "Hyperliquid-style sizing and 10% assumed maintenance across contracts. "
            "The archive has no historical Binance mark or margin schedule."
        ),
        "windows": run_scenarios(
            external,
            reference_windows,
            ("midnight", "delayed_one_hour"),
            dict.fromkeys(external, 5),
            capital,
            5,
        ),
    }
    results["replays"] = sum(
        len(costs)
        for venue in ("hyperliquid", "binance_reference")
        for window in results[venue]["windows"].values()
        for profile in window["profiles"].values()
        for costs in profile.values()
    )
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("native", type=Path)
    parser.add_argument("reference", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--capital", type=float, default=500)
    args = parser.parse_args(argv)
    if not isfinite(args.capital) or args.capital <= 0:
        parser.error("Starting collateral must be positive and finite")
    results = evaluate(args.native, args.reference, args.capital)
    write_json(args.output, results)
    print(f"Published {results['replays']} replays to {args.output}", flush=True)


if __name__ == "__main__":
    main()
