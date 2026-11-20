"""Replay the perpetual breakout rules against public Hyperliquid candles."""

import argparse
import json
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .autopilot import Settings, entry_order, protection_prices, utc_day
from .perps import BAR_MS, BPS, Book, Market, breakout, market_breadth, number, rank_signal

STEP = 5 * 60 * 1000
HOUR = 60 * 60 * 1000
DAY = 24 * 60 * 60 * 1000
TRADED = ("BTC", "ETH", "SOL", "HYPE")
FEE = Decimal("0.00045")


@dataclass(frozen=True)
class Scenario:
    name: str
    reversal: bool = True
    half_spread_bps: Decimal = Decimal(1)
    entry_impact_bps: Decimal = Decimal(1)
    exit_impact_bps: Decimal = Decimal(2)
    loss_fraction: Decimal | None = None


SCENARIOS = (
    Scenario("fixed exits", reversal=False),
    Scenario("failed-breakout exit"),
    Scenario(
        "failed-breakout exit, higher costs",
        half_spread_bps=Decimal(4),
        entry_impact_bps=Decimal(4),
        exit_impact_bps=Decimal(15),
    ),
    Scenario("failed-breakout exit, 1% daily loss test", loss_fraction=Decimal("0.01")),
)


def validated_bars(coin, rows, start, end, period):
    sorted_rows = sorted(rows, key=lambda row: row["t"])
    if [row["t"] for row in sorted_rows] != list(range(start, end, period)):
        raise ValueError(f"{coin} candles do not cover the complete requested period")
    for row in sorted_rows:
        low, high = number(row["l"]), number(row["h"])
        if (
            row["s"] != coin
            or row["T"] != row["t"] + period - 1
            or not 0 < low <= min(number(row["o"]), number(row["c"]))
            or high < max(number(row["o"]), number(row["c"]))
            or number(row["v"]) < 0
        ):
            raise ValueError(f"Invalid candle for {coin} at {row['t']}")
    return sorted_rows


def combine_15m(rows):
    bars = []
    for index in range(0, len(rows), 3):
        first, middle, last = rows[index : index + 3]
        group = (first, middle, last)
        bars.append(
            {
                "t": first["t"],
                "T": last["T"],
                "s": first["s"],
                "i": "15m",
                "o": first["o"],
                "c": last["c"],
                "h": str(max(number(x["h"]) for x in group)),
                "l": str(min(number(x["l"]) for x in group)),
                "v": str(sum(number(x["v"]) for x in group)),
            }
        )
    return bars


