"""Compare frozen entry and exit rules on saved Hyperliquid market data."""

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from pathlib import Path
from statistics import mean, pstdev

from .autopilot import Settings
from .backrun import BAR_MS, DAY, HOUR, SCENARIOS, TRADED, Tape, replay
from .perps import BPS, Signal, number, round_price

ZERO = Decimal(0)
CONFIG = Settings(markets=list(TRADED), max_order_usd="11", max_entries_per_day=6)
PEERS = ("XRP", "DOGE", "BNB", "LINK")


class ConfirmedBreakout:
    interval = BAR_MS

    def signals(self, tape, stamp, cfg):
        for signal in tape.signals(stamp, cfg.min_volume_usd, cfg.max_funding_bps):
            if signal.relative_volume is None or signal.relative_volume < Decimal("1.5"):
                continue
            side = 1 if signal.long else -1
            changes = []
            for coin in {signal.market.coin, "BTC"}:
                bars = tape.previous_bars(coin, stamp, 4 * HOUR // BAR_MS)
                changes.append(number(bars[-1]["c"]) - number(bars[0]["o"]))
            if all(change * side > 0 for change in changes):
                yield signal

    def prepare_position(self, tape, position, stamp):
        return True

    def update_stop(self, tape, position, stamp):
        pass


def closed_ranges(tape, coin, stamp, interval, count):
    """Read complete bars as (high, low, close), consolidating where necessary."""
    group_size = interval // tape.signal_step
    bars = tape.previous_bars(coin, stamp, count * group_size)
    if stamp % interval or len(bars) != count * group_size or bars[0]["t"] % interval:
        raise ValueError("Range calculation needs complete, aligned history")
    return [
        (
            max(number(bar["h"]) for bar in bars[index : index + group_size]),
            min(number(bar["l"]) for bar in bars[index : index + group_size]),
            number(bars[index + group_size - 1]["c"]),
        )
        for index in range(0, len(bars), group_size)
    ]


def average_true_range(ranges):
    return mean(
        max(high - low, abs(high - previous[2]), abs(low - previous[2]))
        for previous, (high, low, _) in zip(ranges[-15:-1], ranges[-14:], strict=True)
    )


def research_stop(tape, position, anchor, distance):
    side = position["side"]
    return round_price(
        anchor - side * distance,
        tape.decimals[position["coin"]],
        ROUND_CEILING if side > 0 else ROUND_DOWN,
    )


@dataclass(frozen=True)
class HourlyRules:
    history: Tape
    interval: int
    lookback: int
    hold_bars: int

    def signals(self, tape, stamp, cfg):
        ranked = []
        for market in tape.markets_at(stamp):
            if market.coin not in TRADED or market.volume < cfg.min_volume_usd:
                continue
            ranges = closed_ranges(self.history, market.coin, stamp, self.interval, self.lookback)
            setup = self.setup(ranges)
            if setup is None:
                continue
            long, trigger = setup
            if market.funding * (1 if long else -1) * BPS > cfg.max_funding_bps:
                continue
            close = ranges[-1][2]
            atr = average_true_range(ranges)
            if atr > 0:
                signal = Signal(market, long, stamp - 1, trigger, close)
                ranked.append((abs(close - trigger) / atr, signal))
        return [
            signal
            for _, signal in sorted(
                ranked, key=lambda pair: (-pair[0], -pair[1].market.volume, pair[1].market.coin)
            )
        ]

    def set_bracket(self, tape, position, distance, target):
        entry = position["entry"]
        if not entry * Decimal("0.005") <= distance <= entry * Decimal("0.04"):
            return False
        position.update(
            stop=research_stop(tape, position, entry, distance),
            target=target,
            hold_ms=self.interval * self.hold_bars,
            stop_distance=distance,
        )
        return True


@dataclass(frozen=True)
class Momentum(HourlyRules):
    channel: int

    def setup(self, ranges):
        close = ranges[-1][2]
        channel = ranges[-self.channel - 1 : -1]
        high = max(bar[0] for bar in channel)
        low = min(bar[1] for bar in channel)
        trend = mean(bar[2] for bar in ranges[:-1])
        if close > high and close > trend:
            return True, high
        if close < low and close < trend:
            return False, low
        return None

    def prepare_position(self, tape, position, stamp):
        ranges = closed_ranges(self.history, position["coin"], stamp, self.interval, 15)
        position["best_close"] = position["entry"]
        return self.set_bracket(tape, position, 2 * average_true_range(ranges), None)

    def update_stop(self, tape, position, stamp):
        close = closed_ranges(self.history, position["coin"], stamp, self.interval, 1)[0][2]
        side = position["side"]
        if (close - position["best_close"]) * side <= 0:
            return
        position["best_close"] = close
        proposed = research_stop(tape, position, close, position["stop_distance"])
        if (proposed - position["stop"]) * side > 0:
            position["stop"] = proposed


class RangeReentry(HourlyRules):
    def bands(self, ranges):
        closes = [row[2] for row in ranges[-22:-2]]
        center = mean(closes)
        width = 2 * pstdev(closes)
        return center - width, center, center + width

    def setup(self, ranges):
        previous, close = (bar[2] for bar in ranges[-2:])
        low, center, high = self.bands(ranges)
        # Require little net drift over the prior two days before fading an extreme.
        if abs(ranges[-3][2] - ranges[0][2]) > 2 * average_true_range(ranges[:-2]):
            return None
        if previous < low < close < center:
            return True, low
        if previous > high > close > center:
            return False, high
        return None

    def prepare_position(self, tape, position, stamp):
        ranges = closed_ranges(self.history, position["coin"], stamp, self.interval, self.lookback)
        _, target, _ = self.bands(ranges)
        entry = position["entry"]
        distance = Decimal("1.5") * average_true_range(ranges)
        if (target - entry) * position["side"] <= 0:
            return False
        return self.set_bracket(tape, position, distance, target)

    def update_stop(self, tape, position, stamp):
        pass


def load_tape(directory, end, days, warmup, *, interval):
    start = end - days * DAY
    hourly = interval == "1h"
    coins = TRADED if hourly else TRADED + PEERS

    def read(name):
        return json.loads((directory / name).read_text())

    # The public API has small response limits, so funding is archived in 16-day chunks.
    def funding(coin):
        return [
            row
            for offset in range(0, days + warmup, 16)
            for row in read(f"{coin}-funding-{offset}.json")
            if start - warmup * DAY <= row["time"] < end
        ]

    sizes = {asset["name"]: asset["szDecimals"] for asset in read("meta.json")["universe"]}
    return Tape(
        {
            "start_ms": start,
            "end_ms": end,
            "meta": {coin: sizes[coin] for coin in coins},
            "candles": {coin: read(f"{coin}-{interval}.json") for coin in coins},
            "funding": {coin: funding(coin) for coin in TRADED},
            "sentiment": [] if hourly else read("sentiment.json"),
        },
        step=HOUR if hourly else BAR_MS,
        warmup_days=warmup,
    )


def metrics(trades):
    wins = [trade["net"] for trade in trades if trade["net"] > 0]
    losses = [trade["net"] for trade in trades if trade["net"] < 0]
    return {
        "trades": len(trades),
        "wins": len(wins),
        **{
            key: sum((trade[key] for trade in trades), ZERO)
            for key in ("gross", "fees", "funding", "net")
        },
        "average_win": mean(wins) if wins else None,
        "average_loss": mean(losses) if losses else None,
        "net_profit_factor": sum(wins) / -sum(losses) if losses else None,
    }


def summarize(result):
    trades = result["trades"]
    return {
        **metrics(trades),
        "attempts": result["attempts"],
        "return_pct": result["return_pct"],
        "realized_drawdown": result["realized_drawdown_usdc"],
        "worst_realized_day": result["worst_realized_day_usdc"],
        "days_at_entry_cap": sum(
            count == CONFIG.max_entries_per_day
            for count in Counter(trade["entered"] // DAY for trade in trades).values()
        ),
        "by_exit": groups(trades, lambda trade: trade["reason"]),
        "by_coin": groups(trades, lambda trade: trade["coin"]),
        "by_side": groups(trades, lambda trade: "long" if trade["side"] > 0 else "short"),
        "by_week": groups(
            trades,
            lambda trade: datetime.fromtimestamp(trade["exited"] / 1000, UTC).strftime("%G-W%V"),
        ),
        "fills": trades,
    }


def groups(trades, key):
    grouped = defaultdict(list)
    for trade in trades:
        grouped[key(trade)].append(trade)
    return {label: metrics(rows) for label, rows in sorted(grouped.items())}


def evaluate(tape, initial, *, split, alternatives, middle_end=None):
    partitions = {"full": (tape.start, tape.end), "early": (tape.start, split)}
    if middle_end is None:
        partitions["holdout"] = (split, tape.end)
    else:
        partitions["holdout"] = (split, middle_end)
        partitions["previously_reviewed"] = (middle_end, tape.end)
    for name, rules, reversal in alternatives:
        for cost, model in (("base", SCENARIOS[1]), ("stress", SCENARIOS[2])):
            for label, (start, end) in partitions.items():
                result = summarize(
                    replay(
                        tape,
                        initial,
                        CONFIG,
                        replace(model, name=name, reversal=reversal),
                        rules=rules,
                        start=start,
                        end=end,
                    )
                )
                yield {
                    "strategy": name,
                    "cost": cost,
                    "partition": label,
                    "start_ms": start,
                    "end_ms": end,
                    **result,
                }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument(
        "--end-date", type=date.fromisoformat, required=True, help="First excluded UTC day"
    )
    parser.add_argument("--starting-usdc", type=Decimal, default=Decimal(50))
    args = parser.parse_args()
    initial = args.starting_usdc
    if not initial.is_finite() or initial <= 0:
        parser.error("--starting-usdc must be a finite positive amount")
    end = int(datetime.combine(args.end_date, datetime.min.time(), UTC).timestamp() * 1000)
    hourly = load_tape(args.archive, end, 180, 14, interval="1h")
    hourly_rules = [
        (
            "20-hour trend breakout",
            Momentum(hourly, HOUR, lookback=49, hold_bars=36, channel=20),
            False,
        ),
        (
            "3-day trend breakout",
            Momentum(hourly, 4 * HOUR, lookback=61, hold_bars=30, channel=18),
            False,
        ),
        (
            "Hourly range reentry",
            RangeReentry(hourly, HOUR, lookback=51, hold_bars=12),
            False,
        ),
    ]
    result = {"starting_usdc": initial}
    tape = load_tape(args.archive, end, 48, 3, interval="15m")
    result["15m"] = list(
        evaluate(
            tape,
            initial,
            split=end - 30 * DAY,
            middle_end=end - 14 * DAY,
            alternatives=[
                ("Deployed breakout", None, True),
                ("Fixed exits", None, False),
                ("Volume and trend confirmation", ConfirmedBreakout(), True),
                *hourly_rules,
            ],
        )
    )
    result["1h"] = list(
        evaluate(
            hourly,
            initial,
            split=end - 60 * DAY,
            alternatives=hourly_rules,
        )
    )
    (args.archive / "study-results.json").write_text(
        json.dumps(result, indent=2, default=str) + "\n"
    )
    for interval in ("15m", "1h"):
        for row in result[interval]:
            if row["partition"] in ("full", "holdout"):
                print(
                    interval,
                    row["strategy"],
                    row["partition"],
                    row["cost"],
                    row["trades"],
                    f"{row['net']:+.4f}",
                )


if __name__ == "__main__":
    main()
