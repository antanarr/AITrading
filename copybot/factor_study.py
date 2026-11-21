"""Offline historical replay of fixed cross-sectional factor portfolios."""

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise
from math import ceil, log, sqrt
from pathlib import Path
from statistics import fmean, median, stdev

from .meme_study import day_hour, hour_date, write_json
from .research_data import ARCHIVE_END_HOUR, load_archive
from .research_execution import BASE, MIN_NOTIONAL, PER_BPS, STRESS
from .research_models import DAY_HOURS, HOUR_MS, excluded_symbol

STARTING_EQUITY = 1_000.0
VOL_HOURS = 168
FACTOR_NAMES = ("funding-24h", "momentum-168h", "reversal-24h")
CADENCES = (24, 72)
SIDES = (1, -1)
LEGS_PER_SIDE = 3
WEIGHT_DRIFT_BUFFER = Decimal("0.25")
MINIMUM_TURNOVER = 1_000_000


@dataclass(frozen=True)
class _Features:
    vol: float
    scores: dict[str, float]


def _features(history, hour, minimum_turnover):
    latest = history.bars.get(hour - 1)
    if latest is None or latest.volume <= 0:
        return None
    rows = [history.bars.get(stamp) for stamp in range(hour - VOL_HOURS - 1, hour)]
    if any(row is None for row in rows):
        return None
    active_turnover = [row.close * row.volume for row in rows[-24:] if row.volume > 0]
    if (
        len(active_turnover) < 9
        or sum(active_turnover) < minimum_turnover
        or median(active_turnover) < minimum_turnover / (24 * 3)
    ):
        return None
    returns = [log(current.close / prior.close) for prior, current in pairwise(rows)]
    risk = sqrt(sum(value * value for value in returns))
    risk24 = sqrt(sum(value * value for value in returns[-24:]))
    if risk <= 0:
        return None
    scores = {"momentum-168h": sum(returns) / risk}
    if risk24 > 0:
        scores["reversal-24h"] = -sum(returns[-24:]) / risk24
    left, right = (hour - 24) * HOUR_MS, hour * HOUR_MS
    settlements = [history.funding.get(stamp) for stamp in range(hour - 24, hour)]
    if all(row is not None and left <= row.time_ms < right for row in settlements):
        scores["funding-24h"] = -fmean(row.rate for row in settlements)
    return _Features(risk, scores)


def _weights(features, factor, held, legs_per_side=LEGS_PER_SIDE, weighting="inverse-vol"):
    ranking = sorted(
        (coin for coin, data in features.items() if factor in data.scores),
        key=lambda coin: (-features[coin].scores[factor], coin),
    )
    retention_rank = 2 * legs_per_side
    if len(ranking) < retention_rank:
        return {}, len(ranking)
    preferences = {1: ranking, -1: list(reversed(ranking))}
    selected = {
        side: [coin for coin in preferences[side][:retention_rank] if held.get(coin, 0) * side > 0]
        for side in SIDES
    }
    for side in SIDES:
        for coin in preferences[side]:
            if len(selected[side]) >= legs_per_side:
                break
            if coin not in selected[side] and coin not in selected[-side]:
                selected[side].append(coin)
    result = {}
    for side in SIDES:
        raw = {
            coin: 1.0 if weighting == "equal" else 1 / features[coin].vol for coin in selected[side]
        }
        total = sum(raw.values())
        result.update({coin: side * 0.5 * weight / total for coin, weight in raw.items()})
    return result, len(ranking)


def _decimal(value):
    return Decimal(str(value))


class _MissingData(Exception):
    pass


