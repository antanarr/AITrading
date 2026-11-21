import sqlite3
from concurrent.futures import Future
from decimal import Decimal

import pytest

from copybot import forward_paper as paper
from copybot.forward_market import BookLevel, FundingEvent, L2Book, MarketContext
from copybot.forward_signals import ForwardDecision, ForwardSnapshot, MarketMeta
from copybot.research_models import HOUR_MS, OrderPlan

D = Decimal
MIDNIGHT = 1000 * paper.DAY_MS


def quote(stamp, *, bid="99.99", ask="100", quantity="100"):
    return L2Book(
        "SOL",
        stamp - 10,
        stamp,
        (BookLevel(D(bid), D(quantity)),),
        (BookLevel(D(ask), D(quantity)),),
    )


def context(stamp, mark="100"):
    return MarketContext("SOL", 3, D(mark), D(100), stamp - 10, stamp, 20)


def signal():
    plan = OrderPlan("SOL", 1, 100.4, 1, 2, None, 240, 0, "ioc", signal_strength=0.7)
    return ForwardDecision(MarketMeta("SOL", 3, 20, True), 1, close=D(100), plan=plan)


def test_both_lanes_share_the_observed_depth_and_exit_independently(tmp_path, monkeypatch):
    stamp = MIDNIGHT + 20_000
    journal_path = tmp_path / "paper.sqlite3"
    journal = paper.PaperJournal(journal_path, MIDNIGHT - HOUR_MS)
    active_quote = quote(stamp + 20)
    current = context(stamp)
    reads = []

    class Market:
        def contexts(self):
            return {"SOL": current}

        def book(self, coin):
            reads.append(coin)
            return active_quote

    class Pool:
        def submit(self, *_args, **_kwargs):
            future = Future()
            future.set_result(
                ForwardSnapshot(
                    MIDNIGHT // HOUR_MS,
                    stamp,
                    stamp,
                    ("SOL",),
                    {"SOL": signal()},
                    True,
                    {},
                    {},
                    "pre_boundary_snapshot",
                )
            )
            return future

    monkeypatch.setattr(paper, "now_ms", lambda: stamp)
    worker = paper.PaperWorker(journal, Market(), type("Loader", (), {"load": None})(), Pool())
    worker.tick()
    low, high = journal.books
    assert reads == ["SOL"]
    assert journal.next_boundary == MIDNIGHT + paper.DAY_MS
    assert low.positions["SOL"].quantity * 3 == high.positions["SOL"].quantity
    assert low.net * 3 == high.net
    assert low.positions["SOL"].collateral(1) == high.positions["SOL"].collateral(3)
    assert journal.db.execute(
        "SELECT count(*) FROM paper_events WHERE kind='entry'"
    ).fetchone() == (2,)

    stamp += 20_000
    current = context(stamp, "97.5")
    active_quote = quote(stamp + 20, bid="97.4", ask="97.6", quantity="3")
    worker.tick()
    assert reads == ["SOL", "SOL"]
    assert not low.positions
    remaining = high.positions["SOL"].quantity
    assert remaining > 0
    worker.tick()
    assert high.positions["SOL"].quantity == remaining
    journal.close()

    reopened = paper.PaperJournal(journal_path, stamp)
    assert reopened.books[1].positions["SOL"].quantity == remaining
    assert reopened.next_boundary == MIDNIGHT + paper.DAY_MS
    reopened.close()


def test_funding_uses_the_closest_observed_oracle_and_only_applies_once():
    entered = MIDNIGHT - 10_000
    portfolio = paper.Portfolio(3)
    order = signal()
    events = []
    paper.entry(portfolio, order.plan, order, context(entered), quote(entered + 20), events)
    position = portfolio.positions["SOL"]
    observation = FundingEvent("SOL", MIDNIGHT + 1000, D("0.0005"), None)
    after = context(MIDNIGHT + 30_000)
    assert paper.settle(portfolio, position, after, (observation,), events)
    assert paper.settle(portfolio, position, after, (observation,), events)
    assert [kind for kind, _ in events] == ["entry", "funding"]
    assert position.funding_open == -position.quantity * D("0.05")
    assert portfolio.net == portfolio.positions["SOL"].funding_open - position.entry_fee
    assert events[-1][1]["oracle_age_ms"] == 11_000


def test_existing_unrelated_sqlite_is_rejected_without_touching_its_tables(tmp_path):
    path = tmp_path / "unrelated.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE operations (id INTEGER)")
    with pytest.raises(ValueError, match="independent paper journal"):
        paper.PaperJournal(path, MIDNIGHT)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("operations",),
        ]
