"""Prepare and load point-in-time public context for the offline directional study.

The JSONL feed records availability as integer UTC epoch hours. Each update
supplies a source, an exclusive expiry and numeric macro_/news_ features.
Missing and expired features are omitted. The builder only fetches development
dates and never opens the research candle archive or trading state.
"""

import argparse
import csv
import json
import math
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from html import unescape
from pathlib import Path
from statistics import mean
from urllib.parse import urlencode, urljoin
from zoneinfo import ZoneInfo

DEVELOPMENT_END = date(2026, 11, 21)
EASTERN = ZoneInfo("America/New_York")
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv"
CBOE_VIX = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
FED = "https://www.federalreserve.gov"
FED_SPEECHES = FED + "/newsevents/speech/{year}-speeches.htm"
FED_FOMC = FED + "/newsevents/pressreleases/{year}-press-fomc.htm"
CFTC_RELEASES = "https://www.cftc.gov/PressRoom/PressReleases"
CFTC_HEADLINE = re.compile(
    r'<time\s+datetime="([^"<>]+)"[^>]*>[^<]+</time>.*?'
    r'<a href="(/PressRoom/PressReleases/\d+-\d+)"[^>]*>(.*?)</a>',
    re.I | re.S,
)
SPEECH_LINK = re.compile(r'href="(/newsevents/speech/[a-z]+(\d{8})[a-z]\.htm)"', re.I)
FOMC_LINK = re.compile(r'href="(/newsevents/pressreleases/monetary(\d{8})a\.htm)"', re.I)
PARAGRAPH = re.compile(r"<p(?:\s[^>]*)?>(.*?)</p>", re.I | re.S)
BITCOIN = re.compile(r"\b(?:bitcoin|btc|xbt|wbtc|kbtc)\b", re.I)
CRYPTO_POLICY = re.compile(r"\b(?:stablecoins?|tokeniz\w+|digital assets?|crypto assets?)\b", re.I)
ENFORCEMENT = re.compile(r"\b(?:charges|fraud|sanctions|court order|lawsuit|sues)\b", re.I)
RISK = re.compile(
    r"\b(?:uncertain\w*|recession\w*|downturn\w*|deteriorat\w*|weaken\w*|stress\w*|volatil\w*)\b",
    re.I,
)
STRENGTH = re.compile(
    r"\b(?:resilien\w*|robust\w*|solid|stabiliz\w*|recover\w*|improv\w*|strong)\b", re.I
)
RATE_CHANGE = re.compile(
    r"decided to (raise|lower|maintain) the target range for the federal funds rate", re.I
)
SERIES = {
    "SP500": ("sp500", 1, False),
    "NASDAQCOM": ("nasdaq", 1, False),
    "DGS2": ("treasury_2y", 2, True),
    "DGS10": ("treasury_10y", 2, True),
    "DCOILWTICO": ("wti", 7, False),
}


@dataclass(frozen=True)
class Update:
    available_hour: int
    expires_hour: int
    source: str
    features: dict[str, float]


def load_context(path: Path, start: int, end: int) -> dict[int, dict[str, float]]:
    """Construct hour-indexed feature rows without interpolation or backfill."""
    if start >= end:
        raise ValueError("Expected a nonempty chronological context interval")
    if end > datetime_hour(datetime.combine(DEVELOPMENT_END, time(), UTC)):
        raise ValueError("Context can only be loaded before the development cutoff")
    updates = []
    with path.open(encoding="utf-8") as feed:
        for line_number, line in enumerate(feed, 1):
            try:
                row = json.loads(line)
                update = Update(**row)
                if (
                    type(update.available_hour) is not int
                    or type(update.expires_hour) is not int
                    or update.expires_hour <= update.available_hour
                    or not isinstance(update.source, str)
                    or not update.source
                    or not isinstance(update.features, dict)
                    or not update.features
                ):
                    raise ValueError("Invalid availability or source")
                for name, value in update.features.items():
                    if (
                        not re.fullmatch(r"(?:macro|news)_[a-z0-9_]+", name)
                        or set(name.split("_")) & {"btc", "bitcoin", "xbt", "wbtc", "kbtc"}
                        or type(value) not in (int, float)
                        or not math.isfinite(value)
                    ):
                        raise ValueError(f"Invalid context feature {name!r}")
            except (ValueError, TypeError) as error:
                raise ValueError(
                    f"Invalid context line {line_number} in {path}: {error}"
                ) from error
            if update.available_hour < end:
                updates.append(update)

    active = {}
    timestamps = {}
    result = {}
    pending = iter(sorted(updates, key=lambda item: item.available_hour))
    next_update = next(pending, None)
    for hour in range(start, end):
        while next_update and next_update.available_hour <= hour:
            for name, value in next_update.features.items():
                if timestamps.get(name) == next_update.available_hour:
                    raise ValueError(f"Duplicate context update for {name} at {timestamps[name]}")
                timestamps[name] = next_update.available_hour
                active[name] = (next_update.expires_hour, float(value))
            next_update = next(pending, None)
        result[hour] = {name: value for name, (expiry, value) in active.items() if expiry > hour}
    return result


