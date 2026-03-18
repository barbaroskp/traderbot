"""CLI entry point.

Commands:
  validate-config  – Parse & validate .env / settings
  run-paper        – Start paper trading (default)
  run-live         – Start live trading (requires allow_live_trading=true)
  status           – Show current bot status
  export-csv       – Export trades / PnL to CSV
"""

from __future__ import annotations

import asyncio
import csv
import json
import sys
from pathlib import Path

import click

from src.config import Settings, load_config
from src.logger import get_logger, setup_logging
from src.portfolio import Portfolio
from src.scheduler import Scheduler
from src.storage import Storage

log = get_logger(__name__)


@click.group()
def cli() -> None:
    """BingX Agent – 24/7 Futures Trading Bot."""


@cli.command("validate-config")
def validate_config() -> None:
    """Parse and validate configuration."""
    try:
        cfg = load_config()
    except Exception as exc:
        click.echo(f"Config validation FAILED: {exc}", err=True)
        sys.exit(1)

    click.echo("Configuration valid.")
    click.echo(f"  Mode:             {'PAPER' if cfg.paper_mode else 'LIVE'}")
    click.echo(f"  Live allowed:     {cfg.allow_live_trading}")
    click.echo(f"  Capital:          {cfg.initial_capital_usdt} USDT")
    click.echo(f"  Max total margin: {cfg.max_total_margin_pct * 100:.0f}% of balance")
    click.echo(f"  Max trade margin: {cfg.max_trade_margin_pct * 100:.0f}% of balance")
    click.echo(f"  Leverage:         {cfg.leverage}x (max {cfg.max_leverage_allowed}x)")
    click.echo(f"  Margin mode:      {cfg.margin_mode.value}")
    click.echo(f"  Scan interval:    {cfg.scan_interval_minutes} min")
    click.echo(f"  EMA fast/slow:    {cfg.fast_ema}/{cfg.slow_ema}")
    click.echo(f"  Entry threshold:  {cfg.entry_threshold_bps} bps")
    click.echo(f"  TP/SL:            {cfg.tp_bps}/{cfg.sl_bps} bps")
    click.echo(f"  Max positions:    {cfg.max_open_positions}")
    click.echo(f"  DB path:          {cfg.db_path}")
    click.echo(f"  Log file:         {cfg.log_file}")

    if cfg.is_live():
        issues = cfg.validate_live_ready()
        if issues:
            click.echo("\nLive readiness issues:", err=True)
            for issue in issues:
                click.echo(f"  - {issue}", err=True)
            sys.exit(1)
        click.echo("\nLive trading: READY")
    else:
        click.echo("\nPaper mode active (live trading disabled)")


@cli.command("run-paper")
def run_paper() -> None:
    """Start the bot in paper trading mode."""
    cfg = load_config()
    cfg.paper_mode = True  # Force paper mode
    setup_logging(cfg.log_level, cfg.log_file)

    click.echo("Starting BingX Agent in PAPER mode...")
    click.echo(f"Capital: {cfg.initial_capital_usdt} USDT | Scan: every {cfg.scan_interval_minutes}min")
    click.echo("Press Ctrl+C to stop.\n")

    scheduler = Scheduler(cfg)
    asyncio.run(scheduler.start())


@cli.command("run-live")
def run_live() -> None:
    """Start the bot in live trading mode."""
    cfg = load_config()

    issues = cfg.validate_live_ready()
    if issues:
        click.echo("Cannot start live trading:", err=True)
        for issue in issues:
            click.echo(f"  - {issue}", err=True)
        sys.exit(1)

    setup_logging(cfg.log_level, cfg.log_file)

    click.echo("Starting BingX Agent in LIVE mode...")
    click.echo(f"Capital: {cfg.initial_capital_usdt} USDT | Scan: every {cfg.scan_interval_minutes}min")
    click.echo("WARNING: Real money at risk!")
    click.echo("Press Ctrl+C to stop.\n")

    scheduler = Scheduler(cfg)
    asyncio.run(scheduler.start())


