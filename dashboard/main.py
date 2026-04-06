import os
import json
import asyncio
import subprocess
import re
from datetime import datetime
from typing import List, Optional, Dict

from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Import existing settings
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

from config.settings import TOKEN_PATH, LOG_DIR, TABLE_NAME
from db.connection import get_conn, get_engine
from strategies.swing_executor import StrategyManager, STRATEGY_DESCRIPTIONS

app = FastAPI(title="Trading HQ Dashboard")


@app.on_event("startup")
async def startup_event():
    from scheduler.auto_scheduler import start_scheduler
    start_scheduler()


@app.on_event("shutdown")
async def shutdown_event():
    from scheduler.auto_scheduler import stop_scheduler
    stop_scheduler()

# Enable CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
LOG_FILE = os.path.join(LOG_DIR, "backfill.log")
BACKFILL_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fetcher", "backfill_fyers_equity.py")

# -------------------------------------------------------
# State Management
# -------------------------------------------------------

class DashboardState:
    is_running = False
    last_run = "Never"
    total_symbols = 0
    processed = 0
    updated = 0
    up_to_date = 0
    total_candles = 0
    current_symbol = "Idle"

    # Track results per symbol for this session
    # symbol -> {"status": "", "candles": 0}
    session_symbol_stats: Dict[str, Dict] = {}

    # DB Stats
    db_size_mb = 0.0
    total_db_rows = 0
    table_name = TABLE_NAME
    min_date = "N/A"
    max_date = "N/A"
    unique_symbols = 0


class DailyRunState:
    is_running = False
    last_run = "Never"
    last_result: dict = {}


class YFinanceState:
    is_running = False
    last_run = "Never"
    last_result: dict = {}
    processed = 0
    total = 0
    current_symbol = "Idle"
    total_new_candles = 0


state = DashboardState()
daily_run_state = DailyRunState()
yf_state = YFinanceState()


# -------------------------------------------------------
# Pydantic Models
# -------------------------------------------------------

class SummaryResponse(BaseModel):
    is_running: bool
    token_valid: bool
    token_expiry: str
    last_run: str
    total_symbols: int
    processed: int
    updated: int
    up_to_date: int
    total_candles: int
    current_symbol: str
    db_size_mb: float
    total_db_rows: int
    table_name: str
    min_date: str
    max_date: str
    unique_symbols: int
    symbol_results: Dict[str, Dict]


# -------------------------------------------------------
# Helpers
# -------------------------------------------------------

def get_db_stats():
    """Fetch database health metrics from PostgreSQL."""
    try:
        conn = get_conn()
        cursor = conn.cursor()

        # Table size
        cursor.execute(
            "SELECT pg_total_relation_size(%s) / 1048576.0",
            (TABLE_NAME,),
        )
        row = cursor.fetchone()
        state.db_size_mb = round(float(row[0] or 0), 2)

        cursor.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT symbol), "
            f"MIN(trade_date), MAX(trade_date) FROM {TABLE_NAME}"
        )
        rows, syms, d1, d2 = cursor.fetchone()
        state.total_db_rows  = rows or 0
        state.unique_symbols = syms or 0
        state.min_date = str(d1) if d1 else "N/A"
        state.max_date = str(d2) if d2 else "N/A"
        conn.close()
    except Exception as e:
        print(f"DB Stat Error: {e}")


