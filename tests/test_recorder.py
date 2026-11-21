import json
import random
import sqlite3
from collections import deque

import pytest
import websocket

from copybot import recorder

STAMP = 1_800_000_000_000


def meta_response(*assets):
    universe = [
        {"name": coin, "szDecimals": 2, "isDelisted": delisted} for coin, _, delisted in assets
    ]
    contexts = [
        {
            "dayNtlVlm": volume,
            "openInterest": "120",
            "oraclePx": "1.01",
            "markPx": "1.00",
            "premium": None,
            "funding": "-0.0005",
            "impactPxs": ["0.99", "1.02"],
        }
        for _, volume, _ in assets
    ]
    return [{"universe": universe}, contexts]


def rows(archive, kind):
    result = []
    for part in archive.files():
        with sqlite3.connect(part) as connection:
            result.extend(
                connection.execute(
                    """
                SELECT coin, exchange_ms, received_ms, trade_id, payload
                FROM observations WHERE kind = ? ORDER BY id
                """,
                    (kind,),
                )
            )
    return result


def trade(coin="DOGE", side="B"):
    return {
        "coin": coin,
        "side": side,
        "px": "100",
        "sz": "2.5",
        "time": STAMP,
        "tid": 4,
        "users": ["buyer_address", "seller_address"],
        "hash": "transaction_hash",
    }


def book(time=STAMP):
    return {
        "coin": "DOGE",
        "time": time,
        "levels": [
            [{"px": "99.95", "sz": "2", "n": 1}, {"px": "99.90", "sz": "3", "n": 2}],
            [{"px": "100.05", "sz": "4", "n": 3}, {"px": "100.26", "sz": "5", "n": 1}],
        ],
    }


def test_meta_covers_native_universe_and_ranks_only_eligible_active_markets(tmp_path):
    response = meta_response(
        ("BTC", "1000", False),
        ("DOGE", "10", False),
        ("NOVA", "50", False),
        ("WIF", "900", True),
        ("NEUTRAL", "110", False),
        ("OLD", "500", False),
    )
    with recorder.Archive(tmp_path, 32 * recorder.MIB, 30) as archive:
        run = recorder.Recorder(archive, top_n=2, book_interval=5)
        run.meta(response, STAMP)
        assert run.watched == ("DOGE",)
        assert run.listing_candidate(STAMP) == "OLD"
        midnight = STAMP // recorder.DAY_MS * recorder.DAY_MS
        run.listing([{"t": midnight - 40 * recorder.DAY_MS, "s": "OLD", "i": "1d"}], STAMP, "OLD")
        assert run.listing_candidate(STAMP) == "NEUTRAL"
        run.listing([], STAMP, "NEUTRAL")
        assert run.listing_candidate(STAMP) == "NOVA"
        run.listing([{"t": midnight, "s": "NOVA", "i": "1d"}], STAMP, "NOVA")
        assert run.watched == ("NOVA", "DOGE")
        assert run.listing_candidate(STAMP) == "DOGE"
        assert run.listing_candidate(STAMP + recorder.LISTING_RETRY_MS) == "NEUTRAL"
        run.meta(response, STAMP + 60_000)
        archive.flush()
        raw_snapshot = rows(archive, "meta")[-1][-1]
        assert isinstance(raw_snapshot, bytes)
        snapshot = recorder.decode_payload(raw_snapshot)
        assert set(snapshot["markets"]) == {asset["name"] for asset in response[0]["universe"]}
        assert snapshot["markets"]["DOGE"]["openInterest"] == "120"
        assert snapshot["markets"]["DOGE"]["oraclePx"] == "1.01"
        assert snapshot["markets"]["DOGE"]["markPx"] == "1.00"
        assert snapshot["markets"]["DOGE"]["premium"] is None
        assert snapshot["markets"]["DOGE"]["funding"] == "-0.0005"
        assert snapshot["markets"]["DOGE"]["impactPxs"] == ["0.99", "1.02"]
        assert snapshot["markets"]["DOGE"]["dayNtlVlm"] == "10"
        assert snapshot["markets"]["WIF"]["isDelisted"] is True
        assert snapshot["markets"]["NOVA"]["recent_listing_estimate"] is True
        assert snapshot["markets"]["NEUTRAL"]["recent_listing_estimate"] is None
        assert len(rows(archive, "listing")) == 3


def test_public_messages_save_only_anonymous_trade_fields_and_sampled_book_depth(tmp_path):
    with recorder.Archive(tmp_path, 32 * recorder.MIB, 30) as archive:
        run = recorder.Recorder(archive, top_n=2, book_interval=5)
        run.select(("DOGE",))
        receive_time = STAMP + 42
        trades = [
            trade(),
            trade(),
            trade("BTC"),
            trade(side="unknown"),
            {**trade(side="A"), "tid": 5},
        ]
        run.feed.message(json.dumps({"channel": "trades", "data": trades}), receive_time)
        for exchange_time in (STAMP, STAMP + 10, STAMP - 100, STAMP + 5_000):
            run.feed.message(
                json.dumps({"channel": "l2Book", "data": book(exchange_time)}),
                receive_time,
            )
        archive.flush()
        written_trades = rows(archive, "trade")
        assert len(written_trades) == 2
        assert written_trades[0][:4] == ("DOGE", STAMP, receive_time, 4)
        assert [json.loads(row[-1]) for row in written_trades] == [
            {"side": "buy", "px": "100", "sz": "2.5"},
            {"side": "sell", "px": "100", "sz": "2.5"},
        ]
        written_books = rows(archive, "book")
        assert len(written_books) == 2
        assert written_books[0][:4] == ("DOGE", STAMP, receive_time, None)
        payload = recorder.decode_payload(written_books[0][-1])
        assert payload["bids"] == book()["levels"][0]
        assert payload["asks"] == book()["levels"][1]
        assert float(payload["spread_bps"]) == 10
        assert payload["observed_depth"]["5"]["bid"]["usd_ntl"] == "199.90"
        assert payload["observed_depth"]["5"]["ask"]["base_sz"] == "4"
        assert payload["observed_depth"]["10"]["bid"]["base_sz"] == "5"
        assert payload["observed_depth"]["25"]["bid"]["outermost_level_reaches_band"] is False
        assert payload["observed_depth"]["25"]["ask"]["outermost_level_reaches_band"] is True
        assert run.counters == {
            "duplicate_trades_in_memory": 1,
            "untracked_trades": 1,
            "invalid_trade_rows": 1,
            "downsampled_books": 1,
            "out_of_order_books": 1,
        }


