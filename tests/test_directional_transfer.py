import hashlib
import json
import zipfile
from datetime import date

import pytest

from copybot.directional_transfer import TRANSFER_END, epoch_hour, load_transfer, shard_plan
from copybot.research_models import HOUR_MS, Bar, Funding, MarketHistory

HEADER = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore"


def csv_bar(hour, *, closing=2.1, close_offset=0):
    opened = hour * HOUR_MS
    return f"{opened},2,3,1,{closing},40,{opened + HOUR_MS - 1 + close_offset},84,4,20,42,0"


def fixture_archive(root, shard, rows, *, header=True):
    archive = shard.local(root, "zip")
    archive.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join([HEADER, *rows] if header else rows)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(f"{shard.stem}.csv", text)
    shard.local(root, "json").write_text(
        json.dumps({"url": shard.url, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()})
    )
    return archive


def test_monthly_sources_and_daily_boundary_end_before_cutoff():
    shards = list(shard_plan("DOGEUSDT", date(2026, 4, 1), TRANSFER_END))
    assert shards[0].monthly
    assert shards[0].start == date(2026, 4, 1)
    assert shards[0].end == date(2026, 5, 1)
    assert len(shards) == 25
    assert all(not shard.monthly for shard in shards[1:])
    assert shards[-1].start == date(2026, 5, 24)
    assert shards[-1].end == TRANSFER_END


def test_load_preserves_bar_boundary_gap_and_static_identity_only(tmp_path):
    first = date(2024, 1, 1)
    month = next(shard_plan("DOGEUSDT", first, date(2024, 2, 1)))
    hour = epoch_hour(first)
    fixture_archive(
        tmp_path,
        month,
        [csv_bar(hour), csv_bar(hour + 1), csv_bar(hour + 3), csv_bar(hour + 4)],
    )
    identity = MarketHistory(
        "DOGE",
        2,
        True,
        hour - 72,
        {hour: Bar(9, 9, 9, 9, 10)},
        {hour: Funding(hour * HOUR_MS, 0.001)},
    )
    unrelated = MarketHistory("BTC", 4, False, None, {}, {})
    actual = load_transfer(tmp_path, hour + 1, hour + 4, {"DOGE": identity, "BTC": unrelated})
    assert set(actual) == {"DOGE"}
    doge = actual["DOGE"]
    assert doge.decimals == 2
    assert doge.is_meme
    assert doge.listing_hour is None
    assert doge.funding == doge.quarter_bars == {}
    bar = Bar(2, 3, 1, 2.1, 40)
    assert doge.bars == {hour + 1: bar, hour + 3: bar}


def test_corrupt_checksum_and_conflicting_candles_are_rejected(tmp_path):
    first = date(2026, 5, 23)
    hour = epoch_hour(first)
    shard = next(shard_plan("DOGEUSDT", first, TRANSFER_END))
    identity = {"DOGE": MarketHistory("DOGE", 2, True, None, {}, {})}
    archive = fixture_archive(
        tmp_path, shard, [csv_bar(hour), csv_bar(hour, closing=2.2)], header=False
    )
    with pytest.raises(ValueError, match="Conflicting DOGEUSDT"):
        load_transfer(tmp_path, hour, hour + 24, identity)
    archive.write_bytes(b"modified")
    with pytest.raises(ValueError, match="checksum"):
        load_transfer(tmp_path, hour, hour + 24, identity)


def test_invalid_close_and_transfer_overrun_are_rejected(tmp_path):
    first = date(2026, 5, 24)
    hour = epoch_hour(first)
    shard = next(shard_plan("DOGEUSDT", first, TRANSFER_END))
    identity = {"DOGE": MarketHistory("DOGE", 2, True, None, {}, {})}
    fixture_archive(tmp_path, shard, [csv_bar(hour, close_offset=1)])
    with pytest.raises(ValueError, match="Invalid OHLCV"):
        load_transfer(tmp_path, hour, hour + 24, identity)
    with pytest.raises(ValueError, match="transfer cutoff"):
        load_transfer(tmp_path, hour, hour + 25, identity)
