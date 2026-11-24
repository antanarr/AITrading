"""Compare the same closed-bar signal on Hyperliquid and Kraken's public data."""

import argparse
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from math import isfinite
from pathlib import Path

import httpx

from .broad_study import run_book
from .forward_signals import ENTRY_CAP, ForwardDecision, MarketMeta, _Bar, _liquidity, _strategy
from .liquid_trend_leverage import WINDOWS
from .meme_study import COSTS, write_json
from .research_data import ARCHIVE_END_HOUR, _inventory, _metadata, _series
from .research_execution import RiskLimits
from .research_models import DAY_HOURS, HOUR_MS, Bar, Funding, MarketHistory, TradePlan

KRAKEN = "https://futures.kraken.com"
INSTRUMENTS_PATH = "/derivatives/api/v3/instruments"
CANDLES_PATH = "/api/charts/v1/trade/{symbol}/1h"
FUNDING_PATH = "/derivatives/api/v4/historicalfundingrates"
COINS = (
    "ETH",
    "HYPE",
    "SOL",
    "XRP",
    "DOGE",
    "ADA",
    "LINK",
    "AVAX",
    "LTC",
    "SUI",
    "BCH",
    "NEAR",
    "UNI",
)
START = WINDOWS["discovery"][0]
END = ARCHIVE_END_HOUR
FIRST = START - 52 * DAY_HOURS
PAGE_HOURS = 1900
VENUE_FEES = {"hyperliquid": (4.5, 1.5), "kraken": (5.0, 2.0)}


def _utc(hour):
    return datetime.fromtimestamp(hour * 3600, UTC).isoformat()


