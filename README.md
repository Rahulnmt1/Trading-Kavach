# Stock Market Bot

An automated intraday trading bot for the Indian stock market (NSE) with built-in market research, multi-strategy signal generation, hard risk limits, and a live dashboard.

## ⚠️ Critical disclaimers — read before using

1. **No bot guarantees profit.** Roughly **80–90% of retail algo traders lose money**. This project gives you well-built infrastructure; **profitability is not guaranteed by any strategy in this codebase**.
2. **Paper-trade for at least 2 weeks** before flipping `LIVE_TRADING=true`. The risk parameters are deliberately conservative.
3. **SEBI compliance** — Algo orders must be tagged & approved by your broker. Daily TOTP login is mandatory under SEBI's 2025 algo framework. This bot does not bypass any regulatory control.
4. **You are responsible** for every order placed in your name. Read the code, understand the strategies, and never deploy capital you can't afford to lose entirely.

## Architecture

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                          EXTERNAL  SOURCES                              │
│    NSE / yfinance  ·  News RSS  ·  OpenAI (GPT-4o)  ·  Broker API       │
└──────┬──────────────────┬─────────────────┬──────────────────┬──────────┘
       │                  │                 │                  │
       ▼                  ▼                 ▼                  ▼
┌─────────────────────────────┐      ┌────────────────────────────────────┐
│   data.py · indicators.py   │      │           research.py              │
│   OHLCV  ·  ticks  ·  TA    │      │  gap · news · LLM picks (top 5)    │
└──────────────┬──────────────┘      └────────────────┬───────────────────┘
               │                                      │
               └────────────────┬─────────────────────┘
                                ▼
        ┌──────────────────────────────────────────────────┐
        │                  executor.py                     │ ◀── scheduler.py
        │   ┌──────────────────────────────────────────┐   │    APScheduler
        │   │           STRATEGY  ENSEMBLE             │   │    09:30 – 14:45
        │   │                                          │   │    IST · 1-min
        │   │   ORB  │  VWAP-revert  │  EMA+ST         │   │
        │   │            └── vote (≥2 of 3) ──┐        │   │
        │   └─────────────────────────────────┼────────┘   │
        │                                     ▼            │
        │                             ┌──────────────┐     │
        │                             │   risk.py    │     │
        │                             │ caps · sizing│     │
        │                             │ kill switch  │     │
        │                             └──────┬───────┘     │
        └────────────────────────────────────┼─────────────┘
                                             │ approved order
                                             ▼
        ┌──────────────────────────────────────────────────┐
        │   broker/   paper  ·  zerodha  ·  dhan           │ ──▶ NSE
        └─────────────────────────┬────────────────────────┘
                                  ▼
        ┌──────────────────────────────────────────────────┐
        │   Redis  (cache.py)                              │
        │   ticks · positions · signals · picks · log      │ ◀──▶ app.py
        │                                                  │     Streamlit UI
        └──────────────────────────────────────────────────┘
```

**Reading the diagram, top to bottom:**

1. **External sources** feed the bot — price data from NSE (via yfinance), news from RSS feeds, an LLM for ranking, and the broker API for live ticks/orders.
2. **Ingestion** — `data.py` produces clean OHLCV bars and runs technical indicators; `research.py` builds a pre-market shortlist using gaps, headlines, and an optional LLM scorer.
3. **Decision** — `scheduler.py` ticks `executor.py` every minute during the trading window. The ensemble runs three strategies in parallel; only setups where **≥2 of 3 agree** are forwarded to `risk.py`, which sizes the position and enforces all hard limits.
4. **Execution** — approved orders flow through a uniform broker interface (`paper`, `zerodha`, or `dhan`).
5. **State + Monitoring** — every step writes to Redis via `cache.py`; the Streamlit dashboard (`app.py`) reads the same store for a live view.

## What it does

- **Pre-market (08:30 IST)** — `research.py` scans your watchlist: gap %, EMA posture, RSI, fresh news headlines, optionally uses GPT-4o to rank top 5 picks with a written rationale. Cached in Redis.
- **Trading window (09:30–14:45 IST)** — every 1 minute the executor pulls 5-min bars, runs the 3-strategy ensemble, and any signal where ≥2 strategies agree is sent to the risk manager.
- **Risk manager** — sizes the position so max loss is ≤1% of capital, blocks new entries if daily loss ≥2%, caps trades-per-day, max open positions, and respects a `KILL_SWITCH` file you can `touch` to halt trading instantly.
- **Square-off (15:15 IST)** — force-closes everything (intraday MIS would auto-square at 15:20 anyway, but this is a safety belt).
- **Dashboard** — Streamlit with live P&L, open positions, today's research picks, signals stream, candlestick charts with overlaid indicators.
- **Backtester** — `python -m cli backtest --days 7` runs the same ensemble on historical bars and reports win rate, net P&L, and per-symbol breakdown — with realistic Zerodha-style fee modeling.

## Quick start

### 1. Install

> **Python version**: 3.11 – 3.14 are supported. Python **3.12 or 3.13 is
> recommended** for the smoothest install — the data-science stack
> (numpy / pandas / pyarrow) has the broadest wheel coverage there.
> Python 3.14 also works (deps are pinned accordingly), but if a transitive
> dep ever falls back to building from source (typical error:
> `Unknown compiler(s): [['gfortran']...]`), recreate the venv on 3.12 or
> 3.13 instead of installing a Fortran toolchain.

```bash
# deactivate 2>/dev/null
# rm -rf .venv
python3.14 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

#### Install Redis (recommended)

Redis is the bot's shared cache. It's how the running bot, the Streamlit
dashboard (`python -m cli dashboard`), and the read-only snapshot
(`python -m cli status`) see the **same** research picks, auto-watchlist, and
open positions. Without Redis the bot still works, but each Python process
gets its own private in-memory cache and the dashboard / status command will
appear empty even while the bot is trading.

On macOS:

```bash
brew install redis              # install
brew services start redis       # start now + auto-start on every login
brew services info redis        # verify (look for: status: started)
redis-cli ping                  # should print: PONG
```

Once Redis is up, every command picks it up automatically — no `.env` change
needed (the default `REDIS_URL=redis://localhost:6379/0` already points at it).
Confirm with `python -m cli status`: the header should read
`cache=redis` instead of `cache=in-memory`.

To stop Redis later: `brew services stop redis`.

```bash
To use Every Morning at 7:30 AM
# That's it — one command.
#python scripts/premarket_preflight.py
python scripts/premarket_preflight.py --auto-cleanup --clean-redis

# If exit code is 0 (or 2 with warnings you accept), launch:
bash scripts/run_bot.sh run --paper
bash scripts/run_bot.sh run --paper --segment fno
python -m cli dashboard

# Detailed system Audit Reports
python scripts/system_audit.py             # human report (color, wrapped, sectioned)
python scripts/system_audit.py --json      # machine-readable JSON to stdout
python scripts/system_audit.py --quiet     # only the final summary
python scripts/system_audit.py --no-log    # skip writing logs/audit/<ts>.{log,json}

#Exit codes

#0 → ✅ GO — all sections OK, system clean
#2 → ⚠️ CONCERNS — only WARNs (e.g. expected pre-market warmup)
#1 → 🛑 NO-GO — at least one critical FAIL

#12 sections (one per investigation pointer)
#	Section	What it covers

#1 - Live processes - equity bot, F&O bot, dashboard, caffeinate (PIDs + uptime)
#2 - Pre-market preflight - parses today's logs/preflight/*.json verdict
#3 - Capital & risk caps - per-segment table; verifies daily_profit_target aligns with ₹3-5K objective
#4 - Critical fix verifications - live regression test of yfinance_proxy for synthetic symbols; _end_of_day uses intraday_bars not latest_quote; _manage_open_positions has 1m→5m→broker-mark fallback
#5 - Corrupted-artefacts quarantine - confirms .corrupted-by-2026-04-30-spot-leak-bug files #still aside; flags any re-emerged 04-30 journal
#6 - Redis hygiene - paper:state:{equity,fno}, holiday cache freshness, stale orders hash count
#7 - Trading-day status - today + tomorrow + next 14 days from live NSE calendar
#8 - Scheduler jobs - parses today's bot_*.log to confirm cron jobs registered per segment
#9 - Strategy readiness - live smoke test — equity ensemble, F&O ensemble, cross-leak guard, credit_spread EMA state per underlying
#10 - Healthcheck dry-run - runs the same 14-check battery as the 09:00/11:00/13:00/15:00 #IST cron, listing every non-OK reason
#11 - Dashboard reachability - HTTP probe of localhost:8501
#12 - Cleanup advisories - stale orders, Streamlit use_container_width deprecation healthcheck-segment-awareness

#Artifacts written
#logs/audit/YYYY-MM-DD_HHMMSS.log — plain-text full report (ANSI stripped)
#logs/audit/YYYY-MM-DD_HHMMSS.json — structured payload with every section, finding, and extras dict (for dashboards / CI)

```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — for paper trading you only need REDIS_URL (optional).
```

Edit `config.yaml` to set your capital, watchlist, and risk limits.

### 3. Run paper trading (no broker key needed)

```bash
# In one terminal — start the EQUITY bot
python -m cli run --paper                     # implicit --segment equity