@cli.command("status")
def status() -> None:
    """Show current bot status from the database."""
    cfg = load_config()
    db = Storage(cfg.db_path)
    portfolio = Portfolio(cfg, db)

    summary = portfolio.get_summary()
    click.echo(json.dumps(summary, indent=2))

    # Last risk state
    last_risk = db.fetch_one(
        "SELECT * FROM risk_state_log ORDER BY id DESC LIMIT 1"
    )
    if last_risk:
        click.echo(f"\nLast risk state: {last_risk['state']} ({last_risk['ts']})")
        click.echo(f"  Reason: {last_risk.get('reason', 'N/A')}")

    # Last error
    last_error = db.fetch_one(
        "SELECT * FROM errors ORDER BY id DESC LIMIT 1"
    )
    if last_error:
        click.echo(f"\nLast error: [{last_error['component']}] {last_error['message']}")

    # Run count
    runs = db.fetch_all("SELECT COUNT(*) as cnt FROM runs")
    click.echo(f"\nTotal runs: {runs[0]['cnt']}")

    db.close()


@cli.command("backtest")
@click.option("--symbols", "-s", default="BTC-USDT", help="Comma-separated symbols")
@click.option("--days", "-d", type=int, default=30, help="Days to backtest")
@click.option("--capital", type=float, default=None, help="Initial capital USDT")
@click.option("--tp-bps", type=float, default=None, help="Override TP bps")
@click.option("--sl-bps", type=float, default=None, help="Override SL bps")
@click.option("--csv-output", is_flag=True, help="Save trade log as CSV")
def backtest(
    symbols: str,
    days: int,
    capital: float | None,
    tp_bps: float | None,
    sl_bps: float | None,
    csv_output: bool,
) -> None:
    """Run backtest on historical data."""
    from src.backtest import BacktestEngine, print_report, save_report_csv

    cfg = load_config()

    overrides: dict = {}
    if tp_bps is not None:
        overrides["tp_bps"] = tp_bps
    if sl_bps is not None:
        overrides["sl_bps"] = sl_bps
    if overrides:
        cfg = cfg.model_copy(update=overrides)

    sym_list = [s.strip() for s in symbols.split(",")]

    engine = BacktestEngine(
        cfg=cfg,
        symbols=sym_list,
        days=days,
        initial_capital=capital,
    )
    report = asyncio.run(engine.run())
    print_report(report)

    if csv_output:
        save_report_csv(report)


@cli.command("reset-db")
@click.option("--confirm", is_flag=True, help="Skip confirmation prompt")
def reset_db(confirm: bool) -> None:
    """Reset all trade data for a fresh start. Keeps contracts table."""
    cfg = load_config()

    if not confirm:
        click.confirm(
            f"This will DELETE all trades, orders, positions, signals, PnL data in {cfg.db_path}. Continue?",
            abort=True,
        )

    db = Storage(cfg.db_path)
    tables = ["positions", "orders", "fills", "signals", "market_stats",
              "pnl_daily", "risk_state_log", "errors", "runs"]
    for table in tables:
        db.execute(f"DELETE FROM {table}")  # noqa: S608
    db.checkpoint_and_vacuum(vacuum=True)
    db.close()

    click.echo(f"Database reset complete. Starting fresh with {cfg.initial_capital_usdt} USDT.")
    click.echo("Cleared tables: " + ", ".join(tables))


@cli.command("db-cleanup")
def db_cleanup() -> None:
    """Run database cleanup and vacuum manually."""
    cfg = load_config()
    db = Storage(cfg.db_path)

    size_before = db.db_size_mb()
    click.echo(f"DB size before: {size_before:.2f} MB")

    deleted = db.cleanup_old_data(cfg.db_retention_days)
    for table, count in deleted.items():
        if count > 0:
            click.echo(f"  Deleted {count} rows from {table}")

    db.vacuum()
    size_after = db.db_size_mb()
    click.echo(f"DB size after: {size_after:.2f} MB")
    click.echo(f"Freed: {size_before - size_after:.2f} MB")
    db.close()


@cli.command("export-csv")
@click.option("--table", type=click.Choice(["trades", "signals", "pnl", "orders"]), default="trades")
@click.option("--output", "-o", default=None, help="Output file path")
def export_csv(table: str, output: str | None) -> None:
    """Export data to CSV."""
    cfg = load_config()
    db = Storage(cfg.db_path)

    query_map = {
        "trades": "SELECT * FROM positions ORDER BY opened_at DESC",
        "signals": "SELECT * FROM signals ORDER BY ts DESC",
        "pnl": "SELECT * FROM pnl_daily ORDER BY date DESC",
        "orders": "SELECT * FROM orders ORDER BY ts DESC",
    }

    rows = db.fetch_all(query_map[table])
    db.close()

    if not rows:
        click.echo(f"No data in '{table}' table.")
        return

    out_path = output or f"data/{table}_export.csv"
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    click.echo(f"Exported {len(rows)} rows to {out_path}")


if __name__ == "__main__":
    cli()
