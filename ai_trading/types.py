from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class TradeAction(str, Enum):
    NONE = "hold"
    BUY = "buy"
    SELL = "sell"


@dataclass
class ModelDecision:
    action: TradeAction
    confidence: float
    stop_pct: float
    take_pct: float
    reason: str
    provider: str

    def is_actionable(self, min_confidence: float) -> bool:
        return self.action != TradeAction.NONE and self.confidence >= min_confidence


@dataclass
class Position:
    symbol: str
    side: TradeAction
    size: float
    entry_price: float
    stop_pct: float
    take_pct: float


@dataclass
class RiskLimits:
    balance: float
    daily_loss_limit: float
    risk_per_trade: float
    accumulated_pnl: float = 0.0

    def can_risk(self, amount: float) -> bool:
        return (self.accumulated_pnl - amount) >= -self.daily_loss_limit

    def register_loss(self, amount: float) -> None:
        self.accumulated_pnl -= amount

    def register_profit(self, amount: float) -> None:
        self.accumulated_pnl += amount


@dataclass
class OrderResult:
    symbol: str
    side: TradeAction
    size: float
    price: float
    status: str
    order_id: Optional[str] = None
