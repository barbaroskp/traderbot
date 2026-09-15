"""Tests for the allocation engine.

Several of these pin bugs that were found by replaying the engine over two years
of history and noticing results that were too good to be true.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.allocator import (
    Allocator,
    AllocatorState,
    RebalanceReason,
    Stance,
)
from src.config import Settings

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture()
def cfg() -> Settings:
    return Settings(
        _env_file=None,
        allocation_basket="AAA-USDT,BBB-USDT",
        rebalance_days=30,
        rebalance_drift_pct=30.0,
        max_portfolio_drawdown_pct=25.0,   # explicitly on for these tests
        reentry_recovery_pct=25.0,
        min_rebalance_trade_quote=1.0,
        db_path=":memory:",
        log_file="",
    )


def _alloc(cfg: Settings) -> Allocator:
    return Allocator(cfg, AllocatorState())


class TestTargets:
    def test_equal_weight(self, cfg) -> None:
        assert _alloc(cfg).target_weights() == {"AAA-USDT": 0.5, "BBB-USDT": 0.5}

    def test_empty_basket_is_a_noop(self, cfg) -> None:
        cfg.allocation_basket = ""
        d = _alloc(cfg).decide(1000.0, {}, 1.0, T0)
        assert not d.rebalance


class TestDeployment:
    def test_initial_deployment_buys_the_basket(self, cfg) -> None:
        d = _alloc(cfg).decide(1000.0, {}, 1.0, T0)
        assert d.rebalance and d.reason is RebalanceReason.INITIAL
        assert {i.symbol for i in d.intents} == {"AAA-USDT", "BBB-USDT"}
        assert all(i.delta_quote == pytest.approx(500.0) for i in d.intents)

    def test_never_emits_leverage_or_shorts(self, cfg) -> None:
        """The engine holds a basket; it does not express directional views."""
        d = _alloc(cfg).decide(1000.0, {}, 1.0, T0)
        assert sum(i.target_quote for i in d.intents) <= 1000.0 + 1e-9
        assert all(i.target_quote >= 0 for i in d.intents)


class TestRebalanceTriggers:
    def test_in_tolerance_does_nothing(self, cfg) -> None:
        a = _alloc(cfg)
        a.decide(1000.0, {}, 1.0, T0)
        d = a.decide(1000.0, {"AAA-USDT": 505.0, "BBB-USDT": 495.0}, 1.0, T0 + timedelta(days=1))
        assert not d.rebalance

    def test_drift_triggers_off_schedule(self, cfg) -> None:
        """This is the response to a sharp move: trim the spike, top up the
        laggard. No forecast is involved."""
        a = _alloc(cfg)
        a.decide(1000.0, {}, 1.0, T0)
        # AAA doubles: weight 1000/1400 = 71%, i.e. 43% above its 50% target,
        # past the 30% drift tolerance.
        d = a.decide(1400.0, {"AAA-USDT": 1000.0, "BBB-USDT": 400.0}, 1.4, T0 + timedelta(days=2))
        assert d.rebalance and d.reason is RebalanceReason.DRIFT
        sell = next(i for i in d.intents if i.symbol == "AAA-USDT")
        buy = next(i for i in d.intents if i.symbol == "BBB-USDT")
        assert sell.delta_quote < 0 and buy.delta_quote > 0

    def test_schedule_triggers_after_the_cadence(self, cfg) -> None:
        cfg.rebalance_drift_pct = 0.0            # isolate the calendar path
        a = _alloc(cfg)
        a.decide(1000.0, {}, 1.0, T0)
        held = {"AAA-USDT": 520.0, "BBB-USDT": 480.0}
        assert not a.decide(1000.0, held, 1.0, T0 + timedelta(days=29)).rebalance
        assert a.decide(1000.0, held, 1.0, T0 + timedelta(days=31)).reason is RebalanceReason.SCHEDULED

    def test_dust_trades_are_skipped(self, cfg) -> None:
        cfg.min_rebalance_trade_quote = 50.0
        a = _alloc(cfg)
        a.decide(1000.0, {}, 1.0, T0)
        d = a.decide(1000.0, {"AAA-USDT": 510.0, "BBB-USDT": 490.0}, 1.0, T0 + timedelta(days=60))
        assert d.intents == []


class TestDrawdownBrake:
    def _tripped(self, cfg) -> Allocator:
        a = _alloc(cfg)
        a.decide(1000.0, {}, 1.0, T0)
        a.decide(1000.0, {"AAA-USDT": 500.0, "BBB-USDT": 500.0}, 1.0, T0 + timedelta(days=1))
        d = a.decide(700.0, {"AAA-USDT": 350.0, "BBB-USDT": 350.0}, 0.7, T0 + timedelta(days=2))
        assert d.reason is RebalanceReason.DE_RISK
        assert a.state.stance is Stance.DEFENSIVE
        return a

    def test_brake_sells_everything(self, cfg) -> None:
        a = _alloc(cfg)
        a.decide(1000.0, {}, 1.0, T0)
        d = a.decide(700.0, {"AAA-USDT": 350.0, "BBB-USDT": 350.0}, 0.7, T0 + timedelta(days=2))
        assert all(i.target_quote == 0.0 and i.delta_quote < 0 for i in d.intents)

    def test_reentry_reads_the_market_not_equity(self, cfg) -> None:
        """The bug this pins: re-entry used to be judged on portfolio equity,
        which is FROZEN while the book sits in stables. The recovery test could
        therefore never become true and the brake was a one-way door — in a
        two-year replay it sold once and stayed in cash for 79% of the sample,
        which made a broken engine look like a spectacular one."""
        a = self._tripped(cfg)
        cash = {"AAA-USDT": 0.0, "BBB-USDT": 0.0}

        # Equity is unchanged (we hold stables) but the market keeps falling.
        assert not a.decide(700.0, cash, 0.6, T0 + timedelta(days=5)).rebalance
        # Market recovers 25% off its low of 0.6 -> 0.75. Equity is STILL 700.
        d = a.decide(700.0, cash, 0.75, T0 + timedelta(days=9))
        assert d.rebalance and d.reason is RebalanceReason.RE_ENTER
        assert a.state.stance is Stance.INVESTED

    def test_reentry_rearms_the_brake(self, cfg) -> None:
        """Otherwise drawdown against the OLD peak is still past the limit on the
        very next cycle, the brake fires again immediately, and the book
        oscillates between stables and the basket paying fees each way."""
        a = self._tripped(cfg)
        cash = {"AAA-USDT": 0.0, "BBB-USDT": 0.0}
        a.decide(700.0, cash, 0.6, T0 + timedelta(days=5))     # market sets a new low
        a.decide(700.0, cash, 0.75, T0 + timedelta(days=9))    # +25% off it -> re-enter
        assert a.state.stance is Stance.INVESTED
        assert a.state.peak_equity == pytest.approx(700.0)

        follow_up = a.decide(
            700.0, {"AAA-USDT": 350.0, "BBB-USDT": 350.0}, 0.75, T0 + timedelta(days=10)
        )
        assert follow_up.reason is not RebalanceReason.DE_RISK

    def test_brake_is_off_by_default(self) -> None:
        """Enabling it cost return and did not reduce max drawdown once the
        re-entry bug was fixed; see config.py for the numbers."""
        assert Settings(_env_file=None).max_portfolio_drawdown_pct == 0.0

    def test_disabled_brake_never_de_risks(self, cfg) -> None:
        cfg.max_portfolio_drawdown_pct = 0.0
        a = _alloc(cfg)
        a.decide(1000.0, {}, 1.0, T0)
        d = a.decide(200.0, {"AAA-USDT": 100.0, "BBB-USDT": 100.0}, 0.2, T0 + timedelta(days=3))
        assert d.reason is not RebalanceReason.DE_RISK
        assert a.state.stance is Stance.INVESTED


class TestStatePersistence:
    def test_peak_survives_restart(self, cfg) -> None:
        """A peak recomputed from current equity would re-arm the brake at the
        depressed level on every restart, quietly disabling it."""
        a = _alloc(cfg)
        a.decide(1000.0, {}, 1.0, T0)
        a.decide(2000.0, {"AAA-USDT": 1000.0, "BBB-USDT": 1000.0}, 2.0, T0 + timedelta(days=1))
        saved = a.state

        restarted = Allocator(cfg, saved)
        d = restarted.decide(1600.0, {"AAA-USDT": 800.0, "BBB-USDT": 800.0}, 1.6,
                             T0 + timedelta(days=2))
        assert d.drawdown_pct == pytest.approx(20.0)
        assert restarted.state.peak_equity == pytest.approx(2000.0)


class TestWeightedBaskets:
    """Basket weights are configuration, never a model output — see
    config.allocation_basket for why selection cannot be automated."""

    def _w(self, spec: str) -> dict[str, float]:
        cfg = Settings(_env_file=None, allocation_basket=spec,
                       db_path=":memory:", log_file="")
        return Allocator(cfg, AllocatorState()).target_weights()

    def test_plain_list_is_equal_weight(self) -> None:
        assert self._w("AAA-USDT,BBB-USDT,CCC-USDT") == pytest.approx(
            {"AAA-USDT": 1/3, "BBB-USDT": 1/3, "CCC-USDT": 1/3}
        )

    def test_explicit_weights(self) -> None:
        assert self._w("AAA-USDT:0.7,BBB-USDT:0.3") == pytest.approx(
            {"AAA-USDT": 0.7, "BBB-USDT": 0.3}
        )

    def test_weights_are_normalised(self) -> None:
        """They need not sum to 1; 7 and 3 mean the same as 0.7 and 0.3."""
        assert self._w("AAA-USDT:7,BBB-USDT:3") == pytest.approx(
            {"AAA-USDT": 0.7, "BBB-USDT": 0.3}
        )

    def test_malformed_entry_is_skipped_not_fatal(self) -> None:
        assert self._w("AAA-USDT:oops,BBB-USDT:1") == pytest.approx({"BBB-USDT": 1.0})

    def test_default_basket_is_btc_dominant(self) -> None:
        w = Allocator(Settings(_env_file=None), AllocatorState()).target_weights()
        assert w["BTC-USDT"] > 0.5
        assert sum(w.values()) == pytest.approx(1.0)

    def test_deployment_respects_explicit_weights(self) -> None:
        cfg = Settings(_env_file=None, allocation_basket="AAA-USDT:0.8,BBB-USDT:0.2",
                       min_rebalance_trade_quote=1.0, db_path=":memory:", log_file="")
        d = Allocator(cfg, AllocatorState()).decide(1000.0, {}, 1.0, T0)
        by = {i.symbol: i.delta_quote for i in d.intents}
        assert by["AAA-USDT"] == pytest.approx(800.0)
        assert by["BBB-USDT"] == pytest.approx(200.0)
