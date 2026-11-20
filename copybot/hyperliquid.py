"""Hyperliquid account reads and a narrowly scoped signed order transport."""

import re
from decimal import Decimal
from pathlib import Path

from .perps import API, dex_for, now_ms, number

LOCAL = Path.home() / "Library/Application Support/Codex Hyperliquid"
KEYCHAIN_SERVICE = "com.antanarr.codex.hyperliquid"


class TradingError(RuntimeError):
    pass


def load_signer(owner, key_file):
    from eth_account import Account

    if key_file is None:
        from keyring.backends.macOS import Keyring

        bound_owner = (LOCAL / "owner.txt").read_text().strip()
        if owner and owner.lower() != bound_owner.lower():
            raise TradingError(
                "Configured account differs from the MetaMask account bound on this Mac"
            )
        owner = bound_owner
        name = "codex-local"
        keyring = Keyring()
        keyring.keychain = None
        key = keyring.get_password(KEYCHAIN_SERVICE, name)
    else:
        if key_file.stat().st_mode & 0o077:
            raise TradingError("Agent key file must be readable only by the worker user")
        name = None
        key = key_file.read_text().strip()
    if not owner or not re.fullmatch(r"0x[0-9a-fA-F]{40}", owner):
        raise TradingError("The trading account must be a public Hyperliquid address")
    try:
        signer = Account.from_key(key)
    except (TypeError, ValueError) as error:
        raise TradingError("No usable API wallet key was found") from error
    if signer.address.lower() == owner.lower():
        raise TradingError("Use a Hyperliquid API wallet key for this account")
    return owner, signer, name


class AccountGateway:
    def __init__(self, feed, owner, signer, name):
        self.feed = feed
        self.owner = owner
        self.signer = signer
        self.name = name
        self.approval_expires = 0
        self.checked_at = 0
        self.exchange = {}

    def verify(self, force=False):
        now = now_ms()
        if not force and now - self.checked_at < 60_000 and self.approval_expires > now + 15_000:
            return self.approval_expires
        agent = self.signer.address.lower()
        role = self.feed.info("userRole", user=self.signer.address)
        if role["role"] != "agent" or role["data"]["user"].lower() != self.owner.lower():
            raise TradingError("API wallet is not authorized for the chosen Hyperliquid account")
        for approval in self.feed.info("extraAgents", user=self.owner):
            if approval["address"].lower() == agent and (
                not self.name or approval["name"] == self.name
            ):
                if approval["validUntil"] > now + 15_000:
                    self.approval_expires = approval["validUntil"]
                    self.checked_at = now
                    return self.approval_expires
        raise TradingError("No current named API wallet approval found for the chosen account")

    def market_state(self, coin):
        state = self.feed.info("clearinghouseState", user=self.owner, dex=dex_for(coin))
        return next(
            (row["position"] for row in state["assetPositions"] if row["position"]["coin"] == coin),
            None,
        )

    def flat_market(self, coin):
        position = self.market_state(coin)
        orders = self.feed.info("frontendOpenOrders", user=self.owner, dex=dex_for(coin))
        return not (position and number(position["szi"])) and not any(
            order["coin"] == coin for order in orders
        )

    def has_market_fills(self, coin, since):
        rows = self.feed.info("userFillsByTime", user=self.owner, startTime=since)
        if len(rows) >= 2000:
            raise TradingError("Fill history is incomplete for order reconciliation")
        return any(row["coin"] == coin for row in rows)

    def lookup(self, coin, cloid):
        row = self.feed.info("orderStatus", user=self.owner, oid=cloid)
        if row["status"] == "unknownOid":
            return None
        if row["status"] != "order" or row["order"]["order"]["coin"] != coin:
            raise TradingError("Order lookup returned different details than expected")
        return row["order"]

    def historical_order(self, coin, cloid):
        rows = self.feed.info("historicalOrders", user=self.owner)
        return next(
            (
                row
                for row in rows
                if row["order"]["coin"] == coin and row["order"].get("cloid") == cloid
            ),
            None,
        )

    def sdk(self, coin, expires_at=None):
        from hyperliquid.exchange import Exchange

        self.verify()
        dex = dex_for(coin)
        if dex not in self.exchange:
            self.exchange[dex] = Exchange(
                self.signer,
                API,
                account_address=self.owner,
                perp_dexs=[dex],
                timeout=6,
            )
        client = self.exchange[dex]
        deadline = min(expires_at or now_ms() + 10_000, self.approval_expires)
        if deadline <= now_ms():
            raise TradingError("Signed action deadline passed before submission")
        client.set_expires_after(deadline)
        return client

    def prepare_margin(self, coin, buy, required_margin):
        def read():
            row = self.feed.info("activeAssetData", user=self.owner, coin=coin)
            if row["coin"] != coin or row["user"].lower() != self.owner.lower():
                raise TradingError("Leverage lookup returned the wrong account or market")
            leverage = row["leverage"]
            ready = leverage["type"] == "isolated" and leverage["value"] == 1
            return ready, number(row["availableToTrade"][0 if buy else 1])

        ready, available = read()
        cushion = Decimal("1.005")  # Leave margin for the entry fee and small price changes.
        if available < required_margin * cushion:
            raise TradingError(f"Insufficient collateral for a 1x order on {coin}")
        sdk = self.sdk(coin)
        if not ready:
            result = sdk.update_leverage(1, coin, is_cross=False)
            ready, available = read()
            if result.get("status") != "ok" or not ready:
                raise TradingError(f"Could not verify 1x isolated margin on {coin}")
            if available < required_margin * cushion:
                raise TradingError(f"Insufficient isolated collateral for {coin}")

    def order(
        self, coin, buy, size, price, cloid, *, reduce_only=False, stop=None, expires_at=None
    ):
        from hyperliquid.utils.types import Cloid

        kind = (
            {"limit": {"tif": "Ioc"}}
            if stop is None
            else {"trigger": {"isMarket": True, "triggerPx": float(stop), "tpsl": "sl"}}
        )
        result = self.sdk(coin, expires_at).order(
            coin,
            buy,
            float(size),
            float(price),
            kind,
            reduce_only=reduce_only,
            cloid=Cloid.from_str(cloid),
        )
        if result.get("status") == "err" and isinstance(result.get("response"), str):
            return result["response"]
        # A per-order rejection is explicit. An unexpected response stays pending for lookup.
        statuses = result.get("response") if result.get("status") == "ok" else None
        if isinstance(statuses, dict):
            statuses = statuses.get("data", {}).get("statuses", [])
            if len(statuses) == 1 and isinstance(statuses[0], dict):
                return statuses[0].get("error")
        return None

    def cancel(self, coin, cloid):
        from hyperliquid.utils.types import Cloid

        self.sdk(coin).cancel_by_cloid(coin, Cloid.from_str(cloid))
