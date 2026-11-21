"""Walk-forward directional model research on completed Hyperliquid history."""

import argparse
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import balanced_accuracy_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from .directional_context import load_context
from .directional_features import HORIZONS, build_frame
from .directional_positioning import load_positioning
from .meme_study import buy_and_hold, day_hour, hour_date, qualification, write_json
from .research_data import ARCHIVE_END_HOUR, load_archive
from .research_execution import BASE, MIN_NOTIONAL, STRESS, RiskLimits

FEATURES = {
    "chart": ("tech_",),
    "native": ("tech_", "market_", "carry_"),
    "macro": ("tech_", "market_", "carry_", "macro_"),
    "context": ("tech_", "market_", "carry_", "macro_", "news_"),
    "stationary": ("tech_", "market_", "carry_", "macro_", "news_"),
    "positioning": ("tech_", "market_", "carry_", "macro_", "news_", "position_"),
    "media": ("tech_", "market_", "carry_", "macro_", "news_"),
}
BASELINES = {"always_long", "trend", "consensus"}
CONFIDENCE = 0.55
LOOKBACK_DAYS = 56
WARMUP_DAYS = 42
FOLD_DAYS = 14
INITIAL_EQUITY = 1000
MAX_STAKE_FRACTION = 0.20
RISK_BUDGET_FRACTION = 0.01


@dataclass(frozen=True)
class Fold:
    fit_start: int
    start: int
    end: int
    period: str


def folds(start, validation, end, lookback=LOOKBACK_DAYS):
    for left, right, period in (
        (start + WARMUP_DAYS * 24, validation, "discovery"),
        (validation, end, "validation"),
    ):
        for hour in range(left, right, FOLD_DAYS * 24):
            yield Fold(
                max(start, hour - lookback * 24), hour, min(right, hour + FOLD_DAYS * 24), period
            )


def fit_rows(frame, fold, horizon):
    columns = [f"price_return_{horizon}", f"funding_return_{horizon}"]
    return frame.loc[
        frame.hour.ge(fold.fit_start)
        & frame.hour.lt(fold.start)
        & frame[f"exit_hour_{horizon}"].lt(fold.start)
    ].dropna(subset=columns)


def market_data(frame, modes):
    columns = [
        name
        for name in frame
        if name.startswith(FEATURES[modes])
        and (modes == "media" or not name.startswith("news_media_"))
    ]
    if modes == "stationary":
        columns = [
            name for name in columns if not (name.startswith("macro_") and name.endswith("_level"))
        ]
    required = {
        "context": "news_",
        "stationary": "macro_",
        "macro": "macro_",
        "positioning": "position_",
        "media": "news_media_",
    }
    if modes in required and not any(name.startswith(required[modes]) for name in columns):
        raise ValueError(f"{modes} mode requires timestamped {required[modes]} features")
    return frame[columns].replace([np.inf, -np.inf], np.nan).astype(float)


def realized_risk(rows, horizon):
    return (rows.tech_1h_realized_vol * np.sqrt(horizon)).clip(lower=0.003)


def models(train, test, columns, horizon, relative=None, *, gross_only=False):
    price = train[f"price_return_{horizon}"]
    adjusted = price if gross_only else price + train[f"funding_return_{horizon}"]
    observed = price.ne(0)
    if observed.sum() < 100 or price[observed].gt(0).nunique() != 2:
        raise ValueError("Fold requires at least 100 non-flat training labels and both directions")
    fit_columns = columns.loc[train.index].loc[observed].nunique().gt(1)
    if not fit_columns.any():
        raise ValueError("Fold has no varying feature observed during training")
    x = columns.loc[train.index, fit_columns]
    x_test = columns.loc[test.index, fit_columns]
    y = price[observed].gt(0)
    logit = make_pipeline(
        SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True),
        StandardScaler(),
        LogisticRegression(C=0.10, max_iter=1_000, tol=1e-4),
    )
    settings = dict(
        max_iter=70,
        max_leaf_nodes=9,
        min_samples_leaf=150,
        learning_rate=0.06,
        l2_regularization=5,
        early_stopping=False,
        random_state=17,
    )
    boost = HistGradientBoostingClassifier(**settings)
    mean_return = HistGradientBoostingRegressor(**settings)
    with threadpool_limits(limits=2):
        logit.fit(x.loc[observed], y)
        boost.fit(x.loc[observed], y)
        # Bound extreme historical labels in the fit. Evaluation uses raw returns.
        mean_return.fit(x, adjusted.clip(-0.10, 0.10))
        predictions = {
            "logistic": logit.predict_proba(x_test)[:, 1],
            "boost": boost.predict_proba(x_test)[:, 1],
            "return": mean_return.predict(x_test),
        }
        if relative:
            outcome = adjusted
            if relative == "volatility":
                outcome = outcome / realized_risk(train, horizon)
            hourly = outcome.groupby(train.hour)
            residual = outcome - hourly.transform("median")
            supported = hourly.transform("count").ge(5) & residual.notna()
            bound = 5 if relative == "volatility" else 0.10
            ranker = HistGradientBoostingRegressor(**settings)
            ranker.fit(x.loc[supported], residual.loc[supported].clip(-bound, bound))
            predictions["relative"] = ranker.predict(x_test)
        return predictions


