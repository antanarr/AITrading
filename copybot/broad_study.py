"""Compare published entry rules on an archived, liquid Hyperliquid universe."""

import argparse
from collections import defaultdict
from dataclasses import asdict
from datetime import date
from math import isfinite, sqrt
from pathlib import Path
from statistics import correlation, mean, median, stdev

from .meme_study import (
    COSTS,
    buy_and_hold,
    day_hour,
    hour_date,
    net_without_both,
    qualification,
    write_json,
)
from .public_strategies import build_plans, variants
from .research_data import ARCHIVE_END_HOUR, load_archive
from .research_execution import RiskLimits, replay
from .research_models import HOUR_MS

SLEEVE_PAIRS = {
    "shared-trend-reversion": ("donchian-20-4h-ioc", "hummingbot-macd-bb-1h-ioc"),
    "shared-daily-trend-reversion": ("donchian-20-24h-ioc", "hummingbot-macd-bb-1h-ioc"),
}
PARTITION_HOURS = 24


def usable_histories(histories, coverage):
    rejected = {}
    for coin in histories:
        asset = coverage["markets"][coin]
        hourly = asset["hourly"]
        if hourly["interior_gaps"]:
            rejected[coin] = "missing hourly candles"
        elif asset["missing_funding_for_hourly"]:
            rejected[coin] = "missing funding settlements"
    return {coin: item for coin, item in histories.items() if coin not in rejected}, rejected


