# Hyperliquid autopilot

**Live entries remain paused.** The source worker continues to reconcile any
existing trade and blocks fresh entries until a strategy qualifies. See the
[meme-perp research harness](HYPERLIQUID_MEME_STUDY.md) for the offline work.

Public scans use Hyperliquid perpetual metadata and closed candles. The
native universe has no fixed ticker-count cap and excludes BTC. The scanner
can also evaluate explicitly configured builder contracts. It reports the
legacy breakout for diagnostics. Research cases run through their own dynamic
eligibility and replay.

The legacy IOC trial required a market on the configured list, a recent order book,
a spread no wider than 8 basis points, and offered size sufficient for the
order. Adverse hourly funding above 2 basis points disqualifies the entry.
The trial uses 1x isolated margin, one bot position at a time and up to six entry
attempts per UTC day. The entry and a reduce-only stop are submitted in one
exchange request. The stop uses the entry limit to set protection about 0.75%
away. If Hyperliquid cancels the attached stop on a partial entry, the worker
checks the filled quantity and attempts to install a stop for that quantity.
A missing or rejected replacement can trigger a reduce-only exit. The worker
also exits on a 1.5% favorable move, after 90 minutes, or when a closed post-entry
candle reverses through the breakout level. Gaps and slippage can move the actual
exit beyond the trigger.

Hyperliquid requires at least $10 of entry notional. The worker selects the
smallest quantity that meets the minimum and applies an $11 sizing cap to
account for lot sizes and the entry price limit. It skips an entry when the
smallest valid order exceeds that cap. Short orders are sized against the
current book and mark because a short can fill above its limit. A fast move can
still lift the actual short notional above the sizing cap. When fills verify
this happened, the worker attempts to close the position and records an incident.

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

The live SQLite journal records order intent and signing deadlines before
submission. If an entry acknowledgment is missing, the worker checks the client
order ID in order status and history, and checks fills and current exposure before
clearing an expired entry as flat. Exposure whose order can't be confirmed needs
operator review. Exit retries wait for the previous request to settle, and the
worker checks flatness twice before removing a stop. Keep the journal when
restarting, and check the order ID and position in Hyperliquid before intervening.
The worker matches opening fills to the exchange order ID and checks the
remaining exposure against the account position. A manual increase or re-entry
on a configured market pauses fresh entries and raises an incident. Keep manual
positions separate from this worker's markets while live.

After closure, the journal records USDC P&L using actual entry and exit fills,
exchange fees, and account funding. It blocks new entries after a **2 USDC**
realized net loss in one UTC day at the trial setting. An open position can lose
more during a gap. The worker logs risk incidents to Railway. The Codex alert
monitor checks for new incidents and missing worker heartbeats every 15 minutes
while its local execution host is available.

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

For venue verification, pass `--network testnet` before the command and use a
testnet-approved wallet and a separate journal. Journals are bound to their
network. Testnet faucet credit and API approval are separate from the funded account.

ETH held in MetaMask needs to be deposited or converted into collateral for
the chosen markets. A wallet owner must sign the funding transaction. The
autopilot only sends trading actions on Hyperliquid.
