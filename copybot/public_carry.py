"""Offline replay of a pinned public two-venue perpetual funding strategy."""

import argparse
import csv
import io
import json
import math
import statistics
import subprocess
import zipfile
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlencode

from .meme_study import day_hour, hour_date, qualification, write_json
from .research_data import load_archive
from .research_models import HOUR_MS, MarketHistory, excluded_symbol

REFERENCE_SHA = "dd5a59eca1bfb6f2184306402d6ac19f4832c85a"
REFERENCE = (
    f"https://github.com/hummingbot/hummingbot/blob/{REFERENCE_SHA}/scripts/v2_funding_rate_arb.py"
)
BINANCE_ARCHIVE = "https://data.binance.vision/data/futures/um/monthly"
BINANCE_API = "https://fapi.binance.com/fapi/v1"
MAX_END = date(2026, 11, 21)
REST_MONTH = date(2026, 11, 1)
LEG_BALANCE_FRACTION = 0.20
PAIR_CAP = 2
FUNDING_THRESHOLD = 0.001
FUNDING_REVERSAL = -0.001
TAKE_PROFIT_FRACTION = 0.01
BASE_FEES = (0.00045, 0.00050)
SLIPPAGE = {"base": (0.0005, 0.0003), "stress": (0.0028, 0.0015)}
MULTIPLIER_CONTRACTS = {"kBONK", "kFLOKI", "kPEPE", "kSHIB"}
NEIRO_LISTING = (
    "https://www.binance.com/en/support/announcement/detail/7fc53c11624b4d788fa97521a3480cda"
)
VALID_INTERVALS = {1, 2, 4, 8}


@dataclass(frozen=True, slots=True)
class Price:
    open: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class Settlement:
    time_ms: int
    hour: int
    rate: float
    interval: int | None
    mark: float | None = None


@dataclass(frozen=True, slots=True)
class Venue:
    symbol: str
    price_multiplier: int


@dataclass(frozen=True, slots=True)
class Variant:
    name: str
    actual_interval: bool = False
    require_entry_basis: bool = False


VARIANTS = (
    Variant("hummingbot_default"),
    Variant("hummingbot_optional_entry_basis", require_entry_basis=True),
    Variant("hummingbot_actual_settlement_interval", actual_interval=True),
)


def binance_venue(coin):
    if excluded_symbol(coin):
        raise ValueError(f"Excluded market {coin}")
    if coin == "kNEIRO":
        return Venue("NEIROUSDT", 1000)
    if coin in MULTIPLIER_CONTRACTS:
        return Venue(f"1000{coin[1:]}USDT", 1)
    if not coin.isascii() or not coin.isalnum() or coin.upper() != coin:
        raise ValueError(f"No verified Binance contract unit for {coin}")
    return Venue(f"{coin}USDT", 1)


def fetch(cache, filename, url):
    path = cache / filename
    missing = cache / f"{filename}.missing"
    if path.exists():
        return path.read_bytes()
    if missing.exists():
        return None
    req = subprocess.run(
        [
            "curl",
            "-sSL",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--max-time",
            "20",
            "--retry",
            "2",
            "-w",
            "\n%{http_code}",
            url,
        ],
        capture_output=True,
        check=True,
    )
    body, status = req.stdout.rsplit(b"\n", maxsplit=1)
    if status == b"404" or (status == b"400" and json.loads(body).get("code") == -1121):
        missing.write_text(url + "\n", encoding="utf-8")
        return None
    if status != b"200":
        raise ValueError(f"Public market-data request returned HTTP {status.decode()} for {url}")
    path.write_bytes(body)
    return body


def month_range(start, end):
    current = date(start.year, start.month, 1)
    while current < end:
        following = date(
            current.year + int(current.month == 12),
            1 if current.month == 12 else current.month + 1,
            1,
        )
        yield current, following
        current = following


def monthly_rows(raw):
    if raw is None:
        return []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        if len(names) != 1 or not names[0].endswith(".csv"):
            raise ValueError("Expected a Binance monthly ZIP with one CSV")
        row_text = archive.read(names[0]).decode("utf-8-sig")
    rows = [row for row in csv.reader(row_text.splitlines()) if row]
    if rows and rows[0][0] in {"open_time", "calc_time"}:
        rows.pop(0)
    if any(not row[0].isdigit() for row in rows):
        raise ValueError("Malformed Binance monthly CSV timestamp")
    return rows


