from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List

import ccxt
import numpy as np
import pandas as pd

from .config import Config

LOGGER = logging.getLogger(__name__)


class MarketDataClient:
    def __init__(self, cfg: Config, exchange: ccxt.Exchange) -> None:
        self.cfg = cfg
        self.exchange = exchange

    def fetch_ohlcv(self, symbol: str) -> pd.DataFrame:
        LOGGER.debug("Fetching OHLCV for %s", symbol)
        raw: List[List[float]] = self.exchange.fetch_ohlcv(
            symbol, timeframe=self.cfg.data.timeframe, limit=self.cfg.data.lookback
        )
        df = pd.DataFrame(
            raw,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df.set_index("timestamp")

    @staticmethod
    def compute_features(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            raise ValueError("No market data available")

        df = df.copy()
        df["rsi"] = MarketDataClient._compute_rsi(df["close"], period=14)
        df["momentum_5"] = df["close"].pct_change(periods=5)
        df.dropna(inplace=True)
        return df

    @staticmethod
    def latest_snapshot(df: pd.DataFrame) -> dict:
        latest = df.iloc[-1]
        return {
            "open": float(latest["open"]),
            "high": float(latest["high"]),
            "low": float(latest["low"]),
            "close": float(latest["close"]),
            "volume": float(latest["volume"]),
            "rsi": float(latest["rsi"]),
            "momentum_5": float(latest["momentum_5"]),
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }

    @staticmethod
    def _compute_rsi(series: pd.Series, period: int) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(window=period, min_periods=period).mean()
        avg_loss = loss.rolling(window=period, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace({0: np.nan})
        rsi = 100 - (100 / (1 + rs))
        return rsi.fillna(method="bfill").fillna(50)
