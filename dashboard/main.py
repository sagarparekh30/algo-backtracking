import os
import json
import asyncio
import subprocess
import re
from datetime import datetime
from typing import List, Optional, Dict

from fastapi import FastAPI, BackgroundTasks, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer
from pydantic import BaseModel

# Import existing settings
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

from config.settings import TOKEN_PATH, LOG_DIR, TABLE_NAME
from db.connection import get_conn, get_engine
from strategies.swing_executor import StrategyManager, STRATEGY_DESCRIPTIONS
from dashboard.auth import require_admin, verify_password, create_access_token
from config.settings import ADMIN_USERNAME, ADMIN_PASSWORD_HASH

app = FastAPI(title="Trading HQ Dashboard")

# ── Shared ML predictor singleton (model stays in memory) ────────────────────
# Loaded once on first use; reloaded after training via reload_ml_predictor()
_ml_predictor = None

def get_ml_predictor():
    """Return the shared MLPredictor, loading from disk if needed."""
    global _ml_predictor
    if _ml_predictor is None:
        from ml.model import MLPredictor
        _ml_predictor = MLPredictor()
        _ml_predictor.load()
    return _ml_predictor

def reload_ml_predictor():
    """Force a fresh load from disk (call after training completes)."""
    global _ml_predictor
    from ml.model import MLPredictor
    p = MLPredictor()
    p.load()
    _ml_predictor = p
    return _ml_predictor


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


class ExecuteCache:
    """Cached result of the last combined signals scan."""
    results: list = []
    count: int = 0
    top_symbols: list = []
    regime: str = "Unknown"
    buy_threshold: float = 0.60
    last_checked: datetime = None
    is_refreshing: bool = False


state = DashboardState()
daily_run_state = DailyRunState()
yf_state = YFinanceState()
execute_cache = ExecuteCache()


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
# Auth endpoints
# -------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
async def login(body: LoginRequest):
    """
    Authenticate admin user. Returns a JWT bearer token on success.

    POST /api/auth/login
    Body: { "username": "admin", "password": "..." }
    """
    if not ADMIN_PASSWORD_HASH:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin password not configured. Set ADMIN_PASSWORD_HASH in .env",
        )
    if body.username != ADMIN_USERNAME or not verify_password(body.password, ADMIN_PASSWORD_HASH):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_access_token(body.username)
    return {"access_token": token, "token_type": "bearer"}


@app.get("/api/auth/me")
async def auth_me(username: str = Depends(require_admin)):
    """Check if the current token is valid and return the username."""
    return {"username": username, "role": "admin"}


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
async def start_backfill(
    background_tasks: BackgroundTasks,
    _: str = Depends(require_admin),
):
    """Start a full data backfill. Admin only."""
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


