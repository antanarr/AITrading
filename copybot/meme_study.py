"""Offline Hyperliquid research with an explicitly sealed chronological Reserve."""

import argparse
import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, time
from math import isfinite
from pathlib import Path

from .research_data import load_archive
from .research_execution import BASE, STRESS, RiskLimits, replay
from .research_models import DAY_HOURS, HOUR_MS, excluded_symbol
from .research_signals import Variant, build_frames, ideas, variants

FILES = (
    "meme_study.py",
    "research_models.py",
    "research_data.py",
    "research_execution.py",
    "research_signals.py",
    "research_taxonomy.json",
    "perps.py",
)
COSTS = {"base": BASE, "stress": STRESS}
MAX_DRAWDOWN_TO_NET = 2.0


def day_hour(value: date) -> int:
    return int(datetime.combine(value, time.min, UTC).timestamp()) // 3600


def hour_date(hour: int) -> str:
    return datetime.fromtimestamp(hour * 3600, UTC).date().isoformat()


def current_hour() -> int:
    return int(datetime.now(UTC).timestamp()) // 3600


def code_digest() -> str:
    digest = hashlib.sha256()
    directory = Path(__file__).parent
    for name in FILES:
        digest.update((directory / name).read_bytes())
    return digest.hexdigest()


def taxonomy_digest(archive: Path) -> str:
    supplied = archive / "taxonomy.json"
    path = supplied if supplied.is_file() else Path(__file__).with_name("research_taxonomy.json")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    if path.suffix != ".json":
        raise ValueError("Research results must use a JSON path")
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temp.replace(path)


def read_json(path):
    return json.loads(path.read_text())


def qualification(track, metrics):
    if track not in ("A", "B"):
        raise ValueError(f"Unknown qualification track: {track}")
    minimum, pf_floor = (25, 1.20) if track == "A" else (20, 1.30)
    pf = metrics["profit_factor"]
    if pf is None:
        pf = float("inf") if metrics["count"] and metrics["wins"] == metrics["count"] else 0
    reasons = []
    if metrics["count"] < minimum:
        reasons.append(f"fewer than {minimum} completed trades")
    if metrics["net"] <= 0:
        reasons.append("nonpositive stress expectancy")
    if pf <= pf_floor:
        reasons.append(f"stress profit factor at or below {pf_floor:.2f}")
    if metrics["net_without_best"] <= 0:
        reasons.append("nonpositive stress net without the best trade")
    if metrics["net_without_best_coin"] <= 0:
        reasons.append("nonpositive stress net without the best coin")
    joint = metrics.get("net_without_best_trade_and_coin")
    if joint is not None and joint <= 0:
        reasons.append("nonpositive stress net excluding both outliers")
    if track == "A" and metrics["win_rate"] <= 0.52:
        reasons.append("stress win rate at or below 52%")
    if (
        track == "B"
        and metrics["net"] > 0
        and metrics["intrabar_drawdown"] > MAX_DRAWDOWN_TO_NET * metrics["net"]
    ):
        reasons.append("intrabar drawdown exceeds twice the stress net profit")
    return {"pass": not reasons, "reasons": reasons}


def evaluate(histories, frames, selected, start, end, limits):
    rows = []
    for variant in selected:
        signals = ideas(frames, variant)
        metrics = {
            key: replay(histories, signals, start, end, costs=cost, limits=limits)
            for key, cost in COSTS.items()
        }
        for score in metrics.values():
            score["net_without_best_trade_and_coin"] = net_without_both(score)
        rows.append(
            {
                "variant": asdict(variant),
                "signal_hours": len(signals),
                "order_plans": sum(len(plans) for plans in signals.values()),
                **metrics,
                "qualification": qualification(variant.track, metrics["stress"]),
            }
        )
    return rows


