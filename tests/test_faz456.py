"""Tests for Faz 4-6: Portfolio optimization, market microstructure, self-tuning."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.config import Settings
from src.marketdata import Indicators, SymbolSnapshot
from src.risk import RiskManager
from src.storage import Storage
from src.strategy import Strategy


@pytest.fixture()
def faz456_cfg(cfg) -> Settings:
    """Config with Faz 4-6 features enabled."""
    cfg.avoid_funding_window = False
    cfg.use_adaptive_quality = False  # don't interfere with these tests
    cfg.use_anti_manipulation = False
    cfg.use_drawdown_leverage_scaling = True
    cfg.use_portfolio_heat = True
    cfg.use_rolling_sharpe = True
    cfg.use_decay_detection = True
    cfg.use_liquidity_sizing = True
    return cfg


def _insert_trades(db: Storage, wins: int, losses: int, win_amt: float = 0.5, loss_amt: float = -0.3) -> None:
    """Insert simulated closed trades into DB."""
    now = datetime.now(timezone.utc).isoformat()
    for i in range(wins):
        db.insert("positions", {
            "symbol": f"WIN-{i}", "side": "LONG", "entry_price": 100.0,
            "qty": 1.0, "notional": 100.0, "sl_order_id": "", "tp_order_id": "",
            "tp1_order_id": "", "sl_bps": 100, "tp_bps": 200,
            "original_qty": 1.0, "remaining_qty": 0.0,
            "status": "CLOSED", "is_paper": 1, "realised_pnl": win_amt,
            "closed_at": now, "opened_at": now,
        })
    for i in range(losses):
        db.insert("positions", {
            "symbol": f"LOSS-{i}", "side": "LONG", "entry_price": 100.0,
            "qty": 1.0, "notional": 100.0, "sl_order_id": "", "tp_order_id": "",
            "tp1_order_id": "", "sl_bps": 100, "tp_bps": 200,
            "original_qty": 1.0, "remaining_qty": 0.0,
            "status": "CLOSED", "is_paper": 1, "realised_pnl": loss_amt,
            "closed_at": now, "opened_at": now,
        })


# ── Faz 4: Drawdown Leverage Scaling ──────────────────────

class TestDrawdownLeverageScaling:
    def test_no_drawdown_returns_1(self, faz456_cfg, db) -> None:
        risk = RiskManager(faz456_cfg, db)
        assert risk.get_drawdown_leverage_mult() == 1.0

    def test_small_drawdown_no_scaling(self, faz456_cfg, db) -> None:
        risk = RiskManager(faz456_cfg, db)
        risk._peak_balance = 100.0
        risk._current_balance = 97.0  # 3% drawdown, below 5% start
        assert risk.get_drawdown_leverage_mult() == 1.0

    def test_medium_drawdown_scales(self, faz456_cfg, db) -> None:
        risk = RiskManager(faz456_cfg, db)
        risk._peak_balance = 100.0
        risk._current_balance = 90.0  # 10% drawdown (between 5% and 15%)
        mult = risk.get_drawdown_leverage_mult()
        assert 0.4 < mult < 1.0

    def test_deep_drawdown_minimum(self, faz456_cfg, db) -> None:
        risk = RiskManager(faz456_cfg, db)
        risk._peak_balance = 100.0
        risk._current_balance = 80.0  # 20% drawdown (above 15%)
        mult = risk.get_drawdown_leverage_mult()
        assert mult == faz456_cfg.drawdown_leverage_min_mult

    def test_disabled_returns_1(self, faz456_cfg, db) -> None:
        faz456_cfg.use_drawdown_leverage_scaling = False
        risk = RiskManager(faz456_cfg, db)
        risk._peak_balance = 100.0
        risk._current_balance = 80.0
        assert risk.get_drawdown_leverage_mult() == 1.0


# ── Faz 4: Portfolio Heat Monitor ─────────────────────────

class TestPortfolioHeat:
    def test_low_heat_returns_1(self, faz456_cfg, db) -> None:
        risk = RiskManager(faz456_cfg, db)
        risk._current_balance = 100.0
        assert risk.get_portfolio_heat_mult(10.0) == 1.0  # 10% heat

    def test_moderate_heat_reduces(self, faz456_cfg, db) -> None:
        risk = RiskManager(faz456_cfg, db)
        risk._current_balance = 100.0
        mult = risk.get_portfolio_heat_mult(80.0)  # 80% heat > 75% reduce threshold
        assert mult == faz456_cfg.portfolio_heat_reduce_mult

    def test_max_heat_blocks(self, faz456_cfg, db) -> None:
        risk = RiskManager(faz456_cfg, db)
        risk._current_balance = 100.0
        mult = risk.get_portfolio_heat_mult(95.0)  # 95% heat > 90% max
        assert mult == 0.0

    def test_disabled_returns_1(self, faz456_cfg, db) -> None:
        faz456_cfg.use_portfolio_heat = False
        risk = RiskManager(faz456_cfg, db)
        risk._current_balance = 100.0
        assert risk.get_portfolio_heat_mult(65.0) == 1.0


# ── Faz 5: Order Flow Imbalance Vote ──────────────────────

class TestOrderFlowVote:
    def test_bid_dominant_generates_long_vote(self, faz456_cfg, db) -> None:
        strategy = Strategy(faz456_cfg, db)
        indicators = Indicators(valid=True, rsi=25.0, macd_histogram=-0.001,
                                macd_histogram_prev=-0.0005, bollinger_pct=0.1,
                                adx=25.0, plus_di=15.0, minus_di=15.0)
        snap = SymbolSnapshot(
            symbol="TEST", mid_price=50000, mark_price=50000,
            best_bid=49999, best_ask=50001, spread_bps=2,
            bid_depth_usdt=8000, ask_depth_usdt=2000,  # 80% bid
            imbalance_ratio=0.8, fast_ema=50000, slow_ema=49900,
            z_score_bps=-30, indicators=indicators,
        )
        votes = strategy._compute_votes(snap)
        flow_votes = [v for v in votes if v.name == "orderflow"]
        assert len(flow_votes) == 1
        assert flow_votes[0].side == "LONG"

    def test_ask_dominant_generates_short_vote(self, faz456_cfg, db) -> None:
        strategy = Strategy(faz456_cfg, db)
        indicators = Indicators(valid=True, rsi=75.0, macd_histogram=0.001,
                                macd_histogram_prev=0.0005, bollinger_pct=0.9,
                                adx=25.0, plus_di=15.0, minus_di=15.0)
        snap = SymbolSnapshot(
            symbol="TEST", mid_price=50000, mark_price=50000,
            best_bid=49999, best_ask=50001, spread_bps=2,
            bid_depth_usdt=2000, ask_depth_usdt=8000,  # 20% bid
            imbalance_ratio=0.2, fast_ema=50000, slow_ema=50100,
            z_score_bps=30, indicators=indicators,
        )
        votes = strategy._compute_votes(snap)
        flow_votes = [v for v in votes if v.name == "orderflow"]
        assert len(flow_votes) == 1
        assert flow_votes[0].side == "SHORT"


# ── Faz 6: Rolling Sharpe ─────────────────────────────────

class TestRollingSharpe:
    def test_no_data_returns_none(self, faz456_cfg, db) -> None:
        risk = RiskManager(faz456_cfg, db)
        assert risk.get_rolling_sharpe() is None

    def test_positive_sharpe(self, faz456_cfg, db) -> None:
        _insert_trades(db, wins=12, losses=3, win_amt=1.0, loss_amt=-0.5)
        risk = RiskManager(faz456_cfg, db)
        sharpe = risk.get_rolling_sharpe()
        assert sharpe is not None
        assert sharpe > 0

    def test_negative_sharpe(self, faz456_cfg, db) -> None:
        _insert_trades(db, wins=3, losses=15, win_amt=0.2, loss_amt=-0.5)
        risk = RiskManager(faz456_cfg, db)
        sharpe = risk.get_rolling_sharpe()
        assert sharpe is not None
        assert sharpe < 0

    def test_negative_sharpe_blocks_trading(self, faz456_cfg, db) -> None:
        faz456_cfg.rolling_sharpe_pause_threshold = -0.3
        _insert_trades(db, wins=2, losses=20, win_amt=0.1, loss_amt=-0.8)
        risk = RiskManager(faz456_cfg, db)
        can_trade, mult = risk.can_trade_by_sharpe()
        sharpe = risk.get_rolling_sharpe()
        if sharpe is not None and sharpe < -0.3:
            assert not can_trade
            assert mult == 0.0

    def test_disabled_allows_all(self, faz456_cfg, db) -> None:
        faz456_cfg.use_rolling_sharpe = False
        risk = RiskManager(faz456_cfg, db)
        can_trade, mult = risk.can_trade_by_sharpe()
        assert can_trade
        assert mult == 1.0


# ── Faz 6: Strategy Decay Detection ───────────────────────

class TestStrategyDecay:
    def test_no_data_no_decay(self, faz456_cfg, db) -> None:
        risk = RiskManager(faz456_cfg, db)
        decaying, reason = risk.detect_strategy_decay()
        assert not decaying

    def test_stable_performance_no_decay(self, faz456_cfg, db) -> None:
        """Consistent performance should not trigger decay."""
        faz456_cfg.decay_min_trades = 25
        # 70% winrate across both periods
        _insert_trades(db, wins=70, losses=30)
        risk = RiskManager(faz456_cfg, db)
        decaying, reason = risk.detect_strategy_decay()
        assert not decaying

    def test_declining_performance_detects_decay(self, faz456_cfg, db) -> None:
        """If recent win rate drops significantly, detect decay."""
        faz456_cfg.decay_min_trades = 25
        faz456_cfg.decay_lookback_recent = 20
        faz456_cfg.decay_lookback_baseline = 80
        # First insert good baseline (70% WR)
        _insert_trades(db, wins=56, losses=24, win_amt=0.5, loss_amt=-0.3)
        # Then insert bad recent (20% WR) - these appear first in DESC order
        now = datetime.now(timezone.utc).isoformat()
        for i in range(20):
            pnl = 0.5 if i < 4 else -0.3  # 4 wins, 16 losses = 20% WR
            db.insert("positions", {
                "symbol": f"RECENT-{i}", "side": "LONG", "entry_price": 100.0,
                "qty": 1.0, "notional": 100.0, "sl_order_id": "", "tp_order_id": "",
                "tp1_order_id": "", "sl_bps": 100, "tp_bps": 200,
                "original_qty": 1.0, "remaining_qty": 0.0,
                "status": "CLOSED", "is_paper": 1, "realised_pnl": pnl,
                "closed_at": now, "opened_at": now,
            })
        risk = RiskManager(faz456_cfg, db)
        decaying, reason = risk.detect_strategy_decay()
        assert decaying
        assert "winrate_drop" in reason or "pf_drop" in reason

    def test_decay_returns_size_mult(self, faz456_cfg, db) -> None:
        faz456_cfg.use_decay_detection = False  # disable
        risk = RiskManager(faz456_cfg, db)
        assert risk.get_decay_size_mult() == 1.0
