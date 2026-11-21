"""Offline Binance USD-M positioning history for matched Hyperliquid markets."""

import argparse
import hashlib
import io
import json
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import numpy as np
import pandas as pd

from .research_data import ARCHIVE_END_HOUR

SOURCE = "https://data.binance.vision/data/futures/um/daily/metrics"
MAPPINGS = {
    "DOGE": "DOGEUSDT",
    "FARTCOIN": "FARTCOINUSDT",
    "kPEPE": "1000PEPEUSDT",
    "TRUMP": "TRUMPUSDT",
    "PENGU": "PENGUUSDT",
    "SPX": "SPXUSDT",
    "kBONK": "1000BONKUSDT",
    "USELESS": "USELESSUSDT",
    "WIF": "WIFUSDT",
    "kSHIB": "1000SHIBUSDT",
    "POPCAT": "POPCATUSDT",
}
FIELDS = {
    "sum_open_interest": "oi_units",
    "sum_open_interest_value": "oi_dollars",
    "count_toptrader_long_short_ratio": "top_accounts",
    "sum_toptrader_long_short_ratio": "top_positions",
    "count_long_short_ratio": "all_accounts",
    "sum_taker_long_short_vol_ratio": "taker",
}
MEASUREMENT_LAG = timedelta(minutes=15)
PUBLICATION_MAX_AGE = timedelta(hours=48)
MEASUREMENT_MAX_AGE = timedelta(minutes=30)
CHANGE_HOURS = (4, 24)
FEATURE_NAMES = [
    "position_oi_usdt_log",
    "position_top_accounts_log_ratio",
    "position_top_positions_log_ratio",
    "position_all_accounts_log_ratio",
    "position_taker_imbalance_1h",
    *(f"position_oi_change_{hours}h" for hours in CHANGE_HOURS),
]
MAX_ZIP = 256 * 1024
MAX_CSV = 512 * 1024


def epoch_hour(day):
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) // 3600


def source_file(symbol, day):
    return f"{symbol}-metrics-{day.isoformat()}"


def object_paths(root, symbol, day):
    stem = root / symbol / source_file(symbol, day)
    return stem.with_suffix(".zip"), stem.with_suffix(".json")