def test_reconnect_records_gap_and_resubscribes_without_duplicate_trades(tmp_path, monkeypatch):
    class Socket:
        def __init__(self):
            self.packets = deque()
            self.sent = []
            self.closed = False
            self.fail = False

        def settimeout(self, _timeout):
            pass

        def send(self, raw):
            packet = json.loads(raw)
            self.sent.append(packet)
            self.packets.append(json.dumps({"channel": "subscriptionResponse", "data": packet}))

        def recv(self):
            if self.fail:
                raise websocket.WebSocketConnectionClosedException()
            return self.packets.popleft()

        def shutdown(self):
            self.closed = True

    first, second = Socket(), Socket()
    sockets = iter((first, second))
    monkeypatch.setattr(websocket, "create_connection", lambda *_args, **_kwargs: next(sockets))
    with recorder.Archive(tmp_path, 32 * recorder.MIB, 30) as archive:
        run = recorder.Recorder(archive, top_n=1, book_interval=5)
        run.select(("DOGE",))
        assert run.feed.poll()
        assert not run.feed.ready()
        assert run.feed.poll()
        assert run.feed.ready()
        packet = json.dumps({"channel": "trades", "data": [trade()]})
        first.packets.append(packet)
        assert run.feed.poll()
        last_packet_ms = run.feed.last_packet_ms
        first.fail = True
        assert not run.feed.poll()
        assert first.closed
        assert run.feed.disconnected_since_ms == last_packet_ms
        run.feed.reconnect_at = 0
        assert run.feed.poll()
        assert run.feed.poll()
        assert run.feed.ready()
        assert run.feed.disconnected_since_ms is None
        assert first.sent == second.sent
        second.packets.append(packet)
        assert run.feed.poll()
        archive.flush()
        assert len(rows(archive, "trade")) == 1
        health = [json.loads(item[-1]) for item in rows(archive, "health")]
        recovered = next(item for item in health if item["event"] == "ws_recovered")
        assert recovered["gap_start_ms"] == last_packet_ms
        assert recovered["replay_complete"] is False
        assert run.counters["ws_connections"] == 2
        assert run.counters["ws_disconnects"] == 1


def test_archive_reopens_deduplicates_rotates_and_prunes(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, "PART_BYTES", 40 * 1024)
    event = recorder.trade_event(trade(), STAMP + 1)
    with recorder.Archive(tmp_path, 85 * 1024, 2) as archive:
        archive.add(event)
        archive.flush(STAMP)
        with (
            pytest.raises(RuntimeError, match="Another recorder"),
            recorder.Archive(
                tmp_path,
                85 * 1024,
                2,
            ),
        ):
            pass
    with recorder.Archive(tmp_path, 85 * 1024, 2) as archive:
        archive.add(event)
        archive.flush(STAMP + 1)
        assert len(archive.files()) == 1
        assert len(rows(archive, "trade")) == 1
        assert archive.counts["duplicate_trades_on_disk"] == 1
        source = random.Random(4)
        for offset in range(2, 10):
            bulk = source.randbytes(24_000).hex()
            archive.add(recorder.Event("meta", STAMP + offset, {"bulk": bulk}))
            archive.flush(STAMP + offset)
        assert archive.size_bytes <= archive.max_bytes
        assert archive.counts["pruned_parts"] > 0
        assert len(archive.files()) >= 1
        archive.add(recorder.Event("health", STAMP + 3 * recorder.DAY_MS, {"event": "heartbeat"}))
        archive.flush(STAMP + 3 * recorder.DAY_MS)
        assert len(archive.files()) == 1
        assert len(rows(archive, "health")) == 1


def test_once_uses_only_public_meta_and_persists_network_failures(tmp_path, monkeypatch):
    def http_roundtrip(_client, coin):
        assert coin is None
        return meta_response(("DOGE", "10", False)), STAMP

    def unexpected_ws(*_args, **_kwargs):
        pytest.fail("One-shot capture must not open a websocket")

    monkeypatch.setattr(
        recorder.Recorder,
        "request",
        staticmethod(
            lambda client, coin=None: http_roundtrip(client, coin),
        ),
    )
    monkeypatch.setattr(websocket, "create_connection", unexpected_ws)
    with recorder.Archive(tmp_path, 32 * recorder.MIB, 30) as archive:
        run = recorder.Recorder(archive, top_n=1, book_interval=5)
        assert run.run(once=True, run_seconds=None) == 0
        assert len(rows(archive, "meta")) == 1

        def invalid_meta():
            return meta_response(), STAMP

        assert not run.capture(invalid_meta)
        archive.flush()
        health = [json.loads(item[-1])["event"] for item in rows(archive, "health")]
        assert "public_http_error" in health
        assert len(rows(archive, "meta")) == 1
