"""Replay beta-hedged Hyperliquid funding spreads using closed observations."""

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from itertools import product
from math import log, sqrt
from pathlib import Path
from statistics import correlation, covariance, fmean, median, stdev, variance

from .broad_study import benchmark, run_book, usable_histories
from .liquid_trend_leverage import WINDOWS
from .meme_study import hour_date, qualification, write_json
from .research_data import ARCHIVE_END_HOUR, load_archive
from .research_execution import STRESS, RiskLimits
from .research_models import DAY_HOURS, HOUR_MS, OrderPlan, TradePlan, excluded_symbol

HOLD_DAYS = (3, 7)
CAPITAL = 500
TURNOVER_MINIMUM = 1_000_000
RETURN_DAYS = 30
FACTOR_MINIMUM_MARKETS = 8
MIN_CORRELATION = 0.75
MIN_BETA, MAX_BETA = 0.25, 3
MAX_BETA_RATIO = 2
MIN_NET_CARRY_BPS = 10
IOC_CAP_BPS = 40
STOP_VOLATILITY = 2
MAX_PAIRS_PER_DECISION = 2
STRESS_ROUND_TRIP_BPS = (
    2 * STRESS.half_spread_bps
    + STRESS.entry_impact_bps
    + STRESS.exit_impact_bps
    + 2 * STRESS.taker_fee_bps
)


@dataclass(frozen=True, slots=True)
class Market:
    coin: str
    close: float
    returns: tuple[float, ...]
    daily_volatility: float
    funding: float | None


@dataclass(frozen=True, slots=True)
class Candidate:
    market: Market
    beta: float


@dataclass(frozen=True, slots=True)
class Pair:
    long: Candidate
    short: Candidate
    long_weight: float
    correlation: float
    net_carry_bps: float
    score: float


def funding_forecast(history, hour):
    """Use the weaker of two same-sign, closed funding windows."""
    settlements = [history.funding.get(stamp) for stamp in range(hour - 72, hour)]
    if any(row is None or row.time_ms >= hour * HOUR_MS for row in settlements):
        return None
    recent = [row.rate for row in settlements[-24:]]
    older = [row.rate for row in settlements[:-24]]
    recent_mean, older_mean = fmean(recent), fmean(older)
    if recent_mean * older_mean <= 0:
        return None
    side = 1 if recent_mean > 0 else -1
    if any(
        sum(side * rate > 0 for rate in window) * 5 < 3 * len(window) for window in (recent, older)
    ):
        return None
    return side * min(abs(recent_mean), abs(older_mean))


def market_inputs(history, hour):
    daily = [history.bars.get(stamp) for stamp in range(hour - DAY_HOURS, hour)]
    if any(bar is None for bar in daily) or daily[-1].volume <= 0:
        return None
    active = [bar.close * bar.volume for bar in daily if bar.volume > 0]
    if len(active) < 9 or sum(active) < TURNOVER_MINIMUM or median(active) < TURNOVER_MINIMUM / 72:
        return None
    endpoints = [
        history.bars.get(stamp)
        for stamp in range(hour - RETURN_DAYS * DAY_HOURS - 1, hour, DAY_HOURS)
    ]
    if any(row is None or row.close <= 0 for row in endpoints):
        return None
    returns = tuple(
        log(endpoints[index + 1].close / endpoints[index].close) for index in range(RETURN_DAYS)
    )
    volatility = stdev(returns)
    if volatility <= 1e-8:
        return None
    return Market(
        history.coin, daily[-1].close, returns, volatility, funding_forecast(history, hour)
    )


def market_candidates(markets):
    if len(markets) < FACTOR_MINIMUM_MARKETS:
        return []
    factor = tuple(fmean(values) for values in zip(*(m.returns for m in markets), strict=True))
    factor_variance = variance(factor)
    if factor_variance <= 1e-12:
        return []
    candidates = []
    for market in markets:
        if market.funding is None:
            continue
        beta = covariance(market.returns, factor) / factor_variance
        if MIN_BETA <= beta <= MAX_BETA:
            candidates.append(Candidate(market, beta))
    return candidates