@app.get("/api/combined/signals")
async def combined_signals():
    """
    Combined signals: strategy setup + ML probability filter.

    Flow:
      1. Run all 16 strategies → find stocks with a valid entry setup
      2. Score each of those stocks through the ML model
      3. Keep only stocks where ML probability >= regime-adjusted BUY threshold
      4. Return sorted by probability desc, then strategy count desc

    Each result includes the full trade plan (entry, stop, target) from the
    strategy PLUS the ML conviction score and expected return %.
    """
    import pandas as pd
    from ml.model import THRESHOLDS
    from ml.regime import compute_regime

    # ── Step 1: Market regime ───────────────────────────────────────────
    try:
        regime_data = compute_regime()
        regime      = regime_data.get("regime", "Neutral")
    except Exception:
        regime_data = {}
        regime      = "Neutral"

    buy_thresh, avoid_thresh = THRESHOLDS.get(regime, THRESHOLDS["Neutral"])

    # ── Step 2: Run all strategies ──────────────────────────────────────
    try:
        manager     = StrategyManager()
        all_signals = manager.get_all_signals()   # {strategy_id: [signal, ...]}
    except Exception as e:
        return {"error": f"Strategy scan failed: {e}", "results": []}

    # Collect per-symbol: all strategies that fired + best trade params
    symbol_map: dict = {}
    for strategy_id, signals in all_signals.items():
        for sig in signals:
            sym = sig.get("symbol", "")
            if not sym:
                continue
            if sym not in symbol_map:
                symbol_map[sym] = {
                    "strategies": [],
                    "entry":      sig.get("entry"),
                    "stop_loss":  sig.get("stop_loss"),
                    "target":     sig.get("target"),
                    "atr":        sig.get("atr"),
                }
            symbol_map[sym]["strategies"].append(strategy_id)

    if not symbol_map:
        return {"results": [], "count": 0, "regime": regime_data,
                "message": "No strategy signals found today"}

    # ── Step 3: Get cached ML model ──────────────────────────────────────
    predictor = get_ml_predictor()
    if predictor.clf is None:
        return {"error": "ML model not trained — POST /api/ml/train first",
                "results": []}

    engine = get_engine()

    # ── Step 4: Score each symbol with the ML model ──────────────────────
    results = []
    for sym, info in symbol_map.items():
        try:
            df = pd.read_sql(
                f"SELECT trade_date, open, high, low, close, volume "
                f"FROM {TABLE_NAME} WHERE symbol = %(sym)s ORDER BY trade_date ASC",
                engine, params={"sym": sym},
            )
            if df.empty or len(df) < 260:
                continue

            ml = predictor.predict_symbol(df, sym, regime=regime)
            if "error" in ml:
                continue

            prob = ml.get("buy_probability", 0)

            # Only keep if ML agrees with the strategy (above regime BUY threshold)
            if prob < buy_thresh:
                continue

            # Live price overlay
            price        = ml.get("price", 0)
            price_source = "last_close"
            tick         = live_feed.get(sym)
            if tick and tick.get("ltp"):
                ltp          = float(tick["ltp"])
                price        = ltp
                price_source = "live"
                exp_ret      = ml.get("expected_return_pct")
                if exp_ret is not None and ltp > 0:
                    ml["price_target"] = round(ltp * (1 + exp_ret / 100), 2)

            strategy_count = len(info["strategies"])

            results.append({
                "symbol":              sym,
                "buy_probability":     prob,
                "confidence":          ml.get("confidence"),
                "expected_return_pct": ml.get("expected_return_pct"),
                "price_target":        ml.get("price_target"),
                "price":               price,
                "price_source":        price_source,
                # Trade plan from strategy
                "entry":               info["entry"],
                "stop_loss":           info["stop_loss"],
                "target":              info["target"],
                "atr":                 info["atr"],
                # Strategy agreement
                "strategies":          info["strategies"],
                "strategy_count":      strategy_count,
                "regime":              regime,
                "buy_threshold_used":  buy_thresh,
            })

        except Exception as e:
            print(f"Combined signal error for {sym}: {e}")

    # Sort: primary = ML probability, secondary = strategy count
    results.sort(
        key=lambda x: (x["buy_probability"], x["strategy_count"]),
        reverse=True,
    )

    total_strategy_signals = sum(len(v) for v in all_signals.values())

    # Update the global cache so /api/execute/poll can serve it
    execute_cache.results       = results
    execute_cache.count         = len(results)
    execute_cache.top_symbols   = [r["symbol"] for r in results[:5]]
    execute_cache.regime        = regime
    execute_cache.buy_threshold = buy_thresh
    execute_cache.last_checked  = datetime.now()

    return {
        "results":                results,
        "count":                  len(results),
        "regime":                 regime_data,
        "buy_threshold":          buy_thresh,
        "symbols_with_signals":   len(symbol_map),
        "ml_confirmed":           len(results),
        "total_strategy_signals": total_strategy_signals,
    }


