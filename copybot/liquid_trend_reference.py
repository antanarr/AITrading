"""Run fixed daily trend rules on the earlier Binance perpetual market archive."""

import argparse
from dataclasses import replace
from datetime import date
from math import isfinite
from pathlib import Path

from .binance_regime_archive import END, START, inspect_binance_regime
from .broad_study import run_book
from .liquid_trend import BASELINE_VARIANT, ENHANCED_VARIANT, build_liquid_trend
from .meme_study import COSTS, day_hour, write_json
from .public_strategies import build_plans
from .research_execution import RiskLimits

COST_MODELS = {name: replace(model, taker_fee_bps=5) for name, model in COSTS.items()}
PERIODS = (
    (START, date(2025, 1, 1)),
    (date(2025, 1, 1), date(2026, 1, 1)),
    (date(2026, 1, 1), END),
)


def period(histories, limits, first, last):
    start, end = day_hour(first), day_hour(last)
    candidates = build_plans(histories, start, end, liquidity_top_n=20)[BASELINE_VARIANT]
    filtered, close_signal, diagnostics = build_liquid_trend(histories, candidates, start, end)
    return {
        "start": first.isoformat(),
        "end_exclusive": last.isoformat(),
        "trend_diagnostics": diagnostics,
        BASELINE_VARIANT: run_book(
            histories, candidates, start, end, limits, profit_lock=0.25, cost_models=COST_MODELS
        ),
        ENHANCED_VARIANT: run_book(
            histories,
            filtered,
            start,
            end,
            limits,
            exit_signal=close_signal,
            profit_lock=0.25,
            cost_models=COST_MODELS,
        ),
    }


def evaluate(archive, equity):
    histories, coverage = inspect_binance_regime(archive)
    if not histories:
        raise ValueError("No complete earlier venue histories")
    limits = RiskLimits(initial_equity=equity, max_leg_fraction=0.25)
    periods = {str(first.year): period(histories, limits, first, last) for first, last in PERIODS}
    periods["continuous"] = period(histories, limits, START, END)
    return {
        "venue": "Binance USD-M perpetuals",
        "evaluation": periods,
        "input_markets": sorted(histories),
        "coverage": coverage,
        "assumptions": {
            "capital_resets_per_year_usdt": equity,
            "funding": "Actual archived settlements, preceding close as settlement price proxy.",
            "costs": "Assumed 5 bps taker fee per fill with the existing base and stress spread and impact model.",
            "execution": "Hourly candle execution proxies, including Hyperliquid-style price rounding and a ten-dollar minimum. This does not validate Binance order filters or liquidation behavior.",
            "universe": "Up to 20 markets selected from the available earlier major contracts. Archive selection may omit delistings.",
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--capital", type=float, default=500)
    args = parser.parse_args(argv)
    if not isfinite(args.capital) or args.capital <= 0:
        parser.error("Starting equity must be positive and finite")
    results = evaluate(args.archive, args.capital)
    write_json(args.output, results)
    for year, result in results["evaluation"].items():
        for name in (BASELINE_VARIANT, ENHANCED_VARIANT):
            score = result[name]["stress"]
            print(year, name, round(score["net"], 2), score["count"], flush=True)


if __name__ == "__main__":
    main()
