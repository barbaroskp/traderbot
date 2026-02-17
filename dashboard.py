#!/usr/bin/env python3
"""BingX Trading Bot - Dashboard v2"""

import sqlite3
import subprocess
import asyncio
from datetime import datetime, timezone
from src.config import load_config
from src.bingx_client import BingXClient


INITIAL_CAPITAL = 20.0  # baslangic sermayesi


async def dashboard():
    cfg = load_config()
    client = BingXClient(cfg)
    # Bot ile aynı DB'yi kullan (config / .env'deki DB_PATH)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    mode_is_paper = 0 if cfg.is_live() else 1

    # ── Balance ────────────────────────────────────────────────
    bal = (await client.get_balance()).get("balance", {})
    balance = float(bal.get("balance", 0))
    equity = float(bal.get("equity", 0))
    unrealised = float(bal.get("unrealizedProfit", 0))
    available = float(bal.get("availableMargin", 0))
    used = float(bal.get("usedMargin", 0))

    # ── Positions from BingX ───────────────────────────────────
    positions = await client.get_positions()
    active = [p for p in positions if float(p.get("positionAmt", 0)) != 0]

    # ── Open orders (SL/TP) ────────────────────────────────────
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
    total_roi = (total_pnl / INITIAL_CAPITAL) * 100
    equity_change = equity - INITIAL_CAPITAL
    winrate = (wins / total_closed * 100) if total_closed > 0 else 0
    profit_factor = (abs(avg_win * wins) / abs(avg_loss * losses)) if losses > 0 and avg_loss != 0 else 0
    total_open = len(active)
    total_orders = len(orders)
    sl_count = len([o for o in orders if o.get("type") == "STOP_MARKET"])
    tp_count = len([o for o in orders if o.get("type") == "TAKE_PROFIT_MARKET"])

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── PRINT ──────────────────────────────────────────────────
    W = 110
    print()
    print("=" * W)
    print(f"  BINGX TRADING BOT DASHBOARD  |  {now_str}")
    print("=" * W)

    # ── HESAP ──────────────────────────────────────────────────
    print()
    print("  HESAP DURUMU")
    print("  " + "-" * 60)
    print(f"  Baslangic:         {INITIAL_CAPITAL:>12.4f} USDT")
    print(f"  Bakiye:            {balance:>12.4f} USDT")
    print(f"  Equity:            {equity:>12.4f} USDT  ({equity_change:>+.4f})")
    print(f"  Kullanilan Margin: {used:>12.4f} USDT")
    print(f"  Kullanilabilir:    {available:>12.4f} USDT")
    print(f"  Acik PnL:          {unrealised:>+12.4f} USDT")
    print("  " + "-" * 60)

    # ── PNL OZET ───────────────────────────────────────────────
    print()
    print("  TOPLAM PERFORMANS")
    print("  " + "-" * 60)
    print(f"  Realize PnL:       {realised_pnl:>+12.4f} USDT")
    print(f"  Acik PnL:          {total_live_pnl:>+12.4f} USDT")
    print(f"  ─────────────────────────────────────")
    print(f"  TOPLAM PNL:        {total_pnl:>+12.4f} USDT")
    print(f"  ROI:               {total_roi:>+11.2f}%")
    print(f"  Baslangic:         {started}")
    print("  " + "-" * 60)

    # ── ACIK POZISYONLAR ───────────────────────────────────────
    print()
    print(f"  ACIK POZISYONLAR ({total_open} adet)  |  SL: {sl_count}  TP: {tp_count}  Toplam Emir: {total_orders}")
    print("  " + "-" * 106)
    if active:
        print(
            f"  {'#':<3} {'Symbol':<15} {'Yon':<6} {'Giris':>10} {'Simdi':>10}"
            f" {'TP':>10} {'SL':>10} {'PnL':>10} {'TP%':>7} {'SL%':>7}"
        )
        print("  " + "-" * 106)
        for i, p in enumerate(active, 1):
            symbol = p.get("symbol", "")
            side = p.get("positionSide", "")
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
                f"  {i:<3} {symbol:<15} {side:<6} {entry:>10.6f} {mark:>10.6f}"
                f" {tp_str:>10} {sl_str:>10} {pnl:>+9.4f}$ {tp_pct:>+6.2f}% {sl_pct:>+6.2f}%"
            )
    else:
        print("  (Acik pozisyon yok)")

    # ── ISLEM ISTATISTIKLERI ───────────────────────────────────
    print()
    print("  ISLEM ISTATISTIKLERI")
    print("  " + "-" * 60)
    print(f"  Toplam Islem:      {total_closed:>10}")
    print(f"  Kazanan:           {wins:>10}  ({winrate:.1f}%)")
    print(f"  Kaybeden:          {losses:>10}  ({100 - winrate:.1f}%)")
    print(f"  Ort. Kazanc:       {avg_win:>+10.4f} USDT")
    print(f"  Ort. Kayip:        {avg_loss:>+10.4f} USDT")
    print(f"  Profit Factor:     {profit_factor:>10.2f}")
    if best:
        print(f"  En Iyi Islem:      {best['symbol']} {best['side']} {best['realised_pnl']:>+.4f} USDT")
    if worst:
        print(f"  En Kotu Islem:     {worst['symbol']} {worst['side']} {worst['realised_pnl']:>+.4f} USDT")
    print("  " + "-" * 60)

    # ── LONG vs SHORT ─────────────────────────────────────────
    print()
    print("  LONG vs SHORT")
    print("  " + "-" * 60)
    print(f"  Long Islem:        {long_count:>10}  PnL: {long_pnl:>+.4f} USDT")
    print(f"  Short Islem:       {short_count:>10}  PnL: {short_pnl:>+.4f} USDT")
    print("  " + "-" * 60)

    # ── SON KAPATILAN ISLEMLER ─────────────────────────────────
    print()
    closed = conn.execute(
        "SELECT symbol, side, entry_price, qty, realised_pnl, closed_at "
        "FROM positions WHERE status='CLOSED' AND is_paper=? ORDER BY closed_at DESC LIMIT 10",
        (mode_is_paper,),
    ).fetchall()
    if closed:
        print(f"  SON KAPATILAN ISLEMLER (son 10)")
        print("  " + "-" * 90)
        print(f"  {'Symbol':<15} {'Yon':<6} {'Giris':>10} {'PnL':>12} {'Kapanis':>20}")
        print("  " + "-" * 90)
        for c in closed:
            pnl = c["realised_pnl"] or 0
            t = (c["closed_at"] or "")[:19]
            marker = "+" if pnl >= 0 else "-"
            print(
                f"  {c['symbol']:<15} {c['side']:<6} {c['entry_price']:>10.6f}"
                f" {pnl:>+11.4f}$ {t:>20}"
            )
    print()

    # ── RISK DURUMU ────────────────────────────────────────────
    risk_state_row = conn.execute(
        "SELECT state, reason, ts FROM risk_state_log ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    current_risk = risk_state_row["state"] if risk_state_row else "NORMAL"
    risk_reason = risk_state_row["reason"] if risk_state_row else "-"
    risk_ts = risk_state_row["ts"] if risk_state_row else "-"

    # Yedek: Bot calisiyorsa ve son risk kaydi 25 dk'dan eskiyse = restart olmus, bellek NORMAL'dir
    import os
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
                risk_reason = "restart (son kayit %.0f dk once)" % _age_min
        except Exception:
            pass

    print("  RISK DURUMU")
    print("  " + "-" * 60)
    risk_icon = {"NORMAL": "NORMAL (Tam Kapasite)", "TIGHT": "TIGHT (Dikkatli)", "ULTRA_TIGHT": "ULTRA_TIGHT (Cok Dikkatli)"}
    print(f"  Risk State:        {risk_icon.get(current_risk, current_risk)}")
    print(f"  Son Sebep:         {risk_reason}")
    print(f"  Risk log ts:       {risk_ts}")
    print(f"  DB (kontrol):      {_db_abs}")
    print("  " + "-" * 60)

    # ── BOT DURUMU ─────────────────────────────────────────────
    print()
    print("  BOT DURUMU")
    print("  " + "-" * 60)
    service_name = cfg.systemd_service_name
    result = subprocess.run(
        ["systemctl", "is-active", service_name], capture_output=True, text=True
    )
    status = (result.stdout or "").strip()
    # active = normal calisiyor; activating = yeni restart sonrasi
    running = status in ("active", "activating")
    status_str = "CALISIYOR" if running else f"DURDU !!! (systemctl: {status or result.stderr or 'unknown'})"
    print(f"  Servis:            {status_str}")
    print(f"  Scan Interval:     {cfg.scan_interval_minutes} dk")
    print(f"  Max Pozisyon:      {cfg.max_open_positions}")
    print(f"  Leverage:          {cfg.leverage}x")
    print(f"  TP:                {cfg.tp_bps} bps ({cfg.tp_bps/100:.2f}%)")
    print(f"  SL:                {cfg.sl_bps} bps ({cfg.sl_bps/100:.2f}%)")
    print(f"  Max Hold:          {cfg.max_hold_minutes} dk")
    print(f"  Min Confluence:    {cfg.min_confluence_score}/5 indicator")
    print(f"  RSI:               {cfg.rsi_oversold}/{cfg.rsi_overbought}")
    print(f"  Kline:             {cfg.kline_interval} ({cfg.kline_limit} mum)")
    print("  " + "-" * 60)
    print()
    print("=" * W)

    conn.close()
    await client.close()


if __name__ == "__main__":
    asyncio.run(dashboard())
