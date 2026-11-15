from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from copybot.engine import PaperEngine
from copybot.models import Assessment, Limits, Quote, Settings, Trade, Trader
from copybot.store import Store

NOW = 1794708000
ALLOW = Assessment(multiplier=1, reason="Test assessment")


def config(**limits):
    return Settings(
        traders=[
            Trader(
                wallet="a",
                sources=["birdeye", "gmgn"],
                allocation="0.5",
                reference_equity_usd="100000",
            ),
            Trader(wallet="b", sources=["nansen"], allocation="0.5", reference_equity_usd="100000"),
        ],
        limits=Limits(**limits),
    )


def trade(event="buy", wallet="a", side="buy", **kwargs):
    return Trade.model_validate(
        dict(
            event_id=event,
            wallet=wallet,
            token="token",
            side=side,
            quantity="1000",
            price_usd="10",
            timestamp=NOW,
        )
        | kwargs
    )


def quote(**kwargs):
    return Quote.model_validate(
        dict(token="token", price_usd="10", liquidity_usd="1000000", timestamp=NOW) | kwargs
    )


@pytest.fixture
def engine():
    store = Store(":memory:", D(1000))
    yield PaperEngine(config(), store)
    store.close()


def test_proportional_buy_debits_cash_and_includes_costs(engine):
    assert engine.process(trade(), quote(), NOW, ALLOW) == "filled"
    assert D(engine.store.get("cash")) == 950
    qty, cost = engine.store.position("a", "token")
    assert cost == 50
    assert qty == D(50) / D("1.003") / D("10.05")
    assert engine.equity(NOW) < 1000  # Fees and adverse execution are not free.


def test_multiple_traders_share_token_but_keep_separate_positions(engine):
    engine.process(trade(), quote(), NOW, ALLOW)
    engine.process(trade("b-buy", "b"), quote(), NOW, ALLOW)
    a_before, _ = engine.store.position("a", "token")
    b_before, _ = engine.store.position("b", "token")
    sell = trade("half", side="sell", quantity="500", leader_balance_before="1000")
    assert engine.process(sell, quote(), NOW) == "filled"
    assert engine.store.position("a", "token")[0] == a_before * D("0.5")
    assert engine.store.position("b", "token")[0] == b_before


def test_ai_reduction_cannot_be_undone_by_exit_scaling(engine):
    reduce = Assessment(multiplier="0.5", reason="Reduce new exposure")
    engine.process(trade(), quote(), NOW, reduce)
    assert D(engine.store.get("cash")) == 975
    qty, cost = engine.store.position("a", "token")
    engine.process(
        trade("sell", side="sell", quantity="250", leader_balance_before="1000"), quote(), NOW
    )
    assert engine.store.position("a", "token") == (qty * D("0.75"), cost * D("0.75"))


def test_disabled_paused_and_ai_suspension_still_allow_full_exit(engine):
    engine.process(trade(), quote(), NOW, ALLOW)
    engine.cfg.traders[0].enabled = False
    engine.store.put("paused", "true")
    veto = Assessment(multiplier=0, reason="Suspend entries")
    assert (
        engine.process(trade("sell", side="sell", leader_balance_before="1000"), quote(), NOW, veto)
        == "filled"
    )
    assert engine.store.positions() == []
    assert D(engine.store.get("cash")) < 1000


def test_missing_or_inconsistent_inventory_never_guesses_sell(engine):
    engine.process(trade(), quote(), NOW, ALLOW)
    qty = engine.store.position("a", "token")[0]
    for i, before in enumerate((None, "0", "500")):
        result = engine.process(
            trade(f"sell-{i}", side="sell", leader_balance_before=before), quote(), NOW
        )
        assert "inventory required" in result
    assert engine.store.position("a", "token")[0] == qty


@pytest.mark.parametrize(
    "signal,mark,reason",
    [
        ({"timestamp": NOW - 91}, {}, "Signal is stale"),
        ({"timestamp": NOW + 1}, {}, "future timestamp"),
        ({}, {"timestamp": NOW - 31}, "Quote is stale"),
        ({}, {"timestamp": NOW + 1}, "future timestamp"),
        ({}, {"token": "other"}, "does not match"),
        ({}, {"liquidity_usd": "10"}, "Liquidity below"),
        ({}, {"price_usd": "11"}, "Price has moved"),
    ],
)
def test_entry_rejections_do_not_spend(engine, signal, mark, reason):
    assert reason in engine.process(trade(**signal), quote(**mark), NOW, ALLOW)
    assert D(engine.store.get("cash")) == 1000
    assert not engine.store.positions()