def parse_log_for_summary():
    """Parses the log file to update the session state."""
    if not os.path.exists(LOG_FILE):
        return

    try:
        with open(LOG_FILE, "r") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 400000))
            lines = f.readlines()

        processed_set = set()
        updated_count = 0
        uptodate_count = 0
        candle_count = 0
        current = "Idle"

        for line in lines:
            # Detect processing start
            match_start = re.search(r"\[(\d+)/(\d+)\] (?:Processing|Incremental update for|Full backfill for) (?:NSE:)?([\w-]+)", line)
            if match_start:
                s_name = match_start.group(3)
                current = s_name
                processed_set.add(s_name)
                state.session_symbol_stats[s_name] = {"status": "active", "candles": 0}
                state.total_symbols = int(match_start.group(2))

            # Detect Up to date
            match_up = re.search(r"(?:NSE:)?([\w-]+) is already up to date", line)
            if match_up:
                s_name = match_up.group(1)
                processed_set.add(s_name)
                uptodate_count += 1
                state.session_symbol_stats[s_name] = {"status": "uptodate", "candles": 0}

            # Detect Completion
            match_comp = re.search(r"✅ Completed - (\d+) candles inserted", line)
            if match_comp:
                count = int(match_comp.group(1))
                candle_count += count
                if current != "Idle":
                    if count > 0:
                        updated_count += 1
                        state.session_symbol_stats[current] = {"status": "updated", "candles": count}
                    else:
                        if state.session_symbol_stats.get(current, {}).get("status") != "uptodate":
                            state.session_symbol_stats[current] = {"status": "uptodate", "candles": 0}

        state.processed = len(processed_set)
        state.updated = updated_count
        state.up_to_date = uptodate_count
        state.total_candles = candle_count
        state.current_symbol = current

    except Exception as e:
        print(f"Log Parse Error: {e}")


# -------------------------------------------------------
# Existing endpoints
# -------------------------------------------------------

@app.get("/api/ui_config")
async def get_ui_config():
    config_path = os.path.join(os.path.dirname(__file__), "ui_config.json")
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            return json.load(f)
    return {}


@app.get("/api/latest_snapshot")
async def get_latest_snapshot():
    try:
        import pandas as pd
        engine = get_engine()
        df = pd.read_sql(f"""
            SELECT symbol, trade_date, open, high, low, close, volume
            FROM (
                SELECT DISTINCT ON (symbol)
                    symbol, trade_date, open, high, low, close, volume
                FROM {TABLE_NAME}
                ORDER BY symbol, trade_date DESC
            ) latest
            ORDER BY trade_date DESC
            LIMIT 10
        """, engine)
        return df.to_dict(orient="records")
    except Exception as e:
        print(f"Snapshot Error: {e}")
        return []


@app.get("/api/signals")
async def get_signals(strategy: str = "golden_rsi"):
    """Returns swing trading signals using the selected strategy."""
    try:
        manager = StrategyManager()
        signals = manager.get_signals(strategy)
        return signals
    except Exception as e:
        print(f"Signal Error: {e}")
        return []


@app.get("/api/status", response_model=SummaryResponse)
async def get_status():
    token_valid = False
    token_expiry = "Unknown"

    if os.path.exists(TOKEN_PATH):
        try:
            with open(TOKEN_PATH, "r") as f:
                data = json.load(f)
                expires_at = datetime.fromisoformat(data["expires_at"])
                token_valid = datetime.now() < expires_at
                token_expiry = expires_at.strftime("%Y-%m-%d %H:%M")
        except:
            pass

    parse_log_for_summary()
    get_db_stats()

    return {
        "is_running": state.is_running,
        "token_valid": token_valid,
        "token_expiry": token_expiry,
        "last_run": state.last_run,
        "total_symbols": state.total_symbols,
        "processed": state.processed,
        "updated": state.updated,
        "up_to_date": state.up_to_date,
        "total_candles": state.total_candles,
        "current_symbol": state.current_symbol,
        "db_size_mb": state.db_size_mb,
        "total_db_rows": state.total_db_rows,
        "table_name": state.table_name,
        "min_date": state.min_date,
        "max_date": state.max_date,
        "unique_symbols": state.unique_symbols,
        "symbol_results": state.session_symbol_stats
    }


async def run_backfill_task():
    state.is_running = True
    state.last_run = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", BACKFILL_SCRIPT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        await process.wait()
    finally:
        state.is_running = False


@app.post("/api/start_backfill")
async def start_backfill(background_tasks: BackgroundTasks):
    if state.is_running:
        return {"message": "Busy"}

    # Reset session data
    state.processed = 0
    state.updated = 0
    state.up_to_date = 0
    state.total_candles = 0
    state.session_symbol_stats = {}

    background_tasks.add_task(run_backfill_task)
    return {"message": "Started"}


