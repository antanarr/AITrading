"""Compare isolated collateral policies on the fixed daily Hyperliquid breakout."""

import argparse
from collections import defaultdict
from dataclasses import replace
from datetime import date
from functools import partial
from math import isfinite
from pathlib import Path
from statistics import median

from .broad_study import benchmark, run_book, usable_histories
from .meme_study import COSTS, day_hour, hour_date, qualification, write_json
from .research_data import ARCHIVE_END_HOUR, load_archive
from .research_execution import PER_BPS, STRESS, IsolatedMargin, RiskLimits
from .research_models import DAY_HOURS, HOUR_MS

WINDOWS = {
    "discovery": (day_hour(date(2026, 5, 25)), day_hour(date(2026, 9, 26))),
    "validation": (day_hour(date(2026, 9, 26)), ARCHIVE_END_HOUR),
}
POLICIES = ("1x", "3x", "5x-strict", "5x-capped", "mixed")
CASCADE = replace(STRESS, name="cascade", adverse_stop_fill_bps=300)
CASCADE_MARK_BPS = 100


def policy_leverage(policy, caps, order, signal_count, same_side):
    if policy in ("1x", "3x", "5x-strict"):
        return int(policy[0])
    if policy == "5x-capped":
        return min(5, caps[order.coin])
    if policy != "mixed":
        raise ValueError(f"Unknown leverage policy {policy}")
    if signal_count > 2 or same_side:
        return 1
    if order.signal_strength is None:
        raise ValueError("Adaptive leverage requires a causal strength value")
    return min(5 if order.signal_strength >= 0.5 else 3, caps[order.coin])


def diagnostics(score):
    legs = [leg for trade in score["trades"] for leg in trade["legs"]]
    entry_days = defaultdict(float)
    for leg in legs:
        entry_days[leg["entered_ms"] // (DAY_HOURS * HOUR_MS)] += leg["net"]
    margin = score["isolated_margin"]
    extra_exit_slippage = 3 * (STRESS.half_spread_bps + STRESS.exit_impact_bps) / PER_BPS
    margin.update(
        median_collateral=median(leg["collateral"] for leg in legs) if legs else None,
        entry_day_clusters=len(entry_days),
        net_without_best_entry_day=score["net"] - max(0.0, max(entry_days.values(), default=0.0)),
        narrow_liquidation_buffers=sum(
            abs(leg["entry"] - leg["initial_stop"]) + leg["entry"] * extra_exit_slippage
            >= 0.75 * abs(leg["entry"] - leg["initial_liquidation_threshold"])
            for leg in legs
        ),
    )


def stage(archive, start, end, equity, offsets):
    from .liquid_trend import BASELINE_VARIANT, build_liquid_trend
    from .public_strategies import build_plans

    if not 0 < start < end <= ARCHIVE_END_HOUR:
        raise ValueError("The leverage study may only read development windows")
    all_histories, coverage = load_archive(archive, end, include_quarters=True)
    histories, excluded = usable_histories(all_histories, coverage)
    caps = {coin: coverage["markets"][coin]["census_max_leverage"] for coin in histories}
    if not caps or any(type(cap) is not int or cap <= 0 for cap in caps.values()):
        raise ValueError("The leverage study requires archived contract leverage caps")
    risk = RiskLimits(initial_equity=equity, max_leg_fraction=0.25)
    clocks = {}
    for offset in offsets:
        daily = build_plans(histories, start, end, daily_offset=offset, liquidity_top_n=20)
        plans, exits, details = build_liquid_trend(
            histories, daily[BASELINE_VARIANT], start, end, daily_offset=offset
        )
        books = {}
        for policy in POLICIES:
            margin = IsolatedMargin(caps, partial(policy_leverage, policy, caps))
            scores = run_book(
                histories,
                plans,
                start,
                end,
                risk,
                exit_signal=exits,
                profit_lock=0.25,
                cost_models=COSTS,
                isolated_margin=margin,
            )
            scores.update(
                run_book(
                    histories,
                    plans,
                    start,
                    end,
                    risk,
                    exit_signal=exits,
                    profit_lock=0.25,
                    cost_models={"cascade": CASCADE},
                    isolated_margin=replace(margin, adverse_mark_bps=CASCADE_MARK_BPS),
                )
            )
            for score in scores.values():
                diagnostics(score)
            scores["track_b"] = qualification("B", scores["stress"])
            books[policy] = scores
        clocks[str(offset)] = {"entries": details, "policies": books}
    return {
        "start": hour_date(start),
        "end_exclusive": hour_date(end),
        "market_caps_from_archive_census": caps,
        "excluded_market_data": excluded,
        "benchmarks": benchmark(histories, start, end, equity, 1_000_000),
        "clocks": clocks,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--capital", type=float, default=500)
    parser.add_argument("--daily-offsets", type=int, nargs="+", default=[0, 8, 16])
    args = parser.parse_args(argv)
    if not isfinite(args.capital) or args.capital <= 0:
        parser.error("Starting collateral must be positive and finite")
    if any(not 0 <= offset < DAY_HOURS for offset in args.daily_offsets):
        parser.error("Daily offsets must be between 0 and 23 UTC")
    report = {
        "starting_collateral": args.capital,
        "mixed_policy": (
            "Use 1x with more than two simultaneous signals or a same-side position, "
            "otherwise 5x for a breakout at least 0.5 ATR beyond the channel or 3x for "
            "a weaker one, capped by the contract. Strength is not a calibrated probability."
        ),
        "collateral_policy": (
            "Common collateral allocation per leg, gross, and side. Leverage multiplies "
            "the allowed loss from the original stop risk formula. Exchange lot rounding, "
            "minimum trade notional and evolving equity can change allocations and fills."
        ),
        "margin_proxy": (
            "Uses smallest-tier maintenance from the archived census, not historical tier changes. "
            "Trade candles stand in for mark prices. Gives liquidation priority within an "
            "ambiguous candle and assumes full loss of isolated collateral. Funding uses "
            "the preceding trade close as its settlement-price proxy."
        ),
        "cascade_stress": (
            "Apply 300 bps additional slippage to protective stop fills and shift the "
            "liquidation mark proxy 100 bps against the position. Stop sizing uses the "
            "ordinary stress costs at entry. This is a sensitivity test."
        ),
        **{
            period: stage(args.archive, start, end, args.capital, args.daily_offsets)
            for period, (start, end) in WINDOWS.items()
        },
    }
    write_json(args.output, report)
    for period in WINDOWS:
        for offset, clock in report[period]["clocks"].items():
            for policy, scores in clock["policies"].items():
                score = scores["stress"]
                print(
                    period,
                    offset,
                    policy,
                    f"net={score['net']:.2f}",
                    f"drawdown={score['intrabar_drawdown']:.2f}",
                    f"trades={score['count']}",
                    f"track_b={scores['track_b']['pass']}",
                )


if __name__ == "__main__":
    main()
