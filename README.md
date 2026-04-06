# APEX — Algorithmic Trading Platform

A full-stack algorithmic trading system built on the Fyers API. Fetches and stores historical NSE equity data, scans 16 swing trading strategies across the Nifty 100, runs backtests, streams live prices, and delivers morning alerts via Telegram — all from a mobile-first web dashboard.

---

## Features

- **Data Station** — Backfill 2 years of daily OHLCV candles for 99 Nifty 100 symbols into SQLite
- **Signal Scanner** — 16 ready-to-use swing trading strategies (trend, mean-reversion, breakout, candle patterns)
- **Backtest Studio** — Event-driven backtesting engine with ATR-based stops and 2:1 R:R targets
- **Live Market** — Real-time prices via Fyers WebSocket (falls back to REST polling)
- **Telegram Alerts** — Morning report with all signals sent automatically by the daily pipeline
- **Risk Manager** — Position sizing by % risk per trade, configurable via environment variables
- **Mobile-First Dashboard** — FastAPI backend + single-page HTML UI, works on phone

---

## Project Structure

```
algo-backtracking/
├── auth/
│   └── token.json              # Fyers access token (auto-generated, gitignored)
├── alerts/
│   └── telegram_bot.py         # Telegram signal & report notifications
├── backtesting/
│   └── engine.py               # Event-driven backtest engine
├── config/
│   ├── settings.py             # All config (from env vars)
│   └── nifty_100_11Jan26.json  # 99-symbol Nifty 100 universe
├── dashboard/
│   ├── index.html              # Single-page mobile dashboard
│   ├── main.py                 # FastAPI backend (all API endpoints)
│   └── ui_config.json          # UI colour / label overrides
├── data/
│   ├── marketdata.db           # SQLite price database
│   └── watchlist.json          # Live feed watchlist
├── fetcher/
│   └── backfill_fyers_equity.py  # Historical data backfill
├── live/
│   ├── feed.py                 # LiveFeed class (WebSocket + REST polling)
│   └── watchlist.py            # Watchlist CRUD helpers
├── logs/                       # Auto-created log files
├── risk/
│   └── manager.py              # Position sizing & trade parameter calc
├── scheduler/
│   └── daily_runner.py         # Full daily pipeline (backfill → scan → alert)
├── strategies/
│   ├── indicators.py           # Pure-function technical indicators
│   └── swing_executor.py       # StrategyManager with 16 strategies
├── load_env.py                 # .env loader (auto-runs on import)
├── login.py                    # Fyers OAuth token generator
└── requirements.txt
```

---

## Quick Start

### 1. Prerequisites

- Python 3.10+
- Fyers trading account with API credentials (`FYERS_CLIENT_ID`, `FYERS_SECRET_KEY`)
- Optional: Telegram bot token for morning alerts

### 2. Install

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure

Create a `.env` file in the project root:

```env
FYERS_CLIENT_ID=your_client_id
FYERS_SECRET_KEY=your_secret_key

# Optional
FYERS_REDIRECT_URI=https://www.google.com
LOOKBACK_YEARS=2
TABLE_NAME=equity_daily_candles_swing_trading

# Telegram (skip if not needed)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Risk management
INITIAL_CAPITAL=500000
RISK_PER_TRADE_PCT=1.0
MAX_OPEN_POSITIONS=10
ATR_MULTIPLIER=2.0
REWARD_RISK_RATIO=2.0
MAX_HOLD_DAYS=20
```

### 4. Login

```bash
python login.py
```

Opens a browser for Fyers OAuth, saves `auth/token.json`. Token is valid for 24 hours.

### 5. Backfill historical data

```bash
python fetcher/backfill_fyers_equity.py
```

Fetches 2 years of daily candles for all 99 symbols (~30-45 min on first run). Subsequent runs are incremental.

### 6. Launch the dashboard

```bash
cd dashboard
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000` in any browser (desktop or mobile).

---

## Dashboard Tabs

### Data Station
- Live sync progress with per-symbol status
- Database health metrics (size, row count, date range)
- Latest candle snapshot table
- **Sync Data** button triggers a background backfill
- **Daily Run** button triggers the full pipeline (backfill + scan + Telegram)

### Signal Scanner
- Select from 16 strategies
- **Scan Universe** scans all 99 symbols and shows hits
- Each result shows: symbol, price, stop loss, target, RSI, volume ratio, ATR
- Tap any symbol to open TradingView chart

### Backtest Studio
- Select strategy + optional single symbol
- Shows: Win Rate, Total P&L, Total Trades, Max Drawdown, Sharpe Ratio, Profit Factor
- Full trade log (entry/exit date, price, P&L, exit reason)
- Entry at next bar open · Stop loss ATR×2 · Target 2:1 R:R · Max 20 days hold

