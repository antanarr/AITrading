# OpenAI veto and ranking replay

This tests whether a model improves the fixed 1× daily breakout.
Both sides of the comparison start at 500 USDC and use the same
market selection, 20-day breakout, prior 50-day average, position
sizing, 2 ATR stops, opposing 10-day channel exit, and 10-day maximum
hold. The current strategy ranks by breakout distance relative to ATR.
An LLM condition can skip or reorder candidates.

## Inputs and evaluation

The May 25 to November 20, 2026 Hyperliquid Development tape contains
92 decision times and 275 candidates. The anonymous packets include
1h, 4h, 24h, 7d, and 28d returns, ATR and trend distances, relative
turnover, breadth, and signed funding over the preceding 1h, 6h, and
24h. The full-input condition also includes lagged equity, VIX, Treasury
yield, and oil movements, official Fed speech and CFTC release counts,
Fed speech tone, and the latest policy-rate decision. The news archive
contains numerical summaries from documented official sources. It has
no original headline or article text. The main test uses the official
news archive and leaves the retrospectively indexed media feed out.

GPT-6.2 Sol scored each date independently through an authenticated
Codex session. Packets omit ticker, calendar date, realized outcomes,
account details, and absolute contract prices. Scoring sessions start
in an empty directory with shell and web access disabled. The historical
simulator validates every score and uses the immutable mapping from
anonymous packet IDs to candidate orders. The anonymization reduces
direct date and ticker cues. A model could still recognize an unusual
combination of historical observations.

The tested interventions are the returned veto alone, ranking alone,
both together, and a narrower veto that requires both a veto flag and
a score at or below 35. The narrower rule was added after inspecting
Discovery and before inspecting its Validation results. The same
procedure is repeated with the chart and funding inputs alone.

The replay charges venue fees, archived funding and base or stressed
spread and slippage. Candle-based execution and lagged macro data
carry the same coverage and data revision limits as the previous
research. Model usage is recorded separately. The report includes the
amount of extra return, if any, available for inference cost. The two
random controls keep either the number of candidates accepted at each
decision or the total number accepted per direction. They test how
much apparent improvement can come from changing activity.
[Directional input provenance](HYPERLIQUID_DIRECTIONAL_STUDY.md)

## Results

All figures are net of stressed trading costs in USDC. The settings in
each row are the same across the two periods.

| Inputs and use of the model | Discovery | Validation | Validation fills |
| --- | ---: | ---: | ---: |
| Fixed 1× baseline | +55.63 | +24.72 | 19 |
| Charts, funding, news and macro. Veto | +1.74 | +25.68 | 16 |
| Charts, funding, news and macro. Rank | +39.46 | +21.85 | 20 |
| Charts, funding, news and macro. Veto and rank | −3.51 | +27.75 | 17 |
| Charts, funding, news and macro. Narrow veto | +34.85 | +31.55 | 18 |
| Charts and funding. Veto | +6.71 | +31.38 | 16 |
| Charts and funding. Rank | +32.92 | +19.62 | 20 |
| Charts and funding. Veto and rank | −8.76 | +38.24 | 17 |
| Charts and funding. Narrow veto | +37.46 | +31.55 | 18 |

The full-input rank policy met the numerical Track B gates in both
development windows. It also earned less than the baseline in both.
The narrower veto improved late profit and reduced late drawdown
from 19.41 to 16.32 USDC, but its earlier drawdown rose from 33.71
to 46.69 USDC while profit fell. The broad full-input veto finished
below 23 of 24 random alternatives in Discovery that accepted the
same total number of long and short candidates. Its most frequent
reason for vetoing a setup was overextension, accounting for 99 of
118 total vetoes. In Discovery the 19 baseline fills absent in the
veto replay had contributed 67.66 USDC. The 19 new fills made 13.32.

The two input variants used 184 completed model calls and 550
candidate assessments. The case for putting this model in the
decision path remains unsupported. Its highest late result came
with an earlier loss, and its early shortfall generally exceeds its
late improvement even before inference costs.

## Reproduce

```sh
python -m copybot.llm_trade_study state/backruns/2026-11-21-broad \
  state/backruns/2026-11-20-directional/context.jsonl \
  state/backruns/2026-11-21-broad/llm-trade-comparison.json \
  --cache state/backruns/2026-11-21-broad/llm-scores --mode both
```

Add `--cached-only` to recalculate policies from the stored model outputs.
The OpenAI API backend also accepts `--provider api --model MODEL` with
an existing key in the environment or AITrading's own Keychain entry.
Live stays paused and the Reserve stays sealed.
