"""Load native Hyperliquid history at a research partition cutoff."""

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import DecimalException
from math import isfinite
from pathlib import Path

from .perps import CANDLE_INTERVALS, number
from .research_models import DAY_HOURS, HOUR_MS, Bar, Funding, MarketHistory, excluded_symbol

ARCHIVE_END_HOUR = int(datetime(2026, 11, 21, tzinfo=UTC).timestamp()) // 3600
_INTERVALS = {"hour": "1h", "day": "1d", "quarter": "15m"}
_TAXONOMY_PATH = Path(__file__).with_name("research_taxonomy.json")
_CATEGORIES = {"meme", "non_meme", "unknown"}


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _metadata(root):
    primary = root / "meta.json"
    census = {}
    files = [primary, *sorted(path for path in root.rglob("meta.json") if path != primary)]
    for path in files:
        if not path.is_file():
            continue
        for row in _read(path)["universe"]:
            coin = row["name"]
            prior = census.setdefault(coin, row)
            if prior["szDecimals"] != row["szDecimals"]:
                raise ValueError(f"Conflicting contract size precision for {coin}")
    if not census:
        raise ValueError(f"No Hyperliquid market census found in {root}")
    return census, [str(path.relative_to(root)) for path in files if path.is_file()]


def _taxonomy(root):
    path = root / "taxonomy.json"
    if not path.is_file():
        path = _TAXONOMY_PATH
    if not path.is_file():
        return {}, {"source": None, "status": "No identity snapshot available"}
    snapshot = _read(path)
    if not snapshot["source"] or not snapshot["captured_at_utc"]:
        raise ValueError("Taxonomy snapshot requires source and capture date")
    for coin, record in snapshot["assets"].items():
        if record["category"] not in _CATEGORIES:
            raise ValueError(f"Unsupported taxonomy category for {coin}")
        if record["category"] != "unknown" and not all(
            record.get(field) for field in ("provider_id", "name", "source")
        ):
            raise ValueError(f"Taxonomy classification for {coin} needs a sourced identity")
    return snapshot["assets"], {key: value for key, value in snapshot.items() if key != "assets"}


def _inventory(root, include_quarters):
    archives = defaultdict(lambda: defaultdict(list))
    prefixes = ("hour", "day", "fund", "quarter") if include_quarters else ("hour", "day", "fund")
    for path in sorted(root.rglob("*.json")):
        prefix, _, name = path.stem.partition("-")
        if prefix not in prefixes or not name:
            continue
        if prefix == "fund":
            name, separator, page = name.rpartition("-")
            if not separator or not page.isdecimal():
                raise ValueError(f"Expected funding page suffix for {path}")
        archives[name][prefix].append(path)
    return archives


def _float(value):
    result = float(number(value))
    if not isfinite(result):
        raise ValueError("Value outside finite float range")
    return result


def _bar(row, coin, interval, span):
    stamp = row["t"]
    if row["s"] != coin or row["i"] != interval or stamp < 0 or stamp % span:
        raise ValueError("Unexpected candle symbol, resolution or open timestamp")
    if row["T"] != stamp + span - 1:
        raise ValueError("Unexpected candle close timestamp")
    bar = Bar(*(_float(row[field]) for field in ("o", "h", "l", "c", "v")))
    if not 0 < bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
        raise ValueError("Invalid candle OHLC")
    if bar.volume < 0:
        raise ValueError("Negative candle volume")
    return bar


def _funding(row, coin):
    stamp = row["time"]
    if row["coin"] != coin or stamp < 0:
        raise ValueError("Unexpected funding symbol or timestamp")
    premium = row.get("premium")
    return Funding(
        stamp, _float(row["fundingRate"]), _float(premium) if premium is not None else None
    )


def _series(paths, kind, coin, cutoff):
    """Bind timestamps before interpreting any value or identity in a row."""
    funding = kind == "fund"
    interval = _INTERVALS.get(kind)
    stamp_key = "time" if funding else "t"
    span = HOUR_MS if funding else CANDLE_INTERVALS[interval]
    values, conflicts = {}, set()
    invalid = 0
    for path in paths:
        rows = _read(path)
        if not isinstance(rows, list):
            raise ValueError(f"Expected a list of observations in {path}")
        for row in rows:
            stamp = row.get(stamp_key) if isinstance(row, dict) else None
            if type(stamp) is not int:
                invalid += 1
                continue
            if stamp >= cutoff or (not funding and stamp + span > cutoff):
                continue
            if not funding and type(row.get("T")) is int and row["T"] >= cutoff:
                continue
            key = stamp // HOUR_MS if kind in {"hour", "fund"} else stamp
            try:
                value = _funding(row, coin) if funding else _bar(row, coin, interval, span)
            except (DecimalException, KeyError, TypeError, ValueError):
                invalid += 1
                continue
            if key in values and values[key] != value:
                conflicts.add(key)
            values[key] = value
    for key in conflicts:
        del values[key]
    return dict(sorted(values.items())), invalid, len(conflicts)


