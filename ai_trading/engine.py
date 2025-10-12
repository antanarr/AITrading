from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, List, Optional

import ccxt

from .config import Config, DEFAULT_CONFIG_PATH
from .data import MarketDataClient
from .ensemble import Ensemble
from .models.anthropic_provider import AnthropicProvider
from .models.openai_provider import GrokProvider, OpenAICompatibleProvider, OpenAIProvider
from .strategy import build_prompt
from .types import ModelDecision

LOGGER = logging.getLogger(__name__)


class TradingEngine:
    def __init__(
        self,
        cfg: Config,
        providers: Dict[str, OpenAICompatibleProvider],
        anthropic_provider: Optional[AnthropicProvider],
        market_data: MarketDataClient,
        execution_client,
    ) -> None:
        self.cfg = cfg
        self.providers = providers
        self.anthropic_provider = anthropic_provider
        self.market_data = market_data
        self.execution = execution_client
        self.ensemble = Ensemble(cfg.ensemble)

    async def run_once(self) -> None:
        for symbol in self.cfg.symbols:
            LOGGER.info("Processing symbol %s", symbol)
            df = self.market_data.fetch_ohlcv(symbol)
            features = self.market_data.compute_features(df)
            snapshot = self.market_data.latest_snapshot(features)
            prompt = build_prompt(symbol, self.cfg.data.timeframe, snapshot)
            decisions = await self._query_models(prompt)
            consensus = self.ensemble.vote(decisions)
            if not consensus:
                continue
            price = snapshot["close"]
            self.execution.execute(
                symbol,
                consensus.action,
                price,
                consensus.stop_pct,
                consensus.take_pct,
            )

    async def _query_models(self, prompt: str) -> List[ModelDecision]:
        tasks = [provider.generate(prompt) for provider in self.providers.values()]
        if self.anthropic_provider:
            tasks.append(self.anthropic_provider.generate(prompt))
        decisions: List[ModelDecision] = []
        for task in asyncio.as_completed(tasks):
            try:
                decision = await task
            except Exception as exc:  # pylint: disable=broad-except
                LOGGER.exception("Provider failed: %s", exc)
                continue
            decisions.append(decision)
            LOGGER.info(
                "Provider %s -> %s (conf %.2f)",
                decision.provider,
                decision.action.value,
                decision.confidence,
            )
        return decisions

    @classmethod
    def from_env(cls, cfg: Optional[Config] = None, paper: bool = True):
        cfg = cfg or Config.load(DEFAULT_CONFIG_PATH)
        use_testnet = os.getenv("USE_TESTNET")
        if use_testnet is not None:
            cfg.exchange.testnet = use_testnet.lower() == "true"
        exchange = cls._init_exchange(cfg, paper)
        market_data = MarketDataClient(cfg, exchange)
        execution_client = ExecutionFactory.create(cfg, exchange, paper)
        providers: Dict[str, OpenAICompatibleProvider] = {}

        openai_cfg = cfg.providers.get("openai")
        if openai_cfg and openai_cfg.enabled and os.getenv("OPENAI_API_KEY"):
            providers["openai"] = OpenAIProvider(
                model=openai_cfg.model or "gpt-4o-mini",
                temperature=openai_cfg.temperature,
                top_p=openai_cfg.top_p,
                max_tokens=openai_cfg.max_tokens,
                timeout=openai_cfg.timeout,
            )

        grok_cfg = cfg.providers.get("grok")
        grok_base = os.getenv("GROK_BASE_URL")
        grok_key = os.getenv("GROK_API_KEY")
        if grok_cfg and grok_cfg.enabled and grok_base and grok_key:
            providers["grok"] = GrokProvider(
                model=grok_cfg.model or "grok-latest",
                base_url=grok_base,
                api_key=grok_key,
                temperature=grok_cfg.temperature,
                top_p=grok_cfg.top_p,
                max_tokens=grok_cfg.max_tokens,
                timeout=grok_cfg.timeout,
            )

        anthropic_provider = None
        anthropic_cfg = cfg.providers.get("anthropic")
        if anthropic_cfg and anthropic_cfg.enabled and os.getenv("ANTHROPIC_API_KEY"):
            anthropic_provider = AnthropicProvider(
                model=anthropic_cfg.model or "claude-3-5-sonnet-20240620",
                temperature=anthropic_cfg.temperature,
                max_tokens=anthropic_cfg.max_tokens,
            )

        return cls(cfg, providers, anthropic_provider, market_data, execution_client)

    @staticmethod
    def _init_exchange(cfg: Config, paper: bool) -> ccxt.Exchange:
        exchange_class = getattr(ccxt, cfg.exchange.name)
        params = cfg.exchange.params.copy()
        if paper and cfg.exchange.testnet:
            params.setdefault("options", {}).update({"defaultType": "swap"})
        exchange = exchange_class(params)
        api_key = os.getenv("API_KEY")
        api_secret = os.getenv("API_SECRET")
        if api_key and api_secret:
            exchange.apiKey = api_key
            exchange.secret = api_secret
        if cfg.exchange.testnet:
            exchange.set_sandbox_mode(True)
        return exchange


class ExecutionFactory:
    @staticmethod
    def create(cfg: Config, exchange: ccxt.Exchange, paper: bool):
        from .execution import ExecutionClient  # Local import to avoid circular dependency

        return ExecutionClient(cfg, exchange, paper=paper)
