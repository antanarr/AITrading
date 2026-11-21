"""Causal beta-hedged major-alt spread research using native Hyperliquid archives."""

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from math import ceil, isfinite, log

import numpy as np

from .directional_features import _bars
from .public_strategies import IOC_CAP_BPS, _atr, _liquid
from .research_data import ARCHIVE_END_HOUR
from .research_models import MarketHistory, OrderPlan, TradePlan

PAIR_VARIANT = "major-alt-beta-pairs"
PAIRS = (
    ("SOL", "ETH"),
    ("AVAX", "SOL"),
    ("LINK", "ETH"),
    ("AAVE", "ETH"),
    ("SUI", "SOL"),
    ("HYPE", "SOL"),
)
FIT_HOURS = 30 * 24
REFIT_HOURS = 7 * 24
MONDAY_ORIGIN = -3 * 24
MIN_HALF_LIFE = 12
MAX_HALF_LIFE = 96
MIN_TRADE_FRACTION = 0.90
ENTRY_Z = 2.0
EXIT_Z = 0.5
STOP_Z = 4.0
STOP_ATR = 6.0


@dataclass(frozen=True)
class SpreadFit:
    alpha: float
    beta: float
    residual_sd: float
    half_life: float

    def zscore(self, left: float, right: float) -> float:
        return (log(left) - self.alpha - self.beta * log(right)) / self.residual_sd

    @property
    def hold_hours(self) -> int:
        return min(MAX_HALF_LIFE, ceil(2 * self.half_life))


def _slope(y: np.ndarray, x: np.ndarray) -> float:
    centered = x - x.mean()
    variance = centered @ centered
    if variance <= np.finfo(float).eps:
        return float("nan")
    return float(centered @ (y - y.mean()) / variance)


def fit_spread(left, right) -> tuple[SpreadFit | None, str | None]:
    """Fit the log-price hedge and residual AR(1) on 720 aligned closed hours."""
    left, right = np.asarray(left, dtype=float), np.asarray(right, dtype=float)
    if (
        left.shape != (FIT_HOURS,)
        or right.shape != (FIT_HOURS,)
        or not (
            np.isfinite(left).all()
            and np.isfinite(right).all()
            and (left > 0).all()
            and (right > 0).all()
        )
    ):
        return None, "incomplete_fit_prices"
    y, x = np.log(left), np.log(right)
    beta = _slope(y, x)
    if not isfinite(beta) or beta <= 0:
        return None, "invalid_beta"
    alpha = float(y.mean() - beta * x.mean())
    residuals = y - alpha - beta * x
    residual_sd = float(residuals.std())
    if not isfinite(residual_sd) or residual_sd <= 1e-8:
        return None, "degenerate_residual"
    rho = _slope(residuals[1:], residuals[:-1])
    if not 0 < rho < 1:
        return None, "invalid_residual_persistence"
    half_life = log(0.5) / log(rho)
    if not MIN_HALF_LIFE <= half_life <= MAX_HALF_LIFE:
        return None, "half_life_outside_range"
    return SpreadFit(alpha, beta, residual_sd, half_life), None


def _weekly_fit(hour: int) -> int:
    return MONDAY_ORIGIN + (hour - MONDAY_ORIGIN) // REFIT_HOURS * REFIT_HOURS


def _legs(pair, direction, model, close, risk):
    unit = 1 / (1 + model.beta)
    return tuple(
        OrderPlan(
            coin=coin,
            side=side,
            limit=float(price * (1 + side * IOC_CAP_BPS / 10_000)),
            atr=float(atr),
            stop_atr=STOP_ATR,
            target_atr=None,
            hold_hours=model.hold_hours,
            ttl_hours=0,
            mode="ioc",
            weight=weight,
        )
        for coin, side, price, atr, weight in zip(
            pair, (direction, -direction), close, risk, (unit, model.beta * unit), strict=True
        )
    )