def _coverage(values, period=1):
    if not values:
        return {"rows": 0, "first": None, "last": None, "interior_gaps": 0}
    first, last = min(values), max(values)
    return {
        "rows": len(values),
        "first": first,
        "last": last,
        "interior_gaps": (last - first) // period + 1 - len(values),
    }


def _consistent_quarters(bars, quarters):
    conflicts = 0
    quarter_ms = CANDLE_INTERVALS["15m"]
    for hour, bar in bars.items():
        stamps = range(hour * HOUR_MS, (hour + 1) * HOUR_MS, quarter_ms)
        if not all(stamp in quarters for stamp in stamps):
            continue
        parts = [quarters[stamp] for stamp in stamps]
        aggregate = (
            parts[0].open,
            max(part.high for part in parts),
            min(part.low for part in parts),
            parts[-1].close,
        )
        if aggregate != (bar.open, bar.high, bar.low, bar.close):
            conflicts += 1
            for stamp in stamps:
                del quarters[stamp]
    return conflicts


def load_archive(path: Path, end_hour: int, include_quarters: bool = False):
    """Return histories and per-market coverage through the exclusive UTC hour."""
    if type(end_hour) is not int or end_hour <= 0:
        raise ValueError("Research cutoff must be a positive UTC epoch hour")
    path = Path(path)
    cutoff = end_hour * HOUR_MS
    census, metadata_paths = _metadata(path)
    taxonomy, taxonomy_provenance = _taxonomy(path)
    archives = _inventory(path, include_quarters)
    assets, coverage = {}, {}
    statuses = Counter()
    for coin, meta in sorted(census.items()):
        if excluded_symbol(coin) or ":" in coin:
            continue
        record = taxonomy.get(coin, {})
        category = record.get("category", "unknown")
        delisted = bool(meta.get("isDelisted", False))
        decimals = meta["szDecimals"]
        shards = archives.get(coin, {})
        bars, bad_bars, bar_conflicts = _series(shards.get("hour", ()), "hour", coin, cutoff)
        funds, bad_funds, fund_conflicts = _series(shards.get("fund", ()), "fund", coin, cutoff)
        days, bad_days, day_conflicts = _series(shards.get("day", ()), "day", coin, cutoff)
        quarters, bad_quarters, quarter_conflicts = _series(
            shards.get("quarter", ()), "quarter", coin, cutoff
        )
        mismatched_quarter_hours = _consistent_quarters(bars, quarters) if quarters else 0
        listing_hour = None
        if bars and days:
            first_day = min(days) // HOUR_MS
            first_hour = min(bars)
            if first_day <= first_hour:
                listing_hour = first_hour if first_hour - first_day < DAY_HOURS else first_day
        # The current census can flag contracts that were still trading in earlier partitions.
        status = "available" if bars else "no_hourly_history"
        if type(decimals) is not int or not 0 <= decimals <= 6:
            status = "invalid_size_precision"
        if status == "available":
            assets[coin] = MarketHistory(
                coin, decimals, category == "meme", listing_hour, bars, funds, quarters
            )
        statuses[status] += 1
        last = max(bars, default=None)
        funding_window = {hour + 1 for hour in bars if hour < last} if bars else set()
        coverage[coin] = {
            "status": status,
            "delisted_in_census": delisted,
            "category": category,
            "identity": record or None,
            "listing_hour_estimate": listing_hour,
            "hourly": _coverage(bars),
            "daily": _coverage(days, CANDLE_INTERVALS["1d"]),
            "funding": _coverage(funds),
            "missing_funding_for_hourly": len(funding_window - funds.keys()),
            "quarter": _coverage(quarters, CANDLE_INTERVALS["15m"]),
            "hourly_reaches_cutoff": last == end_hour - 1,
            "quarter_hourly_ohlc_mismatches": mismatched_quarter_hours,
            "invalid_rows": {
                "hourly": bad_bars,
                "daily": bad_days,
                "funding": bad_funds,
                "quarter": bad_quarters,
            },
            "conflicts": {
                "hourly": bar_conflicts,
                "daily": day_conflicts,
                "funding": fund_conflicts,
                "quarter": quarter_conflicts,
            },
        }
    return assets, {
        "end_hour_exclusive": end_hour,
        "census_files": metadata_paths,
        "census_source": "Hyperliquid native perp meta, captured near the end of the archive",
        "census_survivorship": (
            "Census flags reflect the captured snapshot. Archived delisted contracts may be included. "
            "Markets missing from the census or unavailable from Hyperliquid remain unobserved."
        ),
        "taxonomy": taxonomy_provenance,
        "status_counts": dict(statuses),
        "unmatched_hourly_symbols": sorted(
            coin
            for coin, files in archives.items()
            if "hour" in files and coin not in census and not excluded_symbol(coin)
        ),
        "markets": coverage,
    }