class _Portfolio:
    def __init__(
        self,
        histories,
        costs,
        starting_equity,
        *,
        legs_per_side=LEGS_PER_SIDE,
        weighting="inverse-vol",
        neutrality_band=None,
    ):
        self.histories = histories
        self.costs = costs
        self.starting_equity = starting_equity
        self.legs_per_side = legs_per_side
        self.weighting = weighting
        self.neutrality_band = neutrality_band
        self.positions = {}
        self.prices = {}
        self.lots = {
            coin: Decimal(1).scaleb(-history.decimals) for coin, history in histories.items()
        }
        self.components = Counter()
        self.by_coin = Counter()
        self.by_sleeve = {"long": Counter(), "short": Counter()}
        self.activity = Counter()
        self.rebalances = []
        self.days = []
        self.terminal_positions = {}
        self.day_turnover = 0.0

    @property
    def net(self):
        return sum(self.components[key] for key in ("gross_price", "funding")) - sum(
            self.components[key] for key in ("fees", "slippage")
        )

    @property
    def equity(self):
        return self.starting_equity + self.net

    def mark(self, coin, price):
        if coin in self.positions:
            size = self.positions[coin] * float(self.lots[coin])
            change = size * (price - self.prices[coin])
            self.components["gross_price"] += change
            self.by_sleeve["long" if size > 0 else "short"]["gross_price"] += change
            self.by_coin[coin] += change
            self.prices[coin] = price

    def settlement(self, coin, hour, boundary):
        row = self.histories[coin].funding.get(hour)
        if row is None or row.time_ms // HOUR_MS != hour:
            raise _MissingData(f"missing funding for {coin} at epoch hour {hour}")
        if (row.time_ms == hour * HOUR_MS) != boundary:
            return
        prior = self.histories[coin].bars.get(hour - 1)
        if prior is None:
            raise _MissingData(f"missing prior close for {coin} funding at epoch hour {hour}")
        if prior.volume <= 0:
            self.activity["stale_funding_proxies"] += 1
        size = self.positions[coin] * float(self.lots[coin])
        cash = -size * row.rate * prior.close
        self.components["funding"] += cash
        self.by_sleeve["long" if size > 0 else "short"]["funding"] += cash
        self.components["funding_received" if cash >= 0 else "funding_paid"] += abs(cash)
        self.by_coin[coin] += cash

    def fill_price(self, reference, side, *, exiting=False):
        impact = self.costs.exit_impact_bps if exiting else self.costs.entry_impact_bps
        return _decimal(reference) * (
            1 + side * (_decimal(self.costs.half_spread_bps) + _decimal(impact)) / PER_BPS
        )

    def execution_price(self, coin, target, reference):
        prior = self.positions.get(coin, 0)
        delta = target - prior
        side = 1 if delta > 0 else -1
        # A reversal has one net order, with separate cost assumptions for its two tranches.
        exit_lots = min(abs(prior), abs(delta)) if prior * delta < 0 else 0
        entry_lots = abs(delta) - exit_lots
        return (
            exit_lots * self.fill_price(reference, side, exiting=True)
            + entry_lots * self.fill_price(reference, side)
        ) / abs(delta)

    def trade(self, coin, target, reference):
        prior = self.positions.get(coin, 0)
        delta = target - prior
        quantity = abs(delta) * float(self.lots[coin])
        fill = float(self.execution_price(coin, target, reference))
        turnover = quantity * fill
        fees = turnover * self.costs.taker_fee_bps / PER_BPS
        slippage = quantity * abs(fill - reference)
        self.components["fees"] += fees
        self.components["slippage"] += slippage
        self.by_coin[coin] -= fees + slippage
        self.day_turnover += turnover
        self.activity["taker_orders"] += 1
        if target:
            self.positions[coin] = target
            self.prices[coin] = reference
        else:
            del self.positions[coin]
            del self.prices[coin]

    def target_lots(self, coin, weight, price):
        side = 1 if weight > 0 else -1
        target_weight = _decimal(abs(weight))
        lot = self.lots[coin]
        reference_lot = lot * _decimal(price)
        minimum_lots = ceil(MIN_NOTIONAL / (lot * self.fill_price(price, side)))
        required_equity = minimum_lots * reference_lot / target_weight
        lots = int(_decimal(self.equity) * target_weight / reference_lot)
        return side * lots, minimum_lots, float(required_equity)

    def rebalance(self, factor, features, hour):
        weights, candidates = _weights(
            features, factor, self.positions, self.legs_per_side, self.weighting
        )
        event = {"hour": hour, "eligible_markets": candidates, "target_weights": weights}
        self.rebalances.append(event)
        bars = {
            coin: self.histories[coin].bars.get(hour)
            for coin in self.positions.keys() | weights.keys()
        }
        unavailable = sorted(coin for coin, bar in bars.items() if bar is None or bar.volume <= 0)
        if unavailable:
            event.update(status="unavailable_open", unavailable_markets=unavailable)
            return
        for coin, bar in bars.items():
            self.mark(coin, bar.open)
        if self.equity <= 0:
            raise _MissingData(f"portfolio equity exhausted at epoch hour {hour}")
        targets = {}
        risk_weighted_targets = {}
        thresholds = {}
        under_minimum = []
        buffered = []
        for coin, weight in weights.items():
            reference = bars[coin].open
            target, minimum, threshold = self.target_lots(coin, weight, reference)
            targets[coin] = target
            risk_weighted_targets[coin] = target
            thresholds[coin] = threshold
            if abs(target) < minimum:
                under_minimum.append(coin)
            prior = self.positions.get(coin, 0)
            if prior * weight > 0:
                target_notional = _decimal(self.equity) * _decimal(abs(weight))
                current_notional = abs(prior) * self.lots[coin] * _decimal(reference)
                if abs(current_notional - target_notional) <= WEIGHT_DRIFT_BUFFER * target_notional:
                    targets[coin] = prior
                    buffered.append(coin)
        if thresholds:
            event["minimum_equity_by_coin"] = thresholds
            event["minimum_equity"] = max(thresholds.values())
            event["starting_equity_on_observed_path"] = (
                event["minimum_equity"] * self.starting_equity / self.equity
            )
        below_minimum = sorted(set(under_minimum) - set(buffered))
        if below_minimum:
            self.record_blocked(event, "target_below_minimum", below_minimum)
            return
        deltas, skipped, blocked = self.order_deltas(targets, bars)
        if blocked:
            self.record_blocked(event, "rotation_below_minimum", [blocked])
            return
        if weights and self.neutrality_band is not None:
            deltas, skipped, buffered = self.neutrality_orders(
                event, bars, deltas, skipped, buffered, risk_weighted_targets, under_minimum
            )
        # Vacate slots before opening replacements when the portfolio is full.
        for coin, target in sorted(deltas.items(), key=lambda order: bool(order[1])):
            self.trade(coin, target, bars[coin].open)
        event.update(
            status="rebalanced" if weights else "insufficient_universe",
            orders=len(deltas),
            buffered_incumbents=sorted(buffered),
            skipped_small_deltas=skipped,
            **self.exposure(),
        )

    def record_blocked(self, event, status, markets):
        event.update(status=status, blocked_markets=markets, **self.exposure())
        if (
            self.neutrality_band is not None
            and abs(event["net_imbalance_pct_gross"]) > 100 * self.neutrality_band
        ):
            event.update(neutrality_correction=status, neutrality_blocked_markets=markets)

    def order_deltas(self, targets, bars):
        deltas = {}
        skipped = []
        for coin in sorted(bars):
            prior, target = self.positions.get(coin, 0), targets.get(coin, 0)
            delta = target - prior
            if not delta:
                continue
            fill = self.execution_price(coin, target, bars[coin].open)
            if abs(delta) * self.lots[coin] * fill < MIN_NOTIONAL:
                if prior * target <= 0:
                    return deltas, skipped, coin
                skipped.append(coin)
                continue
            deltas[coin] = target
        return deltas, skipped, None

    def neutrality_orders(
        self, event, bars, deltas, skipped, buffered, full_targets, under_minimum
    ):
        prices = {coin: bar.open for coin, bar in bars.items()}
        projected = self.exposure(self.positions | deltas, prices)["net_imbalance_pct_gross"]
        event["buffered_projected_imbalance_pct_gross"] = projected
        band_pct = 100 * self.neutrality_band
        if abs(projected) <= band_pct:
            return deltas, skipped, buffered
        if under_minimum:
            event.update(
                neutrality_correction="target_below_minimum",
                neutrality_blocked_markets=sorted(under_minimum),
            )
            return deltas, skipped, buffered
        corrected, skipped_corrected, blocked = self.order_deltas(full_targets, bars)
        if blocked:
            event.update(
                neutrality_correction="rotation_below_minimum", neutrality_blocked_markets=[blocked]
            )
            return deltas, skipped, buffered
        residual = self.exposure(self.positions | corrected, prices)["net_imbalance_pct_gross"]
        event["full_rebalance_projected_imbalance_pct_gross"] = residual
        event["neutrality_blocked_markets"] = skipped_corrected
        event["neutrality_full_rebalance_used"] = abs(residual) <= band_pct or abs(residual) < abs(
            projected
        )
        if abs(residual) <= band_pct:
            event["neutrality_correction"] = "within_band"
            return corrected, skipped_corrected, []
        event["neutrality_correction"] = (
            "delta_below_minimum" if skipped_corrected else "lot_residual"
        )
        if abs(residual) < abs(projected):
            return corrected, skipped_corrected, []
        return deltas, skipped, buffered

    def exposure(self, positions=None, prices=None):
        positions = self.positions if positions is None else positions
        prices = self.prices if prices is None else prices
        exposures = [
            lots * float(self.lots[coin]) * prices[coin] for coin, lots in positions.items()
        ]
        long = sum(value for value in exposures if value > 0)
        short = -sum(value for value in exposures if value < 0)
        return {
            "long_notional": long,
            "short_notional": short,
            "net_imbalance": long - short,
            "net_imbalance_pct_gross": 100 * (long - short) / (long + short)
            if long + short
            else 0.0,
        }

    def close_partition(self, hour):
        for coin, lots in sorted(self.positions.items()):
            bar = self.histories[coin].bars[hour]
            if bar.volume <= 0:
                self.terminal_positions[coin] = "zero_volume"
                continue
            fill = self.fill_price(bar.close, -1 if lots > 0 else 1, exiting=True)
            if abs(lots) * self.lots[coin] * fill < MIN_NOTIONAL:
                self.terminal_positions[coin] = "below_minimum"
                continue
            self.trade(coin, 0, bar.close)

    def finish_day(self, hour):
        prior = self.days[-1]["equity"] if self.days else self.starting_equity
        self.days.append(
            {
                "date": hour_date(hour),
                "equity": self.equity,
                "return": self.equity / prior - 1,
                "turnover_pct": 100 * self.day_turnover / prior,
            }
        )
        self.day_turnover = 0.0

    def hour(self, hour, end, factor, features):
        for coin in self.positions:
            if hour not in self.histories[coin].bars:
                history = self.histories[coin]
                issue = (
                    "archived candle series ended"
                    if hour > max(history.bars)
                    else "missing held-market candle"
                )
                raise _MissingData(f"{issue} for {coin} at epoch hour {hour}")
            self.settlement(coin, hour, boundary=True)
        if features is not None:
            self.rebalance(factor, features, hour)
        for coin in self.positions:
            self.settlement(coin, hour, boundary=False)
            bar = self.histories[coin].bars[hour]
            if bar.volume > 0:
                self.mark(coin, bar.close)
            else:
                self.activity["stale_market_hours"] += 1
        if hour == end - 1:
            self.close_partition(hour)
        if (hour + 1) % DAY_HOURS == 0:
            self.finish_day(hour)

    def summarize(self, status, incomplete_reason, stopped_hour):
        peak = self.starting_equity
        drawdown = drawdown_pct = 0.0
        for row in self.days:
            peak = max(peak, row["equity"])
            drawdown = max(drawdown, peak - row["equity"])
            drawdown_pct = max(drawdown_pct, 100 * (peak - row["equity"]) / peak)
        returns = [row["return"] for row in self.days]
        sigma = stdev(returns) if len(returns) >= 2 else 0
        best_coin = max(self.by_coin, key=self.by_coin.get, default=None)
        active = [event for event in self.rebalances if event.get("long_notional", 0) > 0]
        imbalance = [event["net_imbalance_pct_gross"] for event in active]
        band = [abs(value) for value in imbalance]
        return {
            "status": status,
            "incomplete_reason": incomplete_reason,
            "stopped_hour": stopped_hour,
            **{
                key: self.components[key]
                for key in (
                    "gross_price",
                    "funding",
                    "funding_received",
                    "funding_paid",
                    "fees",
                    "slippage",
                )
            },
            "maker_rebate": 0.0,
            "net": self.net,
            "return_pct": 100 * self.net / self.starting_equity,
            "best_coin": best_coin,
            "best_coin_net": self.by_coin.get(best_coin, 0.0),
            "net_ex_best_coin": self.net - self.by_coin.get(best_coin, 0.0),
            "by_coin": dict(sorted(self.by_coin.items())),
            "by_sleeve": {
                side: {key: values[key] for key in ("gross_price", "funding")}
                for side, values in self.by_sleeve.items()
            },
            "average_daily_turnover_pct": fmean(row["turnover_pct"] for row in self.days)
            if self.days
            else None,
            "daily_drawdown": drawdown,
            "daily_drawdown_pct": drawdown_pct,
            "annualized_sharpe": sqrt(365) * fmean(returns) / sigma if sigma else None,
            "minimum_historical_target_equity": max(
                (event["minimum_equity"] for event in self.rebalances if "minimum_equity" in event),
                default=None,
            ),
            "minimum_starting_equity_on_observed_path": max(
                (
                    event["starting_equity_on_observed_path"]
                    for event in self.rebalances
                    if "starting_equity_on_observed_path" in event
                ),
                default=None,
            ),
            "max_absolute_rebalance_imbalance_pct_gross": max(band, default=None),
            "mean_absolute_rebalance_imbalance_pct_gross": fmean(band) if band else None,
            "mean_signed_rebalance_imbalance_pct_gross": fmean(imbalance) if imbalance else None,
            "imbalance_observations": len(imbalance),
            "rebalances_over_5pct_imbalance": sum(value > 5 for value in band),
            "neutrality_corrections": dict(
                Counter(
                    event["neutrality_correction"]
                    for event in self.rebalances
                    if "neutrality_correction" in event
                )
            ),
            "rebalances_over_neutrality_band": sum(
                value > 100 * self.neutrality_band for value in band
            )
            if self.neutrality_band is not None
            else None,
            "rebalance_statuses": dict(
                Counter(event["status"] for event in self.rebalances if "status" in event)
            ),
            "activity": dict(self.activity),
            "terminal_unclosed": self.terminal_positions,
            "daily": self.days,
            "rebalances": self.rebalances,
        }


