# Hyperliquid research redesign

Mainnet entries are paused. The research commands read market archives and
write JSON. They have no wallet, signing path, or SQLite journal access.

## Validation result

**No candidate qualified in Validation.** Zero rules were frozen. The new
Reserve remains unopened and the live execution redesign remains behind
the qualification gate.

The six-hour funding rule came closest. It had **12 wins in 19 trades**
(63.2%), a 1.82 stress profit factor and **+2.6060 USDC** under stress.
It remained positive after removing its best trade and coin, but needed
25 trades. It lost 14.5380 USDC in Discovery, so the recent gain did not
hold across both market periods. Funding itself contributed only 0.1101
USDC in Validation. Most of the gain came from the direction of price.

Lowering the turnover filter to 500,000 USDC gave it 25 trades, but only
13 wins (52.0%), a 1.13 profit factor and −0.6003 USDC without the best
coin. The higher-resolution run produced +2.5688 USDC with 19 trades.

Under stress, the equal-weight meme basket returned +4.2599 USDC in
Discovery and -25.4855 USDC in Validation. The flat benchmark returned
zero in both periods.

| Validation rule | Stress trades | Stress wins | Maker fills/posted under stress | Base net | Stress price P&L | Stress funding | Stress fees | Stress slippage | Stress net |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `funding-6h` | 19 | 12 | 19/26 | +3.1157 | +3.3210 | +0.1101 | +0.1340 | +0.6912 | +2.6060 |
| `funding-extreme` | 5 | 1 | 5/8 | -0.7568 | -0.6403 | +0.0342 | +0.0448 | +0.1928 | -0.8437 |
| `listing-6h` | 0 | 0 | 0/0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| `listing-12h` | 0 | 0 | 0/0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| `dispersion-4h` | 2 | 0 | 2/4 | -0.9158 | -0.8477 | +0.0122 | +0.0140 | +0.0652 | -0.9147 |
| `dispersion-12h` | 5 | 1 | 5/6 | -0.9946 | -0.8524 | +0.0165 | +0.0422 | +0.1766 | -1.0548 |
| `pair-clock` | 1 | 0 | 1/2 | -0.9312 | -0.4208 | -0.0006 | +0.0085 | +0.0395 | -0.4694 |
| `pair-dispersion` | 1 | 0 | 1/2 | -0.4702 | -0.4338 | -0.0002 | +0.0064 | +0.0295 | -0.4699 |
| `funding-squeeze-long` | 2 | 1 | 2/3 | +0.0085 | +0.0301 | +0.0100 | +0.0147 | +0.0690 | -0.0436 |
| `listing-washout-long` | 0 | 0 | 0/0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| `compression-12h` | 49 | 12 | 49/89 | -0.1137 | +2.1829 | -0.0283 | +0.5443 | +3.6824 | -2.0722 |
| `compression-24h` | 41 | 10 | 41/68 | +0.7847 | +0.3460 | -0.0221 | +0.4847 | +2.3678 | -2.5285 |
| `compression-12h-taker` | 81 | 15 | 0/0 | -4.5134 | -3.1463 | -0.0232 | +1.3550 | +5.4092 | -9.9337 |
| `regime-24h` | 77 | 25 | 77/137 | -3.1047 | -2.8868 | -0.0679 | +0.5730 | +2.3610 | -5.8887 |
| `regime-72h` | 52 | 14 | 52/96 | -6.0928 | -4.2561 | -0.0156 | +0.4103 | +1.6919 | -6.3739 |
| `funding-6h:hold_hours=24,stop_atr=1.6` | 16 | 11 | 16/23 | +2.3101 | +2.3387 | +0.1024 | +0.0903 | +0.3716 | +1.9792 |
| `funding-6h:basket_breadth=0.55` | 3 | 2 | 3/3 | +0.1822 | +0.5982 | +0.0379 | +0.0190 | +0.0428 | +0.5743 |
| `compression-12h:hold_hours=36,stop_atr=1.8,trailing_atr=2.25` | 49 | 14 | 49/89 | -2.5853 | -0.3837 | -0.0201 | +0.4837 | +2.7850 | -3.6725 |
| `regime-24h:hold_hours=36,stop_atr=1.7` | 77 | 31 | 77/130 | +1.6787 | +0.8330 | -0.0681 | +0.5317 | +1.9888 | -1.7556 |

