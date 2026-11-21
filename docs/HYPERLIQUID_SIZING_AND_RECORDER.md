# Hyperliquid sizing audit and passive recorder

## What changed

The replay now reports why each order plan was rejected. It also prices
the same plans at 47 and 1,000 USDC with no existing positions, keeping
the 10 USDC venue floor, lot rounding, fees and 1% risk per leg fixed.
A separate portfolio replay at each balance accounts for cash changes
and exposure reserved by open or resting orders. Research balances do
not change the trading account.

The follow-up used the same 124-day Discovery and 56-day Validation.
The previous 15 controls and nine new configurations all ran under base
and stress execution costs. The two qualification tracks retained their
thresholds. A candidate additionally needs positive stress net in
Discovery and Validation before it can be frozen. The Reserve stayed
sealed.

## What the floor blocked

The table counts plans before competition for slots and collateral.
Identical setups can appear in more than one configuration. A rejected
or accepted plan can also expire without filling.

| Stress run | Discovery at 47 | Validation at 47 | At 1,000 with no other orders |
| --- | ---: | ---: | ---: |
| Across the 15 original variants | 814 / 3,933 (20.70%) | 126 / 1,673 (7.53%) | 0 floor rejections |
| Six-hour funding | 76 / 243 (31.28%) | 9 / 45 (20.00%) | 0 floor rejections |
| Six-hour listing fade | 84 / 100 (84.00%) | 0 / 0 | 0 floor rejections |
| Twelve-hour listing fade | 61 / 77 (79.22%) | 0 / 0 | 0 floor rejections |
| Clock-based maker pair | 29 / 35 (82.86%) | 14 / 15 (93.33%) | 0 floor rejections |
| Four-hour dispersion | 7 / 29 (24.14%) | 1 / 5 (20.00%) | 0 floor rejections |
| Twelve-hour dispersion | 17 / 54 (31.48%) | 6 / 15 (40.00%) | 0 floor rejections |

The floor was a large confound for listing fades in Discovery and for
pairs in both periods. No listing rules produced an eligible Validation
plan at either balance. The clock-based maker pair rose from one to
12 completed Validation bets at 1,000 USDC and lost 24.18 USDC under
stress. In the full 1,000 USDC portfolio the floor can still reject an
order when existing reservations leave too little room.

## Results at 1,000 USDC

| Rule | Discovery, stress net | Validation, stress net | Validation trades |
| --- | ---: | ---: | ---: |
| Six-hour funding | −293.60 | +67.97 | 26 |
| Six-hour funding, aligned 72h and 168h basket trend | +40.88 | +6.35 | 3 |
| Four-hour dispersion with basket trend | −3.48 | 0.00 | 0 |
| Twelve-hour compression, trailing | −239.45 | −54.92 | 60 |
| Twelve-hour compression, 1.5R maker take-profit | −239.65 | −93.24 | 63 |
| Twelve-hour compression, 2.5R maker take-profit | −239.75 | −94.67 | 60 |
| Twenty-four-hour regime, existing maker take-profit | −69.19 | −122.55 | 104 |
| Twenty-four-hour regime, 2.5R maker take-profit | −55.33 | −138.22 | 99 |
| Clock-based IOC momentum pair | −38.41 | −13.65 | 2 |
| Clock-based IOC reversion pair | −19.31 | +2.36 | 5 |

The original six-hour funding rule was the only Track A or B
Validation pass at either balance. In the 1,000 USDC run it won
15 of 26 trades, had a 1.72 profit factor and stayed positive after
excluding its best trade and coin together. Discovery contained losses
on both sides of the book, 149.08 USDC from shorts and 144.52 USDC from
longs. The aligned basket variant turned Discovery positive but
reduced 45 Validation plans to five. It completed only three bets and
lost 5.31 USDC after removing its best trade and coin together.
No rule met the combined gate, so there is no qualifying account-size
recommendation. Running any historical Validation funding plan in
isolation would have required at least 76.86 USDC at 1% risk or 38.43
USDC at 2% to cover the most demanding plan. Concurrent orders can
raise that floor. These figures use historical entry prices and modeled
stops.

In Validation, adding the 1.5R maker target to twelve-hour compression
cut modeled slippage from 81.28 to 62.43 USDC. It also cut gross price
P&L from +39.20 to −19.74 USDC. The exit classified more trades as
winners while giving up too much of the larger moves. Both pairs
modes required every capped leg to pass its opening-price check.
In Validation the 10 bp cap rejected 13 of 15 momentum pairs and
10 of 15 reversion pairs. The four-leg, 72-hour dispersion test
produced no candidate. The candle simulation cannot guarantee
atomic execution of live IOC baskets.

