from dataclasses import replace
from decimal import Decimal
from unittest.mock import Mock

import httpx
import pytest

from copybot.autopilot import Journal, Runner, Settings, entry_order, utc_day
from copybot.hyperliquid import AccountGateway, TradingError
from copybot.perp_ledger import ExposureConflict
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
    feed.exit_price.return_value = BOOK.bid
    feed.closed_candle.return_value = None
    feed.scan.return_value = (
        [Signal(MARKET, True, now_ms() - 5000, Decimal(99), Decimal(100))],
        {},
    )
    gateway.configured_exposure.return_value = []
    gateway.market_state.return_value = None
    gateway.lookup.return_value = None
    gateway.historical_order.return_value = None
    gateway.flat_market.return_value = True
    gateway.has_market_fills.return_value = False
    gateway.market_fills.return_value = []
    gateway.market_funding.return_value = Decimal(0)
    gateway.enter.return_value = {}
    gateway.order.return_value = None
    settings = Settings(markets=["BTC"], max_order_usd="10.10")
    yield Runner(feed, gateway, journal, settings), path
    journal.db.close()


def fill(oid, side, size, price, start, *, fee="0.001", timestamp=None):
    return {
        "oid": oid,
        "tid": oid,
        "time": now_ms() if timestamp is None else timestamp,
        "side": side,
        "sz": str(size),
        "px": str(price),
        "startPosition": str(start),
        "feeToken": "USDC",
        "fee": fee,
    }


def open_stop(trigger, size):
    return {
        "status": "open",
        "order": {
            "reduceOnly": True,
            "isTrigger": True,
            "side": "A",
            "triggerPx": str(trigger),
            "origSz": str(size),
        },
    }


def enter_fixture(gateway, order_status, *, partial=None):
    def enter(trade):
        size = partial or trade["requested_size"]
        gateway.market_state.return_value = {"szi": size}
        gateway.market_fills.return_value = [fill(101, "B", size, 100, 0)]
        order_status[trade["stop_id"]] = (
            {"status": "canceled"} if partial else open_stop(trade["stop_trigger"], size)
        )
        return {"entry_oid": 101, "acknowledged_size": size}

    gateway.enter.side_effect = enter
    gateway.lookup.side_effect = lambda coin, cloid: order_status.get(cloid)


def test_entry_timeout_restarts_with_the_same_pending_id(setup, monkeypatch):
    runner, path = setup
    runner.gateway.enter.side_effect = httpx.ReadTimeout("timeout")
    with pytest.raises(httpx.ReadTimeout):
        runner.step()
    restarted_journal = Journal(path)
    trade = restarted_journal.get("trade")
    assert trade["stop_id"]
    restarted = Runner(runner.feed, runner.gateway, restarted_journal, runner.settings)
    assert "Reconciling entry" in restarted.step()
    restarted.gateway.lookup.assert_called_once_with("BTC", trade["entry_id"])
    monkeypatch.setattr("copybot.autopilot.now_ms", lambda: trade["entry_expires"] + 5000)
    assert "Reconciling entry" in restarted.step()
    assert "Confirmed flat" in restarted.step()
    assert restarted_journal.get("trade") is None
    assert restarted.gateway.enter.call_count == 1
    restarted_journal.db.close()


def test_missing_order_details_do_not_hide_an_independent_fill(setup, monkeypatch):
    runner, _ = setup
    runner.gateway.enter.side_effect = httpx.ReadTimeout("unknown submission result")
    with pytest.raises(httpx.ReadTimeout):
        runner.step()
    trade = runner.journal.get("trade")
    runner.gateway.has_market_fills.return_value = True
    monkeypatch.setattr("copybot.autopilot.now_ms", lambda: trade["entry_expires"] + 5000)
    for _ in range(3):
        assert "Reconciling entry" in runner.step()
    assert runner.journal.get("trade")["entry_id"] == trade["entry_id"]
    assert runner.gateway.enter.call_count == 1


def test_partial_entry_replaces_canceled_stop_and_books_net_pnl_while_paused(setup, monkeypatch):
    runner, _ = setup
    gateway = runner.gateway
    orders = {}
    enter_fixture(gateway, orders, partial="0.05")
    filled_ms = now_ms()

    def send(coin, buy, size, limit, cloid, *, reduce_only=False, stop=None, expires_at=None):
        assert coin == "BTC" and not buy and reduce_only and size == Decimal("0.05")
        if stop is not None:
            orders[cloid] = open_stop(stop, size)
        else:
            orders[cloid] = {"status": "filled"}
            gateway.market_state.return_value = None
            gateway.market_fills.return_value.append(
                fill(102, "A", size, 102, "0.05", fee="0.002", timestamp=filled_ms)
            )

    gateway.order.side_effect = send
    assert runner.step() == "Monitoring protected BTC position"
    trade = runner.journal.get("trade")
    assert trade["standalone_stop"] and trade["attached_stop_id"] != trade["stop_id"]
    runner.journal.put("paused", True)
    gateway.market_state.return_value = None
    assert "Reconciling filled size" in runner.step()
    gateway.cancel.assert_not_called()
    gateway.market_state.return_value = {"szi": "0.05"}
    runner.feed.exit_price.return_value = Decimal("102")
    assert "take profit" in runner.step()
    assert "Rechecking flat" in runner.step()
    assert "Canceling" in runner.step()
    gateway.cancel.assert_called_once_with("BTC", trade["stop_id"])
    orders[trade["stop_id"]]["status"] = "canceled"
    gateway.market_funding.return_value = Decimal("-0.006")
    monkeypatch.setattr("copybot.autopilot.now_ms", lambda: filled_ms + 7000)
    result = runner.step()
    assert result["result"] == "Confirmed flat on BTC"
    assert Decimal(result["pnl"]["net_usdc"]) == Decimal("0.091")
    assert Decimal(runner.journal.status()["daily_net_pnl_usdc"]) == Decimal("0.091")
    assert runner.step() == "Entries paused"


