"""Replay research orders using archived public candles and funding settlements."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from fractions import Fraction
from math import ceil
from statistics import mean

from .perps import round_price
from .research_models import HOUR_MS, MarketHistory, OrderPlan, TradePlan, excluded_symbol

QUARTER_MS = HOUR_MS // 4
PER_BPS = 10_000
MIN_NOTIONAL = 10
COMPARISON_EQUITIES = (47, 1_000)
RISK_LEVELS = {"1pct": Fraction(1, 100), "2pct": Fraction(2, 100)}
REJECTIONS = (
    "MIN_NOTIONAL",
    "budget",
    "slot_limit",
    "overlap",
    "post_only_cross",
    "entry_price_cap",
    "invalid_price",
    "data_gap",
    "zero_volume",
    "expired_on_submission",
)


@dataclass(frozen=True)
class CostModel:
    name: str
    half_spread_bps: float
    entry_impact_bps: float
    exit_impact_bps: float
    maker_fee_bps: float = 1.5
    taker_fee_bps: float = 4.5


BASE = CostModel("base", half_spread_bps=3, entry_impact_bps=2, exit_impact_bps=4)
STRESS = CostModel("stress", half_spread_bps=4, entry_impact_bps=4, exit_impact_bps=24)


@dataclass(frozen=True)
class RiskLimits:
    initial_equity: float = 47.0
    risk_fraction: float = 0.01
    max_positions: int = 4
    max_leg_fraction: float = 0.50
    max_gross_fraction: float = 1.0
    max_net_fraction: float = 0.60


@dataclass
class _Leg:
    plan: OrderPlan
    history: MarketHistory
    limit: float
    size: float
    entry: float
    entry_reference: float
    stop: float
    target: float | None
    entry_fee: float
    best_close: float
    last_observed: float
    entered_ms: int | None = None
    entered_slice: int | None = None
    canceled: bool = False
    result: dict | None = None

    @property
    def pending(self):
        return self.entered_ms is None and not self.canceled

    @property
    def open(self):
        return self.entered_ms is not None and self.result is None

    @property
    def notional(self):
        return _number(self.size) * _number(self.entry)

    def fill(self, stamp, slice_start):
        self.entered_ms = stamp
        self.entered_slice = slice_start


@dataclass
class _Bet:
    plan: TradePlan
    legs: list[_Leg]
    submitted_hour: int
    expires_after: int
    sizing: dict
    exit_next: str | None = None
    orphan: bool = False

    def stop_waiting(self):
        for leg in self.legs:
            if leg.pending:
                leg.canceled = True
                self.orphan = self.orphan or any(item.entered_ms is not None for item in self.legs)


def _price(price, decimals, rounding):
    return float(round_price(Decimal(str(price)), decimals, rounding))


def _number(value):
    return Fraction(str(value))


def _before_stop(side, price, stop):
    return (price - stop) * side <= 0


def _through_limit(side, price, limit):
    return (price - limit) * side < 0


def _market_exit(reference, side, costs):
    return reference * (1 + side * (costs.half_spread_bps + costs.exit_impact_bps) / PER_BPS)


def _prepared_leg(plan, history, bar, costs):
    side = plan.side
    if side not in (-1, 1) or plan.limit <= 0 or plan.atr <= 0 or plan.stop_atr <= 0:
        raise ValueError(f"Invalid order plan for {plan.coin}")
    if plan.weight <= 0 or plan.hold_hours < 1 or plan.ttl_hours < 0:
        raise ValueError(f"Invalid order duration or weight for {plan.coin}")
    limit = _price(plan.limit, history.decimals, ROUND_DOWN if side > 0 else ROUND_CEILING)
    if limit <= 0:
        return "invalid_price"
    if plan.mode == "maker":
        offset = Decimal(side) * Decimal(str(costs.half_spread_bps)) / PER_BPS
        executable = Decimal(str(bar.open)) * (1 + offset)
        if not _through_limit(side, Decimal(str(limit)), executable):
            return "post_only_cross"
        entry = reference = limit
        fee_bps = costs.maker_fee_bps
    elif plan.mode in ("ioc", "taker"):
        reference = bar.open
        bps = Decimal(str(costs.half_spread_bps)) + Decimal(str(costs.entry_impact_bps))
        executable = Decimal(str(reference)) * (1 + side * bps / PER_BPS)
        if (executable - Decimal(str(limit))) * side > 0:
            return "entry_price_cap"
        entry = float(executable)
        fee_bps = costs.taker_fee_bps
    else:
        raise ValueError(f"Unknown research order mode {plan.mode}")

    rounding = ROUND_CEILING if side > 0 else ROUND_DOWN
    entry_price = Decimal(str(entry))
    atr = Decimal(str(plan.atr))
    stop = _price(
        entry_price - side * atr * Decimal(str(plan.stop_atr)), history.decimals, rounding
    )
    target = plan.target_price
    if target is None and plan.target_atr is not None:
        target = entry_price + side * atr * Decimal(str(plan.target_atr))
    if target is not None:
        target = _price(target, history.decimals, rounding)
    if stop <= 0 or (entry - stop) * side <= 0:
        return "invalid_price"
    if target is not None and (target <= 0 or (target - entry) * side <= 0):
        return "invalid_price"
    return _Leg(
        plan,
        history,
        limit,
        0.0,
        entry,
        reference,
        stop,
        target,
        entry * fee_bps / PER_BPS,
        entry,
        reference,
    )


class _Sizing:
    def __init__(self, legs, costs, limits):
        self.legs = legs
        self.weights = [_number(leg.plan.weight) for leg in legs]
        self.lots = [Fraction(1, 10**leg.history.decimals) for leg in legs]
        prices = [_number(leg.entry) for leg in legs]
        self.lot_values = [lot * price for lot, price in zip(self.lots, prices, strict=True)]
        # A whole lot at the actual modeled entry must clear the exchange minimum.
        self.minimum_unit = max(
            ceil(MIN_NOTIONAL / value) * value / weight
            for value, weight in zip(self.lot_values, self.weights, strict=True)
        )
        impact = (_number(costs.half_spread_bps) + _number(costs.exit_impact_bps)) / PER_BPS
        taker_fee = _number(costs.taker_fee_bps) / PER_BPS
        entry_fee = (
            _number(costs.maker_fee_bps) / PER_BPS if legs[0].plan.mode == "maker" else taker_fee
        )
        stop_risks = []
        for leg, price, weight in zip(legs, prices, self.weights, strict=True):
            side = leg.plan.side
            stop_fill = _number(leg.stop) * (1 - side * impact)
            stop_loss = (price - stop_fill) * side + price * entry_fee + stop_fill * taker_fee
            stop_risks.append(weight * stop_loss / price)
        self.stop_risk_per_unit = max(stop_risks)
        self.gross_fraction = _number(limits.max_gross_fraction)
        self.net_fraction = _number(limits.max_net_fraction)
        self.leg_fraction = _number(limits.max_leg_fraction)
        self.side_weights = {
            side: sum(
                weight
                for leg, weight in zip(legs, self.weights, strict=True)
                if leg.plan.side == side
            )
            for side in (1, -1)
        }

    def constraints(self, amounts):
        # Each bound is (equity fraction, reserved amount, weight) for the shared basket quantity.
        yield self.gross_fraction, sum(amounts.values()), sum(self.weights)
        # Until every leg fills, directional exposure can reach either side's full notional.
        for side, weight in self.side_weights.items():
            if weight:
                yield self.net_fraction, amounts[side], weight
        yield self.leg_fraction, 0, max(self.weights)

    def maximum_unit(self, capital, amounts, risk_fraction):
        limits = [
            (capital * fraction - occupied) / weight
            for fraction, occupied, weight in self.constraints(amounts)
        ]
        limits.append(capital * risk_fraction / self.stop_risk_per_unit)
        return min(limits)

    def minimum_equities(self, amounts):
        constraints = list(self.constraints(amounts))
        if any(fraction <= 0 for fraction, _, _ in constraints):
            return {}
        portfolio_minimum = max(
            (self.minimum_unit * weight + occupied) / fraction
            for fraction, occupied, weight in constraints
        )
        stop_risk = self.minimum_unit * self.stop_risk_per_unit
        return {
            label: max(portfolio_minimum, stop_risk / fraction)
            for label, fraction in RISK_LEVELS.items()
        }

    def assign(self, unit):
        for leg, weight, lot, value in zip(
            self.legs, self.weights, self.lots, self.lot_values, strict=True
        ):
            leg.size = float(lot * (unit * weight // value))
            leg.entry_fee *= leg.size

    def outcome(self, maximum_unit):
        if maximum_unit <= 0:
            return "budget"
        return "MIN_NOTIONAL" if maximum_unit < self.minimum_unit else "accepted"


def _reserved_notional(reserved):
    amounts = dict.fromkeys((1, -1), Fraction(0))
    for leg in reserved:
        amounts[leg.plan.side] += leg.notional
    return amounts


def _capital_record(thresholds):
    return {key: float(value) for key, value in thresholds.items()}


def _funding(leg, stamp):
    total = 0.0
    assert leg.entered_ms is not None
    for hour in range(leg.entered_ms // HOUR_MS, stamp // HOUR_MS + 1):
        row = leg.history.funding.get(hour)
        if row and leg.entered_ms < row.time_ms <= stamp:
            previous = leg.history.bars.get(hour - 1)
            oracle_proxy = previous.close if previous else leg.history.bars[hour].open
            total -= leg.plan.side * leg.size * row.rate * oracle_proxy
    return total


def _frames(bet, hour):
    start = hour * HOUR_MS
    active = [leg for leg in bet.legs if leg.open or leg.pending]
    quarters = range(start, start + HOUR_MS, QUARTER_MS)
    if all(stamp in leg.history.quarter_bars for leg in active for stamp in quarters):
        for stamp in quarters:
            yield (
                stamp,
                stamp + QUARTER_MS - 1,
                {leg.plan.coin: leg.history.quarter_bars[stamp] for leg in active},
            )
    else:
        yield start, start + HOUR_MS - 1, {leg.plan.coin: leg.history.bars[hour] for leg in active}


class _Replay:
    def __init__(
        self,
        histories,
        costs,
        limits,
        slots_by_strategy=None,
        exit_signal=None,
        data_gap_proxy=False,
        profit_lock_fraction=0.0,
    ):
        self.histories = histories
        self.costs = costs
        self.limits = limits
        self.slots_by_strategy = slots_by_strategy or {}
        self.exit_signal = exit_signal
        self.data_gap_proxy = data_gap_proxy
        self.profit_lock_fraction = _number(profit_lock_fraction)
        self.locked_profit = Fraction(0)
        self.bets = []
        self.trades = []
        self.day_net = defaultdict(float)
        self.week_net = defaultdict(float)
        self.realized_events = defaultdict(float)
        self.net = 0.0
        self.closed_equity = 0.0
        self.marked_peak = self.intrabar_drawdown = 0.0
        self.maker_orders = self.maker_fills = 0
        self.pending_data_gaps = 0
        self.zero_volume_submissions = self.deferred_exit_slices = self.terminal_proxy_exits = 0
        self.marks = {}
        self.submissions = Counter()
        self.empty_book_submissions = {equity: Counter() for equity in COMPARISON_EQUITIES}
        self.executable_plans = []
        self.risk_fraction = _number(limits.risk_fraction)
        self.initial_equity = _number(limits.initial_equity)

    def prepare(self, plan, hour):
        legs = []
        for order in plan.legs:
            history = self.histories[order.coin]
            bar = history.bars.get(hour)
            if bar is None:
                return "data_gap"
            opening = history.quarter_bars.get(hour * HOUR_MS, bar)
            if bar.volume <= 0 or opening.volume <= 0:
                return "zero_volume"
            leg = _prepared_leg(order, history, opening, self.costs)
            if isinstance(leg, str):
                return leg
            legs.append(leg)
        if legs[0].plan.mode == "maker" and any(leg.plan.ttl_hours == 0 for leg in legs):
            return "expired_on_submission"
        return legs

    def inspect_empty_book(self, sizing, rejection, amounts):
        if rejection:
            for outcomes in self.empty_book_submissions.values():
                outcomes[rejection] += 1
            return {}
        minimums = sizing.minimum_equities(amounts)
        for equity, outcomes in self.empty_book_submissions.items():
            unit = sizing.maximum_unit(Fraction(equity), amounts, self.risk_fraction)
            outcomes[sizing.outcome(unit)] += 1
        return minimums

    def submit(self, plan, hour, candidate_number):
        coins = {leg.coin for leg in plan.legs}
        if not coins or len(coins) != len(plan.legs):
            raise ValueError("Research bet requires one or more distinct markets")
        if any(excluded_symbol(coin) for coin in coins):
            raise ValueError("BTC and BTC aliases are excluded from this research")
        if len({leg.mode for leg in plan.legs}) != 1:
            raise ValueError("All legs of a research bet must use the same order mode")

        legs = self.prepare(plan, hour)
        rejection = legs if isinstance(legs, str) else None
        if len(coins) > self.limits.max_positions and rejection is None:
            rejection = "slot_limit"
        sizing = None if rejection else _Sizing(legs, self.costs, self.limits)
        empty_book = _reserved_notional(())
        empty_book_minimums = self.inspect_empty_book(sizing, rejection, empty_book)
        if empty_book_minimums:
            self.executable_plans.append(
                {
                    "hour": hour,
                    "candidate_number": candidate_number,
                    "label": plan.label,
                    "coins": [leg.coin for leg in plan.legs],
                    "minimum_equity": _capital_record(empty_book_minimums),
                }
            )

        reserved = [leg for bet in self.bets for leg in bet.legs if leg.pending or leg.open]
        if coins.intersection(leg.plan.coin for leg in reserved):
            self.submissions["overlap"] += 1
            return
        if rejection:
            self.zero_volume_submissions += rejection == "zero_volume"
            self.submissions[rejection] += 1
            return
        if len(reserved) + len(legs) > self.limits.max_positions:
            self.submissions["slot_limit"] += 1
            return
        if plan.label in self.slots_by_strategy:
            used = sum(
                leg.open or leg.pending
                for bet in self.bets
                if bet.plan.label == plan.label
                for leg in bet.legs
            )
            if used + len(legs) > self.slots_by_strategy[plan.label]:
                self.submissions["slot_limit"] += 1
                return

        total_equity = self.initial_equity + _number(self.closed_equity)
        equity = total_equity - self.locked_profit
        amounts = _reserved_notional(reserved) if reserved else empty_book
        unit = sizing.maximum_unit(equity, amounts, self.risk_fraction)
        outcome = sizing.outcome(unit) if equity > 0 else "budget"
        self.submissions[outcome] += 1
        if outcome != "accepted":
            return

        sizing.assign(unit)
        occupancy_minimums = sizing.minimum_equities(amounts) if reserved else empty_book_minimums
        expiry = hour + min(leg.plan.ttl_hours for leg in legs)
        bet = _Bet(
            plan,
            legs,
            hour,
            expiry,
            {
                "submitted_hour": hour,
                "candidate_number": candidate_number,
                "equity_at_submission": float(equity),
                "total_equity_at_submission": float(total_equity),
                "profit_excluded_from_sizing": float(self.locked_profit),
                "reserved_positions": len(reserved),
                "reserved_long_notional": float(amounts[1]),
                "reserved_short_notional": float(amounts[-1]),
                "minimum_equity_empty_book": _capital_record(empty_book_minimums),
                "minimum_equity_at_submission_occupancy": _capital_record(occupancy_minimums),
            },
        )
        if legs[0].plan.mode == "maker":
            self.maker_orders += len(legs)
        else:
            # All price caps pass the opening proxy before this modeled IOC basket enters.
            # Candles cannot resolve liquidity, queue position, or multi-order coordination.
            for leg in legs:
                leg.fill(hour * HOUR_MS, hour * HOUR_MS)
        self.bets.append(bet)

    def mark(self, leg, price, stamp):
        equity = leg.plan.side * leg.size * (price - leg.entry) - leg.entry_fee
        equity += _funding(leg, stamp)
        self.marks[id(leg)] = min(equity, self.marks.get(id(leg), equity))

    def close(self, leg, stamp, reference, reason, *, maker=False, gap=None, terminal_proxy=False):
        side = leg.plan.side
        price = (
            reference
            if maker
            else _market_exit(reference if gap is None else gap, -side, self.costs)
        )
        exit_fee_bps = self.costs.maker_fee_bps if maker else self.costs.taker_fee_bps
        fees = leg.entry_fee + price * leg.size * exit_fee_bps / PER_BPS
        gross = side * leg.size * (reference - leg.entry_reference)
        slippage = side * leg.size * ((leg.entry - leg.entry_reference) + (reference - price))
        funding = _funding(leg, stamp)
        net = gross - slippage - fees + funding
        leg.result = {
            "coin": leg.plan.coin,
            "side": side,
            "size": leg.size,
            "entered_ms": leg.entered_ms,
            "exited_ms": stamp,
            "entry": leg.entry,
            "exit": price,
            "entry_mode": leg.plan.mode,
            "exit_mode": "maker" if maker else "taker",
            "reason": reason,
            "terminal_proxy": terminal_proxy,
            "gross": gross,
            "funding": funding,
            "fees": fees,
            "slippage": slippage,
            "net": net,
        }
        self.marks[id(leg)] = min(net, self.marks.get(id(leg), net))
        self.net += net
        self.terminal_proxy_exits += terminal_proxy
        self.realized_events[stamp] += net
        day = stamp // (24 * HOUR_MS)
        self.day_net[day] += net
        self.week_net[(day + 3) // 7] += net  # January 1, 1970 was a Thursday.

    def close_at_open(self, bet, stamp, bars, reason):
        bet.stop_waiting()
        reason = "orphan unwind" if bet.orphan else reason
        bet.exit_next = reason
        for leg in bet.legs:
            if leg.open:
                bar = bars[leg.plan.coin]
                if bar.volume <= 0:
                    self.deferred_exit_slices += 1
                    self.mark(leg, leg.last_observed, stamp)
                    continue
                if _before_stop(leg.plan.side, bar.open, leg.stop):
                    self.close(leg, stamp, leg.stop, "stop gap", gap=bar.open)
                elif leg.target is not None and _through_limit(
                    -leg.plan.side, bar.open, leg.target
                ):
                    self.close(leg, stamp, leg.target, "target", maker=True)
                else:
                    self.close(leg, stamp, bar.open, reason)

    def slice(self, bet, bars, start, end, eligible):
        exited_at_open = False
        for leg in bet.legs:
            bar = bars.get(leg.plan.coin)
            if bar is None or bar.volume <= 0:
                if leg.open:
                    self.mark(leg, leg.last_observed, end)
                continue
            side = leg.plan.side
            if eligible and leg.pending and _through_limit(side, bar.open, leg.limit):
                leg.fill(start, start)
                self.maker_fills += 1
            if leg.open:
                self.mark(leg, bar.open, start)
                if _before_stop(side, bar.open, leg.stop):
                    self.close(leg, start, leg.stop, "stop gap", gap=bar.open)
                    exited_at_open = True
                elif (
                    leg.entered_slice != start
                    and leg.target is not None
                    and _through_limit(-side, bar.open, leg.target)
                ):
                    self.close(leg, start, leg.target, "target", maker=True)
                    exited_at_open = True
        if exited_at_open:
            self.close_at_open(bet, start, bars, "basket unwind")
            return

        exited_inside = False
        for leg in bet.legs:
            bar = bars.get(leg.plan.coin)
            if bar is None or bar.volume <= 0:
                continue
            side = leg.plan.side
            worst, best = (bar.low, bar.high) if side > 0 else (bar.high, bar.low)
            if eligible and leg.pending and _through_limit(side, worst, leg.limit):
                leg.fill(end, start)
                self.maker_fills += 1
            if leg.open:
                if _before_stop(side, worst, leg.stop):
                    self.close(leg, end, leg.stop, "stop")
                    exited_inside = True
                    continue
                self.mark(leg, worst, end)
                if (
                    leg.entered_slice != start
                    and leg.target is not None
                    and _through_limit(-side, best, leg.target)
                ):
                    self.close(leg, end, leg.target, "target", maker=True)
                    exited_inside = True
                else:
                    leg.last_observed = bar.close
        if exited_inside:
            bet.stop_waiting()
            bet.exit_next = "orphan unwind" if bet.orphan else "basket unwind"

    def trail(self, leg, hour):
        distance = leg.plan.trailing_atr
        if not distance or leg.entered_ms is None or leg.entered_ms // HOUR_MS >= hour:
            return
        close = leg.last_observed
        side = leg.plan.side
        if (close - leg.best_close) * side > 0:
            leg.best_close = close
            proposed = _price(
                close - side * distance * leg.plan.atr,
                leg.history.decimals,
                ROUND_CEILING if side > 0 else ROUND_DOWN,
            )
            if (proposed - leg.stop) * side > 0:
                leg.stop = proposed

    def run_bet(self, bet, hour, last):
        start = hour * HOUR_MS
        opened = [leg for leg in bet.legs if leg.open]
        expired = hour > bet.expires_after and any(leg.pending for leg in bet.legs)
        if expired:
            bet.stop_waiting()
            if not opened:
                return
        unpriced = [leg.plan.coin for leg in opened if hour not in leg.history.bars]
        if unpriced:
            if not self.data_gap_proxy:
                raise ValueError(
                    f"Missing execution candle for open positions {unpriced} at {hour}"
                )
            bet.stop_waiting()
            for leg in opened:
                self.close(leg, start, leg.last_observed, "data gap", terminal_proxy=True)
            return
        gaps = sum(leg.pending and hour not in leg.history.bars for leg in bet.legs)
        if gaps:
            self.pending_data_gaps += gaps
            bet.stop_waiting()
            if not opened:
                return
            bet.exit_next = "orphan unwind"
        for leg in bet.legs:
            if leg.open:
                self.trail(leg, hour)
        if self.exit_signal and opened and hour > bet.submitted_hour and bet.exit_next is None:
            bet.exit_next = self.exit_signal(bet.plan, bet.submitted_hour, hour)
        for left, right, bars in _frames(bet, hour):
            if not any(leg.pending or leg.open for leg in bet.legs):
                break
            timed_out = any(
                leg.open and left >= leg.entered_ms + leg.plan.hold_hours * HOUR_MS
                for leg in bet.legs
            )
            if bet.exit_next or expired or timed_out:
                bet.stop_waiting()
                reason = "orphan unwind" if bet.orphan else "time"
                self.close_at_open(bet, left, bars, bet.exit_next or reason)
            else:
                self.slice(bet, bars, left, right, eligible=hour > bet.submitted_hour)
            if last and right == start + HOUR_MS - 1:
                bet.stop_waiting()
                for leg in bet.legs:
                    if leg.open:
                        bar = bars[leg.plan.coin]
                        reason = "orphan unwind" if bet.orphan else bet.exit_next or "partition end"
                        terminal_proxy = bar.volume <= 0
                        self.close(
                            leg,
                            right,
                            leg.last_observed if terminal_proxy else bar.close,
                            reason,
                            terminal_proxy=terminal_proxy,
                        )

    def save_finished(self, bet):
        legs = [leg.result for leg in bet.legs if leg.result is not None]
        if not legs:
            return
        self.trades.append(
            {
                "label": bet.plan.label,
                "legs": legs,
                "sizing": bet.sizing,
                "orphan": bet.orphan,
                "entered_ms": min(leg["entered_ms"] for leg in legs),
                "exited_ms": max(leg["exited_ms"] for leg in legs),
                **{key: sum(leg[key] for leg in legs) for key in _PNL_KEYS},
            }
        )

    def hour(self, plans, hour, end):
        self.marks.clear()
        before = self.net
        for candidate_number, plan in enumerate(plans, start=1):
            self.submit(plan, hour, candidate_number)
        active = []
        for bet in self.bets:
            self.run_bet(bet, hour, last=hour == end - 1)
            if any(leg.open or leg.pending for leg in bet.legs):
                active.append(bet)
            else:
                self.save_finished(bet)
        self.bets = active
        if self.marks:
            # Adverse extremes on different markets need not have occurred together.
            self.intrabar_drawdown = max(
                self.intrabar_drawdown, self.marked_peak - (before + sum(self.marks.values()))
            )
        mark_to_market = self.net
        for bet in self.bets:
            for leg in bet.legs:
                if leg.open:
                    close = leg.last_observed
                    mark_to_market += leg.plan.side * leg.size * (close - leg.entry) - leg.entry_fee
                    mark_to_market += _funding(leg, (hour + 1) * HOUR_MS - 1)
        self.closed_equity = mark_to_market
        if self.profit_lock_fraction:
            self.locked_profit = max(
                self.locked_profit, _number(self.net) * self.profit_lock_fraction
            )
        self.marked_peak = max(self.marked_peak, mark_to_market)
        self.intrabar_drawdown = max(self.intrabar_drawdown, self.marked_peak - mark_to_market)


_PNL_KEYS = ("gross", "funding", "fees", "slippage", "net")


def _submission_counts(outcomes):
    return {
        "candidates": sum(outcomes.values()),
        "accepted": outcomes["accepted"],
        "rejected": {reason: outcomes[reason] for reason in REJECTIONS},
    }


def _capital_summary(thresholds):
    summary = {}
    for risk in RISK_LEVELS:
        values = sorted(float(plan[risk]) for plan in thresholds if risk in plan)
        count = len(values)
        summary[risk] = {
            "count": count,
            **{
                name: values[(rank * count + 99) // 100 - 1] if count else None
                for name, rank in (("p50", 50), ("p90", 90), ("p95", 95), ("max", 100))
            },
        }
    return summary


def _summarize(engine, hours):
    trades = sorted(engine.trades, key=lambda trade: (trade["exited_ms"], trade["entered_ms"]))
    values = [trade["net"] for trade in trades]
    wins = [value for value in values if value > 0]
    losses = [-value for value in values if value < 0]
    by_coin = defaultdict(float)
    exposed_hours = set()
    for trade in trades:
        for leg in trade["legs"]:
            by_coin[leg["coin"]] += leg["net"]
            left = leg["entered_ms"] // HOUR_MS
            right = max(leg["entered_ms"], leg["exited_ms"] - 1) // HOUR_MS
            exposed_hours.update(range(left, right + 1))
    current = peak = realized_drawdown = 0.0
    for _, change in sorted(engine.realized_events.items()):
        current += change
        peak = max(peak, current)
        realized_drawdown = max(realized_drawdown, peak - current)
    net = sum(values)
    return {
        "count": len(trades),
        "wins": len(wins),
        "win_rate": len(wins) / len(trades) if trades else 0,
        **{key: sum(trade[key] for trade in trades) for key in _PNL_KEYS},
        "profit_factor": sum(wins) / sum(losses) if losses else None,
        "payoff_ratio": mean(wins) / mean(losses) if wins and losses else None,
        "net_without_best": net - max(0, max(values, default=0)),
        "net_without_best_coin": net - max(0, max(by_coin.values(), default=0)),
        "realized_drawdown": realized_drawdown,
        "intrabar_drawdown": max(engine.intrabar_drawdown, realized_drawdown),
        "worst_day": min(0, min(engine.day_net.values(), default=0)),
        "worst_week": min(0, min(engine.week_net.values(), default=0)),
        "maker_fill_rate": engine.maker_fills / engine.maker_orders if engine.maker_orders else 0,
        "maker_orders": engine.maker_orders,
        "maker_fills": engine.maker_fills,
        "pending_data_gaps": engine.pending_data_gaps,
        "zero_volume_submissions": engine.zero_volume_submissions,
        "deferred_exit_slices": engine.deferred_exit_slices,
        "terminal_proxy_exits": engine.terminal_proxy_exits,
        "profit_excluded_from_sizing_at_end": float(engine.locked_profit),
        "hours_in_market_pct": 100 * len(exposed_hours) / hours if hours else 0,
        "orphans": sum(trade["orphan"] for trade in trades),
        "submission_diagnostics": {
            "initial_equity": engine.limits.initial_equity,
            "risk_fraction_per_leg": engine.limits.risk_fraction,
            "minimum_notional_per_leg": MIN_NOTIONAL,
            "replay": _submission_counts(engine.submissions),
            "empty_book": {
                str(equity): _submission_counts(outcomes)
                for equity, outcomes in engine.empty_book_submissions.items()
            },
            "executable_plans": engine.executable_plans,
            "minimum_equity": {
                "executable_empty_book": _capital_summary(
                    [plan["minimum_equity"] for plan in engine.executable_plans]
                ),
                "filled_empty_book": _capital_summary(
                    [trade["sizing"]["minimum_equity_empty_book"] for trade in trades]
                ),
                "filled_at_submission_occupancy": _capital_summary(
                    [trade["sizing"]["minimum_equity_at_submission_occupancy"] for trade in trades]
                ),
            },
        },
        "by_coin": dict(sorted(by_coin.items())),
        "trades": trades,
    }


def replay(
    histories: dict[str, MarketHistory],
    ideas: dict[int, list[TradePlan]],
    start: int,
    end: int,
    *,
    costs=BASE,
    limits=RiskLimits(),
    slots_by_strategy: dict[str, int] | None = None,
    exit_signal=None,
    data_gap_proxy: bool = False,
    profit_lock_fraction: float = 0.0,
) -> dict:
    """Submit ideas at their epoch hour and liquidate before the exclusive end hour."""
    if end < start:
        raise ValueError("Replay end precedes the start")
    if slots_by_strategy and any(
        type(count) is not int or not 1 <= count <= limits.max_positions
        for count in slots_by_strategy.values()
    ):
        raise ValueError("Each strategy needs a positive slot cap within the portfolio limit")
    if not 0 <= profit_lock_fraction <= 1:
        raise ValueError("Profit lock fraction must be between zero and one")
    engine = _Replay(
        histories,
        costs,
        limits,
        slots_by_strategy,
        exit_signal,
        data_gap_proxy,
        profit_lock_fraction,
    )
    for hour in range(start, end):
        if engine.bets or hour in ideas:
            engine.hour(ideas.get(hour, ()), hour, end)
    return _summarize(engine, end - start)
