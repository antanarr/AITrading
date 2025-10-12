from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional

import ccxt

from .config import Config
from .risk import RiskManager
from .types import OrderResult, Position, TradeAction

LOGGER = logging.getLogger(__name__)


@dataclass
class PaperAccount:
    balance: float = 10_000.0
    positions: Dict[str, Position] = field(default_factory=dict)

    def available_balance(self) -> float:
        return self.balance

    def open_position(
        self,
        symbol: str,
        side: TradeAction,
        notional: float,
        price: float,
        stop_pct: float,
        take_pct: float,
    ) -> OrderResult:
        size = notional / price
        self.positions[symbol] = Position(
            symbol=symbol,
            side=side,
            size=size,
            entry_price=price,
            stop_pct=stop_pct,
            take_pct=take_pct,
        )
        LOGGER.info(
            "Paper trade: %s %s size %.4f @ %.2f", side.value, symbol, size, price
        )
        return OrderResult(symbol=symbol, side=side, size=size, price=price, status="filled")

    def close_position(self, symbol: str, price: float) -> Optional[OrderResult]:
        position = self.positions.pop(symbol, None)
        if not position:
            return None
        pnl = (price - position.entry_price) * position.size
        if position.side == TradeAction.SELL:
            pnl = -pnl
        self.balance += pnl
        LOGGER.info("Paper position closed: %s PnL %.2f", symbol, pnl)
        return OrderResult(
            symbol=symbol,
            side=TradeAction.SELL if position.side == TradeAction.BUY else TradeAction.BUY,
            size=position.size,
            price=price,
            status="closed",
        )


class ExecutionClient:
    def __init__(self, cfg: Config, exchange: ccxt.Exchange, paper: bool = True) -> None:
        self.cfg = cfg
        self.exchange = exchange
        self.paper = paper
        self.paper_account = PaperAccount() if paper else None
        self.risk_manager = RiskManager(cfg.risk)

    def position_size(self, symbol: str, price: float) -> float:
        risk_per_trade = self.cfg.risk.risk_per_trade
        balance = self.paper_account.available_balance() if self.paper else self._balance()
        notional = balance * risk_per_trade
        LOGGER.debug("Risking %.2f on %s (balance %.2f)", notional, symbol, balance)
        return notional

    def execute(
        self,
        symbol: str,
        decision: TradeAction,
        price: float,
        stop_pct: float,
        take_pct: float,
    ) -> Optional[OrderResult]:
        self.risk_manager.reset_if_new_day()
        if self.risk_manager.check_kill_switch():
            LOGGER.error("Kill switch active. No trades will be executed.")
            return None
        if decision == TradeAction.NONE:
            LOGGER.info("Decision is HOLD. No trade executed.")
            return None
        notional = self.position_size(symbol, price if price else 1.0)
        if not self.risk_manager.allow_trade(notional):
            return None
        self.risk_manager.reserve_risk(notional)
        if self.paper:
            trade_price = price if price and price > 0 else 1.0
            return self.paper_account.open_position(
                symbol, decision, notional, trade_price, stop_pct, take_pct
            )
        side = "buy" if decision == TradeAction.BUY else "sell"
        amount = notional / price
        LOGGER.info("Placing order: %s %s amount %.6f", side, symbol, amount)
        order = self.exchange.create_market_order(symbol, side, amount)
        return OrderResult(
            symbol=symbol,
            side=decision,
            size=amount,
            price=price,
            status=order.get("status", "unknown"),
            order_id=order.get("id"),
        )

    def _balance(self) -> float:
        balance = self.exchange.fetch_balance()
        quote = self.cfg.symbols[0].split("/")[1]
        total = balance["total"].get(quote, 0.0)
        return float(total)
