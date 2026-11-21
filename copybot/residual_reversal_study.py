"""Offline multiday altcoin reversal after market-adjusted price shocks."""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from itertools import pairwise, product
from math import log, sqrt
from pathlib import Path
from statistics import mean, median, stdev

from .broad_study import benchmark, run_book, usable_histories
from .meme_study import day_hour, qualification, write_json
from .research_data import ARCHIVE_END_HOUR, load_archive
from .research_execution import RiskLimits
from .research_models import DAY_HOURS, HOUR_MS, OrderPlan, TradePlan, excluded_symbol

START = date(2026, 5, 25)
VALIDATION = date(2026, 9, 26)
END = date(2026, 11, 21)
FIT_DAYS = 30
SHOCK_DAYS = 3
HOLD_HOURS = SHOCK_DAYS * DAY_HOURS
CADENCE_DAYS = 2
TURNOVER_MINIMUM = 1_000_000
TURNOVER_SPIKE = 1.5
MIN_FACTOR_MARKETS = 8
MIN_GAP_SIGMA = 1.5
BETA_MIN = 0.20
BETA_MAX = 3.0
BETA_RATIO_MAX = 3.0
ATR_DAYS = 14
STOP_ATR = 1.5
LIMIT_BPS = 15
SOURCES = (
    "https://www.nber.org/papers/w30917",
    "https://www.nber.org/papers/w8312",
)


@dataclass(frozen=True, slots=True)
class Daily:
    close: float
    high: float
    low: float
    turnover: float
    active_hours: int


@dataclass(frozen=True, slots=True)
class Candidate:
    coin: str
    z_score: float
    beta: float
    turnover_ratio: float
    reference_price: float
    risk_atr: float