def direction_accuracy(test, probability, horizon):
    outcome = test[f"price_return_{horizon}"]
    valid = outcome.notna() & outcome.ne(0)
    y = outcome.loc[valid].gt(0)
    p = np.asarray(probability)[valid.to_numpy()]
    if y.nunique() != 2:
        return {"labels": int(valid.sum()), "eligible": len(test)}
    return {
        "labels": len(y),
        "eligible": len(test),
        "up_fraction": float(y.mean()),
        "accuracy": float(np.mean(y.to_numpy() == (p >= 0.5))),
        "majority_accuracy": float(max(y.mean(), 1 - y.mean())),
        "balanced_accuracy": float(balanced_accuracy_score(y, p >= 0.5)),
        "auc": float(roc_auc_score(y, p)),
        "brier": float(brier_score_loss(y, p)),
    }


def score_columns(test, predictions):
    for name, values in predictions.items():
        test[name] = values
    return test


def cost_return(price, funding, side, costs):
    entry = (costs.half_spread_bps + costs.entry_impact_bps) / 10_000
    exit_ = (costs.half_spread_bps + costs.exit_impact_bps) / 10_000
    fee = costs.taker_fee_bps / 10_000
    return (
        (price + funding) * side
        - entry
        - (1 + price) * exit_
        - fee * (1 + entry * side + (1 + price) * (1 - exit_ * side))
    )


def rank_diagnostics(test, reference, horizon, name):
    price_col, funding_col = f"price_return_{horizon}", f"funding_return_{horizon}"
    observed = test.dropna(subset=[price_col, funding_col]).copy()
    cutoffs = reference[name].quantile([0.05, *np.arange(0.1, 1, 0.1), 0.95]).to_numpy()
    bins = np.searchsorted(cutoffs, observed[name], side="right")
    details = {}
    for bucket in range(len(cutoffs) + 1):
        rows = observed.loc[bins == bucket]
        if rows.empty:
            continue
        price, funding = rows[price_col], rows[funding_col]
        side = -1 if bucket < 6 else 1
        stress = cost_return(price, funding, side, STRESS)
        details[str(bucket)] = {
            "count": len(rows),
            "mean_long_price_bps": float(price.mean() * 10_000),
            "mean_return_atr": float((price / rows.tech_1h_atr.where(rows.tech_1h_atr > 0)).mean())
            if rows.tech_1h_atr.gt(0).any()
            else None,
            "long_up_rate": float(price.gt(0).mean()),
            "stress_win_rate": float(stress.gt(0).mean()),
            "mean_side_base_bps": float(10_000 * cost_return(price, funding, side, BASE).mean()),
            "mean_side_stress_bps": float(stress.mean() * 10_000),
        }
    by_hour = observed.groupby("hour")
    observed["score_rank"] = by_hour[name].rank()
    observed["actual_rank"] = by_hour[price_col].rank()
    observed = observed.loc[by_hour[name].transform("size") >= 5]
    hourly_ic = (
        observed.groupby("hour")
        .apply(
            lambda rows: (
                rows.score_rank.corr(rows.actual_rank)
                if rows.score_rank.nunique() > 1 and rows.actual_rank.nunique() > 1
                else np.nan
            ),
            include_groups=False,
        )
        .dropna()
        if not observed.empty
        else pd.Series(dtype=float)
    )
    dispersion = hourly_ic.std(ddof=0) if not hourly_ic.empty else 0
    low, high = details.get("0"), details.get(str(len(cutoffs)))
    return {
        "training_out_of_time_cutoffs": [float(value) for value in cutoffs],
        "score_bins": details,
        "tails_spread_bps": high["mean_long_price_bps"] - low["mean_long_price_bps"]
        if high and low
        else None,
        "rank_ic_hours": len(hourly_ic),
        "rank_ic": float(hourly_ic.mean()) if not hourly_ic.empty else None,
        "rank_ic_ir": float(hourly_ic.mean() / dispersion) if dispersion > 0 else None,
    }


