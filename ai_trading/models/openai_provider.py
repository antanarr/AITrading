from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import httpx

from ..types import ModelDecision
from .base import ModelProvider


class OpenAICompatibleProvider(ModelProvider):
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.2,
        top_p: float = 1.0,
        max_tokens: int = 512,
        timeout: float = 30.0,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(name)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.extra_headers = extra_headers or {}
        self._client = httpx.AsyncClient(timeout=self.timeout)

    async def generate(self, prompt: str) -> ModelDecision:
        payload = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": "You are a trading signal model. Reply with JSON only.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", **self.extra_headers}
        response = await self._client.post(
            f"{self.base_url}/chat/completions", json=payload, headers=headers
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return self._normalize_payload(parsed)

    async def aclose(self) -> None:
        await self._client.aclose()


class OpenAIProvider(OpenAICompatibleProvider):
    def __init__(self, model: str, **kwargs: Any) -> None:
        api_key = os.getenv("OPENAI_API_KEY", "")
        base_url = kwargs.get("base_url", "https://api.openai.com/v1")
        super().__init__(
            name="openai",
            base_url=base_url,
            api_key=api_key,
            model=model,
            temperature=kwargs.get("temperature", 0.2),
            top_p=kwargs.get("top_p", 1.0),
            max_tokens=kwargs.get("max_tokens", 512),
            timeout=kwargs.get("timeout", 30.0),
        )


class GrokProvider(OpenAICompatibleProvider):
    def __init__(self, model: str, base_url: str, api_key: str, **kwargs: Any) -> None:
        super().__init__(
            name="grok",
            base_url=base_url,
            api_key=api_key,
            model=model,
            temperature=kwargs.get("temperature", 0.2),
            top_p=kwargs.get("top_p", 1.0),
            max_tokens=kwargs.get("max_tokens", 512),
            timeout=kwargs.get("timeout", 30.0),
        )
