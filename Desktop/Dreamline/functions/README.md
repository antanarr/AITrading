# Dreamline Firebase Functions

Firebase Functions backend for Dreamline Phase 3 features.

## Setup

1. Install dependencies:
```bash
cd functions
npm install
```

2. Set environment variables:
```bash
firebase functions:config:set openai.api_key="YOUR_OPENAI_API_KEY"
firebase functions:config:set openai.base="https://api.openai.com"  # Optional, defaults to OpenAI
```

Or use Firebase Functions environment variables (recommended):
```bash
firebase functions:secrets:set OPENAI_API_KEY
```

3. Build:
```bash
npm run build
```

4. Deploy:
```bash
firebase deploy --only functions
```

## Endpoints

- `scopeGate` - Classifies if messages are in-scope for Dreamline
- `oracleExtract` - Extracts symbols, tone, and archetypes from dreams
- `oracleInterpret` - Interprets dreams with history and transits
- `oracleChat` - Pro chat interface with scope gating
- `astroTransitsRange` - Calculates transit ranges (day/week/month/year)
- `horoscopeCompose` - Composes horoscope text from transits
- `incrementUsage` - Atomic usage counter

## Notes

- The OpenAI Responses API endpoint (`/v1/responses`) is used. If this is not available, you may need to adjust the endpoint to use the standard Chat Completions API.
- Update `FunctionsBaseURL` in Info.plist after deployment to match your Firebase project.

