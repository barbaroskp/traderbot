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
    max_total_margin_pct: float = 0.80      # toplam margin: bakiyenin %80'i (sabit 8$ degil!)
    max_trade_margin_pct: float = 0.20      # tek islem margin: bakiyenin %20'si (sabit 3$ degil!)
    per_trade_fraction: float = 0.15        # %15 bakiye margin olarak kullanilsin (6% cok dusuktu)

    # ── Risk ───────────────────────────────────────────────────
    leverage: int = 3                       # conservative: lower base leverage for tighter risk
    leverage_high_conviction: int = 5       # high conviction: moderate leverage
    max_leverage_allowed: int = 7           # hard cap: max 7x (was 15x – too aggressive)
    margin_mode: MarginMode = MarginMode.ISOLATED
    # High-conviction: all indicators agree → larger margin allocation + higher leverage
    high_conviction_min_confluence: int = 5
    high_conviction_min_weighted_score: float = 60.0   # 0-100 normalized: daha ulasabilir esik
    high_conviction_margin_multiplier: float = 1.5     # 1.5x margin (was 2x – calmer sizing)
    max_margin_high_conviction_pct: float = 0.30        # high conviction: bakiyenin %30'u margin (sabit 4$ degil)

    # ── Strategy (EMA baseline) ──────────────────────────────
    fast_ema: int = 9
    slow_ema: int = 21
    entry_threshold_bps: float = 15.0      # dusuk esik: daha fazla coin sinyal uretsin (40 cok katiydi)
    tp_bps: float = 150.0                  # agresif: daha hizli kar al (200 cok uzak, cogu vurmuyordu)
    sl_bps: float = 60.0                   # agresif: siki SL kayiplari hemen kes (R:R = 2.5)
    max_hold_minutes: int = 180            # more time for trade to develop
    cooldown_minutes: int = 7              # agresif: firsatlari kacirma (10 hala uzun)
    max_open_positions: int = 7            # agresif: daha fazla cesitlilik ve firsat (5 az)
    max_z_score_bps: float = 300.0         # genis: breakout firsatlarini da yakala (150 cok daraldi)

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
    require_ema_in_confluence: bool = False   # EMA zorunlu degil: diger indikatorler yeterli (True cok blokluyordu)
    # Mode-aware EMA gate:
    # - None => fallback to require_ema_in_confluence
    # - True/False => explicit behavior per mode
    require_ema_in_mean_reversion: bool | None = None
    require_ema_in_trend_follow: bool | None = None
    require_ema_in_breakout: bool | None = None
    min_confluence_score: int = 3            # 3 indikator yeterli (4 cok katiydi, sinyal uretmiyordu)
    # When EMA is not required, demand stronger agreement.
    min_confluence_no_ema: int = 3           # agresif: 3 yeterli, confidence tier ile kontrol et (4 cok yuksekti)
    min_weighted_score_no_ema: float = 30.0    # agresif: %30 (35 gereksiz engelliyor, tier ile kontrol)
    risk_tight_min_weighted_score: float = 35.0   # TIGHT: biraz daha secici
    risk_ultra_min_weighted_score: float = 45.0   # ULTRA_TIGHT: secici ama hala islem ac
    require_trend_not_against: bool = False  # False = trend filtresi SHORT'lari engellemsin
    trade_with_trend_only: bool = False      # False = her yonde islem ac, indikatörlere güven
    rsi_full_vote_only: bool = False         # False = RSI gecis bolgelerinde de oy verir (True cok kati, sinyal olmuyor)
    min_volume_ratio: float = 0.15  # dusuk volume da kabul et: gece/dusuk likidite donemlerinde islem acabilsin
    use_regime_filter: bool = False  # KAPALI: TREND_FOLLOW/BREAKOUT_WATCH modlari sinyalleri cok engelliyor
    use_breakout_mode: bool = True
    use_orderbook_vote: bool = True
    orderbook_imbalance_long: float = 0.65
    orderbook_imbalance_short: float = 0.35
    use_funding_filter: bool = False  # KAPALI: funding rate filtresi cok sinyal engelliyor
    funding_rate_threshold: float = 0.0003    # more sensitive to funding extremes
    funding_contra_bonus: float = 8.0         # bigger bonus for contrarian funding trades
    # ── Indicator Simplification ─────────────────────────────
    # 22 indikator var ama ~7 bagimsiz bilgi kaynagi. Sadece bagimsiz olanlari kullan.
    # Aktif olmayanlar hesaplanir ama oy sayisina/agirliga katilmaz.
    active_indicators: str = "ema_zscore,rsi,macd,bollinger,adx,orderbook,obv,vwap,funding_rate,multi_tf,vol_regime,orderflow"

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

    # ── Alpha Signals (Faz 2) ────────────────────────────────
    # Funding Rate Mean Reversion: extreme funding → contrarian trade
    weight_funding_rate: float = 15.0

    # Multi-TF Momentum Alignment: higher TF trend as proper vote
    weight_multi_tf: float = 15.0

    # Volatility Regime: squeeze breakout detection
    weight_vol_regime: float = 12.0

    # Volatility Regime SL/TP adaptation
    use_vol_regime_sl_tp: bool = True
    vol_low_atr_bps: float = 30.0       # ATR < 30bps = low vol
    vol_high_atr_bps: float = 120.0     # ATR > 120bps = high vol
    vol_low_sl_mult: float = 0.7        # low vol → tighter SL
    vol_low_tp_mult: float = 0.7        # low vol → tighter TP
    vol_high_sl_mult: float = 1.5       # high vol → wider SL
    vol_high_tp_mult: float = 1.3       # high vol → wider TP (not as much as SL)

    # ── Faz 3: Adaptive Quality + Anti-Manipulation ──────────

    # Adaptive Quality Gate: min weighted_score otomatik ayarlanir
    use_adaptive_quality: bool = True
    adaptive_quality_lookback: int = 50     # son 50 trade'e bak
    adaptive_quality_min_trades: int = 10   # min 10 trade olana kadar default kullan
    adaptive_quality_base_score: float = 40.0   # baslangic min score
    adaptive_quality_win_adjust: float = -2.0   # win streak → daha agresif (score -2)
    adaptive_quality_loss_adjust: float = 0.0   # loss streak'te esik yukseltme YOK — ayni kaliteyle devam
    adaptive_quality_max_score: float = 70.0    # max eşik (cok kisitlama)
    adaptive_quality_min_score: float = 25.0    # min esik (her zaman biraz filtre)

    # Anti-Manipulation Detection
    use_anti_manipulation: bool = True
    wick_ratio_threshold: float = 3.0     # wick/body > 3 = manipulation sinyali
    wick_lookback_bars: int = 3           # son 3 bar'a bak
    rapid_reversal_bps: float = 50.0      # 50bps+ ani reversal = stop hunt olabilir
    manipulation_cooldown_bars: int = 5   # manipulation sonrasi 5 bar bekle

    # Session-Aware Trading (UTC saatleri)
    use_session_awareness: bool = True
    session_asian_start: int = 0          # 00:00 UTC (Tokyo 09:00)
    session_asian_end: int = 8            # 08:00 UTC
    session_eu_start: int = 7             # 07:00 UTC (London 08:00)
    session_eu_end: int = 16              # 16:00 UTC
    session_us_start: int = 13            # 13:00 UTC (NY 09:00)
    session_us_end: int = 22              # 22:00 UTC
    session_dead_zone_start: int = 22     # 22:00-00:00 UTC = dusuk likidite
    session_dead_zone_end: int = 0
    session_dead_zone_size_mult: float = 1.0  # dead zone'da da tam boyut — pozisyon daraltma yok
    session_overlap_bonus_score: float = 5.0  # EU/US overlap bonus (13-16 UTC)

    # Smart Cooldown: kaybedilen pair'de daha uzun, kazanılan pair'de kisaltilmis
    use_smart_cooldown: bool = True
    smart_cooldown_loss_multiplier: float = 2.0  # loss → cooldown 2x
    smart_cooldown_win_multiplier: float = 0.5   # win → cooldown 0.5x
    smart_cooldown_streak_cap: int = 3            # max 3x cooldown

    # Execution Quality Tracking
    use_execution_tracking: bool = True
    max_acceptable_slippage_bps: float = 15.0     # 15bps ustu slippage → uyar
    slippage_reject_threshold_bps: float = 30.0   # 30bps ustu → reject signal for this pair

    # ── Faz 4: Drawdown-Based Leverage Scaling ─────────────
    use_drawdown_leverage_scaling: bool = False   # KAPALI: kayiptan sonra leverage azaltma — mantik dogruysa tam boyutla devam
    drawdown_leverage_start_pct: float = 5.0
    drawdown_leverage_full_pct: float = 15.0
    drawdown_leverage_min_mult: float = 0.4

    # ── Faz 4: Portfolio Heat Monitor ──────────────────────
    use_portfolio_heat: bool = True
    portfolio_heat_max_pct: float = 90.0         # max portfolio risk: bakiyenin %90'i (genis)
    portfolio_heat_reduce_at_pct: float = 75.0   # %75'te kucult (eskisi %40 cok erken daraliyordu)
    portfolio_heat_reduce_mult: float = 0.75     # %75 boyut (eskisi %50 cok agresif azaltiyordu)

    # ── Faz 5: Order Flow Imbalance Signal ─────────────────
    weight_orderflow: float = 12.0
    orderflow_strong_threshold: float = 0.65     # bid_depth / total_depth > 0.65 → LONG
    orderflow_weak_threshold: float = 0.35       # bid_depth / total_depth < 0.35 → SHORT

    # ── Faz 5: Liquidity-Adjusted Sizing ───────────────────
    use_liquidity_sizing: bool = True
    liquidity_sizing_max_pct: float = 2.0        # max %2 of visible depth
    liquidity_sizing_depth_floor_usdt: float = 3000.0  # derinlik alt sinir

    # ── Faz 6: Rolling Sharpe Tracking ─────────────────────
    use_rolling_sharpe: bool = False             # KAPALI: Sharpe bazli pozisyon daraltma/durdurma yok
    rolling_sharpe_lookback: int = 50
    rolling_sharpe_min_trades: int = 15
    rolling_sharpe_pause_threshold: float = -0.5
    rolling_sharpe_reduce_threshold: float = 0.0

    # ── Faz 6: Strategy Decay Detection ────────────────────
    use_decay_detection: bool = False             # KAPALI: strateji decay'de pozisyon daraltma yok
    decay_lookback_recent: int = 20
    decay_lookback_baseline: int = 100
    decay_min_trades: int = 30
    decay_winrate_drop_pct: float = 15.0
    decay_pf_drop_pct: float = 30.0
    decay_action: str = "reduce"
    decay_size_mult: float = 0.5

    # ── Expectancy-Based Signal Scoring ──────────────────────
    use_expectancy_scoring: bool = True
    expectancy_lookback: int = 200          # son 200 islem
    expectancy_min_samples: int = 5         # pattern basina minimum ornek
    expectancy_boost_pct: float = 15.0      # iyi pattern'a +15 skor
    expectancy_penalty_pct: float = 10.0    # kotu pattern'a -10 skor

    # ── Signal Confidence Tiers ────────────────────────────
    use_confidence_tiers: bool = True
    confidence_tier_high_score: float = 70.0    # 70+ = yuksek guven
    confidence_tier_mid_score: float = 50.0     # 50-70 = orta guven
    confidence_tier_high_mult: float = 1.5      # yuksek guven: %50 daha buyuk pozisyon
    confidence_tier_mid_mult: float = 1.0       # orta guven: normal
    confidence_tier_low_mult: float = 0.6       # dusuk guven: %40 kucuk pozisyon

    # ── Momentum Quality Filter ──────────────────────────────
    require_momentum_confirmation: bool = False  # momentum zaten indikator olarak oy veriyor, cift gate yapma
    momentum_bonus_weight: float = 15.0    # bigger momentum bonus (was 10)
    no_momentum_discount: float = 0.8      # hafif ceza: SHORT'lari cok penalize etmesin

    # ── Session/Funding Time Awareness ────────────────────────
    avoid_funding_window: bool = False  # KAPALI: funding window filtresi gereksiz, sinyal kaybettiriyor
    funding_window_minutes: int = 5   # kapali ama yine de dusuk tut

    # ── Correlation Filter ────────────────────────────────────
    use_correlation_filter: bool = True
    max_same_direction_positions: int = 5   # agresif: 5 ayni yon (7 pozisyon, 5'e kadar ayni yon ok)

    # ── Smart Exit ────────────────────────────────────────────
    use_momentum_exit: bool = True           # AKTIF: sadece zarardaki pozisyonlarda MACD cross tetikler
    momentum_exit_min_hold_pct: float = 0.25  # pozisyon max_hold'un %25'ini doldurmadan exit yapma (gurultu filtresi)
    momentum_exit_min_loss_bps: float = 15.0  # en az 15bps zararda olmalikayip yoksa momentum exit yapma
    use_time_decay_sl: bool = True           # AKTIF: zarardaki eski pozisyonlarda SL'yi sik (kaybi sinirla)
    time_decay_start_pct: float = 0.65       # max_hold'un %65'inden sonra baslat (toparlanma sansi ver)
    time_decay_sl_reduction_pct: float = 0.25  # SL'yi en fazla %25 daraltan daha konservatif)
    # ── Profit Lock ──────────────────────────────────────────
    use_profit_lock: bool = True             # AKTIF: kar koruma — TP'nin %60'ina ulasinca SL'yi entry'ye cek
    profit_lock_activation_pct: float = 0.60  # TP'nin %60'i karlaninca aktif
    profit_lock_buffer_bps: float = 5.0       # entry + 5bps (komisyon ustu kucuk kar garanti)

    # ── Tiered TP (Kademeli Kar Al) ─────────────────────────
    # 3 kademeli cikis: hizli kar al + kazanani kostur + trailing
    use_tiered_tp: bool = True               # ANA SWITCH: kademeli TP aktif
    tiered_tp1_fraction: float = 0.40        # %40 pozisyon: ilk hedefte kapat
    tiered_tp1_ratio: float = 0.50           # TP1 = toplam TP'nin %50'si (hizli kar)
    tiered_tp2_fraction: float = 0.30        # %30 pozisyon: ikinci hedefte kapat
    tiered_tp2_ratio: float = 1.00           # TP2 = toplam TP'nin %100'u (tam hedef)
    tiered_tp3_trailing: bool = True          # %30 kalan: trailing stop ile takip
    tiered_tp3_trail_bps: float = 40.0       # trailing mesafe: 40bps (ATR'ye de uyarlanir)
    tiered_tp3_activation_bps: float = 100.0 # trailing aktif: en az 100bps karda olunca
    tiered_move_sl_after_tp1: bool = True    # TP1 sonrasi SL'yi entry'ye cek (risksiz)

    # ── SL Randomization (Stop Hunting Korumasi) ────────────
    use_sl_randomization: bool = True         # SL'ye rastgele offset ekle
    sl_random_min_bps: float = 3.0           # minimum offset: 3bps
    sl_random_max_bps: float = 12.0          # maximum offset: 12bps (SL'yi biraz genislet)

    # ── Daily Loss Circuit Breaker ──────────────────────────
    use_daily_loss_limit: bool = False        # KAPALI: gunluk kayip limiti yok — pozisyon daraltma/durdurma yapma
    daily_loss_limit_pct: float = 0.04
    daily_loss_reduce_pct: float = 0.02
    daily_loss_kill_pct: float = 0.06
    daily_loss_cooldown_minutes: int = 240

    # ── Kelly Criterion Sizing (outputs MARGIN fraction) ──────
    use_kelly_sizing: bool = True
    kelly_fraction: float = 0.3  # quarter-Kelly: more conservative (was 0.5 half-Kelly)
    kelly_min_trades: int = 30   # need more data for reliable stats (was 20)
    kelly_min_fraction: float = 0.05  # minimum %5 (2% cok az: 6$ bakiye ile 0.12$ margin oluyordu)
    kelly_max_fraction: float = 0.20  # cap %20 (8% cok dusuktu, bakiye buyuyunce de kucuk kaliyordu)

    # ── Volatility-Adjusted Sizing (outputs MARGIN amount) ───
    use_volatility_sizing: bool = True
    target_risk_pct: float = 0.03            # %3 risk per trade (1.5% cok konservatif, buyuyemiyor)
    volatility_sizing_atr_mult: float = 1.5 # wider ATR buffer for sizing (was 1.2)

    # ── Selector ───────────────────────────────────────────────
    max_spread_bps: float = 30.0   # genis spread: daha fazla coin (15 cok katiydi, cogu coin 15-30 arasi)
    min_depth_usdt: float = 1000.0 # dusuk esik: kucuk pozisyonlar icin 1k$ yeterli (5k cok yuksekti)
    vol_guard_bps: float = 600.0   # biraz genis: daha fazla firsat
    shortlist_size: int = 300      # daha genis tarama: 300 coin (150 cok azdi)
    min_volume_24h_usdt: float = 1_000_000.0  # 1M$ yeterli (5M cok yuksekti, firsatlari disladik)

    # ── Trade Management ───────────────────────────────────────
    use_dynamic_tp_sl: bool = True
    atr_sl_multiplier: float = 1.2         # ATR-based SL: daha siki
    atr_tp_multiplier: float = 3.0         # ATR-based TP: daha genis (R:R = 2.5x ATR)
    min_sl_bps: float = 50.0              # minimum SL floor raised (was 30 – noise territory)
    min_tp_bps: float = 100.0             # minimum TP floor raised (was 60)
    # TP must be at least this many bps above round-trip fees so that at TP we have net profit
    min_tp_net_bps: float = 25.0           # need 25bps net after fees (was 10)
    use_trailing_stop: bool = False          # KAPALI: tiered_tp3 bunu yapiyor artik
    trailing_activation_pct: float = 0.75
    trailing_distance_pct: float = 0.3
    use_breakeven_stop: bool = False         # KAPALI: tiered_move_sl_after_tp1 bunu yapiyor
    breakeven_activation_pct: float = 0.7
    breakeven_buffer_bps: float = 10.0
    use_partial_tp: bool = False             # KAPALI: tiered_tp sistemi bunu degistirdi
    partial_tp_fraction: float = 0.3
    partial_tp_trigger_pct: float = 0.8

    # ── Dynamic Leverage (conservative tiers) ─────────────────────
    dynamic_leverage_enabled: bool = True
    dyn_leverage_tier1_confluence: int = 3
    dyn_leverage_tier1_weighted_score: float = 40.0   # %40 yeterli (55 hic ulasılamıyordu)
    dyn_leverage_tier1: int = 4                        # moderate: 4x (was 7x)
    dyn_leverage_tier2_confluence: int = 4
    dyn_leverage_tier2_weighted_score: float = 55.0    # %55 tier 2
    dyn_leverage_tier2: int = 5                        # moderate: 5x (was 10x)
    dyn_leverage_tier3_confluence: int = 6
    dyn_leverage_tier3_weighted_score: float = 70.0    # %70 tier 3 (85 asla ulasılamıyordu)
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
    swing_min_confluence: int = 3          # 3 yeterli swing icin (4 cok katiydi)
    swing_require_trend_alignment: bool = False  # trend zorunlu degil: daha fazla swing firsati
    swing_atr_sl_multiplier: float = 1.5
    swing_atr_tp_multiplier: float = 3.0
    swing_cooldown_minutes: int = 30       # longer cooldown for swing
    swing_leverage: int = 3                # lower leverage for swing (longer hold)

    # ── Scheduling ─────────────────────────────────────────────
    scan_interval_minutes: int = 3         # daha sik tarama: firsatlari yakala (5 dk fazla yavas)
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
    selector_lenient_enabled: bool = True   # AKTIF: filter cok kati olursa lenient fallback devreye girsin
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
