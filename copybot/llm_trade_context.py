"""Build compact anonymous research packets from lagged historical context feeds."""

import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, time
from math import isfinite
from pathlib import Path
from typing import Any

from .directional_context import DEVELOPMENT_END, datetime_hour

MACRO_SERIES = {
    "broad_equities": ("sp500", "pct"),
    "growth_equities": ("nasdaq", "pct"),
    "equity_volatility": ("vix", "pct"),
    "short_treasury_yield": ("treasury_2y", "bp"),
    "long_treasury_yield": ("treasury_10y", "bp"),
    "crude_oil": ("wti", "pct"),
}
OFFICIAL_COUNTS = {
    "central_bank_speeches_7d": "news_fed_speeches_7d",
    "central_bank_digital_asset_speeches_7d": "news_fed_digital_asset_speeches_7d",
    "regulatory_digital_asset_headlines_7d": "news_cftc_digital_assets_7d",
    "regulatory_digital_asset_enforcement_7d": "news_cftc_digital_asset_enforcement_7d",
}
SPEECH_TONE = "news_fed_speech_tone_7d"
RATE_DIRECTION = "news_fomc_rate_direction"
VOLATILITY_LEVEL = "macro_vix_level"
MEDIA_COUNTS = {
    "headlines_24h": "news_media_memecoin_headlines_24h",
    "headlines_72h": "news_media_memecoin_headlines_72h",
    "publishers_72h": "news_media_memecoin_publishers_72h",
}
MEDIA_TONE = "news_media_memecoin_headline_tone_72h"
COUNT_FIELDS = {*OFFICIAL_COUNTS.values(), *MEDIA_COUNTS.values()}
OFFICIAL_FIELDS = {SPEECH_TONE, RATE_DIRECTION, VOLATILITY_LEVEL, *OFFICIAL_COUNTS.values()} | {
    f"macro_{source}_change_{window}d" for source, _ in MACRO_SERIES.values() for window in (1, 5)
}
MEDIA_FIELDS = {MEDIA_TONE, *MEDIA_COUNTS.values()}
CUTOFF = datetime_hour(datetime.combine(DEVELOPMENT_END, time(), UTC))
CONTEXT_METHOD = {
    "ages": "Whole-hour lower bounds, rounded down to six hours since modeled eligibility",
    "macro": "Change fields give signed percent changes in prices or signed basis-point changes in Treasury yields, over one or five completed market sessions. The equity volatility bucket approximates options-implied risk. Higher prices, higher implied volatility, and higher yields have different economic interpretations. Daily equity and volatility figures enter the next New York midnight, yield observations two days later, and oil seven days later.",
    "official_news": "Rolling seven-day Federal Reserve speech and CFTC press release counts cover broad official communication and digital-asset regulatory activity. The speech tone is the excess of strength words over risk words per 1,000 words; positive values indicate more strength language. The last FOMC policy-rate decision is +1 for an increase, zero for no change, -1 for a decrease. A high headline count measures activity rather than favorable sentiment. Dated news enters after New York midnight and FOMC decisions wait one hour after release.",
    "media": "Optional 24-hour-lagged headline and publisher counts and signed lexical tone for English memecoin coverage. Positive tone uses more up-move words than down-move words, then averages the signed scores. Publication times anchor the lag while the current search index determines retrospective coverage. For exploratory sensitivity only.",
}


@dataclass(frozen=True, slots=True)
class Observation:
    value: float
    available: int
    expires: int


def _updates(path, fields, end):
    events = []
    seen = set()
    with Path(path).open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
                available, expires = row["available_hour"], row["expires_hour"]
                if type(available) is not int or type(expires) is not int or available >= expires:
                    raise ValueError("Invalid forecast availability interval")
                if available >= end:
                    continue
                features = row["features"]
                if (
                    not isinstance(features, dict)
                    or not isinstance(row["source"], str)
                    or not row["source"]
                ):
                    raise ValueError("Invalid feature mapping or source")
                kept = {}
                for key in fields & features.keys():
                    value = features[key]
                    if type(value) not in (int, float) or not isfinite(value):
                        raise ValueError(f"Invalid recognized feature {key}")
                    if key in COUNT_FIELDS and (value < 0 or value != int(value)):
                        raise ValueError(f"Invalid count for {key}")
                    if key == RATE_DIRECTION and value not in (-1, 0, 1):
                        raise ValueError("Invalid policy-rate decision direction")
                    if key == MEDIA_TONE and not -1 <= value <= 1:
                        raise ValueError("Invalid media tone")
                    if key == VOLATILITY_LEVEL and value <= 0:
                        raise ValueError("Invalid implied-volatility level")
                    if (key, available) in seen:
                        raise ValueError(f"Duplicate publication of {key}")
                    seen.add((key, available))
                    # An already-expired correction still supersedes the previous value.
                    kept[key] = Observation(value, available, expires)
                if kept:
                    events.append((available, kept))
            except (TypeError, KeyError, ValueError) as error:
                raise ValueError(
                    f"Invalid historical context line {line_number} in {path}: {error}"
                ) from error
    return iter(sorted(events, key=lambda event: event[0]))


