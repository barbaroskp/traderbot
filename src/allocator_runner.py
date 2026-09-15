"""Runs the allocator against a live BingX account.

WHAT THIS IS
------------
`src/allocator.py` is a pure decision engine with 26 tests and no way to reach
an exchange. This is the missing half: it reads the account, asks the engine
what the book should look like, and turns the answer into orders.

It is the only execution path in this repository that is backed by a measured
result. The signal engine in `strategy.py` is not wired in here and must not
be — roughly fifty directional rules were tested across crypto and Turkish
equities and none survived its own transaction costs.

WHY NO LEVERAGE, WHEN THE BRIEF SAID "AGGRESSIVE"
--------------------------------------------------
Leverage was not rejected on taste. It was measured, on 8.8 years of daily
data, with funding and commission charged:

    BTC 70 / ETH 30      CAGR      maxDD     calmar
        1.00x          +39.3%     -83.2%      0.472
        1.15x          +39.2%     -88.0%      0.445
        1.30x          +37.2%     -91.5%      0.406
        2.00x          +10.6%     -98.7%      0.108

Every increment lowers return AND deepens drawdown. The theoretical
growth-optimal leverage for this basket is L* = (mu-r)/sigma^2 = 1.05x, and
that figure ignores funding; once ~10%/yr of funding on the levered portion and
discrete rebalancing are charged, the optimum lands at 1.00x. Growth turns
negative at 2.10x — at 3x the arithmetic gives -57.8%/yr and at 5x, -326.8%/yr.

So the aggression in this configuration is not leverage. It is being fully
invested in crypto with no cash buffer, which is already a -83% drawdown
strategy. That is a large risk, taken deliberately, with the number known in
advance.

THE INVARIANTS, AND WHY THEY EXIST
-----------------------------------
This project began with a silent catastrophe: a commit renamed three config
fields to `*_PCT`, pydantic was set to `extra="ignore"`, so the old `.env`
entries were dropped without a word and the defaults applied 80% of balance at
up to 10x. An 8 USDT intended position became most of the account.

`extra="forbid"` fixed the specific bug. The checks in `preflight()` below
exist because the class of bug was never about that one field: any path that
lets intended risk and actual risk diverge silently will eventually do this
again. So the runner refuses to start unless the numbers it is about to act on
agree with the numbers it was configured with, and it measures leverage from
the EXCHANGE's reported positions rather than from its own intentions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.allocator import Allocator, AllocatorState, Stance, TradeIntent
from src.bingx_client import BingXClient
from src.config import Settings
from src.logger import get_logger
from src.storage import Storage

log = get_logger(__name__)

BOOK = "crypto_allocator"


class PreflightError(RuntimeError):
    """Raised when the account does not match what the config assumes.

    Deliberately fatal. An allocator that trades through a state it does not
    understand is how a 'small' position becomes the whole account.
    """


@dataclass
class AccountSnapshot:
    equity_quote: float
    holdings_quote: dict[str, float]
    prices: dict[str, float]
    raw_positions: list[dict] = field(default_factory=list)

    @property
    def gross_notional(self) -> float:
        return sum(abs(v) for v in self.holdings_quote.values())

    @property
    def effective_leverage(self) -> float:
        """Measured from the exchange, never from intent."""
        return self.gross_notional / self.equity_quote if self.equity_quote > 0 else 0.0

    @property
    def has_short(self) -> bool:
        return any(v < 0 for v in self.holdings_quote.values())


async def read_account(client: BingXClient, symbols: list[str]) -> AccountSnapshot:
    """Balance, open positions and marks, as the exchange reports them."""
    bal = await client.get_balance()
    equity = _as_float(bal, "equity", "balance", "availableMargin")

    positions = await client.get_positions()
    holdings: dict[str, float] = {}
    for p in positions or []:
        sym = str(p.get("symbol") or "")
        if not sym:
            continue
        notional = _as_float(p, "positionValue", "notional", "positionAmt")
        side = str(p.get("positionSide") or p.get("side") or "").upper()
        if side == "SHORT" or notional < 0:
            notional = -abs(notional)
        holdings[sym] = holdings.get(sym, 0.0) + notional

    prices: dict[str, float] = {}
    for s in symbols:
        try:
            t = await client.get_ticker(s)
            px = _as_float(t, "lastPrice", "price", "close")
            if px > 0:
                prices[s] = px
        except Exception as exc:                      # noqa: BLE001
            log.warning("ticker failed", extra={"symbol": s, "err": str(exc)})

    return AccountSnapshot(equity_quote=equity, holdings_quote=holdings,
                           prices=prices, raw_positions=list(positions or []))


def preflight(cfg: Settings, snap: AccountSnapshot) -> None:
    """Refuse to trade unless the account matches the configuration.

    Every check here corresponds to a way this repository has actually gone
    wrong, or to an assumption the allocator makes that nothing else enforces.
    """
    problems: list[str] = []

    if snap.equity_quote <= 0:
        problems.append(f"equity is {snap.equity_quote} — cannot size anything")

    # The engine emits no shorts by design. If the account holds one, something
    # other than this runner is trading, and reconciling would fight it.
    if snap.has_short:
        shorts = {k: v for k, v in snap.holdings_quote.items() if v < 0}
        problems.append(f"account holds short positions {shorts}; this engine "
                        f"never emits shorts, so another process is trading")

    # Leverage measured from the exchange, compared to what we intend to allow.
    lev = snap.effective_leverage
    if lev > cfg.max_leverage_allowed + 1e-6:
        problems.append(f"account is at {lev:.2f}x gross leverage, above the "
                        f"configured maximum {cfg.max_leverage_allowed}x")

    # The basket must actually parse, or decide() silently does nothing.
    weights = Allocator(cfg, AllocatorState()).target_weights()
    if not weights:
        problems.append(f"allocation_basket {cfg.allocation_basket!r} parsed to "
                        f"no weights")
    missing = [s for s in weights if s not in snap.prices]
    if missing:
        problems.append(f"no price for basket members {missing}")

    if problems:
        raise PreflightError("; ".join(problems))


def market_index(prices: dict[str, float], weights: dict[str, float]) -> float:
    """A single price level for the basket.

    Required by the allocator because re-entry after the drawdown brake must be
    judged on the MARKET, not on equity: while defensive the book sits in
    stables and its equity does not move, so an equity-based recovery test can
    never become true. That bug once made a broken engine look spectacular.
    """
    return sum(prices.get(s, 0.0) * w for s, w in weights.items())


def load_state(db: Storage) -> AllocatorState:
    raw = db.load_allocator_state(BOOK)
    if not raw:
        return AllocatorState()
    last = raw.get("last_rebalance_at")
    return AllocatorState(
        stance=Stance(raw["stance"]),
        peak_equity=float(raw["peak_equity"]),
        defensive_index_low=float(raw["defensive_index_low"]),
        last_rebalance_at=datetime.fromisoformat(last) if last else None,
        deployed=bool(raw["deployed"]),
    )


def save_state(db: Storage, st: AllocatorState) -> None:
    db.save_allocator_state(BOOK, {
        "stance": st.stance.value,
        "peak_equity": st.peak_equity,
        "defensive_index_low": st.defensive_index_low,
        "last_rebalance_at": st.last_rebalance_at.isoformat()
        if st.last_rebalance_at else None,
        "deployed": st.deployed,
    })


async def execute(client: BingXClient, cfg: Settings, intents: list[TradeIntent],
                  prices: dict[str, float], dry_run: bool) -> list[dict]:
    """Turn intents into orders. Sells first, so buys are funded.

    Ordering matters on a margin account: issuing buys before the corresponding
    sells settle can push gross exposure above the limit for the moments in
    between, which is exactly the state `preflight` would refuse to start from.
    """
    done: list[dict] = []
    for intent in sorted(intents, key=lambda i: i.delta_quote):
        px = prices.get(intent.symbol, 0.0)
        if px <= 0:
            log.warning("no price, skipping", extra={"symbol": intent.symbol})
            continue
        qty = abs(intent.delta_quote) / px
        side = "BUY" if intent.delta_quote > 0 else "SELL"
        record = {"symbol": intent.symbol, "side": side, "qty": qty,
                  "price": px, "delta_quote": intent.delta_quote,
                  "target_quote": intent.target_quote}

        if dry_run:
            record["status"] = "DRY_RUN"
        else:
            try:
                resp = await client.place_order(
                    symbol=intent.symbol, side=side,
                    position_side="LONG", order_type="MARKET", quantity=qty,
                )
                record["status"] = "SENT"
                record["response"] = resp
            except Exception as exc:                  # noqa: BLE001
                record["status"] = "FAILED"
                record["error"] = str(exc)
                log.error("order failed", extra=record)
        done.append(record)
        log.info("allocation order", extra=record)
    return done


async def run_once(cfg: Settings, dry_run: bool = True) -> dict:
    db = Storage(cfg.db_path)
    client = BingXClient(cfg)
    try:
        state = load_state(db)
        alloc = Allocator(cfg, state)
        weights = alloc.target_weights()
        snap = await read_account(client, sorted(weights))

        preflight(cfg, snap)

        idx = market_index(snap.prices, weights)
        decision = alloc.decide(snap.equity_quote, snap.holdings_quote, idx)

        orders: list[dict] = []
        if decision.rebalance and decision.intents:
            orders = await execute(client, cfg, decision.intents, snap.prices, dry_run)

        # Persist AFTER acting, and unconditionally: peak_equity moves on any
        # new high, not only on a rebalance, and losing it means losing the
        # drawdown brake's reference point across a restart.
        save_state(db, alloc.state)
        db.log_allocation_decision(BOOK, dry_run, {
            "rebalance": decision.rebalance,
            "reason": decision.reason.value if decision.reason else None,
            "stance": decision.stance.value,
            "equity_quote": snap.equity_quote,
            "drawdown_pct": decision.drawdown_pct,
            "turnover_quote": decision.turnover_quote,
            "intents": [{"symbol": i.symbol, "delta_quote": i.delta_quote,
                         "target_quote": i.target_quote} for i in decision.intents],
            "note": decision.note,
        })
        return {"snapshot": snap, "decision": decision, "orders": orders}
    finally:
        await client.close()
        db.close()


def _as_float(d: dict, *keys: str) -> float:
    """First key that parses as a number. Exchanges disagree on field names and
    a missing one must not silently become zero equity."""
    if not isinstance(d, dict):
        return 0.0
    for scope in (d, d.get("data") if isinstance(d.get("data"), dict) else {},
                  d.get("balance") if isinstance(d.get("balance"), dict) else {}):
        for k in keys:
            v = (scope or {}).get(k)
            if v in (None, ""):
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def main() -> None:
    ap = argparse.ArgumentParser(description="run the allocator against BingX")
    ap.add_argument("--live", action="store_true",
                    help="actually send orders (default is dry run)")
    ap.add_argument("--basket", default=None,
                    help="override allocation_basket, e.g. 'BTC-USDT:0.7,ETH-USDT:0.3'")
    a = ap.parse_args()

    overrides = {}
    if a.basket:
        overrides["allocation_basket"] = a.basket
    cfg = Settings(**overrides) if overrides else Settings()

    try:
        out = asyncio.run(run_once(cfg, dry_run=not a.live))
    except PreflightError as exc:
        print(f"PREFLIGHT FAILED — not trading.\n  {exc}")
        raise SystemExit(2) from None

    snap, dec = out["snapshot"], out["decision"]
    print(f"equity {snap.equity_quote:,.2f} · gross {snap.gross_notional:,.2f} "
          f"({snap.effective_leverage:.2f}x) · stance {dec.stance.value}")
    print(f"drawdown {dec.drawdown_pct:.1f}% · {dec.note}")
    if not out["orders"]:
        print("no action")
    for o in out["orders"]:
        print(f"  {o['status']:8s} {o['side']:4s} {o['symbol']:12s} "
              f"{o['qty']:.6f} @ {o['price']:,.2f}  "
              f"({o['delta_quote']:+,.2f} USDT)")
    if not a.live and out["orders"]:
        print("\ndry run — nothing was sent. Re-run with --live to execute.")


if __name__ == "__main__":
    main()