# In another — open the dashboard (sidebar lets you switch segments)
python -m cli dashboard
# Or directly: streamlit run app.py
```

To **also run the F&O bot** alongside equity (Phase 4.5 default trades
**defined-risk vertical credit spreads** on NIFTY / BANKNIFTY — sell ATM
PE / buy lower PE on bullish, sell ATM CE / buy higher CE on bearish,
profit on theta decay), start it in a third terminal:

```bash
python -m cli run --paper --segment fno       # different lock, different cache
```

`fno.enabled: true` is the default in `config.yaml`; flip to `false` to
make the F&O bot refuse to start. To trade something other than credit
spreads, edit `config.yaml` → `fno.strategies.enabled` to one of:

* `[credit_spread]` (Phase 4.5 default, ~₹2-5K margin/lot, directional)
* `[iron_condor]` (Phase 4.5 alternative, ~₹2-5K margin/lot, **neutral** — fires on EMA convergence in sideways markets)
* `[option_buy_directional]` (Phase 3, ~₹11K premium/lot)
* `[futures_trend]` (Phase 2, needs `fno.capital.total ≥ ₹150,000`)

The two bots are fully isolated — see [F&O segment](#fno-segment) below.

> **macOS users — wrap the bot with `caffeinate` to prevent sleep gaps**
> macOS will idle-sleep the laptop after a few minutes (your `pmset` defaults
> are typically `sleep 1`), which can silence the scheduler for hours and miss
> the 15:15 IST square-off. Use the bundled launcher instead of bare `python`:
>
> ```bash
> bash scripts/run_bot.sh                 # paper mode (default)
> bash scripts/run_bot.sh run             # live mode (assumes LIVE_TRADING=true in .env)
> ```
>
> It wraps the bot with `caffeinate -i -m -s -w <bot_pid>` so the Mac stays
> awake **only while the bot is running** and is allowed to sleep again the
> moment you Ctrl+C. See [Keeping the bot alive on macOS](#keeping-the-bot-alive-on-macos)
> below for the full layered defense (wake-up schedule, launchd auto-start,
> scheduler misfire grace).

### 4. Backtest before going live

```bash
python -m cli backtest --symbol RELIANCE --days 30
python -m cli walk-forward --days 60          # Sharpe / Sortino / MDD / Calmar
```

### 5. Pre-market research and watchlist refresh

```bash
python -m cli update-watchlist     # auto-pick top symbols by trend + momentum
python -m cli research             # LLM ranks pre-market picks
```

### 6. Daily Zerodha login (live mode only)

```bash
python -m cli login                # automated TOTP, refreshes KITE_ACCESS_TOKEN
```

Run this once each morning before `python -m cli run --live`. It uses
`KITE_USER_ID`, `KITE_PASSWORD`, and `KITE_TOTP_SECRET` from `.env`.

### 7. View today's trades and detailed P&L

Every fill, round-trip trade, and the end-of-day P&L statement are recorded
under `logs/`:

```bash
python -m cli journal                          # today's P&L statement + trade-by-trade table
python -m cli journal --date 2026-04-25        # any past day
python -m cli journal --tail                   # follow today's live trade log (Ctrl+C to stop)
python -m cli journal --write-eod              # (re)generate the EOD report file
```

The Streamlit dashboard (`python -m cli dashboard`) shows the same data live
under "Today's trades & P&L".

### 8. Exit logic — SL, TP, trailing stop, and the daily profit lock-in

Every minute (during the trading window) the executor inspects each open
position against the latest 1-minute bar — *before* it looks for new
entries. Exits happen in three layers:

**1. Hard stop-loss** — long: `low ≤ stop_loss` ⇒ close at `stop_loss`.
Short: `high ≥ stop_loss` ⇒ close at `stop_loss`. Worst-case fill price
is assumed (matches what a broker-side stop would actually fill at).

**2. Hard take-profit** — long: `high ≥ take_profit` ⇒ close at
`take_profit`. Short: `low ≤ take_profit` ⇒ close at `take_profit`.
SL takes precedence if both are hit on the same bar.

**3. Trailing stop** (when `risk.trailing_stop: true`) —

* Once unrealized P&L reaches `risk.trail_activation_r × R` (1R by default,
  where R = entry-time risk-per-share), trailing activates.
* The stop is then ratcheted to whichever is tightest of:
  - **ATR floor**: `peak − risk.trail_atr_mult × ATR(14, 1m)` — gives
    the trade room to breathe through normal noise.
  - **Lock-in floor**: `entry + (1 − risk.trail_lock_r) × R` — guarantees
    we lock in at least `trail_lock_r × R` of profit (0.5R by default,
    so worst case the trade is +0.5R, not breakeven).
* The stop only ever moves in our favor (long: up; short: down). Never
  loosened.
* Live updates appear on the dashboard's "Open positions" panel and are
  cached as `trail:{SYMBOL}` in Redis so a process kill mid-position
  doesn't lose the trail level (the static SL on the position is the
  durable layer).

**The daily profit lock-in** (when `risk.daily_profit_target_pct > 0`) —
once today's realized + unrealized P&L reaches the configured target
(default 5% — i.e. ₹2,500 on a ₹50k account, ₹10,000 on a ₹2L account),
the executor:
1. Force-closes every open position at the next tick (logged as
   `exit_reason: profit_lockin` in the journal).
2. Halts new entries for the rest of the day. The risk manager's
   `evaluate()` will reject signals with
   `daily profit target +X% ≥ +Y% — locked in for the day`.
3. Publishes a `profit_lockin` cache entry the dashboard surfaces as a
   green banner.

This is the single most important guard against the market giving back
your morning gains during the afternoon's chop.

#### Tuning toward your daily P&L objective

Two knobs you'll want to tune (in `config.yaml → risk:`):

| Symptom | Knob to adjust |
| --- | --- |
| Trades exit too quickly, leaving money on the table | Lower `trail_lock_r` (e.g. 0.3) **or** raise `trail_atr_mult` (e.g. 1.5) — gives the trade more room |
| Profitable trades give back gains before TP | Raise `trail_activation_r` to start trailing earlier (e.g. 0.7) |
| Daily profit target hit too rarely | Raise `tp_atr_mult` to e.g. 2.5 (wider TPs) **or** lower `daily_profit_target_pct` |
| Day too often ends in red | Tighten `sl_atr_mult` (e.g. 0.8) or raise `min_agree` to 3 (only act on unanimous signals) |

#### A note on going live

For paper trading, the in-process position manager *is* the SL/TP. For
**live trading on Zerodha**, the position-manager market-order exits
still work, but you should *also* place broker-side bracket orders
(`variety=bo` with `stoploss=` and `squareoff=`) so SL/TP are enforced
even if your laptop sleeps or the bot crashes. The current Kite adapter
only places `MARKET` orders — adding bracket orders is the priority
upgrade before going live with real money.

### 9. Trade economics — net P&L after every charge & tax

Every order on the NSE incurs **6 distinct line-items** beyond just the
brokerage: STT, exchange transaction charges, SEBI charges, stamp duty,
GST, and brokerage. On a ₹50k account placing 8-share trades, the
round-trip toll is **~₹10–15** — which on a 0.3% ATR move is **~30–40%
of the gross profit**. Without seeing this number, paper P&L is a lie.

The dashboard's **Open positions** panel now shows for each live trade:

| Column | Meaning |
| --- | --- |
| Entry / Now / Breakeven | Actual fill, current mark, the exit price you'd need just to net zero |
| SL → Net | Take-home P&L if the stop-loss hits (already minus all fees) |
| Now → Net | Take-home P&L if you closed at the current mark |
| TP → Net | Take-home P&L if the take-profit hits |
| RT fees | Round-trip total fees in rupees |

Below the table, every position has an expander that itemises every leg's
brokerage / STT / exchange / SEBI / stamp / GST individually under the
three scenarios (SL, current, TP) — exactly what a Zerodha contract note
shows.

The same view is on the CLI:

```bash
python -m cli status        # the "Open positions" section now includes net @ SL/Now/TP and a per-position fee table
```

Fees are computed by a single source of truth — `bot/fees.py` —
which the paper broker, dashboard, CLI status, and journal all read from,
so what the dashboard shows is exactly what the broker will debit.

**Reality check it surfaces**: today's NESTLEIND trade has SL → Net ≈
**−₹47** but TP → Net ≈ **+₹25**. The configured 1×ATR / 1.5×ATR ratio
**after fees** is closer to 0.5 R/R, not 1.5 R/R. Either widen the TP
multiplier (e.g. `tp_atr_mult: 2.5` in `config.yaml`) or trade larger size
to dilute the per-trade fee drag.

#### Daily fee/tax audit at 09:00 IST

Indian fee schedules change. STT was raised in 2024, NSE transaction
charges were re-rated, GST is government-set, stamp duty varies by year.
A trading bot whose fee constants are *stale* lies about its P&L.

To prevent that, **every weekday at 09:00 IST** (just before market open)
the scheduler invokes `bot/fee_audit.py::run_fee_audit()`. It:

1. **Fetches charges/pricing pages from THREE independent SEBI-regulated
   brokers**, all of whom are bound by the same SEBI / NSE / BSE /
   Government-of-India rates:
   - [`zerodha.com/charges`](https://zerodha.com/charges/) — primary,
     covers equity intraday + F&O futures + F&O options
   - [`upstox.com/pricing`](https://upstox.com/pricing/) — secondary,
     covers same 3 segments, with date-aware row selection ("From 1st
     April 2026" rows are picked when the effective date ≤ today)
   - [`dhan.co/pricing`](https://dhan.co/pricing/) — tertiary, covers
     equity intraday only (Dhan's F&O tab is JS-loaded)
2. **Parses each source independently** into `{(segment, rate_key): value}`.
3. **Cross-verifies every line-item** across all reachable sources and
   against the configured constants in `bot/fees.py`. Audits all three
   rate tables: `_RATES` (equity intraday), `_FUTURES_RATES`, and
   `_OPTIONS_RATES` — 19 line-items in total.
4. Publishes the result to `fee_audit:latest` and appends one JSONL row
   to `logs/fee_audit/YYYY-MM-DD.jsonl`.
5. Surfaces the status inside the regular health-check — so the
   dashboard's **System health → Fee schedule audit** expander shows
   the per-segment table with per-source values (`z=…  u=…  d=…`) and
   drifted rows highlighted on top.

> Why not NSE/BSE direct? Both serve JavaScript-rendered SPAs that don't
> include the rates in the static HTML — scraping them requires a headless
> browser (heavy infra, fragile). The next-best option is multiple
> independent broker mirrors, since each broker is contractually bound to
> the SAME SEBI/NSE/BSE-published rates. Cross-source agreement gives us
> the same confidence as scraping the regulators directly.

Per-rate verdicts:

| Verdict | Meaning |
| --- | --- |
| **OK** | Every reachable source agrees with the configured value (within 0.5% relative tolerance) |
| **DRIFT_CONFIRMED** | ≥2 sources independently agree on a different value — high confidence the regulator changed the rate |
| **DRIFT_SINGLE** | Only one source observed a different value (the others didn't cover this rate) — verify manually |
| **AMBIGUOUS** | Sources disagree with each other — investigate which is correct (e.g. one is stale) |
| **UNVERIFIED** | No reachable source could observe this rate today (network failure or all parsers couldn't locate it) |

Overall statuses:

| Status | Meaning | What you should do |
| --- | --- | --- |
| **OK** | Every rate in every segment is `OK` | Nothing — trade with confidence |
| **WARN** | Any drift / ambiguous / unverified rate, OR any source unreachable | Run `python scripts/eod_apply_fee_updates.py --dry-run --force-time` to preview the diff, then run it for real after 15:30 IST to patch + restart |

> Drift is **never** silently auto-applied. A rate change is a financial
> event — it requires a human to confirm and commit. The audit's job is
> to be loud about it on the dashboard, not to patch it. The dedicated
> EOD script (`scripts/eod_apply_fee_updates.py`) is the safe tool for
> applying updates: it refuses to run during the trading window, refuses
> if any position is still open, prints a colour-coded diff, takes a
> timestamped backup of `bot/fees.py`, and only then patches + restarts
> both bots.

Run on demand:

```bash
python -m cli verify-fees                  # pretty table
python -m cli verify-fees --json           # machine-readable
```

The CLI exits **0** on OK, **1** on WARN — slots into a cron / CI /
pre-trade gate cleanly.

### 9b. NSE/BSE trading-holiday calendar — single source of truth

The dashboard shows **today's and tomorrow's market status for both
Equity and F&O** as four prominent cards near the top, plus a
collapsible 14-day forward strip and a loud red banner whenever
tomorrow is a non-trading day. Holiday data is pulled directly from
NSE — the authoritative source for both NSE and BSE since the two
exchanges co-ordinate their calendars (you'll never see a one-open /
one-closed day).

* **Endpoint**: `https://www.nseindia.com/api/holiday-master?type=trading`
  — gated by NSE's WAF, so the fetcher seeds session cookies via the
  homepage first, then hits the JSON API with a desktop User-Agent.
* **Per-segment lists**: NSE returns `CM` (equity) and `FO` (futures &
  options) separately. The bot stores both so the dashboard can show
  segment-specific status — important when a single-segment holiday
  ever appears (rare, but it has happened).
* **Caching**: `bot/holidays.py` writes the parsed result to Redis
  under `nse:holidays:v2` with a 24h TTL. The dashboard, CLI, and
  healthcheck all read the same key.
* **Auto-refresh**: scheduler runs a daily refresh at **08:00 IST**
  (single global job, runs on weekends too) so the calendar is always
  fresh by the time you open the dashboard before market open. Aligned
  with the standard 08:00–08:30 pre-market warm-up window so all
  premarket fetches happen in the same band.
* **Offline fallback**: if NSE is unreachable AND Redis is empty, the
  module falls back to a hand-maintained 2026 bootstrap list. The
  dashboard shows a yellow `BOOTSTRAP` pill so you know it's
  potentially stale.

CLI:

```bash
python -m cli holidays                   # next 14 days, both segments
python -m cli holidays --refresh         # force a live NSE pull (also writes cache)
python -m cli holidays --segment fno     # filter to F&O only
python -m cli holidays --days 30 --json  # machine-readable
```

The dashboard panel also has a **"Refresh from NSE"** button for the
case where NSE just published a new circular and you want it picked
up immediately without restarting the bot.

### 10. Periodic health check (auto every 2 h, on the dashboard)

