from __future__ import annotations

import json
import os

import anthropic

from ..types import ModelDecision
from .base import ModelProvider


class AnthropicProvider(ModelProvider):
    def __init__(self, model: str, temperature: float = 0.2, max_tokens: int = 512) -> None:
        super().__init__("anthropic")
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    async def generate(self, prompt: str) -> ModelDecision:
        message = await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system="You are a trading signal model. Reply with JSON only.",
            messages=[{"role": "user", "content": prompt}],
        )
        content = message.content[0].text
        parsed = json.loads(content)
        return self._normalize_payload(parsed)

    async def aclose(self) -> None:
        await self.client.close()