async def _refresh_execute_cache():
    """Background task: re-run combined signals and update cache."""
    if execute_cache.is_refreshing:
        return
    execute_cache.is_refreshing = True
    try:
        import pandas as pd
        from ml.model import THRESHOLDS
        from ml.regime import compute_regime

        regime_data = compute_regime()
        regime      = regime_data.get("regime", "Neutral")
        buy_thresh, _ = THRESHOLDS.get(regime, THRESHOLDS["Neutral"])

        manager     = StrategyManager()
        all_signals = manager.get_all_signals()

        symbol_map: dict = {}
        for strategy_id, sigs in all_signals.items():
            for sig in sigs:
                sym = sig.get("symbol", "")
                if not sym:
                    continue
                if sym not in symbol_map:
                    symbol_map[sym] = {
                        "strategies": [], "entry": sig.get("entry"),
                        "stop_loss": sig.get("stop_loss"), "target": sig.get("target"),
                        "atr": sig.get("atr"),
                    }
                symbol_map[sym]["strategies"].append(strategy_id)

        predictor = get_ml_predictor()
        if predictor.clf is None:
            return

        engine  = get_engine()
        results = []
        for sym, info in symbol_map.items():
            try:
                df = pd.read_sql(
                    f"SELECT trade_date, open, high, low, close, volume "
                    f"FROM {TABLE_NAME} WHERE symbol = %(sym)s ORDER BY trade_date ASC",
                    engine, params={"sym": sym},
                )
                if df.empty or len(df) < 260:
                    continue
                ml = predictor.predict_symbol(df, sym, regime=regime)
                if "error" in ml or ml.get("buy_probability", 0) < buy_thresh:
                    continue
                tick = live_feed.get(sym)
                price = ml.get("price", 0)
                price_source = "last_close"
                if tick and tick.get("ltp"):
                    price = float(tick["ltp"])
                    price_source = "live"
                results.append({
                    "symbol": sym,
                    "buy_probability": ml.get("buy_probability", 0),
                    "confidence": ml.get("confidence"),
                    "expected_return_pct": ml.get("expected_return_pct"),
                    "price_target": ml.get("price_target"),
                    "price": price, "price_source": price_source,
                    "entry": info["entry"], "stop_loss": info["stop_loss"],
                    "target": info["target"], "atr": info["atr"],
                    "strategies": info["strategies"],
                    "strategy_count": len(info["strategies"]),
                })
            except Exception:
                continue

        results.sort(key=lambda x: (x["buy_probability"], x["strategy_count"]), reverse=True)

        prev_count = execute_cache.count
        execute_cache.results       = results
        execute_cache.count         = len(results)
        execute_cache.top_symbols   = [r["symbol"] for r in results[:5]]
        execute_cache.regime        = regime
        execute_cache.buy_threshold = buy_thresh
        execute_cache.last_checked  = datetime.now()

        # Send Telegram alert if new trades appeared
        if len(results) > 0 and (prev_count == 0 or len(results) != prev_count):
            try:
                from alerts.telegram_bot import TelegramAlert
                TelegramAlert().send_execute_alert(
                    results, regime, int(buy_thresh * 100)
                )
            except Exception as e:
                print(f"Telegram execute alert error: {e}")

    except Exception as e:
        print(f"Execute cache refresh error: {e}")
    finally:
        execute_cache.is_refreshing = False


@app.get("/api/execute/poll")
async def execute_poll(background_tasks: BackgroundTasks):
    """
    Lightweight poll endpoint for the Execute tab notification system.

    Returns the cached combined signals count and top symbols.
    If the cache is older than 5 minutes, triggers a background refresh
    and returns the stale data immediately (non-blocking).

    Frontend calls this every 5 minutes to check for new trade ideas
    without running the full heavy scan on every poll.
    """
    from datetime import timedelta

    cache_age_secs = None
    if execute_cache.last_checked:
        cache_age_secs = (datetime.now() - execute_cache.last_checked).total_seconds()

    # Trigger background refresh if cache is stale (> 5 min) or empty
    if cache_age_secs is None or cache_age_secs > 300:
        background_tasks.add_task(_refresh_execute_cache)

    return {
        "count":          execute_cache.count,
        "top_symbols":    execute_cache.top_symbols,
        "regime":         execute_cache.regime,
        "buy_threshold":  execute_cache.buy_threshold,
        "last_checked":   execute_cache.last_checked.isoformat() if execute_cache.last_checked else None,
        "is_refreshing":  execute_cache.is_refreshing,
        "cache_age_secs": int(cache_age_secs) if cache_age_secs is not None else None,
    }


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
        # Reload the singleton so all endpoints immediately use the new model
        reload_ml_predictor()
    except Exception as e:
        print(f"ML Training Error: {e}")
        ml_train_state.last_result = {"error": str(e)}
    finally:
        ml_train_state.is_training = False


