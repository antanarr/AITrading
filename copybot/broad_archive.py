"""Expand the pre-Reserve Hyperliquid archive using public historical endpoints."""

import argparse
import json
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from time import monotonic, sleep, time

import httpx

from .perps import API, CANDLE_INTERVALS
from .research_data import ARCHIVE_END_HOUR, _inventory, _metadata, load_archive
from .research_models import HOUR_MS, excluded_symbol

FUNDING_PAGE_HOURS = 480
MAX_WEIGHT = 1_090
ARCHIVE_START = date(2026, 5, 11)
SERIES = {"hour": "1h", "day": "1d", "quarter": "15m"}


def day_hour(day):
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) // 3600


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(path)


def copy_seed(seed, output, end):
    # Only named market series and metadata belong in the new archive.
    for path in sorted(seed.rglob("*.json")):
        prefix, _, suffix = path.stem.partition("-")
        target = (
            output / "taxonomy.json"
            if path.name == "taxonomy.json"
            else output / "seed" / path.relative_to(seed)
        )
        if target.is_file():
            continue
        if path.name in {"meta.json", "taxonomy.json"}:
            write(target, read(path))
        elif prefix in {*SERIES, "fund"}:
            coin = suffix.rsplit("-", 1)[0] if prefix == "fund" else suffix
            if excluded_symbol(coin):
                continue
            span = CANDLE_INTERVALS[SERIES[prefix]] if prefix in SERIES else 0
            stamp = "t" if span else "time"
            rows = [
                row
                for row in read(path)
                if type(row.get(stamp)) is int
                and row[stamp] < end
                and (not span or (row[stamp] + span <= end and row.get("T", end) < end))
            ]
            write(target, rows)


