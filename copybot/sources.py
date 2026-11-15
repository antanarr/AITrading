from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx

from .models import Candidate, Quote, Trade

D = Decimal
SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_ASSETS = {SOL, USDC, USDT}


class SourceError(RuntimeError):
    pass


class UnsupportedTrade(SourceError):
    """A provider trade cannot be represented as a verified wallet swap."""


def request_json(client: httpx.Client, method: str, url: str, **kwargs):
    """Do not log authenticated URLs, headers, or provider response bodies."""
    try:
        r = client.request(method, url, **kwargs)
    except httpx.RequestError as exc:
        raise SourceError(f"Data request failed ({type(exc).__name__})") from None
    if r.status_code != 200:
        retry = " Rate limited; wait before retrying." if r.status_code == 429 else ""
        raise SourceError(f"Data provider returned HTTP {r.status_code}.{retry}")
    try:
        return r.json(parse_float=Decimal)
    except ValueError:
        raise SourceError("Provider returned invalid JSON") from None


def merge_candidates(candidates: list[Candidate]):
    merged: dict[tuple[str, str], Candidate] = {}
    for c in candidates:
        key = (c.chain, c.wallet)
        if key not in merged:
            merged[key] = c.model_copy(deep=True)
        else:
            merged[key].sources = sorted(set(merged[key].sources + c.sources))
            merged[key].evidence.update(c.evidence)
    return sorted(merged.values(), key=lambda c: (c.chain, c.wallet))


def imported_candidates(path: Path):
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ValueError("Candidate import must be a JSON array")
    return [Candidate.model_validate(x) for x in raw]


class Birdeye:
    base = "https://public-api.birdeye.so"

    def __init__(self, client: httpx.Client, api_key: str):
        if not api_key:
            raise ValueError("BIRDEYE_API_KEY is required")
        self.client = client
        self.headers = {"X-API-KEY": api_key, "x-chain": "solana"}

    def get(self, path, params):
        payload = request_json(
            self.client, "GET", self.base + path, headers=self.headers, params=params
        )
        if payload.get("success") is not True or not isinstance(payload.get("data"), dict):
            raise SourceError("Birdeye reported failure or an unexpected response")
        return payload["data"]

    def discover(self, window="30d", limit=20):
        data = self.get(
            "/trader/gainers-losers",
            {
                "type": window,
                "sort_by": "realized_pnl",
                "limit": limit,
                "offset": 0,
            },
        )
        return [
            Candidate(
                wallet=r["address"],
                sources=["birdeye"],
                evidence={
                    "birdeye": {
                        "window": window,
                        "realized_pnl_usd": str(r["realized_pnl"]),
                        "unrealized_pnl_usd": str(r["unrealized_pnl"]),
                        "trades": r["trade_count"],
                    },
                },
            )
            for r in data["items"]
            if r["network"] == "solana"
        ]

    def trades(self, wallet: str, after: int, before: int):
        rows = []
        for offset in range(0, 10000, 100):
            data = self.get(
                "/trader/txs/seek_by_time",
                {
                    "address": wallet,
                    "after_time": max(1, after - 120),
                    "before_time": before,
                    "tx_type": "swap",
                    "limit": 100,
                    "offset": offset,
                    "ui_amount_mode": "raw",
                },
            )
            rows.extend(data["items"])
            more = data.get("has_next", data.get("hasNext"))
            if more is None:
                raise SourceError("Birdeye omitted pagination status; refusing an incomplete batch")
            if not more:
                return sorted(
                    rows,
                    key=lambda r: (
                        r["block_unix_time"],
                        r.get("block_number", 0),
                        r["tx_hash"],
                        r.get("ins_index") or 0,
                    ),
                )
            if not data["items"]:
                raise SourceError("Birdeye returned an empty page with more results")
        raise SourceError("Trade history exceeds pagination limit; cursor was not advanced")

    def quote(self, token: str):
        r = self.get("/defi/token_overview", {"address": token})
        return Quote(
            token=token,
            price_usd=r["price"],
            liquidity_usd=r["liquidity"],
            timestamp=r["lastTradeUnixTime"],
        )


