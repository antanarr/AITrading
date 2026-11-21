"""Acquire older USD-M candles for an offline cross-venue training prior."""

import argparse
import csv
import hashlib
import io
import json
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from .directional_positioning import MAPPINGS, epoch_hour
from .research_models import HOUR_MS, Bar, MarketHistory

SOURCE = "https://data.binance.vision/data/futures/um"
TRANSFER_END = date(2026, 5, 25)
MAX_ARCHIVE_BYTES = 256 * 1024
MAX_CSV_BYTES = 512 * 1024


@dataclass(frozen=True)
class Shard:
    symbol: str
    start: date
    end: date
    monthly: bool

    @property
    def group(self):
        return "monthly" if self.monthly else "daily"

    @property
    def stem(self):
        period = self.start.isoformat()[:7] if self.monthly else self.start.isoformat()
        return f"{self.symbol}-1h-{period}"

    @property
    def url(self):
        return f"{SOURCE}/{self.group}/klines/{self.symbol}/1h/{self.stem}.zip"

    def local(self, root, suffix):
        return root / self.symbol / self.group / f"{self.stem}.{suffix}"


def shard_plan(symbol, start, end):
    while start < end:
        month_end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        monthly = start.day == 1 and month_end <= end
        following = month_end if monthly else start + timedelta(days=1)
        yield Shard(symbol, start, following, monthly)
        start = following


def _download_shard(root, shard):
    archive, receipt = shard.local(root, "zip"), shard.local(root, "json")
    if archive.is_file() and receipt.is_file():
        return "cached"
    for attempt in range(3):
        try:
            with urlopen(shard.url, timeout=15) as response:
                header = response.headers.get("Last-Modified")
                payload = response.read(MAX_ARCHIVE_BYTES + 1)
            break
        except HTTPError as error:
            if error.code == 404:
                return "unavailable"
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
        except OSError:
            if attempt == 2:
                raise
        time.sleep(2**attempt)
    if len(payload) > MAX_ARCHIVE_BYTES or not zipfile.is_zipfile(io.BytesIO(payload)):
        raise ValueError(f"Unexpected archive response at {shard.url}")
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_suffix(".zip.tmp")
    temporary.write_bytes(payload)
    temporary.replace(archive)
    details = {
        "url": shard.url,
        "modified_header": header,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    temporary = receipt.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(details, indent=2) + "\n", encoding="utf-8")
    temporary.replace(receipt)
    return "downloaded"


def download_transfer(output, start, end, workers):
    if not start < end <= TRANSFER_END:
        raise ValueError("Choose an interval ending on or before the exclusive transfer cutoff")
    if not 1 <= workers <= 16:
        raise ValueError("Choose 1 to 16 workers")
    jobs = [job for symbol in MAPPINGS.values() for job in shard_plan(symbol, start, end)]

    def fetch(shard):
        try:
            status = _download_shard(output, shard)
        except (OSError, ValueError) as error:
            status = f"error: {error}"
        return shard.symbol, shard.stem, status

    output.mkdir(parents=True, exist_ok=True)
    received = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for symbol, name, status in pool.map(fetch, jobs):
            received.setdefault(symbol, {})[name] = status
    (output / "download-status.json").write_text(json.dumps(received, indent=2) + "\n")
    errors = 0
    for symbol, days in received.items():
        missing = sum(status == "unavailable" for status in days.values())
        failed = sum(status.startswith("error:") for status in days.values())
        errors += failed
        print(
            f"{symbol}: {len(days) - missing - failed} ready, {missing} unavailable, {failed} errors"
        )
    return int(bool(errors))


def _existing_shards(root, symbol, first_day, end_day):
    day = first_day
    while day < end_day:
        month_end = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
        monthly = Shard(symbol, day.replace(day=1), month_end, True)
        if month_end <= TRANSFER_END and monthly.local(root, "zip").is_file():
            yield monthly
            day = month_end
        else:
            tomorrow = day + timedelta(days=1)
            daily = Shard(symbol, day, tomorrow, False)
            if daily.local(root, "zip").is_file():
                yield daily
            day = tomorrow


def _shard_bars(root, shard, start, end):
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
            raise ValueError(f"Unexpected ZIP structure in {archive}")
        with zipped.open(name) as raw:
            rows = csv.reader(io.TextIOWrapper(raw, encoding="utf-8"))
            first_hour, last_hour = epoch_hour(shard.start), epoch_hour(shard.end)
            for number, row in enumerate(rows):
                if not row or (
                    number == 0
                    and row[:7]
                    == ["open_time", "open", "high", "low", "close", "volume", "close_time"]
                ):
                    continue
                if len(row) != 12:
                    raise ValueError(f"Unexpected USD-M kline column count in {archive}")
                stamp = int(row[0])
                hour = stamp // HOUR_MS
                if stamp % HOUR_MS or not first_hour <= hour < last_hour:
                    raise ValueError(f"Unexpected UTC candle open time in {archive}")
                if not start <= hour < end:
                    continue
                close_time = int(row[6])
                o, hi, lo, c, volume = (float(value) for value in row[1:6])
                if (
                    close_time != stamp + HOUR_MS - 1
                    or not all(isfinite(value) for value in (o, hi, lo, c, volume))
                    or not 0 < lo <= min(o, c) <= max(o, c) <= hi
                    or volume < 0
                ):
                    raise ValueError(f"Invalid OHLCV at UTC epoch hour {hour} in {archive}")
                yield hour, Bar(o, hi, lo, c, volume)


def load_transfer(
    path: Path, start: int, end: int, template: dict[str, MarketHistory]
) -> dict[str, MarketHistory]:
    """Load older non-overlapping Binance bars with matched Hyperliquid identities.

    This provides historical source-venue price observations only. It does not
    establish Hyperliquid trade availability, listing age, funding or fills.
    """
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 < start < end <= epoch_hour(TRANSFER_END)
    ):
        raise ValueError("Choose positive UTC hours within the earlier transfer cutoff")
    root = Path(path)
    if not root.is_dir():
        raise ValueError(f"Missing transfer archive: {root}")
    first_day = datetime.fromtimestamp(start * 3600, UTC).date()
    end_day = datetime.fromtimestamp((end - 1) * 3600, UTC).date() + timedelta(days=1)
    histories = {}
    for coin, symbol in MAPPINGS.items():
        if coin not in template:
            continue
        bars = {}
        for shard in _existing_shards(root, symbol, first_day, end_day):
            for hour, bar in _shard_bars(root, shard, start, end):
                previous = bars.setdefault(hour, bar)
                if previous != bar:
                    raise ValueError(f"Conflicting {symbol} candle at UTC epoch hour {hour}")
        if bars:
            identity = template[coin]
            histories[coin] = MarketHistory(
                coin=coin,
                decimals=identity.decimals,
                is_meme=identity.is_meme,
                listing_hour=None,
                bars=dict(sorted(bars.items())),
                funding={},
            )
    return histories


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=TRANSFER_END)
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args(argv)
    return download_transfer(args.output, args.start, args.end, args.workers)


if __name__ == "__main__":
    raise SystemExit(main())
