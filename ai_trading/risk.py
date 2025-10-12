from __future__ import annotations

import logging
from datetime import datetime, timezone

from .config import RiskConfig

LOGGER = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, cfg: RiskConfig) -> None:
        self.cfg = cfg
        self.session_start = datetime.now(tz=timezone.utc)
        self.session_loss = 0.0

    def reset_if_new_day(self) -> None:
        now = datetime.now(tz=timezone.utc)
        if now.date() != self.session_start.date():
            self.session_start = now
            self.session_loss = 0.0
            LOGGER.info("Risk manager daily counters reset")

    def reserve_risk(self, amount: float) -> None:
        self.session_loss -= abs(amount)
        LOGGER.info("Reserved risk %.2f (session loss %.2f)", amount, self.session_loss)

    def register_loss(self, amount: float) -> None:
        self.session_loss -= abs(amount)
        LOGGER.warning("Loss registered: %.2f (session loss %.2f)", amount, self.session_loss)

    def register_profit(self, amount: float) -> None:
        self.session_loss += abs(amount)
        LOGGER.info("Profit registered: %.2f (session loss %.2f)", amount, self.session_loss)

    def allow_trade(self, risk_amount: float) -> bool:
        if not self.cfg.kill_switch:
            return True
        projected = self.session_loss - risk_amount
        allowed = projected >= -self.cfg.daily_loss_limit
        if not allowed:
            LOGGER.error(
                "Trade blocked: projected daily loss %.2f exceeds limit %.2f",
                projected,
                self.cfg.daily_loss_limit,
            )
        return allowed

    def check_kill_switch(self) -> bool:
        if not self.cfg.kill_switch:
            return False
        threshold = self.cfg.daily_loss_limit
        breached = self.session_loss <= -threshold
        if breached:
            LOGGER.error(
                "Daily loss limit reached (%.2f <= -%.2f). Trading halted.",
                self.session_loss,
                threshold,
            )
        return breached
