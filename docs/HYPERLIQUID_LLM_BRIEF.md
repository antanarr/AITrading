# Hyperliquid autopilot brief for another LLM

Paste this brief into another model to review the strategy and propose
experiments. Read the repository and raw results if they are available.

## Role and objective

Act as a skeptical quantitative trading researcher and software reviewer.
I want a Hyperliquid-native perpetuals bot that can eventually run automatically.
The live worker is paused. Keep it paused throughout this research.
Propose and test repeatable patterns in liquid meme-coin perps and recently
listed perps. Exclude BTC as a trade and as a signal input. Aim for more
winners than losers and positive net expectancy after fees, spread, market
impact and funding. Explain how a proposed pattern would fail. Compare it
against staying flat. Preserve a chronological test period that the search
has never used.

## Current implementation

- Python project `AITrading`. `copybot/autopilot.py` runs the strategy on
  Railway with a dedicated Hyperliquid agent wallet. The owner wallet is
  connected through MetaMask. Research requires public market data only.
  Do not request keys, wallet signatures, transfers or a live order.
- Live configuration last verified at one position, 1x isolated, approximately
  10 USDC per order with an 11 USDC cap, up to six entries per UTC day and
  a 2 USDC realized daily loss limit. Treat risk settings as separate from
  signal research.
- The prior strategy uses the completed 15-minute close beyond the preceding
  three hours' range, plus a short-term trend check. Volume, breadth and
  funding were among the inputs used to rank signals. The approximate
  stop is 0.75%, the target is 1.5%, and the time cap is 90 minutes. The bot
  can also exit when a closed candle reverses through the breakout level.
- The journal tracks order intent, stop protection, fills, fees and funding.
  Keep paper simulations and operational state separate.

## Evidence through the previous study

- In the 14-day replay, the original rule made 84 trades and lost 1.8124 USDC.
  Price movement alone lost 0.5974 USDC on the same entries and exits.
  Modeled spread and impact cost 0.4338 USDC, fees cost 0.7808 USDC, and
  funding cost 0.0004 USDC.
- Forty-nine trades exited after failed breakouts. Twenty-seven failed by
  the first post-entry close. Holding the same 49 entries to their original
  stop, target or time cap lost 2.0586 USDC compared with 1.9736 USDC under
  the early exit. BTC, ETH, SOL and HYPE all lost in the 48-day baseline.
- The table uses USDC and includes modeled costs. Historical coverage
  ended November 19, 2026 UTC. The 180-day run used hourly fills, while the
  48-day run used 15-minute fills.

| Rule | 48 days | 180 days |
| --- | ---: | ---: |
| Deployed 15-minute breakout | −5.2263 | |
| Fixed exits on deployed entries | −4.9694 | |
| Volume and higher timeframe confirmation | −4.3564 | |
| 20-hour trend breakout | −2.8022 | −5.5176 |
| 3-day trend breakout | +0.8123 | −1.3065 |
| Hourly range reentry | −0.0025 | −4.5455 |
| Stay flat | 0 | 0 |

The 3-day breakout made 0.7896 USDC on 15 trades over the final 60 days.
A single BTC short contributed 0.7932 USDC. Removing it eliminates the
profit. The six-month result at higher assumed costs was −2.2263 USDC.

## Review and experiment request

1. Challenge the conclusions, data leakage controls, fill assumptions and
   trade sample sizes. Explain any reason a result could disappear live.
2. Suggest distinct hypotheses that fit this venue and universe. Examples
   include a failed high-volume squeeze, a persistent cross-coin volume
   rotation, funding that stays extreme after price stalls, and a recent
   listing that defends its opening range after a pullback. Give explicit
   entry, invalidation, exit and data requirements for each.
3. Specify how to test with historical listing eligibility, point-in-time
   liquidity and funding, closed bars, and executable next quotes or
   conservative next-bar fills. Include a higher cost scenario and
   realistic stop gaps. Verify minimum notional and the lot-size cap.
4. Evaluate win rate, net expectancy, count, realized drawdown, cost
   sensitivity, concentration by asset and period, and results without
   the highest P&L trade. Describe how to prevent repeated tuning from
   leaking into the final test period.
5. Rank any viable candidates from the available evidence and state what
   would justify a future testnet order cycle. If none beat the flat
   benchmark on adequate independent evidence, say so plainly.

See `docs/HYPERLIQUID_STRATEGY_STUDY.md` and archived commit `e4aa566`
for these older methods. `docs/HYPERLIQUID_MEME_STUDY.md` describes the
current six-archetype harness and its qualifications. Treat all available
outcomes through November 20, 2026 UTC as development data and keep any new,
prospective Reserve sealed until its designated pass. Exchange documentation
describes candle retention, maker and taker fees, and hourly funding.
[Hyperliquid market data](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)
[Fee schedule](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)
[Funding](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)
