# Hyperliquid backtests before a funded pilot

The study compares the existing daily breakout with two other strategy
families. Every run uses non-BTC markets and data dated before
November 21, 2026 UTC. The later Hyperliquid window was used in prior
research. It remains development evidence. The Reserve stayed sealed.

## Fixed breakout

The original 20-day breakout, SMA50 filter, 2 ATR stop, opposite
10-day channel exit and 10-day timeout stayed fixed. At 500 USDC,
the same collateral rules size the 1× and 3× books. The native replay
checks midnight UTC, 08:00 UTC, 16:00 UTC, and a one-hour delay on
midnight entries and channel exits. The delayed run retains the
original signal and price cap. It tests a latency scenario. The
ongoing paper service has a shorter deadline.

Hyperliquid tests use 68 markets, the existing Discovery and Validation
windows, the full continuous period, and six non-overlapping monthly
resets from June through November 20. The earlier Binance sensitivity
uses 13 major contracts from January 2024 through May 23, 2026, with
ten non-overlapping quarterly resets plus one continuous run. Each
reset starts flat at 500 and closes any remaining positions at its
end. Continuous runs preserve equity and open positions within
their own windows.

All runs include base and stress slippage assumptions as well as a
cascade scenario with 3% extra stop slippage and a 1% adverse mark
displacement. Native taker fees are 4.5 bps per fill. The Binance
scenario assumes 5 bps and a 10% maintenance rate. Both archives have
settled funding rates. These long replays use traded-price candles
as a mark and fill proxy. They cannot establish actual liquidation
or execution outcomes. [Hyperliquid fee schedule](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)

### Results after stress costs

| Window | 1× net | 3× net | 1× drawdown | 3× drawdown |
| --- | ---: | ---: | ---: | ---: |
| Hyperliquid, continuous midnight | +94.78 | +221.78 | 33.71 | 102.07 |
| Hyperliquid, continuous with one-hour delay | +46.73 | +207.28 | 34.01 | 108.36 |
| Hyperliquid, Validation midnight | +24.72 | +76.65 | 19.41 | 57.40 |
| Hyperliquid, Validation 08:00 UTC | +24.90 | +64.38 | 28.98 | 93.68 |
| Hyperliquid, Validation 16:00 UTC | +0.86 | −36.51 | 41.19 | 137.28 |
| Binance, continuous midnight | +33.32 | −52.46 | 132.12 | 452.12 |
| Binance, continuous with one-hour delay | −43.44 | −138.02 | 143.44 | 435.50 |

Native results are USDC. Binance results are USDT and use the
maintenance scenario described above. At midnight, both books made
money in four of six Hyperliquid monthly resets. On Binance, 1× made
money in six of ten quarterly resets and 3× made money in five.
Each won three of ten under the cascade scenario, and three of ten
under ordinary stress with the one-hour delay.

The native continuous midnight result survived removal of both the
best trade and the best coin at 1× (+49.76 USDC). The earlier Binance
reference did not (−3.15 USDT). Under cascade stress the earlier
continuous midnight run lost 55.36 USDT at 1× and 257.08 USDT at 3×.
The 08:00 UTC native variants met the numerical Track B gates in
both development windows. This is a clock sensitivity evaluated
alongside the existing midnight rule, and the 16:00 UTC results
show how easily that clock choice can change the outcome.

The breakout accounted for 348 replays. Together with the studies
below, the batch covers 436 replays. Settings and cost cases share
market history. The six native monthly and ten earlier quarterly
resets give a clearer count of distinct time periods.

## Other strategies

The weekly 28-day momentum rule earned 47.02 USDC in Discovery and
38.46 USDC in Validation on Thursdays at 500 USDC under stress.
On Mondays it lost 29.21 USDC in Discovery and made 2.50 USDC in
Validation. Removing the best trade and coin turned that Monday
Validation result into a 14.24 USDC loss. On the earlier Binance
sample, Thursday momentum lost 58.10 USDT in the 2026 period. Adding
confirmation from the seven-day trend left the result dependent on
the entry day and regime. The 56 replays cover both rules, entry
days, balances, periods, and base and stress costs. [Time-series momentum research](https://doi.org/10.1016/j.jfineco.2011.11.003)

The multiday residual-reversal test pairs an unusually weak asset
with an unusually strong one, sizing the legs to offset their
estimated exposure to the broader non-BTC market. The best 500 USDC
Validation result was +13.32 USDC after stress across nine pairs.
It lost 4.83 USDC in Discovery. Moving the entry schedule by a day
turned its Validation result into a 7.07 USDC loss. No variant
met the Track A gates. The 32 replays cover the basic and
turnover-gated rules, two entry schedules, two balances, two
periods and two cost models. [Short-horizon reversal research](https://www.nber.org/papers/w30917)

## Historical order-book checks

Two preselected Development trades had free historical samples.
The replay applies the same order-book sweep and fee calculation
as the paper service. Each hypothetical WLD entry starts with
an independent 500 USDC book and no other positions.

| WLD short on July 1, 2026 UTC | Fill price at both sizes | 1× quantity | 3× quantity |
| --- | ---: | ---: | ---: |
| 00:00 decision | 0.40664 | 46.6 | 140.0 |
| 8 minutes later | 0.40615 | 46.6 | 140.0 |
| 12 minutes later | 0.40575 | 46.6 | 140.0 |

All orders filled within the 40 bps limit on the sampled book.
The 12-minute delay cost about 22 bps on the modeled short entry
compared with the midnight fill. The study also records the
15-minute observation as an illustration beyond the live paper
deadline. The sample does not cover the WLD exit.

The August 1 VVV sample recorded a mark of 11.541 at 20:32:01 UTC,
below the protective threshold of 11.548. The next eligible
20-level snapshot arrived 2.4 seconds later. It supported a
modeled 8.12 VVV exit at 11.528, about 17 bps below the threshold
before fees. This verified local execution evidence around the
stop. It did not replay the entire position or establish historical
liquidation ordering before the covered period.
[Tardis Hyperliquid coverage](https://docs.tardis.dev/historical-data-details/hyperliquid)

The no-charge source covers six first-of-month dates inside our
native archive. These are enough for several targeted execution
checks. Full nine- or ten-day position paths need continuous book
and mark data. Hyperliquid's official bulk archive charges the
requester for transfers. This study used the free samples only.
[Hyperliquid archive documentation](https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data)

I would keep the breakout in paper at this point. The native results
justify more testing, especially at 1×. The sensitivity to clocks,
drawdown, and the older reference leaves the case for a funded run
unproven. The depth checks improve the execution model on specific
trades. A longer historical replay needs matched book and mark
coverage through both the entries and the exits.

## Reproduce

```sh
python -m copybot.pilot_backtests state/backruns/2026-11-21-broad \
  state/backruns/2026-11-21-binance-majors state/backruns/2026-11-21-broad/pilot-backtests.json
python -m copybot.momentum_study hyperliquid state/backruns/2026-11-21-broad \
  state/backruns/2026-11-21-momentum/hyperliquid.json --capital 300 500
python -m copybot.momentum_study binance state/backruns/2026-11-21-binance-majors \
  state/backruns/2026-11-21-momentum/binance.json --capital 500
python -m copybot.residual_reversal_study state/backruns/2026-11-21-broad \
  state/backruns/2026-11-21-broad/multiday-residual-reversal.json
python -m copybot.historical_book_check state/backruns/2026-11-21-tardis-depth \
  state/backruns/2026-11-21-broad state/backruns/2026-11-21-tardis-depth/book-check-copy.json
```