def fetch_text(url):
    response = subprocess.run(
        [
            "curl",
            "-fsSL",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--max-time",
            "15",
            "--retry",
            "2",
            url,
        ],
        capture_output=True,
        check=False,
    )
    if response.returncode:
        raise RuntimeError(f"Could not read {url}: {response.stderr.decode('utf-8').strip()}")
    return response.stdout.decode("utf-8-sig")


def datetime_hour(stamp):
    return math.ceil(stamp.timestamp() / 3600)


def eastern_hour(day, hour=0):
    return datetime_hour(datetime.combine(day, time(hour), EASTERN))


def csv_prices(raw, date_column, value_column, date_format, start, end):
    rows = {}
    for row in csv.DictReader(raw.splitlines()):
        day = datetime.strptime(row[date_column], date_format).date()
        value = row[value_column]
        if start <= day < end and value not in (".", "", None):
            number = float(value)
            if day in rows or not math.isfinite(number) or number <= 0:
                raise ValueError(f"Invalid {value_column} observation on {day}")
            rows[day] = number
    if not rows:
        raise ValueError(f"Empty {value_column} response for requested dates")
    return sorted(rows.items())


def market_updates(source, rows, name, delay, difference):
    updates = []
    for index, (day, value) in enumerate(rows):
        available = eastern_hour(day + timedelta(days=delay))
        features = {f"macro_{name}_level": value}
        for lookback in (1, 5):
            if index >= lookback:
                previous = rows[index - lookback][1]
                change = value - previous if difference else value / previous - 1
                features[f"macro_{name}_change_{lookback}d"] = change
        updates.append(Update(available, available + 10 * 24, source, features))
    return updates


def macro_series(series, start, end):
    if series == "VIX":
        source = CBOE_VIX
        prices = csv_prices(fetch_text(source), "DATE", "CLOSE", "%m/%d/%Y", start, end)
        return market_updates(source, prices, "vix", 1, False)
    name, delay, difference = SERIES[series]
    query = urlencode({"id": series, "cosd": start, "coed": end - timedelta(days=1)})
    source = f"{FRED}?{query}"
    prices = csv_prices(fetch_text(source), "observation_date", series, "%Y-%m-%d", start, end)
    return market_updates(source, prices, name, delay, difference)


def news_links(template, pattern, start, end):
    links = set()
    for year in range(start.year, (end - timedelta(days=1)).year + 1):
        source = template.format(year=year)
        for path, raw_date in pattern.findall(fetch_text(source)):
            day = datetime.strptime(raw_date, "%Y%m%d").date()
            if start <= day < end:
                links.add((day, urljoin(source, path)))
    return sorted(links)


def plain_text(html):
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", html)).split())


def article_body(html):
    start = html.lower().find("article__time")
    if start == -1:
        raise ValueError("Federal Reserve article date is missing")
    body = html[start:]
    paragraphs = []
    for paragraph in PARAGRAPH.findall(body.split('id="back-top"', 1)[0]):
        words = plain_text(paragraph)
        if "return to text" in words.lower() or BITCOIN.search(words):
            continue
        if len(words.split()) >= 12:
            paragraphs.append(words)
    if not paragraphs:
        raise ValueError("Federal Reserve article body is missing")
    return " ".join(paragraphs)


def speech_record(link):
    day, url = link
    speech = article_body(fetch_text(url))
    word_count = len(speech.split())
    risk = len(RISK.findall(speech))
    strength = len(STRENGTH.findall(speech))
    return day, url, (strength - risk) / word_count * 1000, bool(CRYPTO_POLICY.search(speech))


