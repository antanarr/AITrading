"""Build a retrospective media sensitivity feed from dated publisher headlines.

Google News selects these articles when queried today. Dated headlines and
a conservative availability lag do not reconstruct which articles its index
would have surfaced historically. Use this optional feed for exploratory
ablation only. Its records use the directional_context JSONL format.
"""

import argparse
import json
import re
from bisect import bisect_right
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from statistics import mean
from urllib.parse import urlencode
from xml.etree import ElementTree

from .directional_context import BITCOIN, DEVELOPMENT_END, Update, datetime_hour, fetch_text

MEDIA_SOURCE = "https://news.google.com/rss/search"
MEDIA_QUERY = 'memecoin OR "meme coin"'
RELEASE_DELAY = 24
MAX_QUERIES = 28
PROVIDER_CAP = 100
SCORE_UP = re.compile(
    r"\b(?:rall(?:y|ies|ied)|soar\w*|surge\w*|gain\w*|rebound\w*|recover\w*)\b", re.I
)
SCORE_DOWN = re.compile(
    r"\b(?:crash\w*|slump\w*|plung\w*|sell[ -]?off\w*|fraud|scam\w*|rug[ -]?pull\w*)\b", re.I
)
FEAR_GREED = re.compile(r"\bfear\s*(?:and|&)\s*greed\b", re.I)


@dataclass(frozen=True)
class Headline:
    published: datetime
    publisher: str
    title: str

    @property
    def available_hour(self):
        return datetime_hour(self.published) + RELEASE_DELAY

    @property
    def tone(self):
        positive, negative = len(SCORE_UP.findall(self.title)), len(SCORE_DOWN.findall(self.title))
        return (positive > negative) - (negative > positive)


def search_url(start, end):
    query = f"({MEDIA_QUERY}) after:{start - timedelta(days=1)} before:{end} -bitcoin -btc"
    return f"{MEDIA_SOURCE}?{urlencode({'q': query, 'hl': 'en-US', 'gl': 'US', 'ceid': 'US:en'})}"


def rss_headlines(raw, start, end):
    root = ElementTree.fromstring(raw)
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("The media provider did not return an RSS channel")
    rows = root.findall("./channel/item")
    headlines = []
    for row in rows:
        date_text = row.findtext("pubDate", default="").strip()
        publisher = row.findtext("source", default="").strip()
        title = row.findtext("title", default="").strip()
        if not (date_text and publisher and title):
            raise ValueError("A media record is missing a title, publication time or publisher")
        stamp = parsedate_to_datetime(date_text)
        if stamp.tzinfo is None:
            raise ValueError("A media publication time is missing a timezone")
        stamp = stamp.astimezone(UTC)
        title = title.removesuffix(f" - {publisher}").strip()
        if (
            start <= stamp.date() < end
            and title
            and not (BITCOIN.search(title) or FEAR_GREED.search(title))
        ):
            headlines.append(Headline(stamp, publisher, title))
    return headlines, len(rows)


def collect(start, end):
    date_pairs = []
    for offset in range(0, (end - start).days, 7):
        week = start + timedelta(days=offset)
        date_pairs.append((week, min(week + timedelta(days=7), end)))
    if len(date_pairs) > MAX_QUERIES:
        raise ValueError(f"The media feed is limited to {MAX_QUERIES} weekly queries per build")

    all_headlines = []
    missing = saturated = 0
    for left, right in date_pairs:
        rows, total = rss_headlines(fetch_text(search_url(left, right)), left, right)
        all_headlines.extend(rows)
        missing += not rows
        saturated += total >= PROVIDER_CAP

    last_seen = {}
    deduplicated = []
    for row in sorted(all_headlines, key=lambda item: (item.published, item.publisher, item.title)):
        key = " ".join(re.findall(r"\w+", row.title.casefold()))
        previous = last_seen.get(key)
        if previous is None or row.published - previous >= timedelta(hours=48):
            deduplicated.append(row)
            last_seen[key] = row.published
    return deduplicated, len(date_pairs), missing, saturated


def media_updates(headlines, start, end):
    first = datetime_hour(datetime.combine(start, time(), UTC))
    last = datetime_hour(datetime.combine(end, time(), UTC))
    eligible = sorted(headlines, key=lambda row: row.available_hour)
    hours = [row.available_hour for row in eligible]
    for hour in range(first, last):
        right = bisect_right(hours, hour)
        day = eligible[bisect_right(hours, hour - 24) : right]
        three_days = eligible[bisect_right(hours, hour - 72) : right]
        features = {
            "news_media_memecoin_headlines_24h": len(day),
            "news_media_memecoin_headlines_72h": len(three_days),
            "news_media_memecoin_publishers_72h": len(
                {row.publisher.casefold() for row in three_days}
            ),
        }
        if three_days:
            features["news_media_memecoin_headline_tone_72h"] = mean(row.tone for row in three_days)
        yield Update(hour, hour + 1, MEDIA_SOURCE, features)


def prepare(path, start, end):
    if not start < end <= DEVELOPMENT_END:
        raise ValueError(f"Expected development dates before {DEVELOPMENT_END} UTC")
    headlines, queries, empty, saturated = collect(start - timedelta(days=4), end)
    publisher_counts = Counter(row.publisher for row in headlines)
    active_hours = 0
    with path.open("x", encoding="utf-8") as feed:
        for update in media_updates(headlines, start, end):
            active_hours += "news_media_memecoin_headline_tone_72h" in update.features
            feed.write(json.dumps(asdict(update), allow_nan=False, separators=(",", ":")) + "\n")
    return {
        "classification": "exploratory_retrospective_media_ablation_only",
        "availability_delay_hours": RELEASE_DELAY,
        "weekly_queries": queries,
        "weekly_queries_without_eligible_results": empty,
        "weekly_queries_at_pagination_cap": saturated,
        "unique_headlines_including_warmup": len(headlines),
        "unique_publishers_including_warmup": len(publisher_counts),
        "top_publishers": dict(publisher_counts.most_common(10)),
        "active_headline_hours": active_hours,
        "forecast_hours": (end - start).days * 24,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    options = parser.parse_args()
    print(json.dumps(prepare(options.output, options.start, options.end), indent=2))


if __name__ == "__main__":
    main()