While the bot is running, the scheduler invokes a programmatic health
check **every 2 hours, Mon–Fri at 09:00, 11:00, 13:00 and 15:00 IST**.
Each run audits 12 things: bot process & uptime, macOS sleep prevention,
Redis, active config, pre-market caches (research + auto-watchlist),
signal-stream freshness, market-data freshness, today's bot-log errors,
stale signal-cache cleanup, portfolio & risk budget, **fee schedule
freshness** (see §9 above), and disk space.

The **System health** panel itself sits at the **bottom** of the
dashboard — the active trading data (positions, picks, today's trades,
charts) is what you scan first; system-level diagnostics are one
scroll away.

Each report is:
1. Logged to `logs/bot_YYYY-MM-DD.log` as one summary line.
2. Persisted to `logs/healthcheck/YYYY-MM-DD.jsonl` (full structured detail).
3. **Published to the cache** under `healthcheck:latest` (and a rolling
   `healthcheck:history` of the last 10 runs). The Streamlit dashboard's
   **System health** panel reads from there — overall verdict
   (`OK` / `DEGRADED` / `FAILED`), per-check status table, summary
   metrics, and today's trail are all visible there.

The dashboard is the canonical surface — there's no email side-effect.
(If you ever do want email alerts on degraded health, the CLI `--notify`
flag still wires them up via the existing SMTP `Notifier`.)

Run it manually any time:

```bash
python -m cli healthcheck            # pretty table → stdout, also publishes to dashboard cache
python -m cli healthcheck --notify   # also email the report (opt-in)
python -m cli healthcheck --json     # machine-readable; exit 1 = degraded, 2 = failed
```

The exit code makes it easy to wire into shell `until ...` loops, monitoring
agents, or CI.

### 11. One-shot status snapshot

When you want a single-screen view of *"what is the bot thinking and how much
risk budget is left right now?"* without opening the dashboard:

```bash
python -m cli status
```

It prints, in one go: today's research picks, the active watchlist (auto vs
config fallback), the risk budget remaining (daily-loss used vs cap, trades
used vs cap, open positions vs cap, kill-switch state, trading window), and a
table of currently open positions with unrealised P&L. Read-only and safe to
run any time, even while the bot is live.

### 12. Reliability guards (post-mortem from 2026-04-29)

A real incident on 2026-04-29 exposed five latent failure modes that have
since been closed. The post-mortem is worth reading because each fix
makes more sense once you understand exactly how a paper trade can ride
to EOD even when SL and TP appear to be enforced.

**The incident.** NESTLEIND paper-trade opened at ₹1454.63 at 11:42:02
with SL=₹1450.27 and TP=₹1459.35. The position rode to the 15:15 forced
square-off at ₹1466.57 — well above the configured TP. None of SL, TP, or
the trailing stop ever fired. Forensics revealed two simultaneous causes:

* The primary bot was alive and ticking the **whole day**, but
  `_manage_open_positions` was silently skipping NESTLEIND every minute
  because `intraday_bars(symbol, "1m")` was returning empty bars (yfinance
  gap or stale cache). The SL/TP/trail check was never executed for that
  position. The 5-minute fetcher was working fine, so the dashboard kept
  showing live unrealized P&L — making it look like the position was
  being managed when it wasn't.
* A **separate phantom Executor instance** got created at 13:19 by an
  unrelated CLI command path, restored a corrupt cached `paper:state`
  with `avg_price=₹2400` (a pre-split historical price for NESTLEIND),
  and emitted a fake "trail close" at ₹2405. That fictional close
  appeared in the journal alongside the real 15:15 EOD close, doubly
  confusing the picture.

**Five guards now in place:**

1. **Single-instance lock — `bot/lock.py`.** Every `python -m cli run`
   acquires an exclusive `fcntl.flock` on `<repo>/.bot.lock` before
   starting. A second `cli run` exits with a clear error and the holder
   PID. The lock is auto-released on process exit, even on `kill -9`.
   Override only with `--force-lock` (and only if you're certain).

2. **Cache freshness on restore — `bot/broker/paper.py::_restore_state`.**
   The paper broker now refuses to restore a position whose cached
   `avg_price` diverges from the current market mark by >30%, OR whose
   `saved_at` is from a prior trading day. It logs a loud `[paper]
   REFUSING TO RESTORE …` line and starts fresh.

3. **Tick heartbeat — `bot/executor.py::tick` + `bot/healthcheck.py::_tick_heartbeat`.**
   Every executor tick stamps `heartbeat:tick` in the cache. The
   healthcheck reports **FAIL** when this is >3 min stale during the
   trading window, **WARN** at >90 s. The dashboard shows a red banner
   at the top of the page when the heartbeat is stale.

4. **Position manager never silently skips —
   `bot/executor.py::_manage_open_positions`.** When `intraday_bars(sym,
   "1m")` returns empty, the loop now (a) falls back to 5-minute bars,
   (b) if those are also empty, falls back to the broker's last known
   mark, and (c) only as a true last resort when no price exists at all,
   logs a loud `[manage] {sym} has NO bars and NO mark — position is
   currently unmanaged` warning so the operator can intervene. The
   silent `continue` that caused the NESTLEIND incident is gone.

   Plus a new healthcheck `Open-position data` flags any held position
   whose 1m bars are >2 min stale (or empty entirely) during the
   trading window. This would have caught the NESTLEIND issue at the
   first 11:00 / 13:00 healthcheck.

5. **Scoped daily Redis reset — `bot/daily_reset.py`.** Wipes only the
   intraday-stateful keys (`paper:state`, `signal:*`, `heartbeat:tick`,
   `profit_lockin`, `trail:*`) at bot startup AND at 08:55 IST every
   weekday. Today's research, the auto-watchlist, the healthcheck
   history, and the fee-audit cache are **deliberately preserved** —
   they're produced fresh by their own pre-market jobs and clearing
   them would force redundant re-runs. This is defense in depth on top
   of the freshness gates from #2.

   Why not `FLUSHDB`? Blanket flush would clobber the daily-derived
   caches (research, auto-watchlist) that take minutes to regenerate
   and need to be in Redis by 09:30 for the trading loop to start
   acting. Scoped is safer.

### 12b. F&O synthetic-symbol pricing fix (post-mortem from 2026-04-30)

A second incident on 2026-04-30 surfaced a different class of bug —
not in the live position manager (covered by §12) but in the EOD
square-off pricing path for synthetic F&O instruments.

**The incident.** Two ₹100k put credit spreads opened correctly (NIFTY
24050/23950PE @ ₹44.25 net premium, BANKNIFTY 55000/54900PE @ ₹43.98
net premium). The 15:15 IST square-off then closed them at ₹24,002.40
and ₹54,842.81 respectively — those are the **NIFTY and BANKNIFTY spot
index levels**, NOT the spreads' net premium. The journal recorded
`-₹1,798,059.91` and `-₹6,580,002.21` for a fake `-₹8.3M` "loss" on
₹100k of capital.

**Root cause.** `bot/instruments/fno.py::yfinance_proxy()` had this
prefix-match fallback for futures tradingsymbols:

```python
for u in _YFINANCE_INDEX:
    if s.startswith(u):              # ← matches ANY symbol starting with "NIFTY"
        return _YFINANCE_INDEX[u]
```

Synthetic spread tradingsymbols like `NIFTY26MAY24050-23950PESPRD`
also start with "NIFTY", so the proxy returned `^NSEI` for them.
`bot/data.py::latest_quote()` then happily fetched the NIFTY spot from
yfinance and returned ₹24,002. `executor._end_of_day()` used that as
the spread's "current price" and the paper broker computed P&L
against it.

The per-minute path (`_manage_open_positions`) was unaffected because
it uses `intraday_bars()`, which checks the synthetic-instrument
parsers FIRST and routes to the Black-Scholes synthesis path. The bug
only showed up at EOD, where `_end_of_day` had been calling
`latest_quote()` directly — bypassing the synthesis path.

**Two fixes (defense in depth):**

1. **`yfinance_proxy()` rejects synthetic instruments by parser, not
   by suffix.** It now returns `None` if any of
   `parse_option_tradingsymbol`, `parse_spread_tradingsymbol`, or
   `parse_iron_condor_tradingsymbol` matches — using strict regex
   parsers so a real NSE equity that happens to end in "CE"/"PE"/"IC"
   isn't accidentally rejected. This means ANY future caller that
   asks for a yfinance ticker for a synthetic gets a clean `None` and
   knows to use the synthesis path.

2. **`_end_of_day()` now uses `intraday_bars()`** for the final mark,
   matching what `_manage_open_positions` does the rest of the day.
   No more two pricing paths — there's one synthetic-aware path, and
   it's used everywhere positions are marked.

**Cleanup of corrupt artefacts.** The corrupted ``-₹8.3M`` paper journal
and EOD report from this incident are renamed-aside (suffix
``.corrupted-by-2026-04-30-spot-leak-bug``) by the one-shot script
``scripts/cleanup_fno_2026_04_30.py``. The script also clears the
stale ``paper:state:fno`` and ``portfolio:fno`` Redis keys. It refuses
to run if the F&O bot is still alive.

A new check (`FIX #12`) in `tests/test_fixes.py` pins both fixes:

* `yfinance_proxy()` must return `^NSEI` for `NIFTY` / `NIFTY26MAYFUT`
  and `None` for every synthetic instrument format
* `executor._end_of_day` source must NOT contain `latest_quote(p.symbol)`
  and MUST contain `intraday_bars(p.symbol`

A self-contained verifier for all twelve guards lives at
`tests/test_fixes.py`:

```bash
python tests/test_fixes.py
```

It spawns a real subprocess to test the lock collision, injects a
divergent `paper:state` to test the price-divergence guard, ages a
heartbeat to test the stall detector, seeds a mix of intraday and
daily-derived keys to test the daily reset is correctly scoped, and
asserts the position-manager fallback log lines exist in source.

<a name="fno-segment"></a>
### 13. F&O segment — equity and F&O run as **two isolated bots**

The bot has been extended to trade Futures & Options in addition
to cash equities. Equity and F&O are not just two strategies inside one
process — they are **two completely isolated bots** that share zero state.
That is the answer to "I don't want clashes or locks between equity and
F&O": each segment runs in its own process, holds its own capital budget,
writes to its own Redis namespace, and keeps its own trade journal.

* **Phase 1** (done) — segment isolation scaffolding (locks, Redis
  namespaces, broker, journal, healthcheck, dashboard).
* **Phase 2** (done) — index futures trading via `futures_trend`.
* **Phase 3** (done) — directional **option BUYING** via
  `option_buy_directional` (long CE/PE, full premium debit, BS-priced).
* **Phase 4** (done) — vertical **credit-spread SELLING** via
  `credit_spread`. Bull-put-spread on bullish trend (sell ATM PE / buy
  lower-strike PE); bear-call-spread on bearish trend (sell ATM CE / buy
  higher-strike CE). Defined-risk: max_loss is bounded by
  `strike_width − net_credit`, so margin = `max_loss × qty` (~₹2-5 K
  per lot for ATM weekly NIFTY 100-pt verticals — fits the ₹50K
  budget). Theta is on YOUR side: profit when net price decays
  (canonical 50%-of-credit profit lock + 70%-of-max-loss stop).
  Black-Scholes Greeks (delta/gamma/theta/vega) added in
  `bot/options/pricing.py` for paper-mode position-management decisions.
  Spread is modelled as a SINGLE synthetic SHORT position
  (`InstrumentKind.SPREAD`), keeping the existing position manager /
  risk manager / journal contract intact.
* **Phase 4.5 (CURRENT)** — **iron condors** (4-leg defined-risk
  neutral) via `iron_condor`. Triggered by EMA *convergence* (the
  inverse of credit_spread / option_buy / futures_trend, which all
  trigger on EMA crosses) — when the trend pauses and goes flat, we
  sell a delta-neutral IC and harvest theta. Margin is the worst
  wing's max-loss (NOT the sum of both spread maxes — spot can hit at
  most ONE wing at expiry), making ICs ~50% more capital-efficient
  than running put-spread + call-spread side-by-side. Stock options /
  futures lot table extended (RELIANCE, INFY, HDFCBANK, ICICIBANK,
  TCS, SBIN, AXISBANK, KOTAKBANK, ITC, LT — values current as of Apr
  2026). Streamlit dashboard now shows live per-position Greeks +
  net-portfolio Δ/Θ/vega for the F&O segment.
