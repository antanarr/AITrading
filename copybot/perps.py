"""Public Hyperliquid perpetual market data and breakout signals."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from time import time_ns

BAR_MS = 15 * 60 * 1000
BPS = Decimal(10000)
API = "https://api.hyperliquid.xyz"


def now_ms():
    return time_ns() // 1_000_000


def dex_for(coin):
    return coin.partition(":")[0] if ":" in coin else ""


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite number in Hyperliquid data")
    return result


def round_price(price, size_decimals, rounding=ROUND_DOWN):
    # Perps allow five significant figures, with an exception for integer prices.
    exponent = min(0, max(price.adjusted() - 4, size_decimals - 6))
    return price.quantize(Decimal(1).scaleb(exponent), rounding=rounding)


@dataclass(frozen=True)
class Market:
    coin: str
    decimals: int
    volume: Decimal
    funding: Decimal
    mark: Decimal


@dataclass(frozen=True)
class Signal:
    market: Market
    long: bool
    bar_end: int
    trigger: Decimal
    close: Decimal


@dataclass(frozen=True)
class Book:
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    timestamp: int

    @property
    def spread_bps(self):
        return (self.ask - self.bid) * BPS * 2 / (self.ask + self.bid)


def breakout(market, candles, observed_at):
    closed = [bar for bar in candles if bar["T"] < observed_at]
    closed.sort(key=lambda bar: bar["t"])
    bars = closed[-24:]
    if len(bars) < 24 or observed_at - bars[-1]["T"] > BAR_MS:
        return None
    if any(
        bar["s"] != market.coin
        or bar["i"] != "15m"
        or bar["T"] != bar["t"] + BAR_MS - 1
        or bar["t"] != bars[0]["t"] + index * BAR_MS
        for index, bar in enumerate(bars)
    ):
        return None
    closes = [number(bar["c"]) for bar in bars]
    if min(closes) <= 0:
        return None
    high = max(number(bar["h"]) for bar in bars[-13:-1])
    low = min(number(bar["l"]) for bar in bars[-13:-1])
    recent = sum(closes[-6:]) / 6
    trend = sum(closes) / 24
    if closes[-1] > high and recent > trend:
        return Signal(market, True, bars[-1]["T"], high, closes[-1])
    if closes[-1] < low and recent < trend:
        return Signal(market, False, bars[-1]["T"], low, closes[-1])
    return None


class Feed:
    def __init__(self, client):
        self.client = client

    def info(self, kind, **payload):
        response = self.client.post(f"{API}/info", json={"type": kind, **payload})
        response.raise_for_status()
        return response.json()

    def markets(self, dex):
        meta, contexts = self.info("metaAndAssetCtxs", dex=dex)
        result = {}
        for asset, context in zip(meta["universe"], contexts, strict=True):
            if asset.get("isDelisted") or not context.get("midPx"):
                continue
            coin = asset["name"]
            result[coin] = Market(
                coin,
                asset["szDecimals"],
                number(context["dayNtlVlm"]),
                number(context["funding"]),
                number(context["markPx"]),
            )
        return result

    def book(self, coin):
        result = self.info("l2Book", coin=coin)
        bids, asks = result["levels"]
        if result["coin"] != coin or not bids or not asks:
            raise ValueError(f"No two-sided order book for {coin}")
        bid, ask = bids[0], asks[0]
        book = Book(
            number(bid["px"]),
            number(ask["px"]),
            number(bid["sz"]),
            number(ask["sz"]),
            result["time"],
        )
        if not 0 < book.bid <= book.ask or min(book.bid_size, book.ask_size) <= 0:
            raise ValueError(f"Invalid order book for {coin}")
        if not -1000 <= now_ms() - book.timestamp <= 5000:
            raise ValueError(f"Stale order book for {coin}")
        return book

    def signal(self, market, observed_at):
        candles = self.info(
            "candleSnapshot",
            req={
                "coin": market.coin,
                "interval": "15m",
                "startTime": observed_at - 26 * BAR_MS,
                "endTime": observed_at,
            },
        )
        return breakout(market, candles, observed_at)

    def scan(self, allowed=(), limit=12, min_volume=Decimal(10_000_000), max_funding_bps=2):
        venues = (
            sorted({dex_for(coin) for coin in allowed})
            if allowed
            else [row["name"] if row else "" for row in self.info("perpDexs")]
        )
        with ThreadPoolExecutor(max_workers=6) as pool:
            markets = {
                coin: asset
                for group in pool.map(self.markets, venues)
                for coin, asset in group.items()
            }
            eligible = sorted(
                (
                    asset
                    for asset in markets.values()
                    if asset.volume >= min_volume and (not allowed or asset.coin in allowed)
                ),
                key=lambda asset: (-asset.volume, asset.coin),
            )[:limit]
            observed_at = now_ms()
            signals = list(pool.map(lambda asset: self.signal(asset, observed_at), eligible))
        return [
            signal
            for signal in signals
            if signal
            and (signal.market.funding if signal.long else -signal.market.funding) * BPS
            <= max_funding_bps
        ], {"venues": len(venues), "markets": len(markets), "evaluated": len(eligible)}
