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

    # ── Capital (Margin-Based Futures Sizing) ─────────────────
    # Position sizing: margin = balance × fraction, notional = margin × leverage
    initial_capital_usdt: float = 20.0
    max_total_margin_usdt: float = 8.0     # max total MARGIN: ~40% of capital (was 15 – 75%!)
    max_trade_margin_usdt: float = 3.0     # max MARGIN per trade (was 4.0)
    per_trade_fraction: float = 0.06       # 6% of balance as MARGIN per trade (was 8%)

    # ── Risk ───────────────────────────────────────────────────
    leverage: int = 3                       # conservative: lower base leverage for tighter risk
    leverage_high_conviction: int = 5       # high conviction: moderate leverage
    max_leverage_allowed: int = 7           # hard cap: max 7x (was 15x – too aggressive)
    margin_mode: MarginMode = MarginMode.ISOLATED
    # High-conviction: all indicators agree → larger margin allocation + higher leverage
    high_conviction_min_confluence: int = 6
    high_conviction_min_weighted_score: float = 80.0   # 0-100 normalized: stricter threshold
    high_conviction_margin_multiplier: float = 1.5     # 1.5x margin (was 2x – calmer sizing)
    max_margin_high_conviction_usdt: float = 4.0       # max MARGIN for high conviction trades

    # ── Strategy (EMA baseline) ──────────────────────────────
    fast_ema: int = 9
    slow_ema: int = 21
    entry_threshold_bps: float = 40.0      # conservative: higher bar = fewer but better entries
    tp_bps: float = 200.0                  # genis TP: kazananlari kostur (R:R = 2.86)
    sl_bps: float = 70.0                   # daha siki SL: kayiplari kes
    max_hold_minutes: int = 180            # more time for trade to develop
    cooldown_minutes: int = 20             # longer cooldown: avoid revenge trading same coin
    max_open_positions: int = 3            # fewer positions: focus on quality (was 8)
    max_z_score_bps: float = 150.0         # narrower: reject extreme deviations (was 200)

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
    require_higher_tf_alignment: bool = False   # 15m trend bonus olarak kalsin, hard-block yapmasin
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
    min_confluence_score: int = 4            # need 4+ indicators to agree (was 2 – way too loose)
    # When EMA is not required, demand stronger agreement.
    min_confluence_no_ema: int = 5           # need 5+ without EMA anchor (was 3)
    min_weighted_score_no_ema: float = 55.0    # 0-100 normalized: need 55% agreement without EMA anchor
    risk_tight_min_weighted_score: float = 50.0   # TIGHT: only trade if 50%+ indicator agreement
    risk_ultra_min_weighted_score: float = 65.0   # ULTRA_TIGHT: need 65%+ indicator agreement
    require_trend_not_against: bool = False  # False = trend filtresi SHORT'lari engellemsin
    trade_with_trend_only: bool = False      # False = her yonde islem ac, indikatörlere güven
    rsi_full_vote_only: bool = True          # True = RSI sadece acik os/ob'da oy verir, daha temiz sinyal
    min_volume_ratio: float = 0.4  # skip when volume < 40% of recent avg (was 0 = off)
    use_regime_filter: bool = True
    use_breakout_mode: bool = True
    use_orderbook_vote: bool = True
    orderbook_imbalance_long: float = 0.65
    orderbook_imbalance_short: float = 0.35
    use_funding_filter: bool = True
    funding_rate_threshold: float = 0.0003    # more sensitive to funding extremes
    funding_contra_bonus: float = 8.0         # bigger bonus for contrarian funding trades
    # Weights for combined score (normalized to 0-100 at scoring time)
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
    higher_tf_alignment_bonus: float = 15.0  # require_higher_tf off, bonus ile odullendir (was 12)

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

    # Whale Detection
    weight_whale: float = 12.0
    whale_imbalance_threshold: float = 0.65  # >0.65 bid-heavy, <0.35 ask-heavy

    # Liquidation Cascade Detection
    weight_liq_cascade: float = 15.0
    liq_cascade_min_intensity: float = 0.3  # minimum intensity to trigger vote

    # Volume Profile
    weight_volume_profile: float = 10.0

    # Sentiment Analysis
    use_sentiment: bool = True
    weight_sentiment: float = 10.0
    sentiment_threshold: float = 20.0  # min absolute composite score to trigger vote

    # ── Momentum Quality Filter ──────────────────────────────
    require_momentum_confirmation: bool = False  # momentum zaten indikator olarak oy veriyor, cift gate yapma
    momentum_bonus_weight: float = 15.0    # bigger momentum bonus (was 10)
    no_momentum_discount: float = 0.8      # hafif ceza: SHORT'lari cok penalize etmesin

    # ── Session/Funding Time Awareness ────────────────────────
    avoid_funding_window: bool = True
    funding_window_minutes: int = 10  # 10 min yeterli, 30 min gunun %12.5'ini blokluyor

    # ── Correlation Filter ────────────────────────────────────
    use_correlation_filter: bool = True
    max_same_direction_positions: int = 2   # max 2 same-direction positions (was 5)

    # ── Smart Exit ────────────────────────────────────────────
    use_momentum_exit: bool = False          # KAPALI: kazananlari erken kesiyor
    use_time_decay_sl: bool = False          # KAPALI: toparlanma sansi vermiyor
    time_decay_start_pct: float = 0.6
    time_decay_sl_reduction_pct: float = 0.3

    # ── Kelly Criterion Sizing (outputs MARGIN fraction) ──────
    use_kelly_sizing: bool = True
    kelly_fraction: float = 0.3  # quarter-Kelly: more conservative (was 0.5 half-Kelly)
    kelly_min_trades: int = 30   # need more data for reliable stats (was 20)
    kelly_min_fraction: float = 0.02  # minimum 2% of balance as margin
    kelly_max_fraction: float = 0.08  # cap at 8% of balance as margin (was 12%)

    # ── Volatility-Adjusted Sizing (outputs MARGIN amount) ───
    use_volatility_sizing: bool = True
    target_risk_pct: float = 0.015          # 1.5% of balance risked per trade (was 2%)
    volatility_sizing_atr_mult: float = 1.5 # wider ATR buffer for sizing (was 1.2)

    # ── Selector ───────────────────────────────────────────────
    max_spread_bps: float = 15.0   # tight spread: only liquid coins (was 45 – too loose for memecoins)
    min_depth_usdt: float = 5000.0 # serious liquidity required (was 500 – let HIPPO etc through)
    vol_guard_bps: float = 500.0   # reject extreme volatility earlier (was 800)
    shortlist_size: int = 150      # focus on top 150 liquid coins (was 700)
    min_volume_24h_usdt: float = 5_000_000.0  # minimum $5M 24h volume – reject illiquid coins

    # ── Trade Management ───────────────────────────────────────
    use_dynamic_tp_sl: bool = True
    atr_sl_multiplier: float = 1.2         # ATR-based SL: daha siki
    atr_tp_multiplier: float = 3.0         # ATR-based TP: daha genis (R:R = 2.5x ATR)
    min_sl_bps: float = 50.0              # minimum SL floor raised (was 30 – noise territory)
    min_tp_bps: float = 100.0             # minimum TP floor raised (was 60)
    # TP must be at least this many bps above round-trip fees so that at TP we have net profit
    min_tp_net_bps: float = 25.0           # need 25bps net after fees (was 10)
    use_trailing_stop: bool = False          # KAPALI: kazananlari %40'ta kesiyor, TP'ye ulasamiyor
    trailing_activation_pct: float = 0.75   # acilirsa bile %75 TP'de aktif olsun
    trailing_distance_pct: float = 0.3
    use_breakeven_stop: bool = False         # KAPALI: kucuk kari kilitleyip buyuk TP'yi engelliyor
    breakeven_activation_pct: float = 0.7
    breakeven_buffer_bps: float = 10.0
    use_partial_tp: bool = False             # KAPALI: yari kari erken aliyor, kalan BE'de cikiyor
    partial_tp_fraction: float = 0.3
    partial_tp_trigger_pct: float = 0.8

    # ── Dynamic Leverage (conservative tiers) ─────────────────────
    dynamic_leverage_enabled: bool = True
    dyn_leverage_tier1_confluence: int = 4
    dyn_leverage_tier1_weighted_score: float = 55.0   # need 55%+ score for any leverage boost
    dyn_leverage_tier1: int = 4                        # moderate: 4x (was 7x)
    dyn_leverage_tier2_confluence: int = 5
    dyn_leverage_tier2_weighted_score: float = 70.0    # need 70%+ for tier 2
    dyn_leverage_tier2: int = 5                        # moderate: 5x (was 10x)
    dyn_leverage_tier3_confluence: int = 7
    dyn_leverage_tier3_weighted_score: float = 85.0    # need 85%+ for max leverage
    dyn_leverage_tier3: int = 7                        # capped at 7x (was 15x!)

    # ── Anti-Liquidation ───────────────────────────────────────
    anti_liquidation_enabled: bool = True
    liquidation_safety_margin_pct: float = 25.0  # close if price within 25% of liq price
    max_leverage_for_price: bool = True  # auto-reduce leverage if liq price too close

    # ── Risk State Thresholds ────────────────────────────────────
    risk_state_disabled: bool = True   # True = HER ZAMAN NORMAL, TIGHT/ULTRA yok
    risk_consec_losses_tight: int = 4          # escalate faster on losing streaks (was 8)
    risk_drawdown_pct_tight: float = 5.0       # tighter drawdown trigger (was 8%)
    risk_drawdown_min_for_consec_tight: float = 1.5
    risk_api_error_rate_tight: float = 0.3
    risk_consec_losses_ultra: int = 7          # ultra after 7 losses (was 14)
    risk_drawdown_pct_ultra: float = 12.0      # ultra at 12% drawdown (was 18%)
    risk_drawdown_min_for_consec_ultra: float = 3.0
    risk_api_error_rate_ultra: float = 0.5
    risk_consec_wins_recover: int = 3          # need 3 wins to recover (was 2)
    risk_stable_cycles_recover: int = 5        # need 5 stable cycles (was 3)
    risk_max_minutes_in_tight: int = 30        # longer in tight before auto-recovery (was 20)
    risk_max_minutes_in_ultra: int = 60        # longer in ultra (was 45)

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
    swing_max_positions: int = 2           # separate cap from scalp (was 3)
    swing_min_confluence: int = 4          # stricter swing confluence (was 3)
    swing_require_trend_alignment: bool = True  # 4h trend must confirm
    swing_atr_sl_multiplier: float = 1.5
    swing_atr_tp_multiplier: float = 3.0
    swing_cooldown_minutes: int = 30       # longer cooldown for swing
    swing_leverage: int = 3                # lower leverage for swing (longer hold)

    # ── Scheduling ─────────────────────────────────────────────
    scan_interval_minutes: int = 5         # slower scanning: less overtrading (was 3)
    scan_interval_active_minutes: int = 2  # moderate active scan (was 1)
    use_adaptive_scan: bool = True
    universe_refresh_hours: int = 6

    # ── Runtime Safety & Maintenance ───────────────────────────
    live_slippage_guard_bps: float = 20.0   # tighter slippage guard (was 35 – too loose)
    soft_kill_switch_enabled: bool = False   # DISABLED: bot ASLA durmasin
    soft_kill_api_error_rate: float = 0.85
    soft_kill_drawdown_pct: float = 99.0    # pratik olarak devre disi
    soft_kill_min_balance_ratio: float = 0.01  # pratik olarak devre disi
    soft_kill_cooldown_cycles: int = 0      # bekleme yok
    reconcile_interval_cycles: int = 5
    selector_lenient_enabled: bool = False  # DISABLED: don't loosen filters for garbage coins
    selector_lenient_spread_mult: float = 1.25
    selector_lenient_depth_mult: float = 0.75
    selector_lenient_min_tradeable: int = 8

    db_maintenance_interval_cycles: int = 60
    db_vacuum_interval_cycles: int = 240
    db_retention_days: int = 21

    # ── Dashboard (systemd) ────────────────────────────────────
    # Service name for "systemctl is-active" (dashboard shows CALISIYOR/DURDU from this)
    systemd_service_name: str = "mstis"

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