class HistorySource:
    def __init__(self, client, weight_per_minute):
        self.client = client
        self.weight_per_second = weight_per_minute / 60
        self.available = monotonic()

    def fetch(self, body, weight):
        for attempt in range(4):
            sleep(max(0.0, self.available - monotonic()))
            self.available = monotonic() + weight / self.weight_per_second
            try:
                response = self.client.post(f"{API}/info", json=body)
                if response.status_code == 422:
                    return None
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, list):
                    raise ValueError("Expected a public historical list response")
                return data
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                if attempt == 3 or status not in (429, 500, 502, 503, 504):
                    raise
                delay = 61 if status == 429 else 2**attempt
                print(
                    f"Hyperliquid historical request returned {status}, retrying in {delay}s",
                    flush=True,
                )
            except httpx.TransportError:
                if attempt == 3:
                    raise
                delay = 2**attempt
            self.available = max(self.available, monotonic() + delay)

    def candles(self, coin, interval, start, end):
        span = CANDLE_INTERVALS[interval]
        count = min(5_000, (end - start + span - 1) // span)
        rows = self.fetch(
            {
                "type": "candleSnapshot",
                "req": {"coin": coin, "interval": interval, "startTime": start, "endTime": end - 1},
            },
            20 + (count + 59) // 60,
        )
        if rows is None:
            return None
        return [row for row in rows if start <= row["t"] and row["T"] < end]

    def funding(self, coin, start, end):
        count = (end - start + HOUR_MS - 1) // HOUR_MS
        rows = self.fetch(
            {
                "type": "fundingHistory",
                "coin": coin,
                "startTime": start,
                "endTime": end - 1,
            },
            20 + (count + 19) // 20,
        )
        if rows is None:
            return None
        return [row for row in rows if start <= row["time"] < end]


def candidates(census, inventory, cutoff):
    eligible = []
    for order, (coin, contract) in enumerate(census.items()):
        if (
            excluded_symbol(coin)
            or ":" in coin
            or Path(coin).name != coin
            or type(contract["szDecimals"]) is not int
            or not 0 <= contract["szDecimals"] <= 6
        ):
            continue
        days = [
            row
            for path in inventory.get(coin, {}).get("day", ())
            for row in read(path)
            if row["T"] < cutoff
        ]
        # This is a download priority only. Rolling historical eligibility is
        # decided separately on each forecast hour by the research harness.
        peak_volume = max((float(row["v"]) * float(row["c"]) for row in days), default=0)
        has_hourly = any(bool(read(path)) for path in inventory.get(coin, {}).get("hour", ()))
        eligible.append(
            (not has_hourly, bool(contract.get("isDelisted")), -peak_volume, order, coin)
        )
    return [coin for *_, coin in sorted(eligible)]


def fill_candles(source, output, inventory, coin, start, end):
    current = inventory.get(coin, {})
    for prefix, interval in (("hour", "1h"), ("day", "1d")):
        if any(bool(read(path)) for path in current.get(prefix, ())):
            continue
        target = output / "native" / f"{prefix}-{coin}.json"
        if target.is_file():
            continue
        left = start if prefix == "hour" else start - 366 * 24 * HOUR_MS
        rows = source.candles(coin, interval, left, end)
        write(target, rows or [])
        print(
            f"{coin} {interval}: {len(rows or [])}" + (" unavailable" if rows is None else ""),
            flush=True,
        )


def fill_funding(source, output, history, end, funding_days):
    coin = history.coin
    left = min(history.bars) + 1
    stop = min(end, max(history.bars) + 1)
    if funding_days is not None:
        left = max(left, stop - funding_days * 24)
    for right in range(stop, left, -FUNDING_PAGE_HOURS):
        first = max(left, right - FUNDING_PAGE_HOURS)
        missing = set(range(first, right)) - history.funding.keys()
        if not missing:
            continue
        begin = min(missing)
        finish = max(missing) + 1
        target = output / "native" / f"fund-{coin}-{begin}.json"
        if target.is_file():
            known = read(target)
            if not known or {row["time"] // HOUR_MS for row in known} >= missing:
                continue
        rows = source.funding(coin, begin * HOUR_MS, finish * HOUR_MS)
        write(target, rows or [])
        print(
            f"{coin} funding {begin}–{finish}: {len(rows or [])}"
            + (" unavailable" if rows is None else ""),
            flush=True,
        )


def summarize(output, end):
    histories, receipt = load_archive(output, end, include_quarters=True)
    statuses = Counter()
    groups = defaultdict(list)
    for coin, row in receipt["markets"].items():
        status = row["status"]
        if status == "available":
            status = (
                "incomplete_hourly"
                if row["hourly"]["interior_gaps"]
                else ("incomplete_funding" if row["missing_funding_for_hourly"] else "complete")
            )
        statuses[status] += 1
        groups[status].append(coin)
    details = {
        "end_hour_exclusive": end,
        "native_history_markets": len(histories),
        "status_counts": dict(statuses),
        "market_groups": dict(groups),
        "markets": receipt["markets"],
    }
    write(output / "broad-coverage.json", details)
    print(
        f"Native markets with hourly history: {len(histories)}. Status: {dict(statuses)}",
        flush=True,
    )
    return histories


def run(args):
    start, end = day_hour(args.start), ARCHIVE_END_HOUR
    if not 0 < start < end <= int(time()) // 3600:
        raise ValueError(
            "Choose completed historical acquisition dates within the development cutoff"
        )
    if args.seed.resolve() == args.output.resolve() or args.output.resolve().is_relative_to(
        args.seed.resolve()
    ):
        raise ValueError("Use a separate broad-archive directory")
    if not 60 <= args.rate_weight <= MAX_WEIGHT:
        raise ValueError(f"Expected a request budget between 60 and {MAX_WEIGHT} weight per minute")
    if args.limit is not None and args.limit <= 0:
        raise ValueError("Expected a positive market limit")
    if args.funding_days is not None and args.funding_days <= 0:
        raise ValueError("Expected a positive funding window")
    copy_seed(args.seed, args.output, end * HOUR_MS)
    census, _ = _metadata(args.output)
    inventory = _inventory(args.output, False)
    ranked = candidates(census, inventory, end * HOUR_MS)
    if args.coins:
        coins = list(dict.fromkeys(args.coins))
        unknown = set(coins) - set(ranked)
        if unknown:
            raise ValueError(f"Unsupported native assets: {', '.join(sorted(unknown))}")
    else:
        coins = ranked
    coins = coins[: args.limit]
    with httpx.Client(timeout=25) as client:
        source = HistorySource(client, args.rate_weight)
        if args.phase in ("both", "candles"):
            for coin in coins:
                fill_candles(source, args.output, inventory, coin, start * HOUR_MS, end * HOUR_MS)
        histories = summarize(args.output, end)
        if args.phase in ("funding", "both"):
            for coin in coins:
                if coin in histories:
                    fill_funding(source, args.output, histories[coin], end, args.funding_days)
            summarize(args.output, end)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("seed", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--start", type=date.fromisoformat, default=ARCHIVE_START)
    parser.add_argument(
        "--phase", choices=("candles", "funding", "both", "summary"), default="both"
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--coins", nargs="+", help="Explicit native coins in download order")
    parser.add_argument("--funding-days", type=int)
    parser.add_argument("--rate-weight", type=int, default=MAX_WEIGHT)
    run(parser.parse_args(argv))


if __name__ == "__main__":
    main()
