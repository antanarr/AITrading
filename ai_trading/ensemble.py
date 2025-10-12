from __future__ import annotations

import logging
from collections import Counter
from typing import Iterable, List

from .config import EnsembleConfig
from .types import ModelDecision, TradeAction

LOGGER = logging.getLogger(__name__)


class Ensemble:
    def __init__(self, cfg: EnsembleConfig) -> None:
        self.cfg = cfg

    def vote(self, decisions: Iterable[ModelDecision]) -> ModelDecision | None:
        actionable = [d for d in decisions if d.is_actionable(self.cfg.min_confidence)]
        if not actionable:
            LOGGER.info("No actionable decisions from providers")
            return None

        action_counter = Counter(d.action for d in actionable)
        best_action, count = action_counter.most_common(1)[0]
        if count < self.cfg.require_agreement:
            LOGGER.info(
                "Consensus not reached: %s", {a.value: c for a, c in action_counter.items()}
            )
            return None

        subset = [d for d in actionable if d.action == best_action]
        avg_confidence = sum(d.confidence for d in subset) / len(subset)
        avg_stop = sum(d.stop_pct for d in subset) / len(subset)
        avg_take = sum(d.take_pct for d in subset) / len(subset)
        reason = " | ".join(d.reason for d in subset)

        LOGGER.info(
            "Ensemble consensus: %s with confidence %.2f (%d votes)",
            best_action,
            avg_confidence,
            len(subset),
        )

        return ModelDecision(
            action=best_action,
            confidence=avg_confidence,
            stop_pct=avg_stop,
            take_pct=avg_take,
            reason=reason,
            provider="ensemble",
        )
