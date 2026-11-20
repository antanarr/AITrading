# Hyperliquid autopilot

The worker scans Hyperliquid every five minutes. It checks the 12 highest-volume
markets above $10 million in 24-hour volume across the native and builder
venues. A signal requires a completed 15-minute candle to break the range of
the preceding 12 candles in the direction of the trend. The strategy can go
long, go short or stay flat. Its profitability is unverified.

When several breakouts qualify, the worker checks the highest-ranked setup
first. It favors unusual volume on the breakout candle and ranks an entry
lower if that side pays relatively high funding. For native crypto perps it
also considers how many liquid crypto contracts are moving in the same
direction and gives a small weight to extreme values on the daily [Bitcoin
Fear & Greed Index from Alternative.me](https://alternative.me/crypto/fear-and-greed-index/).
Stock and other builder perps use their own volume and funding. An unavailable
or stale sentiment reading contributes no points. Scan logs and live entry
records include the score, reasons, and source timestamp where available.

Live entries also require a market on the configured list, a recent order book,
a spread no wider than 8 basis points, and offered size sufficient for the
order. Adverse hourly funding above 2 basis points disqualifies the entry.
The initial policy uses 1x isolated margin, one bot position at a time and up
to six entry attempts per UTC day. A 0.75% stop is submitted to Hyperliquid
after the entry fill. The worker checks for a 1.5% take-profit move or a 90-minute
timeout and exits using reduce-only orders. These prices trigger actions.
Actual fills, price gaps and fees determine the outcome.

Hyperliquid requires at least $10 of entry notional. The worker selects the
smallest quantity that meets the minimum and applies an $11 sizing cap to
account for lot sizes and the entry price limit. It skips an entry when the
smallest valid order exceeds that cap. Short orders are sized against the
current book and mark because a short can fill above its limit. A better fill
price can raise the actual short notional above its estimated budget.

## Commands

```sh
python -m pip install -r requirements-autopilot.txt
python -m copybot.autopilot scan
python -m copybot.autopilot check
python -m copybot.autopilot --config examples/autopilot.yaml run --live --interval 300
python -m copybot.autopilot status
python -m copybot.autopilot pause
python -m copybot.autopilot resume
```

`scan` reads public data. `check` verifies the account's API wallet approval
without signing an order. On the Mac, it uses the existing `codex-local` Keychain
entry and the MetaMask account address bound by the Hyperliquid shortcut.
`run --live` can place real orders. Keep a copy of the live configuration
outside the repository. `pause` blocks new entries after any in-flight order.
An open position continues to be managed while paused.

The live SQLite journal records order intent before submission and reconciles
using client order IDs. If the exchange cannot confirm an order, the worker
reports it in `status` and waits. Keep the journal when restarting. Check the
client order ID and position in Hyperliquid before manual intervention. Avoid
manual orders on the worker's configured markets while it is running.

## Railway

The container defaults to public market scans and needs no API key. Run one
replica in Singapore with sleep disabled. For live operation, mount a persistent
volume and point `--db` and `XDG_STATE_HOME` at the volume. Set the public wallet
address in the live configuration and pass a worker-only file containing a
separate named API wallet key via `--agent-key-file`. Authorize that agent in
Hyperliquid using the same MetaMask account. Start only one live worker for the
account. Keep the existing Mac agent in Keychain.

With those files in place, the live worker start command is:

```sh
XDG_STATE_HOME=/data python -m copybot.autopilot --db /data/autopilot.sqlite \
  --config /data/live.yaml --agent-key-file /data/agent.key run --live --interval 300
```

ETH held in MetaMask needs to be deposited or converted into collateral for
the chosen markets. A wallet owner must sign the funding transaction. The
autopilot only sends trading actions on Hyperliquid.
