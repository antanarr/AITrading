from decimal import Decimal as D

import pytest
from test_engine import ALLOW, NOW, config, quote, trade

from copybot.engine import PaperEngine
from copybot.models import Assessment
from copybot.runner import Runner
from copybot.sources import SourceError
from copybot.store import Store


class Feed:
    def __init__(self, rows=None, fail=False):
        self.rows = rows or []
        self.fail = fail

    def trades(self, wallet, after, before):
        if self.fail:
            raise SourceError("429")
        return self.rows if wallet == "a" else []

    def quote(self, token):
        return quote(token=token)


class Inventory:
    def transaction(self, signature):
        return {"slot": 1}

    def block_order(self, slot):
        return {"buy": 0, "sell": 1}

    def normalize(self, row, wallet, tx):
        if row["tx_hash"] == "sell":
            return [trade("sell", side="sell", leader_balance_before="1000")]
        return [trade()]


class FixedAdvisor:
    def __init__(self, fail=False):
        self.fail = fail

    def assess(self, context, now):
        if self.fail:
            raise SourceError("Advisor unavailable")
        return Assessment(multiplier=1, reason="Test only")


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr("copybot.runner.time.time", lambda: NOW)
    store = Store(":memory:", D(1000))
    result = PaperEngine(config(), store)
    for w in ("a", "b"):
        store.put("cursor:" + w, NOW - 10)
        store.put("start:" + w, NOW - 20)
    yield result
    store.close()


def test_orders_same_second_trades_by_chain_order_not_feed_order(engine):
    # API page can list sell before buy even though the chain executed buy first.
    rows = [{"tx_hash": sig, "block_unix_time": NOW} for sig in ("sell", "buy")]
    results = Runner(engine, Feed(rows), Inventory(), FixedAdvisor()).cycle()
    assert [r["event_id"] for r in results] == ["buy", "sell"]
    assert [r["result"] for r in results] == ["filled", "filled"]
    assert engine.store.positions() == []


def test_advisor_outage_does_not_block_copied_exit(engine):
    engine.process(trade(), quote(), NOW, ALLOW)
    rows = [{"tx_hash": "sell", "block_unix_time": NOW}]
    result = Runner(engine, Feed(rows), Inventory(), FixedAdvisor(fail=True)).cycle()
    assert result[0]["result"] == "filled"
    assert not engine.store.positions()
    assert "unavailable" in engine.store.get("last_advisor_error")


def test_advisor_outage_rejects_new_entry(engine):
    rows = [{"tx_hash": "buy", "block_unix_time": NOW}]
    result = Runner(engine, Feed(rows), Inventory(), FixedAdvisor(fail=True)).cycle()
    assert result[0]["result"] == "No valid advisor assessment"
    assert not engine.store.positions()


def test_incomplete_source_batch_does_not_advance_cursors(engine):
    runner = Runner(engine, Feed(fail=True), Inventory(), FixedAdvisor())
    with pytest.raises(SourceError):
        runner.cycle()
    assert engine.store.get("cursor:a") == str(NOW - 10)
    assert not engine.store.positions()


def test_new_watch_starts_at_now_without_copying_old_winners(engine):
    engine.store.db.execute("DELETE FROM meta WHERE key LIKE 'cursor:%'")
    runner = Runner(engine, Feed(fail=True), Inventory(), FixedAdvisor())
    assert runner.cycle() == []
    assert engine.store.get("cursor:a") == str(NOW)


def test_stop_exit_precedes_signal_feed_outage(engine):
    engine.process(trade(), quote(), NOW, ALLOW)

    class CrashedFeed(Feed):
        def quote(self, token):
            return quote(price_usd="8")

    with pytest.raises(SourceError):
        Runner(engine, CrashedFeed(fail=True), Inventory(), FixedAdvisor()).cycle()
    assert not engine.store.positions()


def test_disabled_trader_still_gets_exit_monitoring(engine):
    engine.process(trade(), quote(), NOW, ALLOW)
    engine.cfg.traders[0].enabled = False
    rows = [{"tx_hash": "sell", "block_unix_time": NOW}]
    Runner(engine, Feed(rows), Inventory(), FixedAdvisor()).cycle()
    assert not engine.store.positions()