# -------------------------------------------------------
# New endpoints
# -------------------------------------------------------

@app.get("/api/strategies/list")
async def list_strategies():
    """Return all available strategies with their descriptions."""
    return [
        {"id": sid, "name": sid.replace("_", " ").title(), "description": desc}
        for sid, desc in STRATEGY_DESCRIPTIONS.items()
    ]


@app.get("/api/signals/all")
async def get_all_signals():
    """Run all strategies and return combined results grouped by strategy."""
    try:
        manager = StrategyManager()
        all_signals = manager.get_all_signals()
        return all_signals
    except Exception as e:
        print(f"All Signals Error: {e}")
        return {}


@app.get("/api/backtest")
async def run_backtest(strategy: str = "golden_rsi", symbol: str = None):
    """
    Run a backtest for the specified strategy.

    Query params:
        strategy: Strategy ID (default: golden_rsi)
        symbol: Optional — restrict to a single symbol
    """
    try:
        from backtesting.engine import BacktestEngine
        from risk.manager import RiskManager

        rm = RiskManager()
        engine = BacktestEngine(risk_manager=rm)
        result = engine.run(
            strategy_id=strategy,
            symbol=symbol if symbol else None,
        )
        return result
    except Exception as e:
        print(f"Backtest Error: {e}")
        return {
            "trades": [],
            "metrics": {},
            "equity_curve": [],
            "per_symbol": {},
            "error": str(e),
        }


async def _run_daily_pipeline_task():
    """Background task wrapper for the daily pipeline."""
    daily_run_state.is_running = True
    daily_run_state.last_run = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        from scheduler.daily_runner import run_daily_pipeline
        result = run_daily_pipeline()
        daily_run_state.last_result = result
    except Exception as e:
        print(f"Daily Run Error: {e}")
        daily_run_state.last_result = {"error": str(e)}
    finally:
        daily_run_state.is_running = False


@app.post("/api/daily_run")
async def trigger_daily_run(background_tasks: BackgroundTasks):
    """
    Trigger the complete daily pipeline (backfill + strategy scan + alerts)
    as a background task.
    """
    if daily_run_state.is_running:
        return {"message": "Daily run already in progress", "status": "busy"}

    background_tasks.add_task(_run_daily_pipeline_task)
    return {"message": "Daily run started", "status": "started"}


@app.get("/api/daily_run/status")
async def daily_run_status():
    """Return the current status of the daily pipeline."""
    return {
        "is_running": daily_run_state.is_running,
        "last_run": daily_run_state.last_run,
        "last_result": daily_run_state.last_result,
    }


# -------------------------------------------------------
# Live feed (singleton imported from live.feed)
# -------------------------------------------------------

from live.feed import live_feed
from live.watchlist import load_watchlist, save_watchlist, add_symbol, remove_symbol


@app.get("/api/live/status")
async def live_status():
    """Return live feed connection status and all current prices."""
    return {
        "feed": live_feed.status(),
        "prices": live_feed.get_all(),
        "watchlist": load_watchlist(),
    }


@app.post("/api/live/start")
async def live_start(mode: str = "websocket"):
    """Start the live feed for the current watchlist."""
    if live_feed.is_running():
        return {"message": "Already running", "status": live_feed.status()}
    symbols = load_watchlist()
    prefer_ws = mode != "polling"
    live_feed.start(symbols, prefer_websocket=prefer_ws)
    return {"message": "Feed started", "symbols": symbols, "mode": mode}


@app.post("/api/live/stop")
async def live_stop():
    """Stop the live feed."""
    live_feed.stop()
    return {"message": "Feed stopped"}


