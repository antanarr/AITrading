# Review of the original AITrading project

Reviewed November 15, 2026, starting from `c8e57c0` on GitHub.

The original project is an AI signal starter for centralized exchanges. It asks
several models to vote on buying or selling, then submits an order through CCXT.
It does not discover wallets, follow their transactions, or divide positions
between copied traders. The checkout was behind GitHub; its current history
also contains an unrelated Dreamline snapshot, which this work leaves alone.

## Useful parts

The separation between configuration, market data, model calls, risk controls,
and execution is useful. So are environment-based credentials, HTTP clients with
timeouts, and structured model responses. The replacement follows that broad
structure with fewer dependencies and validated settings.

The old multi-model voting rule has no evidence of an advantage. Agreement
between language models is not a substitute for testing trading decisions.
The new advisor makes a bounded adjustment that can be compared with plain
copying using the same recorded observations.

## Defects that rule out reusing the execution engine

- Paper entries do not reserve cash. Multiple entries can reuse the same money.
- A second entry replaces a position under the same symbol.
- The risk manager compares dollar amounts with a fractional daily-loss setting.
- Stops and take-profit settings are stored without an execution loop that enforces them.
- Paper state disappears on restart.
- The webhook treats any nonempty live environment setting, including `false`, as true.
- Its authentication function is called directly, so FastAPI never injects the supplied header.
  With no configured secret, the endpoint accepts unauthenticated trade requests.
- Trade execution can return no order while the webhook still responds with success.

The replacement entry point uses the paper copy bot. Both legacy engine factory
and execution constructor now reject use. The old webhook returns HTTP 410 and
cannot trade. Original modules remain for reference and are not imported by the
new application. Their historical dependencies are not needed for the new bot.

## First implementation

Completed: candidate discovery adapters, source attribution and deduplication,
proportional entries, trader-specific partial exits, persistent paper accounting,
bounded model assessments, risk exits, replay, monitoring loop, and regression tests.

The first implementation covers Solana spot tokens. It does not sign transactions,
hold private wallet keys, or submit real orders. Provider access and model calls
still need verification with account credentials. No credentials were present in
this project or its process environment during implementation.