def test_unavailable_ai_rejects_entry_and_records_reason(engine):
    assert engine.process(trade(), quote(), NOW) == "No valid advisor assessment"
    assert engine.store.report()["recent_decisions"][0]["status"] == "skipped"


def test_token_limit_is_shared_across_traders(engine):
    engine.cfg.limits.max_token_fraction = D("0.075")
    engine.process(trade(), quote(), NOW, ALLOW)
    engine.process(trade("b", "b"), quote(), NOW, ALLOW)
    # Fees and slippage consume cash, but the shared limit controls marked exposure.
    exposure = sum(D(p["quantity"]) * 10 for p in engine.store.positions())
    assert exposure <= 75
    assert 924 <= D(engine.store.get("cash")) < 926
    assert len(engine.store.positions()) == 2


def test_stale_other_position_blocks_new_entry(engine):
    engine.process(trade(), quote(), NOW, ALLOW)
    assert "Fresh marks required" in engine.process(
        trade("other", token="other", timestamp=NOW + 40),
        quote(token="other", timestamp=NOW + 40),
        NOW + 40,
        ALLOW,
    )


def test_daily_loss_uses_equity_dollars_and_latches(engine):
    engine.process(trade(), quote(), NOW, ALLOW)
    engine.mark(quote(price_usd="1"))
    assert "Daily equity loss" in engine.process(
        trade("next", token="other"), quote(token="other"), NOW, ALLOW
    )
    engine.mark(quote(price_usd="10"))
    assert "Daily equity loss" in engine.process(trade("recovered"), quote(), NOW, ALLOW)


def test_stop_exit_runs_while_paused_and_prevents_immediate_reentry(engine):
    engine.process(trade(), quote(), NOW, ALLOW)
    engine.store.put("paused", "true")
    assert engine.stop_exits({"token": quote(price_usd="8")}, NOW) == ["filled"]
    assert not engine.store.positions()
    engine.store.put("paused", "false")
    assert "cooldown" in engine.process(trade("reentry"), quote(), NOW, ALLOW)


def test_failure_rolls_back_cash_positions_and_decision(engine, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("disk write failed")

    monkeypatch.setattr(engine.store, "record", fail)
    with pytest.raises(RuntimeError):
        engine.process(trade(), quote(), NOW, ALLOW)
    assert D(engine.store.get("cash")) == 1000
    assert not engine.store.positions()
    assert not engine.store.seen("buy")


def test_restart_preserves_money_and_deduplicates(tmp_path):
    path = tmp_path / "paper.sqlite"
    store = Store(path, D(1000))
    engine = PaperEngine(config(), store)
    engine.process(trade(), quote(), NOW, ALLOW)
    before = store.report()
    store.close()
    store = Store(path, D(1000))
    try:
        assert PaperEngine(config(), store).process(trade(), quote(), NOW, ALLOW) == "duplicate"
        assert store.report() == before
    finally:
        store.close()


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1"])
def test_invalid_money_is_rejected(bad):
    with pytest.raises(ValidationError):
        trade(quantity=bad)


def test_roster_deduplicates_wallet_identity_not_source():
    cfg = config()
    assert len(cfg.traders[0].sources) == 2
    with pytest.raises(ValidationError):
        Settings(traders=[cfg.traders[0], cfg.traders[0]])
    with pytest.raises(ValidationError):
        Settings(mode="live")
    with pytest.raises(ValidationError):
        Assessment(multiplier="1.01", reason="Cannot override budget")


def test_removing_trader_with_open_position_is_rejected(engine):
    engine.process(trade(), quote(), NOW, ALLOW)
    with pytest.raises(ValueError, match="Keep traders"):
        PaperEngine(Settings(), engine.store)


def test_quote_updates_cannot_rewind_risk_marks(engine):
    engine.mark(quote(timestamp=NOW + 1, price_usd="9"))
    engine.mark(quote())
    stored = engine.store.db.execute("SELECT price FROM quotes WHERE token='token'").fetchone()
    assert D(stored[0]) == 9
    assert "older than" in engine.process(trade(), quote(), NOW, ALLOW)


def test_only_one_watcher_can_own_account_and_lock_releases(tmp_path):
    path = tmp_path / "account.sqlite"
    first = Store(path, D(1000))
    second = Store(path, D(1000))
    try:
        first.acquire_watch_lock()
        with pytest.raises(ValueError, match="Another watch"):
            second.acquire_watch_lock()
        first.close()
        second.acquire_watch_lock()
    finally:
        second.close()
