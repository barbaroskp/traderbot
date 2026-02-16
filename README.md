# BingX Agent – 24/7 Futures Trading Bot

Autonomous trading bot for **BingX Perpetual Futures (Swap V2)** with EMA mean-reversion strategy, adaptive risk management, and paper/live dual-mode execution.

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│  Universe    │────▶│  Selector    │────▶│  Strategy    │
│  Discovery   │     │  (3-stage)   │     │  (EMA MR)    │
└─────────────┘     └──────────────┘     └──────┬───────┘
                                                 │
     ┌──────────────┐     ┌──────────────┐       ▼
     │  Risk Mgr    │◀───▶│  Execution   │◀── Signals
     │  (FSM)       │     │  Paper/Live  │
     └──────────────┘     └──────┬───────┘
                                 │
                    ┌────────────▼────────────┐
                    │  SQLite + Portfolio     │
                    │  (positions, PnL, logs) │
                    └─────────────────────────┘
```

## Quick Start

### 1. Clone & Install

```bash
cd bingx_agent
cp .env.example .env
# Edit .env with your API credentials

pip install -e ".[dev]"
```

### 2. Validate Configuration

```bash
python -m src.cli validate-config
```

### 3. Run Paper Trading (Default)

```bash
python -m src.cli run-paper
```

### 4. Run via Docker

```bash
cp .env.example .env
# Edit .env

docker-compose up -d
docker-compose logs -f bingx-agent
```

### 5. Check Status

```bash
python -m src.cli status
```

### 6. Export Data

```bash
python -m src.cli export-csv --table trades -o trades.csv
python -m src.cli export-csv --table pnl
python -m src.cli export-csv --table signals
python -m src.cli export-csv --table orders
```

## Configuration

All settings via environment variables or `.env` file:

| Variable | Default | Description |
|---|---|---|
| `BINGX_API_KEY` | (required) | BingX API key |
| `BINGX_API_SECRET` | (required) | BingX API secret |
| `PAPER_MODE` | `true` | Paper trading mode |
| `ALLOW_LIVE_TRADING` | `false` | Must be `true` for live |
| `INITIAL_CAPITAL_USDT` | `50` | Starting capital |
| `MAX_TOTAL_NOTIONAL_USDT` | `10` | Max total exposure |
| `MAX_TRADE_NOTIONAL_USDT` | `3` | Max per-trade size |
| `PER_TRADE_FRACTION` | `0.03` | Fraction of balance per trade |
| `LEVERAGE` | `1` | Default leverage |
| `MARGIN_MODE` | `ISOLATED` | Margin mode |
| `SCAN_INTERVAL_MINUTES` | `10` | Scan frequency |
| `FAST_EMA` / `SLOW_EMA` | `9` / `21` | EMA periods |
| `ENTRY_THRESHOLD_BPS` | `25` | Signal threshold (bps) |
| `TP_BPS` / `SL_BPS` | `40` / `30` | Take-profit / Stop-loss (bps) |
| `MAX_OPEN_POSITIONS` | `2` | Max concurrent positions |
| `MAX_HOLD_MINUTES` | `120` | Position timeout |
| `COOLDOWN_MINUTES` | `20` | Post-trade cooldown |

## Strategy

**EMA Mean Reversion** on mid-price:

1. Compute `z = (mid - fast_ema) / fast_ema` in basis points
2. If `|z| >= entry_threshold_bps`:
   - `z < 0` → **LONG** (price below EMA, expect reversion up)
   - `z > 0` → **SHORT** (price above EMA, expect reversion down)
3. Additional filters: spread, depth, orderbook imbalance, cooldown

Exit: **TP** (limit), **SL** (stop-market), or **timeout** (`max_hold_minutes`).

## Risk Management

Three-state risk machine that **never stops** the bot:

| State | Trigger | Effect |
|---|---|---|
| **NORMAL** | Default | Standard thresholds |
| **TIGHT** | 3 losses / 5% DD / high API errors | +50% entry threshold, 50% notional, +50% cooldown |
| **ULTRA_TIGHT** | 5 losses / 10% DD / very high errors | Top 10 only, 0.5-1 USDT max, low-vol only |

Automatic de-escalation when consecutive wins and stable cycles accumulate.

## Improving profitability (daha çok kazanmak)

- **Script tarafı**: Daha seçici giriş (confluence 4/5, trend filtresi, RSI 25/75), daha iyi risk–ödül (TP 100 bps, SL 50 bps), z-score tavanı ve cooldown ile overtrading azaltma. Tümü config ve strateji ile ayarlanabilir.
- **LLM katmanı (opsiyonel)**: Sunucuda yerel bir LLM (Ollama) çalıştırıp her sinyali onaylatabilirsiniz; model APPROVE / REJECT / REDUCE verir. Böylece script + akıl birlikte çalışır.

### Optional LLM Advisor (Ollama)

Bot, her sinyali çalıştırmadan önce yerel bir LLM’e sorabilir (onay / red / küçült). Bu özellik kapalıyken davranış tamamen script ile aynıdır.

**1. Sunucuda Ollama kurulumu**

```bash
# Linux (curl ile)
curl -fsSL https://ollama.com/install.sh | sh

