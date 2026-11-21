"""Data contracts for the offline Hyperliquid research pipeline."""

from dataclasses import dataclass, field

HOUR_MS = 3_600_000
DAY_HOURS = 24


def excluded_symbol(coin: str) -> bool:
    return coin.rsplit(":", 1)[-1].upper() in {"BTC", "XBT", "WBTC", "KBTC"}


@dataclass(frozen=True, slots=True)
class Bar:
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class Funding:
    time_ms: int
    rate: float
    premium: float | None = None


@dataclass(frozen=True, slots=True)
class MarketHistory:
    coin: str
    decimals: int
    is_meme: bool
    listing_hour: int | None
    bars: dict[int, Bar]
    funding: dict[int, Funding]
    quarter_bars: dict[int, Bar] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OrderPlan:
    coin: str
    side: int
    limit: float
    atr: float
    stop_atr: float
    target_atr: float | None
    hold_hours: int
    ttl_hours: int
    mode: str = "maker"
    trailing_atr: float | None = None
    target_price: float | None = None
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class TradePlan:
    legs: tuple[OrderPlan, ...]
    label: str
