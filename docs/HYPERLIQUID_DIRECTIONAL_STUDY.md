# Hyperliquid directional model study

The paused worker uses a 15-minute breakout and gives a signal up to five
minutes to enter. Relative volume, non-BTC perpetual breadth and funding
affect its ranking. The offline study below trains models to forecast
direction and return. The study has no order entry or access to the live journal.

## Experiment

The development archive covers May 25 through November 20, 2026 UTC.
Discovery starts May 25, Validation starts September 26 and the exclusive
cutoff is November 21. The first 42 days are training only. Models train
on up to 56 earlier days, then forecast an unseen 14-day block. Training
includes only labels whose modeled exits precede the block. Each test
block also ends before any selected trade would cross that boundary.
Prior blocks become eligible training data after they close. The separate
strategy Reserve was never opened.

There are 36,766 eligible market-hour decisions across 24 non-BTC native
perps, including 690 recent-listing decisions. The 79 native features
include returns, ATR, realized volatility, RSI, relative volume, range
position and distance from VWAP or trend on 15m, 1h, 4h and 1d bars.
Cross-sectional breadth, dispersion, relative strength, venue funding
and premium provide positioning and sentiment context. The 15m archive
starts in October and is unavailable for some markets. The Hyperliquid
census and archive favor markets with recoverable history, so survivorship
remains a limit. BTC and its aliases never enter the feature or trading universe.

Three models use fixed regularization. Logistic regression estimates the
probability of an up move, shallow gradient boosting estimates the same
probability with interactions, and a separate boosted regressor estimates
price return plus funding. The hybrid follows the 24h coin trend when
it agrees with meme breadth. When the two disagree, the classifier chooses
a side only if its score is at least 55% for that direction. The return
policy requires predicted magnitude to exceed modeled stressed friction
plus 10 bps. Earlier out-of-time predictions determine the selective
policy's top-fifth threshold per forecast hour. Tail reports also describe
return bins defined by earlier score quantiles and within-hour rank
correlation. A later experiment estimates relative return directly.

The simulator starts with 1,000 USDC and models up to 100 USDC per signal.
It enters at the next hourly open proxy and closes 4, 12, 24 or 48 hours
later, with lot rounding, the $10 minimum, archived hourly funding, and
round-trip base and stress friction. It tests two entry clocks per horizon.
The ranking experiment selects two longs and two shorts, with each side
allocated half the budget. It compares equal weights with capped inverse
volatility weights. All four fills are modeled together. Hourly prices
cannot establish simultaneous executable fills, and summed adverse leg
extrema only approximate basket drawdown. These runs use fixed holding
times without modeling the existing worker's stops or take-profits.

## External feeds

| Feed | Historical input and availability |
| --- | --- |
| Broad risk and macro | S&P 500 and Nasdaq closes, VIX, two- and ten-year yields, and WTI. Equity and VIX data wait until the next New York midnight, yields wait two days and WTI waits seven. The FRED series are lagged and their historical revisions were not verified against vintages. |
| Official news | 47 Fed speeches, six FOMC decisions including warmup, and four CFTC digital-asset headlines. Same-day FOMC text gets a one-hour lag. Speeches and press releases with dates wait until the next New York midnight. Fed tone is a fixed lexical score. |
| Derivatives positioning | 1,991 public Binance USD-M archive files for 11 matched assets. They add open interest, top trader positioning and taker imbalance. The strict variant waits for the archive object's observed publication date. The measurement-time sensitivity run assumes the measurements were obtainable with a 15-minute lag and cannot qualify by itself. |
| Media sentiment | 265 retrospectively indexed memecoin headlines from 130 publisher names. BTC references are excluded and text waits 24 hours after the published time. Today's index can select articles differently from an earlier live feed, so this is an exploratory sensitivity test. No social history was substituted. |

