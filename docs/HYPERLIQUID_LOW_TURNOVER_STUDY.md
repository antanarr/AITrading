# Lower-turnover perp strategies and a second venue

The intraday studies found that frequent entries gave back gross
profits through fees and modeled slippage. This study tests slower
directional positions and a same-venue perp funding hedge. It uses
the archived non-BTC Development periods. Live entries stay
paused, and the Reserve stays sealed.

## Slower directional positions

At each completed daily boundary, rank eligible markets by the
average of their 14- and 56-day log returns scaled by trailing
volatility and each horizon. Both directions must agree for
entry. The portfolio holds each position through routine daily
fluctuations, with a 3 ATR protective stop and a 45-day maximum.
One variant exits when the trend score reaches zero. A second
requires the score to move 0.5 units in the opposing direction to
cut down on exits near zero. Both use 1× exposure and position
sizes derived from volatility and current equity.

This adapts the multi-horizon idea from time-series momentum
research to shorter crypto histories. It tests only two fixed UTC
clocks, midnight and noon. Base and stress results use the native
4.5 bps taker fee and actual archived funding rates. The earlier
Binance sample uses its observed funding settlements and assumes
5 bps of taker fees per fill. Both venues use the same candle-based
slippage proxies. [Time-series momentum research](https://doi.org/10.1016/j.jfineco.2011.11.003)
· [Hyperliquid fees](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)

At midnight UTC, the ordinary exit made 14.37 USDC in Hyperliquid
Discovery and 4.99 in Validation after stress costs. It finished
Validation with 11 trades and 35.64 USDC of intrabar drawdown.
Without the best trade and coin, its Validation result was −7.27.
Moving the decision to noon made the Validation net −2.27.
The buffered exit also lost money in Hyperliquid Validation at both
clocks.

The same buffered rule made 125.69 USDT from 91 trades in the earlier
13-market Binance reference at midnight. At noon it made 128.70 USDT
from 89 trades. The two estimated drawdowns were 63.49 and 55.94 USDT.
Both clocks remained profitable after dropping their best trade
and coin in the continuous earlier sample. In the early-2026 reset,
the midnight version lost 0.54 and the noon version made 7.92
before the same concentration check, which made both negative.
The older run shows why the idea remains worth studying, and the
Hyperliquid Validation sample does not qualify it.

## Same-venue funding hedge

This test paired two different Hyperliquid perps with sufficiently
correlated returns and used the estimated hedge ratio to set their
notionals. The rules require the projected three- or seven-day
funding differential to clear the full stressed cost plus a buffer.
The stricter version requires both legs to collect funding. The
second permits one leg to pay if the pair still clears the hurdle.

The seven-day rule made 11.47 USDC in Discovery on a single stressed
fill. The broader version made 17.65 USDC across three fills.
Its gross price P&L was 18.38, funding was 0.89, and fees and slippage
were 1.63. It needed positive price P&L to make money. Both versions
had zero Validation trades. None of 3,169 matched Validation pairs
cleared the carry hurdle. [Hyperliquid funding rules](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)

## Hyperliquid and Kraken Futures

Both public order-book feeds were reachable. At 100 USD of modeled
immediate taker size, the median of three paired samples gave a
complete round-trip cost of 9.11–10.18 bps on Hyperliquid and
10.59–15.91 bps on Kraken for ETH, SOL, HYPE, XRP and AVAX.
That snapshot includes published base fees. It measures the input
conditions during the sampled period.

The existing daily breakout was then replayed on twelve matching
markets using common Hyperliquid signal times and 500 USD-equivalent
starting equity per venue. Each venue supplies its own prices,
settled funding observations, contract quantity precision and fee
schedule. Stressed fill costs use the same candle proxies.

| Fixed breakout, matched markets | Hyperliquid | Kraken | Closed trades per venue |
| --- | ---: | ---: | ---: |
| Discovery | +55.32 | +55.51 | 28 |
| Validation | +33.23 | +33.40 | 17 |

Both venues show profit during the same sampled market periods.
One missing Kraken funding interval overlapped four open positions,
which makes the small dollar difference inconclusive. Kraken linear
funding accrues during the hour. Its backtest counts completed hourly
funding with an hourly execution proxy. Validation still falls below
the existing 20-trade gate. Kraken is a second public data route,
and the existing signed order adapter is for Hyperliquid.
[Kraken linear perpetual funding](https://support.kraken.com/articles/4844359082772-linear-multi-collateral-derivatives-contract-specifications)
· [Kraken futures market analytics](https://docs.kraken.com/api/docs/futures-api/charts/market-analytics)
· [Kraken fee schedule](https://support.kraken.com/in/articles/360048917612-fee-schedule)

The fixed 1× daily breakout remains the leading Hyperliquid candidate
in this batch. The matched replay tests whether the same signals and
exits transfer between venues on the same dates. It adds execution
evidence and shares the underlying market regimes with the native
study.

## Reproduce

```sh
python -m copybot.buffered_trend_study state/backruns/2026-11-21-broad \
  state/backruns/2026-11-21-binance-majors state/backruns/2026-11-21-broad/buffered-trend.json
python -m copybot.funding_pair_study state/backruns/2026-11-21-broad \
  state/backruns/2026-11-21-broad/same-venue-funding-pairs.json
python -m copybot.funding_pair_study state/backruns/2026-11-21-broad \
  state/backruns/2026-11-21-broad/same-venue-funding-spread.json --allow-paying-leg
python -m copybot.kraken_venue_study state/backruns/2026-11-21-broad \
  state/backruns/2026-11-24-kraken-matched-cli.json
```
