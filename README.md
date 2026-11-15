# AITrading

A personal wallet-copying bot with multiple discovery sources, proportional
position sizing, and optional AI adjustments. This version trades simulated funds.
It cannot submit real orders or access a wallet's private keys.

Birdeye and Nansen discovery adapters find candidate wallets. Wallets found on
GMGN, Defined, or elsewhere can be imported with their source information.
The same wallet found twice becomes one candidate. Discovery does not enable it
for copying: choose the wallets, capital allocations, and reference account sizes
in the configuration first.

## Try the working demo

Requires Python 3.11 or newer.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-copy.txt
.venv/bin/python -m copybot demo
```

The offline demo uses fictional wallets and no credentials. It copies two traders
buying the same token, applies a synthetic size reduction, mirrors a half sale and
a full exit, and ignores a duplicate event. Its output shows the cash balance,
remaining positions, fees, and recorded decisions. The synthetic assessment is
labeled; no AI call or real market performance is implied.

## Discover wallets

Copy `.env.example` to `.env` and add your data-provider keys locally. Do not
commit that file. Birdeye and Nansen keys are separate from exchange credentials.

```sh
.venv/bin/python -m copybot discover --source birdeye --output state/birdeye-candidates.json
.venv/bin/python -m copybot discover --source nansen --output state/nansen-candidates.json
.venv/bin/python -m copybot discover --import examples/candidates.json
```

Sources and imports can be combined in one command. Output files are optional.
`examples/candidates.json` documents
the import format using fictional addresses. GMGN and Defined currently use this
import path; this version does not scrape their sites.

Birdeye discovery returns a 30-day ranking by realized P&L and retains unrealized
P&L and trade count for review. Nansen discovery returns recently active Smart
Money wallets. These are different kinds of evidence, and both need further
evaluation before allocating capital.

## Configure the paper portfolio

Edit `copybot.yaml`. It starts with an empty roster and $1,000 of simulated cash.
For example, this assigns one wallet 30% of the paper portfolio:

```yaml
traders:
  - wallet: REPLACE_WITH_SOLANA_WALLET_ADDRESS
    sources: [birdeye, gmgn]
    allocation: '0.30'
    reference_equity_usd: '100000'
    enabled: true
```

With those settings, a $10,000 entry by that trader requests a $30 copied entry
before the advisor and risk limits. Reference equity is the source account value
you choose for scaling. It is not automatically inferred from a leaderboard.
Enabled allocations cannot exceed 100% in total.

Default limits include $50 per entry, 15% shared exposure to one token, 70% total
exposure, minimum liquidity, price drift, signal freshness, and a daily equity
loss check. Paper fills include fees and adverse slippage. The defaults are
starting settings for evaluation, not investment recommendations.

Exits mirror the fraction sold from the trader's pre-transaction token balance,
verified using finalized Solana transaction data. Each trader has a separate
position record. Disabling a trader or pausing entries still permits their exits.
Keep disabled traders in the roster until their copied positions are closed.

## Monitor real activity with simulated funds

Set `BIRDEYE_API_KEY` and an HTTPS `SOLANA_RPC_URL` whose provider supports
finalized transaction and block history. Then run:

```sh
.venv/bin/python -m copybot watch
```

Add `--once` for one polling cycle. The first cycle establishes when following
starts; it does not buy historical holdings. Subsequent cycles poll wallet trades,
verify token changes and transaction order against Solana, and simulate eligible
copies. Supported targets are Solana spot tokens; perpetual futures and multiple
chains are not implemented.

This polling version prioritizes an auditable record. It is not yet a low-latency
live execution bot. Data outages pause new entries; inspect missed exits and data
gaps before resuming. Stops also require fresh price data.

```sh
.venv/bin/python -m copybot status
.venv/bin/python -m copybot pause
.venv/bin/python -m copybot resume
```

SQLite stores cash, positions, decisions, pause state, and polling cursors in
`state/paper.sqlite`. Restarting does not reset money or repeat processed events.
Use a separate `--db` path for a separate paper account. Global options go before
the command, for example `python -m copybot --db state/trial.sqlite watch`.

## Enable AI assessments

The default `advisor: rules` uses a stated rule to reduce entries after repeated
copied losses. It is explicitly reported as rules-only.

To use the model adapter, set `advisor: openai`, add `OPENAI_API_KEY`, and choose
a model supporting Structured Outputs in `COPYBOT_OPENAI_MODEL`. It can reduce
or suspend entries using the paper account's recent outcomes. It cannot increase
the approved size, override limits, or prevent exits. Invalid or unavailable
assessments reject new entries. Assessments are cached briefly rather than
requesting a new model response for every transaction.

News analysis, independent trade ideas, automatic trader selection, and profitable
performance are not established by this implementation.

## Replay and verification

```sh
.venv/bin/python -m copybot --config examples/demo.yaml --db state/replay.sqlite replay examples/demo.jsonl
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Replay files contain one JSON observation per line: `observed_at`, a normalized
`trade`, a current `quote`, and an optional recorded `assessment`. Observation
times must be chronological. Reusing the same database is idempotent; use a new
database for a separate experiment. Historical observations must contain only
information available at their observation time.

See [data assumptions and current limits](docs/DATA_SOURCES.md) and the
[old-project review](docs/OLD_PROJECT_REVIEW.md). The old exchange engine is
disabled, the old webhook returns HTTP 410, and `main.py` now starts this CLI.
The unrelated historical Dreamline directory is not part of this application.