def net_without_both(metrics):
    trades = metrics["trades"]
    if not trades:
        return 0.0
    by_coin = {}
    for trade in trades:
        for leg in trade["legs"]:
            coin = leg["coin"]
            by_coin[coin] = by_coin.get(coin, 0.0) + leg["net"]
    best_coin = max(by_coin, key=by_coin.get)
    best_trade = max(range(len(trades)), key=lambda index: trades[index]["net"])
    return sum(
        leg["net"]
        for index, trade in enumerate(trades)
        if index != best_trade
        for leg in trade["legs"]
        if leg["coin"] != best_coin
    )


def study_period(archive, start, end, selected, minimum_turnover, limits, include_quarters=False):
    basket_histories, coverage = load_archive(archive, end, include_quarters)
    incomplete_funding = [
        coin for coin in basket_histories if coverage["markets"][coin]["missing_funding_for_hourly"]
    ]
    histories = {
        coin: history
        for coin, history in basket_histories.items()
        if coin not in incomplete_funding
    }
    coverage["excluded_incomplete_funding"] = sorted(incomplete_funding)
    if not histories:
        raise ValueError(f"No archived markets ending before {hour_date(end)}")
    if not any(start in history.bars and end - 1 in history.bars for history in histories.values()):
        raise ValueError("No archived market spans the selected evaluation period")
    if any(excluded_symbol(coin) for coin in basket_histories):
        raise ValueError("Excluded asset found in research inputs")
    frames = build_frames(
        histories,
        start,
        end,
        minimum_turnover=minimum_turnover,
        basket_histories=basket_histories,
    )
    return {
        "start": start,
        "end": end,
        "coverage": coverage,
        "benchmark": buy_and_hold(histories, frames.get(start, {}), start, end, limits),
        "results": evaluate(histories, frames, selected, start, end, limits),
    }


def buy_and_hold(histories, eligible, start, end, limits):
    coins = [
        coin
        for coin in sorted(eligible)
        if histories[coin].is_meme
        and start in histories[coin].bars
        and histories[coin].bars[start].volume > 0
    ]
    gross = funding = 0.0
    if not coins:
        return {"stay_flat": 0.0, "equal_weight": None, "reason": "No eligible opening basket"}
    allocation = limits.initial_equity / len(coins)
    fills = []
    stale = []
    for coin in coins:
        history = histories[coin]
        final = max(
            hour for hour, bar in history.bars.items() if start <= hour < end and bar.volume > 0
        )
        if final != end - 1:
            stale.append(coin)
        first_price, last_price = history.bars[start].open, history.bars[final].close
        size = allocation / first_price
        fills.append((first_price, last_price, size))
        gross += (last_price - first_price) * size
        funding -= sum(
            size * row.rate * history.bars[hour - 1].close
            for hour, row in history.funding.items()
            if start * HOUR_MS < row.time_ms < (final + 1) * HOUR_MS and hour - 1 in history.bars
        )
    net = {}
    for label, model in COSTS.items():
        entry_friction = (model.half_spread_bps + model.entry_impact_bps) / 10_000
        exit_friction = (model.half_spread_bps + model.exit_impact_bps) / 10_000
        fee_rate = model.taker_fee_bps / 10_000
        fees_and_impact = 0.0
        for first, last, size in fills:
            buy, sell = first * (1 + entry_friction), last * (1 - exit_friction)
            fees_and_impact += size * (buy - first + last - sell + (buy + sell) * fee_rate)
        net[label] = gross + funding - fees_and_impact
    return {
        "stay_flat": 0.0,
        "equal_weight": {
            "coins": coins,
            "last_available_mark_before_end": stale,
            "gross": gross,
            "funding": funding,
            "before_execution_costs": gross + funding,
            **net,
        },
    }


def shortlist(discovery, validation):
    prior = {row["variant"]["name"]: row for row in discovery}
    passing = [
        row
        for row in validation
        if row["qualification"]["pass"] and prior[row["variant"]["name"]]["stress"]["net"] > 0
    ]
    passing.sort(
        key=lambda row: (
            -row["stress"]["net_without_best_coin"],
            -prior[row["variant"]["name"]]["stress"]["net"],
            -row["stress"]["net"],
            row["variant"]["name"],
        )
    )
    picked = []
    families = set()
    for row in passing:
        family = row["variant"]["family"]
        if family not in families:
            picked.append(row["variant"])
            families.add(family)
        if len(picked) == 3:
            break
    return picked


