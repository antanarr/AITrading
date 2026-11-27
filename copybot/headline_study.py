"""Replay timestamped public releases on a sealed pre-Reserve market tape."""

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from itertools import pairwise
from math import sqrt
from pathlib import Path
from statistics import mean

import httpx

from .directional_context import EASTERN
from .meme_study import write_json
from .perps import Feed, dex_for, round_price
from .research_data import ARCHIVE_END_HOUR, _bar, _funding
from .research_execution import BASE, MIN_NOTIONAL, PER_BPS, QUARTER_MS, STRESS
from .research_models import HOUR_MS
from .research_signals import _true_range

MINUTE_MS = 60_000
CUTOFF = ARCHIVE_END_HOUR * HOUR_MS
ENTRY_WINDOW = 2 * HOUR_MS
HOLD_TIME = 2 * HOUR_MS
BREAKOUT_WINDOW = 6 * HOUR_MS
ATR_WINDOW = 14 * QUARTER_MS
STOP_ATR = 2
MAKER_TARGET_ATR = 2.5
ENTRY_CAP_BPS = 40
CAPITAL = 500
RISK = CAPITAL * 0.01
MAX_NOTIONAL = CAPITAL * 0.25
NATIVE_MARKET = "ETH"
NVIDIA_MARKET = "xyz:NVDA"
HEADLINE_DELAYS = (1, 5, 15)


@dataclass(frozen=True)
class Release:
    name: str
    eastern: str
    coin: str
    source: str
    source_in_current_feed: bool = False
    control: bool = False

    @property
    def published(self):
        moment = datetime.fromisoformat(self.eastern).replace(tzinfo=EASTERN)
        return int(moment.timestamp() * 1000)


RELEASES = (
    Release(
        "September CPI",
        "2026-10-14T08:30",
        NATIVE_MARKET,
        "https://www.bls.gov/news.release/archives/cpi_10142026.htm",
    ),
    Release(
        "October FOMC",
        "2026-10-28T14:00",
        NATIVE_MARKET,
        "https://www.federalreserve.gov/newsevents/pressreleases/monetary20261028a.htm",
        True,
    ),
    Release(
        "October jobs",
        "2026-11-06T08:30",
        NATIVE_MARKET,
        "https://www.bls.gov/news.release/archives/empsit_11062026.htm",
    ),
    Release(
        "October CPI",
        "2026-11-10T08:30",
        NATIVE_MARKET,
        "https://www.bls.gov/news.release/archives/cpi_11102026.htm",
    ),
    Release(
        "NVIDIA CEO transition",
        "2026-11-12T08:48",
        NVIDIA_MARKET,
        "https://www.globenewswire.com/news-release/2026/11/12/3401635/0/en/nvidia-announces-ceo-transition.html",
    ),
    Release(
        "NVIDIA earnings",
        "2026-11-17T16:20",
        NVIDIA_MARKET,
        "https://www.globenewswire.com/news-release/2026/11/17/3405200/0/en/nvidia-announces-financial-results-for-third-quarter-fiscal-202.html",
    ),
)


def controls(release):
    day = datetime.fromisoformat(release.eastern)
    known_dates = {event.eastern[:10] for event in RELEASES}
    for shift in (1, 7):
        prior = day - timedelta(days=shift)
        while prior.weekday() >= 5 or prior.date().isoformat() in known_dates:
            prior -= timedelta(days=1)
        yield Release(
            f"{release.name} control {shift}",
            prior.isoformat(timespec="minutes"),
            release.coin,
            "Same clock on an earlier weekday",
            control=True,
        )


def bounds(release, step=QUARTER_MS):
    anchor = release.published // step * step
    start = anchor - BREAKOUT_WINDOW - step
    end = anchor + ENTRY_WINDOW + HOLD_TIME + 2 * step
    if not 0 < start < end <= CUTOFF:
        raise ValueError("Headline replay must stay inside Development")
    return start, end


def capture(releases, step):
    with httpx.Client(timeout=25) as client:
        feed = Feed(client)
        precision = {
            row["name"]: row["szDecimals"]
            for dex in {dex_for(release.coin) for release in releases}
            for row in feed.info("meta", dex=dex)["universe"]
            if row["name"] in {NATIVE_MARKET, NVIDIA_MARKET}
        }
        result = {}
        for release in releases:
            start, end = bounds(release, step)
            result[release.name] = {
                "release": asdict(release),
                "decimals": precision[release.coin],
                "candles": feed.info(
                    "candleSnapshot",
                    req={
                        "coin": release.coin,
                        "interval": f"{step // MINUTE_MS}m",
                        "startTime": start,
                        "endTime": end - 1,
                    },
                ),
                "funding": feed.funding_history(release.coin, start, end - 1),
            }
        return result


