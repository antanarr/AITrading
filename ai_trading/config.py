from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml


@dataclass
class ProviderConfig:
    enabled: bool = True
    model: Optional[str] = None
    temperature: float = 0.2
    top_p: float = 1.0
    max_tokens: int = 512
    timeout: float = 30.0


@dataclass
class EnsembleConfig:
    min_confidence: float = 0.6
    require_agreement: int = 2


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.01
    daily_loss_limit: float = 0.05
    kill_switch: bool = True


@dataclass
class DataConfig:
    timeframe: str = "5m"
    lookback: int = 200


@dataclass
class ExchangeConfig:
    name: str = "mexc"
    params: Dict[str, object] = field(default_factory=dict)
    testnet: bool = True


@dataclass
class Config:
    symbols: List[str]
    providers: Dict[str, ProviderConfig]
    ensemble: EnsembleConfig = field(default_factory=EnsembleConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    data: DataConfig = field(default_factory=DataConfig)
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    webhook_secret: Optional[str] = None

    @staticmethod
    def load(path: Path) -> "Config":
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)

        providers_cfg = {
            name: ProviderConfig(**values) for name, values in raw.get("providers", {}).items()
        }

        return Config(
            symbols=raw.get("symbols", ["BTC/USDT"]),
            providers=providers_cfg,
            ensemble=EnsembleConfig(**raw.get("ensemble", {})),
            risk=RiskConfig(**raw.get("risk", {})),
            data=DataConfig(**raw.get("data", {})),
            exchange=ExchangeConfig(**raw.get("exchange", {})),
            webhook_secret=raw.get("webhook_secret"),
        )


DEFAULT_CONFIG_PATH = Path("config.yaml")
