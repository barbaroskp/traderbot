#!/usr/bin/env python3
"""BingX Trading Bot - Dashboard v2"""

import sqlite3
import subprocess
import asyncio
import os
from datetime import datetime, timezone
from src.config import load_config
from src.bingx_client import BingXClient


async def dashboard():
    cfg = load_config()
    initial_capital = cfg.initial_capital_usdt
    client = BingXClient(cfg)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    mode_is_paper = 0 if cfg.is_live() else 1

    bal = (await client.get_balance()).get("balance", {})
    balance = float(bal.get("balance", 0))
    equity = float(bal.get("equity", 0))
    unrealised = float(bal.get("unrealizedProfit", 0))
    available = float(bal.get("availableMargin", 0))
    used = float(bal.get("usedMargin", 0))

    positions = await client.get_positions()
    active = [p for p in positions if float(p.get("positionAmt", 0)) != 0]

    # Leverage per position from DB (we store it when opening)
    open_pos_db = conn.execute(
        "SELECT symbol, side, leverage FROM positions WHERE status='OPEN' AND is_paper=?",
        (mode_is_paper,),
    ).fetchall()
    leverage_by_key = {(str(r["symbol"]), str(r["side"]).upper()): (r["leverage"] or cfg.leverage) for r in open_pos_db}

    orders = await client.get_open_orders()

    # ── DB stats ───────────────────────────────────────────────
    total_closed = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE status='CLOSED' AND is_paper=?",
        (mode_is_paper,),
    ).fetchone()[0]
    realised_pnl = conn.execute(
        "SELECT COALESCE(SUM(realised_pnl),0) FROM positions WHERE status='CLOSED' AND is_paper=?",
        (mode_is_paper,),
    ).fetchone()[0]
    wins = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE status='CLOSED' AND is_paper=? AND realised_pnl > 0",
        (mode_is_paper,),
    ).fetchone()[0]
    losses = total_closed - wins

    # Best / worst trade
    best = conn.execute(
        "SELECT symbol, side, realised_pnl FROM positions WHERE status='CLOSED' AND is_paper=? "
        "ORDER BY realised_pnl DESC LIMIT 1",
        (mode_is_paper,),
    ).fetchone()
    worst = conn.execute(
        "SELECT symbol, side, realised_pnl FROM positions WHERE status='CLOSED' AND is_paper=? "
        "ORDER BY realised_pnl ASC LIMIT 1",
        (mode_is_paper,),
    ).fetchone()

    # Avg win / avg loss
    avg_win = conn.execute(
        "SELECT COALESCE(AVG(realised_pnl),0) FROM positions WHERE status='CLOSED' "
        "AND is_paper=? AND realised_pnl > 0",
        (mode_is_paper,),
    ).fetchone()[0]
    avg_loss = conn.execute(
        "SELECT COALESCE(AVG(realised_pnl),0) FROM positions WHERE status='CLOSED' "
        "AND is_paper=? AND realised_pnl <= 0",
        (mode_is_paper,),
    ).fetchone()[0]

    # Total long / short
    long_count = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE status='CLOSED' AND is_paper=? AND side='LONG'",
        (mode_is_paper,),
    ).fetchone()[0]
    short_count = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE status='CLOSED' AND is_paper=? AND side='SHORT'",
        (mode_is_paper,),
    ).fetchone()[0]
    long_pnl = conn.execute(
        "SELECT COALESCE(SUM(realised_pnl),0) FROM positions WHERE status='CLOSED' "
        "AND is_paper=? AND side='LONG'",
        (mode_is_paper,),
    ).fetchone()[0]
    short_pnl = conn.execute(
        "SELECT COALESCE(SUM(realised_pnl),0) FROM positions WHERE status='CLOSED' "
        "AND is_paper=? AND side='SHORT'",
        (mode_is_paper,),
    ).fetchone()[0]

    # First trade time
    first_trade = conn.execute(
        "SELECT opened_at FROM positions WHERE is_paper=? ORDER BY opened_at ASC LIMIT 1",
        (mode_is_paper,),
    ).fetchone()
    if first_trade and first_trade["opened_at"]:
        started = first_trade["opened_at"][:16]
    else:
        started = "N/A"

    # ── Calculations ───────────────────────────────────────────
    total_live_pnl = sum(float(p.get("unrealizedProfit", 0)) for p in active)
    total_pnl = realised_pnl + total_live_pnl
    total_roi = (total_pnl / initial_capital) * 100 if initial_capital > 0 else 0
    equity_change = equity - initial_capital
    winrate = (wins / total_closed * 100) if total_closed > 0 else 0
    profit_factor = (abs(avg_win * wins) / abs(avg_loss * losses)) if losses > 0 and avg_loss != 0 else 0
    total_open = len(active)
    total_orders = len(orders)
    sl_count = len([o for o in orders if o.get("type") == "STOP_MARKET"])
    tp_count = len([o for o in orders if o.get("type") == "TAKE_PROFIT_MARKET"])

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    W = 118
    print()
    print("=" * W)
    print(f"  BINGX FUTURES DASHBOARD  |  {now_str}")
    print("=" * W)
    print()
    print("  HESAP")
    print("  " + "-" * 56)
    print(f"  Baslangic: {initial_capital:>8.2f}   Bakiye: {balance:>8.2f}   Equity: {equity:>8.2f} ({equity_change:+.2f})")
    print(f"  Margin: {used:>8.2f}   Kullanilabilir: {available:>8.2f}   Acik PnL: {unrealised:>+.4f}")
    print("  " + "-" * 56)
    print()
    print("  PERFORMANS")
    print("  " + "-" * 56)
    print(f"  Realize: {realised_pnl:>+.4f}   Acik: {total_live_pnl:>+.4f}   TOPLAM: {total_pnl:>+.4f} USDT   ROI: {total_roi:>+.2f}%")
    print(f"  Baslangic: {started}")
    print("  " + "-" * 56)

    # ── ACIK POZISYONLAR (kaldıraç ile) ────────────────────────
    print()
    print(f"  ACIK POZISYONLAR ({total_open})  |  SL: {sl_count}  TP: {tp_count}  Emir: {total_orders}")
    print("  " + "-" * 114)
    if active:
        print(f"  {'#':<2} {'Symbol':<14} {'Yon':<6} {'Lev':>4} {'Giris':>10} {'Simdi':>10} {'TP':>10} {'SL':>10} {'PnL':>10} {'TP%':>6} {'SL%':>6}")
        print("  " + "-" * 114)
        for i, p in enumerate(active, 1):
            symbol = p.get("symbol", "")
            side = str(p.get("positionSide", "")).upper() or "LONG"
            lev = leverage_by_key.get((symbol, side), cfg.leverage)
            entry = float(p.get("avgPrice", 0))
            mark = float(p.get("markPrice", 0))
            pnl = float(p.get("unrealizedProfit", 0))

            sym_orders = [o for o in orders if o.get("symbol") == symbol]
            sl_orders = [o for o in sym_orders if o.get("type") == "STOP_MARKET"]
            tp_orders = [o for o in sym_orders if o.get("type") == "TAKE_PROFIT_MARKET"]

            sl_str = f"{float(sl_orders[0]['stopPrice']):.6f}" if sl_orders else "  YOK!"
            tp_str = f"{float(tp_orders[0]['stopPrice']):.6f}" if tp_orders else "  YOK!"

            if mark > 0:
                if side == "LONG":
                    tp_pct = ((float(tp_orders[0]["stopPrice"]) - mark) / mark * 100) if tp_orders else 0
                    sl_pct = ((mark - float(sl_orders[0]["stopPrice"])) / mark * 100) if sl_orders else 0
                else:
                    tp_pct = ((mark - float(tp_orders[0]["stopPrice"])) / mark * 100) if tp_orders else 0
                    sl_pct = ((float(sl_orders[0]["stopPrice"]) - mark) / mark * 100) if sl_orders else 0
            else:
                tp_pct = sl_pct = 0

            print(
                f"  {i:<2} {symbol:<14} {side:<6} {lev:>3}x {entry:>10.6f} {mark:>10.6f}"
                f" {tp_str:>10} {sl_str:>10} {pnl:>+9.4f}$ {tp_pct:>+5.2f}% {sl_pct:>+5.2f}%"
            )
    else:
        print("  (Acik pozisyon yok)")

    # ── ISTATISTIK ──────────────────────────────────────────────
    print()
    print("  ISLEM ISTATISTIKLERI")
    print("  " + "-" * 56)
    print(f"  Toplam: {total_closed}   Kazanan: {wins} ({winrate:.1f}%)   Kaybeden: {losses}")
    print(f"  Ort. kazanc: {avg_win:>+.4f}   Ort. kayip: {avg_loss:>+.4f}   Profit factor: {profit_factor:.2f}")
    if best:
        print(f"  En iyi:   {best['symbol']} {best['side']} {best['realised_pnl']:>+.4f} USDT")
    if worst:
        print(f"  En kotu:  {worst['symbol']} {worst['side']} {worst['realised_pnl']:>+.4f} USDT")
    print(f"  Long:  {long_count} islem  PnL {long_pnl:>+.4f}   |   Short: {short_count} islem  PnL {short_pnl:>+.4f}")
    print("  " + "-" * 56)

    # ── SON KAPATILAN ──────────────────────────────────────────
    print()
    closed = conn.execute(
        "SELECT symbol, side, entry_price, realised_pnl, closed_at "
        "FROM positions WHERE status='CLOSED' AND is_paper=? ORDER BY closed_at DESC LIMIT 10",
        (mode_is_paper,),
    ).fetchall()
    if closed:
        print("  SON KAPATILAN (10)")
        print("  " + "-" * 72)
        print(f"  {'Symbol':<14} {'Yon':<6} {'Giris':>10} {'PnL':>12} {'Kapanis':>20}")
        print("  " + "-" * 72)
        for c in closed:
            pnl = c["realised_pnl"] or 0
            t = (c["closed_at"] or "")[:19]
            print(f"  {c['symbol']:<14} {c['side']:<6} {c['entry_price']:>10.6f} {pnl:>+11.4f}$ {t:>20}")
    print()

    # ── RISK & BOT ────────────────────────────────────────────
    risk_state_row = conn.execute(
        "SELECT state, reason, ts FROM risk_state_log ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    current_risk = risk_state_row["state"] if risk_state_row else "NORMAL"
    risk_reason = risk_state_row["reason"] if risk_state_row else "-"
    risk_ts = risk_state_row["ts"] if risk_state_row else "-"

    _db_abs = os.path.abspath(cfg.db_path)
    _service_name = getattr(cfg, "systemd_service_name", "bingx-agent")
    _run = subprocess.run(
        ["systemctl", "is-active", _service_name], capture_output=True, text=True
    )
    _service_ok = (_run.stdout or "").strip() in ("active", "activating")
    if _service_ok and risk_ts != "-":
        try:
            _log_time = datetime.fromisoformat(risk_ts.replace("Z", "+00:00"))
            if _log_time.tzinfo is None:
                _log_time = _log_time.replace(tzinfo=timezone.utc)
            _age_min = (datetime.now(timezone.utc) - _log_time).total_seconds() / 60
            if _age_min > 25:
                current_risk = "NORMAL"
                risk_reason = "restart"
        except Exception:
            pass

    print("  RISK & BOT")
    print("  " + "-" * 56)
    risk_icon = {"NORMAL": "NORMAL", "TIGHT": "TIGHT", "ULTRA_TIGHT": "ULTRA_TIGHT"}
    print(f"  Risk: {risk_icon.get(current_risk, current_risk)}   Sebep: {risk_reason}")
    status = (_run.stdout or "").strip()
    running = status in ("active", "activating")
    print(f"  Servis: {'CALISIYOR' if running else 'DURDU'}")
    print(f"  Scan: {cfg.scan_interval_minutes} dk   Max pos: {cfg.max_open_positions}   Leverage: {cfg.leverage}x   TP: {cfg.tp_bps} bps   SL: {cfg.sl_bps} bps")
    print(f"  DB: {_db_abs}")
    print("  " + "-" * 56)
    print()
    print("=" * W)

    conn.close()
    await client.close()


if __name__ == "__main__":
    asyncio.run(dashboard())