@app.post("/api/ml/train")
async def ml_train(
    background_tasks: BackgroundTasks,
    _: str = Depends(require_admin),
):
    """
    Train the ML model in the background. Admin only.
    Uses all symbols in the DB — takes 2-5 minutes.
    """
    if ml_train_state.is_training:
        return {"message": "Training already in progress", "status": "busy"}
    background_tasks.add_task(_run_ml_training)
    return {"message": "Training started", "status": "started"}


@app.get("/api/ml/train/status")
async def ml_train_status():
    """Return ML training status and last result."""
    predictor = get_ml_predictor()
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
        from ml.regime import compute_regime

        predictor = get_ml_predictor()
        if predictor.clf is None:
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
        from ml.regime import compute_regime

        predictor = get_ml_predictor()
        if predictor.clf is None:
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


@app.get("/api/ml/data-validation")
async def ml_data_validation():
    """
    Validate what data the model was actually trained on.
    Returns per-symbol date ranges, total rows, years span.
    """
    from ml.model import META_PATH
    import json as _json

    # Load model meta for train_samples, symbols_used, trained_at
    meta = {}
    try:
        if os.path.exists(META_PATH):
            with open(META_PATH) as f:
                meta = _json.load(f)
    except Exception:
        pass

    # Query actual DB coverage per symbol
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    symbol,
                    MIN(trade_date) AS oldest,
                    MAX(trade_date) AS latest,
                    COUNT(*)        AS rows
                FROM {TABLE_NAME}
                GROUP BY symbol
                ORDER BY symbol
            """)
            cols = [d[0] for d in cur.description]
            sym_rows = [dict(zip(cols, r)) for r in cur.fetchall()]

            cur.execute(f"""
                SELECT
                    MIN(trade_date) AS oldest,
                    MAX(trade_date) AS latest,
                    COUNT(*)        AS total_rows,
                    COUNT(DISTINCT symbol) AS total_symbols
                FROM {TABLE_NAME}
            """)
            summary = cur.fetchone()

        for r in sym_rows:
            r["oldest"] = str(r["oldest"])
            r["latest"] = str(r["latest"])
            oldest_dt = r["oldest"]
            latest_dt = r["latest"]
            try:
                from datetime import date as _date
                o = _date.fromisoformat(r["oldest"])
                l = _date.fromisoformat(r["latest"])
                r["years"] = round((l - o).days / 365.25, 1)
            except Exception:
                r["years"] = None

        oldest_overall = str(summary[0]) if summary[0] else None
        latest_overall = str(summary[1]) if summary[1] else None
        total_rows     = int(summary[2]) if summary[2] else 0
        total_symbols  = int(summary[3]) if summary[3] else 0

        years_span = None
        if oldest_overall and latest_overall:
            from datetime import date as _date
            years_span = round(
                (_date.fromisoformat(latest_overall) - _date.fromisoformat(oldest_overall)).days / 365.25, 1
            )

        return {
            "db_coverage": {
                "oldest":        oldest_overall,
                "latest":        latest_overall,
                "total_rows":    total_rows,
                "total_symbols": total_symbols,
                "years_span":    years_span,
            },
            "model_meta": {
                "train_samples": meta.get("train_samples"),
                "test_samples":  meta.get("test_samples"),
                "symbols_used":  meta.get("symbols_used"),
                "trained_at":    meta.get("trained_at"),
                "auc_roc":       meta.get("auc_roc"),
            },
            "per_symbol": sym_rows,
        }
    finally:
        conn.close()


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
        predictor = get_ml_predictor()
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

async def _run_yfinance_task(symbols: list = None):
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
        result = run_yfinance_backfill(symbols=symbols, progress_cb=progress_cb)
        yf_state.last_result = result
        yf_state.current_symbol = "Done"
    except Exception as e:
        print(f"YFinance backfill error: {e}")
        yf_state.last_result = {"error": str(e)}
        yf_state.current_symbol = "Error"
    finally:
        yf_state.is_running = False


@app.post("/api/start_yfinance_backfill")
async def start_yfinance_backfill(
    background_tasks: BackgroundTasks,
    index: str = None,
    _: str = Depends(require_admin),
):
    """
    Fetch full historical data (up to 30 years) for all symbols from Yahoo Finance.
    Optional ?index=NIFTY+50 to limit to a specific index.
    Runs in background — poll /api/yfinance/status for progress. Admin only.
    """
    if yf_state.is_running:
        return {"message": "Yahoo Finance backfill already running", "status": "busy"}

    symbols = None
    index_label = "All symbols"
    if index:
        from config.nse_indices import get_index_symbols
        symbols = get_index_symbols(index)
        if not symbols:
            raise HTTPException(status_code=400, detail=f"Unknown index: {index}")
        index_label = f"{index} ({len(symbols)} symbols)"

    background_tasks.add_task(_run_yfinance_task, symbols)
    return {"message": f"Yahoo Finance backfill started for {index_label}", "status": "started"}


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
async def scheduler_run_data_now(
    background_tasks: BackgroundTasks,
    _: str = Depends(require_admin),
):
    """Manually trigger a data update right now (outside schedule). Admin only."""
    from scheduler.auto_scheduler import trigger_data_update_now, _state
    if _state.data_update_running:
        return {"message": "Data update already running", "status": "busy"}
    background_tasks.add_task(trigger_data_update_now)
    return {"message": "Data update triggered", "status": "started"}


@app.post("/api/scheduler/run_ml_retrain")
async def scheduler_run_ml_now(
    background_tasks: BackgroundTasks,
    _: str = Depends(require_admin),
):
    """Manually trigger an ML retrain right now (outside schedule). Admin only."""
    from scheduler.auto_scheduler import trigger_ml_retrain_now, _state
    if _state.ml_retrain_running:
        return {"message": "ML retrain already running", "status": "busy"}
    background_tasks.add_task(trigger_ml_retrain_now)
    return {"message": "ML retrain triggered", "status": "started"}


# -------------------------------------------------------
# Index registry endpoints
# -------------------------------------------------------

@app.get("/api/indices")
async def list_indices():
    """Return all available NSE indices with metadata (name, category, count)."""
    from config.nse_indices import list_indices as _list
    return {"indices": _list()}


@app.get("/api/indices/{index_name}/symbols")
async def index_symbols(index_name: str):
    """Return the symbol list for a specific index."""
    from config.nse_indices import get_index_symbols, INDEX_REGISTRY
    # URL may encode spaces as %20 — FastAPI decodes automatically
    if index_name not in INDEX_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown index: {index_name}")
    symbols = get_index_symbols(index_name)
    return {"index": index_name, "count": len(symbols), "symbols": symbols}


# -------------------------------------------------------
# Screener endpoints
# -------------------------------------------------------

@app.get("/api/screener/rs")
async def screener_rs(index: str = None):
    """Relative Strength scan — stocks ranked by RS vs universe.
    Optional ?index=NIFTY+50 to scope to a specific index.
    """
    from strategies.screener import relative_strength_scan
    symbols = None
    if index:
        from config.nse_indices import get_index_symbols
        symbols = get_index_symbols(index) or None
    return {"results": relative_strength_scan(symbols=symbols), "index": index or "NIFTY 100"}


@app.get("/api/screener/52w")
async def screener_52w(index: str = None):
    """52-week high scan — stocks near breakout levels."""
    from strategies.screener import fiftytwo_week_scan
    symbols = None
    if index:
        from config.nse_indices import get_index_symbols
        symbols = get_index_symbols(index) or None
    return {"results": fiftytwo_week_scan(symbols=symbols), "index": index or "NIFTY 100"}


@app.get("/api/screener/volume")
async def screener_volume(min_multiplier: float = 2.0, index: str = None):
    """Volume spike scan — unusual volume vs 20-day average."""
    from strategies.screener import volume_spike_scan
    symbols = None
    if index:
        from config.nse_indices import get_index_symbols
        symbols = get_index_symbols(index) or None
    return {"results": volume_spike_scan(min_multiplier, symbols=symbols), "index": index or "NIFTY 100"}


@app.get("/api/sector/heatmap")
async def sector_heatmap():
    """Sector performance heatmap — 1D/1W/1M/3M returns by sector."""
    from strategies.screener import sector_heatmap as _heatmap
    return {"sectors": _heatmap()}


@app.get("/api/earnings")
async def earnings_calendar(days_ahead: int = 21, index: str = None):
    """Upcoming earnings dates (next N days). Optional ?index= to scope."""
    from strategies.screener import earnings_calendar as _cal
    symbols = None
    if index:
        from config.nse_indices import get_index_symbols
        symbols = get_index_symbols(index) or None
    return {"results": _cal(days_ahead, symbols=symbols), "days_ahead": days_ahead, "index": index or "NIFTY 100"}


# -------------------------------------------------------
# Trade Journal
# -------------------------------------------------------

class TradeLogRequest(BaseModel):
    symbol:               str
    entry_price:          float
    stop_loss:            float   = None
    target:               float   = None
    strategy_tags:        str     = ""
    trade_type:           str     = "paper"   # paper | live
    notes:                str     = ""
    buy_probability:      float   = None
    expected_return_pct:  float   = None
    quantity:             int     = 0


class TradeCloseRequest(BaseModel):
    exit_price: float
    notes:      str = ""


@app.post("/api/journal/trade")
async def journal_log_trade(
    body: TradeLogRequest,
    _: str = Depends(require_admin),
):
    """Log a trade to the journal (paper or live)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trade_journal
                    (symbol, strategy_tags, entry_price, stop_loss, target,
                     trade_type, notes, buy_probability, expected_return_pct, quantity)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (
                body.symbol.upper(), body.strategy_tags, body.entry_price,
                body.stop_loss, body.target, body.trade_type, body.notes,
                body.buy_probability, body.expected_return_pct, body.quantity,
            ))
            trade_id = cur.fetchone()[0]
        conn.commit()
        return {"id": trade_id, "message": "Trade logged"}
    finally:
        conn.close()


