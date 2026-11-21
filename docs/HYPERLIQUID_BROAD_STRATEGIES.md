# Broad Hyperliquid strategy comparison

This replay considers liquid native perpetuals across majors, alts and
memecoins. Each directional signal uses trailing traded volume to
determine eligibility per hour. The default minimum is 1 million USDC
of 24-hour turnover. The asset universe has no fixed ticker cap.
BTC aliases are excluded from trading and inputs. The study uses
Development data ending November 21, 2026 at 00:00 UTC, exclusive.
Discovery starts May 25 and Validation starts September 26.

The broader archive reuses the earlier public candles and funding
shards. The first expansion acquired 30 additional complete hourly
series, bringing that archive to 68 price histories. Market-level
coverage distinguishes price data from a complete execution and funding
path. Snapshots are bounded by exchange coverage and delisted markets
with no recoverable history can be underrepresented. The standard
directional screen covers native perpetuals. HIP-3 builder markets and
non-Hyperliquid products would need their own compatible execution and
data validation.

## Allocation and risk

The two starting balances are 300 and 500 USDC. The directional replay
recomputes the usable equity from marked positions and realized P&L
before each entry. It uses a 1% risk budget per leg, divides by estimated
stop loss including execution friction, and then applies shared
exposure limits and exchange lot rounding. The four-position account
allows up to 25% of equity per leg, 100% gross and 60% per side while
orders may fill asynchronously. The USD 10 venue minimum filters out
below-minimum orders. It supplies no target position size. A separate
walk-forward directional model targets 1% of equity per forecast
volatility unit under a 20% allocation cap. Its volatility estimate
does not guarantee that an adverse move will stop there.

The replay also excludes 25% of the realized-profit high from future
sizing. This models the owner's profit-budget idea. The amount is
bookkeeping and open positions can still lose against total account
equity. The run supports changing the fraction, including zero for a
comparison without this budget rule.

## Strategy definitions

| Reference | Replay adaptation |
| --- | --- |
| Hummingbot MACD-BB | Bollinger excursion with MACD conditions on completed 1h candles, with ATR stop and maker take-profit. |
| Freqtrade community trend | EMA20 cross with matching OBV change on 1h candles, with a trailing ATR exit. |
| LEAN Donchian channel | Breakout of the prior 20 complete 4h or daily highs or lows with a trailing ATR exit. |
| Major-alt pairs | Causal log-price OLS fit and AR half-life estimate from the preceding 30 days. One hedge ratio and spread model per weekly fit, threshold entries and outcome-blind reversion/divergence exits. |
| Hummingbot cross-venue funding | Opposite positions in the same asset on Hyperliquid and Binance, normalized funding differential, separately modeled fee schedules and both execution legs. |
| HyperCore spot/perp cash-and-carry | Buy native spot and short its matched perpetual, with pair identity checked beyond the ticker. Funding, basis, both execution legs, cash and margin all enter the replay. |

The directional adaptations use the same Hyperliquid risk and execution
engine. They test capped IOC and maker entry variants. Maker limits
require subsequent prices to trade through the limit. Candle-based
fills remain proxies for queue position and liquidity. The shared
configuration allocates two slots each to Donchian trend and MACD-BB
mean reversion under a single account-wide exposure budget, rejects
duplicate exposure on one market, and tests both priority orders.
It compares both 4h and daily Donchian trend paired with MACD-BB.
Each strategy uses capped IOC entries in the portfolio comparison.

An observed data gap while a position is open is marked with a proxy
exit for diagnostics. A run with such an exit cannot qualify. Both
historical periods must be positive at stressed costs and the selected
Validation strategy must pass Track A or Track B. Reserve data and
live execution remain separate.