* **Phase 5** (skeleton) — Live Kite Connect F&O integration: real
  option-chain LTP (replaces BS synthesis), multi-leg order
  translators for SPREAD / IRON_CONDOR (one synthetic ID → 2-4 real
  Kite orders, atomic with leg-rollback on partial fills), and real
  SPAN+exposure margin via `kite.basket_order_margins()`. The
  scaffolding lives in `bot/broker/zerodha.py` as four
  `NotImplementedError`-raising methods (`fetch_option_chain`,
  `fetch_option_ltp`, `place_spread`, `place_iron_condor`,
  `fetch_real_margin`) — accidental `LIVE_TRADING=true` fails fast
  rather than placing partial multi-leg orders.

#### Capital reality check (Phase 4.5)

The default `fno.capital.total = ₹1,00,000` covers all defined-risk and
single-lot directional structures:

| Strategy | 1-lot cost | Fits ₹1L? |
|---|---|---|
| `credit_spread` | ₹2,250 margin (width 100 − ₹70 credit) × 75 | ✅ Yes (~40 lots fit) |
| `iron_condor` (NEW Phase 4.5) | ₹2,250 margin (worst wing 100 − ₹70 credit) × 75 | ✅ Yes (~40 lots fit) |
| `option_buy_directional` | ₹150 prem × 75 = ₹11,250 | ✅ Yes (8-9 lots fit) |
| `futures_trend` | ₹91,875 margin (NIFTY @24,500 × 5%) | ✅ Tight — 1 lot, no headroom |
| naked-short option | ~₹2.5 L (15% of contract value) | ❌ No — needs ≥₹300K |

To switch strategies: edit `config.yaml` → `fno.strategies.enabled: [<choice>]`.
Bump `fno.capital.total` first if the choice's margin doesn't fit.

You can't run multiple F&O strategies in the same process — the
Ensemble voter would merge signals with incompatible SL/TP scales
(24,400 spot vs ₹120 premium vs ₹70 net credit). To run several, fork
two `--segment fno` processes with different config files (Phase 5
roadmap).

#### Running both segments concurrently

```bash
# terminal 1 — equity bot (the default; backward-compatible behaviour)
bash scripts/run_bot.sh                       # equivalent to: --segment equity

# terminal 2 — F&O bot (only after flipping `fno.enabled: true`)
bash scripts/run_bot.sh run --paper --segment fno
```

Each call acquires its OWN lockfile:

* `.bot.lock.equity` — held by the equity bot
* `.bot.lock.fno`    — held by the F&O bot

