"""Test whether earlier public Binance chart history transfers to Hyperliquid."""

import argparse
from datetime import date
from pathlib import Path

import pandas as pd

from .directional_context import load_context
from .directional_features import build_frame
from .directional_study import (
    LOOKBACK_DAYS,
    basket_reference,
    direction_accuracy,
    fit_predict,
    folds,
    market_data,
    models,
    qualify,
    run_policy,
)
from .directional_transfer import load_transfer
from .meme_study import day_hour, hour_date, write_json
from .research_data import ARCHIVE_END_HOUR, load_archive

TRANSFER_START = date(2024, 1, 1)
TRANSFER_END = date(2026, 5, 25)
PAST = ("logistic", "boost", "return")


def prior_predictions(older, current, cutoff, horizon):
    returns = f"price_return_{horizon}"
    exits = f"exit_hour_{horizon}"
    eligible = older.loc[older[exits].lt(cutoff)].dropna(subset=[returns])
    data = pd.concat([eligible, current], ignore_index=True)
    features = market_data(data, "chart")
    predictions = models(
        data.iloc[: len(eligible)], data.iloc[len(eligible) :], features, horizon, gross_only=True
    )
    return pd.DataFrame(predictions, index=current.index), len(eligible)


def assess(frame, history, boundaries, older, cutoff, horizon):
    prior, samples = prior_predictions(older, frame, cutoff, horizon)
    chart = market_data(frame, "chart")
    official = market_data(frame, "context")
    configs = {"local_chart": [], "local_context": [], "older_venue": [], "blend": []}
    for fold in boundaries:
        chart_test, _ = fit_predict(frame, fold, horizon, chart)
        context_test, _ = fit_predict(frame, fold, horizon, official)
        venue = context_test.copy()
        blend = context_test.copy()
        for key in PAST:
            venue[key] = prior.loc[venue.index, key]
            blend[key] = (prior.loc[blend.index, key] + context_test[key]) / 2
        for mode, block in zip(configs, (chart_test, context_test, venue, blend), strict=True):
            configs[mode].append(block)

    outcomes = {}
    for mode, blocks in configs.items():
        outcomes[mode] = {}
        for period in ("discovery", "validation"):
            period_pairs = [
                (fold, block)
                for fold, block in zip(boundaries, blocks, strict=True)
                if fold.period == period
            ]
            period_folds, period_blocks = zip(*period_pairs, strict=True)
            combined = pd.concat(period_blocks)
            reference = basket_reference(
                frame, history, period_folds[0].start, period_folds[-1].end
            )
            outcomes[mode][period] = {
                "predictions": {
                    name: direction_accuracy(combined, combined[name], horizon)
                    for name in ("logistic", "boost")
                },
                "policies": {
                    name: {
                        str(phase): run_policy(
                            period_blocks,
                            period_folds,
                            history,
                            horizon,
                            name,
                            phase,
                            benchmark=reference,
                        )
                        for phase in (0, horizon // 2)
                    }
                    for name in ("boost", "logistic", "return")
                },
            }
    return outcomes, samples


def run(args):
    cutoff = day_hour(TRANSFER_END)
    validation = day_hour(date(2026, 9, 26))
    end = ARCHIVE_END_HOUR

    histories, _ = load_archive(args.archive, end, include_quarters=True)
    earlier = load_transfer(args.transfer, day_hour(TRANSFER_START), cutoff, histories)
    older = build_frame(earlier, day_hour(TRANSFER_START), cutoff)
    matches = sorted(set(older.coin))
    current = build_frame(histories, cutoff, end)
    current = current.loc[current.coin.isin(matches)].copy()
    context = pd.DataFrame.from_dict(load_context(args.context, cutoff, end), orient="index")
    current = current.join(context, on="hour")
    cuts = list(folds(cutoff, validation, end, LOOKBACK_DAYS))
    report = {
        "prior_venue": "Binance USD-M",
        "earliest_prior_day": TRANSFER_START.isoformat(),
        "prior_cutoff_exclusive": TRANSFER_END.isoformat(),
        "evaluation_venue": "Hyperliquid",
        "evaluation_cutoff_exclusive": hour_date(end),
        "matched_coins": matches,
        "eligible_prior_decisions": len(older),
        "eligible_local_decisions": len(current),
        "models": {},
        "prior_fit_rows": {},
        "qualification": [],
        "limitations": "Matched-symbol transfer experiment. Earlier venue uses gross chart labels "
        "and no funding. All decisions and costs are evaluated on Hyperliquid.",
    }
    for horizon in args.horizons:
        outcomes, count = assess(current, histories, cuts, older, cutoff, horizon)
        report["models"][str(horizon)] = outcomes
        report["prior_fit_rows"][str(horizon)] = count
        report["qualification"].extend(qualify(outcomes, horizon))
        print(
            f"{horizon}h: {count} earlier-venue labels across {len(matches)} matched coins",
            flush=True,
        )
    write_json(args.output, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("transfer", type=Path)
    parser.add_argument("context", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--horizons", nargs="+", type=int, choices=(12, 48), default=[12, 48])
    run(parser.parse_args())


if __name__ == "__main__":
    main()
