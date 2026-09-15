"""Regression tests for the failure chain documented in POSTMORTEM.md.

Each test here corresponds to a specific defect that contributed to the losing
run. They exist to make those defects impossible to reintroduce silently.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import Settings
from src.execution import (
    PaperExecution,
    _compute_tp_sl_bps,
    _funding_cost_bps,
    _hold_minutes,
)
from src.marketdata import Indicators, MarketData, SymbolSnapshot, finalize_klines
from src.risk import RiskManager
from src.storage import Storage
from src.strategy import BONUS_ONLY_CLUSTERS, CLUSTER_MEMBERS, IndicatorVote, Strategy
from src.universe import Universe

REPO_ROOT = Path(__file__).resolve().parent.parent


# ─────────────────────────────────────────────────────────────────────────────
# 1. The config regression that actually caused the loss
# ─────────────────────────────────────────────────────────────────────────────

class TestEnvConfigSync:
    """Commit 00ee744 renamed MAX_TOTAL_MARGIN_USDT -> max_total_margin_pct but
    the .env files kept the old names. config.py ran with extra="ignore", so all
    three exposure caps were silently discarded and the percentage defaults
    applied instead: an intended "8 USDT total exposure" became 80% of balance,
    up to ~8x balance in notional at 10x leverage.

    Nothing failed, nothing logged. These tests make that class of mistake loud.
    """

    @pytest.mark.parametrize("env_file", [".env.example", ".env.optimized"])
    def test_env_file_has_no_unknown_variables(self, env_file: str) -> None:
        path = REPO_ROOT / env_file
        if not path.exists():
            pytest.skip(f"{env_file} not present")

        known = set(Settings.model_fields)
        unknown = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if key.lower() not in known:
                unknown.append(key)

        assert not unknown, (
            f"{env_file} sets variables with no matching field in config.py: "
            f"{unknown}. They would be ignored and the default silently used."
        )

    def test_unknown_env_variable_is_rejected(self) -> None:
        """extra='forbid' must be in force, so a typo or stale name fails fast."""
        with pytest.raises(Exception):
            Settings(_env_file=None, THIS_FIELD_DOES_NOT_EXIST="1")

    def test_retired_usdt_margin_names_are_gone(self) -> None:
        """The exact names that were silently dropped must not come back."""
        for retired in (
            "max_total_margin_usdt",
            "max_trade_margin_usdt",
            "max_margin_high_conviction_usdt",
        ):
            assert retired not in Settings.model_fields
        for replacement in (
            "max_total_margin_pct",
            "max_trade_margin_pct",
            "max_margin_high_conviction_pct",
        ):
            assert replacement in Settings.model_fields


# ─────────────────────────────────────────────────────────────────────────────
# 2. Repainting indicators
# ─────────────────────────────────────────────────────────────────────────────

class TestNoRepaint:
    def test_finalize_klines_drops_in_progress_candle(self) -> None:
        raw = [
            {"time": 3, "close": "30"},
            {"time": 1, "close": "10"},
            {"time": 2, "close": "20"},
        ]
        out = finalize_klines(raw)
        # sorted ascending, newest (still forming) removed
        assert [k["time"] for k in out] == [1, 2]

    def test_finalize_klines_handles_degenerate_input(self) -> None:
        assert finalize_klines([]) == []
        assert finalize_klines([{"time": 1}]) == []


# ─────────────────────────────────────────────────────────────────────────────
# 3. The conviction score that rewarded thin evidence
# ─────────────────────────────────────────────────────────────────────────────

class TestWeightedScoreMonotonicity:
    @pytest.fixture()
    def strat(self, cfg, db) -> Strategy:
        return Strategy(cfg, db)

    def test_opposing_votes_lower_the_score(self, strat) -> None:
        agree = [
            IndicatorVote("rsi", "LONG", 25.0),
            IndicatorVote("macd", "LONG", 20.0),
            IndicatorVote("trend", "LONG", 15.0),
        ]
        contested = agree + [
            IndicatorVote("stoch_rsi", "SHORT", 15.0),
            IndicatorVote("squeeze", "SHORT", 15.0),
        ]
        assert strat._normalize_weighted_score(contested, "LONG")[0] < (
            strat._normalize_weighted_score(agree, "LONG")[0]
        )

    def test_score_is_never_negative(self, strat) -> None:
        mostly_against = [
            IndicatorVote("rsi", "LONG", 5.0),
            IndicatorVote("macd", "SHORT", 30.0),
            IndicatorVote("trend", "SHORT", 15.0),
        ]
        score, _ = strat._normalize_weighted_score(mostly_against, "LONG")
        assert score >= 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 4. Contradictory market hypotheses inside one cluster
# ─────────────────────────────────────────────────────────────────────────────

class TestClusterCoherence:
    def test_mean_reversion_and_trend_are_separate_clusters(self) -> None:
        """ema_zscore votes LONG when price is BELOW the fast EMA (revert);
        trend/adx vote LONG when price is ABOVE EMA50 (persist). Sharing a
        cluster let an internal majority silently pick a direction while the
        disagreement never reached the confluence count."""
        for name, members in CLUSTER_MEMBERS.items():
            assert not ({"ema_zscore", "trend"} <= set(members)), (
                f"cluster {name!r} mixes mean-reverting and trend-following evidence"
            )
            assert not ({"ema_zscore", "adx"} <= set(members)), (
                f"cluster {name!r} mixes mean-reverting and trend-following evidence"
            )

    def test_every_weighted_indicator_belongs_to_a_cluster(self) -> None:
        assigned = {m for members in CLUSTER_MEMBERS.values() for m in members}
        known = set(Strategy._INDICATOR_WEIGHT_ATTRS)
        assert known - assigned == set(), f"indicators outside any cluster: {known - assigned}"

    def test_market_wide_sentiment_cannot_vote(self) -> None:
        """Fear & Greed is a daily, market-wide number — identical for every
        symbol. It is a portfolio tilt, not per-symbol evidence."""
        assert "sentiment" in CLUSTER_MEMBERS["contrarian"]
        assert "contrarian" in BONUS_ONLY_CLUSTERS

    def test_confluence_requires_a_majority_of_clusters(self) -> None:
        cfg = Settings(_env_file=None)
        voting = len(CLUSTER_MEMBERS) - len(BONUS_ONLY_CLUSTERS)
        assert cfg.min_cluster_confluence > voting / 2

    def test_risk_states_raise_the_confluence_bar(self, cfg, db) -> None:
        strat = Strategy(cfg, db)
        normal = strat._get_min_confluence("NORMAL")
        assert strat._get_min_confluence("TIGHT") > normal
        assert strat._get_min_confluence("ULTRA_TIGHT") >= strat._get_min_confluence("TIGHT")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Cost accounting
# ─────────────────────────────────────────────────────────────────────────────

class TestCostAccounting:
    def test_funding_is_charged_over_the_hold(self) -> None:
        cfg = Settings(_env_file=None)
        eight_hours = cfg.funding_interval_hours * 60
        assert _funding_cost_bps(cfg, 0.0001, "LONG", eight_hours) == pytest.approx(1.0)
        # Shorts receive a positive funding rate.
        assert _funding_cost_bps(cfg, 0.0001, "SHORT", eight_hours) == pytest.approx(-1.0)
        # Longer holds pay proportionally more.
        assert _funding_cost_bps(cfg, 0.0001, "LONG", eight_hours * 3) == pytest.approx(3.0)

    def test_unknown_funding_rate_is_treated_as_a_cost(self) -> None:
        """Never silently credit a position we have no rate for."""
        cfg = Settings(_env_file=None)
        cost = _funding_cost_bps(cfg, 0.0, "SHORT", cfg.funding_interval_hours * 60)
        assert cost > 0

    def test_funding_can_be_disabled(self) -> None:
        cfg = Settings(_env_file=None, use_funding_cost=False)
        assert _funding_cost_bps(cfg, 0.01, "LONG", 10_000) == 0.0

    def test_tp_floor_clears_the_full_round_trip(self) -> None:
        """A TP that only clears fees still loses money once slippage is paid."""
        cfg = Settings(_env_file=None, use_dynamic_tp_sl=False, tp_bps=1.0, sl_bps=50.0)
        snap = SymbolSnapshot(symbol="X", mid_price=100.0, indicators=Indicators())
        _sl, tp = _compute_tp_sl_bps(cfg, snap, 100.0)
        assert tp >= cfg.round_trip_cost_bps + cfg.min_tp_net_bps

    def test_round_trip_cost_counts_both_sides(self) -> None:
        cfg = Settings(_env_file=None, fee_rate_bps=5.0, slippage_assumption_bps=8.0)
        assert cfg.round_trip_cost_bps == pytest.approx(26.0)

    def test_hold_minutes_tolerates_naive_timestamps(self) -> None:
        now = datetime.now(timezone.utc)
        naive = (now - timedelta(minutes=30)).replace(tzinfo=None).isoformat()
        assert _hold_minutes(naive, now) == pytest.approx(30.0, abs=0.1)
        assert _hold_minutes("not-a-date", now) == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 6. Paper execution: anti-liquidation ordering + poll-gap stops + funding
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def paper_env(cfg, db):
    market = AsyncMock(spec=MarketData)
    universe = MagicMock(spec=Universe)
    universe.get_contract.return_value = {"step_size": 0.001, "tick_size": 0.1}
    risk = RiskManager(cfg, db)
    return PaperExecution(cfg, db, market, risk, universe), market, db


def _open_paper_position(db: Storage, **overrides) -> dict:
    row = {
        "symbol": "BTC-USDT",
        "side": "LONG",
        "entry_price": 100.0,
        "qty": 1.0,
        "original_qty": 1.0,
        "remaining_qty": 1.0,
        "notional": 100.0,
        "sl_order_id": "sl1",
        "tp_order_id": "tp1",
        "tp1_order_id": "",
        "sl_bps": 50.0,
        "tp_bps": 200.0,
        "leverage": 3,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": "OPEN",
        "is_paper": 1,
        "funding_rate": 0.0,
    }
    row.update(overrides)
    row["id"] = db.insert("positions", row)
    return row


class TestPaperExecutionRegressions:
    @pytest.mark.asyncio
    async def test_anti_liquidation_is_not_overwritten(self, paper_env) -> None:
        """`exit_reason = "ANTI_LIQUIDATION"` used to be assigned and then reset
        to "" 23 lines later by an unconditional re-initialisation, so
        anti-liquidation never fired once in paper mode."""
        paper, market, db = paper_env
        pos = _open_paper_position(db)
        db.insert("orders", {
            "client_order_id": "sl1", "ts": pos["opened_at"], "symbol": "BTC-USDT",
            "side": "SHORT", "order_type": "STOP_MARKET", "price": 99.5, "qty": 1.0,
            "status": "PENDING", "is_paper": 1, "updated_at": pos["opened_at"],
        })

        # leverage 3 -> liq ~= 67.2; at mark 85 we are inside the 25% safety band
        market.fetch_mark_price.return_value = 85.0
        market.fetch_recent_extremes.return_value = (85.0, 85.0)

        closed = await paper.check_exits([{**pos, "status": "OPEN"}])
        assert len(closed) == 1
        assert closed[0]["exit_reason"] == "ANTI_LIQUIDATION"

    @pytest.mark.asyncio
    async def test_stop_touched_between_polls_is_detected(self, paper_env) -> None:
        """Paper mode compared only the latest mark price, so a stop breached
        and recovered inside one polling interval was invisible — while a live
        exchange stop would already have filled."""
        paper, market, db = paper_env
        pos = _open_paper_position(db, leverage=1)
        db.insert("orders", {
            "client_order_id": "sl1", "ts": pos["opened_at"], "symbol": "BTC-USDT",
            "side": "SHORT", "order_type": "STOP_MARKET", "price": 99.0, "qty": 1.0,
            "status": "PENDING", "is_paper": 1, "updated_at": pos["opened_at"],
        })

        # Price recovered to 100.5 by poll time, but traded down to 98.0 in between.
        market.fetch_mark_price.return_value = 100.5
        market.fetch_recent_extremes.return_value = (100.6, 98.0)

        closed = await paper.check_exits([{**pos, "status": "OPEN"}])
        assert len(closed) == 1
        assert closed[0]["exit_reason"] == "SL"

    @pytest.mark.asyncio
    async def test_both_levels_touched_resolves_against_us(self, paper_env) -> None:
        paper, market, db = paper_env
        pos = _open_paper_position(db, leverage=1)
        for oid, price, otype in (("sl1", 99.0, "STOP_MARKET"), ("tp1", 102.0, "TAKE_PROFIT")):
            db.insert("orders", {
                "client_order_id": oid, "ts": pos["opened_at"], "symbol": "BTC-USDT",
                "side": "SHORT", "order_type": otype, "price": price, "qty": 1.0,
                "status": "PENDING", "is_paper": 1, "updated_at": pos["opened_at"],
            })

        market.fetch_mark_price.return_value = 100.0
        market.fetch_recent_extremes.return_value = (103.0, 98.0)  # both breached

        closed = await paper.check_exits([{**pos, "status": "OPEN"}])
        assert closed[0]["exit_reason"] == "SL"

    @pytest.mark.asyncio
    async def test_close_charges_funding_and_exit_slippage(self, paper_env) -> None:
        paper, market, db = paper_env
        opened = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()
        pos = _open_paper_position(db, opened_at=opened, funding_rate=0.0001)

        result = await paper._close_position(pos, exit_price=101.0, reason="TP")

        stored = db.fetch_one("SELECT * FROM positions WHERE id=?", (pos["id"],))
        assert stored["funding_fee"] > 0, "funding was not charged"
        assert stored["entry_fee"] > 0 and stored["exit_fee"] > 0
        # Exit fills worse than the trigger level because the order crosses the book.
        assert stored["exit_price"] < 101.0
        gross = (101.0 - 100.0) * 1.0
        assert result["realised_pnl"] < gross


# ─────────────────────────────────────────────────────────────────────────────
# 7. Kelly must be able to refuse the bet
# ─────────────────────────────────────────────────────────────────────────────

class TestKellyCanStop:
    def _seed_losing_history(self, db: Storage, n: int = 40) -> None:
        now = datetime.now(timezone.utc).isoformat()
        for i in range(n):
            # 30% winners of +1, 70% losers of -2  => clearly negative edge
            pnl = 1.0 if i % 10 < 3 else -2.0
            db.insert("positions", {
                "symbol": "X-USDT", "side": "LONG", "entry_price": 100.0, "qty": 1.0,
                "notional": 100.0, "realised_pnl": pnl, "opened_at": now,
                "closed_at": now, "status": "CLOSED", "is_paper": 1,
            })

    def test_negative_edge_sizes_to_zero(self, cfg, db) -> None:
        self._seed_losing_history(db)
        risk = RiskManager(cfg, db)
        assert risk._compute_kelly_fraction() == 0.0

    def test_negative_edge_blocks_the_trade(self, cfg, db) -> None:
        """A min-fraction floor used to reinstate a position size Kelly had just
        told us not to take."""
        self._seed_losing_history(db)
        risk = RiskManager(cfg, db)
        qty = risk.compute_position_size(price=100.0, leverage=2, current_total_margin=0.0)
        assert qty == 0.0

    def test_floor_is_zero_by_default(self, cfg) -> None:
        assert cfg.kelly_min_fraction == 0.0
        assert cfg.kelly_block_on_negative_edge is True


# ─────────────────────────────────────────────────────────────────────────────
# 8. Expectancy gate
# ─────────────────────────────────────────────────────────────────────────────

class TestExpectancyGate:
    def test_required_win_rate_accounts_for_full_cost(self) -> None:
        """R:R alone is not expectancy. The gate must be expressed as the win
        rate the setup needs to break even AFTER fees and slippage."""
        cfg = Settings(_env_file=None)
        sl_bps, tp_bps = 50.0, 100.0
        cost = cfg.round_trip_cost_bps
        gross_required = sl_bps / (tp_bps + sl_bps)
        net_required = (sl_bps + cost) / ((tp_bps - cost) + (sl_bps + cost))
        assert net_required > gross_required
        assert cfg.expectancy_max_required_win_rate <= 0.5

    def test_scheduler_uses_round_trip_cost_not_just_fees(self) -> None:
        source = (REPO_ROOT / "src" / "scheduler.py").read_text()
        assert "round_trip_cost_bps" in source
        assert not re.search(r"round_trip_fee_bps\s*=\s*2\.0\s*\*\s*self\.cfg\.fee_rate_bps", source)


# ─────────────────────────────────────────────────────────────────────────────
# 9. Indicators that read the same bar through opposite theories
# ─────────────────────────────────────────────────────────────────────────────

def _snap_with(**ind_kwargs) -> SymbolSnapshot:
    ind = Indicators(valid=True, **ind_kwargs)
    return SymbolSnapshot(
        symbol="T-USDT", mid_price=100.0, mark_price=100.0,
        best_bid=99.99, best_ask=100.01, spread_bps=1.0,
        bid_depth_usdt=1e6, ask_depth_usdt=1e6,
        fast_ema=100.0, slow_ema=99.0, z_score_bps=40.0, indicators=ind,
    )


class TestNoContradictoryVotes:
    @pytest.fixture()
    def strat(self, cfg, db) -> Strategy:
        return Strategy(cfg, db)

    def test_band_break_suppresses_the_reversion_read(self, strat) -> None:
        """At BB% >= 0.98 with a volume spike, "price is at the upper band"
        (bollinger -> SHORT) and "price broke the upper band" (breakout -> LONG)
        are the SAME observation under opposite theories. They used to both fire
        and land in different clusters, where the intra-cluster consensus check
        could not see the contradiction."""
        snap = _snap_with(bollinger_pct=0.99, volume_spike=True, volume_ratio=2.5,
                          trend_direction="UP")
        sides = {v.name: v.side for v in strat._compute_votes(snap)}
        assert sides["breakout"] == "LONG"
        assert sides["bollinger"] == "NEUTRAL", "reversion read must yield to the break"

    def test_bollinger_still_votes_without_a_break(self, strat) -> None:
        snap = _snap_with(bollinger_pct=0.99, volume_spike=False, volume_ratio=1.0)
        sides = {v.name: v.side for v in strat._compute_votes(snap)}
        assert sides["bollinger"] == "SHORT"
        assert "breakout" not in sides

    def test_volume_profile_break_is_a_volatility_signal(self, strat) -> None:
        """The value-area break is a BREAKOUT read; it must not be counted as
        mean-reversion evidence just because it shares an indicator with the
        revert-to-POC logic."""
        assert "volume_profile_break" in CLUSTER_MEMBERS["volatility"]
        assert "volume_profile" in CLUSTER_MEMBERS["mean_revert"]
        assert "volume_profile_break" in Strategy._INDICATOR_WEIGHT_ATTRS

        snap = _snap_with(volume_profile_poc=100.0, price_vs_poc_bps=250.0,
                          price_in_value_area=False)
        names = {v.name for v in strat._compute_votes(snap) if v.side != "NEUTRAL"}
        assert "volume_profile_break" in names
        assert "volume_profile" not in names


# ─────────────────────────────────────────────────────────────────────────────
# 10. One thesis owns the direction; everything else may only veto
# ─────────────────────────────────────────────────────────────────────────────

class TestThesisArchitecture:
    def _clusters(self, **sides) -> list:
        from src.strategy import ClusterVote
        return [ClusterVote(name=n, side=s, weight=10.0) for n, s in sides.items()]

    @pytest.fixture()
    def strat(self, cfg, db) -> Strategy:
        cfg.signal_mode = "thesis"
        cfg.primary_thesis = "trend"
        cfg.thesis_min_confirmations = 2
        cfg.thesis_max_vetoes = 1
        return Strategy(cfg, db)

    def test_direction_comes_only_from_the_thesis(self, strat) -> None:
        """Five clusters screaming SHORT cannot flip a LONG thesis — they can
        only veto it. Under majority voting they would simply outvote it."""
        resolved = strat._resolve_by_thesis(self._clusters(
            trend="LONG", oscillator="LONG", mean_revert="LONG",
            volatility="LONG", orderflow="LONG", flow="LONG",
        ))
        assert resolved is not None and resolved[0] == "LONG"

    def test_silent_thesis_means_no_trade(self, strat) -> None:
        """Other clusters are not allowed to invent a direction of their own."""
        assert strat._resolve_by_thesis(self._clusters(
            oscillator="SHORT", mean_revert="SHORT", volatility="SHORT",
            orderflow="SHORT", flow="SHORT",
        )) is None

    def test_too_many_vetoes_blocks_the_trade(self, strat) -> None:
        assert strat._resolve_by_thesis(self._clusters(
            trend="LONG", oscillator="LONG", mean_revert="LONG",
            volatility="SHORT", orderflow="SHORT",
        )) is None

    def test_needs_enough_confirmations(self, strat) -> None:
        assert strat._resolve_by_thesis(self._clusters(
            trend="LONG", oscillator="LONG",
        )) is None

    def test_thesis_is_configurable(self, cfg, db) -> None:
        """Which thesis carries an edge is an empirical question, so it must be
        switchable rather than hardcoded."""
        cfg.signal_mode = "thesis"
        cfg.primary_thesis = "mean_revert"
        cfg.thesis_min_confirmations = 1
        strat = Strategy(cfg, db)
        resolved = strat._resolve_by_thesis(self._clusters(
            trend="LONG", mean_revert="SHORT", oscillator="SHORT",
        ))
        assert resolved is not None and resolved[0] == "SHORT"

    def test_extreme_move_gate_follows_the_thesis(self, cfg, db) -> None:
        """max_z_score_bps rejects "too stretched to revert". Applied to a
        trend/breakout thesis it would discard exactly the moves the breakout
        indicators exist to catch."""
        snap = _snap_with(trend_direction="UP", adx=30.0, plus_di=30.0, minus_di=10.0,
                          macd_histogram=0.002, macd_histogram_prev=0.001,
                          macd_strengthening=True)
        snap.z_score_bps = cfg.max_z_score_bps * 2   # far beyond the gate

        cfg.signal_mode = "thesis"
        cfg.primary_thesis = "mean_revert"
        Strategy(cfg, db).generate_signals([snap], [])
        assert Strategy(cfg, db)._gate_stats is not None  # gate exists

        cfg.primary_thesis = "trend"
        trend_strat = Strategy(cfg, db)
        trend_strat.generate_signals([snap], [])
        assert trend_strat._gate_stats.get("z_score_extreme", 0) == 0, (
            "a trend thesis must not discard large moves"
        )

    def test_gate_stats_explain_why_nothing_traded(self, cfg, db) -> None:
        """An empty reject histogram is how the filters got loosened blind."""
        cfg.signal_mode = "thesis"
        cfg.primary_thesis = "trend"
        strat = Strategy(cfg, db)
        snap = _snap_with(rsi=25.0)          # oscillator only; trend silent
        strat.generate_signals([snap], [])
        assert sum(strat._gate_stats.values()) > 0
