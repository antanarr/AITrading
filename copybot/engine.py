from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .models import Assessment, Quote, Settings, Trade
from .store import Store

D = Decimal
ZERO = D(0)


class PaperEngine:
    def __init__(self, settings: Settings, store: Store):
        self.cfg, self.store = settings, store
        if D(store.get("initial_cash")) != settings.initial_cash_usd:
            raise ValueError(
                "Initial cash differs from this database; use its original configuration"
            )
        missing = {p["wallet"] for p in store.positions()} - {t.wallet for t in settings.traders}
        if missing:
            raise ValueError(
                "Keep traders with open positions in the roster; disable their entries instead"
            )

    def mark(self, quote: Quote):
        self.store.db.execute(
            "INSERT INTO quotes VALUES (?,?,?,?) ON CONFLICT(token) DO UPDATE SET "
            "price=excluded.price, liquidity=excluded.liquidity, timestamp=excluded.timestamp "
            "WHERE excluded.timestamp >= quotes.timestamp",
            (quote.token, str(quote.price_usd), str(quote.liquidity_usd), quote.timestamp),
        )

    def equity(self, now: int):
        value = D(self.store.get("cash"))
        for p in self.store.positions():
            q = self.store.db.execute(
                "SELECT * FROM quotes WHERE token=?", (p["token"],)
            ).fetchone()
            if q is None or not 0 <= now - q["timestamp"] <= self.cfg.limits.max_quote_age_seconds:
                raise ValueError("Fresh marks required for every open position")
            value += D(p["quantity"]) * D(q["price"])
        return value

    def process(self, trade: Trade, quote: Quote, now: int, assessment: Assessment | None = None):
        db = self.store.db
        db.execute("BEGIN IMMEDIATE")
        try:
            result = self._process(trade, quote, now, assessment)
            db.execute("COMMIT")
            return result
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def _process(self, t, q, now, assessment):
        s, limits = self.store, self.cfg.limits
        if s.seen(t.event_id):
            return "duplicate"

        def skip(reason):
            s.record(
                t,
                "skipped",
                reason,
                advisor=assessment.model_dump(mode="json") if assessment else None,
            )
            return reason

        traders = {x.wallet: x for x in self.cfg.traders}
        trader = traders.get(t.wallet)
        if trader is None:
            return skip("Wallet is not in the configured roster")
        if t.token != q.token:
            return skip("Quote token does not match signal")
        if not 0 <= now - t.timestamp <= limits.max_signal_age_seconds:
            return skip("Signal is stale or has a future timestamp")
        if not 0 <= now - q.timestamp <= limits.max_quote_age_seconds:
            return skip("Quote is stale or has a future timestamp")
        previous_quote = s.db.execute(
            "SELECT timestamp FROM quotes WHERE token=?", (q.token,)
        ).fetchone()
        if previous_quote is not None and q.timestamp < previous_quote[0]:
            return skip("Quote is older than the last recorded mark")
        self.mark(q)
        held, cost = s.position(t.wallet, t.token)
        fee_rate, slip = limits.fee_bps / 10000, limits.slippage_bps / 10000
        cash = D(s.get("cash"))
        if t.side == "sell":
            if held == 0:
                return skip("No copied position to sell")
            before = t.leader_balance_before
            if before is None or before <= 0 or t.quantity > before:
                return skip("Verified leader inventory required for proportional exit")
            # Pausing and AI never prevent reducing an existing exposure.
            fraction = t.quantity / before
            quantity = held * fraction
            price = q.price_usd * (1 - slip)
            proceeds = quantity * price
            fee = proceeds * fee_rate
            removed_cost = cost * fraction
            pnl = proceeds - fee - removed_cost
            cash += proceeds - fee
            new_qty, new_cost = held * (1 - fraction), cost * (1 - fraction)
        else:
            if not trader.enabled or trader.allocation == 0:
                return skip("Trader is disabled or has zero allocation")
            if s.get("paused", "false") == "true":
                return skip("New entries are paused")
            if now < int(s.get(f"cooldown:{t.wallet}:{t.token}", "0")):
                return skip("Entry cooldown after a stop exit")
            if q.liquidity_usd < limits.min_liquidity_usd:
                return skip("Liquidity below minimum")
            if abs(q.price_usd / t.price_usd - 1) > limits.max_price_drift:
                return skip("Price has moved beyond the entry limit")
            try:
                equity = self.equity(now)
            except ValueError as exc:
                return skip(str(exc))
            initial = D(s.get("initial_cash"))
            if initial != self.cfg.initial_cash_usd:
                return skip(
                    "Initial cash differs from this database; use its original configuration"
                )
            day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
            if s.get("risk_day") != day:
                s.put("risk_day", day)
                s.put("day_start_equity", equity)
                s.put("daily_halt", "false")
            start = D(s.get("day_start_equity"))
            if equity <= start * (1 - limits.daily_loss_fraction):
                s.put("daily_halt", "true")
            if s.get("daily_halt") == "true":
                return skip("Daily equity loss limit reached")
            if assessment is None:
                return skip("No valid advisor assessment")
            if assessment.multiplier == 0:
                return skip("Advisor suspended entries: " + assessment.reason)
            exposure_token = exposure_trader = exposure_total = ZERO
            for p in s.positions():
                mark = s.db.execute(
                    "SELECT price FROM quotes WHERE token=?", (p["token"],)
                ).fetchone()
                exposure = D(p["quantity"]) * D(mark[0])
                exposure_total += exposure
                if p["token"] == t.token:
                    exposure_token += exposure
                if p["wallet"] == t.wallet:
                    exposure_trader += exposure
            # Fixed reference capital makes the scale explicit and reproducible.
            desired = (
                initial * trader.allocation * t.quantity * t.price_usd / trader.reference_equity_usd
            )
            price = q.price_usd * (1 + slip)
            # Limits include adverse simulated execution and entry fees.
            spend = min(
                desired * assessment.multiplier,
                limits.max_trade_usd,
                initial * trader.allocation - exposure_trader,
                initial * limits.max_token_fraction - exposure_token,
                initial * limits.max_total_fraction - exposure_total,
                cash,
            )
            if spend < limits.min_trade_usd:
                return skip("Order is below minimum after allocation and exposure limits")
            notional = spend / (1 + fee_rate)
            quantity, fee = notional / price, spend - notional
            cash -= spend
            new_qty, new_cost = held + quantity, cost + spend
            pnl = ZERO
        if new_qty == 0:
            s.db.execute("DELETE FROM positions WHERE wallet=? AND token=?", (t.wallet, t.token))
        else:
            s.db.execute(
                "INSERT OR REPLACE INTO positions VALUES (?,?,?,?)",
                (t.wallet, t.token, str(new_qty), str(new_cost)),
            )
        s.put("cash", cash)
        s.record(
            t,
            "filled",
            "Simulated fill",
            quantity,
            price,
            fee,
            pnl,
            assessment.model_dump(mode="json") if assessment else None,
        )
        return "filled"

    def stop_exits(self, quotes: dict[str, Quote], now: int):
        results = []
        for p in self.store.positions():
            q = quotes.get(p["token"])
            if q is None:
                continue
            qty, cost = D(p["quantity"]), D(p["cost"])
            if q.price_usd * qty <= cost * (1 - self.cfg.limits.stop_loss_fraction):
                t = Trade(
                    event_id=f"stop:{p['wallet']}:{p['token']}:{now}",
                    wallet=p["wallet"],
                    token=p["token"],
                    side="sell",
                    quantity=qty,
                    price_usd=q.price_usd,
                    timestamp=now,
                    leader_balance_before=qty,
                )
                result = self.process(t, q, now)
                if result == "filled":
                    self.store.put(f"cooldown:{p['wallet']}:{p['token']}", now + 3600)
                results.append(result)
        return results

    def advisor_context(self, wallet: str, now: int):
        rows = self.store.db.execute(
            "SELECT pnl FROM decisions WHERE wallet=? AND status='filled' AND side='sell' ORDER BY rowid DESC LIMIT 20",
            (wallet,),
        ).fetchall()
        pnl = [D(r[0]) for r in rows]
        return {
            "wallet": wallet,
            "closed_trade_count": len(pnl),
            "realized_pnl_usd": str(sum(pnl, ZERO)),
            "recent_loss_count": sum(x < 0 for x in pnl),
            "as_of": now,
            "positions": [p for p in self.store.positions() if p["wallet"] == wallet],
        }
