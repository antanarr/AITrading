"""Public market observations and depth-based paper fills for native perps."""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal, DecimalException
from fractions import Fraction

from .perps import API, number, round_price
from .perps import now_ms as system_now_ms
from .research_models import HOUR_MS, excluded_symbol

TAKER_FEE_RATE = Decimal("0.00045")
MAX_BOOK_LEVELS = 20
FUNDING_WINDOW_MS = 480 * HOUR_MS
_PUBLIC_FIELDS = {
    "l2Book": {"coin"},
    "metaAndAssetCtxs": set(),
    "fundingHistory": {"coin", "startTime", "endTime"},
    "candleSnapshot": {"req"},
}
ZERO = Decimal(0)


def _valid_coin(coin):
    return (
        isinstance(coin, str)
        and bool(coin)
        and coin.isascii()
        and all(character.isalnum() or character in "._-" for character in coin)
        and not excluded_symbol(coin)
    )


def _require_coin(coin):
    if not _valid_coin(coin):
        raise ValueError("Expected a non-BTC native perpetual market")


def _decimal(value):
    try:
        return number(value)
    except (DecimalException, ValueError) as error:
        raise ValueError("Expected a finite decimal market value") from error


def _size_unit(size_decimals):
    if type(size_decimals) is not int or not 0 <= size_decimals <= 6:
        raise ValueError("Expected Hyperliquid perp size precision in [0, 6]")
    return Decimal(1).scaleb(-size_decimals)


def round_size(quantity: Decimal, size_decimals: int) -> Decimal:
    quantity = _decimal(quantity)
    if quantity < 0:
        raise ValueError("Order quantity must be nonnegative")
    return quantity.quantize(_size_unit(size_decimals), rounding=ROUND_DOWN)


def size_for_notional(notional: Decimal, price: Decimal, size_decimals: int) -> Decimal:
    """Floor exact scaled lots so a cap-price fill cannot exceed the budget."""
    notional, price = _decimal(notional), _decimal(price)
    if notional < 0 or price <= 0:
        raise ValueError("Require nonnegative notional and positive price")
    unit = _size_unit(size_decimals)
    lots = Fraction(notional) // (Fraction(price) * Fraction(unit))
    return Decimal(lots) * unit


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    quantity: Decimal

    def __post_init__(self):
        if not all(
            isinstance(value, Decimal) and value.is_finite() and value > 0
            for value in (self.price, self.quantity)
        ):
            raise ValueError("Order depth requires positive Decimal price and quantity")

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity


@dataclass(frozen=True, slots=True)
class L2Book:
    coin: str
    exchange_ms: int
    received_ms: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]

    def __post_init__(self):
        _require_coin(self.coin)
        if (
            type(self.exchange_ms) is not int
            or type(self.received_ms) is not int
            or not 0 < self.exchange_ms <= self.received_ms
        ):
            raise ValueError(f"Invalid exchange or receipt timestamp for {self.coin}")
        if not 1 <= len(self.bids) <= MAX_BOOK_LEVELS or not 1 <= len(self.asks) <= MAX_BOOK_LEVELS:
            raise ValueError(f"Expected two-sided depth of up to {MAX_BOOK_LEVELS} levels")
        if (
            self.bids[0].price >= self.asks[0].price
            or any(
                left.price <= right.price
                for left, right in zip(self.bids, self.bids[1:], strict=False)
            )
            or any(
                left.price >= right.price
                for left, right in zip(self.asks, self.asks[1:], strict=False)
            )
        ):
            raise ValueError(f"Crossed, locked or unsorted order depth for {self.coin}")


@dataclass(frozen=True, slots=True)
class MarketContext:
    coin: str
    size_decimals: int
    mark: Decimal
    oracle: Decimal
    requested_ms: int
    received_ms: int
    max_leverage: int | None = None


@dataclass(frozen=True, slots=True)
class FundingEvent:
    coin: str
    timestamp_ms: int
    rate: Decimal
    premium: Decimal | None


@dataclass(frozen=True, slots=True)
class IOCFill:
    requested_quantity: Decimal
    limit_price: Decimal | None
    fills: tuple[BookLevel, ...]

    @property
    def quantity(self) -> Decimal:
        return sum((fill.quantity for fill in self.fills), ZERO)

    @property
    def notional(self) -> Decimal:
        return sum((fill.notional for fill in self.fills), ZERO)

    @property
    def average_price(self) -> Decimal | None:
        return self.notional / self.quantity if self.fills else None

    @property
    def fee(self) -> Decimal:
        return self.notional * TAKER_FEE_RATE

    @property
    def remaining(self) -> Decimal:
        return self.requested_quantity - self.quantity


def sweep_ioc(
    book: L2Book,
    *,
    buy: bool,
    quantity: Decimal,
    size_decimals: int,
    limit_price: Decimal | None = None,
) -> IOCFill:
    """Sweep the observed ladder once, preserving any unfilled amount."""
    if type(buy) is not bool:
        raise ValueError("An IOC requires an explicit buy or sell side")
    requested = round_size(quantity, size_decimals)
    cap = None
    if limit_price is not None:
        raw_cap = _decimal(limit_price)
        if raw_cap <= 0:
            raise ValueError("Limit price must be positive")
        cap = round_price(raw_cap, size_decimals, ROUND_DOWN if buy else ROUND_UP)
        if cap <= 0:
            raise ValueError("Limit price is below the first legal tick")
    levels = book.asks if buy else book.bids
    fills = []
    remaining = requested
    for level in levels:
        if not remaining or (cap is not None and (level.price > cap if buy else level.price < cap)):
            break
        filled = min(remaining, round_size(level.quantity, size_decimals))
        if filled:
            fills.append(BookLevel(level.price, filled))
            remaining -= filled
    return IOCFill(requested, cap, tuple(fills))


