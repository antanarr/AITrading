from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv
from pydantic import ValidationError

from .advisor import Advisor
from .credentials import credential_status, load_credentials, open_setup
from .engine import PaperEngine
from .models import Assessment, Quote, Settings, Trade
from .runner import Runner
from .sources import (
    Birdeye,
    Nansen,
    SolanaInventory,
    SourceError,
    imported_candidates,
    merge_candidates,
)
from .store import Store

ROOT = Path(__file__).resolve().parent.parent


def emit(data):
    print(json.dumps(data, indent=2, default=str))


def replay(path, engine, advisor):
    last_time = 0
    for index, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        now = row["observed_at"]
        if not isinstance(now, int) or now < last_time:
            raise ValueError(f"Replay observation times must increase (line {index})")
        last_time = now
        t, q = Trade.model_validate(row["trade"]), Quote.model_validate(row["quote"])
        if "assessment" in row:
            assessment = Assessment.model_validate(row["assessment"])
        else:
            assessment = advisor.assess(engine.advisor_context(t.wallet, now), now)
        engine.process(t, q, now, assessment)
    return engine.store.report()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Personal wallet copying with simulated funds")
    parser.add_argument("--config", type=Path, default=ROOT / "copybot.yaml")
    parser.add_argument("--db", type=Path, default=ROOT / "state/paper.sqlite")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="Run an offline demonstration with fictional wallets")
    cp = sub.add_parser(
        "credentials", help="Open secure macOS credential setup or show saved status"
    )
    cp.add_argument("action", choices=["setup", "status"])
    rp = sub.add_parser("replay", help="Replay normalized observations into the paper ledger")
    rp.add_argument("file", type=Path)
    for name in ("status", "pause", "resume"):
        sub.add_parser(name)
    dp = sub.add_parser("discover", help="Find candidates; does not enable copying")
    dp.add_argument("--source", action="append", choices=["birdeye", "nansen"], default=[])
    dp.add_argument("--import", dest="imports", action="append", type=Path, default=[])
    dp.add_argument("--limit", type=int, choices=range(1, 101), default=20, metavar="1..100")
    dp.add_argument("--output", type=Path)
    wp = sub.add_parser("watch", help="Monitor real wallets while trading only simulated funds")
    wp.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env")
    try:
        if args.command == "credentials":
            if args.action == "setup":
                open_setup()
                emit({"credential_window": "opened", "storage": "macOS Keychain"})
            else:
                emit(credential_status())
            return 0
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            if args.command == "discover":
                candidates = []
                if not args.source and not args.imports:
                    raise ValueError("Choose --source or --import")
                load_credentials([f"{source.upper()}_API_KEY" for source in args.source])
                for name in args.source:
                    source = (
                        Birdeye(client, os.getenv("BIRDEYE_API_KEY", ""))
                        if name == "birdeye"
                        else Nansen(client, os.getenv("NANSEN_API_KEY", ""))
                    )
                    candidates.extend(source.discover(limit=args.limit))
                for path in args.imports:
                    candidates.extend(imported_candidates(path))
                result = [c.model_dump(mode="json") for c in merge_candidates(candidates)]
                if args.output:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(json.dumps(result, indent=2) + "\n")
                emit(result)
                return 0
            cfg_path = ROOT / "examples/demo.yaml" if args.command == "demo" else args.config
            cfg = Settings.model_validate(yaml.safe_load(cfg_path.read_text()))
            if args.command in {"watch", "replay"} and cfg.advisor == "openai":
                load_credentials(["OPENAI_API_KEY", "COPYBOT_OPENAI_MODEL"])
            if args.command == "watch":
                load_credentials(["BIRDEYE_API_KEY", "SOLANA_RPC_URL"])
            if args.command in {"status", "pause", "resume"} and not args.db.exists():
                raise ValueError("Paper database does not exist yet; run replay or watch first")
            store = Store(":memory:" if args.command == "demo" else args.db, cfg.initial_cash_usd)
            try:
                engine = PaperEngine(cfg, store)
                if args.command == "status":
                    emit(store.report())
                elif args.command in {"pause", "resume"}:
                    store.put("paused", "true" if args.command == "pause" else "false")
                    emit({"mode": "paper", "new_entries_paused": args.command == "pause"})
                elif args.command in {"replay", "demo"}:
                    advisor = Advisor(cfg.advisor, cfg.advisor_ttl_seconds, client)
                    path = ROOT / "examples/demo.jsonl" if args.command == "demo" else args.file
                    emit(replay(path, engine, advisor))
                elif args.command == "watch":
                    store.acquire_watch_lock()
                    if not cfg.traders:
                        raise ValueError(
                            "Add selected wallets, allocations, and reference equity to copybot.yaml first"
                        )
                    if any(t.wallet.startswith("demo-") for t in cfg.traders):
                        raise ValueError("Fictional demo wallets cannot be used in watch mode")
                    inventory = SolanaInventory(client, os.getenv("SOLANA_RPC_URL", ""))
                    runner = Runner(
                        engine,
                        Birdeye(client, os.getenv("BIRDEYE_API_KEY", "")),
                        inventory,
                        Advisor(cfg.advisor, cfg.advisor_ttl_seconds, client),
                    )
                    while True:
                        try:
                            emit({"mode": "paper", "decisions": runner.cycle()})
                        except SourceError as exc:
                            # Keep monitoring exits, but never resume new entries after an outage silently.
                            store.put("paused", "true")
                            store.put("last_error", str(exc))
                            print(f"Data failure; new entries paused: {exc}", file=sys.stderr)
                            if args.once:
                                return 1
                        if args.once:
                            break
                        time.sleep(cfg.poll_seconds)
            finally:
                store.close()
        return 0
    except (ValueError, OSError, SourceError, ValidationError, KeyError, TypeError) as exc:
        # Validation errors may contain input values, so show a compact category only.
        message = (
            "Invalid provider/configuration data"
            if isinstance(exc, (ValidationError, KeyError, TypeError))
            else str(exc)
        )
        print(f"Copybot: {message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
