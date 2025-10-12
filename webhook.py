from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from ai_trading.config import Config, DEFAULT_CONFIG_PATH
from ai_trading.engine import TradingEngine
from ai_trading.types import TradeAction

LOGGER = logging.getLogger(__name__)
app = FastAPI()

_engine: Optional[TradingEngine] = None
_engine_lock = asyncio.Lock()


class SignalPayload(BaseModel):
    symbol: str
    side: str
    reason: Optional[str] = None
    confidence: float
    stop_pct: float
    take_pct: float


async def get_engine() -> TradingEngine:
    global _engine  # pylint: disable=global-statement
    async with _engine_lock:
        if _engine is None:
            config_path = os.getenv("AITRADING_CONFIG", DEFAULT_CONFIG_PATH)
            cfg = Config.load(config_path)
            _engine = TradingEngine.from_env(cfg=cfg, paper=not bool(os.getenv("AITRADING_LIVE")))
        return _engine


def verify_secret(secret: Optional[str] = Header(default=None, alias="X-Webhook-Secret")) -> None:
    cfg = Config.load(DEFAULT_CONFIG_PATH)
    if cfg.webhook_secret and secret != cfg.webhook_secret:
        LOGGER.warning("Invalid webhook secret")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid secret")


@app.post("/signal")
async def receive_signal(payload: SignalPayload, engine: TradingEngine = Depends(get_engine)):
    verify_secret()
    side = payload.side.lower()
    if side not in {"buy", "sell"}:
        raise HTTPException(status_code=400, detail="side must be buy or sell")
    decision = TradeAction.BUY if side == "buy" else TradeAction.SELL
    LOGGER.info("Received webhook signal for %s: %s", payload.symbol, side)
    try:
        ohlcv = engine.market_data.fetch_ohlcv(payload.symbol)
        price = float(ohlcv["close"].iloc[-1])
    except Exception as exc:  # pylint: disable=broad-except
        LOGGER.exception("Failed to fetch latest price: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to fetch price") from exc
    engine.execution.execute(
        payload.symbol,
        decision,
        price=price,
        stop_pct=payload.stop_pct,
        take_pct=payload.take_pct,
    )
    return {"status": "ok"}
