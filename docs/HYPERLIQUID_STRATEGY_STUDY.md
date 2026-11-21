# Hyperliquid strategy study

**Keep live entries paused.** The 15-minute breakout lost before fees. The
3-day trend breakout was the strongest alternative over the recent window and
still lost over 180 days. Its recent profit depended heavily on one BTC short.

## Research behind the rules

- Moskowitz, Ooi and Pedersen found that returns tended to persist over one
  to 12 months in liquid traditional futures. Liu and Tsyvinski found that
  Bitcoin returns predicted returns over some subsequent days and weeks.
  I used that evidence to test longer breakout windows and give winners more
  room. The channel lengths and trailing stops are hypotheses for this bot.
  [Time Series Momentum](https://fairmodel.econ.yale.edu/ec439/jpde.pdf)
  [Risks and Returns of Cryptocurrency](https://www.nber.org/system/files/working_papers/w24877/w24877.pdf)
- Wen and colleagues found both intraday momentum and reversal in crypto.
  Results varied with jumps and liquidity. I tested one reversal that requires
  a completed move back into a range after an extreme, with a filter for
  quiet markets. [Intraday Return Predictability in the Cryptocurrency Markets](https://www.sciencedirect.com/science/article/pii/S1062940822000833)

## Why the deployed strategy lost

The 14-day replay's 84 trades lost 1.8124 USDC. Candle price movement accounted
for 0.5974 USDC of that loss, modeled spread and price impact for 0.4338 USDC,
and exchange fees for 0.7808 USDC. Funding cost 0.0004 USDC. This decomposition
holds the original entry and exit decisions fixed.

| Exit in the original 14-day replay | Trades | Net USDC |
| --- | ---: | ---: |
| Failed breakout | 49 | −1.9736 |
| Stop | 11 | −0.9367 |
| Time limit | 19 | +0.3878 |
| Take profit | 5 | +0.7102 |

Of the 49 failed breakouts, 27 reversed by the first post-entry candle close.
Keeping those same 49 entries open until the original stop, target or 90-minute
limit would have rescued seven into winners, but lost 2.0586 USDC as a group.
A separate portfolio replay with the early exit removed lost 1.5080 USDC. Its
different holding times also changed which later entries were available.

The longer replay reached the six-entry cap on all 48 days. Each of the six
daily entry slots had negative net P&L when aggregated across the sample.
All four markets and both trade directions lost. The same-trade loss was
1.0527 USDC from price movements, 1.4903 USDC from modeled spread and impact,
2.6825 USDC from fees and 0.0008 USDC from funding. The base run finished down
11.14% on the approximately 47 USDC starting balance.

## 48 days with 15-minute execution bars

October 3 through November 19, 2026 UTC. The middle 16 days, October 21
through November 5, were reserved for comparison. The final 14 days overlap
the earlier replay. Rules were set in advance and thresholds were left fixed.
Each partition starts flat with the same balance. All figures are net USDC.

| Rules | Trades, all days at base costs | All 48 days | All 48 days, higher costs | Middle 16 days | Middle 16 days, higher costs |
| --- | ---: | ---: | ---: | ---: | ---: |
| Deployed breakout | 288 | −5.2263 | −11.7102 | −1.6747 | −3.7722 |
| Deployed entry with fixed exits | 288 | −4.9694 | −11.8805 | −1.6060 | −3.7646 |
| Volume and trend confirmation | 284 | −4.3564 | −10.5474 | −1.1073 | −3.1549 |
| 20-hour trend breakout | 63 | −2.8022 | −4.1232 | +0.2606 | −0.1581 |
| 3-day trend breakout | 12 | +0.8123 | +0.5517 | +0.0920 | +0.0030 |
| Hourly range reentry | 32 | −0.0025 | −0.7771 | +0.0376 | −0.3221 |
| Stay flat | 0 | 0 | 0 | 0 | 0 |

The stricter 15-minute filter reduced losses but still took 284 trades.
The 3-day rule had five winners from 12 trades. Its best BTC short made 0.7932
USDC, leaving 0.0192 USDC across the other eleven trades. The middle 16-day
result had only four trades.

## 180 days with hourly execution bars

May 24 through November 19, 2026 UTC. The last 60 days, September 21 through
November 19, are a separate chronological partition that overlaps the 48-day
study. The older execution bars support slower rules at this coverage.
All figures are net USDC.

| Rules | Trades, all days at base costs | First 120 days | Last 60 days | Last 60 days, higher costs | All 180 days | All 180 days, higher costs |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 20-hour trend breakout | 226 | −0.6916 | −4.3660 | −6.0903 | −5.5176 | −10.6756 |
| 3-day trend breakout | 54 | −2.0961 | +0.7896 | +0.4608 | −1.3065 | −2.2263 |
| Hourly range reentry | 155 | −4.2674 | −0.2781 | −1.2818 | −4.5455 | −7.8507 |
| Stay flat | 0 | 0 | 0 | 0 | 0 | 0 |

The 3-day breakout had a 3.39 USDC realized drawdown over six months. It made
0.7896 USDC across 15 trades in the recent 60-day partition. Excluding that
same BTC short leaves −0.0036 USDC at base costs and −0.3116 USDC at higher
costs. If testing continues, freeze this rule for an independent period and
require the results to hold after costs without relying on its best trade.

## Rules and execution assumptions

- **Deployed breakout.** Reuses the signal ranker, order sizing and protection
  price logic. It uses a 15-minute close beyond the prior three-hour range
  with trend confirmation, a roughly 0.75% initial stop, 1.5% profit target,
  a 90-minute limit, and an exit when a completed candle crosses the breakout
  level. The fixed-exit variant omits that last rule.
- **Volume and trend confirmation.** Same exits as the deployed rule. Entry
  also requires 1.5 times recent median candle volume and aligned four-hour
  price moves in the asset and BTC.
- **20-hour and 3-day trend breakouts.** A close outside the prior channel and
  on the same side of the prior 48-hour or 10-day mean close, respectively.
  The shorter rule uses hourly signals and a 36-hour time limit. The longer
  rule uses four-hour signals and a five-day time limit. Stop distance is
  twice the average true range of the last 14 signal bars. The stop tightens
  from the best completed close and there is no fixed profit target.
- **Hourly range reentry.** One hourly close outside a two-standard-deviation
  band around the previous 20-hour mean, followed by a close back inside and
  still short of that mean. Skip if the preceding 48-hour net drift exceeds
  twice the then-observable hourly average true range. Use the frozen mean
  as the target, a 1.5-average-true-range stop and a 12-hour time limit.
- **Portfolio.** BTC, ETH, SOL and HYPE perps, one position, trial-sized orders
  around 10 USDC with an 11 USDC cap and current lot sizes. Same daily limits
  across rules. The slower rules skip trades with initial stops outside
  0.5% to 4%. The historical ranker used four liquid peers for breadth and
  an external input removed from the current research harness.
- **Costs.** Use the documented 4.5-basis-point base taker fee on each side and
  actual historical hourly funding rates with the preceding candle close as
  a price proxy. Base spread and impact total roughly 5 basis points round
  trip before fees. The higher-cost run uses 8 basis points of full spread,
  4 of entry impact and 15 of exit impact, with the original entry price cap.
  [Hyperliquid fees](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)
  [Hyperliquid funding](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)
- **Fills.** Use the next bar's open as the midpoint and assume depth for the
  modeled order. Stops take priority if both levels are touched in one bar.
  Opening gaps use the adverse opening price. Trailing values only use closed
  bars. End-of-sample exposure is closed at the final close. Candle prices
  stand in for live marks and quotes. Order-book depth, trigger execution
  through gaps, and live fill probability remain unverified. Drawdown is
  based on closed trades and excludes open-position drawdown.

## Reproduce

Market candles, sizes and funding came from Hyperliquid's public
`/info` endpoint. Checked contiguous candles and hourly funding for each
required market. Independent OHLC checks matched across overlapping bar
resolutions. Some SOL and HYPE volume sums differed across resolutions, so
the study keeps native volumes at each execution resolution. The original
5-minute replay reproduced the saved results byte for byte.
[Hyperliquid public info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)

The local archive is in `state/backruns/2026-11-20-strategy`. It holds each
native candle series as `<coin>-15m.json` or `<coin>-1h.json`, a `meta.json`
response, prior daily `sentiment.json` observations, and funding arrays
`<coin>-funding-<days from end>.json` at offsets 0, 16, 32, and so on.

```sh
python -m copybot.strategy_study state/backruns/2026-11-20-strategy \
  --end-date 2026-11-20 --starting-usdc 46.935912
```

This command is available in archived commit `e4aa566`. It writes
`study-results.json` with both cost models, each time partition, individual
trades, and breakdowns by coin, side, week and exit. Use the
[current market research harness](HYPERLIQUID_MEME_STUDY.md) for new tests.