### Live Market
- Start WebSocket stream or REST polling (3-second interval)
- Add/remove symbols to your watchlist
- Price table with flash animations on tick changes (green/red)
- Market open/close status (NSE hours: Mon–Fri 9:15 AM – 3:30 PM IST)

---

## 16 Trading Strategies

### Trend Following
| ID | Name | Signal Logic |
|----|------|-------------|
| `golden_rsi` | Golden RSI | Above EMA200 + below EMA20 + RSI < 40 |
| `sma_cross` | SMA 20/50 Cross | SMA20 freshly crosses above SMA50 |
| `macd_cross` | MACD Cross | MACD line crosses above signal, histogram turns positive |
| `golden_cross` | Golden Cross | SMA50 freshly crosses above SMA200 |
| `supertrend` | Supertrend | Price crosses above Supertrend line (bullish flip) |
| `adx_trend` | ADX Trend | ADX > 25, +DI > –DI, above SMA50 |
| `ema_ribbon` | EMA Ribbon | EMA8 > EMA21 > EMA55 fresh alignment |

### Mean Reversion
| ID | Name | Signal Logic |
|----|------|-------------|
| `bollinger_bounce` | Bollinger Bounce | At/below lower BB (2σ), RSI < 35, above SMA200 |
| `stochastic` | Stochastic | %K < 20 crosses above %D while above SMA200 |
| `cci_bounce` | CCI Bounce | CCI crosses up from below –100, above SMA50 |

### Breakout
| ID | Name | Signal Logic |
|----|------|-------------|
| `breakout` | Breakout | Close above 20-day high with volume > 1.5× average |
| `high_52w` | 52-Week High | Close above 52-week high with volume > 1.5× |
| `squeeze` | TTM Squeeze | Bollinger Bands break out of Keltner Channels, bullish direction |

### Candle Patterns
| ID | Name | Signal Logic |
|----|------|-------------|
| `volume_surge` | Volume Surge | Volume > 2× average, bullish candle, above SMA50 |
| `hammer` | Hammer | Long lower wick ≥ 2× body, tiny upper wick, in downtrend |
| `bullish_engulfing` | Bullish Engulfing | Today's bullish candle fully engulfs yesterday's bearish body |

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serves the dashboard HTML |
| GET | `/api/status` | System status, DB stats, sync progress |
| POST | `/api/start_backfill` | Start incremental data sync |
| GET | `/api/latest_snapshot` | Latest candle per symbol (top 10) |
| GET | `/api/signals?strategy=golden_rsi` | Run one strategy scan |
| GET | `/api/signals/all` | Run all 16 strategies |
| GET | `/api/strategies/list` | List all strategy IDs + descriptions |
| GET | `/api/backtest?strategy=&symbol=` | Run backtest |
| POST | `/api/daily_run` | Trigger full daily pipeline |
| GET | `/api/daily_run/status` | Daily pipeline run status |
| GET | `/api/live/status` | Feed status + all current prices |
| POST | `/api/live/start?mode=websocket` | Start live feed |
| POST | `/api/live/stop` | Stop live feed |
| GET | `/api/ltp` | Latest prices (feed or one-shot REST) |
| GET | `/api/watchlist` | Get watchlist symbols |
| POST | `/api/watchlist/{symbol}` | Add symbol |
| DELETE | `/api/watchlist/{symbol}` | Remove symbol |

---

## Database Schema

```sql
CREATE TABLE equity_daily_candles_swing_trading (
    symbol     TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    volume     INTEGER,
    source     TEXT NOT NULL,
    PRIMARY KEY (symbol, trade_date)
);
```

---

## Daily Automation

Run the daily pipeline manually or schedule it with cron:

```bash
# Manual
python scheduler/daily_runner.py

# Cron — 8:00 AM IST on weekdays (IST = UTC+5:30)
30 2 * * 1-5  /path/to/venv/bin/python /path/to/scheduler/daily_runner.py
```

Pipeline steps:
1. **Backfill** — fetch yesterday's candles for all 99 symbols
2. **Scan** — run all 16 strategies, count signals per strategy
3. **Alert** — send morning Telegram report with all signals and backfill stats

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Access token expired` | `python login.py` |
| `FYERS_CLIENT_ID not set` | Check your `.env` file or export the variable |
| Strategy scans return 0 results | Run a backfill first — the DB needs at least 200 candles per symbol |
| WebSocket fails, prices stale | Dashboard auto-falls back to REST polling every 3 seconds |
| `DB Stat Error` in logs | Database may not exist yet — run a backfill first |

---

## Notes

- **Token validity**: 24 hours — re-run `login.py` each trading day
- **Rate limiting**: Backfill includes 0.3s delay per API call + 3× retry with backoff
- **No look-ahead bias**: Backtest enters at bar N+1 open after signal at bar N
- **Position sizing**: Risk amount = capital × risk_pct / 100; shares = risk_amount / (entry − stop)
