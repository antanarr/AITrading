"""Reconstruct the bot's exposure and P&L from Hyperliquid fills."""

from dataclasses import dataclass
from decimal import Decimal

from .perps import number


class ExposureConflict(ValueError):
    """The market has fills that the bot cannot treat as its own position."""


@dataclass(frozen=True)
class Execution:
    entered: Decimal
    closed: Decimal
    entry_value: Decimal
    exit_value: Decimal
    fees: Decimal
    first_fill_ms: int
    last_fill_ms: int

    @property
    def remaining(self):
        return self.entered - self.closed

    def pnl(self, *, long, funding):
        if self.entered <= 0 or self.remaining != 0:
            raise ValueError("P&L requires both sides of a flat trade")
        gross = (self.exit_value - self.entry_value) * (1 if long else -1)
        return {
            "gross_usdc": str(gross),
            "fees_usdc": str(self.fees),
            "funding_usdc": str(funding),
            "net_usdc": str(gross - self.fees + funding),
            "entry_notional_usdc": str(self.entry_value),
            "exit_notional_usdc": str(self.exit_value),
            "closed_ms": self.last_fill_ms,
        }


def reconstruct(fills, *, entry_oid, long):
    """Only the committed entry order may add size. Other fills may reduce it."""
    direction = 1 if long else -1
    entry_side = "B" if long else "A"
    entered = closed = entry_value = exit_value = fees = Decimal(0)
    first_fill_ms = last_fill_ms = 0
    seen = {}
    entry_ranges = []
    exit_ranges = []
    for fill in fills:
        key = (fill["time"], fill["tid"])
        if key in seen:
            if seen[key] != fill:
                raise ExposureConflict("Venue returned conflicting versions of a fill")
            continue
        seen[key] = fill
        size, price = number(fill["sz"]), number(fill["px"])
        start = number(fill["startPosition"]) * direction
        if min(size, price) <= 0 or fill["side"] not in ("A", "B") or start < 0:
            raise ExposureConflict("Fill does not match the expected position direction")
        if fill["side"] == entry_side:
            if fill["oid"] != entry_oid:
                raise ExposureConflict("Another order opened exposure in the bot's market")
            entered += size
            entry_value += size * price
            entry_ranges.append((start, start + size))
            first_fill_ms = min(first_fill_ms or fill["time"], fill["time"])
        else:
            if fill["oid"] == entry_oid or start < size:
                raise ExposureConflict("A fill closed more than the bot's position or reversed it")
            closed += size
            exit_value += size * price
            exit_ranges.append((start - size, start))
        if fill["feeToken"] != "USDC":
            raise ExposureConflict("Trade fee cannot be reconciled in USDC")
        fees += number(fill["fee"])
        last_fill_ms = max(last_fill_ms, fill["time"])
    if closed > entered:
        raise ExposureConflict("Close fills exceed verified entry fills")
    before = Decimal(0)
    for start, end in sorted(entry_ranges):
        if start != before:
            raise ExposureConflict("Entry fills do not form a position from zero exposure")
        before = end
    before = entered - closed
    for start, end in sorted(exit_ranges):
        if start != before:
            raise ExposureConflict("Close fills do not match the verified position")
        before = end
    return Execution(entered, closed, entry_value, exit_value, fees, first_fill_ms, last_fill_ms)
