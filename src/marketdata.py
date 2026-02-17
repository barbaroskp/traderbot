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
    # ── RSI Divergence ──
    rsi_bullish_divergence: bool = False  # price new low but RSI higher low → reversal LONG
    rsi_bearish_divergence: bool = False  # price new high but RSI lower high → reversal SHORT


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


class MarketData:
    """Fetches and processes market data for symbol lists."""

    def __init__(self, cfg: Settings, client: BingXClient, db: Storage) -> None:
        self.cfg = cfg
        self.client = client
        self.db = db
        self._ema_states: dict[str, EMAState] = {}

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
        bid_depth = sum(float(b[0]) * float(b[1]) for b in bids[:10])
        ask_depth = sum(float(a[0]) * float(a[1]) for a in asks[:10])
        total_depth = bid_depth + ask_depth

        imbalance = bid_depth / total_depth if total_depth > 0 else 0.5

        return DepthSnapshot(
            best_bid=best_bid,
            best_ask=best_ask,
            spread_bps=spread_bps,
            bid_depth_usdt=bid_depth,
            ask_depth_usdt=ask_depth,
            mid_price=mid,
            imbalance_ratio=imbalance,
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

        # Extract close/high/low prices and volumes
        closes: list[float] = []
        highs: list[float] = []
        lows: list[float] = []
        volumes: list[float] = []
        for k in klines:
            try:
                c = float(k.get("close", k.get("c", 0)))
                h = float(k.get("high", k.get("h", 0)))
                l = float(k.get("low", k.get("l", 0)))
                v = float(k.get("volume", k.get("v", 0)))
                if c > 0 and h > 0 and l > 0:
                    closes.append(c)
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

        # Fetch in parallel: depth, premium index (mark + funding), klines (+ optional higher TF)
        tasks: list = []
        need_higher_tf = self.cfg.use_higher_tf_trend and self.cfg.require_higher_tf_alignment
        if fetch_depth:
            tasks.append(self.fetch_depth(symbol))
        tasks.append(self.fetch_premium_index(symbol))
        tasks.append(self.fetch_klines(symbol))
        if need_higher_tf:
            tasks.append(self.fetch_klines_higher_tf(symbol))

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

        # Use kline-based EMA as primary (reliable, no warmup bug)
        # Fall back to running EMA only if kline indicators are not valid
        if snap.indicators.valid and snap.indicators.kline_fast_ema > 0:
            snap.fast_ema = snap.indicators.kline_fast_ema
            snap.slow_ema = snap.indicators.kline_slow_ema
            snap.z_score_bps = snap.indicators.kline_z_score_bps
        else:
            # Fallback: running EMA (less reliable, needs warmup)
            price_for_ema = snap.mid_price if snap.mid_price > 0 else snap.mark_price
            if price_for_ema > 0:
                snap.fast_ema, snap.slow_ema, snap.z_score_bps = self.update_ema(
                    symbol, price_for_ema
                )

        # Always update running EMA for continuity (used as fallback)
        price_for_ema = snap.mid_price if snap.mid_price > 0 else snap.mark_price
        if price_for_ema > 0:
            self.update_ema(symbol, price_for_ema)

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