Sources include [Cboe VIX](https://www.cboe.com/tradable_products/vix/vix_historical_data/),
[FRED market data](https://fred.stlouisfed.org/series/SP500),
[Fed speeches](https://www.federalreserve.gov/newsevents/speech/2026-speeches.htm),
[Federal Reserve monetary policy releases](https://www.federalreserve.gov/newsevents/pressreleases/2026-press-fomc.htm),
[CFTC releases](https://www.cftc.gov/PressRoom/PressReleases), and
[Binance Public Data](https://github.com/binance/binance-public-data).

## Results

| 12h feature set | Validation AUC, boosting |
| --- | ---: |
| Price and volume charts | 0.525 |
| Hyperliquid charts, breadth, funding and premium | 0.549 |
| Native plus macro | 0.570 |
| Native plus macro and official news | 0.567 |
| Official inputs plus Binance publication-time positioning | 0.565 |
| Official inputs plus hypothetical faster Binance availability | 0.556 |
| Official inputs plus retrospective headline sample | 0.547 |

The increase from native data to macro in Validation did not turn the model
profitable under stress. At 12h, the native hybrid made 78.34 USDC on one
entry clock and lost 135.39 USDC on the other in Validation. Both Discovery
clocks lost. The 24h context return model's confidence filter made
24.80 and 26.91 USDC in Validation, from three and four trades respectively.
It lost on both Discovery clocks.

The 48h gradient booster with official context had 0.625 Validation AUC
and 0.470 Discovery AUC. At the ordinary 50% threshold, Validation accuracy
was 58.2% while always predicting down would have been right 64.7% of the
time. The two 48h entry clocks made 24.96 and 37.70 USDC after stress costs
in Validation, and lost 65.63 and 228.14 USDC in Discovery. One Validation
clock lost 3.48 USDC after excluding its best remaining trade and best coin.
Stay-flat earned 0. Equal-weight meme buy-and-hold earned 230.81 USDC
in evaluated Discovery and lost 538.32 USDC in Validation. The phases
included sharply different conditions.

The 24h context model's rank correlation was −0.001 in Discovery and
+0.116 in Validation, averaged across folds. Directly training on
within-hour relative returns and trading an equal-notional two-by-two basket
lost on both Discovery clocks for every 12h and 24h variant, including
baskets that required a predicted edge after friction.

The next pass removed raw macro levels, fitted volatility-normalized
relative returns, and used capped inverse-volatility weights. The 48h
basket earned 21.79 and 8.19 USDC in Validation on the two clocks. It
lost 45.45 and 64.47 USDC in Discovery, with losses in both halves on
each clock. Daily realized P&L correlations with the phase-opening
meme basket in Validation were +0.19 and +0.03. That weaker directional
exposure still came with inconsistent after-cost returns. The
single-position 48h booster without macro levels also flipped across
clocks, from +10.74/+70.40 USDC in Discovery/Validation at the first
start to −277.44/−4.46 at the second.

No policy met the same stress-cost Track A or Track B on both
Validation clocks, positive net in both periods, and a majority
of profitable test blocks in each period. The learner remains offline.
The full results include base and stress P&L, funding, friction, risk,
win rate, market time, concentration checks and per-fold outcomes.

## Earlier market cycles

As a separate experiment, chart models train on Binance USD-M data from
January 2024 through May 24, 2026. The Hyperliquid decisions and exits
still come from the same development archive. The comparison is
restricted to symbols with eligible history on both venues. It scores a
Hyperliquid-only chart model, a Hyperliquid-only model with the official
context inputs, a fixed model trained on the earlier Binance bars, and
a 50/50 blend of the last two. The fixed model learns from gross chart
returns because its input has no funding history. The test applies
Hyperliquid research funding, lot sizes and friction to all four policies.
This is a transfer test on matched contracts, with selection bias and
possible differences in venue liquidity and participant behavior.

The source archive contains 176,302 hourly candles across 11 matched
contracts. After eligibility and cutoff checks, there were 175,877
earlier-venue labels at 12h and 175,481 at 48h. The matched Hyperliquid
study contained 30,352 eligible market-hour decisions.

At 48h, the older-venue gradient booster made 51.06 and 51.25 USDC in
Validation after stress costs. It lost 78.39 and 69.36 in Discovery.
The blended 48h logistic model made 97.07 and 40.55 in Validation,
then failed the historical gate with Discovery losses of 3.61 and
95.47. At 12h the older-venue booster improved Validation AUC to
0.568 from 0.516 for the matched Hyperliquid chart model, and still
lost 16.26 on one Validation clock. Adding these earlier chart regimes
did not produce a qualifying model or establish that cross-venue
funding, archived delistings, or richer inputs would transfer.

## Reproduce

```sh
python -m pip install -r requirements-directional.txt
python -m copybot.directional_context state/backruns/context.jsonl \
  --start 2026-05-25 --end 2026-11-21
python -m copybot.directional_study state/backruns/2026-11-21-meme \
  state/backruns/directional.json --context state/backruns/context.jsonl \
  --modes chart native macro context --horizons 4 12 24 48 --tails
python -m copybot.directional_study state/backruns/2026-11-21-meme \
  state/backruns/directional-volatility.json --context state/backruns/context.jsonl \
  --modes stationary --horizons 12 24 48 --tails --relative-baskets volatility
python -m copybot.directional_transfer state/backruns/binance-transfer
python -m copybot.directional_transfer_study state/backruns/2026-11-21-meme \
  state/backruns/binance-transfer state/backruns/context.jsonl \
  state/backruns/transfer-replay.json
```

Use `copybot.directional_positioning` to download the positioning archive
and pass its path with `--positioning` and `--modes positioning`. Use
`copybot.directional_media` to build the indexed media feed and add it
with `--media` and `--modes media`. The allocation runs in research at
these sizes. It does not establish feasibility for the original small
account or permission to activate the worker.
