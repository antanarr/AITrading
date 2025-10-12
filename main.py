from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv

from ai_trading.config import Config, DEFAULT_CONFIG_PATH
from ai_trading.engine import TradingEngine


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI ensemble trading bot")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Config file")
    parser.add_argument("--symbols", type=str, default="BTC/USDT", help="Comma separated symbols")
    parser.add_argument("--timeframe", type=str, default="5m", help="Timeframe to trade")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--paper", action="store_true", help="Run in paper trading mode")
    mode.add_argument("--live", action="store_true", help="Run with live orders")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single iteration instead of continuous loop",
    )
    parser.add_argument(
        "--sleep",
        type=int,
        default=60,
        help="Seconds to wait between iterations in live loop",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    load_dotenv()
    cfg = Config.load(args.config)
    cfg.symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    cfg.data.timeframe = args.timeframe
    paper = True if args.paper or not args.live else False

    engine = TradingEngine.from_env(cfg=cfg, paper=paper)

    if args.once:
        await engine.run_once()
        return

    while True:
        await engine.run_once()
        await asyncio.sleep(args.sleep)


if __name__ == "__main__":
    asyncio.run(main())
