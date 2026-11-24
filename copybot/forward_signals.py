"""Public, as-of-midnight market loading for the fixed forward Donchian study."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, DecimalException
from statistics import median
from time import monotonic, sleep

from httpx import HTTPStatusError, TransportError

from .perps import now_ms, number
from .research_models import DAY_HOURS, HOUR_MS, OrderPlan, excluded_symbol

DAY_MS = DAY_HOURS * HOUR_MS
MAX_LAG_MS = 15 * 60 * 1000
MAX_CENSUS_AGE_MS = HOUR_MS
TOP_MARKETS = 20
RANK_HOURS = 30 * DAY_HOURS
SMA_DAYS = 50
ENTRY_DAYS = 20
EXIT_DAYS = 10
ATR_DAYS = 14
MINIMUM_TURNOVER = Decimal(1_000_000)
MINIMUM_ACTIVE_HOURS = 9
ENTRY_CAP = Decimal("0.004")
HOLD_HOURS = 10 * DAY_HOURS


class ForwardDataError(ValueError):
    """The public observations do not establish the requested forward state."""


@dataclass(frozen=True, slots=True)
class MarketMeta:
    coin: str
    decimals: int | None
    max_leverage: int | None
    active: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class UniverseSnapshot:
    observed_ms: int
    markets: Mapping[str, MarketMeta]


@dataclass(frozen=True, slots=True)
class ForwardDecision:
    market: MarketMeta
    rank: int | None
    turnover_30d: Decimal | None = None
    turnover_24h: Decimal | None = None
    close: Decimal | None = None
    atr: Decimal | None = None
    sma50: Decimal | None = None
    entry_high: Decimal | None = None
    entry_low: Decimal | None = None
    exit_long: bool | None = None
    exit_short: bool | None = None
    plan: OrderPlan | None = None
    status: str = "unavailable"


@dataclass(frozen=True, slots=True)
class ForwardSnapshot:
    decision_hour: int
    started_ms: int
    finished_ms: int
    roster: tuple[str, ...]
    decisions: Mapping[str, ForwardDecision]
    complete: bool
    problems: Mapping[str, str]
    exclusions: Mapping[str, str]
    census_basis: str


@dataclass(frozen=True, slots=True)
class _Bar:
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def _native(coin):
    return bool(coin) and ":" not in coin and not excluded_symbol(coin)


def _positive_context(context, field):
    try:
        return context[field] is not None and number(context[field]) > 0
    except (KeyError, DecimalException, ValueError) as exc:
        raise ForwardDataError(f"Invalid market context {field}") from exc


def _market_rows(raw):
    if not isinstance(raw, list) or len(raw) != 2 or not isinstance(raw[0], dict):
        raise ForwardDataError("Expected native market metadata and contexts")
    markets, contexts = raw[0].get("universe"), raw[1]
    if not isinstance(markets, list) or not isinstance(contexts, list) or not markets:
        raise ForwardDataError("Missing native market census")
    if len(markets) != len(contexts):
        raise ForwardDataError("Native market metadata and contexts do not align")
    result = {}
    for asset, context in zip(markets, contexts, strict=True):
        if (
            not isinstance(asset, dict)
            or not isinstance(asset.get("name"), str)
            or not asset["name"]
        ):
            raise ForwardDataError("Invalid native market identity")
        coin = asset["name"]
        if not _native(coin):
            continue
        if coin in result:
            raise ForwardDataError(f"Duplicate native market {coin}")
        decimals, leverage = asset.get("szDecimals"), asset.get("maxLeverage")
        delisted = asset.get("isDelisted", False)
        if type(delisted) is not bool:
            raise ForwardDataError(f"Invalid delisting flag for {coin}")
        if type(decimals) is not int or not 0 <= decimals <= 6:
            raise ForwardDataError(f"Invalid size precision for {coin}")
        if type(leverage) is not int or leverage <= 0:
            raise ForwardDataError(f"Invalid leverage cap for {coin}")
        if delisted:
            result[coin] = MarketMeta(coin, decimals, leverage, False, "delisted")
            continue
        if not isinstance(context, dict):
            raise ForwardDataError(f"Invalid native context for {coin}")
        mark = _positive_context(context, "markPx")
        mid = _positive_context(context, "midPx")
        active = mark and mid
        result[coin] = MarketMeta(
            coin, decimals, leverage, active, None if active else "no_valid_mark_or_mid"
        )
    return result


def _closed_bars(raw, coin, interval, first, boundary):
    if not isinstance(raw, list):
        raise ForwardDataError(f"Invalid {interval} candle response for {coin}")
    width = HOUR_MS if interval == "1h" else DAY_MS
    bars = {}
    for row in raw:
        if not isinstance(row, dict) or type(row.get("t")) is not int:
            raise ForwardDataError(f"Invalid {interval} candle timestamp for {coin}")
        hour = row["t"]
        if not first <= hour < boundary:
            continue
        try:
            if hour % width or row["s"] != coin or row["i"] != interval:
                raise ValueError("unexpected candle identity or interval alignment")
            if type(row["T"]) is not int or row["T"] != hour + width - 1 or row["T"] >= boundary:
                raise ValueError("unexpected close timestamp")
            opening, high, low, closing, volume = map(
                number, (row[k] for k in ("o", "h", "l", "c", "v"))
            )
            if not 0 < low <= min(opening, closing) <= max(opening, closing) <= high or volume < 0:
                raise ValueError("invalid OHLCV")
            candle = _Bar(high, low, closing, volume)
            if hour in bars:
                raise ValueError("duplicate candle")
            bars[hour] = candle
        except (DecimalException, KeyError, ValueError) as exc:
            raise ForwardDataError(f"Invalid {interval} candle for {coin}: {exc}") from exc
    return bars


def _is_complete(bars, first, end, width=HOUR_MS):
    return len(bars) == (end - first) // width and all(
        stamp in bars for stamp in range(first, end, width)
    )


def _liquidity(bars, boundary):
    last = [bars[stamp] for stamp in range(boundary - DAY_MS, boundary, HOUR_MS)]
    notional = [bar.close * bar.volume for bar in last]
    active = [value for value in notional if value > 0]
    volume = sum(notional)
    eligible = (
        notional[-1] > 0
        and volume >= MINIMUM_TURNOVER
        and len(active) >= MINIMUM_ACTIVE_HOURS
        and median(active) >= MINIMUM_TURNOVER / 72
    )
    return volume, eligible


def _fails_recent_liquidity(bars, boundary):
    latest = bars.get(boundary - HOUR_MS)
    if latest is not None and not latest.volume:
        return True
    if all(stamp in bars for stamp in range(boundary - DAY_MS, boundary, HOUR_MS)):
        return not _liquidity(bars, boundary)[1]
    return False


def _daily(bars, boundary):
    result = []
    for day in range(boundary - (SMA_DAYS + 1) * DAY_MS, boundary, DAY_MS):
        hours = [bars.get(stamp) for stamp in range(day, day + DAY_MS, HOUR_MS)]
        if any(bar is None for bar in hours):
            result.append(None)
            continue
        result.append(
            _Bar(
                max(bar.high for bar in hours),
                min(bar.low for bar in hours),
                hours[-1].close,
                sum(bar.volume for bar in hours),
            )
        )
    return result


def _strategy(decision, hours, boundary, entry_allowed):
    days = _daily(hours, boundary)
    if days[-1] is None or hours[boundary - HOUR_MS].volume <= 0:
        return replace(decision, status="missing_fresh_daily_close")
    close = days[-1].close
    previous_exit = days[-EXIT_DAYS - 1 : -1]
    has_exit = all(day is not None and day.volume > 0 for day in previous_exit)
    decision = replace(
        decision,
        close=close,
        exit_long=close < min(day.low for day in previous_exit) if has_exit else None,
        exit_short=close > max(day.high for day in previous_exit) if has_exit else None,
    )
    previous_entry = days[-ENTRY_DAYS - 1 : -1]
    if all(day is not None for day in previous_entry):
        decision = replace(
            decision,
            entry_high=max(day.high for day in previous_entry),
            entry_low=min(day.low for day in previous_entry),
        )
    risk = days[-ATR_DAYS - 1 :]
    if all(day is not None for day in risk):
        atr = (
            sum(
                max(day.high - day.low, abs(day.high - prior.close), abs(day.low - prior.close))
                for prior, day in zip(risk[:-1], risk[1:], strict=True)
            )
            / ATR_DAYS
        )
        decision = replace(decision, atr=atr)
    prior = days[:-1]
    if not all(day is not None and day.volume > 0 for day in prior):
        return replace(decision, status="insufficient_50_day_history")
    sma = sum(day.close for day in prior) / SMA_DAYS
    decision = replace(decision, sma50=sma, status="ready")
    if not entry_allowed or decision.atr is None or decision.atr <= 0:
        return decision
    if close > decision.entry_high and close > sma:
        side, excess = 1, close - decision.entry_high
    elif close < decision.entry_low and close < sma:
        side, excess = -1, decision.entry_low - close
    else:
        return decision
    plan = OrderPlan(
        coin=decision.market.coin,
        side=side,
        limit=float(close * (1 + side * ENTRY_CAP)),
        atr=float(decision.atr),
        stop_atr=2,
        target_atr=None,
        hold_hours=HOLD_HOURS,
        ttl_hours=0,
        mode="ioc",
        signal_strength=float(excess / decision.atr),
    )
    return replace(decision, plan=plan)


class SignalLoader:
    """Load the full eligible native market universe within a shared REST budget."""

    def __init__(
        self,
        info: Callable,
        *,
        rate_weight_per_minute: int = 900,
        now_ms: Callable[[], int] = now_ms,
        monotonic: Callable[[], float] = monotonic,
        sleep: Callable[[float], None] = sleep,
    ):
        if type(rate_weight_per_minute) is not int or not 60 <= rate_weight_per_minute <= 1100:
            raise ValueError(
                "Use a REST budget that leaves capacity below the 1,200/minute venue limit"
            )
        self.info = info
        self.rate = rate_weight_per_minute / 60
        self.now_ms, self.monotonic, self.sleep = now_ms, monotonic, sleep
        self.available = monotonic()

    def _request(self, kind, weight, deadline=None, **payload):
        delay = max(0, self.available - self.monotonic())
        if deadline is not None and self.now_ms() + delay * 1000 > deadline:
            raise ForwardDataError("Forward decision deadline exceeded")
        if delay:
            self.sleep(delay)
        self.available = self.monotonic() + weight / self.rate
        try:
            raw = self.info(kind, **payload)
        except (HTTPStatusError, TransportError, TimeoutError) as exc:
            if isinstance(exc, HTTPStatusError) and exc.response.status_code == 429:
                self.available = max(self.available, self.monotonic() + 61)
            raise ForwardDataError(f"Public {kind} request failed: {exc}") from exc
        if deadline is not None and self.now_ms() > deadline:
            raise ForwardDataError("Forward decision deadline exceeded")
        return raw

    def capture_universe(self, *, deadline=None):
        raw = self._request("metaAndAssetCtxs", 20, deadline)
        return UniverseSnapshot(self.now_ms(), _market_rows(raw))

    def _candles(self, coin, first, boundary, deadline, interval="1h"):
        width = HOUR_MS if interval == "1h" else DAY_MS
        count = (boundary - first) // width
        raw = self._request(
            "candleSnapshot",
            20 + (count + 59) // 60,
            deadline,
            req={"coin": coin, "interval": interval, "startTime": first, "endTime": boundary - 1},
        )
        return _closed_bars(raw, coin, interval, first, boundary)

    def _listed_late(self, coin, hours, first, boundary, deadline):
        if not hours or min(hours) <= first or not _is_complete(hours, min(hours), boundary):
            return False
        day_boundary = min(hours) // DAY_MS * DAY_MS
        return self._daily_listing_matches(coin, day_boundary, boundary, deadline)

    def _daily_listing_matches(self, coin, expected_first, boundary, deadline):
        first = boundary - (SMA_DAYS + 2) * DAY_MS
        days = self._candles(coin, first, boundary, deadline, "1d")
        return bool(
            days
            and min(days) == expected_first > first
            and _is_complete(days, expected_first, boundary, DAY_MS)
        )

    def _retry_ranking_gap(self, coin, hours, first, boundary, deadline):
        missing = [stamp for stamp in range(first, boundary, HOUR_MS) if stamp not in hours]
        if missing[-1] - missing[0] >= DAY_MS:
            return hours
        retry = self._candles(coin, missing[0], missing[-1] + HOUR_MS, deadline)
        if any(hours[stamp] != retry[stamp] for stamp in hours.keys() & retry.keys()):
            raise ForwardDataError("Conflicting candles during ranking history retry")
        return {**hours, **retry}

    def load(
        self,
        decision_hour: int,
        *,
        open_symbols: Iterable[str] = (),
        prior_universe: UniverseSnapshot | None = None,
    ) -> ForwardSnapshot:
        started = self.now_ms()
        if (
            type(decision_hour) is not int
            or decision_hour <= (SMA_DAYS + 2) * DAY_HOURS
            or decision_hour % DAY_HOURS
        ):
            raise ForwardDataError("Expected a midnight UTC epoch hour for the forward decision")
        boundary = decision_hour * HOUR_MS
        deadline = boundary + MAX_LAG_MS
        if not boundary <= started <= deadline:
            raise ForwardDataError(
                "Decision must start within 15 minutes after the live UTC boundary"
            )
        if (
            prior_universe is not None
            and not boundary - MAX_CENSUS_AGE_MS <= prior_universe.observed_ms < boundary
        ):
            raise ForwardDataError(
                "The preceding roster must be captured within one hour before the boundary"
            )
        opens = set(open_symbols)
        if any(not isinstance(coin, str) or not _native(coin) for coin in opens):
            raise ForwardDataError("Open symbols must be native non-BTC markets")
        current = self.capture_universe(deadline=deadline).markets
        before = current if prior_universe is None else prior_universe.markets
        census_basis = (
            "first_post_boundary_snapshot" if prior_universe is None else "pre_boundary_snapshot"
        )
        problems, exclusions, shortlist, cached, turnover = {}, {}, {}, {}, {}
        first_rank = boundary - RANK_HOURS * HOUR_MS
        first_sma = boundary - (SMA_DAYS + 1) * DAY_MS
        for coin in sorted(set(before) | set(current)):
            live, prior = current.get(coin), before.get(coin)
            if live is None or not live.active:
                exclusions[coin] = live.reason if live else "absent_from_current_census"
            elif prior is None or not prior.active:
                exclusions[coin] = "not_active_in_pre_boundary_census"
            else:
                try:
                    hours = self._candles(coin, first_rank, boundary, deadline)
                    if _fails_recent_liquidity(hours, boundary):
                        exclusions[coin] = "fails_closed_24h_liquidity"
                        if coin in opens:
                            cached[coin] = hours
                        continue
                    if not _is_complete(hours, first_rank, boundary):
                        if self._listed_late(coin, hours, first_rank, boundary, deadline):
                            exclusions[coin] = "new_listing_with_less_than_30_days"
                            continue
                        hours = self._retry_ranking_gap(coin, hours, first_rank, boundary, deadline)
                        if not _is_complete(hours, first_rank, boundary):
                            raise ForwardDataError("Incomplete 30-day hourly ranking history")
                    day_volume, eligible = _liquidity(hours, boundary)
                    turnover[coin] = (
                        sum(bar.close * bar.volume for bar in hours.values()),
                        day_volume,
                    )
                    if coin in opens:
                        cached[coin] = hours
                    if not eligible:
                        exclusions[coin] = "fails_closed_24h_liquidity"
                        continue
                    shortlist[coin] = hours
                    if len(shortlist) > TOP_MARKETS:
                        last = max(shortlist, key=lambda symbol: (-turnover[symbol][0], symbol))
                        del shortlist[last]
                except ForwardDataError as error:
                    problems[coin] = str(error)
        roster = tuple(sorted(shortlist, key=lambda coin: (-turnover[coin][0], coin)))
        ranks = {coin: place for place, coin in enumerate(roster, 1)}
        decisions = {}
        for coin in (*roster, *sorted(opens - ranks.keys())):
            meta = (
                current.get(coin)
                or before.get(coin)
                or MarketMeta(coin, None, None, False, "missing_metadata")
            )
            if coin not in current:
                meta = replace(meta, active=False, reason="absent_from_current_census")
            pair_volume = turnover.get(coin, (None, None))
            decision = ForwardDecision(meta, ranks.get(coin), *pair_volume)
            try:
                recent = shortlist.get(coin) or cached.get(coin)
                if recent is None:
                    recent = self._candles(coin, first_rank, boundary, deadline)
                older = self._candles(coin, first_sma, first_rank, deadline)
                history = {**older, **recent}
                decision = _strategy(decision, history, boundary, coin in ranks and meta.active)
                if coin in opens and decision.exit_long is None:
                    raise ForwardDataError(
                        "Open market is missing a verified channel exit observation"
                    )
                if coin in ranks and decision.status != "ready":
                    if (
                        not history
                        or not _is_complete(history, min(history), boundary)
                        or not self._daily_listing_matches(
                            coin, min(history) // DAY_MS * DAY_MS, boundary, deadline
                        )
                    ):
                        raise ForwardDataError("Incomplete 51-day hourly strategy history")
            except ForwardDataError as error:
                problems[coin] = str(error)
                decision = replace(decision, plan=None)
            decisions[coin] = decision

        if decisions and not problems:
            try:
                latest = self.capture_universe(deadline=deadline).markets
            except ForwardDataError as error:
                problems["final_census"] = str(error)
            else:
                for coin, row in decisions.items():
                    market = latest.get(coin) or replace(
                        row.market, active=False, reason="absent_from_final_census"
                    )
                    if not market.active:
                        exclusions[coin] = market.reason
                        decisions[coin] = replace(row, market=market, plan=None, status="inactive")
                    else:
                        decisions[coin] = replace(row, market=market)
        finished = self.now_ms()
        if finished > deadline:
            problems["clock"] = "Forward decision deadline exceeded"
        if problems:
            decisions = {coin: replace(row, plan=None) for coin, row in decisions.items()}
        return ForwardSnapshot(
            decision_hour,
            started,
            finished,
            roster,
            decisions,
            not problems,
            problems,
            exclusions,
            census_basis,
        )
