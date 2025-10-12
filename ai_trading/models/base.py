from __future__ import annotations

import abc
from typing import Any, Dict

from ..types import ModelDecision, TradeAction


class ModelProvider(abc.ABC):
    name: str

    def __init__(self, name: str) -> None:
        self.name = name

    @abc.abstractmethod
    async def generate(self, prompt: str) -> ModelDecision:
        raise NotImplementedError

    def _normalize_payload(self, payload: Dict[str, Any]) -> ModelDecision:
        try:
            action_raw = str(payload.get("action", "hold")).lower()
            action = TradeAction(action_raw if action_raw in TradeAction._value2member_map_ else "hold")
            decision = ModelDecision(
                action=action,
                confidence=float(payload.get("confidence", 0.0)),
                stop_pct=float(payload.get("stop_pct", 0.0)),
                take_pct=float(payload.get("take_pct", 0.0)),
                reason=str(payload.get("reason", "")),
                provider=self.name,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid payload from provider {self.name}: {payload}") from exc
        return decision