def build_pair_plans(
    histories: Mapping[str, MarketHistory],
    start: int,
    end: int,
    minimum_turnover: float = 1_000_000,
) -> tuple[dict[int, list[TradePlan]], dict]:
    """Build sorted two-leg IOC plans and an auditable record of weekly fits."""
    if type(start) is not int or type(end) is not int or not 0 < start < end <= ARCHIVE_END_HOUR:
        raise ValueError("Expected increasing UTC epoch hours inside the development cutoff")
    if not isfinite(minimum_turnover) or minimum_turnover <= 0:
        raise ValueError("Expected finite positive minimum turnover")
    first_fit = _weekly_fit(start)
    first_bar = max(0, first_fit - FIT_HOURS)
    closed, liquid, atr = {}, {}, {}
    for coin in dict.fromkeys(coin for pair in PAIRS for coin in pair):
        history = histories.get(coin)
        if history is None or history.coin != coin or not history.bars:
            continue
        hourly = _bars(history.bars, first_bar, end)
        hourly.index += 1
        closed[coin] = hourly
        liquid[coin] = _liquid(hourly, minimum_turnover)
        atr[coin] = _atr(hourly)

    candidates = defaultdict(list)
    diagnostics = {
        "variant": PAIR_VARIANT,
        "parameters": {
            "lookback_hours": FIT_HOURS,
            "refit": "Monday 00:00 UTC",
            "minimum_active_fit_fraction_per_leg": MIN_TRADE_FRACTION,
            "half_life_hours": [MIN_HALF_LIFE, MAX_HALF_LIFE],
            "entry_new_excursion_z": ENTRY_Z,
            "spread_take_profit_z": EXIT_Z,
            "spread_divergence_stop_z": STOP_Z,
            "emergency_leg_stop_atr": STOP_ATR,
            "maximum_hold": "2 * fit half-life, rounded up and capped at 96 hours",
            "entry_price_cap_bps_per_leg": IOC_CAP_BPS,
            "formal_cointegration_test": False,
        },
        "pair_coverage": {},
        "fits": [],
    }
    for pair in PAIRS:
        left, right = pair
        missing = [coin for coin in pair if coin not in closed]
        label = "/".join(pair)
        stats = Counter()
        if missing:
            diagnostics["pair_coverage"][label] = {"missing_markets": missing}
            continue
        for fit_hour in range(first_fit, end, REFIT_HOURS):
            training_hours = slice(fit_hour - FIT_HOURS + 1, fit_hour)
            asset, hedge = (closed[coin].loc[training_hours] for coin in pair)
            model, rejection = fit_spread(asset.close, hedge.close)
            if model is None:
                stats[rejection] += 1
                continue
            if min(asset.volume.gt(0).mean(), hedge.volume.gt(0).mean()) < MIN_TRADE_FRACTION:
                stats["inactive_fit_candles"] += 1
                continue
            stats["accepted_fits"] += 1
            diagnostics["fits"].append({"pair": list(pair), "hour": fit_hour, **asdict(model)})
            previous_z = model.zscore(asset.close.iloc[-1], hedge.close.iloc[-1])
            for hour in range(fit_hour + 1, min(fit_hour + REFIT_HOURS, end)):
                prices = tuple(closed[coin].at[hour, "close"] for coin in pair)
                if not all(isfinite(price) and price > 0 for price in prices):
                    previous_z = None
                    continue
                z = model.zscore(*prices)
                entered_excursion = previous_z is not None and (
                    previous_z < ENTRY_Z <= z or previous_z > -ENTRY_Z >= z
                )
                previous_z = z
                if not entered_excursion or hour < start:
                    continue
                stats["entry_excursions"] += 1
                risk = tuple(atr[coin].at[hour] for coin in pair)
                if not (
                    all(liquid[coin].at[hour] for coin in pair) and all(value > 0 for value in risk)
                ):
                    stats["entry_liquidity_or_risk_rejects"] += 1
                    continue
                legs = _legs(pair, -1 if z > 0 else 1, model, prices, risk)
                candidates[hour].append((abs(z), pair, TradePlan(legs, PAIR_VARIANT)))
                stats["entry_plans"] += 1
        diagnostics["pair_coverage"][label] = {"missing_markets": [], **stats}
    return {
        hour: [plan for _, _, plan in sorted(rows, key=lambda row: (-row[0], row[1]))]
        for hour, rows in sorted(candidates.items())
    }, diagnostics


def pair_exit_signal(histories: Mapping[str, MarketHistory], diagnostics: dict):
    """Return a shared replay callback using the entry fit and prior traded closes."""
    fits = {
        (tuple(record["pair"]), record["hour"]): SpreadFit(
            record["alpha"], record["beta"], record["residual_sd"], record["half_life"]
        )
        for record in diagnostics["fits"]
    }

    def exit_signal(plan: TradePlan, submitted: int, hour: int) -> str | None:
        if plan.label != PAIR_VARIANT or hour <= submitted:
            return None
        pair = tuple(leg.coin for leg in plan.legs)
        model = fits[pair, _weekly_fit(submitted)]
        first, second = (histories[coin].bars.get(hour - 1) for coin in pair)
        if first is None or second is None or min(first.volume, second.volume) <= 0:
            return None
        signed = plan.legs[0].side * model.zscore(first.close, second.close)
        if signed >= -EXIT_Z:
            return "spread reversion"
        if signed <= -STOP_Z:
            return "spread divergence"
        return None

    return exit_signal
