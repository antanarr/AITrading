"""Acquire independent Binance USD-M candles and funding for regime research."""

import argparse
import csv
import hashlib
import io
import json
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from math import isfinite
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from .directional_transfer import (
    MAX_ARCHIVE_BYTES,
    MAX_CSV_BYTES,
    SOURCE,
    Shard,
    _download_shard,
    _shard_bars,
    epoch_hour,
    shard_plan,
)
from .research_models import HOUR_MS, Funding, MarketHistory

START = date(2024, 1, 1)
END = date(2026, 5, 24)
REST_FUNDING = "https://fapi.binance.com/fapi/v1/fundingRate"
SYMBOLS = {
    coin: f"{coin}USDT"
    for coin in (
        "ETH",
        "SOL",
        "XRP",
        "BNB",
        "DOGE",
        "ADA",
        "LINK",
        "AVAX",
        "LTC",
        "SUI",
        "BCH",
        "NEAR",
        "UNI",
    )
}
FUNDING_HEADER = ["calc_time", "funding_interval_hours", "last_funding_rate"]
# Official Binance USD-M LOT_SIZE snapshot, retrieved 2026-11-21.
LOT_DECIMALS = {
    "ETH": 3,
    "SOL": 2,
    "XRP": 1,
    "BNB": 2,
    "DOGE": 0,
    "ADA": 0,
    "LINK": 2,
    "AVAX": 0,
    "LTC": 3,
    "SUI": 1,
    "BCH": 3,
    "NEAR": 0,
    "UNI": 0,
}
LOT_SOURCE = "https://fapi.binance.com/fapi/v1/exchangeInfo"
FUNDING_CLOCK_TOLERANCE_MS = 5_000


@dataclass(frozen=True)
class FundingShard(Shard):
    @property
    def stem(self):
        return f"{self.symbol}-fundingRate-{self.start.isoformat()[:7]}"

    @property
    def url(self):
        return f"{SOURCE}/monthly/fundingRate/{self.symbol}/{self.stem}.zip"


@dataclass(frozen=True)
class Settlement:
    stamp: int
    rate: float
    interval_hours: int | None

    @property
    def hour(self):
        return self.stamp // HOUR_MS


def monthly_funding_start(symbol, start, end):
    for shard in shard_plan(symbol, start.replace(day=1), end):
        if shard.monthly:
            yield FundingShard(shard.symbol, shard.start, shard.end, True)


def _json_file(path, value):
    temporary = path.with_suffix(".json.tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(value, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(path)


def _partial_receipt(root, symbol, start, end):
    return root / symbol / "partial-funding" / f"{start.isoformat()}_{end.isoformat()}.json"


def _funding_url(symbol, start, end):
    first, stop = epoch_hour(start) * HOUR_MS, epoch_hour(end) * HOUR_MS
    query = urlencode({"symbol": symbol, "startTime": first, "endTime": stop - 1, "limit": 1000})
    return f"{REST_FUNDING}?{query}"


def download_partial_funding(output, symbol, start, end):
    receipt = _partial_receipt(output, symbol, start, end)
    if receipt.is_file():
        return "cached"
    first, stop = epoch_hour(start) * HOUR_MS, epoch_hour(end) * HOUR_MS
    url = _funding_url(symbol, start, end)
    with urlopen(url, timeout=20) as response:
        payload = response.read(MAX_CSV_BYTES + 1)
    if len(payload) > MAX_CSV_BYTES:
        raise ValueError(f"Unexpected oversized partial funding response for {symbol}")
    parsed = json.loads(payload)
    if not isinstance(parsed, list) or len(parsed) >= 1000:
        raise ValueError(f"Unexpected or truncated partial funding response for {symbol}")
    if any(row["symbol"] != symbol or not first <= row["fundingTime"] < stop for row in parsed):
        raise ValueError(f"Unexpected REST funding symbol or timestamp for {symbol}")
    _json_file(receipt, {"url": url, "payload": parsed})
    return "downloaded" if parsed else "unavailable"


def copy_candle(seed, output, shard):
    if seed is None:
        return False
    archive, receipt = shard.local(seed, "zip"), shard.local(seed, "json")
    if not archive.is_file() or not receipt.is_file():
        return False
    target = shard.local(output, "zip")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".zip.tmp")
    temporary.write_bytes(archive.read_bytes())
    temporary.replace(target)
    _json_file(shard.local(output, "json"), json.loads(receipt.read_text(encoding="utf-8")))
    return True


