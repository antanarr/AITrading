"""Point-in-time signals for offline Hyperliquid research."""

from bisect import bisect_left, bisect_right, insort
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from itertools import pairwise
from math import expm1, fsum, log, log1p, sqrt
from statistics import fmean, median

from .research_models import HOUR_MS, Bar, MarketHistory, OrderPlan, TradePlan, excluded_symbol

LISTING_MIN_HOURS = 12
LISTING_MAX_HOURS = 14 * 24
COMPRESSION_HISTORY_HOURS = 7 * 24
RETURN_WINDOWS = (4, 12, 24, 48, 72)
BASKET_TREND_WINDOWS = (72, 168)
MINIMUM_BASKET_SIZE = 5
IOC_LIMIT_BPS = 15
RankedOrder = tuple[float, OrderPlan]


@dataclass(frozen=True, slots=True)
class Variant:
    name: str
    family: str
    track: str
    settings: tuple[tuple[str, float], ...] = ()
    mode: str = "maker"


def variants() -> tuple[Variant, ...]:
    """Return the fixed, coarse search menu in a reproducible order."""
    return (
        Variant("funding-6h", "funding", "A", (("funding_bps", 0.35), ("high_count", 4))),
        Variant("funding-extreme", "funding", "A", (("funding_bps", 0.65), ("high_count", 5))),
        Variant("listing-6h", "listing", "A", (("window", 6),)),
        Variant("listing-12h", "listing", "A", (("window", 12),)),
        Variant("dispersion-4h", "dispersion", "A", (("window", 4), ("multiple", 2.25))),
        Variant("dispersion-12h", "dispersion", "A", (("window", 12), ("multiple", 3.0))),
        Variant("pair-clock", "pair", "A", (("window", 24), ("multiple", 2.5), ("interval", 12))),
        Variant("pair-dispersion", "pair", "A", (("window", 48), ("multiple", 3.5))),
        Variant("funding-squeeze-long", "funding", "B", (("funding_bps", 0.50), ("high_count", 4))),
        Variant("listing-washout-long", "listing", "B", (("window", 6), ("dump_fraction", 0.25))),
        Variant("compression-12h", "compression", "B", (("window", 12), ("percentile", 0.20))),
        Variant("compression-24h", "compression", "B", (("window", 24), ("percentile", 0.25))),
        Variant(
            "compression-12h-taker",
            "compression",
            "B",
            (("window", 12), ("percentile", 0.20)),
            "ioc",
        ),
        Variant("regime-24h", "regime", "B", (("window", 24), ("breadth", 0.65))),
        Variant("regime-72h", "regime", "B", (("window", 72), ("breadth", 0.70))),
        Variant(
            "funding-6h-basket-trend",
            "funding",
            "A",
            (("funding_bps", 0.35), ("high_count", 4), ("basket_trend", 1)),
        ),
        Variant(
            "dispersion-4h-basket-trend",
            "dispersion",
            "A",
            (("window", 4), ("multiple", 2.25), ("basket_trend", 1)),
        ),
        Variant(
            "compression-12h-maker-1.5r",
            "compression",
            "B",
            (("window", 12), ("percentile", 0.20), ("target_r", 1.5)),
        ),
        Variant(
            "compression-12h-maker-2.5r",
            "compression",
            "B",
            (("window", 12), ("percentile", 0.20), ("target_r", 2.5)),
        ),
        Variant(
            "regime-24h-maker-2.5r",
            "regime",
            "B",
            (("window", 24), ("breadth", 0.65), ("target_r", 2.5)),
        ),
        Variant(
            "pair-clock-ioc-momentum",
            "pair",
            "A",
            (("window", 24), ("multiple", 2.5), ("interval", 12), ("ioc_limit_bps", 10)),
            "ioc",
        ),
        Variant(
            "pair-clock-ioc-reversion",
            "pair",
            "A",
            (
                ("window", 24),
                ("multiple", 2.5),
                ("interval", 12),
                ("ioc_limit_bps", 10),
                ("direction", -1),
            ),
            "ioc",
        ),
        Variant(
            "pair-dispersion-ioc-momentum",
            "pair",
            "A",
            (("window", 72), ("multiple", 3.5), ("legs_per_side", 2), ("ioc_limit_bps", 10)),
            "ioc",
        ),
        Variant(
            "pair-dispersion-ioc-reversion",
            "pair",
            "A",
            (
                ("window", 72),
                ("multiple", 3.5),
                ("legs_per_side", 2),
                ("ioc_limit_bps", 10),
                ("direction", -1),
            ),
            "ioc",
        ),
    )


