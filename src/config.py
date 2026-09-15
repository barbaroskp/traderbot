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

    # NOTE: extra="forbid" is deliberate. With extra="ignore" a renamed field
    # (e.g. MAX_TOTAL_MARGIN_USDT -> MAX_TOTAL_MARGIN_PCT) is silently dropped
    # and the default applies, which once let the bot trade at 10x the intended
    # size. Fail loudly instead.
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        extra="forbid",
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
    max_total_margin_pct: float = 0.30      # toplam margin: bakiyenin %30'u
    max_trade_margin_pct: float = 0.10      # tek islem margin: bakiyenin %10'u
    per_trade_fraction: float = 0.05        # %5 bakiye margin olarak kullanilsin

    # ── Risk ───────────────────────────────────────────────────
    # Leverage multiplies BOTH edge and cost. With a per-trade cost of ~20bps of
    # notional, every extra turn of leverage burns balance proportionally faster.
    # Keep it low until an edge is demonstrated out-of-sample.
    leverage: int = 2
    leverage_high_conviction: int = 3
    max_leverage_allowed: int = 3           # hard cap
    margin_mode: MarginMode = MarginMode.ISOLATED
    # High-conviction: all indicators agree → larger margin allocation + higher leverage
    high_conviction_min_confluence: int = 5
    high_conviction_min_weighted_score: float = 65.0
    high_conviction_margin_multiplier: float = 1.25
    max_margin_high_conviction_pct: float = 0.15        # high conviction: bakiyenin %15'i margin

    # ── Strategy (EMA baseline) ──────────────────────────────
    fast_ema: int = 9
    slow_ema: int = 21
    # Trade frequency is the dominant cost driver: every round trip pays
    # ~2*fee + 2*slippage on notional regardless of outcome. These defaults
    # target a low-frequency, high-conviction profile.
    entry_threshold_bps: float = 30.0
    tp_bps: float = 200.0                  # R:R ~2.9 vs sl_bps
    sl_bps: float = 70.0
    max_hold_minutes: int = 180
    cooldown_minutes: int = 60             # per-symbol re-entry cooldown
    max_open_positions: int = 2
    max_z_score_bps: float = 300.0         # above this it is a breakout, not mean reversion

    # ── Multi-Indicator (RSI, MACD, Bollinger) ───────────────
    rsi_period: int = 14
    rsi_oversold: float = 35.0          # genis zone: daha fazla sinyal (25 cok kati, RSI 30-35 de firsat)
    rsi_overbought: float = 65.0        # genis zone: daha fazla sinyal (75 cok kati)
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
    # NOTE: compute_indicators() needs >= 50 CLOSED candles and fetch_* now drops
    # the in-progress candle, so any limit of exactly 50 would silently yield 49
    # and leave higher_tf_trend permanently NEUTRAL. Keep headroom.
    higher_tf_limit: int = 60
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
    require_ema_in_confluence: bool = False   # EMA zorunlu degil: diger indikatorler yeterli (True cok blokluyordu)
    # Mode-aware EMA gate:
    # - None => fallback to require_ema_in_confluence
    # - True/False => explicit behavior per mode
    require_ema_in_mean_reversion: bool | None = None
    require_ema_in_trend_follow: bool | None = None
    require_ema_in_breakout: bool | None = None
    # ── Signal architecture ───────────────────────────────────
    # "thesis": ONE cluster decides the direction and nothing else can flip it;
    #           every other cluster may only CONFIRM or VETO.
    # "vote":   legacy majority-of-clusters behaviour (kept for A/B comparison).
    #
    # Voting is an averaging machine, and averaging is only justified when the
    # voters estimate the SAME quantity with independent noise. Mean reversion
    # ("stretched, will revert") and trend following ("moving, will persist") are
    # two different models of price, not two estimates of one number. Averaging
    # them produces a direction no component actually believes in, and when it
    # loses there is nothing to attribute the loss to. Measured live: clusters
    # disagreed on 95% of symbols, and the old 2-of-6 rule fired a signal on
    # 100% of them — it was not selecting anything.
    signal_mode: Literal["thesis", "vote"] = "thesis"
    primary_thesis: Literal["trend", "mean_revert"] = "trend"
    thesis_min_confirmations: int = 2   # non-thesis clusters that must agree
    thesis_max_vetoes: int = 1          # non-thesis clusters allowed to disagree

    min_confluence_score: int = 3            # legacy: individual indicator count (used by swing)
    # Cluster system: 6 voting clusters. Mean-reverting clusters (oscillator,
    # mean_revert) and the trend cluster are deliberately separated so they can
    # disagree; requiring a majority means we only trade when the two
    # philosophies actually line up (e.g. a pullback inside a trend).
    min_cluster_confluence: int = 4          # 4 of 6 clusters must agree
    # When EMA is not required, demand stronger agreement.
    min_confluence_no_ema: int = 4           # legacy: EMA yoksa 4 indikator
    # weighted_score = agreement quality x cluster breadth, on a 0-100 scale.
    # A 4/6-cluster signal with 70% agreement lands near 47; 6/6 at 90% lands
    # near 90. These thresholds are provisional and should be recalibrated
    # against measured forward returns rather than tuned to hit a trade count.
    min_weighted_score_no_ema: float = 45.0
    risk_tight_min_weighted_score: float = 55.0   # TIGHT: raise the bar, do not lower it
    risk_ultra_min_weighted_score: float = 65.0   # ULTRA_TIGHT: only the very best
    require_trend_not_against: bool = True   # at minimum confluence, do not fight the trend
    trade_with_trend_only: bool = False      # False = allow counter-trend when confluence is high
    rsi_full_vote_only: bool = True          # only vote from genuine overbought/oversold zones
    min_volume_ratio: float = 0.50           # skip dead/illiquid periods
    use_regime_filter: bool = True           # mode-aware entries (mean reversion vs trend vs breakout)
    use_breakout_mode: bool = True
    use_orderbook_vote: bool = True
    orderbook_imbalance_long: float = 0.65
    orderbook_imbalance_short: float = 0.35
    use_funding_filter: bool = False  # KAPALI: funding rate filtresi cok sinyal engelliyor
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
    stoch_rsi_oversold: float = 25.0   # biraz genis: daha fazla sinyal
    stoch_rsi_overbought: float = 75.0  # biraz genis: daha fazla sinyal
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
    avoid_funding_window: bool = False  # KAPALI: funding window filtresi gereksiz, sinyal kaybettiriyor
    funding_window_minutes: int = 5   # kapali ama yine de dusuk tut

    # ── Correlation Filter ────────────────────────────────────
    use_correlation_filter: bool = True
    max_same_direction_positions: int = 3   # max 3 ayni yon pozisyon (2 cok kisitlayiciydi)

    # ── Smart Exit ────────────────────────────────────────────
    use_momentum_exit: bool = False          # KAPALI: kazananlari erken kesiyor
    use_time_decay_sl: bool = False          # KAPALI: toparlanma sansi vermiyor
    time_decay_start_pct: float = 0.6
    time_decay_sl_reduction_pct: float = 0.3

    # ── Kelly Criterion Sizing (outputs MARGIN fraction) ──────
    use_kelly_sizing: bool = True
    kelly_fraction: float = 0.25  # quarter-Kelly
    kelly_min_trades: int = 30    # need enough closed trades for a usable estimate
    # A floor > 0 defeats the whole point of Kelly: with a proven negative edge
    # the optimal bet is ZERO, and a floor forces the bot to keep betting while
    # its own statistics say it is losing.
    kelly_min_fraction: float = 0.0
    kelly_max_fraction: float = 0.10
    kelly_block_on_negative_edge: bool = True  # stop opening trades when Kelly <= 0

    # ── Volatility-Adjusted Sizing (outputs MARGIN amount) ───
    use_volatility_sizing: bool = True
    target_risk_pct: float = 0.03            # %3 risk per trade (1.5% cok konservatif, buyuyemiyor)
    volatility_sizing_atr_mult: float = 1.5 # wider ATR buffer for sizing (was 1.2)

    # ── Selector ───────────────────────────────────────────────
    # Spread IS slippage: a market order crosses roughly half the spread on each
    # side. A 30bps spread cap therefore permitted ~30bps of round-trip
    # slippage, more than the entire expected edge. Keep the universe small and
    # liquid; scanning 300 symbols for "whatever agrees right now" is multiple
    # hypothesis testing, not selection.
    max_spread_bps: float = 8.0
    min_depth_usdt: float = 25_000.0
    vol_guard_bps: float = 600.0
    shortlist_size: int = 25
    min_volume_24h_usdt: float = 50_000_000.0

    # ── Trade Management ───────────────────────────────────────
    use_dynamic_tp_sl: bool = True
    atr_sl_multiplier: float = 1.2         # ATR-based SL: daha siki
    atr_tp_multiplier: float = 3.0         # ATR-based TP: daha genis (R:R = 2.5x ATR)
    min_sl_bps: float = 50.0              # minimum SL floor raised (was 30 – noise territory)
    min_tp_bps: float = 100.0             # minimum TP floor raised (was 60)
    # TP must clear the FULL round-trip cost (fees + slippage) by this margin,
    # otherwise a "winning" trade still books a loss after friction.
    min_tp_net_bps: float = 40.0
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
    # Disabled by default: leverage was being driven by weighted_score, and that
    # score used to REWARD thin evidence (see strategy._normalize_weighted_score).
    # Scaling leverage off a conviction metric is only safe once that metric is
    # validated out-of-sample.
    dynamic_leverage_enabled: bool = False
    dyn_leverage_tier1_confluence: int = 4
    dyn_leverage_tier1_weighted_score: float = 45.0
    dyn_leverage_tier1: int = 2
    dyn_leverage_tier2_confluence: int = 5
    dyn_leverage_tier2_weighted_score: float = 55.0
    dyn_leverage_tier2: int = 2
    dyn_leverage_tier3_confluence: int = 6
    dyn_leverage_tier3_weighted_score: float = 70.0
    dyn_leverage_tier3: int = 3

    # ── Anti-Liquidation ───────────────────────────────────────
    anti_liquidation_enabled: bool = True
    liquidation_safety_margin_pct: float = 25.0  # close if price within 25% of liq price
    max_leverage_for_price: bool = True  # auto-reduce leverage if liq price too close

    # ── Risk State Thresholds ────────────────────────────────────
    risk_state_disabled: bool = False  # keep the de-risking ladder active
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
    swing_trend_limit: int = 60            # >50 after the in-progress candle is dropped
    swing_scan_every_n_cycles: int = 5     # run swing every N scalp cycles (~15 min)
    swing_tp_bps: float = 300.0            # 3% take profit
    swing_sl_bps: float = 150.0            # 1.5% stop loss
    swing_max_hold_minutes: int = 1440     # 24 hours
    swing_max_positions: int = 2           # separate cap from scalp (was 3)
    swing_min_confluence: int = 3          # 3 yeterli swing icin (4 cok katiydi)
    swing_require_trend_alignment: bool = False  # trend zorunlu degil: daha fazla swing firsati
    swing_atr_sl_multiplier: float = 1.5
    swing_atr_tp_multiplier: float = 3.0
    swing_cooldown_minutes: int = 30       # longer cooldown for swing
    swing_leverage: int = 3                # lower leverage for swing (longer hold)

    # ── Scheduling ─────────────────────────────────────────────
    # Signals are computed from CLOSED 5m candles, so scanning faster than the
    # candle interval re-evaluates identical data and only adds API load.
    scan_interval_minutes: int = 5
    scan_interval_active_minutes: int = 5
    use_adaptive_scan: bool = True
    universe_refresh_hours: int = 6

    # ── Runtime Safety & Maintenance ───────────────────────────
    live_slippage_guard_bps: float = 10.0
    # A kill switch that never fires is not a kill switch. "Never stop trading"
    # is what turned a drawdown into a wipeout.
    soft_kill_switch_enabled: bool = True
    soft_kill_api_error_rate: float = 0.50
    soft_kill_drawdown_pct: float = 15.0
    soft_kill_min_balance_ratio: float = 0.70
    soft_kill_cooldown_cycles: int = 20
    reconcile_interval_cycles: int = 5
    # Lenient fallback loosened spread/depth limits whenever the filters found
    # nothing. If nothing passes, the correct action is to not trade.
    selector_lenient_enabled: bool = False
    selector_lenient_spread_mult: float = 1.25
    selector_lenient_depth_mult: float = 0.75
    selector_lenient_min_tradeable: int = 8

    db_maintenance_interval_cycles: int = 60
    db_vacuum_interval_cycles: int = 240
    db_retention_days: int = 21

    # ── Dashboard (systemd) ────────────────────────────────────
    # Service name for "systemctl is-active" (dashboard shows CALISIYOR/DURDU from this)
    systemd_service_name: str = "mstis"

    # ── Allocation engine (src/allocator.py) ──────────────────
    # "SYM,SYM" for equal weight, or "SYM:0.7,SYM:0.3" for explicit weights.
    #
    # The default is deliberately boring. Measured over two years:
    #   BTC only              +42.0%   max drawdown -53.8%
    #   BTC+ETH 50/50         +28.2%                -61.1%
    #   6 "majors"            +58.6%                -63.7%
    #   12 coins              +13.5%                -69.8%
    #
    # The 6-major basket looks best — but I chose those six AFTER seeing which
    # ones did well. It contains XRP (+160%) and TRX (+122%) and excludes DOT
    # (-77%) and AVAX (-65%). That is hindsight, not a strategy, and twelve
    # mechanical selection rules (momentum and contrarian, 30/90/180-day
    # lookbacks, top 3 or 5) ALL failed to beat holding everything.
    #
    # BTC needed no foresight to pick in 2024 and had both the best return and
    # the smallest drawdown of the honest choices. ETH is here for
    # diversification, which the backtest did not reward but which does not
    # depend on this one sample being representative.
    allocation_basket: str = "BTC-USDT:0.7,ETH-USDT:0.3"
    # Calendar cadence. Monthly beat weekly in test; more often just pays fees.
    rebalance_days: int = 30
    # Relative drift from target weight that forces an off-schedule rebalance.
    # Deliberately WIDE, and that is a finding too. Reacting to sharp moves is
    # intuitive but measured monotonically worse the tighter the trigger:
    #   off +55.5% | 75% +54.3% | 50% +52.2% | 40% +50.0% | 30% +47.7%
    # Extra rebalancing in a trending market trims winners early. The monthly
    # cadence already harvests divergence; this trigger exists only so a single
    # asset cannot balloon to a dominant weight between rebalances. It is a
    # concentration guard, not a return driver.
    rebalance_drift_pct: float = 75.0
    # Portfolio drawdown brake. DISABLED BY DEFAULT — and that default is a
    # finding, not an oversight.
    #
    # An early version looked spectacular (+96.6% vs +43.5% buy-and-hold) but
    # that was a bug: re-entry was tested against portfolio equity, which does
    # not move while the book sits in stables, so the brake was a one-way door.
    # It sold once near the top and never returned. Once re-entry was fixed to
    # read market prices, the same settings returned +13.5% and did not even
    # reduce max drawdown (-67% vs -64% unbraked) — it whipsawed, selling dips
    # and buying back 10% higher, 162 trades instead of 92.
    #
    # A 12-cell sweep of (brake, re-entry) then produced results scattered from
    # +7.2% to +100% with neighbouring cells wildly different, which is the
    # signature of noise rather than a stable effect. What IS consistent: a
    # brake reduces losses in bad periods and costs return in good ones. It is
    # a risk-reduction tool, not a return enhancer.
    #
    # Set it if you want to cap drawdown and accept lower expected return. Do
    # NOT tune the number against a backtest — the sweep above shows that
    # selects noise.
    max_portfolio_drawdown_pct: float = 0.0
    # Rise off the MARKET low required before redeploying out of stables.
    reentry_recovery_pct: float = 25.0
    # Trades smaller than this cost more in fees and min-size rejections than
    # the tracking error they remove.
    min_rebalance_trade_quote: float = 10.0

    # ── Database ───────────────────────────────────────────────
    db_path: str = "data/bingx_agent.db"

    # ── Logging ────────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_file: str = "data/bingx_agent.log"

    # ── Fees & funding ────────────────────────────────────────
    # BingX perpetual taker fee is ~0.05% = 5bps. Entries are MARKET orders and
    # SL/TP are STOP_MARKET / TAKE_PROFIT_MARKET, so BOTH sides are taker.
    # The old 4bps default understated cost on every trade, backtest and paper run.
    fee_rate_bps: float = 5.0
    # Slippage actually paid per side when crossing the book with a market order.
    slippage_assumption_bps: float = 8.0
    # Funding is paid every funding_interval_hours while a position is open.
    # Longs pay when the rate is positive, shorts receive (and vice versa).
    # This was previously not deducted anywhere: not in paper, live or backtest.
    use_funding_cost: bool = True
    funding_interval_hours: float = 8.0
    # Fallback rate used when no live funding rate is available (e.g. backtest).
    default_funding_rate_bps: float = 1.0
    # Spread the backtest assumes, since it has no historical order book. Keep
    # it consistent with the pairs max_spread_bps actually admits — a hardcoded
    # optimistic value here makes backtest exits cheaper than live ones.
    backtest_assumed_spread_bps: float = 6.0

    # Expectancy gate. Given a TP/SL pair and the round-trip cost, the win rate
    # needed just to break even is net_sl / (net_tp + net_sl). If a setup needs a
    # higher win rate than this to be worth taking, skip it. The old guard only
    # checked that R:R >= 1.0, which says nothing about expectancy: a 1:1 setup
    # at a 50% win rate loses exactly the friction on every trade.
    expectancy_max_required_win_rate: float = 0.50

    @property
    def round_trip_cost_bps(self) -> float:
        """Total expected friction for one open+close cycle, in bps of notional."""
        return 2.0 * self.fee_rate_bps + 2.0 * self.slippage_assumption_bps

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