@app.get("/api/journal/trades")
async def journal_list_trades(status: str = None, limit: int = 200):
    """List trades — optionally filtered by status (open/win/loss/stopped/cancelled)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if status:
                cur.execute("""
                    SELECT id, symbol, strategy_tags, entry_price, stop_loss, target,
                           entry_date, exit_date, exit_price, status, trade_type,
                           notes, buy_probability, expected_return_pct, actual_return_pct,
                           quantity, pnl, created_at
                    FROM trade_journal WHERE status=%s
                    ORDER BY created_at DESC LIMIT %s
                """, (status, limit))
            else:
                cur.execute("""
                    SELECT id, symbol, strategy_tags, entry_price, stop_loss, target,
                           entry_date, exit_date, exit_price, status, trade_type,
                           notes, buy_probability, expected_return_pct, actual_return_pct,
                           quantity, pnl, created_at
                    FROM trade_journal ORDER BY created_at DESC LIMIT %s
                """, (limit,))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            # Convert dates/datetimes to strings
            for r in rows:
                for k in ("entry_date","exit_date","created_at"):
                    if r.get(k): r[k] = str(r[k])
                for k in ("entry_price","stop_loss","target","exit_price","pnl",
                          "buy_probability","expected_return_pct","actual_return_pct"):
                    if r.get(k) is not None: r[k] = float(r[k])
        return {"trades": rows, "total": len(rows)}
    finally:
        conn.close()


@app.get("/api/journal/stats")
async def journal_stats():
    """Return aggregate stats: win rate, avg R:R, total P&L, streaks."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*)                                          AS total,
                    COUNT(*) FILTER (WHERE status='open')            AS open_trades,
                    COUNT(*) FILTER (WHERE status='win')             AS wins,
                    COUNT(*) FILTER (WHERE status='loss')            AS losses,
                    COUNT(*) FILTER (WHERE status='stopped')         AS stopped,
                    ROUND(AVG(actual_return_pct) FILTER (WHERE status IN ('win','loss','stopped'))::numeric, 2)
                                                                     AS avg_return_pct,
                    ROUND(SUM(pnl) FILTER (WHERE pnl IS NOT NULL)::numeric, 2)
                                                                     AS total_pnl,
                    MAX(actual_return_pct) FILTER (WHERE status='win') AS best_trade_pct,
                    MIN(actual_return_pct) FILTER (WHERE status IN ('loss','stopped')) AS worst_trade_pct,
                    COUNT(*) FILTER (WHERE trade_type='paper')       AS paper_count,
                    COUNT(*) FILTER (WHERE trade_type='live')        AS live_count
                FROM trade_journal
            """)
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
            stats = dict(zip(cols, row))

            closed = (stats["wins"] or 0) + (stats["losses"] or 0) + (stats["stopped"] or 0)
            stats["closed_trades"] = closed
            stats["win_rate"] = round((stats["wins"] or 0) / closed * 100, 1) if closed > 0 else None

            for k in stats:
                if stats[k] is not None:
                    try: stats[k] = float(stats[k]) if '.' in str(stats[k]) else int(stats[k])
                    except: pass

        return stats
    finally:
        conn.close()


@app.put("/api/journal/trade/{trade_id}/close")
async def journal_close_trade(
    trade_id: int,
    body: TradeCloseRequest,
    _: str = Depends(require_admin),
):
    """Manually close a trade with an exit price."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT entry_price, stop_loss, quantity FROM trade_journal WHERE id=%s",
                (trade_id,)
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Trade not found")
            entry_price, stop_loss, qty = float(row[0]), row[1], row[2] or 0

            actual_return_pct = round((body.exit_price - entry_price) / entry_price * 100, 2)
            pnl = round((body.exit_price - entry_price) * qty, 2) if qty else None

            # Determine outcome
            if actual_return_pct >= 2.0:
                status = "win"
            elif stop_loss and body.exit_price <= float(stop_loss):
                status = "stopped"
            else:
                status = "loss"

            cur.execute("""
                UPDATE trade_journal SET
                    exit_date=CURRENT_DATE, exit_price=%s, status=%s,
                    actual_return_pct=%s, pnl=%s,
                    notes=CASE WHEN %s != '' THEN %s ELSE notes END
                WHERE id=%s
            """, (body.exit_price, status, actual_return_pct, pnl,
                  body.notes, body.notes, trade_id))
        conn.commit()
        return {"id": trade_id, "status": status, "actual_return_pct": actual_return_pct, "pnl": pnl}
    finally:
        conn.close()