@dataclass(frozen=True, slots=True)
class PriceRange:
    low: float
    high: float


@dataclass(frozen=True, slots=True)
class SignalFrame:
    coin: str
    is_meme: bool
    age: int | None
    bar: Bar
    previous: Bar
    atr: float
    turnover: float
    volume_multiple: float
    vwap12: float
    vwap24: float
    previous_vwap12: float
    previous_vwap24: float
    prior_ranges: Mapping[int, PriceRange]
    returns: Mapping[int, float]
    hourly_vol: float | None
    vol_ratio: float | None
    funding_bps: tuple[float, ...]
    compression_percentiles: Mapping[int, float]
    listing_peak: float | None
    basket_trends: Mapping[int, float] = field(default_factory=dict)


class _CompressionHistory:
    def __init__(self) -> None:
        self.chronological: deque[float] = deque()
        self.sorted: list[float] = []

    def add(self, compression: float) -> float | None:
        rank = None
        if len(self.chronological) == COMPRESSION_HISTORY_HOURS:
            # Midrank places tied readings at the center of their rank interval.
            first = bisect_left(self.sorted, compression)
            last = bisect_right(self.sorted, compression)
            rank = (first + last) / (2 * COMPRESSION_HISTORY_HOURS)
            oldest = self.chronological.popleft()
            self.sorted.pop(bisect_left(self.sorted, oldest))
        self.chronological.append(compression)
        insort(self.sorted, compression)
        return rank


def _vwap(bars: list[Bar]) -> float:
    volume = sum(bar.volume for bar in bars)
    if not volume:
        return bars[-1].close
    return sum((bar.high + bar.low + bar.close) * bar.volume for bar in bars) / (3 * volume)


def _true_range(bar: Bar, previous: Bar) -> float:
    return max(bar.high - bar.low, abs(bar.high - previous.close), abs(bar.low - previous.close))


def _realized_vol(returns: list[float]) -> float:
    return sqrt(fmean(value * value for value in returns))


def _hourly_funding(history: MarketHistory) -> dict[int, float | None]:
    funding: dict[int, float | None] = {}
    for observation in history.funding.values():
        hour = observation.time_ms // HOUR_MS
        funding[hour] = None if hour in funding else observation.rate * 10_000
    return funding


def _recent_funding(funding: Mapping[int, float | None], hour: int) -> tuple[float, ...]:
    rates = []
    for closed_hour in range(hour - 6, hour):
        rate = funding.get(closed_hour)
        if rate is None:
            # Only expose the contiguous suffix, including the latest closed hour.
            rates.clear()
        else:
            rates.append(rate)
    return tuple(rates)


def _basket_trends(
    members: Mapping[int, list[MarketHistory]], start: int, end: int
) -> dict[int, dict[int, float]]:
    longest = max(BASKET_TREND_WINDOWS)
    links: deque[float] = deque(maxlen=longest)
    trends = {}
    for hour in range(start - longest + 1, end):
        roster = members.get(hour - 1, ())
        if len(roster) < MINIMUM_BASKET_SIZE:
            links.clear()
            continue

        # Hold the prior decision's roster through this link, even for an asset
        # that loses eligibility at the current close. A missing price cuts the chain.
        changes = []
        for history in roster:
            closed = history.bars.get(hour - 1)
            if closed is None:
                break
            previous = history.bars[hour - 2].close
            changes.append((closed.close - previous) / previous)
        else:
            links.append(log1p(fmean(changes)))
            if hour >= start and len(links) >= min(BASKET_TREND_WINDOWS):
                recent = list(links)
                trends[hour] = {
                    window: expm1(fsum(recent[-window:]))
                    for window in BASKET_TREND_WINDOWS
                    if len(links) >= window
                }
            continue
        links.clear()
    return trends


