# Hyperliquid 1× and 3× forward study

The paper service starts two independent 500 USDC portfolios and evaluates
the same fixed trend signals in both. The first eligible decision is
November 22, 2026 at 00:00 UTC, or November 21 at 6:00 p.m. Central.
The scanner starts at that boundary and has up to 15 minutes to verify
the data and model entry. It uses closed candles to rank the top 20
liquid native non-BTC perps by trailing 30-day turnover, then tests
the 20-day breakout with prior SMA50 alignment. It keeps the 2 ATR
protective stop, 10-day opposing channel exit and 10-day timeout.
The code records why it skipped a decision or an individual order.

Each book sizes from its own balance with the same collateral rules.
Both books see the same observed order depth at each decision. The
entry uses a 40 basis-point cap from the signal close. The model walks
the visible bids or asks through the order's limit and cancels any
unfilled entry quantity. An exit can retry the remainder against a new
book. The study charges the standard native-perp base taker fee of
0.045% on each modeled fill and applies published hourly funding
rates using the nearest sampled oracle price. It stores fill levels,
exchange and receipt timestamps, fees, funding, and drawdown.
[Hyperliquid fees](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)
· [Hyperliquid order-book API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)

The study samples market context about every ten seconds. It uses
the observed mark for stops and maintenance margin, with liquidation
counted first if a sample crosses both thresholds. Mark moves between
samples, network delay, and changes in available liquidity can produce
different results on the exchange. Mark, order-book, and funding gaps
are recorded. Review them before comparing realized results.
[Hyperliquid stops](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/take-profit-and-stop-loss-orders-tp-sl)
· [Hyperliquid liquidations](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations)

The service makes public market-data calls only. Its dedicated volume
holds `/data/forward-paper.sqlite3`. The operational worker, market-data
recorder, and Reserve use separate state. The prospective paper period
is development evidence for this experiment and should not be reused
as the Reserve.
[Railway paper study](https://railway.com/project/7cc71f2d-5c3a-4e79-916e-85e59e106503/service/2f4eea82-01dc-4a02-84ed-3a8297a950d4)

For a local input check, use a disposable journal path with `--probe`.
The Docker image for the ongoing study is built from `Dockerfile.forward`.

```sh
python -m copybot.forward_paper --journal state/paper-probe/forward.sqlite3 --probe
```

## Separate strategy research

The New York cash-session study tests whether the broad non-BTC crypto
move from 10 to 11 a.m. Eastern continues or reverses from 3 to 4 p.m.
It also tests the same rules shifted eight hours as a comparison.
In the 61-market Development archive at 500 USDC, following the move
lost 41.98 USDC in Discovery and 29.95 USDC in Validation under stress.
Fading the move lost 75.22 and 51.17 USDC, respectively. Both time-shifted
variants also lost in both windows. For the follow rule's Validation
trades, 11.07 USDC in gross price gains fell short of 41.02 USDC in fees
and modeled slippage. No version advanced.

The rerun on the 68-market archive reached the same conclusion.
Following the move lost 28.61 USDC in Validation after stress costs.
Fading it lost 48.41 USDC. The shifted controls also lost money.

```sh
python -m copybot.intraday_session_study state/backruns/2026-11-21-broad-final-snapshot \
  state/backruns/2026-11-21-broad/us-cash-session-rotation.json
python -m copybot.intraday_session_study state/backruns/2026-11-21-broad \
  state/backruns/2026-11-21-broad/us-cash-session-rotation-broad-68.json
```
