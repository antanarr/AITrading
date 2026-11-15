# Sources and execution assumptions

Checked against the providers' documentation on November 15, 2026.

| Source | Implemented access | Meaning |
| --- | --- | --- |
| Birdeye | API-key discovery from `/trader/gainers-losers`, `type=30d`, ordered by realized P&L | A candidate list, retaining unrealized P&L and trade counts |
| Nansen | API-key discovery from `/api/v1/smart-money/dex-trades` | Wallets with recent Smart Money activity; not a P&L leaderboard |
| GMGN, Defined, other sites | JSON imports with named sources and evidence | User-selected candidates; no scraping or invented API integration |
| Birdeye | Paginated `/trader/txs/seek_by_time` | The single source of trade signals, including for wallets discovered elsewhere |
| Solana RPC | `getTransaction` and `getBlock`, finalized commitment | Independent transaction-balance verification and ordering |
| Birdeye | `/defi/token_overview` | Paper marks and liquidity; not an executable swap quote |

References: [Birdeye discovery](https://data.birdeye.so/docs/data-api/wallet-networth-pnl/get-trader-gainers-losers),
[Birdeye trade history](https://data.birdeye.so/docs/data-api/transactions/get-trader-txs-seek-by-time),
[Birdeye overview](https://data.birdeye.so/docs/data-api/stats/get-defi-token-overview),
[Nansen DEX trades](https://docs.nansen.ai/api/smart-money/dex-trades),
[Solana transactions](https://solana.com/docs/rpc/http/gettransaction), and
[Solana blocks](https://solana.com/docs/rpc/http/getblock).

The bot uses API keys, not automatic payment protocols. Account subscriptions,
rate limits, and endpoint entitlements must be checked in a connected paper run.
Discovery failures are errors, never empty successful leaderboards.

## What proportional means

For entries:

`desired dollars = initial paper capital * trader allocation * leader trade dollars / leader reference equity`

The advisor multiplier, per-trade ceiling, trader allocation, shared token limit,
total portfolio limit, and available cash can reduce that amount. The amount
spent includes simulated entry fees. Reference equity is explicitly configured;
the bot does not pretend it knows a trader's assets held elsewhere.

For exits, sell the same fraction of that trader's copied position as the leader
sold of their token inventory immediately before the transaction. For example,
a 25-token sale from a 100-token balance sells 25% of the copied position. That
continues to work if an earlier entry was reduced by the advisor. Each trader
owns a separate position in the paper ledger even when several buy the same token.

The RPC verifier sums token accounts owned by the leader, using raw amounts and
decimals. A swap leg must match the transaction's net token change. Unsupported
flows are recorded and skipped. Transfer-fee tokens, intermediate swap legs,
ambiguous multi-route transactions, and flows through delegated vaults may fail
this check. An unsupported transaction also pauses new entries for review.
Wrapped SOL and the configured USDC/USDT quote mints are not copied
as independent target tokens in this first version.

## Operational limits of this version

The polling loop waits for finalized transaction data and checks block ordering.
It prioritizes a verifiable paper record over low latency. It is not a subsecond
execution engine. It overlaps history requests by two minutes to catch delayed
indexing, but that does not establish that a third-party index is complete.

On first following a wallet, monitoring starts at the current time. It does not
buy that wallet's existing holdings. Late or old signals are skipped. An outage
pauses new entries and preserves cursors for retry. Before resuming, review the
paper positions for missed exits and provider history gaps. This version does
not reconcile an entire missed trading interval against current leader holdings.

Risk caps apply to marked open exposure against initial paper capital. Profits
can cause exposure to rise above a cap; the cap then blocks additional entries.
The daily loss check uses account equity and a persisted UTC-day baseline taken
at the first entry decision of that day. A breach latches until the next UTC day.
It blocks entries and permits exits. This is not a guarantee on maximum losses.

Stop exits run on observed prices, with a one-hour reentry cooldown per trader
and token. They cannot execute during missing or stale price data. Real execution
would additionally need sellability checks and a route with acceptable price impact.

Simulated fills include configurable fees and adverse slippage. They do not model
every chain fee, MEV effect, depth constraint, failed transaction, or account rent
charge. The demo is fictional and is not evidence of a profitable strategy.

## AI scope

The OpenAI adapter uses Structured Outputs, validates a multiplier from zero to
one, and caches each wallet assessment for a limited time. It evaluates the
copied account's recent realized outcomes and position records. No news feed,
independent alpha model, automatic trader promotion, or learning system is claimed.

`advisor: rules` is explicitly labeled rules-only. `advisor: openai` requires
credentials and a selected model. Refusals, invalid responses, and timeouts reject
new entries. They do not silently fall back to permission or block copied exits.
