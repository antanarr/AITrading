"""Offline New York cash-session continuation and reversal stress test."""

import argparse
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

from .broad_study import benchmark, run_book, usable_histories
from .meme_study import day_hour, qualification, write_json
from .research_data import ARCHIVE_END_HOUR, load_archive
from .research_execution import RiskLimits
from .research_models import HOUR_MS, OrderPlan, TradePlan, excluded_symbol

START = date(2026, 5, 25)
VALIDATION = date(2026, 9, 26)
END = date(2026, 11, 21)
NEW_YORK = ZoneInfo("America/New_York")
HOLIDAYS = {date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7)}
LIQUIDITY_WINDOW = 24
TURNOVER_MINIMUM = 1_000_000
ATR_WINDOW = 14
UNIVERSE_SIZE = 16
MIN_MARKETS = 6
MIN_BREADTH = 0.60
MIN_ATR_REACTION = 0.20
MAX_DAILY_ORDERS = 2
STOP_ATR = 1.25
LIMIT_BPS = 15
GATES = {"follow": 1, "fade": -1}
SESSIONS = {"cash": (10, 15), "off_session_control": (18, 23)}
REFERENCES = [
    "https://doi.org/10.1016/j.jfineco.2018.05.009",
    "https://arxiv.org/abs/2109.12142",
    "https://www.nyse.com/trade/hours-calendars",
]


@dataclass(frozen=True, slots=True)
class Cue:
    coin: str
    turnover: float
    move_in_atr: float


def session_hours(first, end, clock):
    observation, entry = SESSIONS[clock]
    for index in range((end - first).days):
        day = first + timedelta(days=index)
        if day.weekday() >= 5 or day in HOLIDAYS:
            continue
        signal = int(datetime.combine(day, time(observation), NEW_YORK).timestamp()) // 3600
        fill = int(datetime.combine(day, time(entry), NEW_YORK).timestamp()) // 3600
        if day_hour(first) <= signal < fill and fill + 1 < day_hour(end):
            yield day, signal, fill


def trailing_bars(history, hour, length):
    rows = [history.bars.get(stamp) for stamp in range(hour - length, hour)]
    return rows if all(row is not None for row in rows) else []


def trailing_atr(history, hour):
    rows = trailing_bars(history, hour, ATR_WINDOW + 1)
    if not rows:
        return 0.0
    return mean(
        max(
            current.high - current.low,
            abs(current.high - last.close),
            abs(current.low - last.close),
        )
        for last, current in pairwise(rows)
    )


def session_cue(history, opening_hour):
    opening = history.bars.get(opening_hour)
    if opening is None or opening.volume <= 0:
        return None
    trailing = trailing_bars(history, opening_hour, LIQUIDITY_WINDOW)
    if not trailing or sum(bar.volume > 0 for bar in trailing) < 9:
        return None
    notionals = [bar.close * bar.volume for bar in trailing if bar.volume > 0]
    turnover = sum(notionals)
    if turnover < TURNOVER_MINIMUM or median(notionals) < TURNOVER_MINIMUM / 72:
        return None
    atr = trailing_atr(history, opening_hour)
    return Cue(history.coin, turnover, (opening.close - opening.open) / atr) if atr > 0 else None


def build_signals(histories, start, end, clock):
    label = f"intraday-{clock}"
    diagnostics = Counter()
    selected = {}
    plans = {name: {} for name in GATES}
    for day, signal, enter in session_hours(start, end, clock):
        diagnostics["eligible_calendar_sessions"] += 1
        candidates = sorted(
            (
                cue
                for coin, item in histories.items()
                if not excluded_symbol(coin)
                if (cue := session_cue(item, signal)) is not None
            ),
            key=lambda cue: (-cue.turnover, cue.coin),
        )[:UNIVERSE_SIZE]
        if len(candidates) < MIN_MARKETS:
            diagnostics["insufficient_liquid_markets"] += 1
            continue
        middle = median(cue.move_in_atr for cue in candidates)
        side = 1 if middle > 0 else -1
        aligned = [cue for cue in candidates if cue.move_in_atr * side > 0]
        breadth = len(aligned) / len(candidates)
        if abs(middle) < MIN_ATR_REACTION or breadth < MIN_BREADTH:
            diagnostics["failed_impulse_or_breadth"] += 1
            continue
        information = {
            "session_date": day.isoformat(),
            "observed_open_hour": signal,
            "submitted_hour": enter,
            "median_atr_move": middle,
            "aligned_breadth": breadth,
            "liquid_count": len(candidates),
            "coins": [],
        }
        selected[enter] = information
        for cue in aligned[:MAX_DAILY_ORDERS]:
            history = histories[cue.coin]
            reference = history.bars.get(enter - 1)
            atr = trailing_atr(history, enter)
            if reference is None or reference.volume <= 0 or atr <= 0:
                diagnostics["unusable_preentry_reference"] += 1
                continue
            information["coins"].append(cue.coin)
            for strategy, multiplier in GATES.items():
                direction = side * multiplier
                order = OrderPlan(
                    coin=cue.coin,
                    side=direction,
                    limit=reference.close * (1 + direction * LIMIT_BPS / 10_000),
                    atr=atr,
                    stop_atr=STOP_ATR,
                    target_atr=None,
                    hold_hours=1,
                    ttl_hours=0,
                    mode="ioc",
                    signal_strength=abs(middle),
                )
                plans[strategy].setdefault(enter, []).append(
                    TradePlan((order,), f"{label}-{strategy}")
                )
    diagnostics["impulse_sessions"] = len(selected)
    return plans, dict(diagnostics), selected


