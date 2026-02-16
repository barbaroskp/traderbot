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
        env_file=".env",
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
    max_total_notional_usdt: float = 15.0
    max_trade_notional_usdt: float = 5.0
    per_trade_fraction: float = 0.05

    # ── Risk ───────────────────────────────────────────────────
    leverage: int = 2
    leverage_high_conviction: int = 5   # when 5/5 confluence + high score, use this leverage
    max_leverage_allowed: int = 5
    margin_mode: MarginMode = MarginMode.ISOLATED
    # High-conviction: all indicators agree → larger position + higher leverage
    high_conviction_min_confluence: int = 5
    high_conviction_min_weighted_score: float = 75.0
    high_conviction_size_multiplier: float = 1.5   # 1.5x position size when high conviction
    max_trade_notional_high_conviction_usdt: float = 10.0   # cap per trade when high conviction

    # ── Strategy (EMA baseline) ──────────────────────────────
    fast_ema: int = 9
    slow_ema: int = 21
    entry_threshold_bps: float = 30.0   # 30 bps = more EMA votes, more confluence (40 was too few signals)
    tp_bps: float = 100.0               # let winners run (2:1 R:R with 50 bps SL)
    sl_bps: float = 50.0
    max_hold_minutes: int = 180
    cooldown_minutes: int = 12          # slightly longer cooldown = less overtrading
    max_open_positions: int = 5
    max_z_score_bps: float = 150.0     # skip if |z| > this (breakout, not mean reversion)

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
    # When require_ema_in_confluence=True: EMA must vote (LONG/SHORT) + at least 1 other indicator agree
    require_ema_in_confluence: bool = True   # True = EMA zorunlu, uzerine en az 1 oy daha
    min_confluence_score: int = 2   # used only when require_ema_in_confluence=False
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

    # ── Dynamic Leverage ───────────────────────────────────────
    dynamic_leverage_enabled: bool = True
    dyn_leverage_tier1_confluence: int = 3
    dyn_leverage_tier1_weighted_score: float = 50.0
    dyn_leverage_tier1: int = 5
    dyn_leverage_tier2_confluence: int = 4
    dyn_leverage_tier2_weighted_score: float = 65.0
    dyn_leverage_tier2: int = 7
    dyn_leverage_tier3_confluence: int = 5
    dyn_leverage_tier3_weighted_score: float = 80.0
    dyn_leverage_tier3: int = 10

    # ── Scheduling ─────────────────────────────────────────────
    scan_interval_minutes: int = 3
    universe_refresh_hours: int = 6

    # ── Dashboard (systemd) ────────────────────────────────────
    # Service name for "systemctl is-active" (dashboard shows CALISIYOR/DURDU from this)
    systemd_service_name: str = "bingx-agent"

    # ── Database ───────────────────────────────────────────────
    db_path: str = "data/bingx_agent.db"

    # ── Logging ────────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_file: str = "data/bingx_agent.log"

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