def speech_updates(records, start, end):
    for offset in range((end - start).days):
        day = start + timedelta(days=offset)
        recent = [row for row in records if timedelta(0) <= day - row[0] < timedelta(days=7)]
        features = {
            "news_fed_speeches_1d": sum(row[0] == day for row in recent),
            "news_fed_speeches_7d": len(recent),
            "news_fed_digital_asset_speeches_7d": sum(row[3] for row in recent),
        }
        if recent:
            features["news_fed_speech_tone_7d"] = mean(row[2] for row in recent)
        available = eastern_hour(day + timedelta(days=1))
        expires = eastern_hour(day + timedelta(days=2))
        yield Update(available, expires, FED_SPEECHES.format(year=day.year), features)


def cftc_records(start, end):
    records = {}
    for page in range(25):
        rows = CFTC_HEADLINE.findall(fetch_text(f"{CFTC_RELEASES}?page={page}"))
        if not rows:
            raise ValueError("The CFTC archive ended before reaching the requested start date")
        dates = []
        for stamp, path, title in rows:
            day = datetime.fromisoformat(stamp).astimezone(EASTERN).date()
            dates.append(day)
            headline = plain_text(title)
            if (
                start <= day < end
                and CRYPTO_POLICY.search(headline)
                and not BITCOIN.search(headline)
            ):
                records[path] = (day, bool(ENFORCEMENT.search(headline)))
        if min(dates) < start:
            return sorted(records.values())
    raise ValueError("The CFTC page limit was reached before the requested start date")


def cftc_updates(records, start, end):
    for offset in range((end - start).days):
        day = start + timedelta(days=offset)
        recent = [row for row in records if timedelta(0) <= day - row[0] < timedelta(days=7)]
        yield Update(
            eastern_hour(day + timedelta(days=1)),
            eastern_hour(day + timedelta(days=2)),
            CFTC_RELEASES,
            {
                "news_cftc_digital_assets_1d": sum(row[0] == day for row in recent),
                "news_cftc_digital_assets_7d": len(recent),
                "news_cftc_digital_asset_enforcement_7d": sum(row[1] for row in recent),
            },
        )


def fomc_record(link):
    day, url = link
    html = fetch_text(url)
    if not re.search(r"For release at\s+2:00 p\.m\.\s+E[DS]T", html):
        raise ValueError(f"Unconfirmed FOMC release time for {url}")
    change = RATE_CHANGE.search(article_body(html))
    if not change:
        raise ValueError(f"No explicit FOMC rate decision in {url}")
    direction = {"raise": 1.0, "lower": -1.0, "maintain": 0.0}[change[1].lower()]
    available = eastern_hour(day, 15)
    return Update(available, available + 110 * 24, url, {"news_fomc_rate_direction": direction})


def prepare(path, start, end):
    if not start < end <= DEVELOPMENT_END:
        raise ValueError(f"Expected development dates before {DEVELOPMENT_END} UTC")
    warmup = start - timedelta(days=115)
    with ThreadPoolExecutor(max_workers=5) as pool:
        macro_start = start - timedelta(days=24)
        macro = [pool.submit(macro_series, series, macro_start, end) for series in (*SERIES, "VIX")]
        cftc = pool.submit(cftc_records, start - timedelta(days=7), end)
        speech_links = news_links(FED_SPEECHES, SPEECH_LINK, start - timedelta(days=7), end)
        fomc_links = news_links(FED_FOMC, FOMC_LINK, warmup, end)
        if not speech_links or not fomc_links:
            raise ValueError("The official Federal Reserve index supplied no release links")
        records = list(pool.map(speech_record, speech_links))
        updates = list(pool.map(fomc_record, fomc_links))
        updates.extend(speech_updates(records, start - timedelta(days=1), end))
        cftc_news = cftc.result()
        updates.extend(cftc_updates(cftc_news, start - timedelta(days=1), end))
        updates.extend(update for request in macro for update in request.result())

    start_hour = datetime_hour(datetime.combine(start, time(), UTC))
    end_hour = datetime_hour(datetime.combine(end, time(), UTC))
    eligible = [
        row for row in updates if row.expires_hour > start_hour and row.available_hour < end_hour
    ]
    with path.open("x", encoding="utf-8") as feed:
        for row in sorted(eligible, key=lambda item: (item.available_hour, item.source)):
            feed.write(json.dumps(asdict(row), allow_nan=False, separators=(",", ":")) + "\n")
    return {
        "updates": len(eligible),
        "official_speeches": len(records),
        "digital_asset_speeches": sum(record[3] for record in records),
        "cftc_digital_asset_releases": len(cftc_news),
        "fomc_releases": sum(
            update.source in {url for _, url in fomc_links} for update in eligible
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    print(json.dumps(prepare(args.output, args.start, args.end), indent=2))


if __name__ == "__main__":
    main()