- **Listing and overextension.** The listing variants produced no Validation
  signals. The two overextension variants lost 0.9147 and 1.0548 USDC under
  stress, on two and five trades respectively. The study had too few
  qualifying new-listing events to evaluate the washout rule.
- **Paired dispersion.** Each variant posted both legs only once in the stress
  replay. One leg filled each time, leaving directional exposure. The losses
  include the unmatched positions, and there is no evidence of a repeatable
  market-neutral result.
- **Compression.** The twelve-hour maker rule made 2.1829 USDC in raw price
  P&L in Validation. Fees plus slippage cost 4.2267 USDC, mostly on stop exits.
  The price-capped IOC control lost 9.9337 USDC under stress. A longer trail
  and wider stop also lost money.
- **Leader pullback.** A wider stop and longer hold turned raw price P&L
  positive, but total friction still outweighed it. The stress result was
  −1.7556 USDC and its drawdown was 4.3759 USDC.

## Strategy rules

| Archetype | Entry and exit hypotheses |
| --- | --- |
| Funding exhaustion and squeeze | Short an extended, rejected rally when positive funding has persisted. Long a higher-low reclaim following deeply negative funding and a flush. The separate squeeze variant trails a favorable move. |
| New-listing distribution and washout | From 12 hours to 14 days after first observed trading, fade a failed rally below VWAP or recent structure with positive funding. The long variant needs a drawdown, negative funding, and a structure reclaim. |
| Cross-sectional meme overextension | Compare four- or twelve-hour returns with the contemporaneous meme median, scale by ATR as a fraction of price, then require a rejection or volume climax and aligned funding. Take profit toward twelve-hour VWAP. |
| Market-neutral dispersion | Rank eligible memes at a rebalance or dispersion trigger. Size the leader and laggard legs toward equal dollars. Let each maker leg fill independently and charge the cost of closing unmatched exposure. |
| Compression and expansion | Require a prior range in the low tail of the preceding seven-day range distribution and a breakout on at least twice normal volume. Rest at the breakout or a retracement and use an ATR trailing exit. Compare with a price-capped, full-friction IOC control in research. |
| Regime-gated pullback | Use meme-basket breadth and realized volatility to select direction. Let leaders or laggards pull back to VWAP or a recent swing level. |