def download(output, coins, seed, workers):
    if not 1 <= workers <= 16:
        raise ValueError("Choose 1 to 16 workers")
    monthly = [
        shard
        for coin in coins
        for shard in (
            *shard_plan(SYMBOLS[coin], START, END),
            *monthly_funding_start(SYMBOLS[coin], START, END),
        )
    ]

    def fetch(shard):
        try:
            if type(shard) is Shard and not shard.local(output, "zip").is_file():
                if copy_candle(seed, output, shard):
                    return shard.symbol, shard.stem, "reused"
            return shard.symbol, shard.stem, _download_shard(output, shard)
        except (OSError, ValueError, KeyError) as error:
            return shard.symbol, shard.stem, f"error: {error}"

    results = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index, (symbol, stem, status) in enumerate(pool.map(fetch, monthly), start=1):
            results.setdefault(symbol, {})[stem] = status
            if index % 100 == 0 or index == len(monthly):
                print(f"{index}/{len(monthly)} public ZIPs checked", flush=True)

    partial_start = END.replace(day=1)
    for coin in coins:
        symbol = SYMBOLS[coin]
        try:
            status = download_partial_funding(output, symbol, partial_start, END)
        except (OSError, ValueError, KeyError) as error:
            status = f"error: {error}"
        results[symbol]["partial-funding"] = status
    _json_file(output / "download-status.json", results)
    for symbol, records in results.items():
        print(symbol, dict(Counter(records.values())))
    return int(
        any(value.startswith("error:") for rows in results.values() for value in rows.values())
    )


def read_monthly_funding(root, shard):
    archive, receipt = shard.local(root, "zip"), shard.local(root, "json")
    metadata = json.loads(receipt.read_text(encoding="utf-8"))
    payload = archive.read_bytes()
    if (
        metadata["url"] != shard.url
        or len(payload) > MAX_ARCHIVE_BYTES
        or hashlib.sha256(payload).hexdigest() != metadata["sha256"]
    ):
        raise ValueError(f"Bad provenance, size or checksum for {archive}")
    with zipfile.ZipFile(io.BytesIO(payload)) as zipped:
        name = f"{shard.stem}.csv"
        if zipped.namelist() != [name] or zipped.getinfo(name).file_size > MAX_CSV_BYTES:
            raise ValueError(f"Unexpected funding ZIP structure in {archive}")
        with zipped.open(name) as raw:
            rows = csv.reader(io.TextIOWrapper(raw, encoding="utf-8"))
            if next(rows, None) != FUNDING_HEADER:
                raise ValueError(f"Unexpected funding CSV schema in {archive}")
            for stamp, interval, rate in rows:
                result = Settlement(int(stamp), float(rate), int(interval))
                if not epoch_hour(shard.start) <= result.hour < epoch_hour(shard.end):
                    raise ValueError(f"Funding settlement outside its source month in {archive}")
                yield result


def read_partial_funding(root, symbol, start, end):
    path = _partial_receipt(root, symbol, start, end)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data["url"] != _funding_url(symbol, start, end) or not 0 < len(data["payload"]) < 1000:
        raise ValueError(f"Bad or incomplete funding receipt for {symbol}")
    for row in data["payload"]:
        if row["symbol"] != symbol or row.get("rateType", "Regular") != "Regular":
            raise ValueError(f"Unexpected REST funding market or settlement type for {symbol}")
        stamp = row["fundingTime"]
        if type(stamp) is not int or not epoch_hour(start) <= stamp // HOUR_MS < epoch_hour(end):
            raise ValueError(f"REST funding settlement outside its requested interval for {symbol}")
        yield Settlement(stamp, float(row["fundingRate"]), None)


def verify_settlements(records, start, end):
    """Reject funding holes before the simulator treats event-free hours as nonpaying."""
    if not records or records[0].interval_hours is None:
        raise ValueError("Missing prior evidence of funding cadence")
    previous = None
    cadence = records[0].interval_hours
    observed = {}
    for event in records:
        if (
            not start <= event.hour < end
            or event.stamp % HOUR_MS > FUNDING_CLOCK_TOLERANCE_MS
            or not isfinite(event.rate)
            or (event.interval_hours is not None and not 1 <= event.interval_hours <= 24)
        ):
            raise ValueError(f"Invalid funding observation at {event.hour}")
        if previous is None:
            if event.hour - start >= cadence:
                raise ValueError("Funding coverage starts too late")
        elif event.hour - previous not in (cadence, event.interval_hours):
            raise ValueError(f"Unexplained funding cadence between {previous} and {event.hour}")
        observed[event.hour] = Funding(event.stamp, event.rate)
        previous = event.hour
        cadence = event.interval_hours or cadence
    if previous + cadence < end:
        raise ValueError("Funding coverage stops before the candle cutoff")
    return observed