The reference sources are the [Hummingbot controller](https://github.com/hummingbot/hummingbot/blob/02b87048d9c576d6ce2a017ecaa6a6f00cc2e5ea/controllers/directional_trading/macd_bb_v1.py),
[Freqtrade community strategy](https://github.com/freqtrade/freqtrade-strategies/blob/0727df98acc0b49497b1988da3dc37892be5503c/user_data/strategies/futures/TrendFollowingStrategy.py),
[LEAN Donchian indicator](https://github.com/QuantConnect/Lean/blob/ed551458f5f91bedd1409fe5dd8df373f1f1d8ca/Indicators/DonchianChannel.cs),
[Hummingbot funding script](https://github.com/hummingbot/hummingbot/blob/dd5a59eca1bfb6f2184306402d6ac19f4832c85a/scripts/v2_funding_rate_arb.py).

## Historical results

The full funded snapshot included 67 covered markets in Discovery
and 68 in Validation. At 500 USDC and the default 1% risk budget, the
published directional adaptations and the two carry studies gave
the stressed results below. A carry sleeve used the markets where
both instruments could be matched and priced.

| Strategy | Discovery | Validation |
| --- | ---: | ---: |
| Hummingbot MACD-BB, IOC | −46.93 | −25.65 |
| Freqtrade EMA/OBV, IOC | −325.60 | −126.07 |
| 20-bar Donchian, 4h, IOC | −109.82 | −55.32 |
| 20-day Donchian, IOC | −45.28 | +2.23 |
| 20-day Donchian, maker | −19.60 | +16.14 |
| Beta-hedged pairs | −3.84 | −25.77 |
| Daily trend and MACD-BB, shared book | −92.03 | −42.15 |
| Hummingbot cross-venue carry, 64 matched pairs | −7.61 | −7.21 |
| Native spot/perp carry, 28-day hold sensitivity | −1.03 | +0.56 |

The maker daily breakout failed the Discovery requirement. Native
28-day carry's Validation return came from a single trade. Sharing
capital with MACD-BB reduced the daily breakout's stressed returns
in both periods. Neither shared book qualified.

The daily IOC breakout showed an edge worth measuring again, with
clear sensitivity to sizing and breadth. Its initial 49-market
replay had positive stressed returns in both periods at all three
daily boundaries, with 300 and 500 USDC. At 61 markets, the 500 USDC
run was positive in both periods only at 16:00 UTC. At 68 markets,
the 00:00 UTC version also failed Discovery at 300 USDC. Adding
markets changes which signals compete for the shared risk and slots.

| 68-market daily IOC | $300 Discovery | $300 Validation | $500 Discovery | $500 Validation |
| --- | ---: | ---: | ---: | ---: |
| 00:00 UTC, 1% risk | −19.17 | +7.59 | −45.28 | +2.23 |
| 08:00 UTC, 1% risk | +13.60 | +7.50 | +16.40 | −9.45 |
| 16:00 UTC, 1% risk | +4.22 | +12.97 | +4.04 | +11.56 |
| 00:00 UTC, 2% risk | −51.45 | +4.47 | −85.65 | +7.83 |

At 500 USDC, the 16:00 version's estimated intrabar Discovery drawdown
was 68.03 USDC and Discovery net became −16.16 USDC after jointly
excluding the best trade and best asset. These offsets measure
robustness. Picking the most profitable historical clock would change
the strategy after viewing the outcomes. The full-roster 00:00 replay
did not qualify at either balance.

At 500 USDC the primary daily breakout put a median 31.87 USDC into
positions in Discovery and 30.67 in Validation. Validation legs ranged
from 12.32 to 122.80 USDC. Doubling the stop-risk budget increased the
median allocations to 56.51 and 60.91 USDC. More exposure amplified
losses in the earlier market. The tested allocation changes with
current equity, volatility and existing positions.

## Reproduction

```sh
python -m copybot.broad_archive state/backruns/2026-11-21-meme \
  state/backruns/2026-11-21-broad --phase candles --limit 68
python -m copybot.broad_archive state/backruns/2026-11-21-meme \
  state/backruns/2026-11-21-broad --phase funding --limit 68
python -m copybot.broad_study state/backruns/2026-11-21-broad \
  state/backruns/2026-11-21-broad/directional.json --capital 300 500 --pairs
python -m copybot.broad_study state/backruns/2026-11-21-broad \
  state/backruns/2026-11-21-broad/clock-16.json --capital 300 500 \
  --only donchian-20-24h-ioc --daily-offset 16
python -m copybot.public_carry state/backruns/2026-11-21-broad \
  state/backruns/binance-public-carry state/backruns/public-carry.json \
  --complete-native-funding
python -m copybot.directional_study state/backruns/2026-11-21-broad \
  state/backruns/2026-11-21-broad/model.json --universe broad --capital 500 \
  --context state/backruns/2026-11-20-directional/context.jsonl \
  --modes native context --horizons 12 48
```

The earlier [directional study](HYPERLIQUID_DIRECTIONAL_STUDY.md) used
fixed 100 USDC model stakes. Use commits `0240e40` and `7a6da70` to
reproduce those fixtures and historical results. The current walk-forward
command sizes from the simulated balance and volatility.

## Liquidity-ranked daily trend

The follow-up tests the highest 20 markets by trailing 30-day turnover
at the decision time. Ranking includes eligible markets even when they
have no breakout. Both variants enter on a 20-day breakout. The
enhancement requires alignment with the preceding 50 completed daily
closes and uses a 10-day opposite-channel exit. It retains the 2 ATR
protective stop and 10-day maximum hold. It uses the same equity risk,
execution caps, funding and profit budget as the previous broad study.

| Enhanced strategy | $300 earlier | $300 later | $500 earlier | $500 later |
| --- | ---: | ---: | ---: | ---: |
| Midnight UTC | +27.49 | +15.93 | +55.63 | +24.72 |
| 08:00 UTC | +20.91 | +14.06 | +29.74 | +24.90 |
| 16:00 UTC | +36.75 | +4.56 | +60.36 | +0.86 |

All amounts are USDC after stress costs. At midnight UTC and 500 USDC,
the strategy won 14 of 19 Validation trades, with 19.41 USDC estimated
intrabar drawdown and 12.34 USDC net after jointly excluding its top
trade and asset. The midnight run is one trade short of the earlier
20-trade qualification minimum. The 16:00 UTC Validation run turns
negative after the outlier exclusion at 500 USDC. The simpler top-20
variant at midnight UTC lost 47.97 USDC in Discovery and earned 26.47
in Validation at the 500 USDC balance.

The fixed midnight rule was also run against 13 major Binance USD-M
contracts from January 2024 to May 24, 2026 UTC, exclusive. The
archive includes 272,688 hourly bars and 34,086 observed funding
settlements. The enhanced strategy earned **33.32 USDT** over 239
trades starting from 500 USDT in the continuous stressed run. These
results come from a separate market archive and replay. The
continuous estimated intrabar drawdown was 132.12 USDT, and results
excluding its top trade and asset were −3.15 USDT. Separate tests
reset to 500 USDT at each period and earned 86.39 USDT in 2024, lost
36.60 in 2025 and lost 6.05 from January through May 23, 2026. The
reference replay estimates Binance fills with candle proxies and
assumes 5 bps taker fees. [Binance public data](https://github.com/binance/binance-public-data#futures)

This is a paper candidate with observed after-cost profitability
and uneven returns by market period. The Reserve and live worker
remain separate.

```sh
python -m copybot.broad_study state/backruns/2026-11-21-broad \
  state/backruns/2026-11-21-broad/top-20-sma.json --capital 300 500 \
  --liquidity-top-n 20 --enhanced-trend \
  --only donchian-20-24h-ioc-top20-sma50-channel10
python -m copybot.binance_regime_archive state/backruns/2026-11-21-binance-majors \
  --reuse state/backruns/2026-11-20-directional/transfer
python -m copybot.liquid_trend_reference state/backruns/2026-11-21-binance-majors \
  state/backruns/2026-11-21-binance-majors/liquid-trend-continuous.json --capital 500
```

## Leverage with the same collateral

Each policy started with 500 USDC and kept the same collateral allocation
rules. Larger multipliers increased notional exposure and the amount each
stop could lose. Lot sizes, minimum notional, exchange caps and the account
balance after each trade can change which opportunities are filled.
Strict 5× skips contracts capped below 5×. Capped 5× uses the contract's
maximum if it is lower. The mixed policy uses 1× when more than two
signals arrive together or a same-side position is already open. Otherwise
it uses 3×, or 5× when the breakout exceeds the channel by at least
0.5 ATR, subject to the contract cap. The breakout distance is a
heuristic. It has no calibrated win probability.

The replay applies the low-tier maintenance formula using archived census
caps. Historical mark prices and live order-book fills are unavailable.
Traded-price candles stand in for mark prices, and an ambiguous candle
crossing both barriers is scored as liquidation first. Liquidation is
modeled as loss of the full posted collateral plus the entry fee. The
cascade scenario shifts the proxy mark 1% against the position and adds
3% slippage to protective stop exits. These shocks are stress assumptions.
[Hyperliquid liquidation mechanics](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations)

Gemini's hostile review focused on the reused research windows,
dependence among same-day positions, clock sensitivity, and the proximity
of protective stops to liquidation. The leverage replay counts entry-day
clusters and reports results without the best cluster as well as the
existing trade and coin concentration checks.

### Midnight UTC results on 500 USDC

| Policy | Earlier stress net | Later stress net | Later intrabar drawdown | Earlier / later liquidation proxies | Later cascade net |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1× | +55.63 | +24.72 | 19.41 | 0 / 0 | +24.19 |
| 3× | +117.21 | +76.65 | 57.40 | 1 / 0 | +74.71 |
| Strict 5× | +375.26 | +67.85 | 106.85 | 1 / 3 | +27.74 |
| Capped 5× | +269.39 | +89.72 | 94.86 | 2 / 2 | +48.08 |
| Mixed | +61.84 | +14.78 | 28.78 | 0 / 0 | +13.16 |

All policies had 19 completed later-window trades across 9–12 entry
days. Median posted collateral was 21.68–23.67 USDC per trade in that
window. The mixed policy used 1× on 17 trades and 3× on two. Strict
5× reached six modeled later liquidations under the cascade scenario,
and capped 5× reached five. The midnight 3× replay had 11 later trades
whose initial stop plus the extra slippage allowance used at least 75%
of the distance to modeled liquidation.

At 16:00 UTC, later stress net was +0.86 USDC at 1×, −36.51 at 3×,
−89.42 at strict 5×, −106.92 at capped 5× and −1.83 in the mixed
policy. All five lost money at this boundary in the cascade scenario.

As an older-regime sensitivity, the Binance candle archive was replayed
with the same collateral rules, 5 bps taker fees and an assumed 10%
maintenance charge per contract. That last assumption is a scenario,
since the archive lacks historical Binance margin tiers and mark prices.
On a continuous 500 USDT run, net was +33.32 USDT at 1×, −52.46 at 3×,
−481.55 at 5× and +16.29 in the mixed policy. Removing the best trade
and coin made the mixed result −45.70 USDT. Both 3× and the mixed policy
lost more than 150 USDT in the standalone 2025 replay.

The higher leverage increased midnight profit in several replays.
Changing the daily boundary or testing on the older archive exposed
large losses. The midnight variants are below the 20-trade minimum.
No policy was frozen for the Reserve.

```sh
python -m copybot.liquid_trend_leverage state/backruns/2026-11-21-broad \
  state/backruns/2026-11-21-broad/daily-top-20-leverage-cascade.json
```
