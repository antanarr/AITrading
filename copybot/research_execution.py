"""Replay research orders using archived public candles and funding settlements."""

from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from statistics import mean

from .perps import round_price
from .research_models import HOUR_MS, MarketHistory, OrderPlan, TradePlan, excluded_symbol

QUARTER_MS = HOUR_MS // 4
PER_BPS = 10_000
MIN_NOTIONAL = Decimal(10)


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
        return self.size * self.entry

    def fill(self, stamp, slice_start):
        self.entered_ms = stamp
        self.entered_slice = slice_start


@dataclass
class _Bet:
    plan: TradePlan
    legs: list[_Leg]
    submitted_hour: int
    expires_after: int
    exit_next: str | None = None
    orphan: bool = False

    def stop_waiting(self):
        for leg in self.legs:
            if leg.pending:
                leg.canceled = True
                self.orphan = self.orphan or any(item.entered_ms is not None for item in self.legs)


def _price(price, decimals, rounding):
    return float(round_price(Decimal(str(price)), decimals, rounding))


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
        return None
    if plan.mode == "maker":
        offset = Decimal(side) * Decimal(str(costs.half_spread_bps)) / PER_BPS
        executable = Decimal(str(bar.open)) * (1 + offset)
        if not _through_limit(side, Decimal(str(limit)), executable):
            return None
        entry = reference = limit
        fee_bps = costs.maker_fee_bps
    elif plan.mode in ("ioc", "taker"):
        reference = bar.open
        bps = Decimal(str(costs.half_spread_bps)) + Decimal(str(costs.entry_impact_bps))
        executable = Decimal(str(reference)) * (1 + side * bps / PER_BPS)
        if (executable - Decimal(str(limit))) * side > 0:
            return None
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
        return None
    if target is not None and (target <= 0 or (target - entry) * side <= 0):
        return None
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


def _size_legs(legs, reserved, capital, costs, limits):
    if capital <= 0 or len(reserved) + len(legs) > limits.max_positions:
        return False
    gross_available = capital * limits.max_gross_fraction - sum(leg.notional for leg in reserved)
    # A partially filled basket can be directional, so reserve both sides independently.
    net_available = {
        side: capital * limits.max_net_fraction
        - sum(leg.notional for leg in reserved if leg.plan.side == side)
        for side in (1, -1)
    }
    weight = sum(leg.plan.weight for leg in legs)
    caps = [gross_available / weight]
    for side, available in net_available.items():
        side_weight = sum(leg.plan.weight for leg in legs if leg.plan.side == side)
        if side_weight:
            caps.append(available / side_weight)
    for leg in legs:
        stop_fill = _market_exit(leg.stop, -leg.plan.side, costs)
        risk = (leg.entry - stop_fill) * leg.plan.side + leg.entry_fee
        risk += stop_fill * costs.taker_fee_bps / PER_BPS
        caps.append(
            min(
                capital * limits.max_leg_fraction, capital * limits.risk_fraction * leg.entry / risk
            )
            / leg.plan.weight
        )

    unit = min(caps)
    if unit <= 0:
        return False
    for leg in legs:
        price = Decimal(str(leg.entry))
        lot = Decimal(1).scaleb(-leg.history.decimals)
        size = (Decimal(str(unit * leg.plan.weight)) / price).quantize(lot, rounding=ROUND_DOWN)
        if size * price < MIN_NOTIONAL:
            return False
        leg.size = float(size)
        leg.entry_fee *= leg.size
    return True


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
    def __init__(self, histories, costs, limits):
        self.histories = histories
        self.costs = costs
        self.limits = limits
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

    def submit(self, plan, hour):
        coins = {leg.coin for leg in plan.legs}
        if not coins or len(coins) != len(plan.legs):
            raise ValueError("Research bet requires one or more distinct markets")
        if any(excluded_symbol(coin) for coin in coins):
            raise ValueError("BTC and BTC aliases are excluded from this research")
        if len({leg.mode for leg in plan.legs}) != 1:
            raise ValueError("All legs of a research bet must use the same order mode")
        reserved = [leg for bet in self.bets for leg in bet.legs if leg.pending or leg.open]
        if coins.intersection(leg.plan.coin for leg in reserved):
            return
        legs = []
        for order in plan.legs:
            history = self.histories[order.coin]
            bar = history.bars.get(hour)
            if bar is None:
                return
            opening = history.quarter_bars.get(hour * HOUR_MS, bar)
            if bar.volume <= 0 or opening.volume <= 0:
                self.zero_volume_submissions += 1
                return
            leg = _prepared_leg(order, history, bar, self.costs)
            if leg is None:
                return
            legs.append(leg)
        if not _size_legs(
            legs, reserved, self.limits.initial_equity + self.closed_equity, self.costs, self.limits
        ):
            return
        expiry = hour + min(leg.plan.ttl_hours for leg in legs)
        bet = _Bet(plan, legs, hour, expiry)
        if legs[0].plan.mode == "maker":
            self.maker_orders += len(legs)
            if expiry == hour:
                return
        else:
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
            raise ValueError(f"Missing execution candle for open positions {unpriced} at {hour}")
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
                "orphan": bet.orphan,
                "entered_ms": min(leg["entered_ms"] for leg in legs),
                "exited_ms": max(leg["exited_ms"] for leg in legs),
                **{key: sum(leg[key] for leg in legs) for key in _PNL_KEYS},
            }
        )

    def hour(self, plans, hour, end):
        self.marks.clear()
        before = self.net
        for plan in plans:
            self.submit(plan, hour)
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
        self.marked_peak = max(self.marked_peak, mark_to_market)
        self.intrabar_drawdown = max(self.intrabar_drawdown, self.marked_peak - mark_to_market)


_PNL_KEYS = ("gross", "funding", "fees", "slippage", "net")


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
        "hours_in_market_pct": 100 * len(exposed_hours) / hours if hours else 0,
        "orphans": sum(trade["orphan"] for trade in trades),
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
) -> dict:
    """Submit ideas at their epoch hour and liquidate before the exclusive end hour."""
    if end < start:
        raise ValueError("Replay end precedes the start")
    engine = _Replay(histories, costs, limits)
    for hour in range(start, end):
        if engine.bets or hour in ideas:
            engine.hour(ideas.get(hour, ()), hour, end)
    return _summarize(engine, end - start)
