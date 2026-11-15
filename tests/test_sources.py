import json
from decimal import Decimal as D

import httpx
import pytest

from copybot.models import Candidate
from copybot.sources import (
    Birdeye,
    Nansen,
    SolanaInventory,
    SourceError,
    UnsupportedTrade,
    merge_candidates,
    request_json,
)


def test_sources_merge_without_double_counting_or_case_folding_solana_wallets():
    result = merge_candidates(
        [
            Candidate(wallet="AbC", sources=["birdeye"], evidence={"birdeye": {"pnl": 100}}),
            Candidate(wallet="AbC", sources=["gmgn"], evidence={"gmgn": {"pnl": 90}}),
            Candidate(wallet="abc", sources=["nansen"]),
        ]
    )
    assert len(result) == 2
    assert result[0].sources == ["birdeye", "gmgn"]
    assert result[0].evidence["birdeye"]["pnl"] == 100


def test_birdeye_requests_documented_rank_window_and_retains_losses():
    def handler(req):
        assert req.headers["X-API-KEY"] == "test-key"
        assert req.headers["x-chain"] == "solana"
        assert req.url.params["type"] == "30d"
        assert req.url.params["sort_by"] == "realized_pnl"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": [
                        {
                            "address": "wallet",
                            "network": "solana",
                            "realized_pnl": 100,
                            "unrealized_pnl": -200,
                            "trade_count": 50,
                        }
                    ]
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        result = Birdeye(c, "test-key").discover()
    assert result[0].evidence["birdeye"]["unrealized_pnl_usd"] == "-200"


def test_nansen_is_activity_discovery_and_uses_api_key_without_payments():
    def handler(req):
        assert req.headers["apikey"] == "test-key"
        assert "Payment-Signature" not in req.headers
        assert json.loads(req.content)["chains"] == ["solana"]
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "chain": "solana",
                        "trader_address": "w",
                        "block_timestamp": "2026-11-15T00:00:00Z",
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        result = Nansen(c, "test-key").discover()
    assert "not a P&L ranking" in result[0].evidence["nansen"]["basis"]


def test_pagination_fetches_all_pages_before_returning():
    offsets = []

    def handler(req):
        offset = int(req.url.params["offset"])
        offsets.append(offset)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": [{"block_unix_time": 100 + offset, "tx_hash": str(offset)}],
                    "hasNext": offset == 0,
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        rows = Birdeye(c, "key").trades("wallet", 100, 300)
    assert len(rows) == 2
    assert offsets == [0, 100]


def test_rate_limit_is_not_an_empty_success_and_does_not_leak_secrets():
    with httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(429, text="sensitive"))
    ) as c:
        with pytest.raises(SourceError) as err:
            request_json(c, "GET", "https://rpc.example/?api-key=secret")
    assert "429" in str(err.value)
    assert "secret" not in str(err.value)
    assert "sensitive" not in str(err.value)


def tx_balance(amount, wallet="leader"):
    return {
        "owner": wallet,
        "mint": "token",
        "uiTokenAmount": {"amount": str(amount), "decimals": 2},
    }


def test_normalizes_sell_fraction_from_authoritative_pre_transaction_inventory():
    row = {
        "owner": "leader",
        "tx_hash": "sig",
        "block_unix_time": 100,
        "base": {"address": "token", "type_swap": "from", "ui_amount": "25", "price": "4"},
        "quote": {"address": "So11111111111111111111111111111111111111112"},
    }
    tx = {
        "blockTime": 100,
        "meta": {"preTokenBalances": [tx_balance(10000)], "postTokenBalances": [tx_balance(7500)]},
    }
    signal = SolanaInventory.normalize(row, "leader", tx)[0]
    assert signal.side == "sell"
    assert signal.leader_balance_before == 100
    assert signal.quantity == 25
    row["base"]["ui_amount"] = "10"
    with pytest.raises(UnsupportedTrade, match="net wallet change"):
        SolanaInventory.normalize(row, "leader", tx)


def test_multiple_token_accounts_are_summed_without_float_rounding():
    row = {
        "owner": "leader",
        "tx_hash": "sig",
        "block_unix_time": 100,
        "base": {"address": "token", "type_swap": "to", "ui_amount": "0.01", "price": "4"},
        "quote": {"address": "So11111111111111111111111111111111111111112"},
    }
    tx = {
        "blockTime": 100,
        "meta": {
            "preTokenBalances": [tx_balance(100), tx_balance(200)],
            "postTokenBalances": [tx_balance(101), tx_balance(200)],
        },
    }
    assert SolanaInventory.normalize(row, "leader", tx)[0].quantity == D("0.01")


def test_rpc_requires_finalized_success_and_block_order():
    def handler(req):
        body = json.loads(req.content)
        assert body["params"][1]["commitment"] == "finalized"
        if body["method"] == "getBlock":
            return httpx.Response(200, json={"result": {"signatures": ["buy", "sell"]}})
        return httpx.Response(200, json={"result": None})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        rpc = SolanaInventory(c, "https://rpc.example")
        assert rpc.block_order(10) == {"buy": 0, "sell": 1}
        with pytest.raises(SourceError, match="not available"):
            rpc.transaction("sig")
