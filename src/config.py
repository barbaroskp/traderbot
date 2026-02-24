"""Centralised configuration via pydantic-settings.

All values are read from environment / .env file.
Immutable after construction – pass ``cfg`` around, never mutate.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MarginMode(str, Enum):
    ISOLATED = "ISOLATED"
    CROSS = "CROSS"


class Settings(BaseSettings):
    """Application-wide configuration – single source of truth."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── BingX API ──────────────────────────────────────────────
    bingx_api_key: str = ""
    bingx_api_secret: str = ""
    bingx_base_url: str = "https://open-api.bingx.com"

    # ── Mode ───────────────────────────────────────────────────
    paper_mode: bool = True
    allow_live_trading: bool = False

    # ── Capital ────────────────────────────────────────────────
    initial_capital_usdt: float = 50.0
    max_total_notional_usdt: float = 30.0   # aggressive: doubled (was 15)
    max_trade_notional_usdt: float = 8.0    # aggressive: bigger trades (was 5)
    per_trade_fraction: float = 0.08        # aggressive: 8% per trade (was 5%)

    # ── Risk ───────────────────────────────────────────────────
    leverage: int = 5                       # aggressive: 5x base (was 2)
    leverage_high_conviction: int = 10      # aggressive: 10x for best signals (was 5)
    max_leverage_allowed: int = 15          # aggressive: allow up to 15x (was 5)
    margin_mode: MarginMode = MarginMode.ISOLATED
    # High-conviction: all indicators agree → larger position + higher leverage
    high_conviction_min_confluence: int = 5
    high_conviction_min_weighted_score: float = 70.0   # slightly lower bar (was 75)
    high_conviction_size_multiplier: float = 2.0       # aggressive: 2x size (was 1.5)
    max_trade_notional_high_conviction_usdt: float = 15.0  # bigger cap (was 10)

    # ── Strategy (EMA baseline) ──────────────────────────────
    fast_ema: int = 9
    slow_ema: int = 21
    entry_threshold_bps: float = 25.0      # aggressive: lower bar = more entries (was 30)
    tp_bps: float = 120.0                  # aggressive: wider TP = let winners run (was 100)
    sl_bps: float = 50.0
    max_hold_minutes: int = 120            # aggressive: faster rotation (was 180)
    cooldown_minutes: int = 6              # aggressive: faster re-entry (was 12)
    max_open_positions: int = 8            # aggressive: more simultaneous positions (was 5)
    max_z_score_bps: float = 200.0         # aggressive: wider range for entries (was 150)

    # ── Multi-Indicator (RSI, MACD, Bollinger) ───────────────
    rsi_period: int = 14
    rsi_oversold: float = 25.0          # stricter: only clear oversold counts
    rsi_overbought: float = 75.0        # stricter: only clear overbought counts
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    macd_crossover_boost: float = 1.3
    macd_weakening_multiplier: float = 0.5
    bollinger_period: int = 20
    bollinger_std: float = 2.0
    ema_trend: int = 50  # trend EMA for direction filter
    kline_interval: str = "5m"
    kline_limit: int = 100
    higher_tf_interval: str = "15m"
    higher_tf_limit: int = 50
    use_higher_tf_trend: bool = True
    require_higher_tf_alignment: bool = False
    atr_period: int = 14
    adx_period: int = 14
    adx_trend_threshold: float = 25.0
    adx_strong_threshold: float = 40.0
    bb_squeeze_threshold: float = 0.02
    breakout_bb_pct_high: float = 0.98
    breakout_bb_pct_low: float = 0.02
    volume_spike_ratio: float = 2.0

    # ── Signal Confluence ────────────────────────────────────
    # Legacy switch kept for backward compatibility with existing .env files.
    # Mode-aware switches below override this when set.
    require_ema_in_confluence: bool = True
    # Mode-aware EMA gate:
    # - None => fallback to require_ema_in_confluence
    # - True/False => explicit behavior per mode
    require_ema_in_mean_reversion: bool | None = None
    require_ema_in_trend_follow: bool | None = None
    require_ema_in_breakout: bool | None = None
    min_confluence_score: int = 2
    # When EMA is not required, demand stronger agreement.
    min_confluence_no_ema: int = 3
    min_weighted_score_no_ema: float = 70.0
    risk_tight_min_weighted_score: float = 50.0   # TIGHT: 4/5 ok if score >= this
    risk_ultra_min_weighted_score: float = 62.0   # ULTRA_TIGHT: 4/5 ok if score >= this
    require_trend_not_against: bool = False  # False = trende ters de gir (daha fazla islem)
    trade_with_trend_only: bool = False  # False = trende ters de girebilir (daha fazla islem)
    rsi_full_vote_only: bool = False     # False = RSI orta bolgede de hafif oy verir, daha fazla 3/5 confluence
    min_volume_ratio: float = 0.0  # 0 = off; e.g. 0.3 = skip when volume < 30% of recent avg
    use_regime_filter: bool = True
    use_breakout_mode: bool = True
    use_orderbook_vote: bool = True
    orderbook_imbalance_long: float = 0.65
    orderbook_imbalance_short: float = 0.35
    use_funding_filter: bool = True
    funding_rate_threshold: float = 0.0005
    funding_contra_bonus: float = 5.0
    # Weights for combined score (total = 100)
    weight_ema: float = 25.0
    weight_rsi: float = 25.0
    weight_macd: float = 20.0
    weight_bollinger: float = 15.0
    weight_trend: float = 15.0
    weight_orderbook: float = 10.0
    weight_breakout: float = 20.0
    weight_volume_spike: float = 5.0
    weight_vwap: float = 10.0
    weight_momentum: float = 10.0
    weight_stoch_rsi: float = 15.0
    weight_adx: float = 10.0
    stoch_rsi_oversold: float = 20.0
    stoch_rsi_overbought: float = 80.0
    higher_tf_alignment_bonus: float = 15.0  # weighted score bonus when 15m trend confirms signal

    # ── New Indicators (Tier 1+2) ─────────────────────────────
    # Taker Buy/Sell Ratio (proxy from candle direction)
    weight_taker_ratio: float = 15.0
    taker_buy_ratio_long: float = 0.58   # >58% taker buys = bullish
    taker_buy_ratio_short: float = 0.42  # <42% taker buys = bearish
    taker_ratio_period: int = 20

    # OBV (On-Balance Volume)
    weight_obv: float = 10.0

    # Williams %R
    weight_williams_r: float = 10.0
    williams_r_period: int = 14
    williams_r_oversold: float = -80.0   # below = oversold -> LONG
    williams_r_overbought: float = -20.0 # above = overbought -> SHORT

    # TTM Squeeze (Keltner Channels + Bollinger)
    weight_squeeze: float = 15.0
    keltner_period: int = 20
    keltner_atr_mult: float = 1.5

    # Open Interest
    use_open_interest: bool = True
    weight_oi: float = 15.0
    oi_change_threshold_pct: float = 3.0  # % OI change that triggers a vote

    # Price Velocity / Acceleration
    weight_velocity: float = 5.0
    velocity_lookback: int = 5
    velocity_threshold_bps: float = 30.0  # min velocity bps/bar to count

    # ── Momentum Quality Filter ──────────────────────────────
    require_momentum_confirmation: bool = True
    momentum_bonus_weight: float = 10.0
    no_momentum_discount: float = 0.7  # weighted score multiplier when momentum missing

    # ── Session/Funding Time Awareness ────────────────────────
    avoid_funding_window: bool = True
    funding_window_minutes: int = 15  # aggressive: tighter window (was 30)

    # ── Correlation Filter ────────────────────────────────────
    use_correlation_filter: bool = True
    max_same_direction_positions: int = 5   # aggressive: was 3

    # ── Smart Exit ────────────────────────────────────────────
    use_momentum_exit: bool = True
    use_time_decay_sl: bool = True
    time_decay_start_pct: float = 0.4       # aggressive: start tightening earlier (was 0.5)
    time_decay_sl_reduction_pct: float = 0.5

    # ── Kelly Criterion Sizing ──────────────────────────────────
    use_kelly_sizing: bool = True
    kelly_fraction: float = 0.5  # half-Kelly (safer than full Kelly)
    kelly_min_trades: int = 20   # need at least this many closed trades for reliable stats
    kelly_min_fraction: float = 0.02  # minimum 2% even if Kelly says less
    kelly_max_fraction: float = 0.12  # cap at 12% even if Kelly says more

    # ── Volatility-Adjusted Sizing ────────────────────────────
    use_volatility_sizing: bool = True
    target_risk_pct: float = 0.02           # aggressive: 2% risk per trade (was 1%)
    volatility_sizing_atr_mult: float = 1.2 # aggressive: less conservative sizing (was 1.5)

    # ── Selector ───────────────────────────────────────────────
    max_spread_bps: float = 45.0   # 25 cok sikti (425 sembol eleniyordu), 45 = daha fazla tradeable
    min_depth_usdt: float = 500.0
    vol_guard_bps: float = 800.0
    shortlist_size: int = 700

    # ── Trade Management ───────────────────────────────────────
    use_dynamic_tp_sl: bool = True
    atr_sl_multiplier: float = 1.0
    atr_tp_multiplier: float = 2.0
    min_sl_bps: float = 30.0
    min_tp_bps: float = 60.0
    use_trailing_stop: bool = True
    trailing_activation_pct: float = 0.4   # fraction of TP to activate trailing
    trailing_distance_pct: float = 0.6     # fraction of SL distance for trail
    use_breakeven_stop: bool = True
    breakeven_activation_pct: float = 0.3
    breakeven_buffer_bps: float = 2.0
    use_partial_tp: bool = True
    partial_tp_fraction: float = 0.5
    partial_tp_trigger_pct: float = 0.5

    # ── Dynamic Leverage (aggressive tiers) ─────────────────────
    dynamic_leverage_enabled: bool = True
    dyn_leverage_tier1_confluence: int = 3
    dyn_leverage_tier1_weighted_score: float = 45.0   # aggressive: lower bar (was 50)
    dyn_leverage_tier1: int = 7                        # aggressive: 7x (was 5)
    dyn_leverage_tier2_confluence: int = 4
    dyn_leverage_tier2_weighted_score: float = 60.0    # aggressive: (was 65)
    dyn_leverage_tier2: int = 10                       # aggressive: 10x (was 7)
    dyn_leverage_tier3_confluence: int = 5
    dyn_leverage_tier3_weighted_score: float = 75.0    # aggressive: (was 80)
    dyn_leverage_tier3: int = 15                       # aggressive: 15x (was 10)

    # ── Anti-Liquidation ───────────────────────────────────────
    anti_liquidation_enabled: bool = True
    liquidation_safety_margin_pct: float = 25.0  # close if price within 25% of liq price
    max_leverage_for_price: bool = True  # auto-reduce leverage if liq price too close

    # ── Risk State Thresholds ────────────────────────────────────
    risk_consec_losses_tight: int = 8
    risk_drawdown_pct_tight: float = 8.0
    risk_drawdown_min_for_consec_tight: float = 2.0
    risk_api_error_rate_tight: float = 0.3
    risk_consec_losses_ultra: int = 14
    risk_drawdown_pct_ultra: float = 18.0
    risk_drawdown_min_for_consec_ultra: float = 5.0
    risk_api_error_rate_ultra: float = 0.5
    risk_consec_wins_recover: int = 2
    risk_stable_cycles_recover: int = 3
    risk_max_minutes_in_tight: int = 20
    risk_max_minutes_in_ultra: int = 45

    # ── Swing Trading (1h candles, wider TP/SL) ────────────────
    swing_enabled: bool = True
    swing_interval: str = "1h"
    swing_kline_limit: int = 100
    swing_trend_interval: str = "4h"       # trend filter timeframe
    swing_trend_limit: int = 50
    swing_scan_every_n_cycles: int = 5     # run swing every N scalp cycles (~15 min)
    swing_tp_bps: float = 300.0            # 3% take profit
    swing_sl_bps: float = 150.0            # 1.5% stop loss
    swing_max_hold_minutes: int = 1440     # 24 hours
    swing_max_positions: int = 3           # separate cap from scalp
    swing_min_confluence: int = 3
    swing_require_trend_alignment: bool = True  # 4h trend must confirm
    swing_atr_sl_multiplier: float = 1.5
    swing_atr_tp_multiplier: float = 3.0
    swing_cooldown_minutes: int = 30       # longer cooldown for swing
    swing_leverage: int = 3                # lower leverage for swing (longer hold)

    # ── Scheduling ─────────────────────────────────────────────
    scan_interval_minutes: int = 3
    scan_interval_active_minutes: int = 1  # faster scan when positions are open
    use_adaptive_scan: bool = True
    universe_refresh_hours: int = 6

    # ── Runtime Safety & Maintenance ───────────────────────────
    live_slippage_guard_bps: float = 35.0
    soft_kill_switch_enabled: bool = True
    soft_kill_api_error_rate: float = 0.85
    soft_kill_drawdown_pct: float = 35.0
    soft_kill_min_balance_ratio: float = 0.15
    soft_kill_cooldown_cycles: int = 2
    reconcile_interval_cycles: int = 5
    selector_lenient_enabled: bool = True
    selector_lenient_spread_mult: float = 1.25
    selector_lenient_depth_mult: float = 0.75
    selector_lenient_min_tradeable: int = 8

    db_maintenance_interval_cycles: int = 60
    db_vacuum_interval_cycles: int = 240
    db_retention_days: int = 21

    # ── Dashboard (systemd) ────────────────────────────────────
    # Service name for "systemctl is-active" (dashboard shows CALISIYOR/DURDU from this)
    systemd_service_name: str = "bingx-agent"

    # ── Database ───────────────────────────────────────────────
    db_path: str = "data/bingx_agent.db"

    # ── Logging ────────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_file: str = "data/bingx_agent.log"

    # ── Fees ──────────────────────────────────────────────────
    fee_rate_bps: float = 4.0  # default 4bps; set via FEE_RATE_BPS env var

    # ── Paper sim ──────────────────────────────────────────────
    slippage_assumption_bps: float = 3.0

    # ── LLM Advisor (optional) ─────────────────────────────────
    # When enabled, local Ollama model reviews each signal before execution (approve/reject/reduce)
    use_llm_advisor: bool = False
    ollama_model: str = "llama3.2:3b"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_timeout_seconds: float = 8.0
    ollama_max_calls_per_cycle: int = 3  # limit so cycle doesn't wait too long

    # ── Derived helpers ────────────────────────────────────────
    @property
    def db_full_path(self) -> Path:
        return Path(self.db_path)

    @property
    def log_full_path(self) -> Path:
        return Path(self.log_file)

    @field_validator("leverage")
    @classmethod
    def _leverage_range(cls, v: int) -> int:
        if v < 1 or v > 125:
            raise ValueError("leverage must be between 1 and 125")
        return v

    @field_validator("leverage_high_conviction")
    @classmethod
    def _leverage_high_range(cls, v: int) -> int:
        if v < 1 or v > 125:
            raise ValueError("leverage_high_conviction must be between 1 and 125")
        return v

    def is_live(self) -> bool:
        return not self.paper_mode and self.allow_live_trading

    def validate_live_ready(self) -> list[str]:
        """Return list of issues preventing live trading."""
        issues: list[str] = []
        if not self.bingx_api_key:
            issues.append("BINGX_API_KEY is empty")
        if not self.bingx_api_secret:
            issues.append("BINGX_API_SECRET is empty")
        if not self.allow_live_trading:
            issues.append("ALLOW_LIVE_TRADING is false")
        if self.paper_mode:
            issues.append("PAPER_MODE is true – set to false for live")
        return issues


def load_config() -> Settings:
    """Factory – creates a validated config instance."""
    return Settings()
