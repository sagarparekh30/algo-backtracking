# APEX — Algorithmic Trading Platform
## Complete Business & Technical Documentation

---

## Table of Contents

1. [Platform Overview](#1-platform-overview)
2. [Architecture](#2-architecture)
3. [Data Layer](#3-data-layer)
4. [Strategy Engine](#4-strategy-engine)
5. [ML Prediction System](#5-ml-prediction-system)
6. [Risk Management](#6-risk-management)
7. [Backtesting Engine](#7-backtesting-engine)
8. [Live Market Feed](#8-live-market-feed)
9. [Auto-Scheduler](#9-auto-scheduler)
10. [Alert System](#10-alert-system)
11. [Dashboard & API](#11-dashboard--api)
12. [Database Schema](#12-database-schema)
13. [Configuration Reference](#13-configuration-reference)
14. [Deployment Guide](#14-deployment-guide)
15. [Workflow — End to End](#15-workflow--end-to-end)

---

## 1. Platform Overview

APEX is a full-stack algorithmic swing trading platform built for the Indian equity market (NSE). It is designed for a single trader or small fund that wants systematic, data-driven trade ideas rather than manual chart reading.

### What it does

| Capability | Description |
|---|---|
| **Data collection** | Fetches up to 30 years of daily OHLCV history for 100 Nifty 100 stocks from Yahoo Finance and Fyers API, stored in PostgreSQL |
| **Signal scanning** | Runs 16 swing trading strategies across all 100 stocks every day to find entry opportunities |
| **ML prediction** | RandomForest classifier predicts probability of a stock gaining ≥2% in the next 5 trading days; RandomForest regressor predicts the expected return % |
| **Backtesting** | Event-driven simulator replays every strategy signal on historical data to measure actual win rate, profit factor, and drawdown |
| **Live prices** | Real-time price stream via Fyers WebSocket; overlaid on ML predictions so signals always show current price |
| **Risk sizing** | Automatically calculates position size, stop loss, and target for every trade using ATR-based risk management |
| **Auto-scheduler** | Runs daily data updates at 4:00 PM IST after market close; retrains ML model every Sunday at 10:00 PM IST |
| **Alerts** | Sends morning trade alerts to Telegram with today's best signals |
| **Dashboard** | Mobile-first web UI accessible from phone/laptop with no installation required |

### Universe

- **100 stocks** — Nifty 100 index constituents (large-cap NSE equities)
- **Exchange** — NSE (National Stock Exchange of India)
- **Timeframe** — Daily candles (swing trading, holds 2–20 days)
- **Data sources** — Yahoo Finance (history up to 30 years) + Fyers API (incremental daily updates + live prices)

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Docker Host                          │
│                                                             │
│   ┌──────────────────────┐    ┌────────────────────────┐   │
│   │   app container      │    │   db container         │   │
│   │                      │    │                        │   │
│   │  FastAPI (port 8000) │◄──►│  PostgreSQL 16         │   │
│   │  APScheduler         │    │  (port 5433 on host)   │   │
│   │  ML training         │    │                        │   │
│   │  Strategy scanner    │    └────────────────────────┘   │
│   │                      │                                  │
│   └──────────────────────┘                                  │
│          │         │                                        │
│          │         └── ./auth/  (bind-mount, token.json)   │
│          │                                                  │
└──────────┼──────────────────────────────────────────────────┘
           │
           ▼
    External APIs:
    - Yahoo Finance (yfinance)
    - Fyers API (historical + live)
    - Telegram Bot API (alerts)
```

### Technology stack

| Layer | Technology |
|---|---|
| Backend API | Python 3.11, FastAPI, Uvicorn |
| Database | PostgreSQL 16 |
| DB driver | psycopg2-binary, SQLAlchemy 2.0 |
| ML | scikit-learn 1.8 (RandomForest, CalibratedClassifierCV) |
| Data processing | pandas 2.x, numpy |
| Scheduling | APScheduler 3.x (BackgroundScheduler, CronTrigger) |
| Live feed | Fyers WebSocket / REST |
| Historical data | yfinance, fyers-apiv3 |
| Containerisation | Docker, Docker Compose |
| Frontend | Vanilla HTML/CSS/JS (single-page, no framework) |

---

## 3. Data Layer

### 3.1 Data Sources

#### Yahoo Finance (`fetcher/yfinance_fetcher.py`)
- **Coverage**: Up to 30 years of daily OHLCV (RELIANCE.NS goes back to 1996)
- **Cost**: Free, no authentication required
- **Symbol format**: NSE symbol + `.NS` suffix (e.g. `RELIANCE` → `RELIANCE.NS`)
- **Adjustment**: Auto-adjusted for splits and dividends (`auto_adjust=True`)
- **Use case**: One-time full historical backfill

#### Fyers API (`fetcher/backfill_fyers_equity.py`)
- **Coverage**: Up to 10 years of daily OHLCV
- **Authentication**: OAuth token (refreshed daily via `login.py`)
- **Symbol format**: `NSE:SYMBOL-EQ` (e.g. `NSE:RELIANCE-EQ`)
- **Chunk size**: 365-day chunks (API limit)
- **Retry logic**: 3 attempts with exponential backoff
- **Use case**: Daily incremental updates after market close

#### Fyers Live Feed (`live/feed.py`)
- **Mode 1**: WebSocket (real-time tick-by-tick)
- **Mode 2**: REST polling (fallback if WebSocket fails)
- **Data**: LTP, open, high, low, previous close, change %, volume
- **Use case**: Real-time price overlay on ML predictions and dashboard

### 3.2 Data Pipeline

```
Yahoo Finance (one-time)
        │
        ▼
  Full history → PostgreSQL
        │
        ▼
Fyers API (daily, 4 PM IST)
        │
        ▼
  Incremental update → PostgreSQL
  (INSERT ... ON CONFLICT DO NOTHING)
        │
        ▼
  ML retrain (Sunday, 10 PM IST)
```

### 3.3 Deduplication

Both sources write to the same table. The PRIMARY KEY `(symbol, trade_date)` ensures no duplicate rows regardless of source. When Yahoo Finance and Fyers both have data for the same date, whichever was inserted first wins (the second insert is silently ignored).

### 3.4 Starting the Data Pipeline

```bash
# Step 1 — Full history (run once, takes ~30 min for all 100 stocks)
# Via dashboard: Data tab → Start Yahoo Finance Backfill

# Step 2 — Daily incremental (automated, or manual via dashboard)
# Via dashboard: Data tab → Run Fyers Backfill
```

---

## 4. Strategy Engine

### 4.1 Overview

The strategy engine (`strategies/swing_executor.py`) scans all 100 stocks in the database using one or more of 16 predefined swing trading strategies. Every strategy returns a **BUY signal** or no signal — there are no SELL signals (this is a long-only swing system).

### 4.2 Signal Output Format

Every signal contains:

| Field | Description |
|---|---|
| `symbol` | NSE stock symbol |
| `strategy` | Strategy name |
| `signal` | Always `"BUY"` |
| `close` | Last closing price |
| `entry` | Suggested entry price (usually last close) |
| `stop_loss` | ATR-based stop loss level |
| `target` | 2:1 R:R price target |
| `atr` | ATR(14) value used for sizing |
| `trend` | Overall trend assessment |
| `metric` | Key indicator value that triggered the signal |

### 4.3 The 16 Strategies

#### Trend-Following
| # | ID | Logic | Key Condition |
|---|---|---|---|
| 1 | `golden_rsi` | Pullback in uptrend | Price above EMA200, below EMA20, RSI < 40 |
| 2 | `sma_cross` | Short-term momentum breakout | SMA20 freshly crosses above SMA50 |
| 3 | `macd_cross` | Trend reversal | MACD line crosses above signal line |
| 7 | `golden_cross` | Major trend signal | SMA50 freshly crosses above SMA200 |
| 8 | `supertrend` | ATR trend flip | Price crosses above Supertrend line |
| 10 | `adx_trend` | Strong trend entry | ADX > 25, +DI > −DI, price above SMA50 |
| 14 | `ema_ribbon` | Full bullish alignment | EMA8 > EMA21 > EMA55, price above all three |

#### Mean-Reversion
| # | ID | Logic | Key Condition |
|---|---|---|---|
| 4 | `bollinger_bounce` | Lower band bounce | Price at/below lower BB, RSI < 35, above SMA200 |
| 9 | `stochastic` | Oversold reversal | Stoch %K < 20 crosses above %D, above SMA200 |
| 16 | `cci_bounce` | CCI oversold bounce | CCI crosses up from below −100, above SMA50 |

#### Breakout
| # | ID | Logic | Key Condition |
|---|---|---|---|
| 5 | `breakout` | 20-day high breakout | Close above 20-day high, volume > 1.5× average |
| 6 | `volume_surge` | Institutional accumulation | Volume > 2× average on bullish candle, above SMA50 |
| 13 | `squeeze` | TTM Squeeze breakout | BB breaks out of Keltner Channel bullishly |
| 15 | `high_52w` | 52-week high breakout | Close above 252-day high, volume > 1.5× average |

#### Candlestick Patterns
| # | ID | Logic | Key Condition |
|---|---|---|---|
| 11 | `hammer` | Bullish reversal candle | Lower wick ≥ 2× body, tiny upper wick, in downtrend |
| 12 | `bullish_engulfing` | Two-candle reversal | Today's bullish candle body engulfs yesterday's bearish body |

### 4.4 Technical Indicators Used

All indicators are in `strategies/indicators.py` as pure functions:

- **RSI** — Relative Strength Index (Wilder's smoothing)
- **EMA / SMA** — Exponential and Simple Moving Averages
- **MACD** — Moving Average Convergence Divergence (12/26/9)
- **Bollinger Bands** — 20-period, 2 standard deviations
- **ATR** — Average True Range (14-period)
- **Stochastic** — %K and %D (14/3)
- **ADX / DMI** — Average Directional Index with ±DI lines
- **Supertrend** — ATR-based trailing stop (multiplier 3.0)
- **CCI** — Commodity Channel Index (20-period)
- **Keltner Channels** — Used for Squeeze detection

---

## 5. ML Prediction System

### 5.1 Business Goal

The ML system answers: **"What is the probability this stock will close at least 2% higher 5 trading days from today?"**

It also estimates the **expected return %** so traders know whether the risk/reward is worth taking.

### 5.2 Model Architecture

Two models are trained simultaneously:

| Model | Type | Purpose |
|---|---|---|
| **Classifier** | RandomForest + CalibratedClassifierCV | Predicts buy probability (0–100%) |
| **Regressor** | RandomForest | Predicts expected 5-day return % |

Both are wrapped in a `Pipeline` with `RobustScaler` for price-scale independence.

### 5.3 Training Approach — Walk-Forward Validation

To avoid look-ahead bias (a common ML mistake in finance):

1. For each of the 100 stocks, the data is split **temporally**: oldest 80% → training set, most recent 20% → test set
2. The classifier is trained on the combined training data from all stocks
3. Probabilities are calibrated using isotonic regression so that "60% probability" actually means the stock goes up ~60% of the time
4. The test set (most recent data — what the model has never seen) is used to report AUC, accuracy, and calibration reliability

```
Time ─────────────────────────────────────────────────────►

│◄────────────── 80% Training ──────────────►│◄── 20% Test ──►│
│  1996 ──────────────────────────────────── │ 2021 ──► 2026  │
         Train classifier here                  Evaluate here
```

### 5.4 Features (26 total)

All features are normalised by dividing by price, making the model independent of price level (a ₹100 stock and a ₹5,000 stock are treated the same).

| Category | Features |
|---|---|
| Momentum | `ret_1d`, `ret_5d`, `ret_10d`, `ret_20d` |
| Oscillators | `rsi_14`, `stoch_k`, `stoch_d` |
| MACD | `macd_hist_norm`, `macd_line_norm` |
| Bollinger Bands | `bb_position`, `bb_width` |
| Volatility | `atr_norm` |
| Volume | `vol_ratio` |
| Directional | `adx_val`, `plus_di`, `minus_di` |
| EMA distances | `dist_ema8`, `dist_ema21`, `dist_ema55` |
| SMA distances | `dist_sma50`, `dist_sma200` |
| Candle anatomy | `body_ratio`, `upper_wick_ratio`, `lower_wick_ratio`, `hi_lo_range_norm`, `close_vs_high` |

### 5.5 Market Regime Detection (`ml/regime.py`)

Before running predictions, the platform computes the current **market regime** by scanning all 100 stocks in the database:

| Regime | Condition | BUY Threshold | AVOID Threshold |
|---|---|---|---|
| **Bull** | ≥60% of stocks above SMA50 | 55% probability | 42% |
| **Neutral** | 40%–60% above SMA50 | 60% probability | 40% |
| **Bear** | ≤40% above SMA50 | 65% probability | 38% |

In a Bear regime the model demands a higher conviction (65%) before calling a BUY — fewer but higher-quality signals.

### 5.6 Signal Labels

| Signal | Condition | Meaning |
|---|---|---|
| **BUY** | Probability ≥ buy threshold | Strong upside expected |
| **NEUTRAL** | Between avoid and buy thresholds | No clear edge |
| **AVOID** | Probability ≤ avoid threshold | Bearish or weak setup |

### 5.7 Confidence Labels

| Label | Probability Range |
|---|---|
| Very High | ≥ 75% |
| High | 65–75% |
| Moderate | 55–65% |
| Low | 45–55% |
| Very Low | < 45% |

### 5.8 Model Files

Saved to `ml/models/` (persisted in Docker volume):

| File | Contents |
|---|---|
| `price_predictor.joblib` | Trained calibrated classifier pipeline |
| `price_regressor.joblib` | Trained regressor pipeline |
| `model_meta.json` | AUC, accuracy, F1, calibration buckets, feature importances, training date |

### 5.9 Retraining Schedule

- **Automated**: Every Sunday at 10:00 PM IST (via APScheduler)
- **Manual**: Dashboard → AI tab → Train Model button
- **Duration**: ~3–5 minutes for 100 stocks with 500,000+ rows

---

## 6. Risk Management

### 6.1 Philosophy

Every trade uses **ATR-based position sizing** — the position size is determined by how volatile the stock is (ATR), not a fixed rupee amount. This means:
- Volatile stocks → smaller position (less shares)
- Calm stocks → larger position (more shares)
- Maximum loss per trade is always capped at 1% of capital (configurable)

### 6.2 Trade Parameter Calculation (`risk/manager.py`)

Given an entry price and ATR:

```
Stop Loss  = Entry Price − (ATR × 2.0)
Target     = Entry Price + (ATR × 2.0 × 2.0)   ← 2:1 reward:risk
Risk ₹     = Capital × 1%
Shares     = Risk ₹ ÷ (Entry − Stop Loss)
Position ₹ = Shares × Entry Price
```

### 6.3 Default Parameters

| Parameter | Default | Environment Variable |
|---|---|---|
| Initial Capital | ₹5,00,000 | `INITIAL_CAPITAL` |
| Risk per Trade | 1.0% | `RISK_PER_TRADE_PCT` |
| Max Open Positions | 10 | `MAX_OPEN_POSITIONS` |
| ATR Period | 14 bars | `ATR_PERIOD` |
| ATR Stop Multiplier | 2.0× | `ATR_MULTIPLIER` |
| Reward:Risk Ratio | 2.0:1 | `REWARD_RISK_RATIO` |
| Max Hold Days | 20 | `MAX_HOLD_DAYS` |

### 6.4 Example Trade

```
Stock:       RELIANCE
Entry:       ₹1,400
ATR(14):     ₹28
Stop Loss:   ₹1,400 − (₹28 × 2) = ₹1,344
Target:      ₹1,400 + (₹28 × 4) = ₹1,512
Risk ₹:      ₹5,00,000 × 1% = ₹5,000
Shares:      ₹5,000 ÷ ₹56 = 89 shares
Position ₹:  89 × ₹1,400 = ₹1,24,600  (24.9% of capital)
```

---

## 7. Backtesting Engine

### 7.1 Design Principles

The backtesting engine (`backtesting/engine.py`) is built to be **free of look-ahead bias**:

- Signal is generated using data up to bar N
- Entry price is bar N+1's **open** (not the signal bar's close)
- Exits check each subsequent bar sequentially

### 7.2 Exit Rules (in priority order)

1. **Stop Loss** — If bar low ≤ stop loss price → exit at stop loss
2. **Target** — If bar high ≥ target price → exit at target
3. **Max Hold** — If position held > 20 bars → exit at closing price

### 7.3 Metrics Reported

| Metric | Description |
|---|---|
| Total Trades | Number of completed trades |
| Win Rate | % of trades that hit target |
| Profit Factor | Gross profit ÷ Gross loss |
| Average Win | Average gain on winning trades |
| Average Loss | Average loss on losing trades |
| Max Drawdown | Largest peak-to-trough equity decline |
| Sharpe Ratio | Risk-adjusted return |
| Total Return % | Total return over backtest period |

### 7.4 Per-Symbol Breakdown

Results are reported both in aggregate and per-symbol, so you can see which stocks each strategy works best on.

---

## 8. Live Market Feed

### 8.1 Architecture (`live/feed.py`)

The `LiveFeed` class manages the real-time price connection:

```
Start Feed
    │
    ├── Try WebSocket (Fyers DataSock)
    │       │ Success → real-time ticks, updates internal price dict
    │       │ Failure → fallback to polling
    │
    └── REST Polling (every 5 seconds)
            └── Fyers quotes API for all watchlist symbols
```

### 8.2 Price Data Structure

Each symbol in the live feed stores:

| Field | Description |
|---|---|
| `ltp` | Last traded price |
| `open` | Day open |
| `high` | Day high |
| `low` | Day low |
| `prev_close` | Previous day close |
| `change` | Absolute change (₹) |
| `change_pct` | Percentage change |
| `volume` | Day volume |
| `updated_at` | Timestamp of last update |

### 8.3 Live Price Overlay on ML Predictions

When the live feed is running, the `/api/ml/predict` endpoint automatically replaces the stale DB closing price with the live LTP. The price target is also recalculated from the live price. The UI shows a green **●LIVE** badge vs `(last close)` label so you always know which price you're seeing.

### 8.4 Watchlist

Managed in `data/watchlist.json`. Add/remove symbols via dashboard or API. Only watchlist symbols receive live price streaming.

---

## 9. Auto-Scheduler

### 9.1 Overview (`scheduler/auto_scheduler.py`)

The scheduler runs inside the FastAPI process using APScheduler's `BackgroundScheduler`. It starts automatically when the app starts and runs two recurring jobs.

### 9.2 Job 1 — Daily Data Update

| Setting | Value |
|---|---|
| Schedule | Monday–Friday at **4:00 PM IST** |
| Trigger | After NSE market closes (3:30 PM) |
| Action | Runs `fetcher/backfill_fyers_equity.py` as a subprocess |
| Mode | Incremental — only fetches new candles since last saved date |
| Duration | ~2–5 minutes for 100 symbols |
| Timeout | 60 minutes max |

### 9.3 Job 2 — Weekly ML Retrain

| Setting | Value |
|---|---|
| Schedule | Every **Sunday at 10:00 PM IST** |
| Action | Full walk-forward retrain on all available history |
| Duration | ~3–5 minutes |
| Output | Updates `ml/models/*.joblib` and `model_meta.json` |

### 9.4 Manual Triggers

Both jobs can be triggered manually from the dashboard without waiting for the schedule:
- Dashboard → Data tab → **Run Daily Update**
- Dashboard → AI tab → **Train Model**

Or via API:
```
POST /api/scheduler/run_data_update
POST /api/scheduler/run_ml_retrain
```

### 9.5 Scheduler Configuration

All times are configurable via environment variables:

```env
SCHEDULER_ENABLED=true
SCHEDULER_DATA_UPDATE_TIME=16:00    # HH:MM IST
SCHEDULER_ML_RETRAIN_DAY=sun        # mon/tue/wed/thu/fri/sat/sun
SCHEDULER_ML_RETRAIN_TIME=22:00     # HH:MM IST
```

---

## 10. Alert System

### 10.1 Telegram Bot (`alerts/telegram_bot.py`)

Sends trade alerts to a configured Telegram chat or channel.

### 10.2 Alert Content

The morning alert includes:
- Market regime (Bull/Neutral/Bear) and breadth percentage
- All BUY signals for the day, sorted by ML probability
- For each signal: symbol, strategy, entry, stop loss, target, ML confidence

### 10.3 Daily Pipeline (`scheduler/daily_runner.py`)

The complete automated daily workflow:

```
4:00 PM IST — Market closes
    │
    ├── Step 1: Fyers backfill (fetch today's candles)
    │
    ├── Step 2: Strategy scan (run all 16 strategies)
    │
    ├── Step 3: ML predictions (score all 100 stocks)
    │
    └── Step 4: Telegram alert (send signals to chat)
```

### 10.4 Configuration

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

---

## 11. Dashboard & API

### 11.1 Dashboard Tabs

#### Data Tab
- Fyers backfill status and progress
- Yahoo Finance historical backfill with progress bar
- Database statistics (total rows, date range, source breakdown)
- Auto-scheduler next run times and last results

#### Signals Tab
- Select from 16 strategies via dropdown
- Signal cards with entry, stop loss, target, ATR metric
- Run all 16 strategies simultaneously

#### Backtest Tab
- Select strategy and optional symbol filter
- View aggregate metrics and per-symbol breakdown
- Equity curve visualisation

#### Live Tab
- Manage watchlist (add/remove symbols)
- Start/stop live WebSocket or polling feed
- Real-time price cards with change % and volume

#### AI Tab
- Train Model button with progress indicator
- Model metrics: AUC-ROC, Accuracy, F1
- Market regime gauge (Bull/Neutral/Bear) with breadth bar
- Walk-forward validation results
- Calibration reliability chart (predicted vs actual %)
- Top 10 predictive features with importance bars
- Predictions for all 100 stocks with filter (All/BUY/NEUTRAL/AVOID)
- Each prediction shows: live/last-close price, probability bar, price target, expected return %

### 11.2 Complete API Reference

#### System
| Method | Endpoint | Description |
|---|---|---|
| GET | `/` | Dashboard HTML |
| GET | `/api/status` | DB stats, backfill status, token validity |
| GET | `/api/ui_config` | UI configuration overrides |

#### Data & Backfill
| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/start_backfill` | Start Fyers historical backfill (background) |
| POST | `/api/start_yfinance_backfill` | Start Yahoo Finance backfill (background) |
| GET | `/api/yfinance/status` | Yahoo Finance backfill progress |
| GET | `/api/db/sources` | Row counts by data source |
| GET | `/api/latest_snapshot` | Most recent candle per symbol (top 10) |

#### Strategies & Signals
| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/strategies/list` | All 16 strategies with descriptions |
| GET | `/api/signals?strategy=golden_rsi` | Signals for one strategy |
| GET | `/api/signals/all` | Run all strategies, grouped by strategy |

#### Backtesting
| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/backtest?strategy=golden_rsi&symbol=RELIANCE` | Run backtest |

#### Live Feed
| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/live/status` | Feed status and all current prices |
| POST | `/api/live/start?mode=websocket` | Start live feed |
| POST | `/api/live/stop` | Stop live feed |
| GET | `/api/ltp` | Latest prices (feed or one-shot REST) |
| GET | `/api/watchlist` | Current watchlist |
| POST | `/api/watchlist/{symbol}` | Add symbol to watchlist |
| DELETE | `/api/watchlist/{symbol}` | Remove symbol from watchlist |

#### ML Prediction
| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/ml/train` | Start ML training (background, 3–5 min) |
| GET | `/api/ml/train/status` | Training status and last metrics |
| GET | `/api/ml/predict` | Predictions for all 100 stocks |
| GET | `/api/ml/predict/{symbol}` | Prediction for one symbol |
| GET | `/api/ml/regime` | Current market regime |
| GET | `/api/ml/reliability` | Calibration reliability buckets |

#### Scheduler
| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/scheduler/status` | Job schedules and last run results |
| POST | `/api/scheduler/run_data_update` | Trigger data update now |
| POST | `/api/scheduler/run_ml_retrain` | Trigger ML retrain now |

#### Daily Pipeline
| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/daily_run` | Run full pipeline (backfill + scan + alert) |
| GET | `/api/daily_run/status` | Pipeline status |

---

## 12. Database Schema

### Table: `equity_daily_candles_swing_trading`

This is the only data table. It stores one row per stock per trading day.

| Column | Type | Description |
|---|---|---|
| `symbol` | VARCHAR(50) | NSE plain symbol e.g. `RELIANCE` |
| `trade_date` | DATE | Trading date (YYYY-MM-DD) |
| `open` | NUMERIC(14,4) | Opening price |
| `high` | NUMERIC(14,4) | Day high |
| `low` | NUMERIC(14,4) | Day low |
| `close` | NUMERIC(14,4) | Closing price |
| `volume` | BIGINT | Traded volume (shares) |
| `source` | VARCHAR(20) | Data source: `YFINANCE` or `FYERS` |
| `created_at` | TIMESTAMPTZ | Row insertion timestamp |

**Primary Key**: `(symbol, trade_date)` — prevents duplicates across sources.

### Indexes

| Index | Columns | Purpose |
|---|---|---|
| `idx_candles_symbol` | `symbol` | Fast per-symbol queries |
| `idx_candles_date` | `trade_date DESC` | Fast date-range queries |
| `idx_candles_symbol_date` | `symbol, trade_date DESC` | Combined lookups |
| `idx_candles_source` | `source` | Filter by data source |

### Table: `schema_migrations`

Tracks which migration scripts have been applied.

| Column | Type | Description |
|---|---|---|
| `version` | VARCHAR(50) PK | Migration version e.g. `001` |
| `applied_at` | TIMESTAMPTZ | When migration was applied |
| `description` | TEXT | Human-readable description |

### Useful Queries

```sql
-- Total rows by source
SELECT source, COUNT(*) as rows, MIN(trade_date), MAX(trade_date)
FROM equity_daily_candles_swing_trading
GROUP BY source;

-- Latest data for a specific stock
SELECT * FROM equity_daily_candles_swing_trading
WHERE symbol = 'RELIANCE'
ORDER BY trade_date DESC
LIMIT 10;

-- Stocks with the most history
SELECT symbol, MIN(trade_date) as from_date, COUNT(*) as bars
FROM equity_daily_candles_swing_trading
GROUP BY symbol
ORDER BY bars DESC
LIMIT 10;

-- Check for gaps (symbols with no recent data)
SELECT symbol, MAX(trade_date) as last_date
FROM equity_daily_candles_swing_trading
GROUP BY symbol
HAVING MAX(trade_date) < CURRENT_DATE - INTERVAL '5 days'
ORDER BY last_date;
```

---

## 13. Configuration Reference

All configuration is via environment variables. Copy `.env.example` → `.env`.

### Required

| Variable | Description |
|---|---|
| `FYERS_CLIENT_ID` | Fyers API client ID (from Fyers developer portal) |
| `FYERS_SECRET_KEY` | Fyers API secret key |
| `DATABASE_URL` | PostgreSQL connection string |

### Optional — Data

| Variable | Default | Description |
|---|---|---|
| `FYERS_REDIRECT_URI` | `https://www.google.com` | OAuth redirect URI |
| `LOOKBACK_YEARS` | `10` | Years of Fyers history to fetch |
| `DAILY_LOOKBACK_DAYS` | `10` | Days to re-check on incremental update |
| `SYMBOL_FILE` | `config/nifty_100_11Jan26.json` | Stock universe file |

### Optional — Risk

| Variable | Default | Description |
|---|---|---|
| `INITIAL_CAPITAL` | `500000` | Starting capital in ₹ |
| `RISK_PER_TRADE_PCT` | `1.0` | Max risk per trade as % of capital |
| `MAX_OPEN_POSITIONS` | `10` | Maximum simultaneous open positions |
| `ATR_PERIOD` | `14` | ATR lookback period |
| `ATR_MULTIPLIER` | `2.0` | ATR × multiplier = stop distance |
| `REWARD_RISK_RATIO` | `2.0` | Target = stop distance × this ratio |
| `MAX_HOLD_DAYS` | `20` | Force-exit after this many bars |

### Optional — Scheduler

| Variable | Default | Description |
|---|---|---|
| `SCHEDULER_ENABLED` | `true` | Enable/disable auto-scheduler |
| `SCHEDULER_DATA_UPDATE_TIME` | `16:00` | Daily update time HH:MM IST |
| `SCHEDULER_ML_RETRAIN_DAY` | `sun` | Weekly retrain day |
| `SCHEDULER_ML_RETRAIN_TIME` | `22:00` | Weekly retrain time HH:MM IST |

### Optional — Alerts

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | — | Target chat/channel ID |

---

## 14. Deployment Guide

### 14.1 Prerequisites

- Docker Desktop installed and running
- Fyers trading account with API access enabled
- (Optional) Telegram bot created via @BotFather

### 14.2 First-Time Setup

```bash
# 1. Clone / download the project
cd algo-backtracking

# 2. Configure environment
cp .env.example .env
# Edit .env — fill in FYERS_CLIENT_ID and FYERS_SECRET_KEY

# 3. Get Fyers access token (run locally, outside Docker)
python3 login.py
# Follow the browser prompt, paste the redirect URL back

# 4. Start Docker services
docker compose up --build -d

# 5. Verify both containers are healthy
docker compose ps
# Expected: app (healthy), db (healthy)

# 6. Access dashboard
open http://localhost:8000
```

### 14.3 Loading Historical Data (first run only)

1. Go to dashboard → **Data tab**
2. Click **Start Yahoo Finance Backfill**
3. Wait ~30 minutes (100 stocks × ~0.5s each + download time)
4. Once complete, the DB will have ~500,000+ rows spanning up to 30 years

### 14.4 First ML Training

1. Go to dashboard → **AI tab**
2. Click **Train Model**
3. Wait ~5 minutes
4. Review AUC-ROC, accuracy, and calibration reliability

### 14.5 Daily Operation (fully automated)

Once set up, the system runs on its own:

| Time | Automated Action |
|---|---|
| 4:00 PM IST (Mon–Fri) | Fetch today's candles from Fyers for all 100 stocks |
| 10:00 PM IST (Sunday) | Retrain ML model on latest data |
| Morning (manual) | Check dashboard AI tab for today's BUY signals |

### 14.6 Token Refresh (daily)

Fyers access tokens expire every day. Run locally:

```bash
python3 login.py
```

The `./auth/` folder is bind-mounted into the container so the new token is immediately available — no container restart needed.

### 14.7 Useful Docker Commands

```bash
# View live logs
docker compose logs -f app

# Shell into app container
docker compose exec app bash

# Query database directly
docker compose exec db psql -U trading trading

# Restart app only (without rebuilding)
docker compose restart app

# Full stop (keeps data)
docker compose down

# Full reset (deletes all data)
docker compose down -v

# Rebuild after code changes
docker compose up --build -d
```

### 14.8 pgAdmin Connection

| Field | Value |
|---|---|
| Host | `localhost` |
| Port | `5433` |
| Database | `trading` |
| Username | `trading` |
| Password | `trading` |

---

## 15. Workflow — End to End

### Daily Trader Workflow

```
Morning (before market open)
    │
    ├── Open dashboard → AI tab
    ├── Check Market Regime (Bull/Neutral/Bear)
    ├── Review BUY signals (sorted by probability)
    ├── For each BUY signal:
    │       ├── Note entry price (live if feed running)
    │       ├── Note stop loss and target
    │       ├── Check ML confidence (Very High / High preferred)
    │       └── Cross-check with strategy signal (Signals tab)
    │
    └── Place orders manually in Fyers terminal

During market hours
    │
    ├── Start live feed (Live tab → Start Feed)
    ├── Monitor positions against stop loss / target levels
    └── Exit when stop or target hit (manual execution)

After market close (automated)
    │
    └── 4:00 PM IST — Scheduler fetches today's data automatically
```

### Weekly Review Workflow

```
Sunday evening (automated)
    │
    └── 10:00 PM IST — ML model retrains on latest week's data

Monday morning
    │
    ├── Check model_meta.json for updated AUC / accuracy
    ├── Review top feature importances (did anything shift?)
    └── Adjust confidence thresholds if needed (via .env)
```

---

*Last updated: April 2026*
*Platform: APEX Algorithmic Trading System v1.0*
