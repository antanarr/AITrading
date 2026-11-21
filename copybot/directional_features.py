"""Build causal Hyperliquid features and delayed open-to-open research outcomes."""

from collections.abc import Mapping
from math import isfinite

import numpy as np
import pandas as pd

from .research_models import HOUR_MS, Bar, MarketHistory, excluded_symbol

HORIZONS = (4, 12, 24, 48)
WARMUP_HOURS = 16 * 24
QUARTER_MS = HOUR_MS // 4
MINIMUM_PEERS = 5
BAR_COLUMNS = ["open", "high", "low", "close", "volume"]
PEER_RETURN_WINDOWS = (6, 24, 72)


def _bars(source: Mapping[int, Bar], first: int, end: int, step: int = 1) -> pd.DataFrame:
    index = pd.RangeIndex(first, end, step)
    rows = {
        key: (bar.open, bar.high, bar.low, bar.close, bar.volume)
        for key, bar in source.items()
        if first <= key < end and (key - first) % step == 0
    }
    return pd.DataFrame.from_dict(rows, orient="index", columns=BAR_COLUMNS, dtype=float).reindex(
        index
    )


def _aggregate(hourly: pd.DataFrame, period: int) -> pd.DataFrame:
    grouped = hourly.groupby(hourly.index // period * period, sort=True)
    bars = grouped.agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    # A partially observed candle cannot stand in for a completed larger bar.
    bars.loc[grouped["close"].count() != period, :] = np.nan
    bars.index += period
    return bars


def _divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    return numerator / denominator.where(denominator > 0)


def _chart(bars: pd.DataFrame, resolution: str, windows: tuple[int, ...], trend: int):
    close, volume = bars["close"], bars["volume"]
    high, low = bars["high"], bars["low"]
    previous = close.shift()
    log_close = np.log(close)
    hourly_move = log_close.diff()
    true_range = pd.concat([high - low, (high - previous).abs(), (low - previous).abs()], axis=1)
    true_range = true_range.max(axis=1, skipna=False)
    vwap = _divide(
        (((high + low + close) / 3) * volume).rolling(trend).sum(), volume.rolling(trend).sum()
    )
    gains = close.diff().clip(lower=0).rolling(trend).sum()
    losses = -close.diff().clip(upper=0).rolling(trend).sum()
    center = close.rolling(trend).mean()
    max_high, min_low = high.rolling(trend).max(), low.rolling(trend).min()
    log_volume = np.log1p(volume * close)
    prior_volume = log_volume.shift().rolling(trend)
    metrics = {
        f"return_{window}": (log_close - log_close.shift(window)).where(
            close.rolling(window + 1).count() == window + 1
        )
        for window in windows
    }
    metrics.update(
        {
            "body": np.log(close / bars["open"]),
            "atr": true_range.rolling(trend).mean() / close,
            "realized_vol": hourly_move.rolling(trend).std(ddof=0),
            "trend_gap": _divide(close, center) - 1,
            "vwap_gap": _divide(close, vwap) - 1,
            "rsi": _divide(gains - losses, gains + losses).where(gains + losses != 0, 0),
            "range_location": _divide(close - min_low, max_high - min_low) - 0.5,
            "volume_surprise": _divide(log_volume - prior_volume.mean(), prior_volume.std(ddof=0)),
        }
    )
    return pd.DataFrame({f"tech_{resolution}_{name}": values for name, values in metrics.items()})


def _sample(features: pd.DataFrame, hours: pd.Index, boundaries: pd.Index) -> pd.DataFrame:
    sampled = features.reindex(boundaries)
    sampled.index = hours
    return sampled


def _chart_features(hourly: pd.DataFrame, history: MarketHistory, first: int, end: int):
    hours = hourly.index
    closed = hourly.copy(deep=False)
    closed.index += 1
    features = [
        _chart(closed, "1h", (1, 3, 6, 12, 24, 72, 168), 24).reindex(hours),
        _sample(_chart(_aggregate(hourly, 4), "4h", (1, 3, 6, 12, 18), 14), hours, hours // 4 * 4),
        _sample(_chart(_aggregate(hourly, 24), "1d", (1, 3, 7, 14), 7), hours, hours // 24 * 24),
    ]
    # Preserve the same columns for contracts without verified quarter candles.
    quarters = _bars(history.quarter_bars, first * HOUR_MS, end * HOUR_MS, QUARTER_MS)
    quarters.index += QUARTER_MS
    features.append(_sample(_chart(quarters, "15m", (1, 4, 12, 24), 24), hours, hours * HOUR_MS))
    return pd.concat(features, axis=1)


def _funding_rows(history: MarketHistory, hours: pd.Index) -> pd.DataFrame:
    rows = {
        hour: (row.time_ms, row.rate, row.premium, row.time_ms % HOUR_MS == 0)
        for hour, row in history.funding.items()
        if hours[0] <= hour <= hours[-1] and row.time_ms // HOUR_MS == hour
    }
    return pd.DataFrame.from_dict(
        rows, orient="index", columns=["time", "rate", "premium", "boundary"]
    ).reindex(hours)


def _carry_features(funding: pd.DataFrame) -> pd.DataFrame:
    # Even a funding settlement stamped exactly at the decision is excluded.
    rate = funding["rate"].shift().astype(float) * 10_000
    premium = funding["premium"].shift().astype(float) * 10_000
    return pd.DataFrame(
        {
            "carry_funding_bps": rate,
            "carry_funding_mean_6h": rate.rolling(6).mean(),
            "carry_funding_mean_24h": rate.rolling(24).mean(),
            "carry_funding_change_6h": rate.diff(6),
            "carry_premium_bps": premium,
            "carry_premium_mean_6h": premium.rolling(6).mean(),
        }
    )


def _forward_outcomes(hourly: pd.DataFrame, funding: pd.DataFrame) -> pd.DataFrame:
    hours = hourly.index
    entry_price = hourly["open"].shift(-1).where(hourly["volume"].shift(-1) > 0)
    fields = {"entry_hour": hours + 1, "entry_price": entry_price}
    # Match the execution replay: pay on settlements after entry through exit,
    # using the previous completed close as a proxy for the settlement oracle.
    cash_per_unit = -funding["rate"].astype(float) * hourly["close"].shift()
    at_boundary = funding["boundary"].eq(True)
    boundary_cash = cash_per_unit.where(at_boundary, 0)
    for horizon in HORIZONS:
        shift = -(horizon + 1)
        exit_open = hourly["open"].shift(shift).where(hourly["volume"].shift(shift) > 0)
        complete_prices = hourly["open"].rolling(horizon + 1).count().shift(shift).eq(horizon + 1)
        complete_funding = funding["time"].rolling(horizon + 1).count().shift(shift).eq(horizon + 1)
        accrued = (
            cash_per_unit.rolling(horizon).sum().shift(-horizon)
            - boundary_cash.shift(-1)
            + boundary_cash.shift(shift)
        )
        fields[f"exit_hour_{horizon}"] = hours + horizon + 1
        fields[f"price_return_{horizon}"] = (exit_open / entry_price - 1).where(complete_prices)
        fields[f"funding_return_{horizon}"] = (accrued / entry_price).where(
            complete_prices & complete_funding & exit_open.notna()
        )
    return pd.DataFrame(fields, index=hours)


def _market_rows(
    history: MarketHistory, start: int, end: int, minimum_turnover: float, broad: bool
):
    first = max(0, start - WARMUP_HOURS)
    hourly = _bars(history.bars, first, end)
    hours = hourly.index
    age = pd.Series(np.nan, index=hours)
    if history.listing_hour is not None:
        age = pd.Series(hours - history.listing_hour, index=hours, dtype=float)
        age = age.where(age >= 0)
    recent = age.between(12, 14 * 24)
    closed_notional = (hourly["close"] * hourly["volume"]).shift()
    turnover_12 = closed_notional.rolling(12).sum()
    turnover_24 = closed_notional.rolling(24).sum()
    activity_12 = closed_notional.gt(0).rolling(12).sum()
    activity_24 = closed_notional.gt(0).rolling(24).sum()
    short_history = recent & age.lt(24) & turnover_24.isna()
    turnover = turnover_24.where(~short_history, turnover_12)
    activity = activity_24.where(~short_history, activity_12)
    hourly_turnover = closed_notional.where(closed_notional > 0)
    median_12 = hourly_turnover.rolling(12, min_periods=9).median()
    median_24 = hourly_turnover.rolling(24, min_periods=9).median()
    typical_turnover = median_24.where(~short_history, median_12)
    eligible = (
        (recent | history.is_meme | broad)
        & closed_notional.gt(0)
        & turnover.ge(minimum_turnover)
        & activity.ge(9)
        & typical_turnover.ge(minimum_turnover / 72)
        & (hours >= start)
    )
    if not eligible.any():
        return None
    fields = pd.DataFrame(
        {
            "hour": hours,
            "coin": history.coin,
            "group": "meme" if history.is_meme else "other",
            "decimals": history.decimals,
            "market_is_meme": int(history.is_meme),
            "market_is_recent": recent.astype(int),
            "market_log_listing_age": np.log1p(age),
            "market_log_turnover": np.log1p(turnover),
            "market_active_hours": activity,
        },
        index=hours,
    )
    funding = _funding_rows(history, hours)
    fields.loc[recent & ~fields.market_is_meme.astype(bool), "group"] = "recent"
    return pd.concat(
        [
            fields,
            _chart_features(hourly, history, first, end),
            _carry_features(funding),
            _forward_outcomes(hourly, funding),
        ],
        axis=1,
    ).loc[eligible]


def _peers(rows: pd.DataFrame, broad: bool) -> pd.DataFrame:
    peers = rows if broad else rows.loc[rows["market_is_meme"].eq(1)]
    peers = peers.set_index("hour")
    market = {}
    for window in PEER_RETURN_WINDOWS:
        column = f"tech_1h_return_{window}"
        peer_returns = peers[column].dropna().groupby(level=0)
        count = peer_returns.count()
        good = count >= MINIMUM_PEERS
        median = peer_returns.median().where(good)
        market[f"market_meme_median_{window}h"] = rows["hour"].map(median)
        market[f"market_meme_breadth_{window}h"] = rows["hour"].map(
            (peers[column].dropna().gt(0).groupby(level=0).mean()).where(good)
        )
        market[f"market_relative_return_{window}h"] = rows[column] - rows["hour"].map(median)
        market[f"market_return_rank_{window}h"] = rows.groupby("hour")[column].rank(pct=True)
    dispersion = peers["tech_1h_return_24"].groupby(level=0)
    market["market_meme_dispersion_24h"] = rows["hour"].map(
        dispersion.std(ddof=0).where(dispersion.count() >= MINIMUM_PEERS)
    )
    volatility = peers["tech_1h_realized_vol"].groupby(level=0)
    market["market_meme_realized_vol"] = rows["hour"].map(
        volatility.median().where(volatility.count() >= MINIMUM_PEERS)
    )
    market["market_meme_count"] = rows["hour"].map(peers.groupby(level=0).size()).fillna(0)
    market["carry_funding_rank_6h"] = rows.groupby("hour")["carry_funding_mean_6h"].rank(pct=True)
    return pd.concat([rows, pd.DataFrame(market)], axis=1)


def build_frame(
    histories: Mapping[str, MarketHistory],
    start: int,
    end: int,
    *,
    minimum_turnover: float = 1_000_000,
    broad: bool = False,
) -> pd.DataFrame:
    """Return one row per eligible decision hour and meme or recent-listing market.

    Technical, peer and carry inputs use observations strictly before the
    decision. The modeled entry is the first trade of the next hour and the
    exits are first trades after the configured holding horizons. Missing
    execution observations produce unavailable returns and keep the row in
    the decision universe. All observations come from before the exclusive end.
    A chronological split must also require `exit_hour_<horizon> < split`.
    """
    if type(start) is not int or type(end) is not int or not 0 < start < end:
        raise ValueError("Expected increasing positive UTC epoch hours")
    if not isfinite(minimum_turnover) or minimum_turnover <= 0:
        raise ValueError("Expected finite positive minimum turnover")
    frames = []
    for history in histories.values():
        if not history.bars or excluded_symbol(history.coin) or ":" in history.coin:
            continue
        rows = _market_rows(history, start, end, minimum_turnover, broad)
        if rows is not None:
            frames.append(rows)
    if not frames:
        raise ValueError("No eligible liquid market decisions in the archive window")
    market_rows = pd.concat(frames, ignore_index=True).sort_values(
        ["hour", "coin"], ignore_index=True
    )
    if not broad:
        return _peers(market_rows, False)
    wide = _peers(market_rows, True)
    # Older research artifacts use meme-specific columns. The broad model records its peer set.
    return wide.rename(
        columns={
            col: col.replace("market_meme_", "market_universe_")
            for col in wide
            if col.startswith("market_meme_")
        }
    )
