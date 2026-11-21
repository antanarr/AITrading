import hashlib
import json
import math
import zipfile
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from copybot.directional_positioning import (
    FIELDS,
    SOURCE,
    epoch_hour,
    load_positioning,
    object_paths,
)
from copybot.research_data import ARCHIVE_END_HOUR


def scheduled(day):
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def journal(root, day, modified, label_and_oi):
    symbol = "DOGEUSDT"
    archive, receipt = object_paths(root, symbol, day)
    archive.parent.mkdir(parents=True, exist_ok=True)
    heading = ",".join(("create_time", "symbol", *FIELDS))
    rows = [
        f"{label:%Y-%m-%d %H:%M:%S},{symbol},{oi},{oi * 10},1.2,1.4,1.1,2"
        for label, oi in label_and_oi
    ]
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(archive.with_suffix(".csv").name, "\n".join([heading, *rows]))
    receipt.write_text(
        json.dumps(
            {
                "url": f"{SOURCE}/{symbol}/{archive.name}",
                "modified_utc": modified.isoformat(),
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }
        )
    )
    return archive


def test_strict_features_wait_until_after_publication_and_expire(tmp_path):
    origin = date(2026, 9, 1)
    midnight = scheduled(origin)
    journal(
        tmp_path,
        origin,
        midnight + timedelta(hours=31),
        [(midnight + timedelta(minutes=i * 5), 100) for i in range(288)],
    )
    start = epoch_hour(origin)
    actual = load_positioning(tmp_path, start, start + 96)
    assert actual.index.names == ["hour", "coin"]
    assert set(actual.index.get_level_values("coin")) == {"DOGE"}
    assert actual.index.get_level_values("hour").min() == start + 32
    assert actual.index.get_level_values("hour").max() == start + 71
    row = actual.loc[(start + 32, "DOGE")]
    assert row["position_oi_usdt_log"] == pytest.approx(math.log(1000))
    assert row["position_oi_change_4h"] == 0
    assert pd.isna(row["position_oi_change_24h"])
    assert row["position_taker_imbalance_1h"] == pytest.approx(1 / 3)


def test_hypothetical_measurement_applies_15_minute_lag(tmp_path):
    origin = date(2026, 9, 1)
    midnight = scheduled(origin)
    journal(
        tmp_path,
        origin,
        midnight + timedelta(hours=30),
        [(midnight + timedelta(minutes=minute), oi) for minute, oi in [(45, 100), (50, 900)]],
    )
    start = epoch_hour(origin)
    assert load_positioning(tmp_path, start, start + 12).empty
    hypothetical = load_positioning(tmp_path, start, start + 12, mode="measurement")
    assert list(hypothetical.index) == [(start + 1, "DOGE")]
    assert hypothetical.iloc[0]["position_oi_usdt_log"] == pytest.approx(math.log(1000))


def test_strict_prior_oi_needs_prior_data_to_be_published(tmp_path):
    origin = date(2026, 9, 1)
    midnight = scheduled(origin)
    for day, oi, modified_hours in ((0, 100, 60), (1, 90, 53)):
        source_day = midnight + timedelta(days=day)
        journal(
            tmp_path,
            source_day.date(),
            midnight + timedelta(hours=modified_hours),
            [(source_day + timedelta(minutes=i * 5), oi) for i in range(288)],
        )
    start = epoch_hour(origin)
    strict = load_positioning(tmp_path, start, start + 80)
    assert pd.isna(strict.loc[(start + 54, "DOGE"), "position_oi_change_24h"])
    assert pd.isna(strict.loc[(start + 60, "DOGE"), "position_oi_change_24h"])
    assert strict.loc[(start + 61, "DOGE"), "position_oi_change_24h"] == pytest.approx(
        math.log(0.9)
    )
    assert strict.loc[(start + 61, "DOGE"), "position_oi_usdt_log"] == pytest.approx(math.log(900))


def test_strict_does_not_open_unpublished_payload_and_checks_archive_afterwards(tmp_path):
    origin = date(2026, 9, 1)
    midnight = scheduled(origin)
    archive = journal(
        tmp_path,
        origin,
        midnight + timedelta(hours=30),
        [(midnight + timedelta(minutes=45), 100)],
    )
    archive.write_bytes(b"changed data")
    start = epoch_hour(origin)
    assert load_positioning(tmp_path, start, start + 30).empty
    with pytest.raises(ValueError, match="checksum"):
        load_positioning(tmp_path, start, start + 31)
    with pytest.raises(ValueError, match="archive cutoff"):
        load_positioning(tmp_path, start, ARCHIVE_END_HOUR + 1)
