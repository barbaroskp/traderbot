"""Market data aggregator – price, depth, mark price, indicators.

Collects data required by the selector and strategy layers.
Now includes: EMA, RSI, MACD, Bollinger Bands, kline-based analysis.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.bingx_client import BingXClient, BingXClientError
from src.config import Settings
from src.logger import get_logger
from src.storage import Storage

log = get_logger(__name__)


@dataclass
class DepthSnapshot:
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread_bps: float = 0.0
    bid_depth_usdt: float = 0.0
    ask_depth_usdt: float = 0.0
    mid_price: float = 0.0
    imbalance_ratio: float = 0.0  # bid_depth / (bid + ask), >0.5 = bid heavy
    # ── Whale detection ──
    whale_bid_usdt: float = 0.0      # total whale order volume on bid side
    whale_ask_usdt: float = 0.0      # total whale order volume on ask side
    whale_imbalance: float = 0.5     # whale_bid / (whale_bid + whale_ask)


@dataclass
class Indicators:
    """Technical indicators computed from kline data."""
    rsi: float = 50.0           # 0-100
    macd_line: float = 0.0
    macd_signal: float = 0.0
    macd_histogram: float = 0.0
    macd_histogram_prev: float = 0.0
    bollinger_upper: float = 0.0
    bollinger_mid: float = 0.0
    bollinger_lower: float = 0.0
    bollinger_pct: float = 0.5  # where price sits in BB (0=lower, 1=upper)
    ema_trend: float = 0.0      # long-term EMA for trend direction
    trend_direction: str = "NEUTRAL"  # UP, DOWN, NEUTRAL
    volume_ratio: float = 1.0   # current vol / avg vol
    volume_spike: bool = False
    atr: float = 0.0
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    higher_tf_trend: str = "NEUTRAL"
    valid: bool = False         # True if we have enough data
    # ── Kline-based EMA (reliable, no warmup problem) ──
    kline_fast_ema: float = 0.0
    kline_slow_ema: float = 0.0
    kline_z_score_bps: float = 0.0
    # ── Momentum strength ──
    macd_hist_slope: float = 0.0      # rate of change of MACD histogram (3-bar)
    macd_strengthening: bool = False   # True if histogram growing in signal direction
    rsi_slope: float = 0.0            # RSI rate of change (oversold→rising = bullish momentum)
    # ── VWAP deviation ──
    vwap: float = 0.0
    vwap_deviation_bps: float = 0.0   # price vs VWAP in bps
    # ── Price action ──
    recent_high: float = 0.0          # highest close in last 10 bars
    recent_low: float = 0.0           # lowest close in last 10 bars
    price_position_pct: float = 0.5   # where price sits in recent range (0=low, 1=high)
    # ── Stochastic RSI ──
    stoch_rsi_k: float = 50.0   # %K line (0-100), <20 oversold, >80 overbought
    stoch_rsi_d: float = 50.0   # %D signal line (smoothed %K)
    # ── RSI Divergence ──
    rsi_bullish_divergence: bool = False  # price new low but RSI higher low → reversal LONG
    rsi_bearish_divergence: bool = False  # price new high but RSI lower high → reversal SHORT
    # ── Taker Buy/Sell Ratio (proxy from candle direction) ──
    taker_buy_ratio: float = 0.5  # 0-1, >0.5 = more buy pressure
    # ── OBV (On-Balance Volume) ──
    obv: float = 0.0
    obv_slope: float = 0.0  # normalized OBV trend direction
    obv_divergence_bullish: bool = False  # price lower low, OBV higher low
    obv_divergence_bearish: bool = False  # price higher high, OBV lower high
    # ── Williams %R ──
    williams_r: float = -50.0  # -100 to 0; <-80 oversold, >-20 overbought
    # ── Keltner Channels / TTM Squeeze ──
    keltner_upper: float = 0.0
    keltner_lower: float = 0.0
    squeeze_on: bool = False  # BB inside KC = low vol squeeze (breakout imminent)
    # ── Open Interest ──
    open_interest: float = 0.0
    oi_change_pct: float = 0.0  # % change vs previous snapshot
    # ── Price Velocity / Acceleration ──
    price_velocity_bps: float = 0.0  # avg bps change per bar
    price_acceleration: float = 0.0  # change in velocity
    # ── Liquidation Cascade Detection ──
    liq_cascade_signal: str = "NONE"  # LONG_LIQ, SHORT_SQUEEZE, NONE
    liq_cascade_intensity: float = 0.0  # 0-1, strength of cascade signal


@dataclass
class SymbolSnapshot:
    symbol: str = ""
    mid_price: float = 0.0
    mark_price: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread_bps: float = 0.0
    bid_depth_usdt: float = 0.0
    ask_depth_usdt: float = 0.0
    imbalance_ratio: float = 0.0
    whale_bid_usdt: float = 0.0
    whale_ask_usdt: float = 0.0
    whale_imbalance: float = 0.5
    fast_ema: float = 0.0
    slow_ema: float = 0.0
    z_score_bps: float = 0.0
    # ── New indicator fields ──
    indicators: Indicators = field(default_factory=Indicators)
    funding_rate: float = 0.0
    ts: str = ""


@dataclass
class EMAState:
    """Running EMA state for a symbol."""
    fast: float = 0.0
    slow: float = 0.0
    count: int = 0  # number of data points fed


def _detect_liquidation_cascade(ind: "Indicators") -> None:
    """Detect liquidation cascades from OI change + price velocity.

    - OI dropping sharply + price falling fast → long liquidation cascade
    - OI dropping sharply + price rising fast  → short squeeze
    Intensity = min(1.0, |oi_change| / 5 * |velocity| / 100) as 0-1 score.
    """
    oi_chg = ind.oi_change_pct
    vel = ind.price_velocity_bps
    # Need meaningful OI drop (> 2%) and price movement (> 20 bps/bar)
    if oi_chg < -2.0 and abs(vel) > 20:
        intensity = min(1.0, (abs(oi_chg) / 5.0) * (abs(vel) / 100.0))
        if vel < 0:
            ind.liq_cascade_signal = "LONG_LIQ"
            ind.liq_cascade_intensity = intensity
        else:
            ind.liq_cascade_signal = "SHORT_SQUEEZE"
            ind.liq_cascade_intensity = intensity


class MarketData:
    """Fetches and processes market data for symbol lists."""

    def __init__(self, cfg: Settings, client: BingXClient, db: Storage) -> None:
        self.cfg = cfg
        self.client = client
        self.db = db
        self._ema_states: dict[str, EMAState] = {}
        self._prev_oi: dict[str, float] = {}  # previous open interest per symbol

    def get_ema_state(self, symbol: str) -> EMAState:
        if symbol not in self._ema_states:
            self._ema_states[symbol] = EMAState()
        return self._ema_states[symbol]

    # ── Depth ──────────────────────────────────────────────────

    async def fetch_depth(self, symbol: str) -> DepthSnapshot:
        """Fetch order book and compute spread + depth metrics."""
        try:
            raw = await self.client.get_depth(symbol, limit=20)
        except BingXClientError as exc:
            log.warning("depth fetch failed", extra={"symbol": symbol, "error": str(exc)})
            return DepthSnapshot()

        bids: list[list] = raw.get("bids", [])
        asks: list[list] = raw.get("asks", [])

        if not bids or not asks:
            return DepthSnapshot()

        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid = (best_bid + best_ask) / 2

        spread_bps = ((best_ask - best_bid) / mid) * 10_000 if mid > 0 else 9999

        # Depth in USDT (top N levels)
        bid_sizes = [float(b[0]) * float(b[1]) for b in bids[:10]]
        ask_sizes = [float(a[0]) * float(a[1]) for a in asks[:10]]
        bid_depth = sum(bid_sizes)
        ask_depth = sum(ask_sizes)
        total_depth = bid_depth + ask_depth

        imbalance = bid_depth / total_depth if total_depth > 0 else 0.5

        # Whale detection: orders > 3x median size
        all_sizes = bid_sizes + ask_sizes
        if all_sizes:
            sorted_sizes = sorted(all_sizes)
            median_size = sorted_sizes[len(sorted_sizes) // 2]
            whale_threshold = median_size * 3.0
            whale_bid = sum(s for s in bid_sizes if s >= whale_threshold)
            whale_ask = sum(s for s in ask_sizes if s >= whale_threshold)
            whale_total = whale_bid + whale_ask
            whale_imb = whale_bid / whale_total if whale_total > 0 else 0.5
        else:
            whale_bid, whale_ask, whale_imb = 0.0, 0.0, 0.5

        return DepthSnapshot(
            best_bid=best_bid,
            best_ask=best_ask,
            spread_bps=spread_bps,
            bid_depth_usdt=bid_depth,
            ask_depth_usdt=ask_depth,
            mid_price=mid,
            imbalance_ratio=imbalance,
            whale_bid_usdt=whale_bid,
            whale_ask_usdt=whale_ask,
            whale_imbalance=whale_imb,
        )

    # ── Price fetchers ─────────────────────────────────────────

    async def fetch_premium_index(self, symbol: str) -> dict[str, Any]:
        """Fetch premium index payload (mark + funding) once."""
        try:
            return await self.client.get_mark_price(symbol)
        except BingXClientError as exc:
            log.warning("premium index fetch failed", extra={"symbol": symbol, "error": str(exc)})
            return {}

    async def fetch_mark_price(self, symbol: str) -> float:
        """Fetch current mark price."""
        try:
            raw = await self.fetch_premium_index(symbol)
            return float(raw.get("markPrice", 0))
        except ValueError:
            return 0.0

    async def fetch_ticker_price(self, symbol: str) -> float:
        """Fetch last price from ticker."""
        try:
            raw = await self.client.get_ticker(symbol)
            return float(raw.get("lastPrice", 0))
        except (BingXClientError, ValueError):
            return 0.0

    async def fetch_funding_rate(self, symbol: str) -> float:
        """Fetch current funding rate."""
        try:
            raw = await self.fetch_premium_index(symbol)
            return float(raw.get("lastFundingRate", 0))
        except ValueError:
            return 0.0

    # ── Open Interest ──────────────────────────────────────────

    async def fetch_open_interest(self, symbol: str) -> tuple[float, float]:
        """Fetch open interest and compute change vs previous snapshot.

        Returns (oi_value, oi_change_pct).
        """
        if not self.cfg.use_open_interest:
            return 0.0, 0.0
        try:
            raw = await self.client.get_open_interest(symbol)
            oi = float(raw.get("openInterest", 0))
            prev = self._prev_oi.get(symbol, 0.0)
            change_pct = 0.0
            if prev > 0 and oi > 0:
                change_pct = ((oi - prev) / prev) * 100.0
            self._prev_oi[symbol] = oi
            return oi, change_pct
        except (BingXClientError, ValueError, TypeError) as exc:
            log.debug("open interest fetch failed", extra={"symbol": symbol, "error": str(exc)})
            return 0.0, 0.0

    # ── Kline + Indicators ─────────────────────────────────────

    async def fetch_klines(self, symbol: str) -> list[dict[str, Any]]:
        """Fetch kline/candlestick data."""
        try:
            return await self.client.get_klines(
                symbol,
                interval=self.cfg.kline_interval,
                limit=self.cfg.kline_limit,
            )
        except BingXClientError as exc:
            log.warning("kline fetch failed", extra={"symbol": symbol, "error": str(exc)})
            return []

    async def fetch_klines_custom(
        self, symbol: str, interval: str, limit: int
    ) -> list[dict[str, Any]]:
        """Fetch klines with custom interval/limit (for swing trading)."""
        try:
            return await self.client.get_klines(symbol, interval=interval, limit=limit)
        except BingXClientError as exc:
            log.warning("custom kline fetch failed", extra={
                "symbol": symbol, "interval": interval, "error": str(exc),
            })
            return []

    async def fetch_klines_higher_tf(self, symbol: str) -> list[dict[str, Any]]:
        """Fetch higher-timeframe kline data for trend confirmation."""
        try:
            return await self.client.get_klines(
                symbol,
                interval=self.cfg.higher_tf_interval,
                limit=self.cfg.higher_tf_limit,
            )
        except BingXClientError as exc:
            log.warning("higher-tf kline fetch failed", extra={"symbol": symbol, "error": str(exc)})
            return []

    def compute_indicators(self, klines: list[dict[str, Any]]) -> Indicators:
        """Compute RSI, MACD, Bollinger Bands, trend EMA from kline data.

        Kline format from BingX: each item has open, close, high, low, volume, time
        """
        ind = Indicators()

        if not klines or len(klines) < 50:
            return ind

        # Extract close/high/low/open prices and volumes
        closes: list[float] = []
        opens: list[float] = []
        highs: list[float] = []
        lows: list[float] = []
        volumes: list[float] = []
        for k in klines:
            try:
                c = float(k.get("close", k.get("c", 0)))
                o = float(k.get("open", k.get("o", 0)))
                h = float(k.get("high", k.get("h", 0)))
                l = float(k.get("low", k.get("l", 0)))
                v = float(k.get("volume", k.get("v", 0)))
                if c > 0 and h > 0 and l > 0:
                    closes.append(c)
                    opens.append(o if o > 0 else c)
                    highs.append(h)
                    lows.append(l)
                    volumes.append(v)
            except (ValueError, TypeError):
                continue

        if len(closes) < 50 or len(highs) < 50 or len(lows) < 50:
            return ind

        # ── RSI ──────────────────────────────────────────────
        ind.rsi = self._compute_rsi(closes, self.cfg.rsi_period)

        # ── MACD ─────────────────────────────────────────────
        macd_l, macd_s, macd_h, macd_prev = self._compute_macd(
            closes, self.cfg.macd_fast, self.cfg.macd_slow, self.cfg.macd_signal
        )
        ind.macd_line = macd_l
        ind.macd_signal = macd_s
        ind.macd_histogram = macd_h
        ind.macd_histogram_prev = macd_prev

        # ── Bollinger Bands ──────────────────────────────────
        bb_upper, bb_mid, bb_lower = self._compute_bollinger(
            closes, self.cfg.bollinger_period, self.cfg.bollinger_std
        )
        ind.bollinger_upper = bb_upper
        ind.bollinger_mid = bb_mid
        ind.bollinger_lower = bb_lower
        current_price = closes[-1]
        bb_range = bb_upper - bb_lower
        ind.bollinger_pct = (current_price - bb_lower) / bb_range if bb_range > 0 else 0.5

        # ── Trend EMA ────────────────────────────────────────
        ind.ema_trend = self._compute_ema_single(closes, self.cfg.ema_trend)
        if current_price > ind.ema_trend * 1.002:  # 0.2% above = uptrend
            ind.trend_direction = "UP"
        elif current_price < ind.ema_trend * 0.998:
            ind.trend_direction = "DOWN"
        else:
            ind.trend_direction = "NEUTRAL"

        # ── Volume ratio ─────────────────────────────────────
        if len(volumes) >= 20:
            avg_vol = sum(volumes[-20:]) / 20
            ind.volume_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
            ind.volume_spike = ind.volume_ratio >= self.cfg.volume_spike_ratio

        # ── ATR / ADX ───────────────────────────────────────
        ind.atr = self._compute_atr(highs, lows, closes, self.cfg.atr_period)
        ind.adx, ind.plus_di, ind.minus_di = self._compute_adx(
            highs, lows, closes, self.cfg.adx_period
        )

        # ── Kline-based EMA & Z-Score (reliable, no warmup bug) ──
        ind.kline_fast_ema = self._compute_ema_single(closes, self.cfg.fast_ema)
        ind.kline_slow_ema = self._compute_ema_single(closes, self.cfg.slow_ema)
        if ind.kline_fast_ema > 0:
            ind.kline_z_score_bps = ((current_price - ind.kline_fast_ema) / ind.kline_fast_ema) * 10_000

        # ── Momentum strength (MACD histogram slope over last 3 bars) ──
        if len(closes) >= self.cfg.macd_slow + self.cfg.macd_signal + 3:
            _, _, hist_now, hist_prev = self._compute_macd(
                closes, self.cfg.macd_fast, self.cfg.macd_slow, self.cfg.macd_signal
            )
            _, _, hist_2, _ = self._compute_macd(
                closes[:-1], self.cfg.macd_fast, self.cfg.macd_slow, self.cfg.macd_signal
            )
            ind.macd_hist_slope = hist_now - hist_2  # 2-bar slope
            # Strengthening = histogram moving further from zero in its direction
            if hist_now > 0:
                ind.macd_strengthening = hist_now > hist_prev
            elif hist_now < 0:
                ind.macd_strengthening = hist_now < hist_prev

        # ── RSI slope (3-bar) ──
        if len(closes) >= self.cfg.rsi_period + 4:
            rsi_now = self._compute_rsi(closes, self.cfg.rsi_period)
            rsi_prev = self._compute_rsi(closes[:-2], self.cfg.rsi_period)
            ind.rsi_slope = rsi_now - rsi_prev

        # ── VWAP ──
        if len(closes) >= 20 and len(volumes) >= 20:
            ind.vwap = self._compute_vwap(closes, volumes, highs, lows, period=20)
            if ind.vwap > 0:
                ind.vwap_deviation_bps = ((current_price - ind.vwap) / ind.vwap) * 10_000

        # ── Price action (recent range) ──
        lookback = min(10, len(closes))
        if lookback >= 3:
            recent_closes = closes[-lookback:]
            ind.recent_high = max(recent_closes)
            ind.recent_low = min(recent_closes)
            price_range = ind.recent_high - ind.recent_low
            if price_range > 0:
                ind.price_position_pct = (current_price - ind.recent_low) / price_range

        # ── RSI Divergence (lookback 20 bars, compare two swing points) ──
        if len(closes) >= 40 and ind.rsi > 0:
            div_result = self._detect_rsi_divergence(closes, self.cfg.rsi_period)
            ind.rsi_bullish_divergence = div_result[0]
            ind.rsi_bearish_divergence = div_result[1]

        # ── Stochastic RSI ──
        if len(closes) >= self.cfg.rsi_period + 20:
            stoch_k, stoch_d = self._compute_stochastic_rsi(closes, self.cfg.rsi_period)
            ind.stoch_rsi_k = stoch_k
            ind.stoch_rsi_d = stoch_d

        # ── Taker Buy/Sell Ratio (proxy from candle direction) ──
        if len(opens) >= 10 and len(volumes) >= 10:
            ind.taker_buy_ratio = self._compute_taker_buy_ratio(
                closes, opens, volumes, self.cfg.taker_ratio_period,
            )

        # ── OBV + OBV Divergence ──
        if len(volumes) >= 20:
            ind.obv, ind.obv_slope = self._compute_obv(closes, volumes)
            if len(closes) >= 40:
                ind.obv_divergence_bullish, ind.obv_divergence_bearish = (
                    self._detect_obv_divergence(closes, volumes)
                )

        # ── Williams %R ──
        if len(highs) >= self.cfg.williams_r_period:
            ind.williams_r = self._compute_williams_r(
                highs, lows, closes, self.cfg.williams_r_period,
            )

        # ── Keltner Channels / TTM Squeeze ──
        if len(closes) >= self.cfg.keltner_period:
            ind.keltner_upper, ind.keltner_lower = self._compute_keltner(
                closes, highs, lows,
                self.cfg.keltner_period, self.cfg.keltner_atr_mult, self.cfg.atr_period,
            )
            ind.squeeze_on = self._detect_squeeze(
                ind.bollinger_upper, ind.bollinger_lower,
                ind.keltner_upper, ind.keltner_lower,
            )

        # ── Price Velocity / Acceleration ──
        if len(closes) >= self.cfg.velocity_lookback + 2:
            ind.price_velocity_bps, ind.price_acceleration = self._compute_velocity(
                closes, self.cfg.velocity_lookback,
            )

        ind.valid = True
        return ind

    # ── Indicator math ─────────────────────────────────────────

    @staticmethod
    def _compute_rsi(closes: list[float], period: int) -> float:
        """Compute RSI from close prices."""
        if len(closes) < period + 1:
            return 50.0

        gains: list[float] = []
        losses: list[float] = []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))

        # Use Wilder's smoothed average
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _compute_macd(
        closes: list[float], fast: int, slow: int, signal: int
    ) -> tuple[float, float, float, float]:
        """Compute MACD line, signal line, histogram, prev histogram."""
        if len(closes) < slow + signal + 1:
            return 0.0, 0.0, 0.0, 0.0

        def ema_series(data: list[float], period: int) -> list[float]:
            k = 2.0 / (period + 1)
            result = [data[0]]
            for i in range(1, len(data)):
                result.append(data[i] * k + result[-1] * (1 - k))
            return result

        ema_fast = ema_series(closes, fast)
        ema_slow = ema_series(closes, slow)
        macd_line_series = [f - s for f, s in zip(ema_fast, ema_slow)]
        signal_series = ema_series(macd_line_series, signal)

        macd_val = macd_line_series[-1]
        signal_val = signal_series[-1]
        hist_series = [m - s for m, s in zip(macd_line_series, signal_series)]
        hist = hist_series[-1]
        prev_hist = hist_series[-2] if len(hist_series) > 1 else hist
        return macd_val, signal_val, hist, prev_hist

    @staticmethod
    def _compute_bollinger(
        closes: list[float], period: int, num_std: float
    ) -> tuple[float, float, float]:
        """Compute Bollinger Bands (upper, mid, lower)."""
        if len(closes) < period:
            p = closes[-1] if closes else 0
            return p, p, p

        window = closes[-period:]
        mid = sum(window) / period
        variance = sum((x - mid) ** 2 for x in window) / period
        std = math.sqrt(variance)
        return mid + num_std * std, mid, mid - num_std * std

    @staticmethod
    def _compute_ema_single(closes: list[float], period: int) -> float:
        """Compute a single EMA value from close series."""
        if not closes:
            return 0.0
        k = 2.0 / (period + 1)
        ema = closes[0]
        for i in range(1, len(closes)):
            ema = closes[i] * k + ema * (1 - k)
        return ema

    @staticmethod
    def _compute_atr(
        highs: list[float], lows: list[float], closes: list[float], period: int
    ) -> float:
        """Average True Range (Wilder)."""
        if len(closes) < period + 1:
            return 0.0

        trs: list[float] = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)

        atr = sum(trs[:period]) / period
        for i in range(period, len(trs)):
            atr = (atr * (period - 1) + trs[i]) / period
        return atr

    @staticmethod
    def _compute_adx(
        highs: list[float], lows: list[float], closes: list[float], period: int
    ) -> tuple[float, float, float]:
        """Compute ADX and DI (+DI, -DI)."""
        if len(closes) < period * 2:
            return 0.0, 0.0, 0.0

        plus_dm_list: list[float] = []
        minus_dm_list: list[float] = []
        tr_list: list[float] = []

        for i in range(1, len(closes)):
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            plus_dm = max(up_move, 0.0) if up_move > down_move else 0.0
            minus_dm = max(down_move, 0.0) if down_move > up_move else 0.0
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            plus_dm_list.append(plus_dm)
            minus_dm_list.append(minus_dm)
            tr_list.append(tr)

        atr = sum(tr_list[:period]) / period
        plus_di_smooth = sum(plus_dm_list[:period]) / period
        minus_di_smooth = sum(minus_dm_list[:period]) / period

        dx_list: list[float] = []
        for i in range(period, len(tr_list)):
            atr = (atr * (period - 1) + tr_list[i]) / period
            plus_di_smooth = (plus_di_smooth * (period - 1) + plus_dm_list[i]) / period
            minus_di_smooth = (minus_di_smooth * (period - 1) + minus_dm_list[i]) / period

            plus_di = (plus_di_smooth / atr * 100) if atr > 0 else 0.0
            minus_di = (minus_di_smooth / atr * 100) if atr > 0 else 0.0
            di_sum = plus_di + minus_di
            dx = abs(plus_di - minus_di) / di_sum * 100 if di_sum > 0 else 0.0
            dx_list.append(dx)

        if len(dx_list) < period:
            return 0.0, 0.0, 0.0

        adx = sum(dx_list[:period]) / period
        for i in range(period, len(dx_list)):
            adx = (adx * (period - 1) + dx_list[i]) / period

        # Use latest smoothed DI values
        plus_di = (plus_di_smooth / atr * 100) if atr > 0 else 0.0
        minus_di = (minus_di_smooth / atr * 100) if atr > 0 else 0.0
        return adx, plus_di, minus_di

    @staticmethod
    def _compute_vwap(
        closes: list[float], volumes: list[float],
        highs: list[float], lows: list[float], period: int = 20,
    ) -> float:
        """Compute VWAP (typical price * volume / cumulative volume)."""
        n = min(period, len(closes), len(volumes), len(highs), len(lows))
        if n < 5:
            return 0.0
        cum_pv = 0.0
        cum_vol = 0.0
        for i in range(-n, 0):
            typical = (highs[i] + lows[i] + closes[i]) / 3
            cum_pv += typical * volumes[i]
            cum_vol += volumes[i]
        return cum_pv / cum_vol if cum_vol > 0 else 0.0

    @staticmethod
    def _detect_rsi_divergence(
        closes: list[float], rsi_period: int, lookback: int = 20
    ) -> tuple[bool, bool]:
        """Detect bullish/bearish RSI divergence.

        Bullish: price makes lower low but RSI makes higher low → reversal up
        Bearish: price makes higher high but RSI makes lower high → reversal down

        Returns (bullish_divergence, bearish_divergence).
        """
        if len(closes) < lookback + rsi_period + 5:
            return False, False

        # Compute RSI for recent and previous windows
        recent_closes = closes[-lookback:]
        prev_closes = closes[-(lookback * 2):-lookback]

        if len(prev_closes) < rsi_period + 1:
            return False, False

        rsi_recent = MarketData._compute_rsi(closes, rsi_period)
        rsi_prev = MarketData._compute_rsi(closes[:-lookback], rsi_period)

        price_recent_low = min(recent_closes)
        price_prev_low = min(prev_closes)
        price_recent_high = max(recent_closes)
        price_prev_high = max(prev_closes)

        # Bullish divergence: price lower low + RSI higher low
        bullish = (
            price_recent_low < price_prev_low
            and rsi_recent > rsi_prev
            and rsi_recent < 45  # RSI should be in lower zone
        )

        # Bearish divergence: price higher high + RSI lower high
        bearish = (
            price_recent_high > price_prev_high
            and rsi_recent < rsi_prev
            and rsi_recent > 55  # RSI should be in upper zone
        )

        return bullish, bearish

    @staticmethod
    def _compute_stochastic_rsi(
        closes: list[float], rsi_period: int = 14,
        stoch_period: int = 14, smooth_k: int = 3, smooth_d: int = 3,
    ) -> tuple[float, float]:
        """Compute Stochastic RSI (%K smoothed, %D).

        StochRSI applies a stochastic oscillator to RSI values,
        making it more sensitive than RSI alone for overbought/oversold.
        Returns (%K, %D) both in 0-100 range.
        """
        needed = rsi_period + stoch_period + smooth_k + smooth_d + 5
        if len(closes) < needed:
            return 50.0, 50.0

        # Build RSI series incrementally (efficient: O(n))
        gains: list[float] = []
        losses_l: list[float] = []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0))
            losses_l.append(max(-diff, 0))

        if len(gains) < rsi_period:
            return 50.0, 50.0

        avg_gain = sum(gains[:rsi_period]) / rsi_period
        avg_loss = sum(losses_l[:rsi_period]) / rsi_period

        rsi_series: list[float] = []
        for i in range(rsi_period, len(gains)):
            avg_gain = (avg_gain * (rsi_period - 1) + gains[i]) / rsi_period
            avg_loss = (avg_loss * (rsi_period - 1) + losses_l[i]) / rsi_period
            if avg_loss == 0:
                rsi_series.append(100.0)
            else:
                rs = avg_gain / avg_loss
                rsi_series.append(100 - (100 / (1 + rs)))

        n_rsi_needed = stoch_period + smooth_k + smooth_d
        if len(rsi_series) < n_rsi_needed:
            return 50.0, 50.0

        # Stochastic of RSI values
        stoch_raw: list[float] = []
        for i in range(stoch_period - 1, len(rsi_series)):
            window = rsi_series[i - stoch_period + 1: i + 1]
            lo = min(window)
            hi = max(window)
            if hi - lo > 0:
                stoch_raw.append(((rsi_series[i] - lo) / (hi - lo)) * 100)
            else:
                stoch_raw.append(50.0)

        if len(stoch_raw) < smooth_k:
            return 50.0, 50.0

        # Smooth %K with SMA
        k_smoothed: list[float] = []
        for i in range(smooth_k - 1, len(stoch_raw)):
            k_smoothed.append(sum(stoch_raw[i - smooth_k + 1: i + 1]) / smooth_k)

        if len(k_smoothed) < smooth_d:
            return k_smoothed[-1] if k_smoothed else 50.0, 50.0

        # %D = SMA of smoothed %K
        d_val = sum(k_smoothed[-smooth_d:]) / smooth_d
        return k_smoothed[-1], d_val

    # ── Taker Buy/Sell Ratio (proxy from candle direction) ──────

    @staticmethod
    def _compute_taker_buy_ratio(
        closes: list[float], opens: list[float], volumes: list[float], period: int = 20,
    ) -> float:
        """Proxy taker buy ratio using candle direction * volume.

        Green candle (close >= open) volume counted as buy volume.
        Red candle volume counted as sell volume.
        Returns ratio 0-1 where >0.5 = net buying pressure.
        """
        n = min(period, len(closes), len(opens), len(volumes))
        if n < 5:
            return 0.5
        total_vol = 0.0
        buy_vol = 0.0
        for i in range(-n, 0):
            vol = volumes[i]
            total_vol += vol
            if closes[i] >= opens[i]:  # green candle = buy volume
                buy_vol += vol
        return buy_vol / total_vol if total_vol > 0 else 0.5

    # ── OBV (On-Balance Volume) ──────────────────────────────

    @staticmethod
    def _compute_obv(
        closes: list[float], volumes: list[float],
    ) -> tuple[float, float]:
        """Compute OBV and its normalized slope (5-bar).

        Returns (current_obv, slope).
        Slope > 0 = accumulation, < 0 = distribution.
        """
        if len(closes) < 2 or len(volumes) < 2:
            return 0.0, 0.0
        obv = 0.0
        obv_series: list[float] = [0.0]
        for i in range(1, len(closes)):
            if closes[i] > closes[i - 1]:
                obv += volumes[i]
            elif closes[i] < closes[i - 1]:
                obv -= volumes[i]
            obv_series.append(obv)
        # Slope over last 5 bars (normalized by avg volume to be scale-independent)
        lookback = min(5, len(obv_series) - 1)
        if lookback > 0:
            avg_vol = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 1.0
            slope = (obv_series[-1] - obv_series[-1 - lookback]) / max(avg_vol, 1.0)
        else:
            slope = 0.0
        return obv, slope

    @staticmethod
    def _detect_obv_divergence(
        closes: list[float], volumes: list[float], lookback: int = 20,
    ) -> tuple[bool, bool]:
        """Detect OBV divergence vs price.

        Bullish: price lower low but OBV higher low → accumulation.
        Bearish: price higher high but OBV lower high → distribution.
        Returns (bullish, bearish).
        """
        if len(closes) < lookback * 2 or len(volumes) < lookback * 2:
            return False, False
        # Build OBV series
        obv_series = [0.0]
        for i in range(1, len(closes)):
            if closes[i] > closes[i - 1]:
                obv_series.append(obv_series[-1] + volumes[i])
            elif closes[i] < closes[i - 1]:
                obv_series.append(obv_series[-1] - volumes[i])
            else:
                obv_series.append(obv_series[-1])

        recent_price = closes[-lookback:]
        prev_price = closes[-lookback * 2 : -lookback]
        recent_obv = obv_series[-lookback:]
        prev_obv = obv_series[-lookback * 2 : -lookback]

        bullish = (
            min(recent_price) < min(prev_price)
            and min(recent_obv) > min(prev_obv)
        )
        bearish = (
            max(recent_price) > max(prev_price)
            and max(recent_obv) < max(prev_obv)
        )
        return bullish, bearish

    # ── Williams %R ──────────────────────────────────────────

    @staticmethod
    def _compute_williams_r(
        highs: list[float], lows: list[float], closes: list[float], period: int = 14,
    ) -> float:
        """Williams %R: -100 to 0. <-80 oversold, >-20 overbought."""
        if len(closes) < period or len(highs) < period or len(lows) < period:
            return -50.0
        highest = max(highs[-period:])
        lowest = min(lows[-period:])
        if highest == lowest:
            return -50.0
        return ((highest - closes[-1]) / (highest - lowest)) * -100.0

    # ── Keltner Channels / TTM Squeeze ───────────────────────

    @staticmethod
    def _compute_keltner(
        closes: list[float], highs: list[float], lows: list[float],
        period: int = 20, atr_mult: float = 1.5, atr_period: int = 14,
    ) -> tuple[float, float]:
        """Keltner Channel upper/lower bands = EMA ± ATR * mult."""
        if len(closes) < max(period, atr_period + 1):
            return 0.0, 0.0
        ema = MarketData._compute_ema_single(closes, period)
        atr = MarketData._compute_atr(highs, lows, closes, atr_period)
        return ema + atr_mult * atr, ema - atr_mult * atr

    @staticmethod
    def _detect_squeeze(
        bb_upper: float, bb_lower: float, kc_upper: float, kc_lower: float,
    ) -> bool:
        """TTM Squeeze: BB inside KC = low volatility, breakout imminent."""
        if kc_upper <= 0 or kc_lower <= 0:
            return False
        return bb_lower > kc_lower and bb_upper < kc_upper

    # ── Price Velocity / Acceleration ────────────────────────

    @staticmethod
    def _compute_velocity(
        closes: list[float], lookback: int = 5,
    ) -> tuple[float, float]:
        """Price velocity (avg bps/bar) and acceleration.

        Velocity > 0 = rising, < 0 = falling.
        Acceleration > 0 = speeding up, < 0 = slowing down.
        """
        if len(closes) < lookback + 2:
            return 0.0, 0.0
        changes: list[float] = []
        for i in range(-lookback, 0):
            if closes[i - 1] > 0:
                changes.append(((closes[i] - closes[i - 1]) / closes[i - 1]) * 10_000)
        velocity = sum(changes) / len(changes) if changes else 0.0
        # Acceleration = current velocity vs previous period velocity
        acceleration = 0.0
        if len(closes) >= lookback * 2 + 2:
            prev_changes: list[float] = []
            for i in range(-lookback * 2, -lookback):
                if closes[i - 1] > 0:
                    prev_changes.append(((closes[i] - closes[i - 1]) / closes[i - 1]) * 10_000)
            prev_velocity = sum(prev_changes) / len(prev_changes) if prev_changes else 0.0
            acceleration = velocity - prev_velocity
        return velocity, acceleration

    # ── Running EMA (per-tick, for z-score) ────────────────────

    def update_ema(self, symbol: str, price: float) -> tuple[float, float, float]:
        """Update running EMAs and return (fast_ema, slow_ema, z_score_bps)."""
        state = self.get_ema_state(symbol)
        state.count += 1

        k_fast = 2.0 / (self.cfg.fast_ema + 1)
        k_slow = 2.0 / (self.cfg.slow_ema + 1)

        if state.count == 1:
            state.fast = price
            state.slow = price
        else:
            state.fast = price * k_fast + state.fast * (1 - k_fast)
            state.slow = price * k_slow + state.slow * (1 - k_slow)

        z_bps = ((price - state.fast) / state.fast) * 10_000 if state.fast > 0 else 0
        return state.fast, state.slow, z_bps

    # ── Full snapshot ──────────────────────────────────────────

    async def snapshot_symbol(self, symbol: str, fetch_depth: bool = True) -> SymbolSnapshot:
        """Full snapshot: depth + mark price + EMA + indicators."""
        snap = SymbolSnapshot(symbol=symbol)
        snap.ts = datetime.now(timezone.utc).isoformat()

        # Fetch in parallel: depth, premium index (mark + funding), klines (+ optional higher TF + OI)
        tasks: list = []
        need_higher_tf = self.cfg.use_higher_tf_trend
        need_oi = self.cfg.use_open_interest
        if fetch_depth:
            tasks.append(self.fetch_depth(symbol))
        tasks.append(self.fetch_premium_index(symbol))
        tasks.append(self.fetch_klines(symbol))
        if need_higher_tf:
            tasks.append(self.fetch_klines_higher_tf(symbol))
        if need_oi:
            tasks.append(self.fetch_open_interest(symbol))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        idx = 0
        if fetch_depth:
            depth = results[idx] if not isinstance(results[idx], Exception) else DepthSnapshot()
            if isinstance(depth, DepthSnapshot):
                snap.best_bid = depth.best_bid
                snap.best_ask = depth.best_ask
                snap.spread_bps = depth.spread_bps
                snap.bid_depth_usdt = depth.bid_depth_usdt
                snap.ask_depth_usdt = depth.ask_depth_usdt
                snap.mid_price = depth.mid_price
                snap.imbalance_ratio = depth.imbalance_ratio
                snap.whale_bid_usdt = depth.whale_bid_usdt
                snap.whale_ask_usdt = depth.whale_ask_usdt
                snap.whale_imbalance = depth.whale_imbalance
            idx += 1

        # Premium index -> mark price + funding
        premium_result = results[idx]
        if not isinstance(premium_result, Exception) and isinstance(premium_result, dict):
            try:
                snap.mark_price = float(premium_result.get("markPrice", 0))
            except ValueError:
                snap.mark_price = 0.0
            try:
                snap.funding_rate = float(premium_result.get("lastFundingRate", 0))
            except ValueError:
                snap.funding_rate = 0.0
        idx += 1

        # Klines → indicators
        kline_result = results[idx]
        if not isinstance(kline_result, Exception) and isinstance(kline_result, list):
            snap.indicators = self.compute_indicators(kline_result)
        idx += 1

        # Higher TF trend (only when actively used in filtering)
        if need_higher_tf:
            higher_result = results[idx]
            if not isinstance(higher_result, Exception) and isinstance(higher_result, list):
                higher_ind = self.compute_indicators(higher_result)
                snap.indicators.higher_tf_trend = higher_ind.trend_direction
            idx += 1

        # Open Interest
        if need_oi:
            oi_result = results[idx]
            if not isinstance(oi_result, Exception) and isinstance(oi_result, tuple):
                snap.indicators.open_interest = oi_result[0]
                snap.indicators.oi_change_pct = oi_result[1]
                # Liquidation cascade detection
                _detect_liquidation_cascade(snap.indicators)
            idx += 1

        # Use kline-based EMA as primary (reliable, no warmup bug)
        # Fall back to running EMA only if kline indicators are not valid.
        # Keep running EMA state warm with a single update per snapshot.
        price_for_ema = snap.mid_price if snap.mid_price > 0 else snap.mark_price
        if snap.indicators.valid and snap.indicators.kline_fast_ema > 0:
            snap.fast_ema = snap.indicators.kline_fast_ema
            snap.slow_ema = snap.indicators.kline_slow_ema
            snap.z_score_bps = snap.indicators.kline_z_score_bps
            if price_for_ema > 0:
                self.update_ema(symbol, price_for_ema)
        else:
            # Fallback: running EMA (less reliable, needs warmup)
            if price_for_ema > 0:
                snap.fast_ema, snap.slow_ema, snap.z_score_bps = self.update_ema(
                    symbol, price_for_ema
                )

        # Persist stats
        self.db.insert(
            "market_stats",
            {
                "symbol": symbol,
                "ts": snap.ts,
                "mid_price": snap.mid_price,
                "mark_price": snap.mark_price,
                "best_bid": snap.best_bid,
                "best_ask": snap.best_ask,
                "spread_bps": snap.spread_bps,
                "bid_depth": snap.bid_depth_usdt,
                "ask_depth": snap.ask_depth_usdt,
                "fast_ema": snap.fast_ema,
                "slow_ema": snap.slow_ema,
                "z_score_bps": snap.z_score_bps,
            },
        )

        return snap

    async def snapshot_symbol_swing(
        self,
        symbol: str,
        swing_interval: str = "1h",
        swing_limit: int = 100,
        trend_interval: str = "4h",
        trend_limit: int = 50,
    ) -> SymbolSnapshot:
        """Snapshot using swing timeframe klines (1h) + 4h trend filter."""
        snap = SymbolSnapshot(symbol=symbol)
        snap.ts = datetime.now(timezone.utc).isoformat()

        tasks = [
            self.fetch_depth(symbol),
            self.fetch_premium_index(symbol),
            self.fetch_klines_custom(symbol, swing_interval, swing_limit),
            self.fetch_klines_custom(symbol, trend_interval, trend_limit),
        ]
        if self.cfg.use_open_interest:
            tasks.append(self.fetch_open_interest(symbol))
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Depth
        depth = results[0] if not isinstance(results[0], Exception) else DepthSnapshot()
        if isinstance(depth, DepthSnapshot):
            snap.best_bid = depth.best_bid
            snap.best_ask = depth.best_ask
            snap.spread_bps = depth.spread_bps
            snap.bid_depth_usdt = depth.bid_depth_usdt
            snap.ask_depth_usdt = depth.ask_depth_usdt
            snap.mid_price = depth.mid_price
            snap.imbalance_ratio = depth.imbalance_ratio
            snap.whale_bid_usdt = depth.whale_bid_usdt
            snap.whale_ask_usdt = depth.whale_ask_usdt
            snap.whale_imbalance = depth.whale_imbalance

        # Mark price + funding
        premium = results[1]
        if not isinstance(premium, Exception) and isinstance(premium, dict):
            try:
                snap.mark_price = float(premium.get("markPrice", 0))
            except ValueError:
                snap.mark_price = 0.0
            try:
                snap.funding_rate = float(premium.get("lastFundingRate", 0))
            except ValueError:
                snap.funding_rate = 0.0

        # Swing klines -> indicators
        klines = results[2]
        if not isinstance(klines, Exception) and isinstance(klines, list):
            snap.indicators = self.compute_indicators(klines)

        # 4h trend
        trend_klines = results[3]
        if not isinstance(trend_klines, Exception) and isinstance(trend_klines, list):
            trend_ind = self.compute_indicators(trend_klines)
            snap.indicators.higher_tf_trend = trend_ind.trend_direction

        # Open Interest (swing)
        if self.cfg.use_open_interest:
            oi_result = results[4]
            if not isinstance(oi_result, Exception) and isinstance(oi_result, tuple):
                snap.indicators.open_interest = oi_result[0]
                snap.indicators.oi_change_pct = oi_result[1]
                _detect_liquidation_cascade(snap.indicators)

        # EMA from swing klines
        if snap.indicators.valid and snap.indicators.kline_fast_ema > 0:
            snap.fast_ema = snap.indicators.kline_fast_ema
            snap.slow_ema = snap.indicators.kline_slow_ema
            snap.z_score_bps = snap.indicators.kline_z_score_bps

        return snap

    async def batch_snapshots_swing(
        self,
        symbols: list[str],
        swing_interval: str = "1h",
        swing_limit: int = 100,
        trend_interval: str = "4h",
        trend_limit: int = 50,
        concurrency: int = 3,
    ) -> list[SymbolSnapshot]:
        """Fetch swing snapshots with lower concurrency (heavier API load)."""
        sem = asyncio.Semaphore(concurrency)
        results: list[SymbolSnapshot] = []

        async def _fetch(sym: str) -> SymbolSnapshot:
            async with sem:
                return await self.snapshot_symbol_swing(
                    sym, swing_interval, swing_limit, trend_interval, trend_limit,
                )

        tasks = [_fetch(s) for s in symbols]
        raw = await asyncio.gather(*tasks, return_exceptions=True)
        for r in raw:
            if isinstance(r, SymbolSnapshot):
                results.append(r)
        return results

    async def batch_snapshots(
        self,
        symbols: list[str],
        fetch_depth: bool = True,
        concurrency: int = 5,
    ) -> list[SymbolSnapshot]:
        """Fetch snapshots for many symbols with concurrency control."""
        sem = asyncio.Semaphore(concurrency)
        results: list[SymbolSnapshot] = []

        async def _fetch(sym: str) -> SymbolSnapshot:
            async with sem:
                return await self.snapshot_symbol(sym, fetch_depth=fetch_depth)

        tasks = [_fetch(s) for s in symbols]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in raw_results:
            if isinstance(r, SymbolSnapshot):
                results.append(r)
            elif isinstance(r, Exception):
                log.warning("batch snapshot error", extra={"error": str(r)})

        return results