def blend(left, right, start, phase=0):
    """Alternate entry priority while retaining each sleeve's candidate order."""
    return {
        hour: [
            *first.get(hour, ()),
            *second.get(hour, ()),
        ]
        for hour in sorted(left.keys() | right.keys())
        for first, second in (
            (left, right)
            if ((hour - start) // PARTITION_HOURS + phase) % 2 == 0
            else (right, left),
        )
    }


def allocation_summary(metrics, start, end, initial_equity):
    sizes = sorted(
        leg["size"] * leg["entry"] for trade in metrics["trades"] for leg in trade["legs"]
    )
    fractions = [
        leg["size"] * leg["entry"] / trade["sizing"]["equity_at_submission"]
        for trade in metrics["trades"]
        for leg in trade["legs"]
    ]
    by_strategy = defaultdict(float)
    by_side = defaultdict(float)
    days = defaultdict(lambda: defaultdict(float))
    notional_milliseconds = 0
    for trade in metrics["trades"]:
        by_strategy[trade["label"]] += trade["net"]
        for leg in trade["legs"]:
            by_side["long" if leg["side"] > 0 else "short"] += leg["net"]
            days[trade["label"]][leg["exited_ms"] // (24 * HOUR_MS)] += leg["net"]
            notional_milliseconds += (
                leg["size"] * leg["entry"] * (leg["exited_ms"] - leg["entered_ms"])
            )
    corr = None
    if len(days) == 2:
        left, right = days.values()
        domain = sorted(left.keys() | right.keys())
        left, right = [left.get(day, 0) for day in domain], [right.get(day, 0) for day in domain]
        if len(set(left)) > 1 and len(set(right)) > 1:
            corr = correlation(left, right)
    daily = [
        sum(sleeve.get(day, 0) for sleeve in days.values())
        for day in range(start // PARTITION_HOURS, end // PARTITION_HOURS)
    ]
    deviation = stdev(daily) if len(daily) > 1 else 0
    submissions = metrics["submission_diagnostics"]["replay"]
    rejected = submissions["rejected"]["entry_price_cap"]
    price_checked = submissions["accepted"] + rejected
    return {
        "min_notional": sizes[0] if sizes else None,
        "median_notional": median(sizes) if sizes else None,
        "max_notional": sizes[-1] if sizes else None,
        "median_equity_fraction_per_leg": median(fractions) if fractions else None,
        "net_by_strategy": dict(by_strategy),
        "net_by_side": dict(by_side),
        "daily_pnl_correlation_when_either_sleeve_closes": corr,
        "time_weighted_entry_gross_fraction_of_initial_equity": (
            notional_milliseconds / ((end - start) * HOUR_MS * initial_equity)
        ),
        "ioc_price_cap_rejections": rejected,
        "price_cap_rejection_share_of_accepted_plus_cap": rejected / price_checked
        if price_checked
        else None,
        "annualized_daily_realized_pnl_sharpe": sqrt(365) * mean(daily) / deviation
        if deviation
        else None,
    }


def run_book(
    histories,
    plans,
    start,
    end,
    limits,
    slots=None,
    exit_signal=None,
    profit_lock=0.0,
    *,
    cost_models=COSTS,
    isolated_margin=None,
):
    values = {}
    for label, costs in cost_models.items():
        score = replay(
            histories,
            plans,
            start,
            end,
            costs=costs,
            limits=limits,
            slots_by_strategy=slots,
            exit_signal=exit_signal,
            data_gap_proxy=True,
            profit_lock_fraction=profit_lock,
            isolated_margin=isolated_margin,
        )
        score["net_without_best_trade_and_coin"] = net_without_both(score)
        score["allocation"] = allocation_summary(score, start, end, limits.initial_equity)
        score["submission_diagnostics"].pop("executable_plans")
        values[label] = score
    return values


def benchmark(histories, start, end, equity, minimum_turnover):
    opening = []
    for coin, history in histories.items():
        before = [history.bars.get(hour) for hour in range(start - 24, start)]
        if all(before) and sum(bar.close * bar.volume for bar in before) >= minimum_turnover:
            opening.append(coin)
    return buy_and_hold(
        histories, opening, start, end, RiskLimits(initial_equity=equity), meme_only=False
    )


def evaluate(
    archive,
    start,
    end,
    capitals,
    minimum_turnover,
    include_pairs=False,
    profit_lock=0.25,
    daily_offset=0,
    only=None,
    risk_fraction=0.01,
    liquidity_top_n=None,
    enhanced_trend=False,
):
    all_histories, coverage = load_archive(archive, end, include_quarters=True)
    histories, rejected = usable_histories(all_histories, coverage)
    if not histories:
        raise ValueError("No markets with usable price and funding history")
    plans = build_plans(
        histories,
        start,
        end,
        minimum_turnover,
        daily_offset=daily_offset,
        liquidity_top_n=liquidity_top_n,
    )
    exits = {}
    pair_diagnostics = trend_diagnostics = None
    if include_pairs:
        from .public_pairs import PAIR_VARIANT, build_pair_plans, pair_exit_signal

        plans[PAIR_VARIANT], pair_diagnostics = build_pair_plans(
            histories, start, end, minimum_turnover=minimum_turnover
        )
        exits[PAIR_VARIANT] = pair_exit_signal(histories, pair_diagnostics)
    if enhanced_trend:
        from .liquid_trend import BASELINE_VARIANT, ENHANCED_VARIANT, build_liquid_trend

        plans[ENHANCED_VARIANT], exits[ENHANCED_VARIANT], trend_diagnostics = build_liquid_trend(
            histories, plans[BASELINE_VARIANT], start, end, daily_offset=daily_offset
        )
    if only:
        plans = {name: plans[name] for name in only}
    marked = {
        name: sorted({leg.coin for rows in by_hour.values() for bet in rows for leg in bet.legs})
        for name, by_hour in plans.items()
    }
    books = {}
    for equity in capitals:
        risk = RiskLimits(
            initial_equity=equity,
            max_positions=4,
            risk_fraction=risk_fraction,
            max_leg_fraction=0.25,
            max_gross_fraction=1.0,
            max_net_fraction=0.60,
        )
        candidates = {
            name: run_book(
                histories,
                signals,
                start,
                end,
                risk,
                exit_signal=exits.get(name),
                profit_lock=profit_lock,
            )
            for name, signals in plans.items()
        }
        if not only:
            for portfolio, (left, right) in SLEEVE_PAIRS.items():
                for phase in (0, 1):
                    key = f"{portfolio}-priority-{phase}"
                    candidates[key] = run_book(
                        histories,
                        blend(plans[left], plans[right], start, phase),
                        start,
                        end,
                        risk,
                        slots={left: 2, right: 2},
                        profit_lock=profit_lock,
                    )
        books[str(equity)] = {
            "limits": asdict(risk),
            "candidates": candidates,
            "benchmarks": benchmark(histories, start, end, equity, minimum_turnover),
        }
    return {
        "start": hour_date(start),
        "end_exclusive": hour_date(end),
        "archived_markets": len(all_histories),
        "tested_markets": sorted(histories),
        "meme_markets": sum(history.is_meme for history in histories.values()),
        "excluded_market_data": rejected,
        "signal_markets": marked,
        "pair_diagnostics": pair_diagnostics,
        "trend_diagnostics": trend_diagnostics,
        "capital_comparison": books,
    }


def summarize_qualification(discovery, validation):
    output = {}
    for capital, entry in validation["capital_comparison"].items():
        earlier = discovery["capital_comparison"][capital]["candidates"]
        result = {}
        for name, candidate in entry["candidates"].items():
            net = candidate["stress"]
            tracks = {track: qualification(track, net) for track in ("A", "B")}
            status = (
                earlier[name]["stress"]["net"] > 0
                and net["terminal_proxy_exits"] == 0
                and earlier[name]["stress"]["terminal_proxy_exits"] == 0
                and any(track["pass"] for track in tracks.values())
            )
            result[name] = {"passes_historical": status, "validation_tracks": tracks}
        output[capital] = result
    return output


def run(args):
    start, middle, end = map(day_hour, (args.start, args.validation_start, args.end))
    if not start < middle < end <= ARCHIVE_END_HOUR:
        raise ValueError("Use only the historical Discovery and Validation data")
    if any(not isfinite(value) or value <= 0 for value in args.capital):
        raise ValueError("Starting equity must be positive and finite")
    if not isfinite(args.risk_fraction) or not 0 < args.risk_fraction <= 1:
        raise ValueError("The risk fraction must be positive and no greater than one")
    if args.enhanced_trend and args.liquidity_top_n != 20:
        raise ValueError("The enhanced trend candidate uses the trailing top 20 universe")
    report = {
        "strategies": [asdict(variant) for variant in variants()],
        "shared_sleeves": SLEEVE_PAIRS,
        "minimum_turnover": args.minimum_turnover,
        "profit_lock_fraction": args.profit_lock_fraction,
        "daily_candle_utc_offset": args.daily_offset,
        "risk_fraction": args.risk_fraction,
        "liquidity_top_n": args.liquidity_top_n,
        "discovery": evaluate(
            args.archive,
            start,
            middle,
            args.capital,
            args.minimum_turnover,
            args.pairs,
            args.profit_lock_fraction,
            args.daily_offset,
            args.only,
            args.risk_fraction,
            args.liquidity_top_n,
            args.enhanced_trend,
        ),
        "validation": evaluate(
            args.archive,
            middle,
            end,
            args.capital,
            args.minimum_turnover,
            args.pairs,
            args.profit_lock_fraction,
            args.daily_offset,
            args.only,
            args.risk_fraction,
            args.liquidity_top_n,
            args.enhanced_trend,
        ),
    }
    report["qualification"] = summarize_qualification(report["discovery"], report["validation"])
    write_json(args.output, report)
    for capital, statuses in report["qualification"].items():
        for name, status in statuses.items():
            values = [
                report[period]["capital_comparison"][capital]["candidates"][name]["stress"]["net"]
                for period in ("discovery", "validation")
            ]
            print(
                capital, name, *[round(value, 3) for value in values], status["passes_historical"]
            )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 5, 25))
    parser.add_argument("--validation-start", type=date.fromisoformat, default=date(2026, 9, 26))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 11, 21))
    parser.add_argument("--capital", type=float, nargs="+", default=[300.0, 500.0])
    parser.add_argument("--minimum-turnover", type=float, default=1_000_000)
    parser.add_argument("--pairs", action="store_true")
    parser.add_argument("--profit-lock-fraction", type=float, default=0.25)
    parser.add_argument("--daily-offset", type=int, default=0)
    parser.add_argument("--risk-fraction", type=float, default=0.01)
    parser.add_argument(
        "--liquidity-top-n",
        type=int,
        help="Rank eligible markets by turnover in the preceding 30 days",
    )
    parser.add_argument("--enhanced-trend", action="store_true")
    parser.add_argument("--only", nargs="+")
    args = parser.parse_args(argv)
    if args.only:
        supported = {item.name for item in variants()}
        if args.enhanced_trend:
            from .liquid_trend import ENHANCED_VARIANT

            supported.add(ENHANCED_VARIANT)
        if set(args.only) - supported:
            parser.error("Unknown or disabled strategy in --only")
    run(args)


if __name__ == "__main__":
    main()