def replay_factor(
    histories,
    frames,
    factor,
    cadence,
    start,
    end,
    *,
    costs=BASE,
    equity=STARTING_EQUITY,
    legs_per_side=LEGS_PER_SIDE,
    weighting="inverse-vol",
    clock_offset_days=0,
    neutrality_band=None,
):
    portfolio = _Portfolio(
        histories,
        costs,
        equity,
        legs_per_side=legs_per_side,
        weighting=weighting,
        neutrality_band=neutrality_band,
    )
    anchor = start + (clock_offset_days * DAY_HOURS if cadence == 72 else 0)
    for hour in range(start, end):
        features = (
            frames.get(hour, {}) if hour >= anchor and (hour - anchor) % cadence == 0 else None
        )
        try:
            portfolio.hour(hour, end, factor, features)
        except _MissingData as exc:
            return portfolio.summarize("incomplete", str(exc), hour)
    status = "complete_with_marked_residuals" if portfolio.terminal_positions else "complete"
    return portfolio.summarize(status, None, None)


def study_period(
    histories,
    start,
    end,
    minimum_turnover,
    *,
    funding_72_only=False,
    legs_per_side=LEGS_PER_SIDE,
    weighting="inverse-vol",
    clock_offset_days=0,
    neutrality_band=None,
):
    combinations = (
        [("funding-24h", 72)]
        if funding_72_only
        else [(factor, cadence) for factor in FACTOR_NAMES for cadence in CADENCES]
    )
    frames = {
        hour: {
            coin: features
            for coin, history in histories.items()
            if (features := _features(history, hour, minimum_turnover)) is not None
        }
        for hour in (
            range(start + clock_offset_days * DAY_HOURS, end, 72)
            if funding_72_only
            else range(start, end, DAY_HOURS)
        )
    }
    rows = []
    for universe in ("all-non-btc", "memes-only"):
        chosen = {
            coin: history
            for coin, history in histories.items()
            if universe == "all-non-btc" or history.is_meme
        }
        cohort_frames = {
            hour: {coin: data for coin, data in row.items() if coin in chosen}
            for hour, row in frames.items()
        }
        for factor, cadence in combinations:
            rows.append(
                {
                    "factor": factor,
                    "universe": universe,
                    "rebalance_hours": cadence,
                    **{
                        name: replay_factor(
                            chosen,
                            cohort_frames,
                            factor,
                            cadence,
                            start,
                            end,
                            costs=costs,
                            legs_per_side=legs_per_side,
                            weighting=weighting,
                            clock_offset_days=clock_offset_days,
                            neutrality_band=neutrality_band,
                        )
                        for name, costs in (("base", BASE), ("stress", STRESS))
                    },
                }
            )
    return {"start": start, "end": end, "results": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 5, 25))
    parser.add_argument("--validation-start", type=date.fromisoformat, default=date(2026, 9, 26))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 11, 21))
    parser.add_argument("--minimum-turnover", type=float, default=MINIMUM_TURNOVER)
    parser.add_argument("--funding-72-only", action="store_true")
    parser.add_argument("--legs-per-side", type=int, choices=(2, 3, 4), default=LEGS_PER_SIDE)
    parser.add_argument("--weighting", choices=("inverse-vol", "equal"), default="inverse-vol")
    parser.add_argument("--clock-offset-days", type=int, choices=(0, 1, 2), default=0)
    parser.add_argument("--exclude-coin", action="append", default=[], metavar="SYMBOL")
    parser.add_argument(
        "--neutrality-band",
        type=float,
        help="maximum net imbalance as a fraction of gross, such as 0.05",
    )
    args = parser.parse_args()
    start, validation, end = map(day_hour, (args.start, args.validation_start, args.end))
    if not 0 < start < validation < end <= ARCHIVE_END_HOUR:
        parser.error("Require adjacent historical partitions ending by 2026-11-21 UTC")
    if args.minimum_turnover <= 0 or not args.minimum_turnover < float("inf"):
        parser.error("Require a finite positive minimum turnover")
    if args.neutrality_band is not None and not 0 < args.neutrality_band < 1:
        parser.error("Require a neutrality band between 0 and 1")
    histories, coverage = load_archive(args.archive, end)
    excluded = {coin.strip().upper() for coin in args.exclude_coin}
    unknown = sorted(excluded - {coin.upper() for coin in histories})
    if unknown:
        parser.error(f"Excluded symbols missing from archived markets: {', '.join(unknown)}")
    histories = {
        coin: history
        for coin, history in histories.items()
        if not excluded_symbol(coin) and coin.upper() not in excluded
    }
    if not histories or not any(
        start in history.bars and end - 1 in history.bars for history in histories.values()
    ):
        parser.error("No archived market spans the historical partitions")
    settings = {
        "funding_72_only": args.funding_72_only,
        "legs_per_side": args.legs_per_side,
        "weighting": args.weighting,
        "clock_offset_days": args.clock_offset_days,
        "neutrality_band": args.neutrality_band,
    }
    write_json(
        args.output,
        {
            "model": "exploratory_offline_factor_portfolio",
            "archive": str(args.archive.resolve()),
            "starting_equity": STARTING_EQUITY,
            "minimum_turnover": args.minimum_turnover,
            "minimum_order_notional": MIN_NOTIONAL,
            "incumbent_weight_drift_buffer": float(WEIGHT_DRIFT_BUFFER),
            "excluded_coins": sorted(excluded),
            **settings,
            "coverage": coverage,
            "discovery": study_period(
                histories, start, validation, args.minimum_turnover, **settings
            ),
            "validation": study_period(
                histories, validation, end, args.minimum_turnover, **settings
            ),
        },
    )


if __name__ == "__main__":
    main()