def settlement(stamp, rate, interval, mark=None):
    stamp, rate = int(stamp), float(rate)
    scheduled = (stamp + HOUR_MS // 2) // HOUR_MS
    if (
        abs(stamp - scheduled * HOUR_MS) > 5 * 60 * 1000
        or not math.isfinite(rate)
        or abs(rate) > 0.10
    ):
        raise ValueError(f"Invalid settlement at {stamp}: {rate}")
    if interval is not None and interval not in VALID_INTERVALS:
        raise ValueError(f"Unsupported {interval}-hour interval at {stamp}")
    if mark is not None and (not math.isfinite(mark) or mark <= 0):
        raise ValueError(f"Invalid settlement mark at {stamp}: {mark}")
    return Settlement(stamp, scheduled, rate, interval, mark)


def prices_from_rows(rows, venue):
    result = {}
    for row in rows:
        stamp, close_stamp = int(row[0]), int(row[6])
        hour, remainder = divmod(stamp, HOUR_MS)
        open_px, close_px, volume = map(float, (row[1], row[4], row[5]))
        if remainder or close_stamp != stamp + HOUR_MS - 1 or hour in result:
            raise ValueError(f"Invalid {venue.symbol} hour at {stamp}")
        if (
            not all(map(math.isfinite, (open_px, close_px, volume)))
            or min(open_px, close_px) <= 0
            or volume < 0
        ):
            raise ValueError(f"Invalid {venue.symbol} bar at {stamp}")
        result[hour] = Price(
            open_px * venue.price_multiplier, close_px * venue.price_multiplier, volume
        )
    return result


def load_binance(venue, cache, start, end):
    if end > MAX_END:
        raise ValueError("External archive cutoff exceeded")
    symbol = venue.symbol
    prices, events = {}, []
    missing_months = defaultdict(list)
    for month, following in month_range(start, end):
        if month < REST_MONTH:
            name = f"{symbol}-1h-{month:%Y-%m}.zip"
            bars = monthly_rows(fetch(cache, name, f"{BINANCE_ARCHIVE}/klines/{symbol}/1h/{name}"))
            name = f"{symbol}-fundingRate-{month:%Y-%m}.zip"
            funding = monthly_rows(
                fetch(cache, name, f"{BINANCE_ARCHIVE}/fundingRate/{symbol}/{name}")
            )
            events.extend(settlement(row[0], row[2], int(row[1])) for row in funding)
        else:
            first, last = day_hour(month) * HOUR_MS, day_hour(min(following, end)) * HOUR_MS - 1
            query = {"symbol": symbol, "startTime": first, "endTime": last, "limit": 1000}
            name = f"{symbol}-1h-{month:%Y-%m}-{end}.json"
            bar_query = {**query, "interval": "1h"}
            raw = fetch(cache, name, f"{BINANCE_API}/klines?{urlencode(bar_query)}")
            bars = json.loads(raw) if raw is not None else []
            name = f"{symbol}-fundingRate-{month:%Y-%m}-{end}.json"
            raw = fetch(cache, name, f"{BINANCE_API}/fundingRate?{urlencode(query)}")
            funding = json.loads(raw) if raw is not None else []
            if len(bars) >= 1000 or len(funding) >= 1000:
                raise ValueError(f"Unexpected API pagination limit for {symbol}")
            for row in funding:
                if row["symbol"] != symbol or not first <= int(row["fundingTime"]) <= last:
                    raise ValueError(f"Unexpected API settlement for {symbol}")
                event = settlement(
                    row["fundingTime"],
                    row["fundingRate"],
                    None,
                    float(row["markPrice"]) * venue.price_multiplier,
                )
                period = event.hour - events[-1].hour if events else None
                interval = period if period in VALID_INTERVALS else None
                events.append(
                    Settlement(event.time_ms, event.hour, event.rate, interval, event.mark)
                )
        if not bars:
            missing_months["prices"].append(month.isoformat())
        if not funding:
            missing_months["funding"].append(month.isoformat())
        prices.update(prices_from_rows(bars, venue))
    cutoff = day_hour(end)
    return (
        {hour: bar for hour, bar in prices.items() if hour < cutoff},
        [event for event in events if event.time_ms < cutoff * HOUR_MS],
        dict(missing_months),
    )


@dataclass(slots=True)
class FundingHistory:
    rows: list[Settlement]
    stamps: list[int]
    hours: list[int]
    accumulated: list[float]
    missing_price_hours: list[int]

    @classmethod
    def build(cls, events, prices):
        rows = sorted(events, key=lambda event: event.time_ms)
        if len({row.hour for row in rows}) != len(rows):
            raise ValueError("Overlapping settlement archive records")
        accumulated = [0.0]
        missing_prices = []
        for row in rows:
            bar = prices.get(row.hour - 1)
            ref = row.mark if row.mark is not None else (bar.close if bar else None)
            if ref is None:
                missing_prices.append(row.hour)
            accumulated.append(accumulated[-1] + row.rate * (ref or 0.0))
        return cls(
            rows,
            [row.time_ms for row in rows],
            [row.hour for row in rows],
            accumulated,
            missing_prices,
        )

    def last(self, hour):
        index = bisect_left(self.stamps, hour * HOUR_MS) - 1
        if index < 0:
            return None
        event = self.rows[index]
        max_age = event.interval or max(VALID_INTERVALS)
        return event if hour - event.hour <= max_age else None

    def accrued(self, start, end):
        left, right = bisect_right(self.hours, start), bisect_left(self.hours, end)
        if bisect_right(self.missing_price_hours, start) < bisect_left(
            self.missing_price_hours, end
        ):
            return None
        return self.accumulated[right] - self.accumulated[left] if right > left else 0.0


@dataclass(slots=True)
class Pair:
    coin: str
    venue: Venue
    native: MarketHistory
    binance: dict[int, Price]
    native_funding: FundingHistory
    binance_funding: FundingHistory


def build_pair(history, venue, prices, events):
    native_events = [settlement(row.time_ms, row.rate, 1) for row in history.funding.values()]
    return Pair(
        history.coin,
        venue,
        history,
        prices,
        FundingHistory.build(native_events, history.bars),
        FundingHistory.build(events, prices),
    )


def archive_pairs(native, cache, start, end):
    def load(history):
        venue = binance_venue(history.coin)
        warm = max(min(history.bars) - 24, day_hour(start) - 48)
        first = datetime.fromtimestamp(warm * 3600, UTC).date()
        last = min(end, datetime.fromtimestamp((max(history.bars) + 24) * 3600, UTC).date())
        prices, events, missing = load_binance(venue, cache, first, last)
        pair = build_pair(history, venue, prices, events)
        common = sorted(
            hour
            for hour in history.bars.keys() & prices.keys()
            if day_hour(start) <= hour < day_hour(end)
        )
        paired = [
            hour
            for hour in common
            if (native := history.bars.get(hour - 1))
            and (other := prices.get(hour - 1))
            and native.volume > 0
            and other.volume > 0
        ]
        comparable = [
            hour
            for hour in paired
            if 0.90 <= history.bars[hour - 1].close / prices[hour - 1].close <= 1.10
        ]
        readable = [
            hour
            for hour in comparable
            if pair.native_funding.last(hour) and pair.binance_funding.last(hour)
        ]
        info = {
            "binance_symbol": venue.symbol,
            "price_multiplier": venue.price_multiplier,
            "native_hours": len(history.bars),
            "binance_hours": len(prices),
            "binance_settlements": len(events),
            "joint_hourly_opens": len(common),
            "with_prior_liquidity": len(paired),
            "with_comparable_prior_prices": len(comparable),
            "fully_eligible_hours": len(readable),
            "eligible_first_hour": readable[0] if readable else None,
            "eligible_last_hour": readable[-1] if readable else None,
            "funding_intervals": dict(Counter(str(row.interval) for row in events)),
            "missing_months": missing,
        }
        if comparable:
            info["median_normalized_price_ratio"] = statistics.median(
                history.bars[hour - 1].close / prices[hour - 1].close for hour in comparable
            )
        return pair if readable else None, info

    coverage, pairs = {}, {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = {coin: pool.submit(load, history) for coin, history in sorted(native.items())}
        for coin, task in jobs.items():
            pair, coverage[coin] = task.result()
            if pair is not None:
                pairs[coin] = pair
    return pairs, coverage


@dataclass(frozen=True, slots=True)
class Quote:
    reference: tuple[float, float]
    opening: tuple[float, float]
    advantage: float | None


def quote_at(pair, hour, variant):
    prior = (pair.native.bars.get(hour - 1), pair.binance.get(hour - 1))
    current = (pair.native.bars.get(hour), pair.binance.get(hour))
    if any(bar is None for bar in (*prior, *current)):
        return None
    if any(bar.volume <= 0 for bar in prior):
        return None
    hl, bn = pair.native_funding.last(hour), pair.binance_funding.last(hour)
    interval = bn.interval if bn and variant.actual_interval else 8
    advantage = (bn.rate / interval - hl.rate) * 24 if hl and bn and interval else None
    return Quote(tuple(bar.close for bar in prior), tuple(bar.open for bar in current), advantage)


@dataclass(slots=True)
class Position:
    entered: int
    coin: str
    side: int
    quantity: float
    price: tuple[float, float]
    entry_advantage: float
    reserved: tuple[float, float]
    reference_quote: float
    first_gap: tuple[int, str] | None = None

    def note_gap(self, hour, reason):
        if self.first_gap is None:
            self.first_gap = (hour, reason)


def costs(entry, exit_, side, quantity, slippage, fee_rate):
    bought = entry * (1 + side * slippage)
    sold = exit_ * (1 - side * slippage)
    fee = fee_rate * quantity * (bought + sold)
    slip = quantity * slippage * (entry + exit_)
    return fee, slip


def entry_candidate(pair, quote, capital, variant, slippage):
    if quote.advantage is None or abs(quote.advantage) < FUNDING_THRESHOLD:
        return None, "below_funding_or_stale"
    native, binance = quote.reference
    if not 0.90 <= native / binance <= 1.10:
        return None, "not_price_comparable"
    side = 1 if quote.advantage > 0 else -1
    buy, sell = (0, 1) if side == 1 else (1, 0)
    buyer = quote.reference[buy] * (1 + slippage[buy])
    seller = quote.reference[sell] * (1 - slippage[sell])
    if variant.require_entry_basis and (seller - buyer) / buyer < sum(BASE_FEES):
        return None, "negative_entry_basis"
    # Hyperliquid is the reference leg for Hummingbot's common unit quantity.
    scale = 10**pair.native.decimals
    target_quote = LEG_BALANCE_FRACTION * sum(max(0, balance) for balance in capital)
    max_quantity = min(
        balance / (px * (1 + fee + slip))
        for balance, px, fee, slip in zip(capital, quote.opening, BASE_FEES, slippage, strict=True)
    )
    quantity = math.floor(max(0.0, min(target_quote / native, max_quantity)) * scale) / scale
    notionals = tuple(quantity * px for px in quote.opening)
    if min(notionals) < 10:
        return None, "below_modeled_minimum_notional"
    reserved = tuple(
        value * (1 + fee + slip)
        for value, fee, slip in zip(notionals, BASE_FEES, slippage, strict=True)
    )
    if any(need > have for need, have in zip(reserved, capital, strict=True)):
        return None, "insufficient_venue_collateral"
    return (side, quantity, reserved, target_quote), None


def valued_trade(pair, position, hour, prices, slippage, reason):
    funding = tuple(
        history.accrued(position.entered, hour)
        for history in (pair.native_funding, pair.binance_funding)
    )
    if None in funding:
        return None
    legs = []
    for i, name in enumerate(("hyperliquid", "binance_usdm")):
        side = position.side if i == 0 else -position.side
        qty = position.quantity
        entry, exit_ = position.price[i], prices[i]
        fee, slip = costs(entry, exit_, side, qty, slippage[i], BASE_FEES[i])
        gross = side * qty * (exit_ - entry)
        carry = -side * qty * funding[i]
        legs.append(
            {
                "coin": position.coin,
                "venue": name,
                "symbol": position.coin if i == 0 else pair.venue.symbol,
                "side": side,
                "entry": entry,
                "exit": exit_,
                "quantity": qty,
                "venue_contract_quantity": qty if i == 0 else qty * pair.venue.price_multiplier,
                "notional": qty * entry,
                "gross_price": gross,
                "funding": carry,
                "fees": fee,
                "slippage": slip,
                "net": gross + carry - fee - slip,
            }
        )
    totals = {
        key: sum(leg[key] for leg in legs)
        for key in ("gross_price", "funding", "fees", "slippage", "net")
    }
    return {
        "coin": position.coin,
        "entered_ms": position.entered * HOUR_MS,
        "exited_ms": hour * HOUR_MS,
        "reason": reason,
        "entry_24h_funding_advantage": position.entry_advantage,
        "position_capital": sum(position.reserved),
        "reference_quote_target_per_leg": position.reference_quote,
        "legs": legs,
        **totals,
    }


def summary(trades, equity, balance_series, counters, unresolved, start, end):
    n = len(trades)
    net = math.fsum(row["net"] for row in trades)
    gains = sum(row["net"] for row in trades if row["net"] > 0)
    losses = -sum(row["net"] for row in trades if row["net"] < 0)
    wins = sum(row["net"] > 0 for row in trades)
    by_coin, days = defaultdict(float), defaultdict(float)
    for trade in trades:
        by_coin[trade["coin"]] += trade["net"]
        days[trade["exited_ms"] // (24 * HOUR_MS)] += trade["net"]
    best = max(trades, key=lambda row: row["net"], default=None)
    best_coin = max(by_coin, key=by_coin.get, default=None)
    peak = equity
    drawdown = 0.0
    for balance in balance_series:
        peak = max(peak, balance)
        drawdown = max(drawdown, peak - balance)
    notionals = [leg["notional"] for row in trades for leg in row["legs"]]
    return {
        "starting_equity": equity,
        "leg_balance_fraction": LEG_BALANCE_FRACTION,
        "filled_leg_notionals": {
            "min": min(notionals, default=None),
            "median": statistics.median(notionals) if notionals else None,
            "max": max(notionals, default=None),
        },
        "net": net,
        "count": n,
        "wins": wins,
        "win_rate": wins / n if n else 0.0,
        "profit_factor": gains / losses if losses else None,
        "gross_price_basis_pnl": math.fsum(row["gross_price"] for row in trades),
        "funding": math.fsum(row["funding"] for row in trades),
        "hyperliquid_funding": math.fsum(row["legs"][0]["funding"] for row in trades),
        "binance_funding": math.fsum(row["legs"][1]["funding"] for row in trades),
        "fees": math.fsum(row["fees"] for row in trades),
        "slippage": math.fsum(row["slippage"] for row in trades),
        "net_without_best": net - (best["net"] if best else 0),
        "net_without_best_coin": net - by_coin.get(best_coin, 0),
        "net_without_best_trade_and_coin": math.fsum(
            row["net"] for row in trades if row is not best and row["coin"] != best_coin
        ),
        "worst_realized_day": min(days.values(), default=0.0),
        "hourly_drawdown": drawdown,
        "median_holding_hours": statistics.median(
            (row["exited_ms"] - row["entered_ms"]) / HOUR_MS for row in trades
        )
        if n
        else None,
        "max_pair_capital": max((row["position_capital"] for row in trades), default=0.0),
        "by_coin": dict(by_coin),
        "trades": trades,
        "counts": dict(counters),
        "unresolved_exits": len(unresolved),
        "unresolved_positions": unresolved,
        "start": hour_date(start),
        "end": hour_date(end),
    }


def simulate(pairs, start, end, variant, costs_mode, equity):
    slippage = SLIPPAGE[costs_mode]
    wallet = [equity / 2] * 2
    quarantined = [0.0, 0.0]
    positions, trades, balances, unresolved = {}, [], [], []
    counters = Counter()
    last_priced_hour = {}

    def missing_position(position, hour, reason):
        first_gap_hour, first_gap_reason = position.first_gap or (hour, reason)
        unresolved.append(
            {
                "coin": position.coin,
                "entered_ms": position.entered * HOUR_MS,
                "requested_exit_ms": hour * HOUR_MS,
                "reason": reason,
                "first_gap_ms": first_gap_hour * HOUR_MS,
                "first_gap_reason": first_gap_reason,
                "last_priced_hour": last_priced_hour.get(position.coin, position.entered),
                "position_capital": sum(position.reserved),
            }
        )
        for index in range(2):
            quarantined[index] += position.reserved[index]

    for hour in range(start, end):
        quotes = {coin: quote_at(pair, hour, variant) for coin, pair in pairs.items()}
        unrealized = 0.0
        for coin, position in list(positions.items()):
            pair, quote = pairs[coin], quotes[coin]
            if quote is None:
                position.note_gap(hour, "no paired candle or prior turnover")
                continue
            if quote.advantage is None:
                position.note_gap(hour, "funding estimate unavailable")
            last_priced_hour[coin] = hour
            marked = valued_trade(pair, position, hour, quote.reference, slippage, "mark")
            if marked is None:
                position.note_gap(hour, "settlement price unavailable")
            take = (
                marked is not None
                and marked["net"] > TAKE_PROFIT_FRACTION * position.reference_quote
            )
            reversal = (
                quote.advantage is not None and quote.advantage * position.side < FUNDING_REVERSAL
            )
            reason = "take_profit" if take else "funding_reversal" if reversal else None
            if hour == end - 1:
                reason = reason or "period_end"
            if reason is None:
                unrealized += marked["net"] if marked and not position.first_gap else 0.0
                continue
            result = valued_trade(pair, position, hour, quote.opening, slippage, reason)
            del positions[coin]
            if result is None or position.first_gap:
                missing_position(position, hour, reason)
            else:
                trades.append(result)
                for index, leg in enumerate(result["legs"]):
                    wallet[index] += leg["net"]
        balances.append(sum(wallet) + unrealized)
        if hour == end - 1:
            break
        free = [
            amount - quarantined[i] - sum(pos.reserved[i] for pos in positions.values())
            for i, amount in enumerate(wallet)
        ]
        available = sorted(
            (
                (coin, q)
                for coin, q in quotes.items()
                if q is not None
                and q.advantage is not None
                and abs(q.advantage) >= FUNDING_THRESHOLD
            ),
            key=lambda item: (-abs(item[1].advantage), item[0]),
        )
        for coin, quote in available:
            if coin in positions:
                continue
            counters["above_threshold_candidates"] += 1
            if len(positions) >= PAIR_CAP:
                counters["pair_cap"] += 1
                continue
            entry, rejected = entry_candidate(pairs[coin], quote, free, variant, slippage)
            if entry is None:
                counters[rejected] += 1
                continue
            side, quantity, reserved, target_quote = entry
            positions[coin] = Position(
                hour,
                coin,
                side,
                quantity,
                quote.opening,
                abs(quote.advantage),
                reserved,
                target_quote,
            )
            last_priced_hour[coin] = hour
            for index in range(2):
                free[index] -= reserved[index]
            counters["entered_pairs"] += 1
    for position in positions.values():
        missing_position(position, end - 1, "period end lacks a paired exit")
    return summary(trades, equity, balances, counters, unresolved, start, end)


def tracks(metrics):
    if metrics["unresolved_exits"]:
        return {"status": "insufficient complete cashflows", "count": metrics["unresolved_exits"]}
    # Hourly drawdown is a lower bound for intrahour drawdown. Report A only.
    return qualification("A", metrics)


def run(archive, cache, start, validation, end, *, complete_native_funding=False):
    if not start < validation < end <= MAX_END:
        raise ValueError(f"Require chronological Discovery and Validation ending by {MAX_END} UTC")
    histories, archive_info = load_archive(archive, day_hour(end))
    excluded_incomplete = []
    if complete_native_funding:
        excluded_incomplete = sorted(
            coin
            for coin in histories
            if archive_info["markets"][coin]["missing_funding_for_hourly"]
        )
        for coin in excluded_incomplete:
            del histories[coin]
    cache.mkdir(parents=True, exist_ok=True)
    pairs, coverage = archive_pairs(histories, cache, start, end)
    if not pairs:
        raise ValueError("No comparable paired venue archive coverage")
    periods = {
        "discovery": (day_hour(start), day_hour(validation)),
        "validation": (day_hour(validation), day_hour(end)),
    }
    results = {}
    for equity in (500, 300):
        cases = results[str(equity)] = {}
        for variant in VARIANTS:
            variants = cases[variant.name] = {}
            for period, bounds in periods.items():
                variants[period] = {
                    cost: simulate(pairs, *bounds, variant, cost, equity) for cost in SLIPPAGE
                }
        for entry in cases.values():
            entry["qualification"] = tracks(entry["validation"]["stress"])
            entry["positive_discovery_stress_net"] = entry["discovery"]["stress"]["net"] > 0
    return {
        "reference": REFERENCE,
        "reference_sha": REFERENCE_SHA,
        "binance_data_archive": BINANCE_ARCHIVE,
        "binance_historical_rest": BINANCE_API,
        "fee_model_sources": {
            "hyperliquid": "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees",
            "binance_usdm": "https://www.binance.com/en/support/faq/detail/360033544231",
        },
        "neiro_contract_identity": NEIRO_LISTING,
        "start": start.isoformat(),
        "validation_start": validation.isoformat(),
        "end_exclusive": end.isoformat(),
        "native_archive_coverage": archive_info,
        "require_complete_native_funding": complete_native_funding,
        "excluded_for_incomplete_native_funding": excluded_incomplete,
        "paired_symbols": sorted(pairs),
        "coverage": coverage,
        "variants": [asdict(row) for row in VARIANTS],
        "results": results,
        "assumptions": {
            "funding_entry_minimum_24h": FUNDING_THRESHOLD,
            "funding_reversal_24h": FUNDING_REVERSAL,
            "profit_take_fraction_of_entry_quote_target": TAKE_PROFIT_FRACTION,
            "trade_cap": PAIR_CAP,
            "leg_balance_fraction": LEG_BALANCE_FRACTION,
            "hypothetical_notional_floor_per_leg": 10,
            "research_leverage": 1,
            "initial_cash_each_venue": "half of account capital",
            "sizing": "target each matched leg at 20% of current combined free quote collateral, after all existing collateral reserves and unknown cashflow quarantine. Each venue is capped by its own current available balance, including fee and slippage reserve, before rounding down",
            "taker_fees_by_venue": BASE_FEES,
            "adverse_per_order_slippage_by_venue": SLIPPAGE,
            "funding_signal": "latest strictly previously settled event, corresponding rates are projected forwards",
            "modeled_funding_scheduler": "default uses the reference's fixed 1h/8h; actual-rate version uses monthly recorded Binance intervals and infers valid November intervals from consecutive settlements",
            "valuation": "same size both venues; previous completed bars signal and mark; current bar opens proxy fills; funding times mapped to their scheduled hour, requiring strictly interior events",
            "spot_conversion": "USDC and USDT treated at parity, without transfers or yield on collateral",
            "funding_cashflow_prices": "Binance historical REST marks when available; previous bar close proxies Binance monthly funding marks and Hyperliquid funding oracle prices",
            "gaps": "cashflows with gaps are omitted from realized totals and prominently counted as unresolved; used collateral remains unavailable after an unresolved close. Reported hourly drawdown covers priced positions only and excludes that collateral quarantine",
            "historical_market_precision": "Hyperliquid archive size precision rounds the matched quantity. Binance point-in-time lot sizes and venue-specific minimums are unavailable; $10 per leg is a research assumption",
            "hourly_proxy": "entry basis gate and four market fills use candle proxies, without quote-depth, cross-venue fill simultaneity, mark-based liquidation, intrahour stops, position restoration, or connection delays",
            "changes_from_reference": "one-hour sampling, fractional free collateral sizing, hard pair cap and independent balances; leverage 1x instead of upstream 20x; simultaneous candidates sorted by latest absolute differential; close at sample boundary",
        },
    }


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("archive", type=Path)
    cli.add_argument("cache", type=Path)
    cli.add_argument("output", type=Path)
    cli.add_argument("--start", type=date.fromisoformat, default=date(2026, 5, 25))
    cli.add_argument("--validation", type=date.fromisoformat, default=date(2026, 9, 26))
    cli.add_argument("--end", type=date.fromisoformat, default=MAX_END)
    cli.add_argument(
        "--complete-native-funding",
        action="store_true",
        help="Require complete native hourly funding over the observed price span",
    )
    args = cli.parse_args()
    result = run(
        args.archive,
        args.cache,
        args.start,
        args.validation,
        args.end,
        complete_native_funding=args.complete_native_funding,
    )
    write_json(args.output, result)


if __name__ == "__main__":
    main()
