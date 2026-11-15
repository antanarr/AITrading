from __future__ import annotations

import time

from .advisor import Advisor
from .engine import PaperEngine
from .sources import Birdeye, SolanaInventory, SourceError, UnsupportedTrade


class Runner:
    def __init__(
        self, engine: PaperEngine, birdeye: Birdeye, inventory: SolanaInventory, advisor: Advisor
    ):
        self.engine, self.birdeye = engine, birdeye
        self.inventory, self.advisor = inventory, advisor

    def cycle(self):
        s = self.engine.store
        cutoff = int(time.time())
        # Protective exits run even if signal ingestion or the advisor later fails.
        quotes = {}
        for token in sorted({p["token"] for p in s.positions()}):
            quotes[token] = self.birdeye.quote(token)
            self.engine.mark(quotes[token])
        self.engine.stop_exits(quotes, int(time.time()))
        # Disabled traders with copied positions still need their exits monitored.
        held_wallets = {p["wallet"] for p in s.positions()}
        traders = [t for t in self.engine.cfg.traders if t.enabled or t.wallet in held_wallets]
        prepared = []
        transactions, blocks = {}, {}
        for trader in traders:
            key = "cursor:" + trader.wallet
            cursor = s.get(key)
            if cursor is None:
                s.put(key, cutoff)
                s.put("start:" + trader.wallet, cutoff)
                continue
            rows = self.birdeye.trades(trader.wallet, int(cursor), cutoff)
            for row in rows:
                if row["block_unix_time"] <= int(s.get("start:" + trader.wallet)):
                    continue
                signature = row["tx_hash"]
                if s.get(f"unsupported:{trader.wallet}:{signature}"):
                    continue
                if signature not in transactions:
                    transactions[signature] = self.inventory.transaction(signature)
                tx = transactions[signature]
                try:
                    signals = self.inventory.normalize(row, trader.wallet, tx)
                except UnsupportedTrade as exc:
                    s.put(f"unsupported:{trader.wallet}:{signature}", str(exc))
                    s.put("paused", "true")
                    continue
                slot = tx["slot"]
                if slot not in blocks:
                    blocks[slot] = self.inventory.block_order(slot)
                if signature not in blocks[slot]:
                    raise SourceError("Transaction missing from finalized block")
                for signal in signals:
                    if not s.seen(signal.event_id):
                        prepared.append((slot, blocks[slot][signature], signal))
        # Get fresh marks for the complete portfolio before sizing any new entry.
        tokens = {p["token"] for p in s.positions()} | {v[2].token for v in prepared}
        for token in sorted(tokens):
            quotes[token] = self.birdeye.quote(token)
        for q in quotes.values():
            self.engine.mark(q)
        self.engine.stop_exits(quotes, int(time.time()))
        assessments = {}
        for trader in traders:
            now = int(time.time())
            try:
                assessments[trader.wallet] = self.advisor.assess(
                    self.engine.advisor_context(trader.wallet, now), now
                )
            except SourceError as exc:
                s.put("last_advisor_error", str(exc))
        results = []
        # On-chain transaction order, with sells before buys for token-to-token swaps.
        for _, _, signal in sorted(prepared, key=lambda v: (v[0], v[1], v[2].side == "buy")):
            now = int(time.time())
            results.append(
                {
                    "event_id": signal.event_id,
                    "result": self.engine.process(
                        signal, quotes[signal.token], now, assessments.get(signal.wallet)
                    ),
                }
            )
        for trader in traders:
            s.put("cursor:" + trader.wallet, cutoff)
        s.put("last_successful_cycle", int(time.time()))
        return results