def test_ownership_recovered_from_order_history_after_crash(setup):
    runner, _ = setup
    gateway = runner.gateway
    orders = {}
    enter_fixture(gateway, orders)
    original = gateway.enter.side_effect

    def send_and_timeout(trade):
        original(trade)
        raise httpx.ReadTimeout("acknowledgement lost")

    gateway.enter.side_effect = send_and_timeout
    gateway.historical_order.side_effect = lambda coin, cloid: orders.get(cloid)
    gateway.lookup.side_effect = httpx.ReadTimeout("direct order status unavailable")
    with pytest.raises(httpx.ReadTimeout):
        runner.step()
    with pytest.raises(TradingError, match="no confirmed entry"):
        runner.step()
    gateway.order.assert_not_called()
    trade = runner.journal.get("trade")
    orders[trade["entry_id"]] = {
        "status": "filled",
        "order": {
            "origSz": trade["requested_size"],
            "oid": 101,
        },
    }
    assert runner.step() == "Monitoring protected BTC position"
    gateway.order.assert_not_called()
    assert runner.journal.get("trade")["entry_oid"] == 101


def test_historical_order_requires_exact_client_id_and_market():
    feed = Mock()
    gateway = AccountGateway(feed, "owner", None, None)
    expected = {"order": {"coin": "BTC", "cloid": "expected"}, "status": "filled"}
    feed.info.return_value = [
        {"order": {"coin": "BTC", "cloid": "manual"}},
        {"order": {"coin": "ETH", "cloid": "expected"}},
        expected,
    ]
    assert gateway.historical_order("BTC", "expected") == expected
    assert gateway.historical_order("BTC", "unknown") is None


def test_emergency_exit_retries_only_after_the_previous_deadline(setup, monkeypatch):
    runner, _ = setup
    gateway = runner.gateway
    orders = {}
    enter_fixture(gateway, orders, partial="0.05")
    exits = []

    def send(coin, buy, size, limit, cloid, *, reduce_only=False, stop=None, expires_at=None):
        assert reduce_only and not buy
        if stop is not None:
            return "stop rejected"
        exits.append((cloid, expires_at))
        if len(exits) == 1:
            raise httpx.ReadTimeout("exit acknowledgement missing")

    gateway.order.side_effect = send
    with pytest.raises(httpx.ReadTimeout):
        runner.step()
    assert runner.journal.status()["incident"]
    assert "Waiting for the previous" in runner.step()
    assert len(exits) == 1
    monkeypatch.setattr("copybot.autopilot.now_ms", lambda: exits[0][1] + 5000)
    assert "Reduce-only exit requested" in runner.step()
    assert len(exits) == 2 and exits[0][0] != exits[1][0]


def test_exit_quote_can_use_a_fresh_one_sided_book(monkeypatch):
    feed = Feed(Mock())
    monkeypatch.setattr(
        feed,
        "info",
        lambda *args, **kwargs: {
            "coin": "BTC",
            "time": now_ms(),
            "levels": [[{"px": "100", "sz": "1"}], []],
        },
    )
    assert feed.exit_price("BTC", sell=True) == Decimal(100)
    with pytest.raises(ValueError, match="No executable side"):
        feed.exit_price("BTC", sell=False)


def test_broker_rejection_does_not_reuse_signal(setup):
    runner, _ = setup
    runner.gateway.enter.return_value = {"entry_rejected": "IOC did not execute"}
    assert runner.step() == "No fill on BTC"
    assert runner.step()["result"] == "No eligible entry"
    assert runner.gateway.enter.call_count == 1


def test_manual_reentry_at_same_size_and_price_blocks_orders(setup):
    runner, _ = setup
    gateway = runner.gateway
    orders = {}
    enter_fixture(gateway, orders)
    assert runner.step() == "Monitoring protected BTC position"
    size = gateway.market_state.return_value["szi"]
    gateway.market_fills.return_value.extend(
        [
            fill(102, "A", size, 100, size),
            fill(103, "B", size, 100, 0),
        ]
    )
    with pytest.raises(ExposureConflict, match="Another order opened exposure"):
        runner.step()
    gateway.order.assert_not_called()
    gateway.cancel.assert_not_called()


def test_failed_breakout_uses_a_closed_post_entry_candle(setup, monkeypatch):
    runner, _ = setup
    orders = {}
    enter_fixture(runner.gateway, orders)
    assert runner.step() == "Monitoring protected BTC position"
    trade = runner.journal.get("trade")
    tick = (trade["opened"] // BAR_MS + 1) * BAR_MS
    monkeypatch.setattr("copybot.autopilot.now_ms", lambda: tick + 1)
    runner.feed.closed_candle.return_value = (tick - 1, Decimal("98.5"))
    assert "failed breakout" in runner.step()
    assert runner.gateway.order.call_args.kwargs["reduce_only"] is True


def test_daily_net_loss_and_non_bot_exposure_prevent_fresh_entries(setup):
    runner, _ = setup
    runner.journal.put("daily_pnl_usdc", {utc_day(): "-2.01"})
    assert runner.step() == "Daily net loss limit reached"
    runner.journal.put("daily_pnl_usdc", {utc_day(): "-1.00"})
    runner.gateway.configured_exposure.return_value = ["ETH"]
    with pytest.raises(ExposureConflict, match="Unmanaged position or order on ETH"):
        runner.step()
    runner.gateway.enter.assert_not_called()
    runner.feed.scan.assert_not_called()
