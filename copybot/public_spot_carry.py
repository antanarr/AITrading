"""Historical cash-and-carry replay using public Hyperliquid market data."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, Decimal, DecimalException
from pathlib import Path
from statistics import fmean, median, stdev
from threading import Lock
from time import monotonic, sleep

import httpx

from .research_data import ARCHIVE_END_HOUR, _bar, _funding, _inventory, _series
from .research_models import HOUR_MS, excluded_symbol

INFO_URL = "https://api.hyperliquid.xyz/info"
SPECS_URL = "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/contract-specifications"
FEES_URL = "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees"
FUNDING_URL = "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding"
UNIT_URL = "https://hyperliquid.gitbook.io/hyperliquid-docs/onboarding/how-to-start-trading"
UNIT_DEPLOYER = "0xf036a5261406a394bd63eb4df49c464634a66155"
USDC_TOKEN_ID = "0x6d1e7cde53ba9467b783cb7c530ce054"
DAY = 24
WEEK = 7 * DAY
LONG_HOLD = 28 * DAY
MINIMUM = 10
SPOT_TAKER_BPS = 7.0
PERP_TAKER_BPS = 4.5
PROFILES = {"base": (6.0, 4.0, 2.0), "stress": (15.0, 10.0, 6.0)}
CAPITALS = (500.0, 300.0)
FUNDED_FRACTION = 0.95
MAX_PARTICIPATION = 0.02
MAX_ABS_BASIS = 0.02
PROFIT_RESERVE_FRACTION = 0.25
BTC_WRAPPERS = {"UBTC", "TBTC", "CBBTC", "BTCB", "LBTC", "SBTC", "HBTC", "RBTC"}


@dataclass(frozen=True, slots=True)
class Identity:
    perp: str
    spot: str
    token_id: str
    units_per_contract: int
    provenance: str
    unit_issuer: bool = False


# Spot token IDs distinguish tokens whose display symbols collide.
IDENTITIES = (
    Identity("HYPE", "HYPE", "0x0d01dc56dcaaca66ad901c959b4011ec", 1, SPECS_URL),
    Identity("PURR", "PURR", "0xc1fb593aeffbeb02f85e0308e9956a90", 1, SPECS_URL),
    Identity("ETH", "UETH", "0xe1edd30daaf5caac3fe63569e24748da", 1, UNIT_URL, True),
    Identity("SOL", "USOL", "0x49b67c39f5566535de22b29b0e51e685", 1, UNIT_URL, True),
    Identity("FARTCOIN", "UFART", "0x7650808198966e4285687d3deb556ccc", 1, UNIT_URL, True),
    Identity("PUMP", "UPUMP", "0x544e60f98a36d7b22c0fb5824b84f795", 1, UNIT_URL, True),
    Identity("SPX", "UUUSPX", "0x2ff71b802a6788a052c7f1a58ec863af", 1, UNIT_URL, True),
    Identity("kBONK", "UBONK", "0xb113d34e351cf195733c98442530c099", 1000, UNIT_URL, True),
    Identity("ENA", "UENA", "0x593494b6af79172fa983a0cf1c88e0e0", 1, UNIT_URL, True),
    Identity("XPL", "UXPL", "0x2c54c60600e1d786b2dfc139a38a5a99", 1, UNIT_URL, True),
    Identity("MON", "UMON", "0x58dae745c8c5fed4012f35ef39829c2d", 1, UNIT_URL, True),
    Identity("ZEC", "UZEC", "0x1c994ad3381d31c86c8c2d74ed89a365", 1, INFO_URL, True),
    Identity("AVAX", "UAVAX", "0x730fc3855fb77d2aa5a19dd7891dbe80", 1, INFO_URL, True),
    Identity("WLD", "UWLD", "0x571aa6d5261fb8f28e21a9cdb2e1c646", 1, INFO_URL, True),
    Identity("MEGA", "UMEGA", "0xc99926509a189b40651055a15d3be621", 1, INFO_URL, True),
    Identity("2Z", "UDZ", "0x9cd8fd4cae61e63a10ba7615780ee520", 1, UNIT_URL, True),
    Identity("VIRTUAL", "UVIRT", "0x85b8124314ae77b78b4b6f20ecd93149", 1, INFO_URL, True),
    Identity(
        "PENGU",
        "HPENGU",
        "0x1765b5a9ec8fc3cdbc209c63cac68e86",
        1,
        "https://t.me/s/hyperliquid_announcements/392",
    ),
)


@dataclass(frozen=True, slots=True)
class Pair:
    identity: Identity
    spot_id: str
    spot_decimals: int
    perp_decimals: int
    perp_leverage_ceiling: int

    @property
    def coin(self):
        return self.identity.perp


def is_bitcoin(symbol):
    return excluded_symbol(symbol) or symbol.upper() in BTC_WRAPPERS


def discover(spot, perps, issuer_details, identities=IDENTITIES):
    """Resolve native pairs against the exact USDC and underlying token IDs."""
    tokens = {row["index"]: row for row in spot["tokens"]}
    referenced = {row.token_id: row for row in identities}
    active = {
        row["name"]: row
        for row in perps["universe"]
        if not row.get("isDelisted") and ":" not in row["name"] and not is_bitcoin(row["name"])
    }
    expected = {name.upper(): name for name in active}
    pairs, exceptions = {}, []
    for row in spot["universe"]:
        base, quote = (tokens[index] for index in row["tokens"])
        symbol = base["name"]
        if is_bitcoin(symbol):
            continue
        identity = referenced.get(base["tokenId"])
        if identity is not None and is_bitcoin(identity.perp):
            continue
        plausible = set()
        if identity:
            plausible.add(identity.perp)
        for normalized in (symbol.upper(), "K" + symbol.upper(), symbol.upper()[1:]):
            if normalized in expected:
                plausible.add(expected[normalized])
        if not plausible:
            continue
        issue = None
        if quote["tokenId"] != USDC_TOKEN_ID:
            issue = "noncanonical_or_non_usdc_quote"
        elif identity is None or identity.spot != symbol:
            issue = "underlying_identity_unverified"
        elif identity.perp not in active:
            issue = "native_perp_not_active"
        elif identity.unit_issuer and issuer_details.get(identity.token_id) != UNIT_DEPLOYER:
            issue = "unit_issuer_unverified"
        if issue:
            exceptions.append(
                {
                    "spot_market": row["name"],
                    "spot_symbol": symbol,
                    "quote_symbol": quote["name"],
                    "spot_token_id": base["tokenId"],
                    "plausible_perps": sorted(plausible),
                    "reason": issue,
                }
            )
            continue
        native = active[identity.perp]
        if identity.perp in pairs:
            raise ValueError(f"Multiple reviewed USDC pairs for {identity.perp}")
        pairs[identity.perp] = Pair(
            identity,
            row["name"],
            base["szDecimals"],
            native["szDecimals"],
            native["maxLeverage"],
        )
    return pairs, exceptions


class PublicHistory:
    """Bounded public info requests and a restartable, local historical cache."""

    def __init__(self, client, directory):
        self.client = client
        self.directory = directory
        self.lock = Lock()
        self.available = monotonic()

    def get(self, name, weight, body):
        path = self.directory / f"{name}.json"
        if path.is_file():
            return read_json(path)
        for attempt in range(4):
            with self.lock:
                slot = max(monotonic(), self.available)
                self.available = slot + weight / 16
            sleep(max(0, slot - monotonic()))
            try:
                response = self.client.post(INFO_URL, json=body)
                response.raise_for_status()
                value = response.json()
                write_json(path, value)
                return value
            except (httpx.HTTPError, ValueError) as exc:
                if attempt == 3:
                    raise
                status = (
                    exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                )
                with self.lock:
                    pause = 18 if status == 429 else attempt + 1
                    self.available = max(self.available, monotonic() + pause)
        raise RuntimeError("Public history retry limit reached")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def information(source):
    if isinstance(source, PublicHistory):
        spot = source.get("spot-meta", 20, {"type": "spotMeta"})
        perp = source.get("perp-meta", 20, {"type": "meta"})
        issuer = {}
        known = {row["tokenId"] for row in spot["tokens"]}
        for identity in IDENTITIES:
            if identity.unit_issuer and identity.token_id in known:
                try:
                    token = source.get(
                        f"token-{identity.token_id}",
                        20,
                        {
                            "type": "tokenDetails",
                            "tokenId": identity.token_id,
                        },
                    )
                except (httpx.HTTPError, ValueError):
                    continue
                issuer[identity.token_id] = token.get("deployer")
        return spot, perp, issuer
    spot = read_json(source / "spot-meta.json")
    perp = read_json(source / "perp-meta.json")
    issuer = {
        path.stem.removeprefix("token-"): read_json(path).get("deployer")
        for path in source.glob("token-0x*.json")
    }
    return spot, perp, issuer


def candles_body(symbol, start, end):
    return {
        "type": "candleSnapshot",
        "req": {
            "coin": symbol,
            "interval": "1h",
            "startTime": start * HOUR_MS,
            "endTime": end * HOUR_MS - 1,
        },
    }


def parsed_rows(rows, symbol, start, end, *, funding=False):
    if not isinstance(rows, list):
        raise ValueError(f"Expected a history list for {symbol}")
    results, conflicts = {}, set()
    bad = 0
    for row in rows:
        stamp = row.get("time" if funding else "t") if isinstance(row, dict) else None
        if type(stamp) is not int:
            bad += 1
            continue
        hour = stamp // HOUR_MS
        if not start <= hour < end:
            continue
        try:
            value = _funding(row, symbol) if funding else _bar(row, symbol, "1h", HOUR_MS)
        except (DecimalException, KeyError, TypeError, ValueError):
            bad += 1
            continue
        if hour in results and results[hour] != value:
            conflicts.add(hour)
        results[hour] = value
    for hour in conflicts:
        results.pop(hour, None)
    return results, bad + len(conflicts)


@dataclass(frozen=True, slots=True)
class ArchiveHistory:
    bars: dict
    funding: dict


def archive_reuse(paths, end):
    if not paths:
        return {}
    inventory = defaultdict(lambda: defaultdict(list))
    for path in [paths] if isinstance(paths, Path) else paths:
        if not path.is_dir():
            raise ValueError(f"Perpetual archive directory not found: {path}")
        for coin, files in _inventory(path, False).items():
            for kind, listed in files.items():
                inventory[coin][kind].extend(listed)
    cutoff = end * HOUR_MS
    histories = {}
    for identity in IDENTITIES:
        files = inventory.get(identity.perp)
        if not files:
            continue
        bars, _, _ = _series(files.get("hour", ()), "hour", identity.perp, cutoff)
        funding, _, _ = _series(files.get("fund", ()), "fund", identity.perp, cutoff)
        histories[identity.perp] = ArchiveHistory(bars, funding)
    return histories


def cache_name(kind, symbol, start, end):
    return f"{kind}-{symbol.replace('/', '_')}-{start}-{end}"


def download_pair(source, pair, reuse, start, end):
    coin = pair.coin
    duration = end - start
    spot = source.get(
        cache_name("spot", pair.spot_id, start, end),
        20 + math.ceil(duration / 60),
        candles_body(pair.spot_id, start, end),
    )
    bars, _ = parsed_rows(spot, pair.spot_id, start, end)
    if not bars:
        return
    prior = reuse.get(coin)
    if prior is None or not all(hour in prior.bars for hour in bars):
        source.get(
            cache_name("perp", coin, start, end),
            20 + math.ceil(duration / 60),
            candles_body(coin, start, end),
        )
    for left in range(min(bars), end, 20 * DAY):
        right = min(end, left + 20 * DAY)
        if prior is not None and all(hour in prior.funding for hour in range(left, right)):
            continue
        source.get(
            cache_name("fund", coin, left, right),
            20 + math.ceil((right - left) / 20),
            {
                "type": "fundingHistory",
                "coin": coin,
                "startTime": left * HOUR_MS,
                "endTime": right * HOUR_MS - 1,
            },
        )


def collect(directory, reuse, start, end):
    directory.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=15, trust_env=False) as client:
        source = PublicHistory(client, directory)
        pairs, excluded = discover(*information(source))
        print(
            f"Recognized {len(pairs)} USDC spot/perp pairs. Reviewed {len(excluded)} exclusions.",
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                coin: pool.submit(download_pair, source, pair, reuse, start, end)
                for coin, pair in pairs.items()
            }
            for coin, future in futures.items():
                try:
                    future.result()
                except (httpx.HTTPError, ValueError) as exc:
                    print(f"{coin} collection incomplete: {exc}", flush=True)
                else:
                    print(f"{coin} history checked", flush=True)
    return pairs, excluded


@dataclass(slots=True)
class History:
    pair: Pair
    spot: dict
    perp: dict
    funding: dict
    evidence: dict


def cache_rows(directory, kind, symbol, start, end):
    path = directory / f"{cache_name(kind, symbol, start, end)}.json"
    return read_json(path) if path.is_file() else []


def load_pairs(directory, pairs, reuse, start, end):
    histories = {}
    for coin, pair in pairs.items():
        spot, bad_spot = parsed_rows(
            cache_rows(directory, "spot", pair.spot_id, start, end),
            pair.spot_id,
            start,
            end,
        )
        downloaded, bad_perp = parsed_rows(
            cache_rows(directory, "perp", coin, start, end),
            coin,
            start,
            end,
        )
        cached_funding = []
        for path in sorted(directory.glob(f"fund-{coin}-*.json")):
            cached_funding.extend(read_json(path))
        funding, bad_funding = parsed_rows(cached_funding, coin, start, end, funding=True)
        archive = reuse.get(coin)
        archived_bars, archived_funding = (
            ({}, {})
            if archive is None
            else (
                {hour: bar for hour, bar in archive.bars.items() if start <= hour < end},
                {hour: row for hour, row in archive.funding.items() if start <= hour < end},
            )
        )
        overlaps = sum(
            archived_bars.get(hour) != bar
            for hour, bar in downloaded.items()
            if hour in archived_bars
        )
        funding_overlaps = sum(
            archived_funding.get(hour) != row
            for hour, row in funding.items()
            if hour in archived_funding
        )
        for hour in set(archived_bars) & downloaded.keys():
            if archived_bars[hour] != downloaded[hour]:
                del archived_bars[hour]
                del downloaded[hour]
        for hour in set(archived_funding) & funding.keys():
            if archived_funding[hour] != funding[hour]:
                del archived_funding[hour]
                del funding[hour]
        perp = {**archived_bars, **downloaded}
        combined_funding = {**archived_funding, **funding}
        paired = spot.keys() & perp.keys()
        ratios = [
            perp[hour].close / (spot[hour].close * pair.identity.units_per_contract)
            for hour in paired
            if spot[hour].volume > 0 and perp[hour].volume > 0
        ]
        evidence = {
            "pair": asdict(pair),
            "spot_hours": len(spot),
            "perp_hours": len(perp),
            "funding_hours": len(combined_funding),
            "same_hour_bars": len(paired),
            "joint_funding_hours": len(paired & combined_funding.keys()),
            "same_hour_first": min(paired, default=None),
            "same_hour_last": max(paired, default=None),
            "active_close_price_ratio_median_after_multiplier": median(ratios) if ratios else None,
            "invalid_spot_rows": bad_spot,
            "invalid_perp_rows": bad_perp,
            "invalid_funding_rows": bad_funding,
            "archive_bars_considered": len(archived_bars),
            "archive_funding_rows_considered": len(archived_funding),
            "conflicting_archive_perp_hours": overlaps,
            "conflicting_archive_funding_hours": funding_overlaps,
        }
        histories[coin] = History(pair, spot, perp, combined_funding, evidence)
    return histories


def trading_liquidity(bars, hour):
    prior = [bars.get(stamp) for stamp in range(hour - DAY, hour)]
    if any(row is None for row in prior) or prior[-1].volume <= 0:
        return None
    liquid = [row.volume * row.close for row in prior if row.volume > 0]
    return median(liquid) if len(liquid) >= 12 else None


def quantity_step(pair):
    spot = Decimal(10) ** -pair.spot_decimals
    perp = Decimal(pair.identity.units_per_contract) * Decimal(10) ** -pair.perp_decimals
    return max(spot, perp)


def slip_rate(model, leg, order_notional, liquidity):
    floor = model[leg]
    # Share of representative traded-hour volume, not displayed book depth.
    scaled = model[2] * math.sqrt(order_notional / (0.001 * liquidity))
    return (floor + scaled) / 10_000


def stamp_text(hour):
    return datetime.fromtimestamp(hour * 3600, UTC).isoformat()


def expected_carry(history, decision, equity, model, hold=WEEK):
    pair = history.pair
    spot = history.spot.get(decision - 1)
    perp = history.perp.get(decision - 1)
    funding = [history.funding.get(hour) for hour in range(decision - WEEK, decision)]
    if spot is None or perp is None or any(row is None for row in funding):
        return None, "insufficient_prior_history"
    # Funding records whose settlement occurred at or after decision are unavailable.
    if any(row.time_ms >= decision * HOUR_MS for row in funding):
        return None, "prior_funding_not_settled_by_decision"
    ratio = perp.close / (spot.close * pair.identity.units_per_contract)
    if abs(ratio - 1) > MAX_ABS_BASIS:
        return None, "prior_close_basis_outside_2pct"
    spot_liquidity = trading_liquidity(history.spot, decision)
    perp_liquidity = trading_liquidity(history.perp, decision)
    if not spot_liquidity or not perp_liquidity:
        return None, "insufficient_prior_liquidity"
    notional = equity * FUNDED_FRACTION / 2
    if notional > MAX_PARTICIPATION * min(spot_liquidity, perp_liquidity):
        return None, "prior_hourly_turnover_capacity"
    fees = (SPOT_TAKER_BPS + PERP_TAKER_BPS) / 10_000 * 2
    slippage = 2 * (
        slip_rate(model, 0, notional, spot_liquidity)
        + slip_rate(model, 1, notional, perp_liquidity)
    )
    # A spot/perp basis forecast would be a separate hypothesis. Assume flat basis for selection.
    return sum(row.rate for row in funding) * hold / WEEK - fees - slippage, None


def common_qty(equity, pair, spot_price, perp_price):
    unit_cost = (
        spot_price * (1 + SPOT_TAKER_BPS / 10_000)
        + perp_price * (1 + PERP_TAKER_BPS / 10_000) / pair.identity.units_per_contract
    )
    step = quantity_step(pair)
    steps = (Decimal(str(equity * FUNDED_FRACTION / unit_cost)) / step).to_integral_value(
        rounding=ROUND_DOWN,
    )
    return float(steps * step)


def execute(history, opened, closed, equity, model):
    spot, perp, funding = history.spot, history.perp, history.funding
    pair = history.pair
    multiplier = pair.identity.units_per_contract
    if any(hour not in spot or hour not in perp for hour in range(opened, closed + 1)):
        return None, "missing_held_prices"
    # Entry and exit take place just after their hour opens. This excludes entry-hour
    # settlement and includes the exit-hour settlement before exposure is closed.
    if any(hour not in funding for hour in range(opened + 1, closed + 1)):
        return None, "missing_held_funding"
    if any(funding[hour].time_ms // HOUR_MS != hour for hour in range(opened + 1, closed + 1)):
        return None, "invalid_held_funding_clock"
    if min(spot[opened].volume, perp[opened].volume, spot[closed].volume, perp[closed].volume) <= 0:
        return None, "untraded_execution_candle"
    entry_liq = (trading_liquidity(spot, opened), trading_liquidity(perp, opened))
    exit_liq = (trading_liquidity(spot, closed), trading_liquidity(perp, closed))
    if not all((*entry_liq, *exit_liq)):
        return None, "unobservable_execution_liquidity"
    ref_approx = equity * FUNDED_FRACTION / 2
    spot_in = spot[opened].open * (1 + slip_rate(model, 0, ref_approx, entry_liq[0]))
    perp_in = perp[opened].open * (1 - slip_rate(model, 1, ref_approx, entry_liq[1]))
    if perp_in <= 0:
        return None, "slippage_exceeds_executable_price_proxy"
    quantity = common_qty(equity, pair, spot_in, perp_in)
    contracts = quantity / multiplier
    entry_notional = (quantity * spot_in, contracts * perp_in)
    if min(entry_notional) < MINIMUM:
        return None, "venue_minimum_or_rounding"
    exit_ref = (quantity * spot[closed].open, contracts * perp[closed].open)
    spot_out = spot[closed].open * (1 - slip_rate(model, 0, exit_ref[0], exit_liq[0]))
    perp_out = perp[closed].open * (1 + slip_rate(model, 1, exit_ref[1], exit_liq[1]))
    if spot_out <= 0:
        return None, "slippage_exceeds_executable_price_proxy"
    exit_notional = (quantity * spot_out, contracts * perp_out)
    fees = (
        (entry_notional[0] + exit_notional[0]) * SPOT_TAKER_BPS
        + (entry_notional[1] + exit_notional[1]) * PERP_TAKER_BPS
    ) / 10_000
    basis = quantity * (spot[closed].open - spot[opened].open) + contracts * (
        perp[opened].open - perp[closed].open
    )
    raw_price = quantity * (spot_out - spot_in) + contracts * (perp_in - perp_out)
    slippage = basis - raw_price
    cash_funding = 0.0
    maintenance_rate = 1 / (2 * pair.perp_leverage_ceiling)
    minimum_cushion = math.inf
    for hour in range(opened, closed):
        high = perp[hour].high
        short_equity = entry_notional[1] * (1 - PERP_TAKER_BPS / 10_000) + cash_funding
        short_equity += contracts * (perp_in - high)
        minimum_cushion = min(minimum_cushion, short_equity - contracts * high * maintenance_rate)
        # The archived funding payload has rates, and hourly spot close is the available oracle proxy.
        cash_funding += quantity * spot[hour].close * funding[hour + 1].rate
    net = basis + cash_funding - slippage - fees
    trade = {
        "coin": pair.coin,
        "entry_utc_ms": opened * HOUR_MS,
        "exit_utc_ms": closed * HOUR_MS,
        "entry_utc": stamp_text(opened),
        "exit_utc": stamp_text(closed),
        "equity_before_usdc": equity,
        "equity_after_usdc": equity + net,
        "basis_pnl_usdc": basis,
        "funding_pnl_usdc": cash_funding,
        "slippage_usdc": slippage,
        "fees_usdc": fees,
        "net_usdc": net,
        "entry_realized_basis_bps": (perp_in / (spot_in * multiplier) - 1) * 10_000,
        "exit_realized_basis_bps": (perp_out / (spot_out * multiplier) - 1) * 10_000,
        "perp_initial_cash_margin_usdc": entry_notional[1],
        "capital_committed_at_entry_usdc": (
            entry_notional[0] * (1 + SPOT_TAKER_BPS / 10_000)
            + entry_notional[1] * (1 + PERP_TAKER_BPS / 10_000)
        ),
        "gross_entry_notional_usdc": sum(entry_notional),
        "isolated_short_maintenance_cushion_proxy_usdc": minimum_cushion,
        "exit_over_prior_turnover_capacity": any(
            order > MAX_PARTICIPATION * turnover
            for order, turnover in zip(exit_ref, exit_liq, strict=True)
        ),
        "spot": {
            "symbol": pair.spot_id,
            "side": "long",
            "units": quantity,
            "entry_px": spot_in,
            "exit_px": spot_out,
            "entry_reference_px": spot[opened].open,
            "exit_reference_px": spot[closed].open,
        },
        "perp": {
            "symbol": pair.coin,
            "side": "short",
            "contracts": contracts,
            "entry_px": perp_in,
            "exit_px": perp_out,
            "entry_reference_px": perp[opened].open,
            "exit_reference_px": perp[closed].open,
            "spot_units_per_contract": multiplier,
        },
    }
    return trade, None


def replay(
    histories,
    start,
    end,
    starting_equity,
    profile,
    *,
    adaptive,
    include_trades=True,
    profit_reserve=0.0,
    hold=WEEK,
):
    equity, peak, max_drawdown = starting_equity, starting_equity, 0.0
    models = PROFILES[profile]
    trades, returns, reasons = [], [], Counter()
    planned_windows = 0
    for opened in range(start, end - hold, hold):
        planned_windows += 1
        locked = profit_reserve * (peak - starting_equity)
        available = equity - locked
        if available <= 0:
            reasons["no_available_capital"] += 1
            returns.append(0.0)
            continue
        scored = {}
        for coin, history in histories.items():
            score, reason = expected_carry(history, opened, available, models, hold)
            if reason:
                reasons[reason] += 1
            else:
                scored[coin] = score
        if not scored:
            reasons["all_markets_ineligible_at_decision"] += 1
            returns.append(0.0)
            continue
        coin = min(scored, key=lambda name: (-scored[name], name))
        if adaptive and scored[coin] <= 0:
            reasons["no_positive_projected_funding_after_cost"] += 1
            returns.append(0.0)
            continue
        trade, problem = execute(histories[coin], opened, opened + hold, available, models)
        if problem:
            reasons[problem] += 1
            reasons["unreplayable_selected_windows"] += 1
            continue
        trade["projected_funding_less_roundtrip_cost_bps"] = scored[coin] * 10_000
        trade["account_equity_before_usdc"] = equity
        trade["available_entry_equity_usdc"] = available
        trade["locked_profit_before_usdc"] = locked
        trades.append(trade)
        returns.append(trade["net_usdc"] / equity)
        equity += trade["net_usdc"]
        peak = max(peak, equity)
        trade["account_equity_after_usdc"] = equity
        trade["locked_profit_after_usdc"] = profit_reserve * (peak - starting_equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
    dispersion = stdev(returns) if len(returns) > 1 else 0.0
    spot_notionals = [trade["spot"]["units"] * trade["spot"]["entry_px"] for trade in trades]
    perp_notionals = [trade["perp_initial_cash_margin_usdc"] for trade in trades]
    profile_data = {
        "starting_equity_usdc": starting_equity,
        "modeled_end_equity_usdc": equity,
        "holding_period_days": hold // DAY,
        "final_locked_profit_usdc": profit_reserve * (peak - starting_equity),
        "entry_notional_usdc": {
            leg: {
                "min": min(samples, default=None),
                "mean": fmean(samples) if samples else None,
                "max": max(samples, default=None),
            }
            for leg, samples in (("spot", spot_notionals), ("perp", perp_notionals))
        },
        "grid_windows": planned_windows,
        "completed_trades": len(trades),
        "positive_trades": sum(trade["net_usdc"] > 0 for trade in trades),
        "markets_traded": dict(Counter(trade["coin"] for trade in trades)),
        "total_return_pct": (equity / starting_equity - 1) * 100,
        "period_exit_max_drawdown_pct": max_drawdown * 100,
        "annualized_grid_sharpe": fmean(returns) / dispersion * math.sqrt(365 * DAY / hold)
        if dispersion
        else None,
        "components_usdc": {
            key: sum(trade[key] for trade in trades)
            for key in (
                "basis_pnl_usdc",
                "funding_pnl_usdc",
                "slippage_usdc",
                "fees_usdc",
                "net_usdc",
            )
        },
        "liquidation_proxy_breaches": sum(
            trade["isolated_short_maintenance_cushion_proxy_usdc"] <= 0 for trade in trades
        ),
        "exit_over_prior_turnover_capacity": sum(
            trade["exit_over_prior_turnover_capacity"] for trade in trades
        ),
        "diagnostics": dict(reasons),
    }
    if include_trades:
        profile_data["trades"] = trades
    return profile_data


def run_study(histories, excluded, start, validation, end, capital=CAPITALS):
    partitions = {"discovery": (start, validation), "validation": (validation, end)}
    results = {}
    for phase, (left, right) in partitions.items():
        results[phase] = {
            "utc_start": stamp_text(left),
            "utc_end_exclusive": stamp_text(right),
            "adaptive_weekly_carry": {
                str(amount): {
                    model: replay(histories, left, right, amount, model, adaptive=True)
                    for model in PROFILES
                }
                for amount in capital
            },
            "adaptive_weekly_carry_profit_reserve_25pct": {
                str(amount): {
                    model: replay(
                        histories,
                        left,
                        right,
                        amount,
                        model,
                        adaptive=True,
                        profit_reserve=PROFIT_RESERVE_FRACTION,
                    )
                    for model in PROFILES
                }
                for amount in capital
            },
            "adaptive_28d_carry": {
                str(amount): {
                    model: replay(
                        histories, left, right, amount, model, adaptive=True, hold=LONG_HOLD
                    )
                    for model in PROFILES
                }
                for amount in capital
            },
            "single_market_weekly_control": {
                coin: {
                    model: replay(
                        {coin: history},
                        left,
                        right,
                        capital[0],
                        model,
                        adaptive=False,
                        include_trades=False,
                    )
                    for model in PROFILES
                }
                for coin, history in histories.items()
            },
        }
    return {
        "method": {
            "selection": "On a fixed non-overlapping UTC grid, select the highest trailing seven-day funding extrapolated to the holding horizon, less both legs' modeled round-trip costs. Require a positive estimate and start each phase flat.",
            "holding_period_days": [WEEK // DAY, LONG_HOLD // DAY],
            "control": "Independently replay the same weekly grid for each reviewed market using prior eligibility and no positive funding hurdle.",
            "base_spot_taker_fee_bps": SPOT_TAKER_BPS,
            "base_perp_taker_fee_bps": PERP_TAKER_BPS,
            "severity": {
                name: {
                    "spot_slip_floor_bps_per_leg": rates[0],
                    "perp_slip_floor_bps_per_leg": rates[1],
                    "participation_impact_bps_scale": rates[2],
                }
                for name, rates in PROFILES.items()
            },
            "funding_clock": "Funding paid by the long to the short when positive. Entry-hour funding excluded, exit-hour funding included. Same-token spot hourly close proxies unavailable oracle price.",
            "price_clock": "Only previous completed candles/funding select entries; first trade of the requested hourly bar proxies each side's execution independently.",
            "constraints": {
                "spot_units_equal_perp_contracts_times_multiplier": True,
                "isolated_perp_cash_margin_leverage": 1,
                "capital_fraction": FUNDED_FRACTION,
                "min_order_usdc_per_leg": MINIMUM,
                "max_abs_prior_basis": MAX_ABS_BASIS,
                "max_prior_hourly_turnover_participation": MAX_PARTICIPATION,
                "positions_concurrently_open": 1,
                "profit_reserve_fraction_of_realized_equity_high_above_start": PROFIT_RESERVE_FRACTION,
            },
            "limitations": [
                "Independent first trades and assumed slippage cannot establish simultaneous executable liquidity.",
                "Native spot market selection uses today's metadata and is subject to survivor bias.",
                "Historical venue oracle and mark prices, exact order-book liquidity, liquidation execution, and collateral-transfer timing are unavailable.",
                "Non-HYPE and non-PURR native perps can reference a USDT oracle yet settle in USDC. The replay does not model independent USDT/USDC changes or bridge impairment.",
                "An unreplayable selected position is disclosed and omitted from modeled equity, so any such estimate is incomplete.",
            ],
            "provider_sources": [SPECS_URL, FEES_URL, FUNDING_URL, INFO_URL],
        },
        "coverage_by_market": {coin: history.evidence for coin, history in histories.items()},
        "plausible_excluded_pairs": excluded,
        "phases": results,
    }


def utc_hour(value):
    return (date.fromisoformat(value) - date(1970, 1, 1)).days * DAY


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True, help="Local cache of public inputs")
    parser.add_argument(
        "--perp-archive",
        type=Path,
        action="append",
        help="Existing research candle and funding archive, repeat for multiple archives",
    )
    parser.add_argument(
        "--collect", action="store_true", help="Fetch public metadata and bounded historical inputs"
    )
    parser.add_argument("--start", default="2026-05-25", type=utc_hour)
    parser.add_argument("--validation", default="2026-09-26", type=utc_hour)
    parser.add_argument("--end", default="2026-11-21", type=utc_hour)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 0 < args.start < args.validation < args.end <= ARCHIVE_END_HOUR:
        parser.error(
            "Require chronological Discovery and Validation with an exclusive end by November 21, 2026 UTC."
        )
    if args.end - (args.start - WEEK) > 5_000:
        parser.error(
            "The candle request including seven-day warmup exceeds the official 5,000-bar limit."
        )
    start = args.start - WEEK
    reuse = archive_reuse(args.perp_archive, args.end)
    pairs, excluded = (
        collect(args.data, reuse, start, args.end)
        if args.collect
        else discover(*information(args.data))
    )
    histories = load_pairs(args.data, pairs, reuse, start, args.end)
    report = run_study(histories, excluded, args.start, args.validation, args.end)
    write_json(args.output, report)
    print(f"Wrote {len(histories)} reviewed markets to {args.output}", flush=True)


if __name__ == "__main__":
    main()