The study evaluated a small menu of versions for each archetype. Exit retests
widened the funding and momentum stops, extended holding windows, and tried
a funding breadth gate. The lower-turnover and finer-candle runs were
separate sensitivity checks. Research on pump-and-dump episodes motivated
the rejection hypothesis. Past work on crypto momentum with realistic costs
motivated a direct continuation-versus-reversal comparison.
[Cryptocurrency Pump-and-Dump Schemes](https://doi.org/10.1017/S0022109025000201)
[Momentum in the Cryptocurrency Market](https://academic.oup.com/raps/advance-article/doi/10.1093/rapstu/raag011/8914819)

## Archive and chronology

- Discovery ran from May 25 through September 25, 2026 UTC. Validation ran
  from September 26 through November 20. There was a 14-day warm-up before
  Discovery for markets that already traded. Each partition starts flat.
- The native census held 234 non-BTC symbols. Of those, 196 had daily
  candles and 38 had contiguous hourly prices and complete hourly funding
  over their available spans. The replay had 28 positively identified meme
  markets and five first-observed listings during the study. Four historical
  series remained available for markets marked delisted in the census.
- Meme identity uses a separate exchange-to-provider identity map. Unknown
  labels are excluded from the meme basket. A dated unknown market may
  still enter the recent-listing cohort. Listing age uses the oldest daily
  candle corroborated by the hourly series. It refers to the perp listing on
  this venue, rather than the token launch. Today's taxonomy and the loss of
  archives for some contracts create survivorship and classification bias.
- Eligibility recalculates using previous closed candles. The main test
  required 1 million USDC trailing turnover, at least nine active hours,
  minimum median traded-hour turnover and a traded latest closed bar.
  Dollar turnover is estimated from candle prices and volume. It cannot
  establish historical spread or executable quantity.
- Fifteen-minute data was available for fourteen markets. The sensitivity
  uses it where complete and otherwise falls back to hourly bars. Rollups
  disagreed for seventeen early XTEN hours, all with carry-forward
  zero-volume quarters, so those groups use hourly execution.
- The earlier meme study had already opened a historical test through
  November 20. It is development evidence in this redesign. A new Reserve
  must be later than the freeze date and live in a separate archive. The
  CLI checks the frozen candidate configuration, code and taxonomy, then
  claims a one-time result receipt before running the frozen rules.

The public scanner captures mark, oracle, open interest, premium, impact
prices, funding, and turnover. It can fetch native 15-minute, one-hour,
four-hour and daily candles and hourly funding. It does not reconstruct
historical open interest, trade flow, quotes or queue position.
[Native Hyperliquid metadata and funding API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)
[Hyperliquid candles and books](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)

## Execution and scoring

- Maker entries rest only on the passive side of the modeled opening
  quote. For a long, a later bar must trade strictly below the limit. For
  a short, it must trade strictly above it. A touch or a zero-volume
  carry-forward candle cannot fill. Orders that expire incur zero cost.
  A take-profit limit also needs trade-through. When order is ambiguous,
  an adverse stop takes priority. Adverse stop gaps execute at the next
  observed opening price with modeled taker friction.
- Maker fees use 1.5 basis points with zero spread cost. Taker fees use
  4.5 basis points. Base adds three basis points of half-spread and
  two/four of entry/exit impact. Stress adds four of half-spread and
  four/twenty-four of entry/exit impact. Funding uses the recorded
  hourly settlement and prior candle close as an oracle-price proxy.
  These costs are proxies. Real queue position, size and extreme gaps
  can materially affect results.
- The hypothetical portfolio starts at 47 USDC, risks up to 1% at each
  modeled stop, and holds at most four positions. Notional is bounded
  by 50% of equity per leg, 100% gross and 60% in either direction. The
  directional cap reserves both sides separately because paired orders may
  fill independently. Order size rounds down to the exchange lot size and
  skips when the risk-limited order cannot meet the 10 USDC minimum.
- Track A requires at least 25 Validation trades, greater than 52% winning,
  stress profit factor above 1.20, and positive stress net even after
  removing the strongest trade and strongest coin. Track B requires at least
  20 trades, stress profit factor above 1.30, the same outlier checks and
  controlled intrabar drawdown. This test defines controlled as no more
  than twice positive stress net profit. The code checks each outlier
  individually and both together. Paired returns count by bet.
- Benchmarks are staying flat and a theoretical, fractional equal-dollar
  portfolio of eligible meme perps at the start of each period. The
  benchmark includes funding and taker friction. At 47 USDC, equal
  allocations can fall below the exchange minimum. Intrabar drawdown uses
  an adverse candle-price envelope for positions, while realized drawdown
  uses settled trade P&L.
  [Taker and maker fees](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)
  [Hourly funding](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)

## Next data to record

- Timestamped L2 and trade flow for each qualifying instrument around setup,
  submission, expiry and exit. Retain book age, spread, depth at the planned
  price and across several ticks, trade price and size, aggressor side,
  sequencing, and time spent through the modeled maker price. This tests
  the simulated fills, queue assumptions, and orphan cost before any paper
  or testnet trial.
- Periodic native mark, oracle, premium, funding, open interest, impact prices,
  and turnover, plus listing, delisting and metadata events. Reliable earlier
  timestamps and contract identities would let the listing cohort include
  contracts that later failed. OI and trade-flow changes would distinguish
  liquidations and actual continuation from candle shapes.

## Reproduce Discovery and Validation

```sh
python -m copybot.meme_study explore state/backruns/2026-11-21-meme \
  state/backruns/2026-11-21-redesign/study.json \
  --start 2026-05-25 --validation-start 2026-09-26 --end 2026-11-21 \
  --retest funding-6h:hold_hours=24,stop_atr=1.6 \
  --retest funding-6h:basket_breadth=0.55 \
  --retest compression-12h:hold_hours=36,stop_atr=1.8,trailing_atr=2.25 \
  --retest regime-24h:hold_hours=36,stop_atr=1.7
```

The study JSON contains signal counts, order attempts, fills, maker fill
rates, hours in market, gross price P&L, funding, fees, slippage, profit
factor, win rate, payoff ratio, both drawdowns, worst days and weeks,
outlier checks and individual legs. The funding sensitivity files are in
the same research directory.
