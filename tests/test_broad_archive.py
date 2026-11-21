from datetime import UTC, date, datetime

from copybot.broad_archive import HistorySource, copy_seed, fill_funding, read, write
from copybot.research_models import HOUR_MS, Bar, Funding, MarketHistory


def test_copy_seed_ends_at_cutoff_and_skips_nonmarket_files(tmp_path):
    seed, output = tmp_path / "old", tmp_path / "broad"
    cutoff = int(datetime(2026, 11, 21, tzinfo=UTC).timestamp() * 1000)
    before = {"t": cutoff - HOUR_MS, "T": cutoff - 1}
    future = {"t": cutoff, "T": cutoff + HOUR_MS - 1}
    write(seed / "meta.json", {"universe": []})
    write(seed / "taxonomy.json", {"assets": {}})
    write(seed / "hour-DOGE.json", [before, future])
    write(seed / "hour-BTC.json", [before, future])
    write(seed / "fund-DOGE-1.json", [{"time": cutoff - 1}, {"time": cutoff}])
    write(seed / "reserve-results.json", {"sealed": True})

    copy_seed(seed, output, cutoff)

    assert read(output / "seed" / "hour-DOGE.json") == [before]
    assert read(output / "seed" / "fund-DOGE-1.json") == [{"time": cutoff - 1}]
    assert read(output / "taxonomy.json") == {"assets": {}}
    assert not (output / "seed" / "hour-BTC.json").exists()
    assert not (output / "seed" / "reserve-results.json").exists()
    assert read(seed / "hour-DOGE.json") == [before, future]


def test_candle_and_funding_requests_use_exclusive_end_and_weight(monkeypatch):
    source = HistorySource(None, 1_100)
    calls = []
    cutoff = int(
        datetime.combine(date(2026, 11, 21), datetime.min.time(), tzinfo=UTC).timestamp() * 1000
    )
    start = cutoff - 120 * HOUR_MS

    def fetch(body, weight):
        calls.append((body, weight))
        if body["type"] == "candleSnapshot":
            return [
                {"t": cutoff - HOUR_MS, "T": cutoff - 1},
                {"t": cutoff, "T": cutoff + HOUR_MS - 1},
            ]
        return [{"time": start}, {"time": cutoff}]

    monkeypatch.setattr(source, "fetch", fetch)
    assert source.candles("DOGE", "1h", start, cutoff) == [{"t": cutoff - HOUR_MS, "T": cutoff - 1}]
    assert source.funding("DOGE", start, cutoff) == [{"time": start}]
    assert calls == [
        (
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": "DOGE",
                    "interval": "1h",
                    "startTime": start,
                    "endTime": cutoff - 1,
                },
            },
            22,
        ),
        ({"type": "fundingHistory", "coin": "DOGE", "startTime": start, "endTime": cutoff - 1}, 26),
    ]


def test_funding_fetch_only_requests_missing_span(tmp_path):
    bars = {hour: Bar(2, 3, 1, 2.5, 12) for hour in range(1_000, 1_006)}
    history = MarketHistory(
        "PS",
        2,
        True,
        None,
        bars,
        {hour: Funding(hour * HOUR_MS, 0.1) for hour in (1_001, 1_002, 1_004)},
    )

    class Source:
        def __init__(self):
            self.calls = []

        def funding(self, coin, start, end):
            self.calls.append((coin, start, end))
            return [{"time": hour * HOUR_MS} for hour in range(start // HOUR_MS, end // HOUR_MS)]

    source = Source()
    fill_funding(source, tmp_path, history, 1_006, None)
    assert source.calls == [("PS", 1_003 * HOUR_MS, 1_006 * HOUR_MS)]
    fill_funding(source, tmp_path, history, 1_006, None)
    assert len(source.calls) == 1
