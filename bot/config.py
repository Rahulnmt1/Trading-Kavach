"""Loads runtime configuration from `.env` (secrets) and `config.yaml` (strategy params)."""
from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


class EnvSettings(BaseSettings):
    """Secrets and runtime toggles, loaded from environment."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    LIVE_TRADING: bool = False
    BROKER: str = "paper"

    # FIX #36 (2026-05-16) — Pluggable market-data source.
    # Selects which backend serves bot.data.intraday_bars and history:
    #   yfinance  (default — legacy free Yahoo scrape)
    #   dhan      (free with Dhan trading account, no daily login)
    #   auto      (try dhan first, fall back to yfinance)
    # Empty / unset = yfinance (preserves pre-Phase-5 behaviour).
    DATA_SOURCE: str = ""

    KITE_API_KEY: Optional[str] = None
    KITE_API_SECRET: Optional[str] = None
    KITE_ACCESS_TOKEN: Optional[str] = None
    KITE_USER_ID: Optional[str] = None
    KITE_PASSWORD: Optional[str] = None
    KITE_TOTP_SECRET: Optional[str] = None

    DHAN_CLIENT_ID: Optional[str] = None
    DHAN_ACCESS_TOKEN: Optional[str] = None

    REDIS_URL: str = "redis://localhost:6379/0"

    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: str = "gpt-4o-mini"
    NEWS_FEEDS: str = ""

    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 587
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_FROM: Optional[str] = None
    NOTIFY_TO: str = ""
    NOTIFY_LEVEL: str = "INFO"

    LOG_LEVEL: str = "INFO"
    TZ: str = "Asia/Kolkata"

    @property
    def news_feed_list(self) -> List[str]:
        return [u.strip() for u in self.NEWS_FEEDS.split(",") if u.strip()]

    @property
    def notify_recipients(self) -> List[str]:
        return [a.strip() for a in self.NOTIFY_TO.split(",") if a.strip()]


# ---------- YAML config ----------

class CapitalCfg(BaseModel):
    total: float = 100_000
    deployable_pct: float = 0.8


class RiskCfg(BaseModel):
    max_daily_loss_pct: float = 2.0
    max_loss_per_trade_pct: float = 1.0
    max_trades_per_day: int = 5
    max_open_positions: int = 3
    max_position_pct: float = 25.0
    reward_risk_ratio: float = 1.5
    trailing_stop: bool = True
    sl_atr_mult: float = 1.0
    tp_atr_mult: float = 1.5
    # Lock in profits: when realized + unrealized P&L for the day reaches
    # `daily_profit_target_pct` of capital, square off everything and
    # halt new entries for the rest of the session. Set to 0 to disable.
    daily_profit_target_pct: float = 0.0
    # Trailing stop: once price has moved >= `trail_activation_r` × R in the
    # winning direction, the stop is moved to lock in `trail_lock_r` × R of
    # profit and then trailed using `trail_atr_mult` × ATR.
    trail_activation_r: float = 1.0
    trail_lock_r: float = 0.5
    trail_atr_mult: float = 1.0

    # FIX #37 (2026-05-16) — Fee-aware entry gate.
    #
    # Refuses any trade whose expected round-trip fees (entry + exit at
    # take-profit) exceed this percentage of the expected gross profit.
    # Uses bot.fees.roundtrip_breakdown to compute realistic fees for
    # the actual segment (equity / futures / options) — so the gate
    # respects the FIX #21r STT rates, the flat ₹20 brokerage, GST,
    # exchange/SEBI/stamp charges.
    #
    # Why this matters: option-buy strategies in particular can fire
    # signals where the SL/TP envelope is tight relative to the premium,
    # producing an expected gross of ₹500-1500 against ~₹100-150 of
    # round-trip fees on a single NIFTY lot. That's 10-30% drag —
    # absorbing a string of breakeven trades grinds capital down even
    # when "win-rate" looks fine.
    #
    # 25% threshold rationale: a strategy with 50% win-rate and 1.5
    # reward/risk needs gross-fee-drag below ~30% to clear
    # the breakeven (calculated against 60-day backtest distributions).
    # 25% leaves a small safety margin while letting through the bulk
    # of historical trades. Adjust UP to be more permissive,
    # DOWN to be stricter. Set to 0 / 100+ to disable the gate.
    #
    # Spreads and iron-condors are EXEMPT (defined-risk credit
    # strategies have a different P&L mechanic — gross is the credit
    # collected, not the TP/SL distance — and the fee gate's TP-based
    # gross calculation doesn't apply directly).
    max_fee_pct_of_gross: float = 25.0


class SessionCfg(BaseModel):
    market_open: str = "09:15"
    trade_start: str = "09:30"
    trade_cutoff: str = "14:45"
    square_off: str = "15:15"
    timezone: str = "Asia/Kolkata"

    def t(self, key: str) -> time:
        h, m = map(int, getattr(self, key).split(":"))
        return time(h, m)


class ORBCfg(BaseModel):
    range_minutes: int = 15
    breakout_buffer_pct: float = 0.05
    # ``HH:MM`` IST. ORB will return HOLD for any bar whose timestamp
    # is at or after this time. Default 11:30 reflects the empirical
    # observation that opening-range breakouts have follow-through edge
    # mostly in the first ~2 hours of the session; later "breakouts" of
    # the morning range are typically late moves that mean-revert by
    # square-off. Set to "13:30" (= ``session.trade_cutoff``) to disable
    # this gate and let ORB fire any time during the trading window.
    entry_cutoff: str = "11:30"


class VWAPRevertCfg(BaseModel):
    deviation_pct: float = 0.6
    rsi_overbought: float = 70
    rsi_oversold: float = 30


class EMASupertrendCfg(BaseModel):
    ema_fast: int = 9
    ema_slow: int = 21
    supertrend_period: int = 10
    supertrend_multiplier: float = 3.0


class FuturesTrendCfg(BaseModel):
    """Phase-2 F&O index futures trend-following strategy.

    Operates on 5m bars of the underlying spot (paper-mode proxy via
    yfinance) or futures bars (live via Kite, Phase 5). Wider EMA and
    ATR multipliers than the equity ``EMA_Supertrend`` strategy because
    index futures have larger per-bar ranges in absolute points.
    """
    ema_fast: int = 20
    ema_slow: int = 50
    sl_atr_mult: float = 2.0
    tp_atr_mult: float = 3.0
    # Require the EMA-fast/slow crossover to be within this many bars to
    # avoid chasing a trend that's already played out.
    cross_lookback_bars: int = 5


class CreditSpreadCfg(BaseModel):
    """Phase-4 vertical credit spread strategy.

    Same EMA20/EMA50 cross trigger as ``futures_trend`` and
    ``option_buy_directional``, but expresses the directional view by
    SELLING a defined-risk vertical credit spread:

      * Bullish cross → BULL PUT spread (sell ATM PE / buy lower PE)
      * Bearish cross → BEAR CALL spread (sell ATM CE / buy higher CE)

    Defined-risk economics:
      max_loss/share = strike_width − net_credit_collected
      max_gain/share = net_credit_collected
      margin/lot     = max_loss/share × lot_size

    For NIFTY ATM weekly with strike_width=100 and ~₹70 net credit,
    margin per lot is ~₹2,250 — fits the ₹50K F&O budget comfortably.
    """
    ema_fast: int = 20
    ema_slow: int = 50
    cross_lookback_bars: int = 5
    # Strike-width in points between short and long legs.
    # NIFTY: 50/100/200 are typical (we default 100 for a balance of
    # credit vs margin). BANKNIFTY: 200/300/500 (default 200).
    strike_width: int = 100
    # IV / risk-free for paper-mode BS pricing of both legs.
    iv: float = 0.15
    risk_free_rate: float = 0.07
    # Profit lock: close when net price decays to (1 - x) × entry credit.
    # 0.50 means "lock in 50% of the credit" — the canonical
    # intraday-credit-spread target.
    profit_lock_pct: float = 0.50
    # Loss cap: close when net price rises to entry_credit + x × max_loss.
    # 0.70 means "stop out at 70% of structural max loss" — wider than
    # 50% to give the position room to breathe through chop.
    sl_max_loss_pct: float = 0.70


class IronCondorCfg(BaseModel):
    """Phase-4.5 iron-condor (4-leg neutral defined-risk) strategy.

    Triggered by EMA *convergence* (the absence of a fresh cross), the
    inverse of ``futures_trend`` / ``credit_spread`` / ``option_buy``.
    When |fast_ema − slow_ema| / spot drops below ``ema_flat_threshold``,
    the trend has rolled over flat and theta-collection becomes
    favourable — we sell a delta-neutral iron condor.

    Defined-risk economics:
      max_gain/share = net_credit_collected
      max_loss/share = max(put_width, call_width) − net_credit
      margin/lot     = max_loss/share × lot_size

    For NIFTY ATM weekly with put_width=call_width=100, wings_distance=100,
    and ~₹70 net credit, margin per lot is ~₹2,250 (same as a single
    100-pt vertical) but the win zone is far wider (spot can drift ±100
    points from ATM and we still keep all the credit).
    """
    ema_fast: int = 20
    ema_slow: int = 50
    # Trend-flatness gauge: |fast - slow| / spot. Below this fraction
    # we treat the regime as consolidation. 0.30% is mild — equivalent
    # to a 60-point spread on a 20K NIFTY. Tighten for picky entries,
    # widen to fire more often.
    ema_flat_threshold: float = 0.0030
    # Strike-width parameters for the two protective wings. Set
    # asymmetric to express a slight directional skew (e.g. wider call
    # wing for a slightly bearish lean).
    put_width: int = 100              # short-put → long-put distance
    call_width: int = 100             # short-call → long-call distance
    # Distance from spot to the SHORT strikes (the inner legs that
    # collect the bulk of the credit). Larger values = lower credit but
    # wider win zone. Default 100 (~50 points each side of ATM after
    # rounding to NIFTY's 50-pt strike grid).
    wings_distance: int = 100
    # IV / risk-free for paper-mode BS pricing of all four legs.
    iv: float = 0.15
    risk_free_rate: float = 0.07
    # Profit lock: close when net price decays to (1 - x) × entry credit.
    # 0.50 = lock 50% of credit (canonical IC target — most theta has
    # already been collected by the time the IC has decayed to half its
    # entry price).
    profit_lock_pct: float = 0.50
    # Loss cap: close when net price rises to entry_credit + x × max_loss.
    # 0.70 = stop at 70% of structural max — wider than 50% to give the
    # position room to breathe through chop, since IC max-loss only
    # occurs in the unlikely tail-event of spot expiring beyond a wing.
    sl_max_loss_pct: float = 0.70


class OptionBuyDirectionalCfg(BaseModel):
    """Phase-3 F&O directional option-buying strategy.

    Same EMA20/EMA50 crossover trigger as :class:`FuturesTrendCfg` but
    expresses the directional view by buying an at-the-money option
    rather than the future:

      * Bullish cross → buy ATM CE
      * Bearish cross → buy ATM PE

    SL and TP are computed in the **underlying** ATR space, then
    translated to **option-premium** space via Black-Scholes (since the
    option premium moves non-linearly in the underlying).

    Why option-buying first? It has CAPPED downside (max loss = full
    premium paid) and works in the small-capital regime our
    ``fno.capital.total = ₹50,000`` default sits in. Option-selling
    (Phase 4) needs margin and can have unlimited loss.
    """
    ema_fast: int = 20
    ema_slow: int = 50
    sl_atr_mult: float = 2.0          # SL distance in spot ATR
    tp_atr_mult: float = 3.0          # TP distance in spot ATR
    cross_lookback_bars: int = 5
    # Black-Scholes assumptions for paper-mode premium synthesis. These
    # are constants per ``DEFAULT_IV`` / ``DEFAULT_RISK_FREE`` in
    # ``bot/options/pricing.py`` and exposed here for per-segment
    # tweaking (e.g. set higher iv on volatile expiry day).
    iv: float = 0.15                   # Annualised volatility, decimal
    risk_free_rate: float = 0.07       # Annualised, decimal
    # Floor the SL premium at this fraction of the entry premium —
    # caps the maximum allowed premium drop before SL fires.
    #
    # FIX #31 (2026-05-15): raised 0.30 → 0.65 in response to the
    # BANKNIFTY26MAY54200CE -₹22,711 disaster. The old 0.30 floor
    # allowed up to 70% premium loss per trade, well above the 1.6%
    # per-trade-loss cap when scaled by lot size. With 0.65 floor the
    # worst-case premium drop is 35%, which on a ₹62K capital-at-risk
    # trade caps loss at ~₹22K — still hefty, but bounded. Combined
    # with FIX #29 (positions are now managed all the way to
    # square_off so SL actually triggers) and FIX #30 (daily kill
    # switch force-closes the book at -2% daily P&L), the realistic
    # worst single-trade loss is the per-trade-loss cap (~₹8K on
    # ₹500K F&O capital).
    #
    # If the BS-derived SL is already tighter (smaller drop) than this
    # floor, the BS value is used — the floor only applies as a
    # CAP on how loose the SL can be.
    min_sl_premium_pct: float = 0.65

    # FIX #32 (2026-05-15) — Volatility-regime filter ("theta-trap"
    # avoidance).
    #
    # Long-option buyers need a TRENDING underlying to overcome theta.
    # If realised vol is low (chop / range-bound regime), theta wins
    # even if the EMA cross fires. Today's BANKNIFTY 13:11 entry was
    # exactly this scenario — realised 1h vol was 9.32% (annualised),
    # the lowest of any signal event in the journal. Spot then chopped
    # within a 1.1% range and theta + adverse delta cratered the
    # premium 36% by EOD square-off (₹698 → ₹448 = -₹22,711.61 net).
    #
    # Empirical calibration (signal-fire-moment realised vol over 1h):
    #
    #   2026-05-15 13:11  BANKNIFTY  9.32-10.77% →  -₹22,711  (CATASTROPHE)
    #   2026-05-15 11:26  BANKNIFTY 14.07%       →   -₹7,431  (loss bounded by SL)
    #   2026-05-14 12:09  NIFTY     14.11%       →   +₹3,061  (TP)
    #   2026-05-14 09:43  NIFTY     25.28%       →   +₹9,208  (TP)
    #   2026-05-14 09:30  NIFTY     26.22%       →   -₹5,602  (SL)
    #   2026-05-08 09:41  NIFTY     22.51%       →   +₹5,266  (lockin)
    #
    # 12% floor cleanly skips the catastrophic 13:11 entry (well below
    # threshold) while preserving every TP winner above 14%. A 10%
    # floor was tested first but the realised-vol estimate fluctuates
    # ~±1% across yfinance fetches, so 12% gives proper safety margin.
    #
    # Set to 0.0 to disable the filter (returns to pre-FIX-#32 behaviour).
    min_realized_vol_pct: float = 0.12
    realized_vol_lookback_bars: int = 12      # 12 × 5m = 1h of bars

    # FIX #33 (2026-05-15) — RSI extreme filter ("buying-the-top" guard).
    #
    # Long-option entries on a momentum cross are vulnerable to
    # mean-reversion when the underlying has already had a strong
    # run-up. Today's 13:11 BANKNIFTY entry: RSI(14) on 5m = 67.8 —
    # well into "near-overbought" territory after a 200-pt rally
    # over the prior hour. The EMA20/50 cross was a LATE momentum
    # signal that fired right at the local top; spot then reverted
    # 500+ points over the next 2h.
    #
    # Filter: refuse a long CE entry when RSI > ``rsi_overbought``
    # (default 65) and a long PE entry when RSI < ``rsi_oversold``
    # (default 35). This is the canonical institutional
    # mean-reversion safeguard — used by every desk that does
    # systematic options buying.
    #
    # Set both to 0/100 to disable.
    rsi_period: int = 14
    rsi_overbought: float = 65.0    # Block CE buys when RSI ≥ this
    rsi_oversold: float = 35.0      # Block PE buys when RSI ≤ this

    # FIX #34 (2026-05-15) — Bollinger %B mean-reversion filter.
    #
    # %B = (price - lower_band) / (upper_band - lower_band). Values
    # near 1.0 mean price is hugging the upper Bollinger Band — i.e.,
    # ~2σ above the 20-period MA. Buying calls at the upper band is
    # a classic "fading the obvious" mistake; the band is a
    # statistical resistance that mean-reverters short.
    #
    # Today's 13:11 BANKNIFTY entry: %B = 90% (close ₹54,246 vs upper
    # ₹54,283, lower ₹53,909). Strong BLOCK signal under any
    # reasonable threshold.
    #
    # Filter: refuse CE buy when %B > ``bb_upper_threshold`` (default
    # 0.85), refuse PE buy when %B < ``bb_lower_threshold`` (default
    # 0.15). Combined with the RSI filter above, we have two
    # independent mean-reversion guards that AGREE on today's entry.
    #
    # Set to 1.0/0.0 to disable.
    bb_period: int = 20
    bb_std: float = 2.0
    bb_upper_threshold: float = 0.85   # Block CE buys when %B ≥ this
    bb_lower_threshold: float = 0.15   # Block PE buys when %B ≤ this

    # FIX #35 (2026-05-15) — Multi-source price validation.
    #
    # Before placing a trade we cross-check the yfinance spot used
    # by the strategy against NSE's free public REST endpoint. If
    # the two sources disagree by more than this percentage we
    # refuse the trade — symptom of a yfinance bad-tick / stale
    # cache / aggregator glitch.
    #
    # Threshold rationale: NSE pricing is the authoritative source.
    # Normal scrape lag between NSE and yfinance is <0.05% (a few
    # basis points on indices). 1.0% gives huge safety margin
    # against false positives while still catching the rare
    # multi-percent divergence that signals a real data problem.
    #
    # Set to 0.0 to disable. The check is fail-open: when NSE is
    # unreachable (network blip, rate-limit) the trade proceeds
    # on yfinance alone.
    multisource_max_divergence_pct: float = 1.0


class EnsembleCfg(BaseModel):
    min_agree: int = 2


class MultiTimeframeCfg(BaseModel):
    enabled: bool = False
    base_interval: str = "5m"
    confirm_interval: str = "15m"


class StrategiesCfg(BaseModel):
    enabled: List[str] = Field(default_factory=lambda: ["orb", "vwap_revert", "ema_supertrend"])
    ensemble: EnsembleCfg = EnsembleCfg()
    multitimeframe: MultiTimeframeCfg = MultiTimeframeCfg()
    orb: ORBCfg = ORBCfg()
    vwap_revert: VWAPRevertCfg = VWAPRevertCfg()
    ema_supertrend: EMASupertrendCfg = EMASupertrendCfg()
    # F&O-specific sub-configs. These are only consulted when the segment
    # is FNO; they live on the shared StrategiesCfg so the YAML schema
    # stays uniform between segments.
    futures_trend: FuturesTrendCfg = FuturesTrendCfg()
    option_buy_directional: OptionBuyDirectionalCfg = OptionBuyDirectionalCfg()
    credit_spread: CreditSpreadCfg = CreditSpreadCfg()
    iron_condor: IronCondorCfg = IronCondorCfg()


class ResearchCfg(BaseModel):
    enabled: bool = True
    run_at: str = "08:30"
    top_n: int = 5


class WatchlistUpdaterCfg(BaseModel):
    enabled: bool = False
    run_at: str = "08:00"
    top_n: int = 15
    min_avg_volume: int = 500_000
    trend_lookback_days: int = 20
    momentum_lookback_days: int = 10
    # NOTE: the auto-watchlist updater ALWAYS writes the freshly-selected
    # symbols back to `config.yaml` (under `watchlist.symbols`). This is
    # mandatory by design so that `config.yaml` is always the source of truth
    # for "what the bot is currently trading", surviving Redis flushes and
    # process restarts. The previous `write_back_to_yaml` toggle has been
    # removed for that reason.


class ExecutionCfg(BaseModel):
    order_type: str = "MARKET"
    product: str = "MIS"
    slippage_bps: float = 5
    # NOTE: paper-trading starting cash is intentionally NOT a separate field.
    # It is derived from `capital.total` so the YAML stays the single source
    # of truth. A previous `paper_starting_cash` field has been removed.


class FeedCfg(BaseModel):
    use_websocket: bool = True
    poll_interval_seconds: int = 60


class WalkForwardCfg(BaseModel):
    enabled: bool = False
    is_window_days: int = 60
    oos_window_days: int = 20
    step_days: int = 20


class BacktestCfg(BaseModel):
    walk_forward: WalkForwardCfg = WalkForwardCfg()


class LoggingCfg(BaseModel):
    to_file: bool = True
    dir: str = "logs"


class FnoCfg(BaseModel):
    """Optional Futures & Options segment configuration.

    When present in ``config.yaml`` under the top-level ``fno:`` key, this
    block configures the F&O segment which runs in its own process,
    holds its own capital budget, and writes to its own Redis namespace.
    The top-level ``capital`` / ``risk`` / ``watchlist`` / ``strategies``
    keys remain the EQUITY segment's config — F&O does NOT inherit them.

    Set ``enabled: false`` (the default if omitted) to disable F&O entirely
    so ``python -m cli run --segment fno`` exits with a clear error.
    """

    enabled: bool = False
    capital: CapitalCfg = CapitalCfg()
    risk: RiskCfg = RiskCfg()
    # Watchlist for F&O is a list of underlyings (NIFTY, BANKNIFTY, ...).
    # Once Phase 2 lands the symbols here will be expanded into concrete
    # tradingsymbols (futures / option strikes) by the F&O instrument
    # resolver. For Phase 1 the list is stored as-is.
    watchlist: dict = Field(default_factory=lambda: {"symbols": []})
    # Strategy list for F&O is intentionally separate from the equity
    # one — they target different instruments and use different signals.
    # Phase 1 ships an empty `enabled` list (no strategies registered);
    # Phase 2 will add ``futures_trend`` and ``options_directional`` etc.
    strategies: StrategiesCfg = StrategiesCfg(enabled=[])
    # How many calendar days BEFORE monthly expiry to roll positions to
    # the next contract. Default 2 = roll on expiry-week Tuesday — matches
    # how active futures traders avoid the dying contract's vanishing
    # liquidity and basis-collapse volatility on expiry day. Set to 0 to
    # keep contracts until the day AFTER expiry (legacy behaviour).
    rollover_buffer_days: int = 2


class MarketDataCfg(BaseModel):
    """FIX #36 — Pluggable market-data source selection.

    The selected ``source`` decides which backend serves
    :func:`bot.data.intraday_bars` and :func:`bot.data.history`:

      * ``yfinance`` — free Yahoo scrape (default; preserves
        pre-Phase-5 behaviour).
      * ``dhan``     — Dhan REST + WebSocket (free with a Dhan
        trading account, no daily login). Falls back to yfinance
        when credentials are missing or the upstream is down.
      * ``auto``     — try Dhan first, fall back to yfinance.

    Env var ``DATA_SOURCE`` overrides this YAML setting when set.
    """
    source:   str = "yfinance"
    fallback: str = "yfinance"


class AppConfig(BaseModel):
    capital: CapitalCfg = CapitalCfg()
    risk: RiskCfg = RiskCfg()
    session: SessionCfg = SessionCfg()
    watchlist: dict = Field(default_factory=lambda: {"symbols": []})
    strategies: StrategiesCfg = StrategiesCfg()
    research: ResearchCfg = ResearchCfg()
    watchlist_updater: WatchlistUpdaterCfg = WatchlistUpdaterCfg()
    execution: ExecutionCfg = ExecutionCfg()
    feed: FeedCfg = FeedCfg()
    backtest: BacktestCfg = BacktestCfg()
    logging: LoggingCfg = LoggingCfg()
    market_data: MarketDataCfg = MarketDataCfg()
    # Optional F&O segment block. ``None`` means "F&O is disabled" — the
    # equity behaviour above is fully backward-compatible without it.
    fno: Optional[FnoCfg] = None

    @property
    def symbols(self) -> List[str]:
        return list(self.watchlist.get("symbols", []))


@lru_cache(maxsize=1)
def load_config() -> AppConfig:
    cfg_path = PROJECT_ROOT / "config.yaml"
    if not cfg_path.exists():
        return AppConfig()
    with cfg_path.open() as fh:
        raw = yaml.safe_load(fh) or {}
    return AppConfig(**raw)


@lru_cache(maxsize=1)
def env() -> EnvSettings:
    return EnvSettings()