def _expire(active, hour):
    for key in tuple(active):
        if active[key].expires <= hour:
            del active[key]


def _age(hour, observations):
    oldest_time = min(row.available for row in observations)
    return ((hour - oldest_time) // 6) * 6


def _quantize(value, step):
    return round(round(value / step) * step, 2)


def _macro(active, hour):
    packet = {}
    for label, (source, unit) in MACRO_SERIES.items():
        rows = {
            f"change_{period}session_{unit}": active[f"macro_{source}_change_{period}d"]
            for period in (1, 5)
            if f"macro_{source}_change_{period}d" in active
        }
        if rows:
            series = packet[label] = {
                key: _quantize(row.value * 100, 5 if unit == "bp" else 0.5)
                for key, row in rows.items()
            }
            series["age_hours_floor_6h"] = _age(hour, rows.values())
    level = active.get(VOLATILITY_LEVEL)
    if level is not None:
        regime = (
            "quiet"
            if level.value < 15
            else "typical"
            if level.value < 25
            else "elevated"
            if level.value < 35
            else "stressed"
        )
        packet["equity_volatility_regime"] = {
            "level_bucket": regime,
            "age_hours_floor_6h": _age(hour, (level,)),
        }
    return packet


def _official_news(active, hour):
    packet = {}
    counts = {label: active[key] for label, key in OFFICIAL_COUNTS.items() if key in active}
    if counts:
        packet["counts"] = {
            **{label: int(row.value) for label, row in counts.items()},
            "oldest_age_hours_floor_6h": _age(hour, counts.values()),
        }
    if (tone := active.get(SPEECH_TONE)) is not None:
        packet["central_bank_speech_tone_7d"] = {
            "relative_strength_per_1000_words_rounded": round(tone.value),
            "age_hours_floor_6h": _age(hour, (tone,)),
        }
    if (rate := active.get(RATE_DIRECTION)) is not None:
        packet["last_policy_decision"] = {
            "rate_direction": int(rate.value),
            "age_hours_floor_6h": _age(hour, (rate,)),
        }
    return packet


def _media(active, hour):
    packet = {label: int(active[key].value) for label, key in MEDIA_COUNTS.items() if key in active}
    if (tone := active.get(MEDIA_TONE)) is not None:
        packet["headline_tone_72h"] = _quantize(tone.value, 0.2)
    if packet:
        packet["bucket_age_hours_floor_6h"] = _age(hour, active.values())
        packet["retrospective_selection"] = True
    return packet


def load_trade_context(
    context_path: Path | None,
    media_path: Path | None,
    start_hour: int,
    end_hour: int,
) -> dict[int, dict[str, Any]]:
    """Join anonymous context at each decision hour, excluding expired and future updates."""
    if (
        type(start_hour) is not int
        or type(end_hour) is not int
        or not 0 <= start_hour < end_hour <= CUTOFF
    ):
        raise ValueError(
            "Context requires a nonempty UTC hour interval within the Development cutoff"
        )
    official = _updates(context_path, OFFICIAL_FIELDS, end_hour) if context_path else iter(())
    media = _updates(media_path, MEDIA_FIELDS, end_hour) if media_path else iter(())
    next_official, next_media = next(official, None), next(media, None)
    active, media_active = {}, {}
    result = {}
    for hour in range(start_hour, end_hour):
        while next_official and next_official[0] <= hour:
            active.update(next_official[1])
            next_official = next(official, None)
        while next_media and next_media[0] <= hour:
            media_active.update(next_media[1])
            next_media = next(media, None)
        _expire(active, hour)
        _expire(media_active, hour)
        packet = {
            name: data
            for name, data in (
                ("macro", _macro(active, hour)),
                ("official_news", _official_news(active, hour)),
                ("media", _media(media_active, hour)),
            )
            if data
        }
        result[hour] = packet
    return result


def context_coverage(packets):
    counts = Counter(section for packet in packets.values() for section in packet)
    n = len(packets)
    return {
        "hours": n,
        "available_hours": {name: counts[name] for name in ("macro", "official_news", "media")},
        "missing_hours": {name: n - counts[name] for name in ("macro", "official_news", "media")},
        "macro_hours_by_series": {
            name: sum(name in row.get("macro", {}) for row in packets.values())
            for name in MACRO_SERIES
        },
        "official_counts_hours": sum(
            "counts" in row.get("official_news", {}) for row in packets.values()
        ),
        "news_tone_hours": sum(
            "central_bank_speech_tone_7d" in row.get("official_news", {})
            for row in packets.values()
        ),
        "media_tone_hours": sum(
            "headline_tone_72h" in row.get("media", {}) for row in packets.values()
        ),
    }