def download_one(root, symbol, day):
    archive, receipt = object_paths(root, symbol, day)
    if archive.is_file() and receipt.is_file():
        return "cached"
    source = f"{SOURCE}/{symbol}/{archive.name}"
    for attempt in range(3):
        try:
            with urlopen(source, timeout=20) as response:
                modified = parsedate_to_datetime(response.headers["Last-Modified"]).astimezone(UTC)
                payload = response.read(MAX_ZIP + 1)
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
    if len(payload) > MAX_ZIP or not zipfile.is_zipfile(io.BytesIO(payload)):
        raise ValueError(f"Unexpected archive response at {source}")
    archive.parent.mkdir(parents=True, exist_ok=True)
    temp = archive.with_suffix(".zip.tmp")
    temp.write_bytes(payload)
    temp.replace(archive)
    details = {
        "url": source,
        "modified_utc": modified.isoformat(),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    temp = receipt.with_suffix(".json.tmp")
    temp.write_text(json.dumps(details, indent=2) + "\n", encoding="utf-8")
    temp.replace(receipt)
    return "downloaded"


def download(root, start, end, coins, workers):
    if not start < end or epoch_hour(end) > ARCHIVE_END_HOUR:
        raise ValueError("Choose completed development dates (exclusive end)")
    if not 1 <= workers <= 16:
        raise ValueError("Choose 1 to 16 workers")
    days = [start + timedelta(days=index) for index in range((end - start).days)]
    symbols = [MAPPINGS[coin] for coin in dict.fromkeys(coins)]
    jobs = [(symbol, day) for day in days for symbol in symbols]

    def fetch(job):
        symbol, day = job
        try:
            return symbol, day.isoformat(), download_one(root, symbol, day)
        except (OSError, ValueError, KeyError) as error:
            return symbol, day.isoformat(), f"error: {error}"

    root.mkdir(parents=True, exist_ok=True)
    received = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index, (symbol, day, status) in enumerate(pool.map(fetch, jobs), start=1):
            received.setdefault(symbol, {})[day] = status
            if index % 100 == 0 or index == len(jobs):
                print(f"{index}/{len(jobs)} public archives checked", flush=True)
    (root / "download-status.json").write_text(json.dumps(received, indent=2) + "\n")
    print({symbol: dict(pd.Series(days).value_counts()) for symbol, days in received.items()})
    return int(
        any(status.startswith("error:") for rows in received.values() for status in rows.values())
    )


def read_object(root, symbol, day, cutoff, strict):
    archive, receipt = object_paths(root, symbol, day)
    if not archive.is_file() or not receipt.is_file():
        return None
    details = json.loads(receipt.read_text(encoding="utf-8"))
    if details["url"] != f"{SOURCE}/{symbol}/{archive.name}":
        raise ValueError(f"Unexpected source URL for {receipt}")
    modified = datetime.fromisoformat(details["modified_utc"])
    day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    if modified.tzinfo is None or modified < day_start + timedelta(days=1):
        raise ValueError(f"Missing or invalid publication timestamp for {receipt}")
    if strict and modified >= cutoff:
        return None
    payload = archive.read_bytes()
    if len(payload) > MAX_ZIP or hashlib.sha256(payload).hexdigest() != details["sha256"]:
        raise ValueError(f"Bad size or checksum for {archive}")
    with zipfile.ZipFile(io.BytesIO(payload)) as zipped:
        name = archive.with_suffix(".csv").name
        if zipped.namelist() != [name] or zipped.getinfo(name).file_size > MAX_CSV:
            raise ValueError(f"Unexpected file structure in {archive}")
        raw = pd.read_csv(io.BytesIO(zipped.read(name)), dtype=str)
    if not {"create_time", "symbol", *FIELDS}.issubset(raw.columns):
        raise ValueError(f"Unexpected CSV schema in {archive}")
    if not raw["symbol"].eq(symbol).all():
        raise ValueError(f"Unexpected symbol in {archive}")
    observed = pd.to_datetime(raw["create_time"], utc=True, errors="coerce", format="ISO8601")
    # Old and new timestamp conventions can both put an observation at UTC midnight.
    # The enclosing source day keeps those observations separate.
    eligible = observed.between(day_start, day_start + timedelta(days=1)) & observed.lt(cutoff)
    sample = (
        raw.loc[eligible, list(FIELDS)].rename(columns=FIELDS).apply(pd.to_numeric, errors="coerce")
    )
    sample.index = pd.DatetimeIndex(observed[eligible])
    sample = sample.where((sample > 0) & np.isfinite(sample)).sort_index()
    duplicate = sample[sample.index.duplicated(keep=False)]
    if not duplicate.empty:
        conflicts = duplicate.groupby(level=0).nunique(dropna=False).gt(1).any(axis=1)
        sample = sample.drop(index=conflicts[conflicts].index)
        sample = sample[~sample.index.duplicated(keep="first")]
    return sample, day_start, modified


def project_object(observed, midnight, modified):
    if observed.empty:
        return None
    seconds = observed.index
    observed = observed.copy()
    taker = observed["taker"]
    taker_bias = ((taker - 1) / (taker + 1)).rolling("1h", min_periods=6).mean()
    observed["position_taker_imbalance_1h"] = taker_bias
    anchors = pd.date_range(midnight + timedelta(hours=1), periods=24, freq="h")
    available_by = anchors - MEASUREMENT_LAG
    idx = seconds.get_indexer(available_by, method="pad", tolerance=MEASUREMENT_MAX_AGE)
    usable = idx >= 0
    points = observed.iloc[idx[usable]].copy()
    points["observed_at"] = points.index
    points["published_at"] = pd.Timestamp(modified)
    points["source_day"] = pd.Timestamp(midnight)
    points.index = anchors[usable]
    return points


def hourly_positions(root, symbol, start, end, strict):
    cutoff = datetime.fromtimestamp(end * 3600, UTC)
    warmup = datetime.fromtimestamp(start * 3600, UTC).date() - timedelta(days=3)
    last_day = (cutoff - timedelta(seconds=1)).date()
    objects = []
    for offset in range((last_day - warmup).days + 1):
        data = read_object(root, symbol, warmup + timedelta(days=offset), cutoff, strict)
        if data is not None:
            hourly = project_object(*data)
            if hourly is not None and not hourly.empty:
                objects.append(hourly)
    if not objects:
        return None
    frame = pd.concat(objects).asfreq("h")
    frame["position_oi_usdt_log"] = np.log(frame["oi_dollars"])
    for field in ("top_accounts", "top_positions", "all_accounts"):
        frame[f"position_{field}_log_ratio"] = np.log(frame[field])
    for hours in CHANGE_HOURS:
        frame[f"position_oi_change_{hours}h"] = np.log(
            frame["oi_units"] / frame["oi_units"].shift(hours)
        )
        frame[f"previous_published_{hours}h"] = frame["published_at"].shift(hours)
    return frame


def project_forecasts(raw, clock, strict):
    if not strict:
        return raw.reindex(clock)[FEATURE_NAMES]
    daily = raw.dropna(subset=["observed_at"]).groupby("source_day").tail(1).copy()
    daily["decision_at"] = daily["published_at"].dt.floor("h") + timedelta(hours=1)
    daily = daily.sort_values(["decision_at", "observed_at"]).drop_duplicates(
        "decision_at", keep="last"
    )
    # A later receipt for an older archive cannot replace fresher observed data.
    daily = daily.loc[daily["observed_at"].eq(daily["observed_at"].cummax())].set_index(
        "decision_at"
    )
    projected = daily.reindex(clock, method="pad")
    ages = clock - projected["observed_at"]
    projected.loc[ages.gt(PUBLICATION_MAX_AGE), FEATURE_NAMES] = np.nan
    for hours in CHANGE_HOURS:
        future_previous = projected[f"previous_published_{hours}h"].ge(clock)
        projected.loc[future_previous, f"position_oi_change_{hours}h"] = np.nan
    return projected[FEATURE_NAMES]


def load_positioning(path: Path, start: int, end: int, *, mode="strict") -> pd.DataFrame:
    """Return a (UTC epoch hour, Hyperliquid symbol) frame of numerical features.

    Strict mode uses the observed ZIP object publication timestamp. Measurement mode
    is an API-availability hypothesis that cannot establish historical publication.
    """
    if mode not in ("strict", "measurement"):
        raise ValueError("Unknown positioning availability mode")
    if type(start) is not int or type(end) is not int or start >= end or end > ARCHIVE_END_HOUR:
        raise ValueError("Require a completed research interval inside the archive cutoff")
    root = Path(path)
    if not root.is_dir():
        raise ValueError(f"Missing positioning archive: {root}")
    clock = pd.to_datetime(np.arange(start, end, dtype=np.int64), unit="h", utc=True)
    frames = []
    for coin, symbol in MAPPINGS.items():
        raw = hourly_positions(root, symbol, start, end, mode == "strict")
        if raw is None:
            continue
        features = project_forecasts(raw, clock, mode == "strict").dropna(how="all").copy()
        features.index = features.index.as_unit("s").asi8 // 3600
        features.index.name = "hour"
        features["coin"] = coin
        frames.append(features.set_index("coin", append=True))
    if not frames:
        return pd.DataFrame(
            columns=FEATURE_NAMES, index=pd.MultiIndex.from_arrays([[], []], names=["hour", "coin"])
        )
    return pd.concat(frames).sort_index()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--coins", nargs="+", choices=tuple(MAPPINGS), default=list(MAPPINGS))
    parser.add_argument("--workers", type=int, default=12)
    options = parser.parse_args(argv)
    return download(options.output, options.start, options.end, options.coins, options.workers)


if __name__ == "__main__":
    raise SystemExit(main())