def daily_series(history):
    buckets = defaultdict(list)
    for hour, bar in sorted(history.bars.items()):
        buckets[hour // DAY_HOURS].append(bar)
    return {
        day: Daily(
            rows[-1].close,
            max(bar.high for bar in rows),
            min(bar.low for bar in rows),
            sum(bar.close * bar.volume for bar in rows),
            sum(bar.volume > 0 for bar in rows),
        )
        for day, rows in buckets.items()
        if len(rows) == DAY_HOURS
    }


def liquid(row):
    return row is not None and row.active_hours >= 9 and row.turnover >= TURNOVER_MINIMUM


def daily_returns(rows):
    return {
        day: log(row.close / rows[day - 1].close)
        for day, row in rows.items()
        if day - 1 in rows and row.active_hours > 0 and rows[day - 1].active_hours > 0
    }


def factor_returns(series, asset_returns):
    constituents = defaultdict(dict)
    for coin, returns in asset_returns.items():
        for day, ret in returns.items():
            if liquid(series[coin].get(day - 1)):
                constituents[day][coin] = ret
    return {day: (sum(values.values()), values) for day, values in constituents.items()}


def factor_for(coin, summed, values):
    count = len(values) - (coin in values)
    return (summed - values.get(coin, 0.0)) / count if count >= MIN_FACTOR_MARKETS else None


def fit_residuals(prices, factors):
    xm, ym = mean(factors), mean(prices)
    factor_variation = sum((value - xm) ** 2 for value in factors)
    if factor_variation <= 1e-12:
        return None
    beta = sum((x - xm) * (y - ym) for x, y in zip(factors, prices, strict=True)) / factor_variation
    if not BETA_MIN <= beta <= BETA_MAX:
        return None
    alpha = ym - beta * xm
    residual_scale = stdev(y - alpha - beta * x for x, y in zip(factors, prices, strict=True))
    return (alpha, beta, residual_scale) if residual_scale > 1e-8 else None


def signal(coin, decision, series, returns, factor):
    if not liquid(series.get(decision - 1)):
        return None
    days = list(range(decision - FIT_DAYS - SHOCK_DAYS, decision))
    if any(day not in returns or day not in factor for day in days):
        return None
    observed = [returns[day] for day in days]
    market = [factor_for(coin, *factor[day]) for day in days]
    if any(value is None for value in market):
        return None
    coefficients = fit_residuals(observed[:FIT_DAYS], market[:FIT_DAYS])
    if coefficients is None:
        return None
    alpha, beta, residual_scale = coefficients
    residual = sum(
        y - alpha - beta * x for x, y in zip(market[FIT_DAYS:], observed[FIT_DAYS:], strict=True)
    )
    shock_turnover = mean(series[day].turnover for day in days[FIT_DAYS:])
    normal_turnover = median(series[day].turnover for day in days[:FIT_DAYS])
    if normal_turnover <= 0:
        return None
    atr_rows = [series[day] for day in range(decision - ATR_DAYS - 1, decision)]
    atr = mean(
        max(bar.high - bar.low, abs(bar.high - prior.close), abs(bar.low - prior.close))
        for prior, bar in pairwise(atr_rows)
    )
    if atr <= 0:
        return None
    return Candidate(
        coin,
        residual / (sqrt(SHOCK_DAYS) * residual_scale),
        beta,
        shock_turnover / normal_turnover,
        series[decision - 1].close,
        atr,
    )


def choose(candidates, volume_gate):
    long = [row for row in candidates if row.z_score <= -MIN_GAP_SIGMA]
    short = [row for row in candidates if row.z_score >= MIN_GAP_SIGMA]
    best = None
    for loser, winner in product(long, short):
        if max(loser.beta, winner.beta) > BETA_RATIO_MAX * min(loser.beta, winner.beta):
            continue
        if volume_gate and min(loser.turnover_ratio, winner.turnover_ratio) < TURNOVER_SPIKE:
            continue
        if best is None or winner.z_score - loser.z_score > best[1].z_score - best[0].z_score:
            best = loser, winner
    return best


def order_pair(pair, label):
    loser, winner = pair
    total = loser.beta + winner.beta
    weights = (winner.beta / total, loser.beta / total)
    legs = tuple(
        OrderPlan(
            row.coin,
            side,
            row.reference_price * (1 + side * LIMIT_BPS / 10_000),
            row.risk_atr,
            STOP_ATR,
            None,
            HOLD_HOURS,
            0,
            mode="ioc",
            weight=weight,
            signal_strength=abs(row.z_score),
        )
        for row, side, weight in zip(pair, (1, -1), weights, strict=True)
    )
    return TradePlan(legs, label)


def build_plans(histories, start, end):
    series = {
        coin: daily_series(rows)
        for coin, rows in sorted(histories.items())
        if not excluded_symbol(coin)
    }
    returns = {coin: daily_returns(rows) for coin, rows in series.items()}
    factor = factor_returns(series, returns)
    plans = defaultdict(dict)
    diagnostics = defaultdict(Counter)
    snapshots = defaultdict(dict)
    for day in range(day_hour(start) // DAY_HOURS, day_hour(end) // DAY_HOURS):
        hour = day * DAY_HOURS
        if hour + HOLD_HOURS >= day_hour(end):
            break
        phase = (day - day_hour(START) // DAY_HOURS) % CADENCE_DAYS
        candidates = tuple(
            row
            for coin, rows in series.items()
            if (row := signal(coin, day, rows, returns[coin], factor)) is not None
        )
        for gate in (False, True):
            mode = "turnover-shock" if gate else "residual"
            key = f"{mode}-clock-{phase}"
            counters = diagnostics[key]
            counters["calendar_anchors"] += 1
            counters["eligible_asset_observations"] += len(candidates)
            pair = choose(candidates, gate)
            if pair is None:
                counters["no_opposing_shock_pair"] += 1
                continue
            counters["pairs_submitted"] += 1
            plans[key][hour] = [order_pair(pair, key)]
            snapshots[key][hour] = {
                row.coin: {
                    "z_score": row.z_score,
                    "market_beta": row.beta,
                    "turnover_ratio": row.turnover_ratio,
                }
                for row in pair
            }
    return plans, {key: dict(value) for key, value in diagnostics.items()}, snapshots


def robustness(metrics, snapshots):
    by_coin = defaultdict(float)
    betas = []
    for trade in metrics["trades"]:
        entry = trade["entered_ms"] // HOUR_MS
        if entry in snapshots and len(trade["legs"]) == 2:
            weights = [leg["size"] * leg["entry"] for leg in trade["legs"]]
            exposure = sum(
                leg["side"] * notional * snapshots[entry][leg["coin"]]["market_beta"]
                for leg, notional in zip(trade["legs"], weights, strict=True)
            )
            betas.append(abs(exposure) / sum(weights))
        for leg in trade["legs"]:
            by_coin[leg["coin"]] += leg["net"]
    best_coin = max(by_coin, key=by_coin.get, default=None)
    best_pair = max(metrics["trades"], key=lambda row: row["net"], default=None)
    paired_exclusion = sum(
        trade["net"]
        for trade in metrics["trades"]
        if trade is not best_pair and all(leg["coin"] != best_coin for leg in trade["legs"])
    )
    return {
        "largest_contributing_asset": best_coin,
        "net_after_dropping_best_pair_and_all_baskets_with_best_asset": paired_exclusion,
        "mean_abs_target_beta_exposure_per_gross_notional_after_rounding": mean(betas)
        if betas
        else None,
    }


def run(archive, start, validation, end):
    if not START <= start < validation < end <= END or day_hour(end) > ARCHIVE_END_HOUR:
        raise ValueError("Stay within the Development archive, exclusive of Reserve")
    available, coverage = load_archive(archive, day_hour(end), include_quarters=True)
    histories, excluded = usable_histories(available, coverage)
    if len(histories) < MIN_FACTOR_MARKETS + 1:
        raise ValueError("Need sufficient complete native funding and price histories")
    results = {}
    for label, (first, last) in {
        "discovery": (start, validation),
        "validation": (validation, end),
    }.items():
        plans, diagnostics, signals = build_plans(histories, first, last)
        case = results[label] = {
            "signal_diagnostics": diagnostics,
            "signals": signals,
            "by_equity": {},
        }
        for equity in (300, 500):
            risk_limits = RiskLimits(initial_equity=equity, max_leg_fraction=0.25)
            screen = case["by_equity"][str(equity)] = {
                "stay_flat": 0,
                "buy_hold": benchmark(
                    histories, day_hour(first), day_hour(last), equity, TURNOVER_MINIMUM
                ),
                "runs": {},
            }
            for name in diagnostics:
                values = run_book(
                    histories, plans.get(name, {}), day_hour(first), day_hour(last), risk_limits
                )
                for metrics in values.values():
                    metrics["robustness"] = robustness(metrics, signals.get(name, {}))
                values["qualification"] = qualification("A", values["stress"])
                screen["runs"][name] = values
    return {
        "description": "Fade 3-day asset-specific shocks after hedging pre-estimated sensitivity to a BTC-free native market factor. Inspired by equity liquidity provision reversal research.",
        "interval": {
            "discovery_start": start.isoformat(),
            "validation_start": validation.isoformat(),
            "end_exclusive": end.isoformat(),
        },
        "sources": SOURCES,
        "parameters": {
            "factor_liquidity_filter_previous_daily_turnover": TURNOVER_MINIMUM,
            "min_market_factor_peers_excluding_self": MIN_FACTOR_MARKETS,
            "regression_fit_days_before_shock": FIT_DAYS,
            "residual_shock_days": SHOCK_DAYS,
            "minimum_z_score_magnitude_both_legs": MIN_GAP_SIGMA,
            "optional_same_asset_turnover_multiplier_both_legs": TURNOVER_SPIKE,
            "beta_bounds": [BETA_MIN, BETA_MAX],
            "max_leg_beta_ratio": BETA_RATIO_MAX,
            "notional_weights": "Each leg's target weight is proportional to its opposing leg's positive trailing market beta",
            "cadence_days": CADENCE_DAYS,
            "clock_offsets_days": [0, 1],
            "stop_atr_multiple": STOP_ATR,
            "hold_hours": HOLD_HOURS,
            "max_leg_equity_fraction": 0.25,
            "risk_fraction": 0.01,
            "starting_equity": [300, 500],
            "ioc_reference_cap_bps": LIMIT_BPS,
            "limitations": "OHLCV prices proxy intrabar executions, historical Oracle funding marks are unavailable, beta estimates can drift and do not guarantee market neutrality, two legs assume available IOC execution",
        },
        "usable_markets": sorted(histories),
        "excluded_incomplete_markets": excluded,
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--start", type=date.fromisoformat, default=START)
    parser.add_argument("--validation", type=date.fromisoformat, default=VALIDATION)
    parser.add_argument("--end", type=date.fromisoformat, default=END)
    args = parser.parse_args()
    write_json(args.output, run(args.archive, args.start, args.validation, args.end))


if __name__ == "__main__":
    main()