def _public_get(client, path, params=None):
    for attempt in range(3):
        try:
            response = client.get(KRAKEN + path, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError:
            if attempt == 2:
                raise
            time.sleep(0.25 * (attempt + 1))


def _candle_page(rows, start, end):
    bars = {}
    for row in rows:
        hour, remainder = divmod(row["time"], HOUR_MS)
        if not start <= hour < end:
            continue
        if remainder:
            raise ValueError("Kraken hourly candle is misaligned")
        bar = Bar(*(float(row[key]) for key in ("open", "high", "low", "close", "volume")))
        if not all(
            isfinite(value) for value in (bar.open, bar.high, bar.low, bar.close, bar.volume)
        ):
            raise ValueError("Non-finite Kraken candle")
        if not 0 < bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
            raise ValueError("Invalid Kraken candle OHLC")
        if bar.volume < 0 or (hour in bars and bars[hour] != bar):
            raise ValueError("Invalid Kraken volume or conflicting candles")
        bars[hour] = bar
    return bars


def _kraken_candles(client, symbol):
    candles = {}
    for first in range(FIRST, END, PAGE_HOURS):
        end = min(first + PAGE_HOURS, END)
        while first < end:
            page = _public_get(
                client,
                CANDLES_PATH.format(symbol=symbol),
                {"from": first * 3600, "to": end * 3600 - 1, "count": PAGE_HOURS},
            )
            bars = _candle_page(page["candles"], first, end)
            candles.update(bars)
            if not bars or not page.get("more_candles"):
                break
            first = max(bars) + 1
    return candles


def _kraken_funding(rows, bars, start, end):
    funding = {}
    for row in rows:
        timestamp = datetime.fromisoformat(row["timestamp"])
        if timestamp.tzinfo is None:
            raise ValueError("Kraken funding timestamp is missing its timezone")
        hour, remainder = divmod(timestamp.timestamp(), 3600)
        hour = int(hour)
        if not start <= hour < end - 1 or hour not in bars:
            continue
        if remainder:
            raise ValueError("Kraken funding timestamp is misaligned")
        absolute_rate = float(row["fundingRate"])
        if not isfinite(absolute_rate):
            raise ValueError("Non-finite Kraken funding")
        # Funding accrues over the hour following the timestamp. The engine multiplies by
        # the preceding close, so scaling here recovers observed USD per full contract-hour.
        effective = hour + 1
        observation = Funding(effective * HOUR_MS, absolute_rate / bars[hour].close)
        if effective in funding and funding[effective] != observation:
            raise ValueError("Conflicting Kraken funding observations")
        funding[effective] = observation
    return funding


def _download(client, coin, instrument):
    symbol = instrument["symbol"]
    decimals = instrument["contractValueTradePrecision"]
    if instrument["contractSize"] != 1 or type(decimals) is not int or not 0 <= decimals <= 6:
        raise ValueError(f"Unexpected Kraken contract units or quantity precision for {symbol}")
    candles = _kraken_candles(client, symbol)
    observations = _public_get(
        client, FUNDING_PATH, {"symbol": symbol, "from": START * 3600, "to": END * 3600}
    )
    funding = _kraken_funding(observations["rates"], candles, START, END)
    return MarketHistory(coin, decimals, False, None, candles, funding)


def _hyperliquid_histories(archive):
    metadata, census_sources = _metadata(archive)
    inventory = _inventory(archive, False)
    histories, quality = {}, {}
    for coin in COINS:
        if coin not in metadata:
            continue
        shards = inventory.get(coin, {})
        bars, bad_bar_rows, bar_conflicts = _series(
            shards.get("hour", ()), "hour", coin, END * HOUR_MS
        )
        funding, bad_fund_rows, fund_conflicts = _series(
            shards.get("fund", ()), "fund", coin, END * HOUR_MS
        )
        histories[coin] = MarketHistory(
            coin, metadata[coin]["szDecimals"], False, None, bars, funding
        )
        quality[coin] = dict(
            invalid_candles=bad_bar_rows,
            conflicting_candles=bar_conflicts,
            invalid_funding=bad_fund_rows,
            conflicting_funding=fund_conflicts,
        )
    return histories, quality, census_sources


def _closed_plans(histories, start, end):
    plans, decisions = {}, {}
    for coin, history in histories.items():
        hours = {
            stamp * HOUR_MS: _Bar(
                *(Decimal(str(value)) for value in (bar.high, bar.low, bar.close, bar.volume))
            )
            for stamp, bar in history.bars.items()
            if FIRST <= stamp < end
        }
        market = MarketMeta(coin, history.decimals, 1, True)
        for stamp in range(start, end, DAY_HOURS):
            if not all(hour in history.bars for hour in range(stamp - 30 * DAY_HOURS, stamp)):
                continue
            boundary = stamp * HOUR_MS
            _, eligible = _liquidity(hours, boundary)
            decision = _strategy(ForwardDecision(market, None), hours, boundary, eligible)
            decisions[stamp, coin] = decision
            if decision.plan is not None:
                plans.setdefault(stamp, []).append(decision.plan)
    for rows in plans.values():
        rows.sort(key=lambda plan: (-plan.signal_strength, plan.coin))
    return plans, decisions


def _venue_plans(plans, source, venues):
    # Every clock and direction comes from the same closed Hyperliquid candles. Transport the
    # percentage entry cap and ATR to each venue's own prior close. Drop a signal on both
    # venues if either venue lacks that close.
    routed = {venue: {} for venue in venues}
    for hour, orders in plans.items():
        for order in orders:
            if any(hour - 1 not in market[order.coin].bars for market in venues.values()):
                continue
            original_close = source[order.coin].bars[hour - 1].close
            for venue, market in venues.items():
                close = market[order.coin].bars[hour - 1].close
                leg = replace(
                    order,
                    limit=close * (1 + float(ENTRY_CAP) * order.side),
                    atr=order.atr * close / original_close,
                )
                routed[venue].setdefault(hour, []).append(TradePlan((leg,), "daily-matched-venue"))
    return routed


def _score_window(venues, start, end, capital):
    coins = [
        coin
        for coin in COINS
        if all(
            coin in history
            and sum(hour not in history[coin].bars for hour in range(start, end)) <= 6
            for history in venues.values()
        )
    ]
    if not coins:
        raise ValueError(f"No matching hourly price histories for {_utc(start)} to {_utc(end)}")
    matched = {venue: {coin: markets[coin] for coin in coins} for venue, markets in venues.items()}
    plans, decisions = _closed_plans(matched["hyperliquid"], start, end)
    routed = _venue_plans(plans, matched["hyperliquid"], matched)

    def close_on_channel(plan, _submitted, current):
        leg = plan.legs[0]
        decision = decisions.get((current, leg.coin))
        if decision is not None and (decision.exit_long if leg.side == 1 else decision.exit_short):
            return "10-day channel exit"
        return None

    scores = {}
    for venue, histories in matched.items():
        taker, maker = VENUE_FEES[venue]
        costs = {
            label: replace(cost, taker_fee_bps=taker, maker_fee_bps=maker)
            for label, cost in COSTS.items()
        }
        books = run_book(
            histories,
            routed[venue],
            start,
            end,
            RiskLimits(initial_equity=capital, max_leg_fraction=0.25),
            exit_signal=close_on_channel,
            profit_lock=0.25,
            cost_models=costs,
        )
        scores[venue] = {label: _summary(book, histories) for label, book in books.items()}
    return {
        "start_utc": _utc(start),
        "end_exclusive_utc": _utc(end),
        "markets": coins,
        "excluded_from_match": [coin for coin in COINS if coin not in coins],
        "signal_opportunities": sum(map(len, plans.values())),
        "matched_opportunities": sum(map(len, routed["hyperliquid"].values())),
        "first_matched_signal_utc": _utc(min(routed["hyperliquid"]))
        if routed["hyperliquid"]
        else None,
        "results": scores,
    }


def _summary(book, histories):
    result = {
        field: book[field]
        for field in (
            "net",
            "gross",
            "funding",
            "fees",
            "slippage",
            "count",
            "intrabar_drawdown",
            "net_without_best_trade_and_coin",
            "terminal_proxy_exits",
        )
    }
    result["held_missing_full_hour_funding"] = [
        {"coin": leg["coin"], "effective_utc": _utc(hour)}
        for trade in book["trades"]
        for leg in trade["legs"]
        for hour in range(leg["entered_ms"] // HOUR_MS + 1, leg["exited_ms"] // HOUR_MS + 1)
        if hour not in histories[leg["coin"]].funding
    ]
    return result


def _coverage(histories):
    return {
        coin: {
            "candles_by_window": {
                window: sum(hour in history.bars for hour in range(start, end))
                for window, (start, end) in WINDOWS.items()
            },
            "pre_discovery_history_hours": sum(
                hour in history.bars for hour in range(FIRST, START)
            ),
            "full_hour_funding_missing_count": sum(
                hour not in history.funding for hour in range(START + 1, END)
            ),
            "full_hour_funding_gaps_with_candles_utc": [
                _utc(hour)
                for hour in range(START + 1, END)
                if hour not in history.funding and hour - 1 in history.bars
            ],
            "size_decimals": history.decimals,
        }
        for coin, history in histories.items()
    }


def compare(archive, capital):
    hyperliquid, archive_quality, census_paths = _hyperliquid_histories(archive)
    with httpx.Client(timeout=14) as client:
        kraken_census = {
            row["symbol"]: row for row in _public_get(client, INSTRUMENTS_PATH)["instruments"]
        }
        instruments = {
            coin: kraken_census[f"PF_{coin}USD"]
            for coin in COINS
            if f"PF_{coin}USD" in kraken_census
        }
        with ThreadPoolExecutor(max_workers=5) as pool:
            downloads = [
                pool.submit(_download, client, coin, row) for coin, row in instruments.items()
            ]
            histories = [download.result() for download in downloads]
            kraken = {history.coin: history for history in histories}
    venues = {"hyperliquid": hyperliquid, "kraken": kraken}
    return {
        "archive_cutoff_exclusive_utc": _utc(END),
        "starting_collateral": capital,
        "leverage": 1,
        "sources": {
            "hyperliquid_archive": str(archive),
            "hyperliquid_census_files": census_paths,
            "kraken_candles": KRAKEN + CANDLES_PATH,
            "kraken_instruments": KRAKEN + INSTRUMENTS_PATH,
            "kraken_funding": KRAKEN + FUNDING_PATH,
            "kraken_retrieved_utc": datetime.now(UTC).isoformat(),
        },
        "assumptions": {
            "signal": "Same Hyperliquid 00:00 UTC 20-day breakout, SMA50 and 10-day exit on both venues. The signal waits for its own 51 closed days. The comparison uses 13 preset non-BTC contracts and their current census identities.",
            "prices": "Completed hourly trade OHLC only; historical spread and market impact use shared replay proxies. The engine uses the Hyperliquid tick approximation on both venues and venue-specific size precision, with a common $10 floor. Kraken reported tick sizes are recorded below.",
            "funding": "Kraken public rates are USD per contract per hour starting at their timestamp. Full-hour funding is booked at the subsequent boundary. Partial-hour accrual and inferred gap fills are omitted. The endpoint may return out-of-window data, whose timestamps are filtered before reading rate values.",
            "collateral": "USD and USDC at parity. Conversion, transfers and venue-specific margin tiers are outside this replay.",
        },
        "cost_models": {
            venue: {
                label: asdict(replace(model, taker_fee_bps=fees[0], maker_fee_bps=fees[1]))
                for label, model in COSTS.items()
            }
            for venue, fees in VENUE_FEES.items()
        },
        "coverage": {venue: _coverage(histories) for venue, histories in venues.items()},
        "hyperliquid_archive_quality": archive_quality,
        "kraken_instrument_ticks": {coin: row["tickSize"] for coin, row in instruments.items()},
        "windows": {
            name: _score_window(venues, start, end, capital)
            for name, (start, end) in WINDOWS.items()
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--capital", type=float, default=500)
    args = parser.parse_args(argv)
    if not isfinite(args.capital) or args.capital <= 0:
        parser.error("Starting collateral must be positive and finite")
    report = compare(args.archive, args.capital)
    write_json(args.output, report)
    for name, window in report["windows"].items():
        for venue, labels in window["results"].items():
            for label, result in labels.items():
                print(name, venue, label, f"net={result['net']:.2f}", f"trades={result['count']}")


if __name__ == "__main__":
    main()