class PublicMarket:
    """Timestamp public Hyperliquid observations without account or signing access."""

    def __init__(
        self,
        client,
        *,
        max_book_age_ms: int = 10_000,
        clock: Callable[[], int] = system_now_ms,
    ):
        if type(max_book_age_ms) is not int or max_book_age_ms <= 0:
            raise ValueError("Order book age limit must be positive milliseconds")
        self.client = client
        self.max_book_age_ms = max_book_age_ms
        self.clock = clock

    def info(self, kind, **kwargs):
        if kind not in _PUBLIC_FIELDS or kwargs.keys() != _PUBLIC_FIELDS[kind]:
            raise ValueError("Forward market feed accepts public market endpoints only")
        if kind == "candleSnapshot":
            request = kwargs["req"]
            if (
                not isinstance(request, dict)
                or request.keys() != {"coin", "interval", "startTime", "endTime"}
                or request["interval"] not in ("1h", "1d")
                or type(request["startTime"]) is not int
                or type(request["endTime"]) is not int
                or not 0 < request["startTime"] <= request["endTime"]
            ):
                raise ValueError("Invalid forward candle snapshot request")
            _require_coin(request["coin"])
        elif "coin" in kwargs:
            _require_coin(kwargs["coin"])
        response = self.client.post(f"{API}/info", json={"type": kind, **kwargs})
        response.raise_for_status()
        return response.json()

    def book(self, coin: str, *, now_ms: int | None = None) -> L2Book:
        _require_coin(coin)
        data = self.info("l2Book", coin=coin)
        received = self.clock() if now_ms is None else now_ms
        try:
            if data["coin"] != coin:
                raise ValueError(f"Order book returned the wrong market for {coin}")
            bids, asks = (
                tuple(BookLevel(_decimal(level["px"]), _decimal(level["sz"])) for level in side)
                for side in data["levels"]
            )
            book = L2Book(coin, data["time"], received, bids, asks)
        except (KeyError, TypeError) as error:
            raise ValueError(f"Malformed order book for {coin}") from error
        if book.received_ms - book.exchange_ms > self.max_book_age_ms:
            raise ValueError(f"Stale order book for {coin}")
        return book

    def contexts(self) -> dict[str, MarketContext]:
        requested = self.clock()
        data = self.info("metaAndAssetCtxs")
        received = self.clock()
        if type(requested) is not int or type(received) is not int or not 0 < requested <= received:
            raise ValueError("Invalid local timestamps for market context")
        try:
            meta, contexts = data
            rows = zip(meta["universe"], contexts, strict=True)
            result = {}
            seen = set()
            for asset, row in rows:
                if not isinstance(asset, dict):
                    continue
                coin = asset.get("name")
                if not _valid_coin(coin):
                    continue
                if coin in seen:
                    raise ValueError(f"Duplicate native perp market context for {coin}")
                seen.add(coin)
                if asset.get("isDelisted") or not isinstance(row, dict):
                    continue
                size_decimals = asset.get("szDecimals")
                try:
                    _size_unit(size_decimals)
                    mark = _decimal(row.get("markPx"))
                    oracle = _decimal(row.get("oraclePx"))
                except ValueError:
                    continue
                if mark > 0 and oracle > 0:
                    maximum = asset.get("maxLeverage")
                    result[coin] = MarketContext(
                        coin,
                        size_decimals,
                        mark,
                        oracle,
                        requested,
                        received,
                        maximum if type(maximum) is int and maximum > 0 else None,
                    )
        except (TypeError, KeyError) as error:
            raise ValueError("Malformed native perp context response") from error
        if not result:
            raise ValueError("No usable native perp context was returned")
        return result

    def funding(self, coin: str, start_ms: int, end_ms: int) -> tuple[FundingEvent, ...]:
        """Fetch published events within inclusive bounds. Missing events stay missing."""
        _require_coin(coin)
        if type(start_ms) is not int or type(end_ms) is not int or not 0 <= start_ms <= end_ms:
            raise ValueError("Invalid inclusive funding history bounds")
        observations = {}
        for cursor in range(start_ms, end_ms + 1, FUNDING_WINDOW_MS):
            stop = min(end_ms, cursor + FUNDING_WINDOW_MS - 1)
            data = self.info("fundingHistory", coin=coin, startTime=cursor, endTime=stop)
            if not isinstance(data, list) or len(data) >= 500:
                raise ValueError(f"Malformed or truncated funding history for {coin}")
            for row in data:
                try:
                    stamp, rate = row["time"], _decimal(row["fundingRate"])
                    raw_premium = row.get("premium")
                    if row["coin"] != coin or type(stamp) is not int or not cursor <= stamp <= stop:
                        raise ValueError(f"Unexpected funding market or timestamp for {coin}")
                    event = FundingEvent(
                        coin,
                        stamp,
                        rate,
                        _decimal(raw_premium) if raw_premium is not None else None,
                    )
                except (TypeError, KeyError) as error:
                    raise ValueError(f"Malformed funding observation for {coin}") from error
                hour = stamp // HOUR_MS
                if observations.setdefault(hour, event) != event:
                    raise ValueError(f"Conflicting funding observations in hour {hour} for {coin}")
        return tuple(observations[hour] for hour in sorted(observations))
