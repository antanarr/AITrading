"""Public Hyperliquid perpetual market data and breakout signals."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from decimal import ROUND_DOWN, Decimal, DecimalException
from statistics import median
from time import time_ns

import httpx

BAR_MS = 15 * 60 * 1000
BPS = Decimal(10000)
API = "https://api.hyperliquid.xyz"
SENTIMENT_SOURCE = "https://alternative.me/crypto/fear-and-greed-index/"
SENTIMENT_API = "https://api.alternative.me/fng/"
SENTIMENT_MAX_AGE_MS = 36 * 60 * 60 * 1000


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


def optional_number(value):
    if value is None:
        return None
    try:
        return number(value)
    except (DecimalException, ValueError):
        return None


@dataclass(frozen=True)
class Market:
    coin: str
    decimals: int
    volume: Decimal
    funding: Decimal
    mark: Decimal
    change_24h: Decimal | None = None


@dataclass(frozen=True)
class Signal:
    market: Market
    long: bool
    bar_end: int
    trigger: Decimal
    close: Decimal
    relative_volume: Decimal | None = None
    score: int = 0
    factors: tuple[str, ...] = ()

    def evidence(self):
        return {
            "score": self.score,
            "reasons": self.factors,
            "breakout_volume_vs_median": (
                str(round(self.relative_volume, 2)) if self.relative_volume is not None else None
            ),
            "entry_side_funding_bps_per_hour": str(
                (self.market.funding if self.long else -self.market.funding) * BPS
            ),
        }


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
    long = closes[-1] > high and recent > trend
    if not long and not (closes[-1] < low and recent < trend):
        return None
    volumes = [optional_number(bar.get("v")) for bar in bars[-13:]]
    ratio = None
    if all(volume is not None and volume >= 0 for volume in volumes):
        baseline = median(volumes[:-1])
        if baseline > 0:
            ratio = volumes[-1] / baseline
    return Signal(market, long, bars[-1]["T"], high if long else low, closes[-1], ratio)


def market_breadth(markets, min_volume):
    changes = [
        market.change_24h
        for market in markets
        if not dex_for(market.coin)
        and market.volume >= min_volume
        and market.change_24h is not None
    ]
    return {
        "advancing": sum(change > 0 for change in changes),
        "declining": sum(change < 0 for change in changes),
        "total": len(changes),
    }


def rank_signal(signal, breadth, sentiment):
    score = 0
    factors = []
    ratio = signal.relative_volume
    if ratio is not None and ratio >= Decimal("1.5"):
        score += 2
        factors.append("Breakout volume is at least 1.5x its recent median")
    elif ratio is not None and ratio < Decimal("0.75"):
        score -= 1
        factors.append("Breakout volume is below 0.75x its recent median")

    # Native crypto perps share the breadth and BTC sentiment context. Builder markets may track stocks.
    if not dex_for(signal.market.coin):
        total = breadth["total"]
        aligned, opposed = ("advancing", "declining") if signal.long else ("declining", "advancing")
        if total >= 5 and breadth[aligned] * 5 >= 3 * total:
            score += 1
            factors.append("Broad crypto market move supports this direction")
        elif total >= 5 and breadth[opposed] * 5 >= 3 * total:
            score -= 1
            factors.append("Broad crypto market move opposes this direction")
        index = sentiment.get("index")
        # Use only extreme readings and give them less weight than volume confirmation.
        if index is not None and (index <= 25 or index >= 75):
            score += 1 if signal.long == (index >= 75) else -1
            factors.append(
                f"Bitcoin Fear & Greed is {'high' if index >= 75 else 'low'} (Alternative.me)"
            )

    direction_funding = (signal.market.funding if signal.long else -signal.market.funding) * BPS
    if direction_funding >= Decimal("0.5"):
        score -= 1
        factors.append("This side pays at least 0.5 basis points per hour in funding")
    return replace(signal, score=score, factors=tuple(factors))


class Feed:
    def __init__(self, client):
        self.client = client
        self.sentiment_observation = None
        self.sentiment_retry_ms = 0
        self.sentiment_status = "unavailable"

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
            previous = optional_number(context.get("prevDayPx"))
            mid = optional_number(context["midPx"])
            result[coin] = Market(
                coin,
                asset["szDecimals"],
                number(context["dayNtlVlm"]),
                number(context["funding"]),
                number(context["markPx"]),
                mid / previous - 1 if mid and mid > 0 and previous and previous > 0 else None,
            )
        return result

    def fear_and_greed(self, observed_at):
        if observed_at >= self.sentiment_retry_ms:
            self.sentiment_retry_ms = observed_at + 5 * 60 * 1000
            try:
                response = self.client.get(SENTIMENT_API, timeout=2.5)
                response.raise_for_status()
                body = response.json()
                if body["metadata"]["error"]:
                    raise ValueError("Sentiment source reported an error")
                row = body["data"][0]
                index = int(str(row["value"]))
                timestamp = int(str(row["timestamp"])) * 1000
                if not 0 <= index <= 100 or not 0 < timestamp <= observed_at:
                    raise ValueError("Invalid Fear & Greed observation")
                if self.sentiment_observation and timestamp < self.sentiment_observation[1]:
                    raise ValueError("Fear & Greed observation moved backward")
                self.sentiment_observation = (index, timestamp)
                self.sentiment_status = "current"
                self.sentiment_retry_ms = observed_at + 60 * 60 * 1000
            except (httpx.HTTPError, ValueError, IndexError, KeyError, TypeError):
                self.sentiment_status = "unavailable"

        data = {"source": "Alternative.me", "source_url": SENTIMENT_SOURCE}
        if self.sentiment_observation:
            index, timestamp = self.sentiment_observation
            if 0 <= observed_at - timestamp <= SENTIMENT_MAX_AGE_MS:
                status = "current" if self.sentiment_status == "current" else "cached"
                return {**data, "status": status, "index": index, "observed_at_ms": timestamp}
            return {**data, "status": "stale", "observed_at_ms": timestamp}
        return {**data, "status": "unavailable"}

    def _book_snapshot(self, coin):
        result = self.info("l2Book", coin=coin)
        if result["coin"] != coin:
            raise ValueError(f"Order book returned the wrong market for {coin}")
        if not -1000 <= now_ms() - result["time"] <= 5000:
            raise ValueError(f"Stale order book for {coin}")
        return result

    def book(self, coin):
        result = self._book_snapshot(coin)
        bids, asks = result["levels"]
        if not bids or not asks:
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
        return book

    def exit_price(self, coin, sell):
        levels = self._book_snapshot(coin)["levels"][0 if sell else 1]
        if not levels:
            raise ValueError(f"No executable side for the exit on {coin}")
        price = number(levels[0]["px"])
        if price <= 0 or number(levels[0]["sz"]) <= 0:
            raise ValueError(f"Invalid executable quote for {coin}")
        return price

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
            sorted({"", *(dex_for(coin) for coin in allowed)})
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
            sentiment_task = pool.submit(self.fear_and_greed, observed_at)
            signals = list(pool.map(lambda asset: self.signal(asset, observed_at), eligible))
            sentiment = sentiment_task.result()
        breadth = market_breadth(markets.values(), min_volume)
        ranked = [
            rank_signal(signal, breadth, sentiment)
            for signal in signals
            if signal
            and (signal.market.funding if signal.long else -signal.market.funding) * BPS
            <= max_funding_bps
        ]
        ranked.sort(key=lambda signal: (-signal.score, -signal.market.volume, signal.market.coin))
        return ranked, {
            "venues": len(venues),
            "markets": len(markets),
            "evaluated": len(eligible),
            "market_context": {
                "native_perp_breadth_24h": breadth,
                "bitcoin_fear_and_greed": sentiment,
            },
        }