def build_frames(
    histories: Mapping[str, MarketHistory],
    start_hour: int,
    end_hour: int,
    *,
    minimum_turnover: float = 250_000,
    minimum_active_hours: int = 9,
    basket_histories: Mapping[str, MarketHistory] | None = None,
) -> dict[int, dict[str, SignalFrame]]:
    """Prepare eligible markets at UTC hour boundaries, with an exclusive end hour.

    Candle keys represent opens. Decisions at hour H observe candles and funding
    from hours strictly before H. Warmup builds features without emitting ideas.
    Basket prices may include markets that were excluded from execution for data quality.
    """
    frames: dict[int, dict[str, SignalFrame]] = {}
    warmup = max(LISTING_MAX_HOURS, COMPRESSION_HISTORY_HOURS + max(RETURN_WINDOWS))
    trading_coins = {history.coin: history for history in histories.values()}
    basket_coins = {
        history.coin: history
        for history in (histories if basket_histories is None else basket_histories).values()
        if history.is_meme and not excluded_symbol(history.coin)
    }
    basket_members: dict[int, list[MarketHistory]] = defaultdict(list)
    basket_start = start_hour - max(BASKET_TREND_WINDOWS)
    for coin, history in sorted((basket_coins | trading_coins).items()):
        if excluded_symbol(history.coin) or not history.bars:
            continue
        basket_member = coin in basket_coins
        first_decision = basket_start if basket_member else start_hour
        recent: deque[Bar] = deque(maxlen=1 + max(RETURN_WINDOWS))
        compression = {window: _CompressionHistory() for window in (12, 24)}
        funding = _hourly_funding(history)
        first = max(start_hour - warmup, min(history.bars))
        listing = history.listing_hour
        if listing is not None:
            first = max(first, listing)
        listing_peak = None
        for candle_hour in range(first, end_hour - 1):
            bar = history.bars.get(candle_hour)
            if bar is None:
                if recent:
                    recent.clear()
                    compression = {window: _CompressionHistory() for window in (12, 24)}
                listing_peak = None
                continue
            hour = candle_hour + 1
            age = None if listing is None else hour - listing
            if age == 1:
                listing_peak = bar.high
            elif listing_peak is not None and age is not None and age <= LISTING_MAX_HOURS:
                listing_peak = max(listing_peak, bar.high)
            prior = list(recent)
            recent.append(bar)
            ranges = {
                window: PriceRange(
                    min(row.low for row in prior[-window:]),
                    max(row.high for row in prior[-window:]),
                )
                for window in (6, 12, 24)
                if len(prior) >= window
            }
            percentiles = {}
            for window, series in compression.items():
                bounds = ranges.get(window)
                if bounds:
                    rank = series.add((bounds.high - bounds.low) / prior[-1].close)
                    if rank is not None:
                        percentiles[window] = rank
            if hour < first_decision or len(recent) < 12:
                continue
            # Young markets use only the history they could have accumulated since listing.
            turnover_window = 24 if age is None else min(age, 24)
            if len(recent) < turnover_window:
                continue
            closed = list(recent)
            active_turnover = [
                row.close * row.volume for row in closed[-turnover_window:] if row.volume > 0
            ]
            prior_volumes = [row.volume for row in prior[-24:] if row.volume > 0]
            turnover = sum(active_turnover)
            if (
                len(active_turnover) < minimum_active_hours
                or turnover < minimum_turnover
                or not active_turnover
                or median(active_turnover) < minimum_turnover / (24 * 3)
                or bar.volume <= 0
                or not prior_volumes
            ):
                continue
            atr_rows = prior[-15:]
            atr = fmean(_true_range(row, prev) for prev, row in pairwise(atr_rows))
            if atr <= 0:
                continue
            # Require a full observed turnover window for index membership. Young
            # trading signals can use a shorter window based on listing estimates.
            if basket_member and len(recent) >= 24:
                basket_members[hour].append(history)
            if hour < start_hour or coin not in trading_coins:
                continue
            returns = {
                window: log(bar.close / closed[-1 - window].close)
                for window in RETURN_WINDOWS
                if len(closed) > window
            }
            vol_rows = prior[-73:]
            hourly_returns = [log(row.close / prev.close) for prev, row in pairwise(vol_rows)]
            hourly_vol = _realized_vol(hourly_returns) if len(hourly_returns) >= 24 else None
            prior_vol = _realized_vol(hourly_returns[:-24]) if len(hourly_returns) >= 48 else 0
            vol_ratio = _realized_vol(hourly_returns[-24:]) / prior_vol if prior_vol else None
            baseline_volume = median(prior_volumes)
            frames.setdefault(hour, {})[history.coin] = SignalFrame(
                coin=history.coin,
                is_meme=history.is_meme,
                age=age,
                bar=bar,
                previous=prior[-1],
                atr=atr,
                turnover=turnover,
                volume_multiple=bar.volume / baseline_volume,
                vwap12=_vwap(closed[-12:]),
                vwap24=_vwap(closed[-24:]),
                previous_vwap12=_vwap(prior[-12:]),
                previous_vwap24=_vwap(prior[-24:]),
                prior_ranges=ranges,
                returns=returns,
                hourly_vol=hourly_vol,
                vol_ratio=vol_ratio,
                funding_bps=_recent_funding(funding, hour),
                compression_percentiles=percentiles,
                listing_peak=listing_peak,
            )
    for hour, trends in _basket_trends(basket_members, start_hour, end_hour).items():
        if hour in frames:
            frames[hour] = {
                coin: replace(frame, basket_trends=trends) for coin, frame in frames[hour].items()
            }
    return frames