def variant_from_json(row):
    keys = {**row, "settings": tuple(tuple(pair) for pair in row["settings"])}
    return Variant(**keys)


def retest_variant(value, menu):
    name, separator, overrides = value.partition(":")
    if not separator or name not in menu:
        raise ValueError("Retest format is NAME:hold_hours=H,stop_atr=S,trailing_atr=T")
    settings = dict(menu[name].settings)
    updates = {}
    for override in overrides.split(","):
        key, separator, raw = override.partition("=")
        if (
            not separator
            or key not in {"hold_hours", "stop_atr", "trailing_atr", "basket_breadth"}
            or key in updates
        ):
            raise ValueError(f"Invalid retest override: {override}")
        number = float(raw)
        if not isfinite(number) or number <= 0 or (key == "hold_hours" and not number.is_integer()):
            raise ValueError(f"Invalid retest value: {override}")
        if key == "basket_breadth" and (menu[name].family != "funding" or not 0.5 <= number <= 1):
            raise ValueError("Funding breadth must be between 0.5 and 1")
        updates[key] = number
    settings.update(updates)
    return replace(menu[name], name=value, settings=tuple(settings.items()))


def explore(args):
    discovery, validation, end = map(day_hour, (args.start, args.validation_start, args.end))
    if not discovery < validation < end <= current_hour():
        raise ValueError("Discovery and Validation must be adjacent completed periods")
    if not args.archive.is_dir():
        raise ValueError("Research archive does not exist")
    menu = {variant.name: variant for variant in variants()}
    selection = list(menu.values())
    if args.variants:
        selection = [candidate for candidate in selection if candidate.name in args.variants]
        if len(selection) != len(set(args.variants)):
            raise ValueError("Unrecognized candidate name")
    selection.extend(retest_variant(value, menu) for value in args.retest)
    if not isfinite(args.initial_equity) or args.initial_equity <= 0:
        raise ValueError("Initial research equity must be positive and finite")
    limits = RiskLimits(initial_equity=args.initial_equity)
    early = study_period(
        args.archive, discovery, validation, selection, args.minimum_turnover, limits, args.quarters
    )
    later = study_period(
        args.archive, validation, end, selection, args.minimum_turnover, limits, args.quarters
    )
    write_json(
        args.output,
        {
            "archive": str(args.archive.resolve()),
            "taxonomy_digest": taxonomy_digest(args.archive),
            "minimum_turnover": args.minimum_turnover,
            "limits": asdict(limits),
            "quarters": args.quarters,
            "code_digest": code_digest(),
            "discovery": early,
            "validation": later,
            "candidates": shortlist(early["results"], later["results"]),
        },
    )
    for row in later["results"]:
        stress = row["stress"]
        print(
            f"{row['variant']['name']}: {stress['count']} completed, "
            f"{stress['net']:+.4f} USDC stress, {'PASS' if row['qualification']['pass'] else 'fail'}"
        )