def fit_predict(frame, fold, horizon, columns, tails=False, relative=None):
    train = fit_rows(frame, fold, horizon)
    test = frame.loc[
        frame.hour.ge(fold.start)
        & frame.hour.lt(fold.end)
        & frame[f"exit_hour_{horizon}"].lt(fold.end)
    ].copy()
    prediction = models(train, test, columns, horizon, relative)
    score_columns(test, prediction)
    report = {
        "fit_start": hour_date(fold.fit_start),
        "test_start": hour_date(fold.start),
        "test_end_exclusive": hour_date(fold.end),
        "fit_rows": len(train),
        "eligible_test_rows": len(test),
        "logistic": direction_accuracy(test, prediction["logistic"], horizon),
        "boost": direction_accuracy(test, prediction["boost"], horizon),
    }
    if tails:
        calibration_start = fold.start - FOLD_DAYS * 24
        inner = Fold(fold.fit_start, calibration_start, fold.start, "calibration")
        prior = fit_rows(frame, inner, horizon)
        reference = train.loc[train.hour.ge(calibration_start)].copy()
        score_columns(reference, models(prior, reference, columns, horizon, relative))
        report["tail_diagnostics"] = {}
        for name, center in (("logistic", 0.5), ("boost", 0.5), ("return", 0)):
            confidence = (reference[name] - center).abs().groupby(reference.hour).max()
            test[f"{name}_tail_cutoff"] = float(confidence.quantile(0.80))
            report["tail_diagnostics"][name] = rank_diagnostics(test, reference, horizon, name)
        if relative:
            report["tail_diagnostics"]["relative"] = rank_diagnostics(
                test, reference, horizon, "relative"
            )
    return test, report


def choose_signals(test, horizon, policy, phase=0):
    # Complete earlier signals before the next entry, using one global UTC clock.
    grid = test.loc[(test.hour % horizon).eq(phase % horizon)].copy()
    trend = np.sign(grid.tech_1h_return_24.fillna(0))
    breadth = (
        "market_universe_breadth_24h"
        if "market_universe_breadth_24h" in grid
        else "market_meme_breadth_24h"
    )
    sector = np.sign(grid[breadth].fillna(0.5) - 0.5)
    agreement = trend.eq(sector) & trend.ne(0)
    if policy == "always_long":
        grid["side"] = 1
        grid["rank"] = grid.market_log_turnover
    elif policy in {"trend", "consensus"}:
        grid["side"] = trend if policy == "trend" else trend.where(agreement, 0)
        grid["rank"] = grid.tech_1h_return_24.abs()
    else:
        strategy = policy.removeprefix("tail_")
        score = grid["boost" if strategy == "hybrid" else strategy]
        if strategy == "return":
            threshold = (
                2 * STRESS.taker_fee_bps
                + 2 * STRESS.half_spread_bps
                + STRESS.entry_impact_bps
                + STRESS.exit_impact_bps
                + 10
            ) / 10_000
            raw = score
        else:
            threshold = CONFIDENCE - 0.5
            raw = score - 0.5
        if policy.startswith("tail_"):
            threshold = grid[f"{strategy}_tail_cutoff"].clip(lower=threshold)
        grid["side"] = np.sign(raw).where(raw.abs() >= threshold, 0)
        grid["rank"] = raw.abs()
        if policy == "hybrid":
            grid["side"] = grid.side.where(~agreement, trend)
            # Normalize probability and rule-driven conviction for comparable ranking.
            grid["rank"] = grid["rank"].where(~agreement, threshold)
    return (
        grid.loc[grid.side.ne(0)]
        .sort_values(["hour", "rank", "coin"], ascending=[True, False, True])
        .drop_duplicates("hour")
    )


