# AITrading Starter

This repository packages a multi-model ensemble trading bot that can run in paper or live mode. It orchestrates signals from GPT, Claude, and any OpenAI-compatible endpoint (Grok, etc.), applies consensus logic, and routes resulting trades through `ccxt`.

## Features

- **Multi-model voting brain** – Prompt OpenAI, Anthropic, and Grok-compatible models. Each model must respond with JSON containing `{action, confidence, stop_pct, take_pct, reason}`.
- **Ensemble logic** – Require at least two models to agree above a configurable confidence threshold before executing trades.
- **Execution layer** – Paper trading account simulation or live trading through any `ccxt` exchange (defaults to MEXC, also works with Phemex/Binance/Coinbase, etc.).
- **Webhook server** – Optional FastAPI webhook that can receive TradingView alerts and forward them to the execution layer.
- **Risk guardrails** – Configurable risk-per-trade, daily loss limit, and kill-switch behaviour baked into the execution flow.

## Quick start

1. **Create a virtual environment and install dependencies**

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure API keys**

   ```bash
   cp .env.example .env
   nano .env
   ```

   Fill in any credentials you have:

   - `OPENAI_API_KEY`
   - `ANTHROPIC_API_KEY`
   - `GROK_BASE_URL` and `GROK_API_KEY` for an OpenAI-compatible Grok endpoint
   - Exchange credentials (`API_KEY`, `API_SECRET`) and set `USE_TESTNET=true` for sandbox trading

3. **Update `config.yaml`**

   - Add/remove symbols
   - Adjust the ensemble consensus threshold
   - Tune risk controls or switch exchanges (MEXC by default)

4. **Paper trade**

   ```bash
   python main.py --symbols BTC/USDT --timeframe 5m --paper --once
   ```

   Use `--once` to run a single cycle. Drop it to keep looping at the configured interval (default 60 seconds).

5. **Go live**

   ```bash
   python main.py --symbols BTC/USDT --timeframe 5m --live
   ```

   Ensure you have loaded real API keys and disabled testnet mode in `config.yaml` before going live.

## TradingView → Webhook

1. Launch the webhook server:

   ```bash
   uvicorn webhook:app --host 0.0.0.0 --port 8000
   ```

2. Use this alert payload in TradingView:

   ```json
   {
     "symbol": "BTC/USDT",
     "side": "{{strategy.order.action}}",
     "reason": "TV alert: {{ticker}} {{close}}",
     "confidence": 0.66,
     "stop_pct": 0.01,
     "take_pct": 0.02
   }
   ```

3. Point the alert to `http://YOUR_IP:8000/signal`. If you set `webhook_secret` in `config.yaml`, add the same value as an `X-Webhook-Secret` header in the TradingView alert.

## Configuration reference

Key tunables in `config.yaml`:

- `providers` – Toggle or update model IDs, temperature, and token limits.
- `ensemble.min_confidence` – Minimum model confidence for a vote to count.
- `ensemble.require_agreement` – Minimum number of models that must agree before a trade fires.
- `risk` – Risk per trade (fraction of account balance) and daily loss kill switch.
- `exchange` – Select a `ccxt` exchange, override parameters, and toggle sandbox mode.

## Roadmap / next steps

- Add exchange-specific futures parameters (isolated/hedge mode, leverage) for MEXC or Phemex.
- Expand strategy prompts and include additional indicators.
- Persist trade logs and performance metrics to disk or a database.

Contributions and customisations are welcome—start by tweaking `config.yaml` and the prompts in `ai_trading/strategy.py`.