@app.get("/api/ltp")
async def get_ltp():
    """
    Return latest prices for all watchlist symbols.
    If feed is not running, falls back to a one-shot REST quote call.
    """
    prices = live_feed.get_all()

    # If feed isn't running, do a one-shot REST fetch
    if not prices:
        try:
            from fyers_apiv3 import fyersModel
            from live.feed import _fyers_symbol, _plain_symbol

            with open(TOKEN_PATH) as f:
                token_data = json.load(f)
            access_token = token_data.get("access_token", "")

            from config.settings import FYERS_CLIENT_ID as CID
            fyers = fyersModel.FyersModel(client_id=CID, token=access_token, log_path=LOG_DIR)

            symbols = load_watchlist()
            fyers_syms = ",".join(_fyers_symbol(s) for s in symbols)
            response = fyers.quotes({"symbols": fyers_syms})

            if response.get("s") == "ok":
                for item in response.get("d", []):
                    raw_sym = item.get("n", "")
                    symbol = _plain_symbol(raw_sym) if raw_sym else None
                    if not symbol:
                        continue
                    v = item.get("v", {})
                    prices[symbol] = {
                        "symbol":     symbol,
                        "ltp":        round(float(v.get("ltp", 0) or 0), 2),
                        "open":       round(float(v.get("open_price", 0) or 0), 2),
                        "high":       round(float(v.get("high_price", 0) or 0), 2),
                        "low":        round(float(v.get("low_price", 0) or 0), 2),
                        "prev_close": round(float(v.get("prev_close_price", 0) or 0), 2),
                        "change":     round(float(v.get("ch", 0) or 0), 2),
                        "change_pct": round(float(v.get("chp", 0) or 0), 4),
                        "volume":     int(v.get("volume", 0) or 0),
                        "updated_at": datetime.now().strftime("%H:%M:%S"),
                        "source":     "rest_snapshot",
                    }
        except Exception as e:
            print(f"LTP REST fetch error: {e}")

    return list(prices.values())


@app.get("/api/watchlist")
async def get_watchlist():
    return {"symbols": load_watchlist()}


@app.post("/api/watchlist/{symbol}")
async def add_to_watchlist(symbol: str):
    symbols = add_symbol(symbol)
    # If feed is running, subscribe the new symbol
    if live_feed.is_running():
        live_feed.subscribe([symbol])
    return {"symbols": symbols}


@app.delete("/api/watchlist/{symbol}")
async def remove_from_watchlist(symbol: str):
    symbols = remove_symbol(symbol)
    return {"symbols": symbols}


# -------------------------------------------------------
# ML Prediction endpoints
# -------------------------------------------------------

class MLTrainState:
    is_training = False
    last_trained = "Never"
    last_result: dict = {}


ml_train_state = MLTrainState()


async def _run_ml_training():
    """Background task: train the ML model."""
    ml_train_state.is_training = True
    try:
        from ml.model import MLPredictor
        predictor = MLPredictor()
        result = predictor.train()
        ml_train_state.last_result = result
        ml_train_state.last_trained = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        print(f"ML Training Error: {e}")
        ml_train_state.last_result = {"error": str(e)}
    finally:
        ml_train_state.is_training = False


@app.post("/api/ml/train")
async def ml_train(background_tasks: BackgroundTasks):
    """
    Train the ML model in the background.
    Uses all symbols in the DB — takes 2-5 minutes.
    """
    if ml_train_state.is_training:
        return {"message": "Training already in progress", "status": "busy"}
    background_tasks.add_task(_run_ml_training)
    return {"message": "Training started", "status": "started"}


@app.get("/api/ml/train/status")
async def ml_train_status():
    """Return ML training status and last result."""
    from ml.model import MLPredictor
    predictor = MLPredictor()
    return {
        "is_training":  ml_train_state.is_training,
        "last_trained": ml_train_state.last_trained,
        "is_trained":   predictor.is_trained(),
        "meta":         predictor.get_meta(),
        "last_result":  ml_train_state.last_result,
    }