def market_tape(release, snapshot, step):
    start, end = bounds(release, step)
    if snapshot["release"] != asdict(release):
        raise ValueError(f"Snapshot release mismatch for {release.name}")
    decimals = snapshot["decimals"]
    if type(decimals) is not int or not 0 <= decimals <= 6:
        raise ValueError(f"Invalid size precision for {release.coin}")
    candles, funding = {}, {}
    for row in snapshot["candles"]:
        stamp = row["t"]
        if not start <= stamp < end or stamp in candles:
            raise ValueError(f"Unexpected candle timestamp for {release.name}")
        candles[stamp] = _bar(row, release.coin, f"{step // MINUTE_MS}m", step)
    for row in snapshot["funding"]:
        stamp = row["time"]
        if not start <= stamp < end or stamp // HOUR_MS in funding:
            raise ValueError(f"Unexpected funding timestamp for {release.name}")
        funding[stamp // HOUR_MS] = _funding(row, release.coin)
    if set(candles) != set(range(start, end, step)):
        raise ValueError(f"Incomplete candle tape for {release.name}")
    return candles, funding, decimals


def current_availability(release, context_path):
    if not release.source_in_current_feed:
        return None
    with context_path.open(encoding="utf-8") as feed:
        for line in feed:
            row = json.loads(line)
            stamp = row["available_hour"] * HOUR_MS
            if (
                release.published <= stamp < min(CUTOFF, release.published + ENTRY_WINDOW)
                and row["source"] == release.source
                and "news_fomc_rate_direction" in row["features"]
            ):
                return stamp
    raise ValueError(f"No archived rate context for {release.name}")


def choose_signal(candles, published, known=None, *, chart=False, step=QUARTER_MS):
    anchor = published // step * step
    before = candles[anchor - step].close
    atr_length = ATR_WINDOW // step + 1
    for stamp in range(anchor, anchor + ENTRY_WINDOW, step):
        close_time = stamp + step
        if close_time > published + ENTRY_WINDOW or (
            not chart and (known is None or close_time < known)
        ):
            continue
        prior = [candles[t] for t in range(stamp - BREAKOUT_WINDOW, stamp, step)]
        bar = candles[stamp]
        if bar.volume <= 0:
            continue
        atr = mean(_true_range(bar, prev) for prev, bar in pairwise(prior[-atr_length:]))
        move = bar.close - bar.open
        if atr <= 0 or abs(move) < atr * (1 if chart else 0.75):
            continue
        side = 1 if move > 0 else -1
        if chart:
            boundary = max(bar.high for bar in prior) if side > 0 else min(bar.low for bar in prior)
            if side * (bar.close - boundary) <= 0:
                continue
        elif side * (bar.close - before) < atr * 0.75:
            continue
        return {"time_ms": close_time, "side": side, "atr": atr, "reference": bar.close}
    return None


def exit_trade(candles, start, step, side, stop, target):
    end = start + HOLD_TIME
    for stamp in range(start, end, step):
        bar = candles[stamp]
        if bar.volume <= 0:
            continue
        adverse, favorable = (bar.low, bar.high) if side > 0 else (bar.high, bar.low)
        if side * (bar.open - stop) <= 0:
            return stamp, bar.open, "gap_stop"
        if side * (adverse - stop) <= 0:
            return stamp + step, stop, "stop"
        if target is not None and stamp > start and side * (favorable - target) > 0:
            return stamp + step, target, "maker_target"
    if candles[end].volume <= 0:
        raise ValueError("No observed trade in the scheduled exit candle")
    return end, candles[end].open, "time"


def simulate(
    candles,
    funding,
    decimals,
    signal,
    costs,
    fee_multiplier,
    step=QUARTER_MS,
    *,
    maker_target=False,
):
    if signal is None:
        return {"status": "no_signal", "net": 0.0}
    start, side, atr, reference = (signal[key] for key in ("time_ms", "side", "atr", "reference"))
    first = candles[start]
    entry = first.open * (1 + side * (costs.half_spread_bps + costs.entry_impact_bps) / PER_BPS)
    cap = reference * (1 + side * ENTRY_CAP_BPS / PER_BPS)
    cap = float(round_price(Decimal(str(cap)), decimals, ROUND_DOWN if side > 0 else ROUND_CEILING))
    result = {"signal": signal, "net": 0.0}
    if first.volume <= 0 or side * (entry - cap) > 0:
        return {**result, "status": "ioc_unfilled", "opening_price": first.open}
    scaled_atr = atr * sqrt(QUARTER_MS / step)
    risk_distance = STOP_ATR * scaled_atr
    units = Decimal(str(min(MAX_NOTIONAL / entry, RISK / risk_distance))).quantize(
        Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN
    )
    size = float(units)
    if size * entry < MIN_NOTIONAL:
        return {**result, "status": "min_notional"}
    stop = entry - side * risk_distance
    if stop <= 0:
        return {**result, "status": "invalid_stop"}
    target = None
    if maker_target:
        level = Decimal(str(entry + side * MAKER_TARGET_ATR * scaled_atr))
        if level > 0:
            target = float(round_price(level, decimals, ROUND_CEILING if side > 0 else ROUND_DOWN))
    exit_time, exit_reference, reason = exit_trade(candles, start, step, side, stop, target)
    if reason == "maker_target":
        exit_price, exit_fee = exit_reference, costs.maker_fee_bps
    else:
        exit_price = exit_reference * (
            1 - side * (costs.half_spread_bps + costs.exit_impact_bps) / PER_BPS
        )
        exit_fee = costs.taker_fee_bps
    fees = (entry * costs.taker_fee_bps + exit_price * exit_fee) * size * fee_multiplier / PER_BPS
    slippage = side * ((entry - first.open) + (exit_reference - exit_price)) * size
    gross = side * (exit_reference - first.open) * size
    carry = 0.0
    for hour in range(start // HOUR_MS, (exit_time - 1) // HOUR_MS + 1):
        settlement = hour * HOUR_MS
        if start <= settlement < exit_time:
            payment = funding.get(hour)
            if payment is None:
                raise ValueError(f"Missing funding settlement for open position at {hour}")
            if start <= payment.time_ms < exit_time:
                carry -= side * size * candles[settlement - step].close * payment.rate
    return {
        **result,
        "status": "filled",
        "net": gross + carry - fees - slippage,
        "gross": gross,
        "funding": carry,
        "fees": fees,
        "slippage": slippage,
        "entry": entry,
        "size": size,
        "exit": exit_price,
        "exit_ms": exit_time,
        "exit_reason": reason,
    }


def replay(release, snapshot, context_path, step, maker_target):
    candles, funding, decimals = market_tape(release, snapshot, step)
    published = release.published
    current = current_availability(release, context_path)
    baseline = choose_signal(candles, published, chart=True, step=step)
    current_signal = choose_signal(candles, published, current, step=step)
    signals = {"chart": baseline, "current_feed": first_signal(baseline, current_signal)}
    for minutes in HEADLINE_DELAYS:
        headline = choose_signal(candles, published, published + minutes * MINUTE_MS, step=step)
        signals[f"headline_{minutes}m"] = headline
        signals[f"chart_plus_{minutes}m"] = first_signal(baseline, headline)
    anchor = published // step * step
    prior = candles[anchor - step].close
    reaction = [candles[t] for t in range(anchor, anchor + ENTRY_WINDOW, step)]
    # The range includes pre-release trades when the release falls inside a candle.
    extremes = {
        "high_pct": 100 * (max(bar.high for bar in reaction) / prior - 1),
        "low_pct": 100 * (min(bar.low for bar in reaction) / prior - 1),
    }
    return {
        "release": asdict(release),
        "published_ms": published,
        "current_feed_ms": current,
        "reaction_range": extremes,
        "policies": {
            name: {
                cost.name: simulate(
                    candles,
                    funding,
                    decimals,
                    signal,
                    cost,
                    2 if release.coin == NVIDIA_MARKET else 1,
                    step,
                    maker_target=maker_target,
                )
                for cost in (BASE, STRESS)
            }
            for name, signal in signals.items()
        },
    }


def first_signal(chart, headline):
    return min(
        (row for row in (chart, headline) if row is not None),
        key=lambda row: row["time_ms"],
        default=None,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("context", type=Path)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--capture", action="store_true", help="Fetch public pre-Reserve market data"
    )
    parser.add_argument("--nvidia-5m", action="store_true", help="Replay only NVIDIA at 5 minutes")
    parser.add_argument("--maker-target", action="store_true", help="Add a resting 2.5 ATR target")
    args = parser.parse_args()
    if args.nvidia_5m:
        step = 5 * MINUTE_MS
        releases = [release for release in RELEASES if release.coin == NVIDIA_MARKET]
    else:
        step = QUARTER_MS
        releases = [row for release in RELEASES for row in (release, *controls(release))]
    if args.capture:
        if args.snapshot.exists():
            parser.error("Snapshot already exists")
        write_json(args.snapshot, capture(releases, step))
    tape = json.loads(args.snapshot.read_text(encoding="utf-8"))
    rows = [
        replay(release, tape[release.name], args.context, step, args.maker_target)
        for release in releases
    ]
    write_json(
        args.output,
        {
            "public_market_endpoint": "https://api.hyperliquid.xyz/info",
            "cutoff_ms": CUTOFF,
            "capital": CAPITAL,
            "bar_minutes": step // MINUTE_MS,
            "maker_target_atr": MAKER_TARGET_ATR if args.maker_target else None,
            "events": rows,
        },
    )


if __name__ == "__main__":
    main()