class Nansen:
    """Smart Money activity discovery; activity is not a profitability ranking."""

    def __init__(self, client: httpx.Client, api_key: str):
        if not api_key:
            raise ValueError("NANSEN_API_KEY is required")
        self.client, self.api_key = client, api_key

    def discover(self, limit=20):
        payload = request_json(
            self.client,
            "POST",
            "https://api.nansen.ai/api/v1/smart-money/dex-trades",
            headers={"apikey": self.api_key},
            json={
                "chains": ["solana"],
                "pagination": {"page": 1, "per_page": limit},
                "order_by": [{"field": "block_timestamp", "direction": "DESC"}],
            },
        )
        return merge_candidates(
            [
                Candidate(
                    wallet=r["trader_address"],
                    sources=["nansen"],
                    evidence={
                        "nansen": {
                            "basis": "recent Smart Money activity, not a P&L ranking",
                            "last_observed_trade": r["block_timestamp"],
                        },
                    },
                )
                for r in payload["data"]
                if r["chain"] == "solana"
            ]
        )


class SolanaInventory:
    """Verify swap direction and sell fractions against finalized transaction balances."""

    def __init__(self, client: httpx.Client, rpc_url: str):
        if not rpc_url.startswith("https://"):
            raise ValueError("SOLANA_RPC_URL must use HTTPS")
        self.client, self.url = client, rpc_url

    def transaction(self, signature: str):
        payload = request_json(
            self.client,
            "POST",
            self.url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [
                    signature,
                    {
                        "encoding": "jsonParsed",
                        "commitment": "finalized",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            },
        )
        tx = payload.get("result")
        if tx is None or tx.get("meta") is None:
            raise SourceError("Finalized transaction is not available; retry this batch")
        if tx["meta"]["err"] is not None:
            raise SourceError("Failed on-chain transaction cannot be copied")
        return tx

    def block_order(self, slot: int):
        payload = request_json(
            self.client,
            "POST",
            self.url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getBlock",
                "params": [
                    slot,
                    {
                        "commitment": "finalized",
                        "transactionDetails": "signatures",
                        "rewards": False,
                    },
                ],
            },
        )
        result = payload.get("result")
        if result is None or "signatures" not in result:
            raise SourceError("Finalized block ordering unavailable; retry this batch")
        return {sig: index for index, sig in enumerate(result["signatures"])}

    @staticmethod
    def normalize(row: dict, wallet: str, tx: dict):
        if row["owner"] != wallet:
            raise UnsupportedTrade("Trade owner does not match the followed wallet")
        meta = tx["meta"]
        result = []
        for leg in (row["base"], row["quote"]):
            token = leg["address"]
            if token in QUOTE_ASSETS:
                continue
            side = {"to": "buy", "from": "sell"}.get(leg.get("type_swap"))
            if side is None:
                raise UnsupportedTrade("Unknown swap direction")

            def balance(key, mint=token):
                total = D(0)
                for b in meta[key]:
                    if b.get("owner") == wallet and b["mint"] == mint:
                        a = b["uiTokenAmount"]
                        total += D(a["amount"]) / (D(10) ** a["decimals"])
                return total

            pre, post = balance("preTokenBalances"), balance("postTokenBalances")
            delta = post - pre
            quantity = D(str(leg["ui_amount"]))
            expected = quantity if side == "buy" else -quantity
            # Multi-route/internal legs are not independent wallet trades.
            if abs(delta - expected) > max(D("0.000000001"), quantity * D("0.000001")):
                raise UnsupportedTrade("Swap leg differs from net wallet change; not copied")
            if tx["blockTime"] != row["block_unix_time"]:
                raise UnsupportedTrade("Transaction timestamp disagrees with signal")
            result.append(
                Trade(
                    event_id=f"solana:{row['tx_hash']}:{wallet}:{token}:{side}",
                    wallet=wallet,
                    token=token,
                    side=side,
                    quantity=quantity,
                    price_usd=leg["price"],
                    timestamp=row["block_unix_time"],
                    leader_balance_before=pre,
                )
            )
        return result