def matched_pairs(candidates, days, counts, *, allow_paying_leg=False):
    pairs = []
    for long, short in product(candidates, repeat=2):
        if long.market.funding >= short.market.funding:
            continue
        if not allow_paying_leg and (long.market.funding >= 0 or short.market.funding <= 0):
            continue
        counts["funding_direction_pairs"] += 1
        if max(long.beta, short.beta) > MAX_BETA_RATIO * min(long.beta, short.beta):
            continue
        long_returns, short_returns = long.market.returns, short.market.returns
        pair_correlation = correlation(long_returns, short_returns)
        if pair_correlation < MIN_CORRELATION:
            continue
        counts["correlation_and_beta_qualified"] += 1
        long_weight = short.beta / (short.beta + long.beta)
        short_weight = 1 - long_weight
        forecast = (
            days
            * DAY_HOURS
            * (-long_weight * long.market.funding + short_weight * short.market.funding)
        )
        net_bps = forecast * 10_000 - STRESS_ROUND_TRIP_BPS
        if net_bps < MIN_NET_CARRY_BPS:
            continue
        counts["projected_net_carry_qualified"] += 1
        paired_vol = sqrt(days) * stdev(
            long_weight * first - short_weight * second
            for first, second in zip(long_returns, short_returns, strict=True)
        )
        pairs.append(
            Pair(
                long,
                short,
                long_weight,
                pair_correlation,
                net_bps,
                net_bps / max(0.005, paired_vol),
            )
        )
    return sorted(
        pairs, key=lambda pair: (-pair.score, pair.long.market.coin, pair.short.market.coin)
    )


def pair_plan(pair, days):
    legs = []
    for candidate, side, weight in (
        (pair.long, 1, pair.long_weight),
        (pair.short, -1, 1 - pair.long_weight),
    ):
        market = candidate.market
        legs.append(
            OrderPlan(
                market.coin,
                side,
                market.close * (1 + side * IOC_CAP_BPS / 10_000),
                market.close * market.daily_volatility,
                STOP_VOLATILITY,
                None,
                days * DAY_HOURS,
                0,
                mode="ioc",
                weight=weight,
                signal_strength=pair.score,
            )
        )
    return TradePlan(tuple(legs), f"funding-pair-{days}d")


def build_plans(histories, start, end, offset, *, allow_paying_leg=False):
    if not 0 < start < end <= ARCHIVE_END_HOUR:
        raise ValueError("Use chronological Development windows")
    plans = {days: {} for days in HOLD_DAYS}
    signals = {days: {} for days in HOLD_DAYS}
    diagnostics = defaultdict(Counter)
    eligible = [
        row for row in histories.values() if not excluded_symbol(row.coin) and ":" not in row.coin
    ]
    for hour in range(start + (offset - start) % DAY_HOURS, end, DAY_HOURS):
        markets = [market for row in eligible if (market := market_inputs(row, hour)) is not None]
        candidates = market_candidates(markets)
        for days in HOLD_DAYS:
            if hour + days * DAY_HOURS >= end:
                continue
            counts = diagnostics[days]
            counts["calendar_decisions"] += 1
            counts["eligible_market_observations"] += len(markets)
            counts["qualified_market_beta_observations"] += len(candidates)
            pairs = matched_pairs(candidates, days, counts, allow_paying_leg=allow_paying_leg)
            used = set()
            for pair in pairs:
                coins = (pair.long.market.coin, pair.short.market.coin)
                if used.intersection(coins):
                    continue
                trades = plans[days].setdefault(hour, [])
                trades.append(pair_plan(pair, days))
                signals[days].setdefault(hour, {})[coins] = pair
                counts["submitted_pair_signals"] += 1
                used.update(coins)
                if len(trades) == MAX_PAIRS_PER_DECISION:
                    break
    return plans, signals, diagnostics