## Passive data collection

The separate Railway recorder is running in Singapore on a 2,048 MB
volume. The first heartbeat after the storage upgrade confirmed a
committed context snapshot, 24 order books and 272 trades, with all
subscriptions acknowledged. The initial pilot volume was detached
and preserved. The trading worker stays paused.
[Recorder service](https://railway.com/project/7cc71f2d-5c3a-4e79-916e-85e59e106503/service/fb5f02e3-a461-4f47-966b-716839c3f7fc)

The recorder connects to the public native perpetual market feed for
market context every minute and subscribes to public trades and L2
for the eight highest-volume eligible non-BTC meme or recent-listing
perps. Books are sampled every five seconds. The database preserves
price levels, spread, dollar depth within 5/10/25 bps, side and size of
trades, exchange and receive timestamps, contract metadata changes,
subscription counts and feed gaps. Trade records omit wallet addresses.

The recorder compresses large book and market context payloads in SQLite.
Use `copybot.recorder.decode_payload` to read JSON or compressed payloads.
The Railway recorder rotates files by UTC day or size and retains up
to 30 days subject to the 1,536 MiB archive limit. Oldest files age out first.
It runs with a single writer and its own data directory. A reconnect
cannot recover every missed public event, so analysis must check the
recorded gap markers. Sampling L2 every five seconds also limits
reconstruction of faster queue changes.
[Hyperliquid public subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions)

Start the public recorder on its dedicated volume:

```sh
python -m pip install -r requirements-recorder.txt
python -m copybot.recorder --output /data --top-n 8 --book-seconds 5 \
  --max-mib 1536 --retention-days 30
```

For local verification, use a disposable directory and add
`--run-seconds 35` or `--once`. The Railway image can be built with
`Dockerfile.recorder`.

## Reproduce the research

```sh
python -m copybot.meme_study explore state/backruns/2026-11-21-meme \
  state/backruns/2026-11-20-sizing/equity-1000.json --initial-equity 1000 \
  --start 2026-05-25 --validation-start 2026-09-26 --end 2026-11-21
```

Repeat with `--initial-equity 47` and a different JSON output. Both artifacts
include price P&L, costs, fills, risk thresholds and the complete
per-variant and per-period rejection breakdown.

## Follow-up with Gemini

The clean factor test used daily and three-day clocks, no candle entry
patterns, and three legs per side at inverse-volatility weights. Incumbents
held on while ranked in the top or bottom six. Their sizes stayed in
place unless the current target weight drifted by more than 25%. Trading
costs applied only to nonzero deltas that passed the order minimum.

The 72-hour funding rank was positive in both periods. The all-market
version earned 331.25 and 221.36 USDC under stress. Removing the best
coin's contribution left −74.96 and +27.66. The meme-only version earned
80.69 and 156.32, falling to −50.58 and +5.82 after removing its
respective largest contributor. Neither momentum nor short-term
reversal had two profitable periods.

```sh
python -m copybot.factor_study state/backruns/2026-11-21-meme \
  state/backruns/2026-11-20-sizing/factors.json \
  --start 2026-05-25 --validation-start 2026-09-26 --end 2026-11-21
```

The portfolio was dollar-neutral at its target. Lot rounding, retained
positions, and small-delta filters allow the actual net exposure to drift.
Returns are stressed candle simulations. The evidence does not satisfy
the existing Reserve qualification gate.

### Funding-carry robustness

Gemini challenged whether one profitable rebalance clock and two dominant
coins explained the result. We ran 28 fixed robustness scenarios covering
three start-day offsets, two through four legs per side, equal and
inverse-volatility weights, and explicit re-ranking after removing SKR in
Discovery or PONS in Validation. Each scenario still used the 72-hour
clock, rank retention, the 25% drift rule and fees on deltas.

| Stress net, USDC | Discovery offsets 0 / 1 / 2 | Validation offsets 0 / 1 / 2 |
| --- | --- | --- |
| All markets, 3 per side, inverse-volatility | +331.25 / −347.23 / −150.20 | +221.36 / +324.94 / +263.81 |
| All markets, 3 per side, equal | +322.25 / −449.90 / −293.80 | +301.09 / +374.60 / +219.73 |
| All markets, 4 per side, equal | +66.32 / −99.03 / −282.48 | +337.28 / +383.72 / +421.49 |
| All markets, 3 per side, inverse-volatility, re-ranked without top contributor | −102.22 / −302.58 / −148.28 | +58.28 / +291.91 / +246.14 |
| Meme-only, 3 per side, inverse-volatility | +80.69 / +49.32 / +61.44 | +156.32 / +114.80 / +99.73 |
| Same meme-only runs, excluding best coin's contribution | −50.58 / −85.47 / −118.13 | +5.82 / −5.64 / −35.07 |

The first all-market run's Discovery gross price plus funding contribution
was +594.19 USDC on the long side and −190.52 on the short side, before
execution costs. Validation reversed to −283.69 long and +540.72 short.
Mean absolute imbalance after rebalancing was only 3–4% of gross exposure
in Validation, but daily return correlations with the phase-open meme
basket ranged from −0.58 to −0.45. Equal dollar exposure left substantial
directional market sensitivity. The four-leg model with a 5% neutrality
band still lost after removing its best coin in both periods.

**No funding-carry configuration met the agreed clock, concentration and
market-correlation checks.** Equal weights reduced the observed capital
required to reproduce the 3-per-side Discovery sizing path to 82.86–155.36
USDC across clocks, but the improved sizing did not restore robustness.
The results favor the passive measurement next.

Reproduce an individual robustness case with `--funding-72-only`,
`--clock-offset-days 0|1|2`, `--legs-per-side 2|3|4`, and
`--weighting equal|inverse-vol`. Use repeatable `--exclude-coin` flags
to remove markets before ranking. The local `factor-robustness.json`
includes base and stress returns, component P&L, Sharpe, drawdown, turnover,
rebalance exposures, minimum equity and benchmark correlations.

### Prospective maker measurement

The passive audit uses 16-minute anchors to keep horizons from overlapping
for each coin. Each quote rests for 60 seconds. It can only fill on a
subsequent opposite-side trade strictly through the quoted price after a
250 ms modeled activation delay. The primary metric is the executable
five-minute round-trip markout after entry and exit fees. Exit fills walk
recorded bid or ask depth for the full position. The report also includes
entry-to-mid markout, one- and 15-minute horizons, fill rates, and
breakdowns by day and market.

Three conditional screens run alongside the unconditional quote baseline.
They scan on UTC minute boundaries, use data available by the decision
time, and allow one trigger per coin and signal per 16-minute window.

| Screen | Fixed entry observation |
| --- | --- |
| Flow plus depth continuation | In the preceding 60 seconds, at least 2,500 USDC of aggressor flow with signed imbalance of at least 0.50 in magnitude. Top-five bid/ask depth imbalance must be at least 0.20 in the same direction. |
| Open-interest flush | Buy after a five-minute mark price decline of at least 1.75%, open interest down at least 3%, and mark at least 10 bps below oracle. |
| Premium fade | Fade mark relative to oracle at a distance of at least 20 bps on either side. |

The open-interest and premium rules require market context no more than
75 seconds old. The REST snapshot has a receive time. Book and trade
events also have an exchange time, and the audit checks both clocks.
Conditional candidates need at least 500 distinct eligible
anchors, greater than +3 bps mean executable five-minute net markout, a
positive lower confidence bound after resampling UTC days, and positive
results across at least four markets. Sparse signals are reported as
insufficient evidence. The unconditional baseline calibrates adverse
selection and fill rate.

The first prospective interval starts November 22 and ends at
November 29, 2026 at 00:00 UTC, exclusive. A timestamp-only preflight
checks for seven complete days. An evidence screen also requires at
least 500 distinct eligible quote anchors. This window tests the
passive-fill model and is separate from a frozen strategy's Reserve.

```sh
python -m copybot.microstructure_study /path/to/recorder-sqlite \
  --start 2026-11-22 --end 2026-11-29 --check-only
```

When coverage is ready, replace `--check-only` with
`--output /path/to/markouts.json`. This analysis leaves the recorded
files unchanged. The volume file listing was verified against the mounted
recorder. Download the closed SQLite parts for the study period before
running the audit. The part for a UTC day closes after the recorder writes
its first part for the next day.

```sh
railway volume --project 7cc71f2d-5c3a-4e79-916e-85e59e106503 \
  --environment 8dbad813-f8d8-447f-8a86-2afd541bbdc3 files \
  --volume 4b18442a-1043-4e40-a64f-49982f89dbe7 list / --json
```

Use the same project, environment, and volume arguments with
`download /market-<part>.sqlite3 /path/to/recorder-sqlite/market-<part>.sqlite3`
for each closed part covering the study interval.
