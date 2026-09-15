"""Tests for the live allocator runner.

Most of these pin `preflight`, which exists because this project's first
disaster was a silent divergence between intended risk and actual risk: a
rename to `*_PCT` plus `extra="ignore"` dropped three env vars without a word
and applied 80% of balance at up to 10x leverage to a position meant to be 8
USDT.

`extra="forbid"` fixed that specific field. These tests defend the general
case: the runner must refuse to act whenever the account it can see disagrees
with the configuration it was given.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src.allocator import AllocatorState, Stance
from src.allocator_runner import (
    BOOK,
    AccountSnapshot,
    PreflightError,
    _as_float,
    load_state,
    market_index,
    preflight,
    save_state,
)
from src.config import Settings
from src.storage import Storage


@pytest.fixture()
def cfg() -> Settings:
    return Settings(
        _env_file=None,
        allocation_basket="BTC-USDT:0.7,ETH-USDT:0.3",
        max_leverage_allowed=3,
        db_path=":memory:",
        log_file="",
    )


def snap(**over) -> AccountSnapshot:
    base = dict(
        equity_quote=10_000.0,
        holdings_quote={"BTC-USDT": 7_000.0, "ETH-USDT": 3_000.0},
        prices={"BTC-USDT": 60_000.0, "ETH-USDT": 3_000.0},
    )
    base.update(over)
    return AccountSnapshot(**base)


class TestSnapshotArithmetic:
    def test_gross_notional_counts_shorts_by_absolute_size(self) -> None:
        """A short is exposure, not negative exposure, for a risk limit."""
        s = snap(holdings_quote={"A": 5_000.0, "B": -5_000.0})
        assert s.gross_notional == 10_000.0

    def test_effective_leverage_is_measured_not_assumed(self) -> None:
        assert snap().effective_leverage == pytest.approx(1.0)
        assert snap(equity_quote=5_000.0).effective_leverage == pytest.approx(2.0)

    def test_zero_equity_does_not_divide_by_zero(self) -> None:
        assert snap(equity_quote=0.0).effective_leverage == 0.0

    def test_detects_a_short(self) -> None:
        assert not snap().has_short
        assert snap(holdings_quote={"A": -1.0}).has_short


class TestPreflight:
    def test_a_clean_account_passes(self, cfg) -> None:
        preflight(cfg, snap())

    def test_refuses_on_zero_equity(self, cfg) -> None:
        with pytest.raises(PreflightError, match="equity"):
            preflight(cfg, snap(equity_quote=0.0))

    def test_refuses_when_the_account_holds_shorts(self, cfg) -> None:
        """The engine never emits a short. One being present means something
        else is trading the account, and reconciling would fight it."""
        with pytest.raises(PreflightError, match="short"):
            preflight(cfg, snap(holdings_quote={"BTC-USDT": -5_000.0}))

    def test_refuses_above_the_configured_leverage(self, cfg) -> None:
        """The check that the original incident needed: leverage is read from
        the exchange's positions, not inferred from what we meant to do."""
        with pytest.raises(PreflightError, match="leverage"):
            preflight(cfg, snap(equity_quote=1_000.0))   # 10x actual

    def test_refuses_on_an_unparseable_basket(self, cfg) -> None:
        cfg.allocation_basket = ""
        with pytest.raises(PreflightError, match="no weights"):
            preflight(cfg, snap())

    def test_refuses_when_a_basket_member_has_no_price(self, cfg) -> None:
        with pytest.raises(PreflightError, match="no price"):
            preflight(cfg, snap(prices={"BTC-USDT": 60_000.0}))

    def test_reports_every_problem_at_once(self, cfg) -> None:
        """One fix per run is how a shutdown loop happens at 3am."""
        with pytest.raises(PreflightError) as e:
            preflight(cfg, snap(equity_quote=0.0, prices={}))
        assert "equity" in str(e.value) and "price" in str(e.value)