def trade_components(row, history, horizon, costs, limit):
    price = row.entry_price
    gross_return = getattr(row, f"price_return_{horizon}")
    carry_return = getattr(row, f"funding_return_{horizon}")
    if not all(np.isfinite(value) for value in (price, gross_return, carry_return)):
        return None
    side = int(row.side)
    entry_impact = (costs.half_spread_bps + costs.entry_impact_bps) / 10_000
    exit_impact = (costs.half_spread_bps + costs.exit_impact_bps) / 10_000
    entry = price * (1 + side * entry_impact)
    exit_price = price * (1 + gross_return)
    lot_scale = 10**history.decimals
    quantity = float(np.floor(limit * lot_scale / entry) / lot_scale)
    if entry * quantity < MIN_NOTIONAL:
        return {}
    exit_fill = exit_price * (1 - side * exit_impact)
    gross = quantity * price * side * gross_return
    carry = quantity * price * side * carry_return
    fees = quantity * (entry + exit_fill) * costs.taker_fee_bps / 10_000
    friction = quantity * (price * entry_impact + exit_price * exit_impact)
    exit_hour = getattr(row, f"exit_hour_{horizon}")
    bars = [history.bars[hour] for hour in range(row.entry_hour, exit_hour)]
    adverse = min(bar.low for bar in bars) if side == 1 else max(bar.high for bar in bars)
    conservative_funding = min(0.0, carry)
    marked = (adverse * (1 - side * exit_impact) - entry) * side * quantity
    marked -= quantity * (entry + adverse) * costs.taker_fee_bps / 10_000
    breadth = getattr(
        row, "market_universe_breadth_24h", getattr(row, "market_meme_breadth_24h", np.nan)
    )
    return {
        "hour": int(row.hour),
        "exit_hour": int(exit_hour),
        "coin": row.coin,
        "group": row.group,
        "side": side,
        "gross": gross,
        "funding": carry,
        "fees": fees,
        "slippage": friction,
        "net": gross + carry - fees - friction,
        "worst_open_pnl": marked + conservative_funding,
        "regime": "up" if breadth >= 0.60 else "down" if breadth <= 0.40 else "mixed",
    }


def trade_summary(trades, starting_equity=INITIAL_EQUITY):
    days, weeks, coins, regimes = {}, {}, {}, {}
    peak = equity = starting_equity
    drawdown = intrabar_drawdown = 0.0
    nets = [row["net"] for row in trades]
    for row in trades:
        net = row["net"]
        intrabar_drawdown = max(intrabar_drawdown, peak - equity - row["worst_open_pnl"])
        equity += net
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        day = date.fromisoformat(hour_date(row["exit_hour"]))
        week = day.isocalendar()
        for key, counts in (
            (day.isoformat(), days),
            (f"{week.year}-W{week.week:02}", weeks),
            (row["coin"], coins),
            (row["regime"], regimes),
        ):
            counts[key] = counts.get(key, 0.0) + net
    wins = [value for value in nets if value > 0]
    losses = [value for value in nets if value < 0]
    total = sum(nets)
    best = max(nets, default=0)
    best_coin = max(coins, key=coins.get) if coins else None
    other = [row["net"] for row in trades if row["coin"] != best_coin]
    return {
        **{
            key: float(sum(row[key] for row in trades))
            for key in ("gross", "funding", "fees", "slippage")
        },
        "net": float(total),
        "count": len(trades),
        "wins": len(wins),
        "win_rate": len(wins) / len(trades) if trades else 0,
        "expectancy": float(total / len(trades)) if trades else None,
        "profit_factor": float(sum(wins) / -sum(losses)) if losses else None,
        "payoff_ratio": float(np.mean(wins) / -np.mean(losses)) if wins and losses else None,
        "drawdown": float(drawdown),
        "intrabar_drawdown": float(max(intrabar_drawdown, drawdown)),
        "worst_day": float(min(days.values(), default=0)),
        "worst_week": float(min(weeks.values(), default=0)),
        "positive_weeks": sum(value > 0 for value in weeks.values()),
        "active_weeks": len(weeks),
        "net_without_best": float(total - max(0, best)),
        "net_without_best_coin": float(sum(other)),
        "net_without_best_trade_and_coin": float(sum(other) - max(0, max(other, default=0))),
        "best_coin": best_coin,
        "net_by_coin": {coin: float(value) for coin, value in coins.items()},
        "net_by_regime": {regime: float(value) for regime, value in regimes.items()},
    }


