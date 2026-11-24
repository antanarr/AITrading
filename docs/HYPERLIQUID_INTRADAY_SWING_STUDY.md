# Hyperliquid intraday swing study

On Monday, November 23, 2026, several liquid perps rallied in the
morning and gave back most of the move before the 6 p.m. Central
paper decision. These measurements use the price at midnight Central
as the starting point and traded Hyperliquid candles thereafter.
[Hyperliquid candle data](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#candle-snapshot)

| Perp | Peak versus midnight | Move by the 6 p.m. check |
| --- | ---: | ---: |
| SOL | +9.15% | +0.20% |
| HYPE | +10.81% | +1.26% |
| LIT | +16.35% | +1.68% |
| ZEC | +14.06% | −1.95% |

VVV traded as high as 26.926 against its prior 20-day high of 26.678.
The 15-minute bars did not close above that level. At the paper
decision it closed at 24.533, which left it inside the range. The
full paper scan completed and found no qualifying 20-day close.
The old 15-minute chart rule identified SOL, HYPE, LIT and ZEC long
signals at 8 a.m. Central. That was a check of the entry signal
only. It does not establish which orders could have filled or
what the resulting trades would have earned.

## Faster rules

The replay compares two approaches to the coverage gap. The hourly
continuation strategy uses the top 20 liquid markets from the
prior day, at least eight peers with observations, and a broad
impulse. At least two-thirds of peers must move in the chosen
direction and their median move must reach 0.75 prior ATR. The
traded asset must clear its six-hour range with a move of at least
one ATR. The three presets are a capped taker entry with a target,
a maker entry on a pullback with a target, and a capped taker entry
with a longer trailing exit.

The reversal strategy looks for a closed one-hour candle that
extends beyond a prior 12- or 24-hour range and closes back inside.
It requires at least twice recent median hourly dollar volume and
rejects candles that travel through both boundaries. It enters
with a capped taker order, an ATR-scaled stop, a maker target,
and a six-hour timeout.

The backtests use the earlier Hyperliquid Development archive. Both
start with 500 USDC and include fees, modeled slippage, and actual
archived funding rates. The November 23 tape explains the forward
coverage gap. Its prices and trade outcomes were outside the
backtest periods. The Reserve stays sealed.

## Results after stress costs

| Rule | Discovery net | Validation net | Validation trades |
| --- | ---: | ---: | ---: |
| Hourly continuation, 6h taker with target | −256.13 | −59.94 | 227 |
| Hourly continuation, 6h maker on pullback | −261.12 | −92.17 | 163 |
| Hourly continuation, 12h taker with trailing exit | −270.64 | −51.97 | 214 |
| Reversal of a failed 12h range break | −260.86 | −149.79 | 457 |
| Reversal of a failed 24h range break | −264.94 | −86.15 | 417 |

The trailing continuation had the best Validation result. It earned
51.76 USDC in gross price P&L, then paid 20.66 in fees, 82.55 in
modeled slippage, and 0.52 in funding. It made 0.84 USDC with base
costs and lost 270.64 USDC in Discovery under stress. The best
Validation reversal also had gross gains overwhelmed by trading
costs. All five variants failed their qualification gates.

## Reproduce

```sh
python -m copybot.intraday_move_study state/backruns/2026-11-21-broad \
  state/backruns/2026-11-21-broad/hourly-broad-impulse.json
python -m copybot.intraday_reversal_study state/backruns/2026-11-21-broad \
  state/backruns/2026-11-21-broad/intraday-reversal.json
```