class TestMarketIndex:
    def test_weighted_price_level(self) -> None:
        idx = market_index({"A": 100.0, "B": 200.0}, {"A": 0.5, "B": 0.5})
        assert idx == pytest.approx(150.0)

    def test_missing_price_contributes_zero_rather_than_raising(self) -> None:
        assert market_index({"A": 100.0}, {"A": 0.5, "B": 0.5}) == pytest.approx(50.0)

    def test_it_moves_with_prices_while_equity_is_frozen(self) -> None:
        """The reason this function exists. While defensive the book sits in
        stables and equity does not move, so re-entry judged on equity can
        never fire — the brake becomes a one-way door."""
        w = {"A": 1.0}
        assert market_index({"A": 50.0}, w) < market_index({"A": 100.0}, w)


class TestStatePersistence:
    def test_round_trip(self) -> None:
        db = Storage(":memory:")
        st = AllocatorState(
            stance=Stance.DEFENSIVE, peak_equity=25_000.0,
            defensive_index_low=1_234.5,
            last_rebalance_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
            deployed=True,
        )
        save_state(db, st)
        back = load_state(db)
        assert back.stance is Stance.DEFENSIVE
        assert back.peak_equity == pytest.approx(25_000.0)
        assert back.defensive_index_low == pytest.approx(1_234.5)
        assert back.deployed is True
        assert back.last_rebalance_at == st.last_rebalance_at

    def test_missing_state_is_a_fresh_allocator_not_an_error(self) -> None:
        assert load_state(Storage(":memory:")) == AllocatorState()

    def test_peak_equity_survives_a_restart(self) -> None:
        """If the peak were recomputed from current equity, every restart would
        re-arm the drawdown brake at the already-depressed level and silently
        disable it."""
        db = Storage(":memory:")
        save_state(db, AllocatorState(peak_equity=100_000.0, deployed=True))
        assert load_state(db).peak_equity == pytest.approx(100_000.0)

    def test_decisions_are_append_only(self) -> None:
        db = Storage(":memory:")
        for i in range(3):
            db.log_allocation_decision(BOOK, True, {
                "rebalance": True, "reason": "drift", "stance": "INVESTED",
                "equity_quote": 1000.0 + i, "drawdown_pct": 0.0,
                "turnover_quote": 10.0, "intents": [], "note": f"n{i}",
            })
        n = db._get_conn().execute(
            "SELECT COUNT(*) FROM allocator_decisions WHERE book=?", (BOOK,)
        ).fetchone()[0]
        assert n == 3


class TestFieldParsing:
    """Exchanges disagree on field names, and a missed one must not silently
    become zero equity — which would make every position look infinitely
    levered, or nothing tradeable at all."""

    def test_first_matching_key_wins(self) -> None:
        assert _as_float({"equity": "5"}, "equity", "balance") == 5.0

    def test_falls_through_to_the_next_key(self) -> None:
        assert _as_float({"balance": "7"}, "equity", "balance") == 7.0

    def test_looks_inside_a_data_envelope(self) -> None:
        assert _as_float({"data": {"equity": "9"}}, "equity") == 9.0

    def test_looks_inside_a_balance_envelope(self) -> None:
        assert _as_float({"balance": {"equity": "11"}}, "equity") == 11.0

    def test_unparseable_values_are_skipped_not_crashed(self) -> None:
        assert _as_float({"equity": "abc", "balance": "3"}, "equity", "balance") == 3.0

    def test_nothing_found_is_zero_and_preflight_catches_it(self, cfg) -> None:
        assert _as_float({}, "equity") == 0.0
        with pytest.raises(PreflightError):
            preflight(cfg, snap(equity_quote=_as_float({}, "equity")))


class TestNoLeverageByMeasurement:
    """Leverage was rejected on evidence, not taste: on 8.8 years of daily data
    with funding and commission charged, BTC70/ETH30 returned +39.3%/yr at 1.0x
    and +10.6%/yr at 2.0x, with drawdown deepening from -83% to -99% along the
    way. Growth turns negative at 2.10x."""

    def test_default_config_does_not_invite_leverage(self) -> None:
        assert Settings(_env_file=None).max_leverage_allowed <= 3

    def test_the_allocator_never_emits_leverage_or_shorts(self, cfg) -> None:
        from src.allocator import Allocator

        d = Allocator(cfg, AllocatorState()).decide(10_000.0, {}, 1.0)
        assert sum(i.target_quote for i in d.intents) <= 10_000.0 + 1e-9
        assert all(i.target_quote >= 0 for i in d.intents)
