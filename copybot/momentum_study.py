"""Replay two fixed, own-return trend rules on native and earlier venue archives."""

import argparse
from collections import defaultdict
from dataclasses import asdict
from datetime import date
from math import isfinite, sqrt
from pathlib import Path

import numpy as np

from .binance_regime_archive import END as BINANCE_END
from .binance_regime_archive import START as BINANCE_START
from .binance_regime_archive import inspect_binance_regime
from .broad_study import benchmark, run_book, usable_histories
from .directional_features import _aggregate, _bars
from .liquid_trend_reference import COST_MODELS as BINANCE_COSTS
from .meme_study import COSTS, day_hour, qualification, write_json
from .public_strategies import IOC_CAP_BPS, _atr, _liquid
from .research_data import ARCHIVE_END_HOUR, load_archive
from .research_execution import RiskLimits
from .research_models import DAY_HOURS, OrderPlan, TradePlan, excluded_symbol

MOMENTUM = "tsmom-28d-weekly"
AGREEMENT = "tsmom-7d-28d-agreement-weekly"
WEEK_HOURS = 7 * DAY_HOURS
CLOCKS = {"monday": -3 * DAY_HOURS, "thursday": 0}
TURNOVER_FLOOR = 1_000_000
STOP_ATR = 2
# The old position must be flat before the replay considers the next week's entries.
HOLD_HOURS = WEEK_HOURS - 1
HYPERLIQUID_PERIODS = (
    ("discovery", date(2026, 5, 25), date(2026, 9, 26)),
    ("validation", date(2026, 9, 26), date(2026, 11, 21)),
)
BINANCE_PERIODS = (
    ("2024", BINANCE_START, date(2025, 1, 1)),
    ("2025", date(2025, 1, 1), date(2026, 1, 1)),
    ("2026", date(2026, 1, 1), BINANCE_END),
)
SOURCES = {
    "time_series_momentum": "https://doi.org/10.1016/j.jfineco.2011.11.003",
    "multiple_momentum_lookbacks": (
        "https://www.aqr.com/insights/research/journal-article/a-century-of-evidence-on-trend-following-investing"
    ),
}


def build_momentum_plans(histories, start, end, clock):
    """Score weekly decisions using only complete daily prices before the execution hour."""
    if not 0 < start < end <= ARCHIVE_END_HOUR or clock not in CLOCKS:
        raise ValueError("Expected an assigned weekday and dates inside the development cutoff")
    ranked = {name: defaultdict(list) for name in (MOMENTUM, AGREEMENT)}
    first = max(0, start - 30 * DAY_HOURS)
    for history in histories.values():
        coin = history.coin
        if excluded_symbol(coin) or ":" in coin or not history.bars:
            continue
        hourly = _bars(history.bars, first, end)
        daily = _aggregate(hourly, DAY_HOURS)
        liquid = _liquid(hourly.set_axis(hourly.index + 1), TURNOVER_FLOOR)
        log_close = np.log(daily.close)
        daily_moves = log_close.diff()
        fast, slow = log_close.diff(7), log_close.diff(28)
        vol = daily_moves.rolling(28).std(ddof=0) * sqrt(28)
        atr = _atr(daily)
        valid = (
            atr.gt(0)
            & slow.ne(0)
            & vol.gt(0)
            & liquid.reindex(daily.index, fill_value=False)
            & ((daily.index - CLOCKS[clock]) % WEEK_HOURS == 0)
            & (daily.index >= start)
            & (daily.index < end)
        )
        for hour in daily.index[valid]:
            side = 1 if slow.at[hour] > 0 else -1
            leg = OrderPlan(
                coin,
                side,
                float(daily.close.at[hour] * (1 + side * IOC_CAP_BPS / 10_000)),
                float(atr.at[hour]),
                STOP_ATR,
                None,
                HOLD_HOURS,
                0,
                "ioc",
                signal_strength=float(abs(slow.at[hour]) / vol.at[hour]),
            )
            ranked[MOMENTUM][int(hour)].append(TradePlan((leg,), MOMENTUM))
            if side * fast.at[hour] > 0:
                ranked[AGREEMENT][int(hour)].append(TradePlan((leg,), AGREEMENT))
    return {
        name: {
            hour: sorted(picks, key=lambda item: (-item.legs[0].signal_strength, item.legs[0].coin))
            for hour, picks in sorted(book.items())
        }
        for name, book in ranked.items()
    }


