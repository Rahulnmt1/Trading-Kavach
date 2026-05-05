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
    # Floor the SL premium at this fraction of the entry premium so a
    # single fast spike against us doesn't take 90%+ of premium before
    # the trail can engage. 0.30 = max 70% premium loss per trade.
    min_sl_premium_pct: float = 0.30


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