def _overlay_live_prices(results: list) -> list:
    """
    Replace the DB close price with the live feed LTP wherever available.
    Adds a 'price_source' field: 'live' | 'last_close'.
    Also adjusts price_target using the live price so it's accurate.
    """
    live_prices = live_feed.get_all()   # {symbol: {ltp, ...}} or {}

    for r in results:
        sym  = r.get("symbol", "")
        tick = live_prices.get(sym)
        if tick and tick.get("ltp"):
            ltp = float(tick["ltp"])
            r["price"]        = ltp
            r["price_source"] = "live"
            r["updated_at"]   = tick.get("updated_at", "")
            # Recompute price target from live price + expected return
            exp_ret = r.get("expected_return_pct")
            if exp_ret is not None and ltp > 0:
                r["price_target"] = round(ltp * (1 + exp_ret / 100), 2)
        else:
            r["price_source"] = "last_close"

    return results


@app.get("/api/ml/predict")
async def ml_predict_all():
    """
    Run ML predictions for all symbols, regime-adjusted thresholds.
    Prices are overlaid with live feed LTP where the feed is running.
    """
    try:
        from ml.model import MLPredictor
        from ml.regime import compute_regime

        predictor = MLPredictor()
        if not predictor.load():
            return {"error": "Model not trained yet — POST /api/ml/train first", "results": []}

        regime_data = compute_regime()
        regime      = regime_data.get("regime", "Neutral")
        results     = predictor.predict_all(regime=regime)
        results     = _overlay_live_prices(results)

        live_count = sum(1 for r in results if r.get("price_source") == "live")
        return {
            "results":    results,
            "count":      len(results),
            "regime":     regime_data,
            "live_count": live_count,
        }
    except Exception as e:
        print(f"ML Predict Error: {e}")
        return {"error": str(e), "results": []}


@app.get("/api/ml/predict/{symbol}")
async def ml_predict_symbol(symbol: str):
    """Run ML prediction for a single symbol with live price overlay."""
    try:
        import pandas as pd
        from ml.model import MLPredictor
        from ml.regime import compute_regime

        predictor = MLPredictor()
        if not predictor.load():
            return {"error": "Model not trained yet — POST /api/ml/train first"}

        regime_data = compute_regime()
        regime      = regime_data.get("regime", "Neutral")

        engine = get_engine()
        df     = pd.read_sql(
            f"SELECT trade_date, open, high, low, close, volume "
            f"FROM {TABLE_NAME} WHERE symbol = %(sym)s ORDER BY trade_date ASC",
            engine, params={"sym": symbol.upper()},
        )

        if df.empty:
            return {"error": f"No data found for {symbol}"}

        result = predictor.predict_symbol(df, symbol.upper(), regime=regime)

        # Overlay live price if feed is running for this symbol
        tick = live_feed.get(symbol.upper())
        if tick and tick.get("ltp"):
            ltp = float(tick["ltp"])
            result["price"]        = ltp
            result["price_source"] = "live"
            result["updated_at"]   = tick.get("updated_at", "")
            exp_ret = result.get("expected_return_pct")
            if exp_ret is not None and ltp > 0:
                result["price_target"] = round(ltp * (1 + exp_ret / 100), 2)
        else:
            result["price_source"] = "last_close"

        return result
    except Exception as e:
        print(f"ML Predict Symbol Error: {e}")
        return {"error": str(e)}


@app.get("/api/ml/regime")
async def ml_regime():
    """
    Compute current market regime from DB breadth data.
    Returns: regime (Bull/Neutral/Bear), breadth_pct, avg_atr_pct, volatility_label.
    """
    try:
        from ml.regime import compute_regime
        return compute_regime()
    except Exception as e:
        print(f"Regime Error: {e}")
        return {"error": str(e)}


@app.get("/api/ml/reliability")
async def ml_reliability():
    """
    Return calibration reliability buckets from the last training run.
    Shows: when model predicted X%, actual hit rate was Y%.
    """
    try:
        from ml.model import MLPredictor
        predictor = MLPredictor()
        meta = predictor.get_meta()
        return {
            "reliability_buckets": meta.get("reliability_buckets", []),
            "regressor_mae_pct":   meta.get("regressor_mae_pct"),
            "regressor_r2":        meta.get("regressor_r2"),
            "validation_method":   meta.get("validation_method"),
            "wf_test_ratio_pct":   meta.get("wf_test_ratio_pct"),
        }
    except Exception as e:
        return {"error": str(e)}