def _period(histories, start, end, capitals, costs, report):
    if not histories:
        raise ValueError("No usable archived markets in the evaluation period")
    by_clock = {clock: build_momentum_plans(histories, start, end, clock) for clock in CLOCKS}
    rules = {}
    for capital in capitals:
        limits = RiskLimits(initial_equity=capital, max_positions=4, max_leg_fraction=0.25)
        results = {}
        for clock, versions in by_clock.items():
            results[clock] = {}
            for name, plans in versions.items():
                row = run_book(histories, plans, start, end, limits, cost_models=costs)
                row["clock_anchors_with_signals"] = len(plans)
                row["signal_markets"] = sorted(
                    {p.legs[0].coin for orders in plans.values() for p in orders}
                )
                row["qualification"] = {
                    track: qualification(track, row["stress"]) for track in ("A", "B")
                }
                results[clock][name] = row
        rules[str(capital)] = {
            "risk": asdict(limits),
            "portfolio": results,
            "benchmark_shared_hyperliquid_fee_model": benchmark(
                histories, start, end, capital, TURNOVER_FLOOR
            ),
        }
    return {"markets": sorted(histories), "coverage_exclusions": report, "capital": rules}


def study(venue, archive, capitals):
    periods = HYPERLIQUID_PERIODS if venue == "hyperliquid" else BINANCE_PERIODS
    costs = COSTS if venue == "hyperliquid" else BINANCE_COSTS
    earlier = inspect_binance_regime(archive) if venue == "binance" else None
    report = {
        "venue": venue,
        "sources": SOURCES,
        "assumptions": {
            "own_return_days": [7, 28],
            "clock": {clock: "00:00 UTC" for clock in CLOCKS},
            "hold_hours": HOLD_HOURS,
            "stop_atr_daily": STOP_ATR,
            "24h_turnover_floor": TURNOVER_FLOOR,
            "ioc_cap_bps": IOC_CAP_BPS,
            "profit_lock": False,
            "costs": {name: asdict(model) for name, model in costs.items()},
            "adaptation": "Published time-series momentum adapted to weekly crypto. No direct paper replication.",
            "benchmark": "The broad buy-and-hold comparison uses the shared Hyperliquid fees and mark proxies on both venues.",
            "binance_execution": "Candle proxy with Binance archived lot sizes and funding but Hyperliquid-style price rounding and venue minimum.",
        },
        "evaluation": {},
    }
    for label, first, last in periods:
        start, end = day_hour(first), day_hour(last)
        if earlier is not None:
            histories, coverage = earlier
            excluded = {
                coin: row["reason"] for coin, row in coverage.items() if coin not in histories
            }
        else:
            histories, coverage = load_archive(archive, end, include_quarters=True)
            histories, excluded = usable_histories(histories, coverage)
        result = _period(histories, start, end, capitals, costs, excluded)
        report["evaluation"][label] = {
            "utc_start": first.isoformat(),
            "utc_end_exclusive": last.isoformat(),
            **result,
        }
        print(venue, label, len(histories), "markets", flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("venue", choices=("hyperliquid", "binance"))
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--capital", nargs="+", type=float, default=[500])
    args = parser.parse_args(argv)
    if any(not isfinite(capital) or capital <= 0 for capital in args.capital):
        parser.error("Capital must be positive and finite")
    write_json(args.output, study(args.venue, args.archive, args.capital))


if __name__ == "__main__":
    main()