class Tape:
    def __init__(self, data, *, step=STEP, warmup_days=1):
        self.start, self.end = data["start_ms"], data["end_ms"]
        if step not in (STEP, BAR_MS, HOUR) or warmup_days < 1:
            raise ValueError("Unsupported replay interval or warmup")
        self.step = step
        self.history = self.start - warmup_days * DAY
        if self.start % DAY or self.end % DAY or self.end <= self.start:
            raise ValueError("Backrun requires complete UTC trading days")
        self.decimals = data["meta"]
        self.execution = {}
        self.signal_step = max(BAR_MS, step)
        self.bars = {}
        self.turnover = {}
        for coin, rows in data["candles"].items():
            period = step if coin in TRADED else self.signal_step
            bars = validated_bars(coin, rows, self.history, self.end, period)
            if coin in TRADED:
                self.execution[coin] = bars
            if period == STEP:
                bars = combine_15m(bars)
            self.bars[coin] = bars
            totals = [Decimal(0)]
            for bar in bars:
                totals.append(totals[-1] + number(bar["c"]) * number(bar["v"]))
            self.turnover[coin] = totals
        self.funding = {}
        self.funding_times = {}
        for coin in TRADED:
            rows = sorted(
                (row["time"], number(row["fundingRate"])) for row in data["funding"][coin]
            )
            hours = [time // HOUR for time, _ in rows]
            expected = list(range(self.history // HOUR, self.end // HOUR))
            if hours != expected:
                raise ValueError(f"{coin} funding observations are missing or duplicated")
            self.funding[coin] = rows
            self.funding_times[coin] = [time for time, _ in rows]
        self.sentiment = sorted(
            (int(row["timestamp"]) * 1000, int(row["value"])) for row in data["sentiment"]
        )
        if any(not 0 <= value <= 100 for _, value in self.sentiment):
            raise ValueError("Invalid historical sentiment")

    def open_bar(self, coin, time):
        return self.execution[coin][(time - self.history) // self.step]

    def previous_bars(self, coin, time, count):
        index = (time - self.history) // self.signal_step
        return self.bars[coin][max(0, index - count) : index]

    def prior_close(self, coin, time):
        return number(self.previous_bars(coin, time, 1)[0]["c"])

    def markets_at(self, time):
        index = (time - self.history) // self.signal_step
        window = DAY // self.signal_step
        for coin, bars in self.bars.items():
            totals = self.turnover[coin]
            volume = totals[index] - totals[index - window]
            close, prev = number(bars[index - 1]["c"]), number(bars[index - window]["o"])
            end = bisect_left(self.funding_times.get(coin, []), time)
            funding = self.funding[coin][end - 1][1] if end else Decimal(0)
            mark = number(self.open_bar(coin, time)["o"]) if coin in TRADED else close
            yield Market(coin, self.decimals[coin], volume, funding, mark, close / prev - 1)

    def signals(self, time, min_volume, max_funding):
        if self.signal_step != BAR_MS:
            raise ValueError("The breakout baseline requires 15-minute signals")
        markets = list(self.markets_at(time))
        breadth = market_breadth(markets, min_volume)
        # Publication time within a historical sentiment day is unavailable.
        previous = [value for stamp, value in self.sentiment if stamp < time // DAY * DAY]
        sentiment = {"index": previous[-1]} if previous else {}
        candidates = []
        for asset in markets:
            if asset.coin not in TRADED or asset.volume < min_volume:
                continue
            signal = breakout(asset, self.previous_bars(asset.coin, time, 24), time)
            if signal and asset.funding * (1 if signal.long else -1) * BPS <= max_funding:
                candidates.append(rank_signal(signal, breadth, sentiment))
        return sorted(
            candidates,
            key=lambda signal: (-signal.score, -signal.market.volume, signal.market.coin),
        )

    def funding_cash(self, coin, side, size, entered, exited):
        times = self.funding_times[coin]
        applicable = self.funding[coin][bisect_right(times, entered) : bisect_right(times, exited)]
        return sum(
            (
                -side * size * rate * number(self.open_bar(coin, time - self.step)["c"])
                for time, rate in applicable
            ),
            Decimal(0),
        )


def exit_signal(tape, trade, stamp, cfg, scenario, *, intrabar):
    row = tape.open_bar(trade["coin"], stamp)
    side, opened = trade["side"], number(row["o"])
    stop = trade["stop"]
    goal = trade.get("target", trade["entry"] * (1 + side * cfg.target_bps / BPS))
    goal_mid = None if goal is None else goal / (1 - side * scenario.half_spread_bps / BPS)
    if not intrabar:
        if (opened - stop) * side <= 0:
            return "stop gap", opened
        if goal_mid is not None and (opened - goal_mid) * side >= 0:
            return "take profit", opened
        if stamp - trade["entered"] >= trade.get("hold_ms", cfg.max_hold_minutes * 60_000):
            return "maximum hold", opened
        if scenario.reversal and stamp % BAR_MS == 0 and stamp > trade["entered"]:
            if (tape.prior_close(trade["coin"], stamp) - trade["breakout"]) * side <= 0:
                return "failed breakout", opened
        return None
    worst, best = (number(row[key]) for key in (("l", "h") if side == 1 else ("h", "l")))
    if (worst - stop) * side <= 0:
        return "stop", stop
    if goal_mid is not None and (best - goal_mid) * side >= 0:
        return "take profit", goal_mid
    return None


def replay(tape, initial, cfg, scenario, *, rules=None, start=None, end=None):
    start = tape.start if start is None else start
    end = tape.end if end is None else end
    if not tape.start <= start < end <= tape.end or start % DAY or end % DAY:
        raise ValueError("Replay partition must be within the tape and use UTC day boundaries")
    interval = tape.signal_step if rules is None else rules.interval
    pnl_by_day = defaultdict(Decimal)
    starts = {}
    attempts = Counter()
    risk_blocks = set()
    fees = funding_total = net_total = peak = drawdown = Decimal(0)
    trades = []
    position = None

    def close(stamp, reason, mid):
        nonlocal position, fees, funding_total, net_total, peak, drawdown
        side, size, entry = position["side"], position["size"], position["entry"]
        execution = mid * (1 - side * (scenario.half_spread_bps + scenario.exit_impact_bps) / BPS)
        cost = (entry + execution) * size * FEE
        funding = tape.funding_cash(position["coin"], side, size, position["entered"], stamp)
        gross = side * size * (execution - entry)
        net = gross - cost + funding
        fees += cost
        funding_total += funding
        net_total += net
        peak = max(peak, net_total)
        drawdown = max(drawdown, peak - net_total)
        pnl_by_day[utc_day(stamp)] += net
        trades.append(
            {
                **position,
                "exited": stamp,
                "exit": execution,
                "reason": reason,
                "gross": gross,
                "fees": cost,
                "funding": funding,
                "net": net,
            }
        )
        position = None

    for stamp in range(start, end, tape.step):
        day = utc_day(stamp)
        if day not in starts:
            starts[day] = initial + net_total
            if position:
                opening_mark = number(tape.open_bar(position["coin"], stamp)["o"])
                starts[day] += (
                    position["side"] * position["size"] * (opening_mark - position["entry"])
                )
        if position:
            if rules is not None and stamp % interval == 0:
                rules.update_stop(tape, position, stamp)
            signal = exit_signal(tape, position, stamp, cfg, scenario, intrabar=False)
            if signal:
                close(stamp, *signal)
                continue
        elif stamp % interval == 0 and attempts[day] < cfg.max_entries_per_day:
            loss = cfg.max_daily_loss_usdc
            if scenario.loss_fraction is not None:
                loss = min(loss, starts[day] * scenario.loss_fraction)
            if pnl_by_day[day] <= -loss:
                risk_blocks.add(day)
                continue
            if initial + net_total < cfg.max_order_usd * Decimal("1.005"):
                continue
            signals = (
                tape.signals(stamp, cfg.min_volume_usd, cfg.max_funding_bps)
                if rules is None
                else rules.signals(tape, stamp, cfg)
            )
            for signal in signals:
                side = 1 if signal.long else -1
                mid, half = signal.market.mark, scenario.half_spread_bps / BPS
                book = Book(
                    mid * (1 - half), mid * (1 + half), Decimal(10**6), Decimal(10**6), stamp
                )
                proposed = entry_order(signal, book, cfg)
                if not proposed:
                    continue
                size, limit = proposed
                filled = (book.ask if signal.long else book.bid) * (
                    1 + side * scenario.entry_impact_bps / BPS
                )
                if (filled - limit) * side > 0:
                    attempts[day] += 1
                    break
                trigger, _ = protection_prices(
                    limit, long=signal.long, decimals=signal.market.decimals, stop_bps=cfg.stop_bps
                )
                if (mid - trigger) * side <= 0 or trigger <= 0:
                    continue
                position = {
                    "coin": signal.market.coin,
                    "side": side,
                    "entered": stamp,
                    "size": size,
                    "entry": filled,
                    "stop": trigger,
                    "breakout": signal.trigger,
                }
                if rules is not None and not rules.prepare_position(tape, position, stamp):
                    position = None
                    continue
                attempts[day] += 1
                if filled * size > cfg.max_order_usd:
                    close(stamp, "filled notional exceeded cap", mid)
                break
        if position:
            signal = exit_signal(tape, position, stamp, cfg, scenario, intrabar=True)
            if signal:
                close(stamp + tape.step - 1, *signal)
    if position:
        close(
            end - 1,
            "sample ended",
            number(tape.open_bar(position["coin"], end - tape.step)["c"]),
        )
    return {
        "scenario": scenario.name,
        "filled_trades": len(trades),
        "attempts": sum(attempts.values()),
        "profitable_trades": sum(trade["net"] > 0 for trade in trades),
        "net_usdc": net_total,
        "fees_usdc": fees,
        "funding_usdc": funding_total,
        "return_pct": net_total * 100 / initial,
        "realized_drawdown_usdc": drawdown,
        "worst_realized_day_usdc": min(pnl_by_day.values(), default=0),
        "exit_reasons": dict(Counter(trade["reason"] for trade in trades)),
        "days_loss_limit_blocked": sorted(risk_blocks),
        "net_by_day_usdc": dict(sorted(pnl_by_day.items())),
        "trades": trades,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--starting-usdc", type=Decimal, default=Decimal(50))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    initial = args.starting_usdc
    if not initial.is_finite() or initial <= 0:
        parser.error("--starting-usdc must be a finite positive amount")
    cfg = Settings(markets=list(TRADED), max_order_usd="11", max_entries_per_day=6)
    tape = Tape(json.loads(args.source.read_text()))
    results = [replay(tape, initial, cfg, scenario) for scenario in SCENARIOS]
    args.output.write_text(
        json.dumps(
            {
                "start_ms": tape.start,
                "end_ms": tape.end,
                "starting_usdc": initial,
                "results": results,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )
    for row in results:
        print(f"{row['scenario']}: {row['filled_trades']} trades, {row['net_usdc']:+.4f} USDC net")


if __name__ == "__main__":
    main()
