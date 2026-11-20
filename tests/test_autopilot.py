from dataclasses import replace
from decimal import Decimal
from unittest.mock import Mock

import httpx
import pytest

from copybot.autopilot import Journal, Runner, Settings, entry_order
from copybot.hyperliquid import TradingError
from copybot.perps import (
    BAR_MS,
    SENTIMENT_MAX_AGE_MS,
    Book,
    Feed,
    Market,
    Signal,
    breakout,
    market_breadth,
    now_ms,
    rank_signal,
    round_price,
)

MARKET = Market("BTC", 5, Decimal("100000000"), Decimal(0), Decimal("100"))
BOOK = Book(Decimal("99.99"), Decimal("100.01"), Decimal(1), Decimal(1), now_ms())


@pytest.mark.parametrize("long", [True, False])
def test_breakout_uses_only_completed_contiguous_bars(long):
    bars = []
    for index in range(25):
        close = 100 if index != 23 else (103 if long else 97)
        bars.append(
            {
                "s": "BTC",
                "i": "15m",
                "t": index * BAR_MS,
                "T": (index + 1) * BAR_MS - 1,
                "c": str(close),
                "h": "101",
                "l": "99",
                "v": "200" if index == 23 else "100",
            }
        )
    signal = breakout(MARKET, bars, 24 * BAR_MS + 10)
    assert signal.long == long
    assert signal.bar_end == bars[23]["T"]
    assert signal.relative_volume == Decimal(2)
    assert breakout(MARKET, bars[1:], 24 * BAR_MS + 10) is None