def _recent_listing(frame: SignalFrame) -> bool:
    return frame.age is not None and LISTING_MIN_HOURS <= frame.age <= LISTING_MAX_HOURS


def _order(
    frame: SignalFrame,
    variant: Variant,
    side: int,
    *,
    stop: float,
    target: float | None,
    hold: int,
    ttl: int,
    offset: float = 0.25,
    price: float | None = None,
    target_price: float | None = None,
    trail: float | None = None,
) -> OrderPlan:
    close = frame.bar.close
    limit = close - side * offset * frame.atr if price is None else price
    settings = dict(variant.settings)
    stop_atr = settings.get("stop_atr", stop)
    target_r = settings.get("target_r")
    if target_r is not None:
        target = target_r * stop_atr
    if variant.mode == "ioc":
        limit = close * (1 + side * settings.get("ioc_limit_bps", IOC_LIMIT_BPS) / 10_000)
        ttl = 0
    return OrderPlan(
        coin=frame.coin,
        side=side,
        limit=limit,
        atr=frame.atr,
        stop_atr=stop_atr,
        target_atr=target,
        hold_hours=int(settings.get("hold_hours", hold)),
        ttl_hours=ttl,
        mode=variant.mode,
        trailing_atr=settings.get("trailing_atr", trail) if target_r is None else None,
        target_price=target_price,
    )


def _funding_idea(
    frame: SignalFrame, variant: Variant, settings: dict[str, float]
) -> RankedOrder | None:
    rates = frame.funding_bps
    if len(rates) != 6:
        return None
    bar, previous, atr = frame.bar, frame.previous, frame.atr
    recent = frame.prior_ranges[6]
    floor = settings["funding_bps"]
    required = settings["high_count"]
    squeeze = variant.track == "B"
    if (
        not squeeze
        and sum(rate >= floor for rate in rates) >= required
        and rates[-1] >= floor
        and fmean(rates) >= floor
        and bar.close > frame.vwap24 + 0.15 * atr
        and bar.high >= recent.high - 0.35 * atr
        and bar.close < previous.close
        and bar.high - max(bar.open, bar.close) >= 0.15 * atr
    ):
        return fmean(rates), _order(frame, variant, -1, stop=1.25, target=1.55, hold=16, ttl=3)
    if (
        sum(rate <= -floor for rate in rates) >= required
        and rates[-1] <= -floor
        and fmean(rates) <= -floor
        and recent.low <= frame.vwap12 - 0.75 * atr
        and bar.low > previous.low
        and bar.close > max(previous.high, frame.vwap12)
        and bar.close > bar.open
        and (not squeeze or frame.volume_multiple >= 1.25)
    ):
        if squeeze:
            return -fmean(rates), _order(
                frame, variant, 1, stop=1.25, target=None, hold=24, ttl=3, trail=1.8
            )
        return -fmean(rates), _order(frame, variant, 1, stop=1.25, target=1.70, hold=16, ttl=3)
    return None


def _listing_idea(
    frame: SignalFrame, variant: Variant, settings: dict[str, float]
) -> RankedOrder | None:
    if not _recent_listing(frame) or frame.listing_peak is None or len(frame.funding_bps) < 3:
        return None
    window = int(settings["window"])
    swing = frame.prior_ranges.get(window)
    if swing is None:
        return None
    bar, previous, atr = frame.bar, frame.previous, frame.atr
    vwap = frame.vwap12 if window == 6 else frame.vwap24
    previous_vwap = frame.previous_vwap12 if window == 6 else frame.previous_vwap24
    latest = frame.funding_bps[-1]
    funding = fmean(frame.funding_bps[-3:])
    crossed_vwap = previous.close >= previous_vwap and bar.close < vwap
    if (
        variant.track == "A"
        and latest > 0
        and funding >= 0.20
        and swing.high - swing.low >= 1.50 * atr
        and swing.high >= previous_vwap + 0.45 * atr
        and (crossed_vwap or bar.close < swing.low)
        and bar.close < previous.close
    ):
        return (swing.high - bar.close) / atr, _order(
            frame, variant, -1, stop=1.25, target=1.75, hold=12, ttl=3
        )
    dump = 1 - swing.low / frame.listing_peak
    if (
        variant.track == "B"
        and dump >= settings["dump_fraction"]
        and latest <= -0.50
        and funding <= -0.55
        and bar.low > previous.low
        and bar.close > swing.high
        and bar.close > vwap
    ):
        return dump, _order(frame, variant, 1, stop=1.25, target=None, hold=24, ttl=3, trail=2.0)
    return None


