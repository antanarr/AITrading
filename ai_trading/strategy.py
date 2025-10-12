from __future__ import annotations

from textwrap import dedent
from typing import Dict

import pandas as pd

from .types import TradeAction


PROMPT_TEMPLATE = dedent(
    """
    You are part of an ensemble of trading models. You are given the latest market snapshot
    for {symbol} on the {timeframe} timeframe. Decide whether to buy, sell, or hold.

    Reply strictly as JSON with keys: action (buy/sell/hold), confidence (0-1 float),
    stop_pct (decimal percent of entry price for stop loss), take_pct (decimal percent for
    take profit), and reason (concise string).

    Market data:
    price_open: {open}
    price_high: {high}
    price_low: {low}
    price_close: {close}
    volume: {volume}
    rsi: {rsi}
    momentum_5: {momentum_5}
    timestamp: {timestamp}
    """
).strip()


def build_prompt(symbol: str, timeframe: str, snapshot: Dict[str, float]) -> str:
    prompt = PROMPT_TEMPLATE.format(symbol=symbol, timeframe=timeframe, **snapshot)
    return prompt