def test_sentiment_refresh_uses_recent_cache_and_rejects_expired_readings():
    timestamp = 1_800_000_000_000
    requested = []

    def respond(request):
        requested.append(request.url.host)
        if len(requested) > 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "metadata": {"error": None},
                "data": [
                    {"value": "18", "timestamp": str(timestamp // 1000)},
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        feed = Feed(client)
        current = feed.fear_and_greed(timestamp)
        assert current["index"] == 18 and current["source"] == "Alternative.me"
        assert feed.fear_and_greed(timestamp + 60_000) == current
        cached = feed.fear_and_greed(timestamp + 60 * 60 * 1000)
        assert cached["status"] == "cached" and cached["index"] == 18
        stale = feed.fear_and_greed(timestamp + SENTIMENT_MAX_AGE_MS + 1)
        assert stale["status"] == "stale" and "index" not in stale
        assert requested == ["api.alternative.me"] * 3


def test_crypto_context_applies_to_native_perps_only():
    markets = [
        replace(MARKET, coin=coin, change_24h=Decimal("-0.02"))
        for coin in ("BTC", "ETH", "SOL", "HYPE", "XRP")
    ]
    builder = replace(MARKET, coin="xyz:XYZ100", change_24h=Decimal("0.03"))
    breadth = market_breadth([*markets, builder], min_volume=10_000_000)
    assert breadth == {"advancing": 0, "declining": 5, "total": 5}
    native_short = Signal(markets[0], False, 1, Decimal(100), Decimal(99), Decimal("1.8"))
    builder_short = replace(native_short, market=builder)
    assert rank_signal(native_short, breadth, {"index": 18}).score == 4
    assert rank_signal(builder_short, breadth, {"index": 18}).score == 2
    assert rank_signal(native_short, breadth, {"status": "unavailable"}).score == 3


def test_scan_prioritizes_ranked_signals_and_keeps_existing_funding_limit(monkeypatch):
    high_volume = replace(MARKET, coin="ETH", volume=Decimal("200000000"))
    expensive = replace(MARKET, coin="HYPE", funding=Decimal("0.001"))
    feed = Feed(Mock())
    monkeypatch.setattr(
        feed,
        "markets",
        lambda dex: {asset.coin: asset for asset in (MARKET, high_volume, expensive)},
    )
    monkeypatch.setattr(feed, "fear_and_greed", lambda observed_at: {"status": "unavailable"})
    monkeypatch.setattr(
        feed,
        "signal",
        lambda asset, observed_at: Signal(
            asset,
            True,
            observed_at - 1,
            Decimal(100),
            Decimal(101),
            Decimal("0.5") if asset == high_volume else Decimal(2),
        ),
    )
    signals, scan = feed.scan(allowed=["BTC", "ETH", "HYPE"])
    assert [signal.market.coin for signal in signals] == ["BTC", "ETH"]
    assert scan["evaluated"] == 3
    assert signals[0].evidence()["breakout_volume_vs_median"] == "2.00"


@pytest.mark.parametrize("long", [True, False])
def test_sizing_respects_tick_minimum_and_configured_budget(long):
    signal = Signal(MARKET, long, now_ms() - 5000, Decimal(99 if long else 101), Decimal(100))
    assert entry_order(signal, BOOK, Settings(markets=["BTC"], max_order_usd=10)) is None
    size, limit = entry_order(signal, BOOK, Settings(markets=["BTC"], max_order_usd="10.10"))
    quote = BOOK.ask if long else BOOK.bid
    assert size * min(quote, limit) >= 10
    assert size * max(quote, limit, MARKET.mark) <= Decimal("10.10")
    assert size.as_tuple().exponent == -5
    assert round_price(limit, 5) == limit


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "journal.sqlite"
    journal = Journal(path)
    gateway = Mock()
    feed = Mock()
    feed.book.return_value = BOOK
    feed.scan.return_value = (
        [Signal(MARKET, True, now_ms() - 5000, Decimal(99), Decimal(100))],
        {},
    )
    gateway.market_state.return_value = None
    gateway.lookup.return_value = None
    gateway.flat_market.return_value = True
    gateway.order.return_value = None
    settings = Settings(markets=["BTC"], max_order_usd="10.10")
    yield Runner(feed, gateway, journal, settings), path
    journal.db.close()


def test_entry_timeout_restarts_with_the_same_pending_id(setup):
    runner, path = setup
    runner.gateway.order.side_effect = httpx.ReadTimeout("timeout")
    with pytest.raises(httpx.ReadTimeout):
        runner.step()
    restarted_journal = Journal(path)
    trade = restarted_journal.get("trade")
    restarted = Runner(runner.feed, runner.gateway, restarted_journal, runner.settings)
    with pytest.raises(TradingError, match="entry is unconfirmed"):
        restarted.step()
    restarted.gateway.lookup.assert_called_once_with("BTC", trade["entry_id"])
    assert restarted.gateway.order.call_count == 1
    restarted_journal.db.close()


def test_partial_entry_stop_and_take_profit_while_paused(setup):
    runner, _ = setup
    gateway = runner.gateway
    orders = {}
    position = {"szi": "0.05", "entryPx": "100"}
    gateway.lookup.side_effect = lambda coin, cloid: orders.get(cloid)

    def send(coin, buy, size, limit, cloid, *, reduce_only=False, stop=None):
        assert coin == "BTC"
        if not reduce_only:
            orders[cloid] = {
                "status": "canceled",
                "order": {
                    "origSz": str(size),
                    "sz": str(size - Decimal("0.05")),
                },
            }
            gateway.market_state.return_value = position
        elif stop is not None:
            assert not buy and size == Decimal("0.05")
            orders[cloid] = {
                "status": "open",
                "order": {
                    "reduceOnly": True,
                    "isTrigger": True,
                    "side": "A",
                    "triggerPx": str(stop),
                    "origSz": str(size),
                },
            }
        else:
            assert not buy and size == Decimal("0.05")
            orders[cloid] = {"status": "filled"}
            gateway.market_state.return_value = None

    gateway.order.side_effect = send
    assert runner.step() == "Monitoring protected BTC position"
    trade = runner.journal.get("trade")
    assert Decimal(trade["owned_size"]) == Decimal("0.05")
    runner.journal.put("paused", True)
    runner.feed.book.return_value = Book(
        Decimal("102"), Decimal("102.02"), Decimal(1), Decimal(1), now_ms()
    )
    assert "take profit" in runner.step()
    assert "Canceling" in runner.step()
    gateway.cancel.assert_called_once_with("BTC", trade["stop_id"])
    orders[trade["stop_id"]]["status"] = "canceled"
    assert runner.step() == "Confirmed flat on BTC"
    assert runner.step() == "Entries paused"


def test_stop_rejection_sends_reduce_only_exit(setup):
    runner, _ = setup
    gateway = runner.gateway
    position = {"szi": "0.10", "entryPx": "100"}

    def send(coin, buy, size, limit, cloid, *, reduce_only=False, stop=None):
        if not reduce_only:
            gateway.lookup.return_value = {"status": "filled", "order": {"origSz": str(size)}}
            gateway.market_state.return_value = position
        if stop is not None:
            return "invalid trigger"
        return None

    gateway.order.side_effect = send
    assert "protective stop rejected" in runner.step()
    assert gateway.order.call_args.kwargs == {"reduce_only": True}
    assert gateway.order.call_count == 3


def test_broker_rejection_does_not_reuse_signal(setup):
    runner, _ = setup
    runner.gateway.order.return_value = "IOC did not execute"
    assert runner.step() == "No fill on BTC"
    assert runner.step()["result"] == "No eligible entry"
    assert runner.gateway.order.call_count == 1