@app.delete("/api/journal/trade/{trade_id}")
async def journal_delete_trade(
    trade_id: int,
    _: str = Depends(require_admin),
):
    """Delete a trade from the journal."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM trade_journal WHERE id=%s RETURNING id", (trade_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Trade not found")
        conn.commit()
        return {"deleted": trade_id}
    finally:
        conn.close()


@app.post("/api/journal/settle")
async def journal_settle_paper(
    _: str = Depends(require_admin),
):
    """
    Auto-settle paper trades older than 5 days.
    Looks up actual close price from the DB and marks WIN / LOSS / STOPPED.
    """
    from config.settings import TABLE_NAME
    conn = get_conn()
    settled = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, symbol, entry_price, stop_loss, entry_date
                FROM trade_journal
                WHERE status='open' AND trade_type='paper'
                  AND entry_date <= CURRENT_DATE - INTERVAL '5 days'
            """)
            open_trades = cur.fetchall()

            for trade_id, symbol, entry_price, stop_loss, entry_date in open_trades:
                entry_price = float(entry_price)
                # Get the close price 5 trading days after entry
                cur.execute(f"""
                    SELECT close FROM {TABLE_NAME}
                    WHERE symbol=%s AND trade_date > %s
                    ORDER BY trade_date ASC LIMIT 1 OFFSET 4
                """, (symbol, entry_date))
                price_row = cur.fetchone()
                if not price_row:
                    continue

                exit_price = float(price_row[0])
                actual_return_pct = round((exit_price - entry_price) / entry_price * 100, 2)

                if actual_return_pct >= 2.0:
                    status = "win"
                elif stop_loss and exit_price <= float(stop_loss):
                    status = "stopped"
                else:
                    status = "loss"

                cur.execute("""
                    UPDATE trade_journal SET
                        exit_date=entry_date + INTERVAL '5 days',
                        exit_price=%s, status=%s, actual_return_pct=%s,
                        notes='Auto-settled after 5 days'
                    WHERE id=%s
                """, (exit_price, status, actual_return_pct, trade_id))

                settled.append({
                    "id": trade_id, "symbol": symbol,
                    "status": status, "actual_return_pct": actual_return_pct,
                })

        conn.commit()
        return {"settled": len(settled), "trades": settled}
    finally:
        conn.close()


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