def inspect_binance_regime(path: Path, *, coins=tuple(SYMBOLS)):
    """Return candle/funding histories and per-symbol availability receipts."""
    root = Path(path)
    if not root.is_dir():
        raise ValueError(f"Missing Binance regime archive: {root}")
    start, end = epoch_hour(START), epoch_hour(END)
    histories, report = {}, {}
    for coin in coins:
        symbol = SYMBOLS[coin]
        try:
            bars = {}
            for shard in shard_plan(symbol, START, END):
                for hour, bar in _shard_bars(root, shard, start, end):
                    if bars.setdefault(hour, bar) != bar:
                        raise ValueError(f"Conflicting bar at {hour}")
            if len(bars) != end - start:
                raise ValueError(f"Incomplete candle coverage: {len(bars)} of {end - start} hours")
            settlements = [
                event
                for shard in monthly_funding_start(symbol, START, END)
                for event in read_monthly_funding(root, shard)
            ]
            settlements.extend(read_partial_funding(root, symbol, END.replace(day=1), END))
            settlements.sort(key=lambda event: event.stamp)
            fund = verify_settlements(settlements, start, end)
        except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile) as error:
            report[coin] = {"status": "incomplete", "reason": str(error)}
            continue
        histories[coin] = MarketHistory(coin, LOT_DECIMALS[coin], coin == "DOGE", None, bars, fund)
        report[coin] = {
            "status": "complete",
            "bars": len(bars),
            "settlements": len(fund),
            "nonpaying_hours": len(bars) - len(fund),
            "interval_histogram": dict(
                Counter(next_event.hour - event.hour for event, next_event in pairwise(settlements))
            ),
            "first_hour": start,
            "end_hour_exclusive": end,
            "lot_size_decimals": LOT_DECIMALS[coin],
        }
    return histories, report


def load_binance_regime(path: Path, start: int, end: int, *, coins=tuple(SYMBOLS)):
    """Return the independently validated USD-M series clipped to a replay window."""
    left, right = epoch_hour(START), epoch_hour(END)
    if type(start) is not int or type(end) is not int or not left <= start < end <= right:
        raise ValueError("Select UTC epoch hours strictly inside the independent Binance window")
    histories, _ = inspect_binance_regime(path, coins=coins)
    if not histories:
        raise ValueError("No Binance markets have complete candle and funding coverage")
    for coin, history in histories.items():
        if start == left and end == right:
            continue
        histories[coin] = MarketHistory(
            coin,
            history.decimals,
            history.is_meme,
            history.listing_hour,
            {hour: bar for hour, bar in history.bars.items() if start <= hour < end},
            {hour: row for hour, row in history.funding.items() if start <= hour < end},
        )
    return histories


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--reuse", type=Path, help="Existing Binance directional transfer archives")
    parser.add_argument("--coins", nargs="+", choices=tuple(SYMBOLS), default=list(SYMBOLS))
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument(
        "--verify", action="store_true", help="Validate local archive without network requests"
    )
    options = parser.parse_args(argv)
    if options.verify:
        histories, report = inspect_binance_regime(options.output, coins=options.coins)
        _json_file(
            options.output / "regime-coverage.json",
            {
                "source": SOURCE,
                "final_partial_month_funding_source": REST_FUNDING,
                "lot_size_source": LOT_SOURCE,
                "lot_size_note": "Current LOT_SIZE snapshot, historical specification revisions unverified.",
                "start": START.isoformat(),
                "end_exclusive": END.isoformat(),
                "markets": report,
            },
        )
        print(f"{len(histories)} complete Binance USD-M markets")
        for coin, row in report.items():
            if row["status"] != "complete":
                print(f"{coin}: {row['reason']}")
        return int(len(histories) != len(options.coins))
    return download(
        options.output, list(dict.fromkeys(options.coins)), options.reuse, options.workers
    )


if __name__ == "__main__":
    raise SystemExit(main())