# -------------------------------------------------------
# Yahoo Finance backfill endpoints
# -------------------------------------------------------

async def _run_yfinance_task():
    yf_state.is_running = True
    yf_state.last_run = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    yf_state.processed = 0
    yf_state.total_new_candles = 0
    yf_state.current_symbol = "Starting…"

    def progress_cb(processed, total, symbol, total_new):
        yf_state.processed = processed
        yf_state.total = total
        yf_state.current_symbol = symbol
        yf_state.total_new_candles = total_new

    try:
        from fetcher.yfinance_fetcher import run_yfinance_backfill
        result = run_yfinance_backfill(progress_cb=progress_cb)
        yf_state.last_result = result
        yf_state.current_symbol = "Done"
    except Exception as e:
        print(f"YFinance backfill error: {e}")
        yf_state.last_result = {"error": str(e)}
        yf_state.current_symbol = "Error"
    finally:
        yf_state.is_running = False


@app.post("/api/start_yfinance_backfill")
async def start_yfinance_backfill(background_tasks: BackgroundTasks):
    """
    Fetch full historical data (up to 30 years) for all symbols from Yahoo Finance.
    Runs in background — poll /api/yfinance/status for progress.
    """
    if yf_state.is_running:
        return {"message": "Yahoo Finance backfill already running", "status": "busy"}
    background_tasks.add_task(_run_yfinance_task)
    return {"message": "Yahoo Finance backfill started", "status": "started"}


@app.get("/api/yfinance/status")
async def yfinance_status():
    """Return Yahoo Finance backfill progress."""
    pct = round(yf_state.processed / yf_state.total * 100, 1) if yf_state.total > 0 else 0
    return {
        "is_running":       yf_state.is_running,
        "last_run":         yf_state.last_run,
        "processed":        yf_state.processed,
        "total":            yf_state.total,
        "pct":              pct,
        "current_symbol":   yf_state.current_symbol,
        "total_new_candles":yf_state.total_new_candles,
        "last_result":      yf_state.last_result,
    }


@app.get("/api/db/sources")
async def db_sources():
    """Return row counts grouped by data source."""
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT source, COUNT(*) as rows, COUNT(DISTINCT symbol) as symbols, "
            f"MIN(trade_date) as from_date, MAX(trade_date) as to_date "
            f"FROM {TABLE_NAME} GROUP BY source ORDER BY rows DESC"
        )
        rows = cursor.fetchall()
        conn.close()
        return [
            {"source": r[0], "rows": r[1], "symbols": r[2],
             "from_date": r[3], "to_date": r[4]}
            for r in rows
        ]
    except Exception as e:
        return {"error": str(e)}


# -------------------------------------------------------
# Scheduler endpoints
# -------------------------------------------------------

@app.get("/api/scheduler/status")
async def scheduler_status():
    """Return auto-scheduler status, next run times, and last results."""
    from scheduler.auto_scheduler import get_scheduler_status
    return get_scheduler_status()


@app.post("/api/scheduler/run_data_update")
async def scheduler_run_data_now(background_tasks: BackgroundTasks):
    """Manually trigger a data update right now (outside schedule)."""
    from scheduler.auto_scheduler import trigger_data_update_now, _state
    if _state.data_update_running:
        return {"message": "Data update already running", "status": "busy"}
    background_tasks.add_task(trigger_data_update_now)
    return {"message": "Data update triggered", "status": "started"}


@app.post("/api/scheduler/run_ml_retrain")
async def scheduler_run_ml_now(background_tasks: BackgroundTasks):
    """Manually trigger an ML retrain right now (outside schedule)."""
    from scheduler.auto_scheduler import trigger_ml_retrain_now, _state
    if _state.ml_retrain_running:
        return {"message": "ML retrain already running", "status": "busy"}
    background_tasks.add_task(trigger_ml_retrain_now)
    return {"message": "ML retrain triggered", "status": "started"}


# -------------------------------------------------------
# Static files
# -------------------------------------------------------

@app.get("/")
async def get_index():
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