def freeze(args):
    data = read_json(args.study)
    if data["code_digest"] != code_digest():
        raise ValueError("Research code changed since the candidate study ran")
    if taxonomy_digest(Path(data["archive"])) != data["taxonomy_digest"]:
        raise ValueError("Research taxonomy changed since the candidate study ran")
    candidates = shortlist(data["discovery"]["results"], data["validation"]["results"])
    if not candidates:
        raise ValueError(
            "No candidate qualified in Validation with positive stress net in Discovery"
        )
    if candidates != data["candidates"]:
        raise ValueError("Study candidates no longer match the qualifying results")
    now = current_hour()
    start, end = map(day_hour, (args.reserve_start, args.reserve_end))
    next_day = (now // DAY_HOURS + 1) * DAY_HOURS
    if start < next_day or start < data["validation"]["end"] or end <= start:
        raise ValueError("Reserve must be a prospective period after Validation")
    manifest = {
        "study_digest": hashlib.sha256(args.study.read_bytes()).hexdigest(),
        "code_digest": code_digest(),
        "training_archive": data["archive"],
        "taxonomy_digest": data["taxonomy_digest"],
        "frozen_at": datetime.now(UTC).isoformat(),
        "start": start,
        "end": end,
        "minimum_turnover": data["minimum_turnover"],
        "limits": data["limits"],
        "quarters": data["quarters"],
        "candidates": candidates,
    }
    if args.manifest.suffix != ".json":
        raise ValueError("Research manifest must use a JSON path")
    with args.manifest.open("x") as output:
        json.dump(manifest, output, indent=2, allow_nan=False)
        output.write("\n")


def execute_reserve(args):
    manifest = read_json(args.manifest)
    if manifest["code_digest"] != code_digest():
        raise ValueError("Research code differs from the frozen version")
    start, end = manifest["start"], manifest["end"]
    if current_hour() < end:
        raise ValueError(f"Reserve has not closed. First excluded UTC day is {hour_date(end)}")
    archive = args.archive.resolve()
    training = Path(manifest["training_archive"]).resolve()
    if archive.is_relative_to(training) or training.is_relative_to(archive):
        raise ValueError("Reserve requires a separate archive")
    if not archive.is_dir() or args.output.exists():
        raise ValueError("Reserve archive missing or output already exists")
    if args.output.suffix != ".json":
        raise ValueError("Reserve results must use a JSON path")
    if taxonomy_digest(archive) != manifest["taxonomy_digest"]:
        raise ValueError("Reserve taxonomy differs from the frozen taxonomy")
    if not 1 <= len(manifest["candidates"]) <= 3:
        raise ValueError("Reserve manifest must contain one to three frozen candidates")
    receipt = args.manifest.with_suffix(".reserve-attempted.json")
    with receipt.open("x") as output:
        json.dump(
            {
                "started_at": datetime.now(UTC).isoformat(),
                "archive": str(archive),
                "output": str(args.output.resolve()),
            },
            output,
        )
        output.write("\n")
    study = study_period(
        archive,
        start,
        end,
        [variant_from_json(row) for row in manifest["candidates"]],
        manifest["minimum_turnover"],
        RiskLimits(**manifest["limits"]),
        manifest.get("quarters", False),
    )
    write_json(args.output, study)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    initial = actions.add_parser("explore", help="Run Discovery and Validation only")
    initial.add_argument("archive", type=Path)
    initial.add_argument("output", type=Path)
    initial.add_argument("--start", type=date.fromisoformat, required=True)
    initial.add_argument("--validation-start", type=date.fromisoformat, required=True)
    initial.add_argument("--end", type=date.fromisoformat, required=True)
    initial.add_argument("--minimum-turnover", type=float, default=1_000_000)
    initial.add_argument(
        "--initial-equity",
        type=float,
        default=RiskLimits().initial_equity,
        help="Hypothetical USDC equity. Does not affect live or paper balances.",
    )
    initial.add_argument("--quarters", action="store_true")
    initial.add_argument("--variants", nargs="+")
    initial.add_argument("--retest", action="append", default=[], metavar="NAME:KEY=VALUE")
    selection = actions.add_parser(
        "freeze", help="Seal qualified rules before a prospective Reserve"
    )
    selection.add_argument("study", type=Path)
    selection.add_argument("manifest", type=Path)
    selection.add_argument("--reserve-start", type=date.fromisoformat, required=True)
    selection.add_argument("--reserve-end", type=date.fromisoformat, required=True)
    reserve = actions.add_parser("reserve", help="Run already frozen candidates once")
    reserve.add_argument("manifest", type=Path)
    reserve.add_argument("archive", type=Path)
    reserve.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        {"explore": explore, "freeze": freeze, "reserve": execute_reserve}[args.action](args)
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
