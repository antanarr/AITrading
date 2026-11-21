import json
from decimal import Decimal

import httpx
import pytest

from copybot.perps import Feed
from copybot.research_data import ARCHIVE_END_HOUR, load_archive
from copybot.research_models import HOUR_MS


def save(root, filename, rows):
    path = root / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows))


def candle(coin, stamp, interval="1h", price="10"):
    widths = {"1h": HOUR_MS, "1d": 24 * HOUR_MS, "15m": HOUR_MS // 4}
    return {
        "s": coin,
        "i": interval,
        "t": stamp,
        "T": stamp + widths[interval] - 1,
        "o": price,
        "h": price,
        "l": price,
        "c": price,
        "v": "100",
    }


def funding(coin, hour, rate="0.0001"):
    return {"coin": coin, "time": hour * HOUR_MS + 123, "fundingRate": rate, "premium": "-0.01"}


def test_archive_merges_shards_and_truncates_all_inputs_before_validation(tmp_path):
    first = ARCHIVE_END_HOUR - 24
    end = first + 4
    save(
        tmp_path,
        "meta.json",
        {
            "universe": [
                {"name": "NEW", "szDecimals": 1},
                {"name": "SHUT", "szDecimals": 2, "isDelisted": True},
                {"name": "EMPTY", "szDecimals": 2},
                {"name": "BTC", "szDecimals": 3},
                {"name": "xyz:BTC", "szDecimals": 3},
                {"name": "xyz:ETH", "szDecimals": 3},
            ]
        },
    )
    save(
        tmp_path,
        "taxonomy.json",
        {
            "source": "Fixture research",
            "captured_at_utc": "2026-11-20T00:00:00Z",
            "assets": {
                "NEW": {
                    "provider_id": "verified-new",
                    "category": "meme",
                    "name": "New Coin",
                    "source": "https://example.com/new",
                }
            },
        },
    )
    early = candle("NEW", (first + 1) * HOUR_MS)
    late = candle("NEW", (end - 1) * HOUR_MS)
    save(tmp_path, "hour-NEW.json", [early, {"t": end * HOUR_MS}])
    save(tmp_path, "other-coins/hour-NEW.json", [late, early])
    save(tmp_path, "expansion/day-NEW.json", [candle("NEW", first * HOUR_MS, "1d")])
    save(tmp_path, "fund-NEW-0.json", [funding("NEW", first + 2), {"time": end * HOUR_MS}])
    save(tmp_path, "expansion/fund-NEW-0.json", [funding("NEW", first + 2)])
    save(
        tmp_path,
        "quarter-NEW.json",
        [
            candle("NEW", (end - 1) * HOUR_MS, "15m"),
            {"t": end * HOUR_MS},
        ],
    )
    save(tmp_path, "hour-SHUT.json", [candle("SHUT", first * HOUR_MS)])
    save(tmp_path, "hour-BTC.json", {"unexpected": "excluded before read"})
    save(tmp_path, "hour-xyz:BTC.json", {"unexpected": "excluded before read"})

    histories, coverage = load_archive(tmp_path, end, include_quarters=True)
    assert set(histories) == {"NEW", "SHUT"}
    assert histories["NEW"].is_meme
    assert not histories["SHUT"].is_meme
    assert histories["NEW"].listing_hour is None
    assert list(histories["NEW"].bars) == [first + 1, end - 1]
    assert list(histories["NEW"].funding) == [first + 2]
    assert histories["NEW"].funding[first + 2].premium == -0.01
    assert list(histories["NEW"].quarter_bars) == [(end - 1) * HOUR_MS]
    markets = coverage["markets"]
    assert markets["NEW"]["hourly"]["interior_gaps"] == 1
    assert all(count == 0 for count in markets["NEW"]["invalid_rows"].values())
    assert markets["SHUT"]["delisted_in_census"]
    assert markets["SHUT"]["category"] == "unknown"
    assert markets["EMPTY"]["status"] == "no_hourly_history"
    assert coverage["status_counts"] == {"available": 2, "no_hourly_history": 1}

    histories, _ = load_archive(tmp_path, first + 24)
    assert histories["NEW"].listing_hour == first + 1
    assert not histories["NEW"].quarter_bars


def test_conflicting_or_inconsistent_rows_leave_visible_archive_gaps(tmp_path):
    first = ARCHIVE_END_HOUR - 24
    save(tmp_path, "meta.json", {"universe": [{"name": "ALT", "szDecimals": 1}]})
    save(
        tmp_path,
        "hour-ALT.json",
        [candle("ALT", hour * HOUR_MS) for hour in range(first, first + 3)],
    )
    save(tmp_path, "expansion/hour-ALT.json", [candle("ALT", (first + 1) * HOUR_MS, price="11")])
    save(tmp_path, "expansion/fund-ALT-0.json", [funding("ALT", first + 1)])
    save(tmp_path, "fund-ALT-0.json", [funding("ALT", first + 1, rate="0.1")])
    save(
        tmp_path,
        "quarter-ALT.json",
        [
            candle("ALT", stamp, "15m", price="11")
            for stamp in range(first * HOUR_MS, (first + 1) * HOUR_MS, HOUR_MS // 4)
        ],
    )
    histories, coverage = load_archive(tmp_path, first + 3, include_quarters=True)
    market = coverage["markets"]["ALT"]
    assert list(histories["ALT"].bars) == [first, first + 2]
    assert not histories["ALT"].funding
    assert not histories["ALT"].quarter_bars
    assert market["conflicts"] == {"hourly": 1, "daily": 0, "funding": 1, "quarter": 0}
    assert market["hourly"]["interior_gaps"] == market["missing_funding_for_hourly"] == 1
    assert market["quarter_hourly_ohlc_mismatches"] == 1


def test_taxonomy_requires_a_verified_identity_for_named_categories(tmp_path):
    save(tmp_path, "meta.json", {"universe": [{"name": "ALT", "szDecimals": 1}]})
    save(
        tmp_path,
        "taxonomy.json",
        {
            "source": "Fixture research",
            "captured_at_utc": "2026-11-20T00:00:00Z",
            "assets": {"ALT": {"category": "meme"}},
        },
    )
    with pytest.raises(ValueError, match="needs a sourced identity"):
        load_archive(tmp_path, ARCHIVE_END_HOUR)


def test_live_scan_reads_all_eligible_native_contexts_without_bitcoin(monkeypatch):
    coins = [*(f"ALT{index}" for index in range(16)), "BTC", "xyz:BTC", "DELISTED", "BROKEN"]
    rows = [{"name": name, "szDecimals": 2} for name in coins]
    rows[-2]["isDelisted"] = True
    valid = {
        "midPx": "10",
        "markPx": "10.1",
        "dayNtlVlm": "10000001",
        "dayBaseVlm": "1000000",
        "funding": "-0.0001",
        "prevDayPx": "9.9",
        "openInterest": "10000",
        "oraclePx": "10.01",
        "premium": "-0.001",
        "impactPxs": ["9.99", "10.01"],
    }
    contexts = [valid] * (len(coins) - 1) + [{**valid, "markPx": "NaN"}]
    inspected = {}

    def handler(request):
        assert request.url.host == "api.hyperliquid.xyz"
        assert json.loads(request.content) == {"type": "metaAndAssetCtxs", "dex": ""}
        return httpx.Response(200, json=[{"universe": rows}, contexts])

    def inspect(market, _):
        inspected[market.coin] = market

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        feed = Feed(client)
        monkeypatch.setattr(feed, "signal", inspect)
        _, coverage = feed.scan()

    assert coverage["evaluated"] == 16
    assert set(inspected) == set(coins[:16])
    first = inspected["ALT0"]
    assert (first.mid, first.mark, first.oracle) == (
        Decimal("10"),
        Decimal("10.1"),
        Decimal("10.01"),
    )
    assert (first.open_interest, first.volume_base) == (Decimal("10000"), Decimal("1000000"))
    assert first.premium == Decimal("-0.001")
    assert (first.impact_bid, first.impact_ask) == (Decimal("9.99"), Decimal("10.01"))


def test_feed_requests_native_candles_and_funding_from_hyperliquid():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        feed = Feed(client)
        for interval in ("15m", "1h", "4h", "1d"):
            feed.candles("ALT", interval, 100, 200)
        feed.funding_history("ALT", 100, 200)

    assert requests == [
        {
            "type": "candleSnapshot",
            "req": {"coin": "ALT", "interval": interval, "startTime": 100, "endTime": 200},
        }
        for interval in ("15m", "1h", "4h", "1d")
    ] + [{"type": "fundingHistory", "coin": "ALT", "startTime": 100, "endTime": 200}]