def robustness(summary, signals):
    exposures, notional_imbalances, model_carry, realized_carry, hold_hours = [], [], [], [], []
    paired_net, asset_net = defaultdict(float), defaultdict(float)
    trades = summary["trades"]
    for trade in trades:
        legs = trade["legs"]
        pair_key = tuple(leg["coin"] for leg in legs)
        pair = signals[trade["entered_ms"] // HOUR_MS][pair_key]
        notionals = [leg["size"] * leg["entry"] for leg in legs]
        total_notional = sum(notionals)
        exposures.append(
            abs(notionals[0] * pair.long.beta - notionals[1] * pair.short.beta) / total_notional
        )
        notional_imbalances.append(abs(notionals[0] - notionals[1]) / total_notional)
        model_carry.append(pair.net_carry_bps + STRESS_ROUND_TRIP_BPS)
        realized_carry.append(trade["funding"] * 10_000 / total_notional)
        hold_hours.append((trade["exited_ms"] - trade["entered_ms"]) / HOUR_MS)
        paired_net[pair_key] += trade["net"]
        for leg in legs:
            asset_net[leg["coin"]] += leg["net"]
    largest_asset = max(asset_net, key=asset_net.get, default=None)
    best_trade = max(trades, key=lambda trade: trade["net"], default=None)
    return {
        "unique_pairs": len(paired_net),
        "largest_contributing_asset": largest_asset,
        "net_excluding_best_trade_and_all_pairs_with_largest_asset": sum(
            trade["net"]
            for trade in trades
            if trade is not best_trade
            and all(leg["coin"] != largest_asset for leg in trade["legs"])
        ),
        "mean_abs_beta_exposure_per_gross_after_rounding": fmean(exposures) if exposures else None,
        "mean_abs_notional_imbalance": fmean(notional_imbalances) if exposures else None,
        "median_projected_full_hold_carry_bps": median(model_carry) if model_carry else None,
        "median_realized_carry_bps": median(realized_carry) if realized_carry else None,
        "median_hold_hours": median(hold_hours) if hold_hours else None,
        "net_by_pair": {"/".join(key): value for key, value in sorted(paired_net.items())},
    }


def evaluate(archive, offset, *, allow_paying_leg=False):
    limits = RiskLimits(initial_equity=CAPITAL, max_leg_fraction=0.25)
    report = {
        "scope": "Native Hyperliquid funding pairs, chronological Development only.",
        "execution_limits": (
            "Trade candles proxy opening fills and funding settlement price. Both IOC legs "
            "fill together in the replay or both are skipped. Exchange execution can leave "
            "an unhedged leg. Each position has an independent protective stop."
        ),
        "parameters": {
            "risk_limits": asdict(limits),
            "hold_days": HOLD_DAYS,
            "daily_decision_utc_offset": offset,
            "funding_confirmation_hours": (24, 48),
            "beta_return_days": RETURN_DAYS,
            "minimum_correlation": MIN_CORRELATION,
            "maximum_beta_ratio": MAX_BETA_RATIO,
            "minimum_24h_turnover": TURNOVER_MINIMUM,
            "stress_round_trip_bps_per_gross": STRESS_ROUND_TRIP_BPS,
            "minimum_expected_net_carry_bps_per_gross": MIN_NET_CARRY_BPS,
            "allow_one_paying_leg_for_positive_net_carry": allow_paying_leg,
            "individual_stop_daily_volatility": STOP_VOLATILITY,
            "ioc_cap_bps": IOC_CAP_BPS,
        },
        "periods": {},
    }
    for period, (start, end) in WINDOWS.items():
        available, coverage = load_archive(archive, end, include_quarters=True)
        histories, excluded = usable_histories(available, coverage)
        if len(histories) < FACTOR_MINIMUM_MARKETS:
            raise ValueError("Insufficient markets with archived candles and funding")
        plans, signals, diagnostics = build_plans(
            histories, start, end, offset, allow_paying_leg=allow_paying_leg
        )
        studies = {}
        for days in HOLD_DAYS:
            scores = run_book(histories, plans[days], start, end, limits, profit_lock=0.25)
            for outcome in scores.values():
                outcome["pair_diagnostics"] = robustness(outcome, signals[days])
            stress = scores["stress"]
            studies[f"{days}d"] = {
                **scores,
                "track_a": qualification("A", stress),
                "signals": dict(diagnostics[days]),
            }
            print(
                period,
                f"{days}d",
                f"pairs={stress['count']}",
                f"stress={stress['net']:.2f}",
                flush=True,
            )
        report["periods"][period] = {
            "start": hour_date(start),
            "end_exclusive": hour_date(end),
            "markets": sorted(histories),
            "excluded_markets": excluded,
            "flat_net": 0,
            "buy_hold": benchmark(histories, start, end, CAPITAL, TURNOVER_MINIMUM),
            "studies": studies,
        }
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--offset", type=int, choices=range(DAY_HOURS), default=0)
    parser.add_argument("--allow-paying-leg", action="store_true")
    args = parser.parse_args(argv)
    write_json(
        args.output, evaluate(args.archive, args.offset, allow_paying_leg=args.allow_paying_leg)
    )


if __name__ == "__main__":
    main()