def by_session(score, calendar):
    timeline = {row["submitted_hour"]: row["session_date"] for row in calendar.values()}
    returns = defaultdict(float)
    for trade in score["trades"]:
        key = timeline[trade["entered_ms"] // HOUR_MS]
        returns[key] += trade["net"]
    groups = list(returns.values())
    interval = None
    if len(groups) >= 10:
        rng = random.Random(10301)
        means = sorted(mean(rng.choices(groups, k=len(groups))) for _ in range(2500))
        interval = [means[62], means[2437]]
    return {
        "sessions_with_fills": len(groups),
        "positive_sessions": sum(value > 0 for value in groups),
        "session_bootstrap_95pct_mean_pnl_interval": interval,
        "net_by_session_date": dict(sorted(returns.items())),
    }


def run(archive, start, validation, end):
    if not START <= start < validation < end <= END or day_hour(end) > ARCHIVE_END_HOUR:
        raise ValueError("Stay within the May–November Development calendar, exclusive of Reserve")
    available, coverage = load_archive(archive, day_hour(end), include_quarters=True)
    histories, rejected = usable_histories(available, coverage)
    if not histories:
        raise ValueError("No markets with complete price and funding in this archive")
    cases = {}
    for clock in SESSIONS:
        cases[clock] = clock_runs = {}
        for name, (first, last) in {
            "discovery": (start, validation),
            "validation": (validation, end),
        }.items():
            variants, diagnostics, calendar = build_signals(histories, first, last, clock)
            clock_runs[name] = case = {
                "diagnostics": diagnostics,
                "calendar": calendar,
                "by_equity": {},
            }
            for equity in (300, 500):
                risk = RiskLimits(initial_equity=equity, max_leg_fraction=0.25)
                results = case["by_equity"][str(equity)] = {
                    "buy_hold_benchmark": benchmark(
                        histories, day_hour(first), day_hour(last), equity, TURNOVER_MINIMUM
                    ),
                    "stay_flat_benchmark": 0,
                }
                for strategy, plans in variants.items():
                    scored = run_book(histories, plans, day_hour(first), day_hour(last), risk)
                    for score in scored.values():
                        score["session_diagnostics"] = by_session(score, calendar)
                    scored["qualification"] = qualification("B", scored["stress"])
                    results[strategy] = scored
    return {
        "calendar": {
            "discovery_start": start.isoformat(),
            "validation_start": validation.isoformat(),
            "end_exclusive": end.isoformat(),
        },
        "mechanism": "Opening-session direction may carry over into late-session execution flow. This transfers a published US equity clock hypothesis to native perps; it does not replicate the study's asset or bars.",
        "sources": REFERENCES,
        "parameters": {
            "session_window_hours_new_york": SESSIONS,
            "hold_hours": 1,
            "min_24h_turnover": TURNOVER_MINIMUM,
            "max_liquid_universe": UNIVERSE_SIZE,
            "minimum_markets": MIN_MARKETS,
            "breadth_minimum": MIN_BREADTH,
            "minimum_median_open_move_atr": MIN_ATR_REACTION,
            "max_orders_per_session": MAX_DAILY_ORDERS,
            "ioc_cap_bps": LIMIT_BPS,
            "stop_atr_multiple": STOP_ATR,
            "equity_risk_per_position": 0.01,
            "max_leg_equity_fraction": 0.25,
            "capital_balances": [300, 500],
            "rule": "Median and breadth of complete native opening candles set direction; rank aligned assets by preceding 24h turnover; positions cross the book and close after one hour or their stop; use an identical off-session negative control",
        },
        "usable_markets": sorted(histories),
        "excluded_incomplete_markets": rejected,
        "results": cases,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--start", type=date.fromisoformat, default=START)
    parser.add_argument("--validation", type=date.fromisoformat, default=VALIDATION)
    parser.add_argument("--end", type=date.fromisoformat, default=END)
    args = parser.parse_args()
    write_json(args.output, run(args.archive, args.start, args.validation, args.end))


if __name__ == "__main__":
    main()
