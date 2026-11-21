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

The separate Railway recorder is running in Singapore with its own
512 MB volume. The first heartbeat confirmed a committed context
snapshot, 24 order books and 272 trades, with all subscriptions
acknowledged. The trading worker stays paused.
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
The recorder rotates files by UTC day or size and retains up to
30 days subject to the 384 MiB archive limit. Oldest files age out first.
It runs with a single writer and its own data directory. A reconnect
cannot recover every missed public event, so analysis must check the
recorded gap markers. Sampling L2 every five seconds also limits
reconstruction of faster queue changes.
[Hyperliquid public subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions)

Start the public recorder on its dedicated volume:

```sh
python -m pip install -r requirements-recorder.txt
python -m copybot.recorder --output /data --top-n 8 --book-seconds 5 \
  --max-mib 384 --retention-days 30
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