def _vol_scaled_return(frame: SignalFrame, window: int, baseline: float = 0.0) -> float:
    volatility = max(frame.hourly_vol or 0, 0.001)
    return (frame.returns[window] - baseline) / (volatility * sqrt(window))


def _dispersion_ideas(
    market: list[SignalFrame], variant: Variant, settings: dict[str, float]
) -> list[RankedOrder]:
    window = int(settings["window"])
    basket = [frame for frame in market if window in frame.returns and frame.atr > 0]
    if len(basket) < MINIMUM_BASKET_SIZE:
        return []
    baseline = median(frame.returns[window] for frame in basket)
    result = []
    for frame in basket:
        score = (frame.returns[window] - baseline) / (frame.atr / frame.bar.close)
        if abs(score) <= settings["multiple"]:
            continue
        side = -1 if score > 0 else 1
        bar = frame.bar
        wick = (
            bar.high - max(bar.open, bar.close)
            if side == -1
            else min(bar.open, bar.close) - bar.low
        )
        rejected = wick >= max(0.30 * (bar.high - bar.low), 0.20 * frame.atr)
        climax = frame.volume_multiple >= 2.2
        if (
            not (rejected or climax)
            or side * (bar.close - bar.open) <= 0
            or len(frame.funding_bps) < 3
            or -side * fmean(frame.funding_bps[-3:]) < 0.15
            or -side * frame.funding_bps[-1] <= 0
            or side * (frame.vwap12 - bar.close) < 0.50 * frame.atr
        ):
            continue
        order = _order(
            frame, variant, side, stop=1.15, target=None, target_price=frame.vwap12, hold=12, ttl=3
        )
        result.append((abs(score), order))
    return result


def _ranked_basket(market: list[SignalFrame], window: int) -> list[tuple[float, SignalFrame]]:
    scores = [
        (_vol_scaled_return(frame, window), frame)
        for frame in market
        if window in frame.returns and frame.hourly_vol
    ]
    return sorted(scores, key=lambda item: (item[0], item[1].coin))


def _pair_spread(ranks: list[tuple[float, SignalFrame]], legs_per_side: int) -> float:
    return fmean(score for score, _ in ranks[-legs_per_side:]) - fmean(
        score for score, _ in ranks[:legs_per_side]
    )


def _pair_idea(
    market: list[SignalFrame],
    previous: list[SignalFrame],
    hour: int,
    variant: Variant,
    settings: dict[str, float],
) -> TradePlan | None:
    window = int(settings["window"])
    interval = int(settings.get("interval", 0))
    if interval and hour % interval != 0:
        return None
    ranks = _ranked_basket(market, window)
    trigger = settings["multiple"]
    legs_per_side = int(settings.get("legs_per_side", 1))
    minimum = max(MINIMUM_BASKET_SIZE, 2 * legs_per_side)
    if len(ranks) < minimum or _pair_spread(ranks, legs_per_side) < trigger:
        return None
    if not interval:
        previous_ranks = _ranked_basket(previous, window)
        if len(previous_ranks) < minimum or _pair_spread(previous_ranks, legs_per_side) >= trigger:
            return None
    direction = int(settings.get("direction", 1))
    legs = tuple(
        _order(frame, variant, side, stop=1.8, target=None, hold=12, ttl=3)
        for side, ranks_on_side in (
            (direction, reversed(ranks[-legs_per_side:])),
            (-direction, ranks[:legs_per_side]),
        )
        for _, frame in ranks_on_side
    )
    return TradePlan(legs, variant.name) if all(leg.limit > 0 for leg in legs) else None


