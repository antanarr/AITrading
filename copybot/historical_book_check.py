"""Bounded, offline order-book checks for two preselected Development cases."""

import argparse
import gzip
import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .forward_market import BookLevel, L2Book, sweep_ioc
from .forward_paper import CAPITAL, DECISION_WINDOW_MS, MIN_NOTIONAL, Portfolio
from .forward_signals import ENTRY_CAP, ForwardDecision, MarketMeta, _Bar, _strategy
from .perps import number
from .research_data import ARCHIVE_END_HOUR, load_archive
from .research_models import HOUR_MS

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MILLISECOND = timedelta(milliseconds=1)
WLD_BOUNDARY = datetime(2026, 7, 1, tzinfo=UTC)
VVV_STRESS_SIZE = Decimal("8.12")
QUOTE_WAIT_MS = 10_000
MAX_RAW_BYTES = 2 * 1024 * 1024
DELAYS = (0, 8, 12, 15)


def epoch_ms(value):
    return (datetime.fromisoformat(value) - EPOCH) // MILLISECOND


def read_capture(directory, receipt, coin):
    name = receipt["file"]
    if Path(name).name != name or not name.endswith(".log.gz"):
        raise ValueError("Expected a local raw public market capture")
    start = epoch_ms(receipt["window_start"])
    end = epoch_ms(receipt["window_end_exclusive"])
    if not 0 < start < end <= ARCHIVE_END_HOUR * HOUR_MS:
        raise ValueError("Capture must stay inside the Development archive cutoff")
    payload = (directory / name).read_bytes()
    if len(payload) != receipt["bytes"] or hashlib.sha256(payload).hexdigest() != receipt["sha256"]:
        raise ValueError(f"Capture receipt integrity check failed for {name}")
    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as archive:
        text = archive.read(MAX_RAW_BYTES + 1)
    if len(text) > MAX_RAW_BYTES:
        raise ValueError("Capture exceeds the bounded pilot size")
    books, marks = [], []
    previous = start
    for line in text.decode("utf-8").splitlines():
        if not line:
            continue
        stamp, payload = line.split(" ", 1)
        received = epoch_ms(stamp)
        if not previous <= received < end:
            raise ValueError(f"Capture order or timestamp mismatch in {name}")
        previous = received
        row = json.loads(payload)
        data = row["data"]
        if data["coin"] != coin:
            raise ValueError(f"Unexpected instrument in {name}")
        if row["channel"] == "l2Book":
            bids, asks = [
                tuple(BookLevel(number(level["px"]), number(level["sz"])) for level in side)
                for side in data["levels"]
            ]
            books.append(L2Book(coin, data["time"], received, bids, asks))
        elif row["channel"] == "activeAssetCtx":
            context = data["ctx"]
            mark, oracle = number(context["markPx"]), number(context["oraclePx"])
            if mark <= 0 or oracle <= 0:
                raise ValueError("Invalid mark or oracle in capture")
            marks.append((received, mark, oracle))
        else:
            raise ValueError(f"Unexpected data channel in {name}")
    return books, marks


def first_book(books, arrival):
    for book in books:
        if book.received_ms > arrival + QUOTE_WAIT_MS:
            break
        if arrival <= book.received_ms and book.received_ms - book.exchange_ms <= QUOTE_WAIT_MS:
            return book
    return None


def book_evidence(book, arrival):
    bid, ask = book.bids[0].price, book.asks[0].price
    return {
        "exchange_ms": book.exchange_ms,
        "received_ms": book.received_ms,
        "wait_ms": book.received_ms - arrival,
        "exchange_to_capture_ms": book.received_ms - book.exchange_ms,
        "bid_levels": len(book.bids),
        "ask_levels": len(book.asks),
        "bid": bid,
        "ask": ask,
        "spread_bps": (ask - bid) * 20_000 / (ask + bid),
    }


def fill_evidence(fill, side, reference):
    return {
        "limit": fill.limit_price,
        "requested": fill.requested_quantity,
        "filled": fill.quantity,
        "remaining": fill.remaining,
        "notional": fill.notional,
        "average_price": fill.average_price,
        "fee": fill.fee,
        "adverse_price_move_bps": (
            (Decimal(side) * (fill.average_price - reference) * 10_000 / reference)
            if fill.average_price is not None
            else None
        ),
        "levels": [
            {"price": item.price, "quantity": item.quantity, "notional": item.notional}
            for item in fill.fills
        ],
    }


