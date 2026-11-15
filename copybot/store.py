from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path


class Store:
    """All money values are decimal strings; decisions and fills commit atomically."""

    def __init__(self, path: Path | str, initial_cash: Decimal):
        self.path = str(path)
        self.watch_lock = None
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS positions (
                wallet TEXT, token TEXT, quantity TEXT NOT NULL, cost TEXT NOT NULL,
                PRIMARY KEY(wallet, token));
            CREATE TABLE IF NOT EXISTS decisions (
                event_id TEXT PRIMARY KEY, timestamp INTEGER, wallet TEXT, token TEXT,
                side TEXT, status TEXT, reason TEXT, quantity TEXT, price TEXT,
                fee TEXT, pnl TEXT, advisor TEXT);
            CREATE TABLE IF NOT EXISTS quotes (
                token TEXT PRIMARY KEY, price TEXT, liquidity TEXT, timestamp INTEGER);
        """)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('cash', ?)", (str(initial_cash),))
        self.db.execute(
            "INSERT OR IGNORE INTO meta VALUES ('initial_cash', ?)", (str(initial_cash),)
        )

    def close(self):
        self.db.close()
        if self.watch_lock is not None:
            self.watch_lock.close()

    def acquire_watch_lock(self):
        import fcntl

        self.watch_lock = open(self.path + ".watch.lock", "a")
        try:
            fcntl.flock(self.watch_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.watch_lock.close()
            self.watch_lock = None
            raise ValueError("Another watch process is already using this paper account") from None

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))

    def positions(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM positions ORDER BY wallet, token")]

    def position(self, wallet, token):
        row = self.db.execute(
            "SELECT * FROM positions WHERE wallet=? AND token=?", (wallet, token)
        ).fetchone()
        return (Decimal(row["quantity"]), Decimal(row["cost"])) if row else (Decimal(0), Decimal(0))

    def seen(self, event_id):
        return (
            self.db.execute("SELECT 1 FROM decisions WHERE event_id=?", (event_id,)).fetchone()
            is not None
        )

    def record(
        self,
        trade,
        status,
        reason,
        quantity=Decimal(0),
        price=Decimal(0),
        fee=Decimal(0),
        pnl=Decimal(0),
        advisor=None,
    ):
        self.db.execute(
            "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                trade.event_id,
                trade.timestamp,
                trade.wallet,
                trade.token,
                trade.side,
                status,
                reason,
                str(quantity),
                str(price),
                str(fee),
                str(pnl),
                json.dumps(advisor or {}),
            ),
        )

    def report(self):
        positions = self.positions()
        marks = {r["token"]: dict(r) for r in self.db.execute("SELECT * FROM quotes")}
        equity = Decimal(self.get("cash"))
        for position in positions:
            mark = marks.get(position["token"])
            if mark is None:
                equity = None
                break
            equity += Decimal(position["quantity"]) * Decimal(mark["price"])
        return {
            "mode": "paper",
            "cash_usd": self.get("cash"),
            "marked_equity_usd": str(equity) if equity is not None else None,
            "marks": list(marks.values()),
            "paused": self.get("paused", "false") == "true",
            "daily_halt": self.get("daily_halt", "false") == "true",
            "last_error": self.get("last_error"),
            "last_advisor_error": self.get("last_advisor_error"),
            "last_successful_cycle": self.get("last_successful_cycle"),
            "unsupported_transactions": [
                dict(r)
                for r in self.db.execute(
                    "SELECT key, value FROM meta WHERE key LIKE 'unsupported:%' ORDER BY rowid DESC LIMIT 20"
                )
            ],
            "positions": positions,
            "recent_decisions": [
                dict(r)
                for r in self.db.execute("SELECT * FROM decisions ORDER BY rowid DESC LIMIT 30")
            ],
        }