def stake_budget(equity, horizon, hourly_volatility):
    volatility = hourly_volatility * np.sqrt(horizon)
    fraction = (
        min(MAX_STAKE_FRACTION, RISK_BUDGET_FRACTION / volatility)
        if np.isfinite(volatility) and volatility > 0
        else MAX_STAKE_FRACTION
    )
    return float(max(0, equity) * fraction)


def replay(picks, histories, horizon, costs, starting_equity=INITIAL_EQUITY):
    trades = []
    unavailable = too_small = 0
    equity = starting_equity
    for row in picks.itertuples():
        # Cap the cost at the current cash balance after earlier positions close.
        limit = stake_budget(equity, horizon, row.tech_1h_realized_vol)
        trade = trade_components(row, histories[row.coin], horizon, costs, limit)
        if trade is None:
            unavailable += 1
            continue
        if not trade:
            too_small += 1
            continue
        trades.append(trade)
        equity += trade["net"]
    return {
        **trade_summary(trades, starting_equity),
        "signals": len(picks),
        "unavailable_outcomes": unavailable,
        "below_minimum": too_small,
        "hours_in_market": horizon * len(trades),
    }, trades


def choose_baskets(test, horizon, phase, gated, relative="raw"):
    grid = test.loc[(test.hour % horizon).eq(phase % horizon)].copy()
    basket_rows = []
    if relative == "volatility":
        grid["risk"] = realized_risk(grid, horizon)
        grid = grid.dropna(subset=["risk"])
    for _, rows in grid.groupby("hour"):
        if len(rows) < 5:
            continue
        ranked = rows.sort_values(["relative", "coin"])
        longs, shorts = ranked.tail(2), ranked.head(2)
        if relative == "volatility":
            weights = [
                ((1 / side.risk) / (1 / side.risk).sum()).clip(0.30, 0.70) / 2
                for side in (longs, shorts)
            ]
            longs, shorts = longs.assign(weight=weights[0]), shorts.assign(weight=weights[1])
            expected = (longs.relative * longs.risk * longs.weight).sum() - (
                shorts.relative * shorts.risk * shorts.weight
            ).sum()
        else:
            longs, shorts = longs.assign(weight=0.25), shorts.assign(weight=0.25)
            expected = (longs.relative.mean() - shorts.relative.mean()) / 2
        if gated and expected < 0.0055:
            continue
        basket_rows.extend((longs.assign(side=1), shorts.assign(side=-1)))
    return pd.concat(basket_rows) if basket_rows else grid.iloc[:0].assign(side=0)


def replay_baskets(picks, histories, horizon, costs, starting_equity=INITIAL_EQUITY):
    bets, legs = [], []
    unavailable = too_small = 0
    equity = starting_equity
    for _, basket in picks.groupby("hour"):
        limit = stake_budget(equity, horizon, basket.tech_1h_realized_vol.median())
        fills = [
            trade_components(row, histories[row.coin], horizon, costs, limit * row.weight)
            for row in basket.itertuples()
        ]
        if any(trade is None for trade in fills):
            unavailable += 1
            continue
        if not all(fills):
            too_small += 1
            continue
        trade = {
            key: sum(leg[key] for leg in fills)
            for key in ("gross", "funding", "fees", "slippage", "net", "worst_open_pnl")
        }
        trade.update({key: fills[0][key] for key in ("hour", "exit_hour", "regime")})
        trade["coin"] = "basket"
        bets.append(trade)
        legs.extend(fills)
        equity += trade["net"]
    result = trade_summary(bets, starting_equity)
    coin_stats = trade_summary(legs, starting_equity)
    for key in ("best_coin", "net_by_coin", "net_without_best_coin"):
        result[key] = coin_stats[key]
    residual = {row["hour"]: row["net"] for row in bets}
    for row in legs:
        if row["coin"] == result["best_coin"]:
            residual[row["hour"]] -= row["net"]
    result["net_without_best_trade_and_coin"] = sum(residual.values()) - max(
        0, max(residual.values(), default=0)
    )
    return {
        **result,
        "signals": picks.hour.nunique(),
        "unavailable_outcomes": unavailable,
        "below_minimum": too_small,
        "hours_in_market": horizon * len(bets),
        "legs": len(legs),
    }, bets