A second invocation of the SAME segment is rejected (the same single-instance
guard from [Reliability guards](#12-reliability-guards-post-mortem-from-2026-04-29) §1).
A different segment's lock is independent.

#### What's namespaced by segment

| Concern | Equity key / path | F&O key / path |
|---|---|---|
| Lock file | `.bot.lock.equity` | `.bot.lock.fno` |
| Paper book | `paper:state:equity` | `paper:state:fno` |
| Per-symbol signals | `signal:equity:RELIANCE` | `signal:fno:NIFTY26500CE` |
| Trailing-stop snapshot | `trail:equity:RELIANCE` | `trail:fno:NIFTY26500CE` |
| Tick heartbeat | `heartbeat:tick:equity` | `heartbeat:tick:fno` |
| Profit lock-in flag | `profit_lockin:equity` | `profit_lockin:fno` |
| Portfolio snapshot | `portfolio:equity` | `portfolio:fno` |
| Trade journal | `logs/trades/equity/YYYY-MM-DD.jsonl` | `logs/trades/fno/YYYY-MM-DD.jsonl` |
| EOD report | `logs/eod/equity/YYYY-MM-DD.txt` | `logs/eod/fno/YYYY-MM-DD.txt` |
| Healthcheck cache | `healthcheck:latest:equity` | `healthcheck:latest:fno` |

Daily-derived caches are **shared** intentionally (they're produced by the
equity scheduler's pre-market jobs and consumed by both segments where
relevant): `research:YYYY-MM-DD`, `watchlist:auto`, `fee_audit:latest`,
`bars:*`. The fee audit doesn't run twice — only the equity scheduler
registers it; both healthchecks read the same cached audit result.

#### Capital budget — separate ledgers, never mingled

`config.yaml` has two top-level capital blocks:

```yaml
capital:                # equity capital (top-level, backward-compat)
  total: 100000

fno:
  enabled: true         # opt-in
  capital:
    total: 100000       # SEPARATE ₹1,00,000 budget for F&O
```

A runaway loss in one segment can never bleed into the other's daily-loss
cap because each `RiskManager` instance reads its own `seg.capital.total`
and tracks its own daily-loss counter. The two `PaperBroker` instances
keep separate cash balances and separate `_positions` dicts. Even on a
single Redis instance, the keys never overlap.

#### CLI commands accept `--segment`

```bash
python -m cli run        --segment equity        # OR --segment fno
python -m cli journal    --segment fno --tail
python -m cli journal    --segment equity        # default; same as before
python -m cli status     --segment fno
python -m cli healthcheck --segment fno
```

The default for every command remains `equity`, so all existing usage and
scripts keep working unchanged.

#### Dashboard

The Streamlit dashboard now has a sidebar segment selector (Equity / F&O,
F&O option only appears when `fno.enabled: true`). Switching re-reads
everything from that segment's namespaced keys. A "Combined P&L" sidebar
widget shows realized P&L from both segments at a glance regardless of
which one the main panel is showing.

#### What Phase 2 added (futures)

| Capability | Status | Where it lives |
|---|---|---|
| Index futures (NIFTY / BANKNIFTY) tradeable | ✅ Phase 2 | `bot/strategies/fno/futures_trend.py` |
| Lot-size table + monthly expiry resolver | ✅ Phase 2 | `bot/instruments/fno.py` |
| Futures tradingsymbol formatter (NIFTY → NIFTY26MAYFUT) | ✅ Phase 2 | `bot/instruments/fno.py::tradingsymbol` |
| F&O futures fee schedule (Zerodha rates) | ✅ Phase 2 | `bot/fees.py::compute_fees(segment="futures")` |
| Paper broker margin model (5% SPAN+exposure) | ✅ Phase 2 | `bot/broker/paper.py::place_order` |
| Lot-multiple position sizing | ✅ Phase 2 | `bot/risk.py::evaluate` (FNO branch) |
| yfinance index proxy (^NSEI, ^NSEBANK) | ✅ Phase 2 | `bot/data.py::to_yf` |

#### What Phase 3 added (option buying)

| Capability | Status | Where it lives |
|---|---|---|
| Black-Scholes pricer (call/put + put-call-parity verified) | ✅ Phase 3 | `bot/options/pricing.py` |
| Strike-step rounding (NIFTY=50, BANKNIFTY=100) + ATM resolver | ✅ Phase 3 | `bot/instruments/fno.py::atm_strike` |
| Option tradingsymbol formatter (NIFTY26MAY24600CE) + parser | ✅ Phase 3 | `bot/instruments/fno.py::option_tradingsymbol` |
| F&O options fee schedule (5x equity STT, 26x futures exchange) | ✅ Phase 3 | `bot/fees.py::compute_fees(segment="options")` |
| Paper broker option BUY (full premium debit, no margin) | ✅ Phase 3 | `bot/broker/paper.py::place_order` |
| Premium-based option position sizing | ✅ Phase 3 | `bot/risk.py::evaluate` (OPTION branch) |
| Synthetic option OHLC bars from underlying spot via BS | ✅ Phase 3 | `bot/data.py::_synth_option_bars` |
| `option_buy_directional` strategy (long CE / long PE) | ✅ Phase 3 | `bot/strategies/fno/option_buy.py` |

#### What Phase 4 added (credit spreads + Greeks)

| Capability | Status | Where it lives |
|---|---|---|
| Greeks (Δ, γ, θ, vega) — put-call delta-parity verified | ✅ Phase 4 | `bot/options/pricing.py::all_greeks` |
| Vertical credit-spread max-loss / margin formula | ✅ Phase 4 | `bot/options/margin.py` |
| `InstrumentKind.SPREAD` synthetic single-position model | ✅ Phase 4 | `bot/broker/base.py` |
| Spread tradingsymbol format (`NIFTY26MAY24500-24400PESPRD`) | ✅ Phase 4 | `bot/instruments/fno.py::spread_tradingsymbol` |
| Spread resolver (ATM short + width offset, bull-put / bear-call) | ✅ Phase 4 | `bot/instruments/fno.py::resolve_credit_spread` |
| Paper broker SPREAD entry (margin = max_loss + premium credit) | ✅ Phase 4 | `bot/broker/paper.py::place_order` |
| Synthetic net-spread-price OHLC bars (BS on both legs) | ✅ Phase 4 | `bot/data.py::_synth_spread_bars` |
| Defined-risk position sizing (margin-based, lot multiples) | ✅ Phase 4 | `bot/risk.py::evaluate` (SPREAD branch) |
| `credit_spread` strategy (bull-put / bear-call) | ✅ Phase 4 | `bot/strategies/fno/credit_spread.py` |
| End-to-end smoke test (Fix #7-#9) | ✅ Phase 4 | `tests/test_fixes.py` |

#### What Phase 4.5 added (iron condors + dashboard Greeks + stock-options)

| Capability | Status | Where it lives |
|---|---|---|
| `InstrumentKind.IRON_CONDOR` synthetic 4-leg position | ✅ Phase 4.5 | `bot/broker/base.py` |
| IC tradingsymbol format (`NIFTY26MAY24300-24400-24700-24800IC`) | ✅ Phase 4.5 | `bot/instruments/fno.py::iron_condor_tradingsymbol` |
| IC resolver (ATM ± wings_distance, both width legs) | ✅ Phase 4.5 | `bot/instruments/fno.py::resolve_iron_condor` |
| Capital-efficient IC margin (worst wing − credit, NOT sum) | ✅ Phase 4.5 | `bot/broker/paper.py::place_order` |
| Synthetic net-IC-price OHLC bars (4-leg BS) | ✅ Phase 4.5 | `bot/data.py::_synth_iron_condor_bars` |
| `iron_condor` strategy (EMA-convergence trigger) | ✅ Phase 4.5 | `bot/strategies/fno/iron_condor.py` |
| IC sizing in risk manager (defined-risk, lot multiples) | ✅ Phase 4.5 | `bot/risk.py::evaluate` (IC branch) |
| F&O Greeks panel on dashboard (per-position + portfolio Δ/Θ/vega) | ✅ Phase 4.5 | `app.py` |
| Stock-options/futures lot table (10 most-liquid F&O stocks) | ✅ Phase 4.5 | `bot/instruments/fno.py::LOT_SIZES` |
| End-to-end IC smoke test (Fix #10) | ✅ Phase 4.5 | `tests/test_fixes.py` |

#### What Phase 5 scaffolding added (going-live skeletons)

| Capability | Status | Where it lives |
|---|---|---|
| Live Kite NFO instrument-master loader | 🟡 Skeleton | `bot/broker/zerodha.py::_instruments` |
| Live option-chain LTP fetch (replaces BS synthesis) | 🟡 Skeleton | `bot/broker/zerodha.py::fetch_option_chain` / `fetch_option_ltp` |
| Multi-leg spread order translator (SPREAD → 2 real orders) | 🟡 Skeleton | `bot/broker/zerodha.py::place_spread` |
| Multi-leg IC order translator (IC → 4 real orders, longs first) | 🟡 Skeleton | `bot/broker/zerodha.py::place_iron_condor` |
| Real SPAN+exposure margin via Kite basket-margin API | 🟡 Skeleton | `bot/broker/zerodha.py::fetch_real_margin` |

Each Phase 5 method raises `NotImplementedError` with a clear TODO
list — accidental `LIVE_TRADING=true` fails fast rather than placing
partial multi-leg orders.

#### F&O going-live checklist (Phase 5 hand-off)

Before flipping `LIVE_TRADING=true` for the F&O bot, complete these
steps in order. Each one is independent — completing 1-3 unlocks
options-buying live; 4-5 unlock spreads; 6-7 unlock iron condors. Skip
ahead at your own risk; partial-fill semantics on a 4-leg IC can leave
naked-short legs exposed for ₹L+ loss in seconds.

1. **Kite Connect subscription** (~₹2,000/month) and TOTP-driven
   daily login working (`python -m cli login` returns a fresh
   `KITE_ACCESS_TOKEN`). The equity adapter (`bot/broker/zerodha.py`)
   already implements equity orders — verify with a single
   single-share dummy on a low-priced equity first.
2. **Implement `_instruments()` smoke test** — call
   `kite.instruments("NFO")` once at bot startup and cache. Should
   return ~40,000 rows. Memory ~50 MB; fine.
3. **Implement `fetch_option_ltp(tradingsymbol)`** — for each
   tradingsymbol the bot mints, look up `instrument_token` in the
   cached master, then `kite.ltp([f"NFO:{tradingsymbol}"])`. Replaces
   the BS fallback in `bot/data.py::_synth_option_bars` for live
   mode (gate behind `env.LIVE_TRADING`).
4. **Implement `place_spread(order)`** — translate
   `NIFTY26MAY24500-24400PESPRD` into two real Kite orders. Place
   the LONG leg FIRST (so any rejection short-circuits before we sell a
   naked option). Persist a `spread_id → leg_order_ids` map in Redis;
   reconstitute the synthetic Position once both legs fill.
5. **Implement `fetch_real_margin([orders])`** — call
   `kite.basket_order_margins(...)` and return the actual SPAN+
   exposure margin. Risk manager's F&O sizing branch consults this in
   live mode (replaces the heuristic `max_loss × qty`).
6. **Implement `place_iron_condor(order)`** — same shape as
   `place_spread` but FOUR legs. **Both LONG legs first** (protective
   wings — these are the ones that prevent unbounded loss). Then both
   SHORT legs. If any short rejects, immediately close the longs.
   Reconstitute one IC Position once all four fill.
7. **Smoke-test on 1-lot, 1-day expiry, illiquid strike** — the
   smallest-possible order to verify atomicity. Watch `kite.orders()`
   for partial fills. Set `KILL_SWITCH` ready before placing the
   order.

Until all seven are green, leave `LIVE_TRADING=false` and let the
paper broker run. The Phase 4.5 implementation is a complete
end-to-end paper system; you can validate the strategy logic without
touching real money.

#### What's still NOT included (lands in later phases)

| Capability | Lands in |
|---|---|
| Naked short options strategy (margin model exists, no strategy yet) | Phase 6 |
| F&O pre-market research agent (IV-rank ranking) | Phase 6 |
| Two F&O strategies in parallel (multi-config processes) | Phase 6 |
| Live Phase 5 implementations (replace skeleton `NotImplementedError`s) | Phase 5+ |

#### Phase 2 strategy: `futures_trend`

EMA20 / EMA50 crossover on 5-minute bars of the underlying index spot
(yfinance proxy in paper mode). The configured defaults — wider than the
equity strategies because index futures move more in absolute INR per bar
— are ATR×2.0 stop-loss and ATR×3.0 take-profit, with a 5-bar
cross-lookback to avoid chasing trends that have already played out.
Tunable per-segment in `config.yaml` under `fno.strategies.futures_trend`.

Confidence is set at 0.70 vs. equity's 0.75 because we're trading off a
spot proxy with futures-vs-cash basis skew (typically 0.1-0.5% on stable
sessions, more under volatility). This will be tightened to 0.80+ once
Phase 5 swaps in real Kite futures bars.

#### Phase 3 strategy: `option_buy_directional` (default)

Same EMA20/EMA50 crossover engine as `futures_trend`, but on a bullish
cross it buys the **at-the-money CE** of the current monthly expiry, and
on a bearish cross it buys the **at-the-money PE**. Both are LONG
positions: max loss is the full premium paid, max gain is uncapped (call)
or capped at strike (put).

How SL/TP work:

1. The strategy picks an ATR-based stop-loss and take-profit in
   **spot space** — same shape as `futures_trend`:
   `sl_spot = entry_spot − 2.0 × ATR`, `tp_spot = entry_spot + 3.0 × ATR`.
2. It then translates those spot levels to **premium space** via
   Black-Scholes at the SL spot and TP spot:
   `sl_premium = BS(sl_spot, K, T, σ, r, opt_type)`.
3. The signal returns the OPTION's tradingsymbol with SL/TP in premium.
4. The position manager evaluates SL/TP against **synthetic option bars**
   that `bot/data.py::intraday_bars` produces from the underlying spot
   bars + Black-Scholes — internally consistent with step 2.

The `min_sl_premium_pct` config (default 0.30) floors the SL premium at
30% of entry premium, so a single fast spike can't take more than 70%
of the premium before the position manager reacts.

Confidence is set at 0.65 (vs futures 0.70) because options carry IV
risk on top of basis risk: a sudden IV crush after the cross can drop
premium even as the underlying continues in our favour. Phase 5 will
calibrate IV from live Kite quotes; until then a constant 15% IV is
"close enough" but introduces ±10-20% noise vs real fills.

#### Phase 4 strategy: `credit_spread` (default)

Same EMA20/EMA50 cross trigger as the other F&O strategies. On each
cross we open a defined-risk vertical credit spread:

| Cross | Spread type | Construction |
|---|---|---|
| Bullish (fast crosses above slow + price above fast) | bull-put-spread | sell ATM PE, buy `(ATM − strike_width)` PE |
| Bearish (fast crosses below slow + price below fast) | bear-call-spread | sell ATM CE, buy `(ATM + strike_width)` CE |

Defined-risk math (NIFTY width-100 bull-put @ entry net credit ₹70):

```
short PE   premium ≈  ₹100  (ATM, larger time value)
long PE    premium ≈  ₹30   (₹100 OTM, smaller time value)
net credit  = 100 − 30  = ₹70 / share
max_loss/share = width − net_credit = 100 − 70 = ₹30
margin/lot     = max_loss/share × lot  = 30 × 75 = ₹2,250
max_gain/lot   = net_credit × lot      = 70 × 75 = ₹5,250  (capped)
```

SL / TP in NET-PRICE space:

* **TP** = entry × (1 − `profit_lock_pct`) — close when net price decays
  to half of entry credit. Default 50% lock = canonical intraday-spread
  target. This is theta-decay + favourable-direction profit.
* **SL** = entry + `sl_max_loss_pct` × max_loss/share — close when net
  price climbs to 70% of structural max-loss above entry. Wider than
  50% to ride through normal chop.

The position is modelled as a SINGLE synthetic SHORT position at the
spread's tradingsymbol (`NIFTY26MAY24500-24400PESPRD`) — the existing
position manager evaluates SL/TP against synthetic net-price bars
that `bot/data.py::_synth_spread_bars` produces from the underlying
spot bars + Black-Scholes on both legs. Going-live (Phase 5) will
expand the spread tradingsymbol back into two real Kite orders at
order-placement time.

#### Phase 4.5 strategy: `iron_condor` (NEW default)

The **anti-cross** strategy. Where every other F&O strategy fires on
fresh EMA20/EMA50 crossings, the iron condor fires when the EMAs
*converge* — when the trend has paused and price is consolidating.
This is exactly when theta-collection is most attractive and
directional bets are weakest. Trigger:

```
flatness = |ema20 − ema50| / spot < 0.30%   →   sell iron condor
```

Each fire opens a 4-leg defined-risk neutral structure:

```
                    spot now: 24530
   long PE    short PE         short CE   long CE
   24350      24450            24650      24750
     │         │                │           │
     └─ ₹100 ─┘                └── ₹100 ──┘
       put-spread                  call-spread
                  ↑ (delta-neutral by symmetry)
```

Defined-risk math (NIFTY 100/100 wings, net credit ₹70):

```
short PE  ≈ ₹95  (ATM-side)        short CE  ≈ ₹95  (ATM-side)
long PE   ≈ ₹65  (OTM)             long CE   ≈ ₹65  (OTM)
put-side credit  = 95 − 65 = ₹30   call-side credit = 95 − 65 = ₹30
total credit     = 30 + 30 = ₹60  (lower than a single vertical)

max_gain/share   = net_credit                          = ₹60
max_loss/share   = max(put_width, call_width) − credit  = 100 − 60 = ₹40
                                ↑
              NOT (put_width + call_width − credit) — spot can hit
              AT MOST ONE wing at expiry; capital-efficient by ~50%

margin/lot       = max_loss/share × lot                = 40 × 75 = ₹3,000
breakeven_lower  = put_short  − net_credit             = 24450 − 60 = 24390
breakeven_upper  = call_short + net_credit             = 24650 + 60 = 24710
profit zone      = (24390, 24710) — ~320 pts wide
```

SL / TP in NET-PRICE space (same shape as `credit_spread`):

* **TP** = entry × (1 − `profit_lock_pct`) — default 50% lock
* **SL** = entry + `sl_max_loss_pct` × max_loss/share — default 70%

Asymmetric wings (`put_width=100, call_width=200`) express a
directional skew (skew towards calls = slight bearish bias). For
neutral, leave both at the same value.

Confidence is set at 0.55 (vs spread 0.65, options 0.65, futures
0.70) — neutral structures pay you more often (~70% win rate) but
the per-trade payoff is smaller, so each individual signal carries
less conviction than a directional one.

The position is modelled as a SINGLE synthetic SHORT position at the
IC's tradingsymbol (`NIFTY26MAY24300-24400-24700-24800IC`). All four
legs are BS-priced together to produce net-IC-price bars in
`bot/data.py::_synth_iron_condor_bars`. Phase 5 will translate the
synthetic ID into four real Kite orders, placing the protective
LONG legs FIRST (so any rejection short-circuits before we sell a
naked option).

#### Phase 3 fee economics — why options drag harder than futures

Every options fee leg is calculated against **option premium**, not
contract value. For a 1-lot NIFTY 24600 CE @ ₹150, premium turnover is
75 × ₹150 = ₹11,250 — about 1/164th of the contract value (75 × 24,600
= ₹1,845,000). So even though the percentage rates LOOK higher than
futures, the absolute fees per round-trip are much smaller.

A worked round-trip:

```
Entry:  buy 1 lot ATM CE @ ₹150 prem = ₹11,250 cash debit + ₹17 fees
Exit:   sell 1 lot ATM CE @ ₹200 prem = ₹15,000 cash credit − ₹38 fees (incl. ₹9.38 STT)
                                         (STT = 0.0625% × 75 × 200 = ₹9.38)

Gross P&L: (200 − 150) × 75 = ₹3,750
Net P&L:   ₹3,750 − ₹17 − ₹38 = ~₹3,695

Break-even premium move: ~₹0.75/share, i.e. premium needs to move
0.5% on a ₹150 entry just to cover fees. Easy on a 100-pt favourable
spot move; brutal on a flat day after IV crush.
```

The Fix #8 smoke test verifies this round-trip end-to-end —
`tests/test_fixes.py` shows the actual numbers from a real run.

### 14. Going live (DANGEROUS)

After ≥2 weeks of profitable paper trading:

1. Set `LIVE_TRADING=true` in `.env`
2. Set `BROKER=zerodha` (or `dhan`)
3. Fill in broker API keys + run `python -m cli login`
4. Start with **₹10,000 capital max** for the first week
5. Monitor every trade — email alerts fire on every fill and rejection

## Module map

> The block between the markers below is **auto-generated** from the project tree
> by `python -m cli regen-readme`. Do not hand-edit; install the git hook with
> `bash scripts/install_hooks.sh` to keep it fresh on every commit.

<!-- AUTO-MODULE-MAP-START -->
```text
Stock-Market-Bot/
├── README.md                            Full usage + warnings + going-live checklist
├── requirements.txt                     Pinned deps
├── .env.example                         Copy to .env (broker keys, OpenAI, Redis, SMTP, TOTP)
├── config.yaml                          Capital, risk limits, watchlist, strategy params
├── cli.py                               Typer CLI: run / login / backtest / research / dashboard …
├── app.py                               Streamlit dashboard (P&L, positions, charts, signals)
├── bot/
│   ├── broker/
│   │   ├── base.py                      Broker interface (ABC) + Order / Position dataclasses.
│   │   ├── dhan.py                      Dhan API adapter (skeleton).
│   │   ├── paper.py                     Paper broker — full simulator with slippage and Indian fee model.
│   │   └── zerodha.py                   Zerodha Kite Connect adapter (skeleton).
│   ├── feeds/
│   │   └── kite_ws.py                   Zerodha KiteTicker WebSocket — sub-second tick stream pushed to Redis.
│   ├── instruments/
│   │   └── fno.py                       F&O instrument definitions and resolvers.
│   ├── options/
│   │   ├── margin.py                    Margin model for paper-mode option selling (Phase 4).
│   │   └── pricing.py                   Black-Scholes-Merton pricer for paper-mode option premiums.
│   ├── strategies/
│   │   ├── fno/
│   │   │   ├── credit_spread.py         Credit-spread strategy — F&O segment, Phase 4.
│   │   │   ├── futures_trend.py         Index-futures trend-following strategy — F&O segment, Phase 2.
│   │   │   ├── iron_condor.py           Iron-condor strategy — F&O segment, Phase 4.5.
│   │   │   └── option_buy.py            Directional option-buying strategy — F&O segment, Phase 3.
│   │   ├── base.py                      Strategy base class, Signal dataclass, and shared ATR-based SL/TP helper.
│   │   ├── ema_supertrend.py            EMA crossover with Supertrend filter.
│   │   ├── ensemble.py                  Ensemble voter — combines multiple strategies into one final signal.
│   │   ├── multitimeframe.py            Multi-timeframe wrapper: confirms a fast-frame signal on a slower frame.
│   │   ├── orb.py                       Opening Range Breakout (ORB) strategy.
│   │   └── vwap_revert.py               VWAP mean-reversion strategy.
│   ├── backtest.py                      Bar-by-bar backtester.
│   ├── backtest_advanced.py             Advanced backtester — performance metrics + walk-forward analysis.
│   ├── cache.py                         Redis cache wrapper with a no-op in-memory fallback when Redis is unavailable.
│   ├── config.py                        Loads runtime configuration from `.env` (secrets) and `config.yaml` (strategy params).
│   ├── daily_reset.py                   Scoped daily Redis reset.
│   ├── data.py                          Market data layer.
│   ├── executor.py                      Executor — the orchestrator.
│   ├── fee_audit.py                     Daily fee-schedule audit against authoritative public sources.
│   ├── fees.py                          Indian intraday fee model — equity (intraday) + F&O futures + F&O options.
│   ├── healthcheck.py                   Periodic health check for the running bot.
│   ├── holidays.py                      NSE/BSE trading-holiday calendar.
│   ├── indicators.py                    Technical indicators implemented in pure pandas/numpy (no `ta-lib`).
│   ├── journal.py                       Trade journal — per-day live log of every order, paired round-trip trade, and EOD P&L.
│   ├── lock.py                          Single-instance lock for the bot process.
│   ├── logger.py                        Structured logging via loguru. Writes to stdout + rotating file.
│   ├── notify.py                        Notifier — sends email alerts on fills and risk rejections.
│   ├── research.py                      Pre-market research agent.
│   ├── risk.py                          Risk manager — the most important module in the bot.
│   ├── scheduler.py                     APScheduler-based runner with IST market hours.
│   ├── segment.py                       Segment — equity vs F&O isolation primitive.
│   ├── universe.py                      NSE stock universe — used by the watchlist updater as the candidate pool.
│   └── watchlist_updater.py             Auto-watchlist updater.
└── scripts/
    ├── clean_redis_session.py           Standalone, manual version of bot.daily_reset — wipes intraday Redis keys for both segments. Has --status (read-only) and --dry-run modes.
    ├── cleanup_equity_2026_05_04.py     One-shot cleanup of the 2026-05-04 ADANIENT phantom-short incident (~₹25k ledger leak from duplicate _end_of_day call).
    ├── cleanup_fno_2026_04_30.py        One-shot cleanup of the 2026-04-30 F&O paper-bot ``-₹8.3M`` incident.
    ├── eod_apply_fee_updates.py         End-of-day automation — re-runs the multi-source fee audit, applies confirmed drifts to bot/fees.py with backup, and restarts both bots. Auto-detects + cleans the May-04 corruption signature on the way.
    ├── install_hooks.sh
    ├── premarket_preflight.py           Pre-market pre-flight — maker/checker harness with logged outputs. Pins FIX #12 + FIX #13; checks Redis session-key freshness.
    ├── run_bot.sh
    ├── system_audit.py                  Read-only deep audit of running bots — heartbeats, signal pipeline, broker state, Redis layout, scheduler, fee audit, holiday calendar. Use mid-day for "is everything OK?".
    ├── update_readme.py                 Auto-regenerate sections of README.md from the actual project tree.
    └── zerodha_login.py                 Zerodha Kite Connect — daily TOTP-based access-token bootstrap.
```
<!-- AUTO-MODULE-MAP-END -->

## Live trading extras

| Feature | What it does | How to use |
|---------|--------------|-----------|
| **Email alerts** | Sends an email on every fill and every risk-rejection. Async (never blocks the trading loop). Set `NOTIFY_LEVEL=WARNING` to mute fill notices. | Configure `SMTP_*` and `NOTIFY_TO` in `.env`. Test with `python -m cli notify-test`. |
| **KiteTicker WebSocket** | Replaces 60-second polling with a true sub-second tick stream. Ticks land in Redis under `tick:<SYMBOL>`. | Set `BROKER=zerodha` and `feed.use_websocket: true`. Auto-engages when `LIVE_TRADING=true`. |
| **Daily TOTP login** | Refreshes `KITE_ACCESS_TOKEN` automatically using your username + password + TOTP seed. SEBI compliant — no session sharing. | `python -m cli login` (run once each morning). |
| **Walk-forward backtest** | Reports OOS Sharpe / Sortino / MDD / Calmar / profit factor — the only metrics that aren't overfit. | `python -m cli walk-forward --days 60` |
| **Multi-timeframe confirmation** | A 5-minute strategy signal is only taken if the 15-minute trend agrees. Cuts countertrend noise sharply. | `strategies.multitimeframe.enabled: true` in `config.yaml`. |
| **Auto-watchlist** | Each morning, scans NIFTY 100 and ranks by liquidity, SMA20 trend slope, momentum and ATR. Top N become the day's watchlist, **always written back to `config.yaml`** so the YAML stays the canonical source of truth. Comments and ordering in the file are preserved (via `ruamel.yaml`). Empty selections are skipped to avoid clobbering. | Enabled by default; tune in `watchlist_updater:`. Manually: `python -m cli update-watchlist`. |
| **Self-updating README** | The Module map above regenerates from the actual project tree, so it never drifts as you add files. | `python -m cli regen-readme` or install the git hook (`scripts/install_hooks.sh`). |
| **Daily trade journal** | Every fill is appended to `logs/trades/YYYY-MM-DD.jsonl` (live tail-able). Round-trip trades go to `.csv`. A pretty-printed P&L statement is written to `logs/eod/YYYY-MM-DD.txt` 2 minutes after square-off. | `python -m cli journal` for today, `--date` for any past day, `--tail` to follow live, or open the dashboard. |

### Daily trade journal — file layout

```text
logs/
  trades/
    2026-04-27.jsonl     # one JSON event per line  (FILL / TRADE_OPEN / TRADE_CLOSED)
    2026-04-27.csv       # round-trip trades only — easy to import into Excel / Sheets
  eod/
    2026-04-27.txt       # formatted End-of-Day P&L statement (auto-written 15:17 IST)
```

Each `TRADE_CLOSED` event records: side (LONG/SHORT), qty, entry/exit price &
time, duration, gross P&L, total fees, net P&L, exit reason
(`stop_loss` / `take_profit` / `eod_squareoff` / `manual`), and the strategy
that produced the entry. The EOD report aggregates these into total trades,
win rate, profit factor, payoff ratio, biggest win/loss, and per-strategy +
per-symbol breakdowns.

## Daily operational flow — how the code actually runs

> Research and the watchlist updater do **not** run during 09:15–14:45. They run
> *before* the bell, produce cached outputs, and the per-minute tick simply
> consumes those caches. The flow below is grounded in the code that actually
> runs.

### The four phases — what YOU do each trading day

A trading day is split into four phases. Phase 1 has a single-command
verifier (`premarket_preflight.py`) so you don't have to remember the
ten manual checks; the rest are thin operator routines.

#### Phase 0 — Cleanup (only after an incident or capital change)

Skip this on a normal day. Run it the night before / the morning of:

* a config change that shifted ``capital.total`` (the paper broker
  refuses to restore positions whose cached ``avg_price`` differs by
  >30% from the current mark, but a clean Redis is friendlier than a
  loud refusal in the logs),
* a known incident like the 2026-04-30 ``-₹8.3M`` synthetic-symbol
  pricing bug — the corrupted ``paper:state:fno`` snapshot won't
  auto-heal until you run the cleanup script.

```bash
# Stop the F&O bot first (Ctrl-C in its terminal).
python scripts/cleanup_fno_2026_04_30.py
```

The cleanup script (a) refuses to run while the F&O bot is alive,
(b) renames the corrupted journal/EOD/CSV aside with a
``.corrupted-by-2026-04-30-spot-leak-bug`` suffix (preserved for
post-mortem), and (c) deletes the stale ``paper:state:fno`` and
``portfolio:fno`` Redis keys.

#### Phase 1 — Pre-market pre-flight (07:30 – 08:00 IST)

A single command that validates every precondition before launch:

```bash
python scripts/premarket_preflight.py
```

This is a **maker/checker harness** — every step is one of two kinds:

* **Checker** (read-only): Python venv active, no stale `cli.py run`
  processes, no orphan `.bot.lock.*` files, Redis reachable, disk space
  OK, logs/ writable, `config.yaml` parses cleanly, `paper:state:*`
  cash balances are sane (catches BOTH the 2026-04-30 spread blow-up
  and the 2026-05-04 phantom-short signatures, then routes to the
  right cleanup script), and **Redis session-key freshness** —
  intraday keys (`signal:`, `trail:`, `heartbeat:tick:`,
  `paper:state:`, `eod_done:`) are scanned for timestamps older than
  12 h. Stale keys cause WARN with a pointer to
  `scripts/clean_redis_session.py`.
* **Maker** (mutates state): refresh NSE holiday calendar, run the
  source-level regression pins for FIX #12 (synthetic-symbol pricing,
  the 2026-04-30 fix) AND FIX #13 (EOD race idempotency + equity
  over-sell guard, the 2026-05-04 fix). If ANY pin regresses, the
  preflight FAILs critical and the bot is unsafe to start.
* **Decision checkers** (post-make): today is a trading day,
  tomorrow advisory (heads-up if MIS positions need to plan around
  a holiday).

Output:

* Pretty terminal table with ``PASS / WARN / FAIL / SKIP`` per step
  plus elapsed time and next-step suggestions.
* Tee'd to ``logs/preflight/YYYY-MM-DD_HHMMSS.log`` (plain text, grep-able).
* Machine-readable sidecar at ``logs/preflight/YYYY-MM-DD_HHMMSS.json``.
* ``logs/preflight/latest.log`` symlink for quick `tail -f`.

Exit codes:

* **0** — all checks PASSED → safe to start the bots
* **1** — at least one critical FAIL → **DO NOT START**
* **2** — some warnings → review and decide

CLI flags:

```bash
python scripts/premarket_preflight.py --auto-cleanup   # auto-run the
                                                       # matching cleanup
                                                       # script if a
                                                       # corruption signature
                                                       # is detected (Apr-30
                                                       # OR May-04)
python scripts/premarket_preflight.py --clean-redis    # auto-wipe stale
                                                       # Redis session keys
                                                       # if the freshness
                                                       # check WARNs
python scripts/premarket_preflight.py --json           # JSON to stdout
                                                       # in addition to
                                                       # the table
```

##### Manual Redis session cleanup

The bot's scheduler runs `bot.daily_reset.daily_reset` automatically at
**08:55 IST** every weekday — but only while the bot is running. If you
stop the bot mid-day, it crashes, or you start it after 08:55 IST, the
daily reset never fires and yesterday's keys can leak into today
(this is what caused the 2026-05-04 11:46–13:19 trading blind-spot —
a stale signal from earlier in the day short-circuited the live tick).

`scripts/clean_redis_session.py` is the standalone equivalent and is
safe to run any time the bots are stopped. Three modes:

```bash
python scripts/clean_redis_session.py --status   # READ-ONLY report:
                                                  # how many keys, which
                                                  # are stale, what would
                                                  # change. No writes.
                                                  # Exit 2 if any stale
                                                  # found (preflight
                                                  # uses this).
python scripts/clean_redis_session.py --dry-run   # Show the cleanup plan
                                                  # without applying it.
python scripts/clean_redis_session.py             # Do it. Refuses to
                                                  # run while any
                                                  # `cli.py run`
                                                  # process is alive.
```

What it cleans (per segment):

| Key                            | Why clear it                                |
|--------------------------------|---------------------------------------------|
| `paper:state:<seg>`            | Broker positions/cash — intraday only      |
| `heartbeat:tick:<seg>`         | Last-tick timestamp — new process, new clock |
| `profit_lockin:<seg>`          | Daily P&L target halt — resets per day     |
| `signal:<seg>:*`               | Per-symbol signal cache (root cause of stale-signal blind-spots) |
| `trail:<seg>:*`                | Trailing-stop snapshots — must rebuild from live data |
| `eod_done:<seg>` (if past date)| EOD idempotency marker (today's marker is preserved) |

What it explicitly does NOT clear: `research:YYYY-MM-DD`, `watchlist:auto`,
`healthcheck:latest:*`, `fee_audit:latest`, `holidays:*`, `bars:*`, `orders`.

#### Phase 2 — Launch the bots (immediately after Phase 1 passes)

Open three terminals. Aim to be running by **07:45 IST** so the 08:00
scheduler hooks (watchlist updater) catch you:

```bash
# Terminal 1 — equity bot
bash scripts/run_bot.sh run --paper

# Terminal 2 — F&O bot
bash scripts/run_bot.sh run --paper --segment fno

# Terminal 3 — dashboard
python -m cli dashboard
```

Open the printed URL (typically ``http://localhost:8501``) in your
browser. Once both bots log ``[scheduler] segment=<seg> ready`` you
can walk away — the rest is automatic.

What auto-runs (no operator action needed):

| IST time | Job | What you'll see on the dashboard |
|---|---|---|
| **06:00** | NSE holiday calendar refresh | Schedule-card source pill stays 🟢 NSE |
| **08:00** | Watchlist updater | Watchlist count refreshed; top-15 logged |
| **08:30** | Research agent | "Today's research picks" populates (Equity) |
| **08:55** | Daily Redis reset | Stale intraday keys cleared |
| **09:00** | Fee/tax audit | "Fee schedule audit" card on System tab |
| **09:00 / 11 / 13 / 15** | Health checks | System tab updates each cycle |
| **09:15** | Market opens | Header pill flips to 🟢 MARKET OPEN |
| **09:30** | Trade window opens | Heartbeat ♥ chip green; signals stream |
| **14:45** | Trade cutoff | No new entries; existing positions kept |
| **15:15** | Square-off | All MIS positions force-closed |
| **15:17** | EOD report | Performance tab → EOD report populated |

#### Phase 3 — During market hours (monitoring)

Refresh the dashboard browser tab every 30–60 minutes and check the
header status pills:

| Indicator | Healthy state | Action if not |
|---|---|---|
| `♥ <60s` chip | Green, age < 60 s | **>3 min red** = bot stalled — check `tail -f logs/bot_*.log` and `python -m cli healthcheck --segment <seg>` |
| `🟢 MARKET OPEN` | Green during 09:15-15:30 | If 🔴 HOLIDAY appears unexpectedly → run `python -m cli holidays --refresh` to pull the latest NSE list |
| Hero P&L number | Color-coded (green/red), progress bar tracking target | If progress > 80% → daily target lock-in is imminent (no new entries after lock-in) |
| Open-positions table | "Net @ Now" matches your read of the underlying | If a position shows Now ≈ entry ± 2×ATR for >30 min → check the bot log; the position-manager fallback chain may have engaged |

Quick CLI snapshot at any time:

```bash
python -m cli status                     # picks + watchlist + risk budget + open positions
python -m cli status --segment fno       # F&O view
```

Live tail logs (only when something looks off):

```bash
tail -f logs/bot_$(date +%F).log         # equity (default)
tail -f logs/bot_fno_$(date +%F).log     # F&O (if separate file)
```

#### Phase 4 — Post-close review (after 15:30 IST)

```bash
python -m cli journal --segment equity   # today's EOD report — equity
python -m cli journal --segment fno      # today's EOD report — F&O
ls -la logs/trades/{equity,fno}/$(date +%F).csv  # CSV exports
grep -E "ERROR|WARNING" logs/bot_$(date +%F).log | head -20   # any noise?
```

The bots can be **left running** past 15:30 — they idle until tomorrow's
06:00 holiday refresh (the cron triggers gate everything to ``mon-fri``
so they no-op gracefully on weekends). Or Ctrl-C if you want to free
the Mac for the night. Both are fine.

#### Cheat-sheet to pin near your monitor

```text
PRE:    python scripts/premarket_preflight.py
GO:     bash scripts/run_bot.sh run --paper                      (eq)
        bash scripts/run_bot.sh run --paper --segment fno        (fno)
        python -m cli dashboard
WATCH:  dashboard ♥<60s + 🟢 OPEN + hero P&L + progress bar
POST:   python -m cli journal --segment equity / --segment fno
```

#### Escalation triggers — when to STOP and investigate

If any of these happen, **don't trust the bot — pause trading**:

1. **Heartbeat red `🛑 STALL`** at top of dashboard — bot is silent. Check the bot terminal for tracebacks; restart if needed.
2. **System tab overall = `FAILED`** — read the failing checks. Some are FYI (e.g. "Bot log: 5 errors") but a Redis or Portfolio failure is critical.
3. **Day P&L exceeds −2 %** — bot should auto-halt, but verify `daily_pnl_pct` in the snapshot is < −2 % and no new orders are placed.
4. **Open position with no SL/TP** — bug; close manually via Zerodha/Kite.
5. **Cash < 0** — calculation bug like 2026-04-30; run `python scripts/cleanup_fno_2026_04_30.py` and file an issue.

### Daily timeline (Mon–Fri, IST)

```text
08:00  NSE holidays      ─►  refresh holiday-master JSON → cache (runs daily, even Sat/Sun)
08:15  watchlist updater  ─►  scans NIFTY 100 → top 15 → writes config.yaml + cache
08:30  research agent     ─►  scans those 15 → top 10 picks (LLM/heuristic) → cache
08:55  daily Redis reset  ─►  clear intraday-only keys for both segments (per-segment scope)
09:00  fee/tax audit      ─►  cross-check fee schedule across Zerodha + Upstox + Dhan (equity scheduler only)
09:00  health-check       ─►  publish healthcheck:latest:<seg> for the dashboard (also at 11/13/15)
09:15  market opens        (bot waits — outside trade_start)
09:30  trade_start         ◄── executor.tick() begins acting
13:30  trade_cutoff        ◄── no NEW positions after this (was 14:45)
15:15  square_off          ◄── force-close all open positions (race-safe via eod_done marker)
15:17  EOD report          ─►  logs/eod/YYYY-MM-DD.txt for each segment
  │
  │   every 1 minute, on each cached pick:
  │     fetch 5-min bars → ensemble vote → risk check → broker
  │
14:45  trade_cutoff        ── no NEW positions; existing positions held
15:15  square_off          ── force-close everything, journal each fill
15:17  EOD P&L report      ── write logs/journal/eod-YYYY-MM-DD.txt
```

The cron jobs that wire this up are in `bot/scheduler.py`:

```python
# bot/scheduler.py — jobs registered at bot startup
if cfg.watchlist_updater.enabled:
    wh, wm = map(int, cfg.watchlist_updater.run_at.split(":"))
    sched.add_job(
        update_watchlist,
        CronTrigger(day_of_week="mon-fri", hour=wh, minute=wm, timezone=IST),
        id="watchlist_updater",
        replace_existing=True,
    )
    logger.info("Scheduled watchlist updater at {}", cfg.watchlist_updater.run_at)

if cfg.research.enabled:
    rh, rm = map(int, cfg.research.run_at.split(":"))
    sched.add_job(
        run_research,
        CronTrigger(day_of_week="mon-fri", hour=rh, minute=rm, timezone=IST),
        id="pre_market_research",
        replace_existing=True,
    )
    logger.info("Scheduled pre-market research at {}", cfg.research.run_at)

so = cfg.session.t("square_off")
sched.add_job(
    executor.tick,
    CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*/1", timezone=IST),
    id="executor_tick",
    replace_existing=True,
)
```

### What each pre-market job produces

| Job | When | Reads | Writes |
|-----|------|-------|--------|
| **Watchlist updater** (`update_watchlist`) | 08:00 | NIFTY 100 daily bars from yfinance | Top 15 ranked by liquidity + SMA20 slope + momentum + ATR fitness → `watchlist.symbols` in `config.yaml` + cache key `watchlist:auto` |
| **Research agent** (`run_research`) | 08:30 | Each watchlist symbol: gap %, EMA9 / EMA21 posture, RSI14, news headlines | Top N picks (default 5) with `bias` (long/short/neutral), `score`, `rationale` → cache key `research:YYYY-MM-DD` |

So by 09:30 the cache holds two artifacts: a *"what universe to look at"* (watchlist) and a *"of those, what to focus on today"* (research picks).

### The 09:30–14:45 trading loop — what the bot does every minute

`executor.tick()` is the orchestrator. Each call is a self-contained cycle:

```python
# bot/executor.py — the per-minute tick
def tick(self) -> None:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return

    if self.risk.should_square_off(now.time()):
        self._end_of_day()
        return

    if not self.risk.in_trading_window(now.time()):
        return

    marks: dict[str, float] = {}
    signals: List[Signal] = []
    for sym in self._watchlist():
        df = intraday_bars(sym, "5m")
        if df.empty:
            continue
        marks[sym] = float(df["close"].iloc[-1])
        sig = self.ensemble.generate(sym, df)
        signals.append(sig)
        self._publish(f"signal:{sym}", {
            "type": sig.type.value, "price": sig.price,
            "stop_loss": sig.stop_loss, "take_profit": sig.take_profit,
            "confidence": sig.confidence, "reason": sig.reason,
            "ts": now.isoformat(),
        })

    self.broker.update_marks(marks)

    for sig in signals:
        if sig.type == SignalType.HOLD:
            continue
        decision = self.risk.evaluate(sig)
        logger.info("[risk] {} {} -> {}: {}", sig.symbol, sig.type.value,
                    "APPROVED" if decision.allow else "REJECTED", decision.reason)
        if not decision.allow:
            self.notifier.rejection(sig, decision.reason)
            continue
        self._place(sig, decision.qty)
        self.risk.record_trade()

    self._publish_state()
```

Step by step, every minute:

1. **Time gate.** If weekend → skip. If past 15:15 → square-off path. If before 09:30 or after 14:45 → return (no new entries).
2. **Pick the symbols to evaluate.** The key line is `for sym in self._watchlist()`. That helper has a 3-tier fallback chain:

   ```python
   # bot/executor.py
   def _watchlist(self) -> List[str]:
       picks = todays_picks()
       if picks:
           symbols = [p.symbol for p in picks if p.bias in ("long", "short")]
           if symbols:
               return symbols
       return auto_watchlist() or self.cfg.symbols
   ```

   - **First choice:** today's research picks with `bias=long|short` (filters out `neutral`).
   - **Fallback 1:** the auto-watchlist top-15 (so we still have something to trade if research crashed).
   - **Fallback 2:** the static `watchlist.symbols` from `config.yaml` (last-ditch safety net).
3. **Pull 5-min bars** for each focus symbol via `intraday_bars`. Note: the tick fires every 1 min but the strategies operate on 5-min bars — so 4 out of 5 ticks essentially see the same candle and re-evaluate (cheap).
4. **Run the strategy ensemble.** Each of ORB / VWAP-revert / EMA-Supertrend votes BUY/SELL/HOLD. The ensemble emits a non-HOLD signal only when ≥2 of 3 agree (and, when MTF is on, the 15m frame must confirm).
5. **Publish each signal** to cache as `signal:<symbol>` so the dashboard's "Signals" panel reflects it live.
6. **Mark-to-market.** Last 5-min close gets pushed to the broker (`update_marks`) so paper P&L, SL, and TP tracking reflect the current price.
7. **Risk gate.** For every non-HOLD signal, `RiskManager` checks: daily-loss circuit-breaker, per-trade max-loss → position size, max trades/day, max open positions, kill-switch file. If approved, it returns a `qty` sized so worst-case loss ≤ 1% of `capital.total`.
8. **Place order** via the broker (paper or live), record the fill in `bot/journal.py`, send a fill email, and bump the trade counter.
9. **Publish portfolio snapshot** (`cash`, positions, P&L) to cache → Streamlit reads it on its next refresh.

### How "research → watchlist → trade" assist each other

```text
   ┌─ universe (NIFTY 100, hardcoded) ────────────────────────┐
   │                                                          │
   │  08:00  watchlist_updater                                │
   │   • daily bars + liquidity/trend/momentum/ATR scoring    │
   │   • Top 15 → `auto_watchlist` cache + config.yaml        │
   │                                                          │
   │  08:30  research                                         │
   │   • Each of the 15: gap %, EMA posture, RSI, headlines   │
   │   • LLM/heuristic ranks → top 5 (`research:<date>`)      │
   │                                                          │
   └──────────────────────────────────────────────────────────┘
                       │
                       ▼
   09:30 — 14:45  executor.tick() every minute
        focus = research picks (or auto-watchlist fallback)
        bars  → ensemble → risk → broker → journal → cache
                       │
                       ▼
   14:45  no new entries  ──► positions held
   15:15  square-off all  ──► fills journaled
   15:17  EOD P&L written ──► logs/journal/eod-*.txt
```

In one line: **the watchlist updater changes WHAT we consider; research changes WHICH of those we focus on; the tick changes WHEN we act.** All three are decoupled, all cached, and all replaceable independently.

### Things people often miss

- **Neither research nor watchlist re-runs mid-day.** They are explicitly designed as pre-market passes so stock selection is not chased intraday. If you want a fresh research pass mid-session, run `python -m cli research` from another terminal — the tick will pick up the updated cache on its next minute.
- **Bot started after 09:30?** Whatever is already in `research:<today>` cache is used. If the cache is empty (e.g., Redis was just bounced and the bot has not done its 08:30 run today), the executor falls through to `auto_watchlist()` and finally `cfg.symbols`.
- **`feed.use_websocket: true` (Zerodha live):** the KiteTicker subscribes to the **union** of `cfg.symbols` ∪ `auto_watchlist()` at startup — broader than what we trade — so any of those symbols can stream sub-second ticks. The trade list is still narrowed to research picks during the tick.
- **Between 14:45 and 15:15** the tick returns early (no `update_marks`, no signal evaluation). Open paper positions sit at their last mark; live positions still have their broker-side SL/TP. Everything is force-closed at 15:15 anyway.
- **Stalled `research` cache during testing:** the cache key is `research:YYYY-MM-DD`, so it self-rotates daily — you do not need to clear it manually.

### Verifying the behavior in real time

When you run `python -m cli run --paper`, useful commands in side terminals:

```bash
# minute-by-minute decisions
tail -f logs/bot.log | grep -E '\[risk\]|\[strat\]|tick'

# fills as they happen
python -m cli journal --tail

# what the bot thinks today's picks are right now (read cache)
python -c "from bot.research import todays_picks; [print(p) for p in todays_picks()]"

# current watchlist after auto-update
python -c "from bot.watchlist_updater import auto_watchlist; print(auto_watchlist())"
```

## Keeping the bot alive on macOS

Running an intraday bot on a laptop has one specific failure mode: **macOS
sleeps the system after a short idle period and freezes the Python process
along with it.** When the system wakes, in-memory schedulers can drop the
fire-times they slept through. This actually happened on 2026-04-28 — a
13:59 → 15:37 silent gap meant the 15:15 square-off didn't run until 15:37.

The defense is layered. Stack as many as you can:

### Layer 1 — `caffeinate` wrapper (recommended for everyone)

`caffeinate` is built into macOS. It prevents idle-sleep, disk-sleep, and
system-sleep **only while the bot process is alive**. Use the bundled
launcher script:

```bash
bash scripts/run_bot.sh                 # paper mode
bash scripts/run_bot.sh run --paper     # explicit
```

The script runs `caffeinate -i -m -s -w <bot_pid>` alongside the bot, so the
sleep lock is released the instant you Ctrl+C. No admin password needed; no
state to remember to undo.

To verify it's working in another terminal:

```bash
pmset -g assertions | grep -E 'PreventUserIdleSystemSleep|PreventDiskIdle'
# you should see two lines naming the caffeinate process; before launch, none.
```

### Layer 2 — `pmset` morning wake schedule (handles overnight lid-closed)

If you sometimes shut the lid at night and the Mac is still asleep at 08:00
when the watchlist job is supposed to fire, schedule a daily wake. One-time
setup, requires `sudo`:

```bash
# Wake the Mac (or power on, if shut down with lid open) every weekday at 07:30 IST.
sudo pmset repeat wakeorpoweron MTWRF 07:30:00

# Verify
pmset -g sched
```

Combined with Layer 1, this gives you: "Mac wakes at 07:30 → if `run_bot.sh`
is already running it stays awake; if not, your launchd agent in Layer 3
starts it and immediately holds the sleep lock."

To clear the schedule later: `sudo pmset repeat cancel`.

### Layer 3 — `launchd` LaunchAgent for unattended auto-start

Make the bot start automatically at login (or system boot if you also
configure it as a `LaunchDaemon` instead) and auto-restart on crash. Create
`~/Library/LaunchAgents/com.stockbot.runner.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>                <string>com.stockbot.runner</string>
    <key>WorkingDirectory</key>     <string>/Users/YOU/Documents/RWork/Demos/Stock-Market-Bot</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>scripts/run_bot.sh</string>
    </array>
    <key>RunAtLoad</key>            <true/>
    <key>KeepAlive</key>            <true/>
    <key>StandardOutPath</key>      <string>/Users/YOU/Documents/RWork/Demos/Stock-Market-Bot/logs/launchd-stdout.log</string>
    <key>StandardErrorPath</key>    <string>/Users/YOU/Documents/RWork/Demos/Stock-Market-Bot/logs/launchd-stderr.log</string>
    <!-- Optional: run only on weekdays at 07:45 IST instead of always-on. -->
    <!--
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>45</integer></dict>
        <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>45</integer></dict>
        <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>45</integer></dict>
        <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>45</integer></dict>
        <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>45</integer></dict>
    </array>
    -->
</dict>
</plist>
```

Load and verify:

```bash
launchctl load  ~/Library/LaunchAgents/com.stockbot.runner.plist
launchctl list | grep stockbot          # should print the PID
launchctl unload ~/Library/LaunchAgents/com.stockbot.runner.plist   # to stop
```

### Layer 4 — APScheduler `misfire_grace_time` (already done)

Even with everything above, brief sleep windows can still happen (Power Nap,
hardware glitches). The scheduler in `bot/scheduler.py` registers every job
with `misfire_grace_time` and `coalesce=True`, so when the Mac wakes the most
recent missed run still fires:

| Job          | Grace     | Behaviour after a sleep gap                        |
| ------------ | --------- | -------------------------------------------------- |
| executor_tick | 2 min   | Skip stale ticks → resume on next minute boundary  |
| watchlist_updater | 30 min | Fire on wake if within 30 min of 08:00         |
| pre_market_research | 30 min | Fire on wake if within 30 min of 08:30        |
| end_of_day   | 6 h      | **Always** fire square-off, no matter how late     |
| eod_report   | 6 h      | Same — never miss the daily P&L statement          |

Plus a startup catch-up: if the bot is launched after `square_off` with
positions still open, it closes them and writes the EOD report immediately
instead of waiting until the next trading day.

### Layer 5 — Move to a small cloud VM (production-grade)

For real money this is what you should ultimately do. A `t3.micro` AWS / a
₹400/month Hetzner CX11 / a Raspberry Pi 4 in your home — anything that's
always-on, always on AC power, and has no UI doing App Nap. Identical code,
identical config, just `git clone` and `bash scripts/run_bot.sh`.

### Quick health check

```bash
# Is the bot's sleep lock currently held?
pmset -g assertions | grep -E 'caffeinate|stockbot' && echo "✓ awake-lock active"

# When was the system last asleep?
log show --last 24h --predicate 'eventMessage contains "Sleep"' \
  | grep -E 'Entering|Wake reason' | tail -20
```

## Strategy summary

- **ORB (Opening Range Breakout)** — long on break above first 15-min high; short on break below first 15-min low. Stop at the opposite end of the range.
- **VWAP mean-reversion** — fade extremes when price is far from VWAP **and** RSI confirms (oversold for long, overbought for short).
- **EMA + Supertrend** — long when 9-EMA > 21-EMA **and** Supertrend is bullish; opposite for short.
- **Ensemble** — final signal requires **≥2 of 3** strategies to agree (configurable).

## Risk controls (hard-coded behaviour)

- Daily loss ≥ `max_daily_loss_pct` → bot disables itself for the rest of the day
- Per-trade stop-loss sized so max loss ≤ `max_loss_per_trade_pct` of capital
- No more than `max_trades_per_day` total trades
- No more than `max_open_positions` open at once
- All positions force-closed at `square_off` time
- "Kill switch" file `KILL_SWITCH` in project root → bot exits cleanly

## Going live checklist

- [ ] 2+ weeks of paper trading with positive P&L
- [ ] Backtested on at least 6 months of data
- [ ] Broker API keys registered + algo IDs approved by broker
- [ ] Static IP whitelisted with broker
- [ ] Capital limited to amount you can afford to lose 100% of
- [ ] Phone alerts configured for fills + errors
- [ ] Read every line of `bot/risk.py` and `bot/executor.py`

## License

MIT — but the **risk is entirely yours**. The author is not a SEBI-registered investment adviser.
