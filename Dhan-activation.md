To activate Dhan as your live data source

When ready (no rush — works in current paper config):

1. Open a Dhan trading account at https://dhan.co/ (free, same-day approval)

2. Generate persistent access token at https://web.dhan.co/profile → "Access DhanHQ API" → "Generate Token" (no daily login unlike Kite Connect)

3. Add to .env:

DHAN_CLIENT_ID=1100xxxxxx
DHAN_ACCESS_TOKEN=eyJ0eXAi...
DATA_SOURCE=dhan

4. pip install dhanhq if needed (it's already in requirements.txt)

5. Run python scripts/premarket_preflight.py — should still PASS

6.Start bots normally — they pick up Dhan as primary, yfinance stays as fallback safety net

To revert at any time: unset DATA_SOURCE and restart.