def _compression_idea(
    frame: SignalFrame, variant: Variant, settings: dict[str, float]
) -> RankedOrder | None:
    window = int(settings["window"])
    rank = frame.compression_percentiles.get(window)
    if rank is None or rank > settings["percentile"] or frame.volume_multiple < 2:
        return None
    bounds = frame.prior_ranges[window]
    bar, atr = frame.bar, frame.atr
    if bar.close >= bounds.high + 0.10 * atr and bar.close > bar.open:
        side, breakout = 1, bounds.high
    elif bar.close <= bounds.low - 0.10 * atr and bar.close < bar.open:
        side, breakout = -1, bounds.low
    else:
        return None
    return frame.volume_multiple * (1 - rank), _order(
        frame,
        variant,
        side,
        stop=1.35,
        target=None,
        hold=24,
        ttl=2 if window == 12 else 4,
        price=breakout,
        trail=2.0,
    )


def _regime_ideas(
    market: list[SignalFrame], variant: Variant, settings: dict[str, float]
) -> list[RankedOrder]:
    regime = [frame for frame in market if 24 in frame.returns and frame.vol_ratio is not None]
    if len(regime) < MINIMUM_BASKET_SIZE or median(frame.vol_ratio for frame in regime) > 1.80:
        return []
    up = sum(frame.bar.close > frame.vwap24 and frame.returns[24] > 0 for frame in regime)
    down = sum(frame.bar.close < frame.vwap24 and frame.returns[24] < 0 for frame in regime)
    breadth = settings["breadth"] * len(regime)
    if up >= breadth:
        side = 1
    elif down >= breadth:
        side = -1
    else:
        return []
    ranks = _ranked_basket(regime, int(settings["window"]))
    if len(ranks) < MINIMUM_BASKET_SIZE:
        return []
    count = max(1, len(ranks) // 3)
    leaders = ranks[-count:] if side == 1 else ranks[:count]
    result = []
    for strength, frame in leaders:
        if side * strength < 0.75:
            continue
        atr, price = frame.atr, frame.bar.close
        swing = frame.prior_ranges[6]
        extreme, support = (swing.high, swing.low) if side == 1 else (swing.low, swing.high)
        distance = side * (price - frame.vwap24)
        if distance < -0.15 * atr or side * (extreme - price) < 0.40 * atr:
            continue
        anchors = [
            value
            for value in (frame.vwap12, support)
            if 0.10 * atr <= side * (price - value) <= 1.15 * atr
        ]
        if not anchors:
            continue
        limit = min(anchors, key=lambda value: abs(price - value))
        order = _order(frame, variant, side, stop=1.35, target=2.8, hold=24, ttl=4, price=limit)
        result.append((side * strength, order))
    return result


def ideas(
    frames: Mapping[int, Mapping[str, SignalFrame]], variant: Variant
) -> dict[int, list[TradePlan]]:
    """Return ranked, deterministic order plans for each completed decision hour."""
    settings = dict(variant.settings)
    single_market = {
        "funding": _funding_idea,
        "listing": _listing_idea,
        "compression": _compression_idea,
    }
    basket_market = {"dispersion": _dispersion_ideas, "regime": _regime_ideas}
    output = {}
    for hour in sorted(frames):
        rows = frames[hour].values()
        if variant.family == "pair":
            plan = _pair_idea(
                [frame for frame in rows if frame.is_meme],
                [frame for frame in frames.get(hour - 1, {}).values() if frame.is_meme],
                hour,
                variant,
                settings,
            )
            if plan:
                output[hour] = [plan]
            continue
        if variant.family in basket_market:
            ranked = basket_market[variant.family](
                [frame for frame in rows if frame.is_meme], variant, settings
            )
        else:
            rule = single_market[variant.family]
            ranked = [
                signal
                for frame in rows
                if (frame.is_meme or _recent_listing(frame))
                and (signal := rule(frame, variant, settings)) is not None
            ]
            if variant.family == "funding" and "basket_breadth" in settings:
                returns = [
                    frame.returns[24] for frame in rows if frame.is_meme and 24 in frame.returns
                ]
                ranked = [
                    item
                    for item in ranked
                    if len(returns) >= MINIMUM_BASKET_SIZE
                    and sum(change * item[1].side > 0 for change in returns) / len(returns)
                    >= settings["basket_breadth"]
                ]
        if settings.get("basket_trend"):
            ranked = [
                (score, leg)
                for score, leg in ranked
                if all(
                    leg.side * frames[hour][leg.coin].basket_trends.get(window, 0) > 0
                    for window in BASKET_TREND_WINDOWS
                )
            ]
        if ranked:
            plans = [
                TradePlan((leg,), variant.name)
                for _, leg in sorted(ranked, key=lambda item: (-item[0], item[1].coin))
                if leg.limit > 0
            ]
            if plans:
                output[hour] = plans
    return output
