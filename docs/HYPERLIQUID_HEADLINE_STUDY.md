# Can the bot trade a headline before the move is over?

NVIDIA published Jensen Huang’s resignation at 7:48 a.m. Central on November 12, 2026. A five-minute Hyperliquid replay entered a short two minutes later, both with a fast headline alert and with charts alone. The short moved in the right direction before reversing into its stop. It lost 2.58 after stressed costs. A follow-up with a resting take-profit banked 2.36 on that same short. The existing daily breakout has no immediate entry route for company headlines. [NVIDIA’s timestamped announcement](https://www.globenewswire.com/news-release/2026/11/12/3401635/0/en/nvidia-announces-ceo-transition.html)

Across the six releases, adding an alert reduced the loss from 8.69 to 5.75 after stressed costs. With the resting profit target, the result improved from a loss of 1.92 to a loss of 0.76. There was no profitable alert policy across the sample under stressed costs. The as-built news context changed none of the replay entries. It covered the Fed decision at a one-hour delay and carried no minute-level CPI, jobs, or NVIDIA release. [Existing news inputs](HYPERLIQUID_LLM_TRADE_STUDY.md)

## Six release windows

These are the stress results from the 15-minute replay with the protective stop and two-hour timeout. The fast-headline column permits the chart entry as well as the alert entry. It takes the first signal.

| Release | Perp | Chart-only entry | Entry with fast headline | Chart net | Fast-headline net |
| --- | --- | ---: | ---: | ---: | ---: |
| [September CPI, Oct. 14](https://www.bls.gov/news.release/archives/cpi_10142026.htm) | ETH | Short, +45 min | Short, +15 min | −1.51 | +2.58 |
| [Fed decision, Oct. 28](https://www.federalreserve.gov/newsevents/pressreleases/monetary20261028a.htm) | ETH | Long, +45 min | Long, +45 min | −0.85 | −0.85 |
| [October jobs, Nov. 6](https://www.bls.gov/news.release/archives/empsit_11062026.htm) | ETH | Flat | Short, +15 min | 0.00 | −1.16 |
| [October CPI, Nov. 10](https://www.bls.gov/news.release/archives/cpi_11102026.htm) | ETH | Short, +15 min | Short, +15 min | −1.46 | −1.46 |
| [NVIDIA CEO transition, Nov. 12](https://www.globenewswire.com/news-release/2026/11/12/3401635/0/en/nvidia-announces-ceo-transition.html) | xyz:NVDA | Short, +12 min | Short, +12 min | −2.62 | −2.62 |
| [NVIDIA earnings, Nov. 17](https://www.globenewswire.com/news-release/2026/11/17/3405200/0/en/nvidia-announces-financial-results-for-third-quarter-fiscal-202.html) | xyz:NVDA | Long, +10 min | Long, +10 min | −2.25 | −2.25 |

The five-minute check used the same six-hour chart window and two-hour maximum hold, with a volatility adjustment for the shorter bars. Earnings triggered a long five minutes after the release and lost 2.23 with either exit. The resignation short was profitable with the target because the price traded through 154.22 before the reversal. An alert arriving five minutes after the resignation delayed the alert-only entry to seven minutes after the release. The chart-only entry still fired at two minutes. The November 23 reinstatement fell after the November 21 UTC Development cutoff and was excluded. [NVIDIA reinstatement release](https://www.globenewswire.com/news-release/2026/11/23/3408381/0/en/nvidia-board-reinstates-jensen-huang-as-president-ceo-and-board-director.html)

| 15-minute release replay | Stress fills / wins | Gross price P&L | Funding | Fees | Spread and slippage | Base net | Stress net |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Chart or chart plus existing feed | 5 / 0 | −5.65 | +0.003 | 0.79 | 2.25 | −7.34 | −8.69 |
| Chart plus fast headlines | 6 / 1 | −2.16 | +0.004 | 0.90 | 2.69 | −3.05 | −5.75 |
| Chart with resting target | 5 / 2 | +0.30 | +0.003 | 0.67 | 1.55 | −1.13 | −1.92 |
| Chart plus fast headlines with resting target | 6 / 3 | +1.64 | +0.004 | 0.75 | 1.65 | +1.71 | −0.76 |

On 12 earlier same-clock weekday control windows, giving the looser rule a synthetic alert reduced stressed profit from 6.83 to 3.30. With the maker target, it increased the loss from 0.85 to 1.88. The controls can contain unrelated market news. They probe how the same entry rule behaves at the same clock time without a verified alert from the release list. Staying flat returns zero throughout.

## What the replay measures

- The chart benchmark watches for a six-hour range breakout with a closed candle body at least one ATR. An alert permits entry after a 0.75 ATR directional candle and a 0.75 ATR move from the last pre-release close. Direction comes from price. The replay measures what publication timing adds to the chart rule. It has no historical feed receipt log or model interpreting the story. For scheduled releases, a calendar can supply the early timing used here.
- The data uses actual exchange candles and hourly funding. Each case starts independently at 500 USD-equivalent collateral, caps 1× notional at 125, and targets at most 5 of initial risk before costs and gaps. Entry uses a 40 bp capped IOC with the next opening trade as a price proxy. Risk is based on two ATR, and a gap past the stop exits at the worse opening price. Event spreads and market impact are modeled. This replay cannot confirm order-book fills. For publication inside a candle, the first eligible signal candle can include some pre-release trades.
- The stress case charges 8 bp of spread and impact at entry and 28 bp at market exit, on top of fees. It uses the venue’s tier-zero native fees of 4.5 bp taker and 1.5 bp maker per side. The NVDA example assumes the standard 2× HIP-3 multiplier, without historical builder discount or account-tier records. The resting 2.5 ATR profit target was a follow-up after seeing the reversals. It uses the existing intraday research target, excludes the entry candle, requires a strict trade-through, and gives the stop priority if both prices trade in one candle. The recorded exit time for an intrabar event is the end of its candle. [Exchange candles and funding](https://hyperliquid.gitbook.io/Hyperliquid-docs/for-developers/api/info-endpoint) · [Hyperliquid fees](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees) · [HIP-3 fee reference](https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-3-builder-deployed-perpetuals)

These are targeted case studies on two perps. Results depend on the entry and exit rules, and the timing tests cannot measure the value of headline interpretation. No strategy changes or live orders followed from this run. The Reserve stayed sealed.

## Same collateral at 1×, 2×, 3×, and 5×

The leverage rerun keeps the 500 starting balance, the same isolated collateral per trade, and the same entry, stop, and take-profit rules. The NVIDIA resignation trade used 124.95 of collateral at every leverage. The six-release total uses the 15-minute chart plus fast headlines with a maker target. The two NVIDIA columns use the finer five-minute replay. Figures include stressed fees, funding, spread, and slippage.

| Leverage | Resignation trade size | Resignation net | Earnings net | All six releases net |
| --- | ---: | ---: | ---: | ---: |
| 1× | $124.95 | +$2.36 | −$2.23 | −$0.76 |
| 2× | $249.91 | +$4.72 | −$4.45 | −$1.51 |
| 3× | $374.86 | +$7.08 | −$6.68 | −$2.27 |
| 5× | $624.77 | +$11.80 | −$11.13 | −$3.78 |

The 5× result across all six releases was +$8.55 under base costs, −$3.78 under stressed costs, and −$60.04 in a separate gap sensitivity with an extra 3% adverse fill on stop exits and a 1% adverse adjustment to the liquidation mark proxy. With the original two-hour timeout and protective stop, the 5× six-release result was −$28.76 under stressed costs. The resignation short alone lost $12.89 with that exit.

The 5× resignation trade had a stop at $159.89 and an estimated initial liquidation price of $179.86. No modeled liquidation occurred in the six headline windows or 12 comparison windows at these four leverages. The model uses the exchange’s current first-tier maintenance limits for ETH and xyz:NVDA as proxies for the historical rules. It uses traded candles as mark-price proxies, applies funding to isolated margin, and gives liquidation priority when both liquidation and a stop are crossed in one candle. It assumes the full isolated collateral is lost in that case. The fee assumptions match the original replay. [Hyperliquid liquidation rules](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations)

## Reproduce from the local replay tapes

```sh
study=state/backruns/2026-11-26-headlines
context=state/backruns/2026-11-20-directional/context.jsonl
python -m copybot.headline_study "$context" "$study/tape.json" "$study/results.json"
python -m copybot.headline_study "$context" "$study/tape.json" "$study/target-results.json" --maker-target
python -m copybot.headline_study "$context" "$study/nvidia-tape-5m.json" "$study/nvidia-results-5m.json" --nvidia-5m
python -m copybot.headline_study "$context" "$study/nvidia-tape-5m.json" "$study/nvidia-target-results-5m.json" --nvidia-5m --maker-target
```

The captured tapes are stored in the ignored local research directory. Add `--capture` with an unused snapshot path to obtain a fresh copy while the exchange's rolling candle history still covers these dates.

The leverage runs also use the public margin snapshot saved at `state/backruns/2026-11-26-headlines/leverage/margin.json`.

```sh
for lev in 1 2 3 5; do
  python -m copybot.headline_study "$context" "$study/tape.json" "$study/leverage/target-${lev}x.json" \
    --maker-target --leverage "$lev" --margin-snapshot "$study/leverage/margin.json"
  python -m copybot.headline_study "$context" "$study/nvidia-tape-5m.json" "$study/leverage/nvidia-target-${lev}x.json" \
    --nvidia-5m --maker-target --leverage "$lev" --margin-snapshot "$study/leverage/margin.json"
done
```