def entry_probe(book, side, reference, atr, decimals, leverage):
    cap = reference * (1 + side * ENTRY_CAP)
    touch = book.asks[0].price if side > 0 else book.bids[0].price
    sizing_price = cap if side > 0 else max(cap, touch)
    quantity = Portfolio(leverage).requested_quantity(side, decimals, sizing_price, atr)
    if quantity * min(cap, touch) < MIN_NOTIONAL:
        return {"status": "minimum_notional", "requested": quantity, "fee": Decimal(0)}
    fill = sweep_ioc(book, buy=side > 0, quantity=quantity, size_decimals=decimals, limit_price=cap)
    status = "unfilled" if not fill.quantity else ("partial" if fill.remaining else "filled")
    return {"status": status, **fill_evidence(fill, side, reference)}


def wld_setup(histories, boundary):
    history = histories["WLD"]
    bars = {
        hour * HOUR_MS: _Bar(
            number(row.high), number(row.low), number(row.close), number(row.volume)
        )
        for hour, row in history.bars.items()
    }
    market = MarketMeta("WLD", history.decimals, None, True)
    decision = _strategy(ForwardDecision(market, None), bars, boundary, entry_allowed=True)
    if decision.plan is None or decision.plan.side != -1:
        raise ValueError(
            "The preselected WLD short did not reproduce from closed Development candles"
        )
    return decision.close, decision.atr, history.decimals


def evaluate(directory, archive):
    sources = json.loads((directory / "july-august-receipt.json").read_text(encoding="utf-8"))
    boundary = (WLD_BOUNDARY - EPOCH) // MILLISECOND
    histories, _ = load_archive(archive, boundary // HOUR_MS)
    reference, atr, decimals = wld_setup(histories, boundary)
    books = [
        book
        for receipt in sources["wld_entry_captures"]
        for book in read_capture(directory, receipt, "WLD")[0]
    ]
    books.sort(key=lambda book: book.received_ms)
    delays = []
    for minutes in DELAYS:
        arrival = boundary + minutes * 60_000
        quote = first_book(books, arrival)
        row = {"delay_minutes": minutes, "scheduled_ms": arrival, "quote": None}
        if quote:
            expired = quote.received_ms >= boundary + DECISION_WINDOW_MS
            row.update(
                quote=book_evidence(quote, arrival),
                within_forward_entry_window=not expired,
                independent_empty_portfolios={
                    str(leverage): entry_probe(quote, -1, reference, atr, decimals, leverage)
                    for leverage in (1, 3)
                },
            )
        delays.append(row)
    vvv = sources["vvv_mark_check"]
    observation = None
    if vvv["capture"] and vvv["first_observed_trigger_in_hour"]:
        vvv_books, marks = read_capture(directory, vvv["capture"], "VVV")
        trigger = epoch_ms(vvv["first_observed_trigger_in_hour"])
        threshold = number(vvv["threshold"])
        observed_mark = next(mark for stamp, mark, _ in marks if stamp == trigger)
        if observed_mark > threshold:
            raise ValueError("The cited VVV mark does not reach the protective threshold")
        exit_quote = first_book(vvv_books, trigger)
        observation = {
            "threshold": threshold,
            "first_observed_trigger_in_hour": vvv["first_observed_trigger_in_hour"],
            "mark": observed_mark,
            "quote": None,
        }
        if exit_quote:
            fill = sweep_ioc(
                exit_quote,
                buy=False,
                quantity=VVV_STRESS_SIZE,
                size_decimals=histories["VVV"].decimals,
                limit_price=observed_mark * Decimal("0.90"),
            )
            observation.update(
                quote=book_evidence(exit_quote, trigger),
                exit_fill=fill_evidence(fill, -1, threshold),
            )
    return {
        "scope": "Two preselected Development observations. Round-trip evidence is incomplete.",
        "quote_policy": "First fresh observed ladder within 10 seconds of hypothetical order arrival.",
        "wld": {
            "boundary": WLD_BOUNDARY.isoformat(),
            "side": "short",
            "closed_reference": reference,
            "atr": atr,
            "size_decimals_from_archived_census": decimals,
            "portfolio_equity_per_scenario_usdc": CAPITAL,
            "delay_checks": delays,
            "capture_receipts": sources["wld_entry_captures"],
        },
        "vvv": {"protective_exit_evidence": observation, "capture_receipts": vvv},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", type=Path)
    parser.add_argument("development_archive", type=Path)
    parser.add_argument("output", type=Path)
    options = parser.parse_args(argv)
    report = evaluate(options.captures, options.development_archive)
    with options.output.open("x", encoding="utf-8") as result:
        json.dump(report, result, indent=2, default=str, allow_nan=False)
        result.write("\n")


if __name__ == "__main__":
    main()