# Model indir (hafif: 3B, daha iyi mantık: 7B)
ollama pull llama3.2:3b
# veya: ollama pull qwen2.5:7b
```

**2. Servisi çalıştır**

```bash
ollama serve   # arka planda veya systemd ile
```

**3. Bot config**

`.env` içinde:

```
USE_LLM_ADVISOR=true
OLLAMA_MODEL=llama3.2:3b
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_TIMEOUT_SECONDS=8
OLLAMA_MAX_CALLS_PER_CYCLE=3
```

**4. Davranış**

- Her cycle’da en fazla `OLLAMA_MAX_CALLS_PER_CYCLE` sinyal LLM’e gider (döngü süresini uzatmamak için).
- Cevap **APPROVE** → işlem aynen açılır.
- **REJECT** → o sinyal atlanır.
- **REDUCE** → pozisyon büyüklüğü yarıya iner.
- Zaman aşımı veya hata → APPROVE kabul edilir (bot bloke olmaz).

LLM’e giden bağlam: sembol, yön, z-score, RSI, MACD, BB, trend, confluence, açık pozisyon sayısı, bakiye, realise PnL özeti.

## Live Trading Checklist

Before enabling live trading:

1. Run paper mode for at least 24-48 hours
2. Review trades in the CSV export
3. Verify API credentials work: `python -m src.cli validate-config`
4. Set in `.env`:
   ```
   PAPER_MODE=false
   ALLOW_LIVE_TRADING=true
   ```
5. Start with minimal capital and conservative settings
6. Monitor logs: `tail -f data/bingx_agent.log | python -m json.tool`

## Testing

```bash
pytest -v
pytest --cov=src
```

Test coverage includes:
- HMAC-SHA256 signature generation
- Rate limiter backoff behavior
- Universe discovery & caching
- Selector filter pipeline
- Strategy signal generation
- Paper fill simulation (SL/TP/timeout)
- Risk state transitions
- Idempotent order IDs
- Storage schema & queries

## Project Structure

```
bingx_agent/
├── src/
│   ├── config.py        # Pydantic settings
│   ├── logger.py        # JSON structured logging
│   ├── bingx_client.py  # API client (auth + rate limit)
│   ├── universe.py      # Contract discovery
│   ├── marketdata.py    # Price/depth/EMA aggregation
│   ├── selector.py      # 3-stage tradeable filter
│   ├── strategy.py      # EMA mean-reversion signals
│   ├── risk.py          # Risk state machine
│   ├── execution.py     # Paper + Live execution
│   ├── portfolio.py     # Position & PnL tracking
│   ├── storage.py       # SQLite persistence
│   ├── scheduler.py     # Main 24/7 loop
│   ├── llm_advisor.py   # Optional Ollama signal advisor
│   └── cli.py           # CLI commands
├── tests/
├── data/                # SQLite DB + logs (gitignored)
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── .env.example
└── README.md
```

## Monitoring

Logs are JSON-structured. Every cycle logs:
- Universe size, tradeable count
- Open positions, total exposure
- Realised/unrealised PnL
- Risk state
- API latency, rate limit hits
- Cycle duration (ms)

```bash
# Pretty-print latest logs
tail -20 data/bingx_agent.log | python -m json.tool

# Filter for trades only
grep '"paper: position' data/bingx_agent.log | python -m json.tool
```

## Troubleshooting

| Issue | Solution |
|---|---|
| `Config validation FAILED` | Check `.env` file exists and values are valid |
| `rate limited (429)` | Reduce `SCAN_INTERVAL_MINUTES` or reduce shortlist size |
| `universe refresh failed` | Check network / API status; bot uses cached data |
| `0 tradeable symbols` | Normal during low-liquidity periods; bot continues |
| `ULTRA_TIGHT state` | Bot is protecting capital; will auto-recover |
| Docker healthcheck failing | Run `validate-config` manually to diagnose |

## License

Private / Internal Use.
