"""Test slower non-BTC trend positions across Hyperliquid and older Binance data."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date
from itertools import pairwise
from math import log, sqrt
from pathlib import Path
from statistics import fmean, median, pstdev

from .binance_regime_archive import END as REFERENCE_END
from .binance_regime_archive import START as REFERENCE_START
from .binance_regime_archive import SYMBOLS, inspect_binance_regime
from .broad_study import benchmark, run_book, usable_histories
from .liquid_trend_leverage import WINDOWS
from .meme_study import COSTS, day_hour, hour_date, qualification, write_json
from .research_data import ARCHIVE_END_HOUR, load_archive
from .research_execution import RiskLimits
from .research_models import DAY_HOURS, OrderPlan, TradePlan, excluded_symbol
from .research_signals import _true_range

CAPITAL = 500
FAST_DAYS = 14
SLOW_DAYS = 56
VOL_DAYS = 28
MINIMUM_TURNOVER = 1_000_000
MIN_ACTIVE_HOURS = 9
IOC_CAP_BPS = 40
HOLD_DAYS = 45
OFFSETS = (0, 12)
EXIT_BANDS = {"zero-exit": 0.0, "buffered-exit": 0.5}
SOURCE = "https://doi.org/10.1016/j.jfineco.2011.11.003"


@dataclass(frozen=True, slots=True)
class Day:
    close: float
    low: float
    high: float
    turnover: float
    active_hours: int
    traded_last_hour: bool


def closed_days(history, start, end, offset):
    first = start - (SLOW_DAYS + 1) * DAY_HOURS
    first += (offset - first) % DAY_HOURS
    days = {}
    for end_hour in range(first, end, DAY_HOURS):
        bars = [history.bars.get(hour) for hour in range(end_hour - DAY_HOURS, end_hour)]
        if any(bar is None for bar in bars):
            continue
        days[end_hour] = Day(
            bars[-1].close,
            min(bar.low for bar in bars),
            max(bar.high for bar in bars),
            sum(bar.close * bar.volume for bar in bars),
            sum(bar.volume > 0 for bar in bars),
            bars[-1].volume > 0,
        )
    return days


def trend_at(days, hour):
    sequence = [
        days.get(stamp) for stamp in range(hour - SLOW_DAYS * DAY_HOURS, hour + 1, DAY_HOURS)
    ]
    if any(row is None or row.close <= 0 or row.active_hours == 0 for row in sequence):
        return None
    last = sequence[-1]
    if not last.traded_last_hour:
        return None
    volatility = pstdev(
        log(day.close / prev.close) for prev, day in pairwise(sequence[-VOL_DAYS - 1 :])
    )
    atr = fmean(_true_range(day, prev) for prev, day in pairwise(sequence[-15:]))
    if volatility <= 0 or atr <= 0:
        return None
    fast, slow = (
        log(last.close / sequence[-days - 1].close) / (volatility * sqrt(days))
        for days in (FAST_DAYS, SLOW_DAYS)
    )
    score = (fast + slow) / 2
    side = 1 if score > 0 else -1
    entry = abs(score) >= 1 and min(fast * side, slow * side) > 0
    return score, side if entry else 0, last.close, atr


def build_plans(histories, start, end, offset):
    if not 0 < start < end <= ARCHIVE_END_HOUR or offset not in range(DAY_HOURS):
        raise ValueError("Use increasing hours inside the Development cutoff and a UTC clock")
    observations, ideas = {}, {}
    for coin, history in histories.items():
        if excluded_symbol(coin) or ":" in coin:
            continue
        days = closed_days(history, start, end, offset)
        for hour in range(start + (offset - start) % DAY_HOURS, end, DAY_HOURS):
            if (trend := trend_at(days, hour)) is None:
                continue
            score, side, close, atr = trend
            observations[(hour, coin)] = score
            day = days[hour]
            if not side or day.active_hours < MIN_ACTIVE_HOURS or day.turnover < MINIMUM_TURNOVER:
                continue
            plan = OrderPlan(
                coin,
                side,
                close * (1 + side * IOC_CAP_BPS / 10_000),
                atr,
                3,
                None,
                HOLD_DAYS * DAY_HOURS,
                0,
                mode="ioc",
                signal_strength=abs(score),
            )
            ideas.setdefault(hour, []).append(TradePlan((plan,), "volatility-normalized-trend"))
    for candidates in ideas.values():
        candidates.sort(key=lambda plan: (-plan.legs[0].signal_strength, plan.legs[0].coin))
    return ideas, observations


def make_exit(observations, band):
    def exit_signal(plan, _submitted_hour, hour):
        leg = plan.legs[0]
        score = observations.get((hour, leg.coin))
        if score is not None and score * leg.side <= -band:
            return "trend reversal"
        return None

    return exit_signal


def platform_replays(histories, periods, cost_models):
    if not histories:
        raise ValueError("No usable archive markets")
    first, last = min(x[0] for x in periods.values()), max(x[1] for x in periods.values())
    signals = {offset: build_plans(histories, first, last, offset) for offset in OFFSETS}
    limits = RiskLimits(initial_equity=CAPITAL, max_leg_fraction=0.25)
    results = {}
    for name, (start, end) in periods.items():
        clocks = {}
        for offset, (orders, observations) in signals.items():
            candidates = {hour: rows for hour, rows in orders.items() if start <= hour < end}
            clocks[str(offset)] = cases = {}
            for rule, band in EXIT_BANDS.items():
                score = run_book(
                    histories,
                    candidates,
                    start,
                    end,
                    limits,
                    exit_signal=make_exit(observations, band),
                    profit_lock=0.25,
                    cost_models=cost_models,
                )
                stress = score["stress"]
                score["track_b"] = qualification("B", stress)
                held = [
                    (trade["exited_ms"] - trade["entered_ms"]) / 3_600_000
                    for trade in stress["trades"]
                ]
                score["median_held_hours"] = median(held) if held else None
                cases[rule] = score
                print(
                    name,
                    offset,
                    rule,
                    f"net={stress['net']:.2f}",
                    f"trades={stress['count']}",
                    flush=True,
                )
        results[name] = {
            "start": hour_date(start),
            "end_exclusive": hour_date(end),
            "clocks": clocks,
            "benchmarks": benchmark(histories, start, end, CAPITAL, MINIMUM_TURNOVER),
        }
    return results


def evaluate(native, reference):
    full, coverage = load_archive(native, ARCHIVE_END_HOUR, include_quarters=True)
    native_history, exclusions = usable_histories(full, coverage)
    study = {
        "source": SOURCE,
        "parameters": {
            "starting_usdc": CAPITAL,
            "lookbacks_days": [FAST_DAYS, SLOW_DAYS],
            "volatility_window_days": VOL_DAYS,
            "stop_atr": 3,
            "hold_days": HOLD_DAYS,
            "entry_at_least_standardized_score": 1,
            "exit_opposite_bands": EXIT_BANDS,
            "turnover_minimum": MINIMUM_TURNOVER,
            "adaptation": "Daily candidate scan with discrete fixed entry and buffered exit. The source paper uses longer horizons.",
        },
        "hyperliquid": {
            "markets": sorted(native_history),
            "excluded": exclusions,
            "periods": platform_replays(
                native_history,
                {**WINDOWS, "continuous": (WINDOWS["discovery"][0], ARCHIVE_END_HOUR)},
                COSTS,
            ),
        },
    }
    def read_market(coin):
        return inspect_binance_regime(reference, coins=(coin,))

    history, coverage = {}, {}
    with ThreadPoolExecutor(max_workers=8) as workers:
        for histories, observations in workers.map(read_market, SYMBOLS):
            history.update(histories)
            coverage.update(observations)
    beginning, ending = day_hour(REFERENCE_START), day_hour(REFERENCE_END)
    split_2025, split_2026 = day_hour(date(2025, 1, 1)), day_hour(date(2026, 1, 1))
    earlier_periods = {
        "continuous": (beginning, ending),
        "2024": (beginning, split_2025),
        "2025": (split_2025, split_2026),
        "2026": (split_2026, ending),
    }
    earlier_costs = {name: replace(cost, taker_fee_bps=5) for name, cost in COSTS.items()}
    study["binance_reference"] = {
        "coverage": coverage,
        "markets": sorted(history),
        "assumptions": "5 bps assumed taker fee and shared 1× candle execution proxies with observed funding.",
        "periods": platform_replays(history, earlier_periods, earlier_costs),
    }
    return study


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("native", type=Path)
    parser.add_argument("reference", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    write_json(args.output, evaluate(args.native, args.reference))


if __name__ == "__main__":
    main()