def basket_reference(frame, histories, first, end):
    broad = "market_universe_breadth_24h" in frame
    members = [
        histories[coin]
        for coin in frame.loc[frame.hour.eq(first), "coin"]
        if (broad or histories[coin].is_meme) and first + 1 in histories[coin].bars
    ]
    marks = {}
    for day in range(first, end, 24):
        returns = []
        for history in members:
            opening = history.bars.get(day + 1)
            closing = history.bars.get(day + 23)
            if opening and closing and opening.volume > 0 and closing.volume > 0:
                returns.append(closing.close / opening.open - 1)
        if returns:
            marks[day // 24] = float(np.mean(returns))
    return pd.Series(marks, dtype=float)


def market_sensitivity(trades, benchmark, starting_equity=INITIAL_EQUITY):
    days = {}
    for row in trades:
        day = row["exit_hour"] // 24
        days[day] = days.get(day, 0) + row["net"] / starting_equity
    realized = pd.Series(days, dtype=float).reindex(benchmark.index, fill_value=0)
    variance = benchmark.var()
    valid = len(benchmark) >= 2 and variance > 0
    return {
        "utc_days": len(benchmark),
        "daily_realized_pnl_correlation": float(realized.corr(benchmark))
        if valid and realized.std() > 0
        else None,
        "daily_realized_pnl_beta": float(realized.cov(benchmark) / variance) if valid else None,
    }


def run_policy(
    fold_data,
    fold_bounds,
    histories,
    horizon,
    policy,
    phase=0,
    relative=None,
    benchmark=None,
    starting_equity=INITIAL_EQUITY,
):
    scored = pd.concat(fold_data)
    if policy.startswith("basket"):
        picks = choose_baskets(scored, horizon, phase, policy == "basket_gated", relative)
        simulate = replay_baskets
    else:
        picks = choose_signals(scored, horizon, policy, phase)
        simulate = replay
    base, _ = simulate(picks, histories, horizon, BASE, starting_equity)
    stress, trades = simulate(picks, histories, horizon, STRESS, starting_equity)
    individual = []
    for fold in fold_bounds:
        values = [row for row in trades if fold.start <= row["hour"] < fold.end]
        selected = picks.hour.between(fold.start, fold.end, inclusive="left")
        individual.append(
            {
                "start": hour_date(fold.start),
                "end_exclusive": hour_date(fold.end),
                "trades": len(values),
                "net": float(sum(row["net"] for row in values)),
                "signals": int(picks.loc[selected, "hour"].nunique()),
            }
        )
    split = (fold_bounds[0].start + fold_bounds[-1].end) // 2
    result = {
        "base": base,
        "stress": stress,
        "folds": individual,
        "positive_folds": sum(item["net"] > 0 for item in individual),
        "market_time_fraction": stress["hours_in_market"]
        / sum(f.end - f.start for f in fold_bounds),
        "stress_trades": trades,
        "stress_net_first_half": sum(row["net"] for row in trades if row["hour"] < split),
        "stress_net_second_half": sum(row["net"] for row in trades if row["hour"] >= split),
    }
    if benchmark is not None:
        key = (
            "universe_benchmark_sensitivity"
            if "market_universe_breadth_24h" in scored
            else "meme_benchmark_sensitivity"
        )
        result[key] = market_sensitivity(trades, benchmark, starting_equity)
    return result


def evaluate_mode(
    frame,
    histories,
    boundaries,
    horizon,
    mode,
    tails=False,
    relative=None,
    starting_equity=INITIAL_EQUITY,
):
    columns = market_data(frame, mode)
    scored = {"discovery": [], "validation": []}
    summaries = {"discovery": [], "validation": []}
    for fold in boundaries:
        predictions, summary = fit_predict(frame, fold, horizon, columns, tails, relative)
        scored[fold.period].append(predictions)
        summaries[fold.period].append(summary)
    result = {
        "feature_support_fraction": {
            name: float(value) for name, value in columns.notna().mean().items()
        }
    }
    policies = ["logistic", "boost", "return", "hybrid"]
    if tails:
        policies.extend(("tail_boost", "tail_return"))
    if relative:
        policies.extend(("basket", "basket_gated"))
    if mode == "native":
        policies.extend(sorted(BASELINES))
    for period, blocks in scored.items():
        period_folds = [fold for fold in boundaries if fold.period == period]
        combined = pd.concat(blocks)
        benchmark = basket_reference(frame, histories, period_folds[0].start, period_folds[-1].end)
        result[period] = {
            "predictions": {
                name: direction_accuracy(combined, combined[name], horizon)
                for name in ("logistic", "boost")
            },
            "folds": summaries[period],
            "policies": {
                name: {
                    str(phase): run_policy(
                        blocks,
                        period_folds,
                        histories,
                        horizon,
                        name,
                        phase,
                        relative,
                        benchmark,
                        starting_equity,
                    )
                    for phase in (0, horizon // 2)
                }
                for name in policies
            },
        }
    return result


def qualify(results, on_horizon, positioning_mode="strict"):
    shortlisted = []
    for mode, outcome in results.items():
        for policy, runs in outcome["discovery"]["policies"].items():
            if policy in BASELINES:
                continue
            notes = []
            if (mode == "positioning" and positioning_mode == "measurement") or mode == "media":
                notes.append(
                    "historical availability assumption limits this to an exploratory ablation"
                )
            scores = {}
            for period in ("discovery", "validation"):
                phase_results = outcome[period]["policies"][policy]
                for phase in runs:
                    row = phase_results[phase]
                    stress = row["stress"]
                    scores[f"{period}_{phase}"] = stress["net"]
                    if stress["net"] <= 0:
                        notes.append(f"{period} phase {phase} has nonpositive stress net")
                    if stress["unavailable_outcomes"]:
                        notes.append(f"{period} phase {phase} has unresolved attempted exits")
                    if row["positive_folds"] <= len(row["folds"]) / 2:
                        notes.append(f"{period} phase {phase} fails majority-positive folds")
                    if period == "validation":
                        tracks = {track: qualification(track, stress) for track in ("A", "B")}
                        if not any(track["pass"] for track in tracks.values()):
                            notes.append(f"{period} phase {phase} fails both tracks")
            shortlisted.append(
                {
                    "mode": mode,
                    "policy": policy,
                    "horizon": on_horizon,
                    "passes_historical_checks": not notes,
                    "stress_net": scores,
                    "reasons": notes,
                }
            )
    return shortlisted


def run(args):
    start, validation, end = map(day_hour, (args.start, args.validation_start, args.end))
    if not start + (WARMUP_DAYS + FOLD_DAYS) * 24 <= validation < end <= ARCHIVE_END_HOUR:
        raise ValueError(
            "Select historical Discovery and Validation dates within the development archive cutoff"
        )
    if args.lookback_days < 28:
        raise ValueError("Require at least 28 days of training history")
    if not np.isfinite(args.minimum_turnover) or args.minimum_turnover <= 0:
        raise ValueError("Minimum turnover must be positive and finite")
    if not np.isfinite(args.capital) or args.capital <= 0:
        raise ValueError("Starting equity must be positive and finite")
    boundaries = list(folds(start, validation, end, args.lookback_days))
    histories, info = load_archive(args.archive, end, include_quarters=True)
    if args.universe == "broad":
        from .broad_study import usable_histories

        histories, info["excluded_incomplete_market_data"] = usable_histories(histories, info)
    frame = build_frame(
        histories,
        start,
        end,
        minimum_turnover=args.minimum_turnover,
        broad=args.universe == "broad",
    )
    if args.context:
        context = pd.DataFrame.from_dict(load_context(args.context, start, end), orient="index")
        if context.empty:
            raise ValueError("The context file has no usable observations in this interval")
        frame = frame.join(context, on="hour")
    if args.media:
        media = pd.DataFrame.from_dict(load_context(args.media, start, end), orient="index")
        if media.empty:
            raise ValueError("The media file has no usable observations in this interval")
        frame = frame.join(media, on="hour")
    if args.positioning:
        positioning = load_positioning(args.positioning, start, end, mode=args.positioning_mode)
        if positioning.empty:
            raise ValueError("The positioning archive has no usable observations in this interval")
        frame = frame.join(positioning, on=["hour", "coin"])
    modes = args.modes or (["chart", "native", "context"] if args.context else ["chart", "native"])
    horizons = args.horizons or [4, 12]
    relative_target = args.relative_baskets
    report = {
        "archive_cutoff_exclusive": hour_date(end),
        "discovery_start": hour_date(start),
        "validation_start": hour_date(validation),
        "training_lookback_days": args.lookback_days,
        "fold_days": FOLD_DAYS,
        "training_only_warmup_days": WARMUP_DAYS,
        "initial_equity": args.capital,
        "maximum_stake_fraction": MAX_STAKE_FRACTION,
        "volatility_budget_fraction": RISK_BUDGET_FRACTION,
        "minimum_turnover": args.minimum_turnover,
        "universe": args.universe,
        "tail_calibration": args.tails,
        "relative_baskets": relative_target,
        "confidence_cutoff": CONFIDENCE,
        "archive_coverage": info,
        "context_source_file": str(args.context) if args.context else None,
        "media_source_file": str(args.media) if args.media else None,
        "positioning_path": str(args.positioning) if args.positioning else None,
        "positioning_availability_mode": args.positioning_mode if args.positioning else None,
        "stay_flat_usdc": 0,
        "models": {},
        "qualification": [],
        "execution": "Next hourly-open proxy, one position, modeled taker fills and hourly funding, fixed horizon.",
        ("equal_weight_buy_hold" if args.universe == "broad" else "equal_weight_meme_buy_hold"): {
            label: buy_and_hold(
                histories,
                frame.loc[frame.hour.eq(opening), "coin"],
                opening + 1,
                closing,
                RiskLimits(initial_equity=args.capital),
                meme_only=args.universe != "broad",
            )
            for opening, closing, label in (
                (boundaries[0].start, validation, "discovery"),
                (validation, end, "validation"),
            )
        },
    }
    for horizon in horizons:
        results = {}
        for mode in modes:
            results[mode] = evaluate_mode(
                frame,
                histories,
                boundaries,
                horizon,
                mode,
                args.tails,
                relative_target,
                args.capital,
            )
            summary = results[mode]["validation"]
            print(
                f"{horizon}h {mode}: AUC {summary['predictions']['boost'].get('auc', 0):.3f}",
                flush=True,
            )
        report["models"][str(horizon)] = results
        report["qualification"].extend(qualify(results, horizon, args.positioning_mode))
    write_json(args.output, report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--context", type=Path)
    parser.add_argument("--media", type=Path, help="Optional retrospective indexed-news ablation")
    parser.add_argument("--positioning", type=Path, help="Optional official derivatives archive")
    parser.add_argument("--positioning-mode", choices=("strict", "measurement"), default="strict")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 5, 25))
    parser.add_argument("--validation-start", type=date.fromisoformat, default=date(2026, 9, 26))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 11, 21))
    parser.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS)
    parser.add_argument("--minimum-turnover", type=float, default=1_000_000)
    parser.add_argument("--capital", type=float, default=INITIAL_EQUITY)
    parser.add_argument(
        "--universe", choices=("meme-and-recent", "broad"), default="meme-and-recent"
    )
    parser.add_argument("--modes", nargs="+", choices=tuple(FEATURES))
    parser.add_argument("--horizons", nargs="+", choices=HORIZONS, type=int)
    parser.add_argument(
        "--tails",
        action="store_true",
        help="Fit thresholds on earlier held-forward forecasts and report tail lift",
    )
    parser.add_argument(
        "--relative-baskets",
        nargs="?",
        const="raw",
        choices=("raw", "volatility"),
        help="Test equal-notional or volatility-scaled 2x2 relative-return baskets",
    )
    args = parser.parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
