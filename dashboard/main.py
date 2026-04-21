import logging
import math
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


_CANDLE_LOOKBACK_DAYS = 730   # ~505 trading days — enough for EMA200 + any indicator


def _bulk_fetch_candles(engine, symbols: list | None = None) -> dict:
    """
    Fetch last _CANDLE_LOOKBACK_DAYS of OHLCV for all (or specified) symbols
    in ONE query, return dict {symbol: DataFrame}.

    Replaces the pattern of 100 individual pd.read_sql calls in loops.
    """
    sym_filter = ""
    params: dict = {}
    if symbols:
        sym_filter = "AND symbol = ANY(%(syms)s)"
        params["syms"] = symbols

    df_all = pd.read_sql(
        f"""
        SELECT symbol, trade_date, open, high, low, close, volume
        FROM {TABLE_NAME}
        WHERE trade_date >= CURRENT_DATE - INTERVAL '{_CANDLE_LOOKBACK_DAYS} days'
        {sym_filter}
        ORDER BY symbol, trade_date ASC
        """,
        engine,
        params=params or None,
    )
    if df_all.empty:
        return {}
    return {sym: grp.reset_index(drop=True) for sym, grp in df_all.groupby("symbol", sort=False)}


def _sanitize(obj):
    """Recursively replace NaN/Inf floats with None so JSON serialization never fails."""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, (np.floating, np.integer)):
        v = float(obj)
        return None if (math.isnan(v) or math.isinf(v)) else v
    return obj

import os
import json
import asyncio
import subprocess
import re
from datetime import datetime
from typing import Optional, Dict

from fastapi import FastAPI, BackgroundTasks, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import sqlalchemy

# Import existing settings
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

from config.settings import TOKEN_PATH, LOG_DIR, TABLE_NAME
from db.connection import get_conn, get_engine
from strategies.swing_executor import StrategyManager, STRATEGY_DESCRIPTIONS
from dashboard.auth import require_admin, require_user

app = FastAPI(title="TradeGenie")

# Serve dashboard static assets (dashboard.js, etc.)
_dashboard_dir = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=_dashboard_dir), name="static")

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
    _run_db_migrations()
    # Auto-bootstrap: fetch 30-year history + train model if DB is empty
    from scheduler.bootstrap import run_bootstrap_if_needed
    run_bootstrap_if_needed()
    # Auto-train if DB has data but model file is missing or never trained
    import threading
    threading.Thread(target=_auto_train_if_needed, name="auto-train-check", daemon=True).start()
    # Pre-warm slow caches so first user request is fast
    threading.Thread(target=_prewarm_caches, name="cache-prewarm", daemon=True).start()


def _auto_train_if_needed():
    """
    On startup: if the DB has enough data but the ML model has never been
    trained (no model file), kick off a training run immediately without
    waiting for the daily schedule.
    """
    import time as _t
    import os as _os
    _t.sleep(5)   # wait for DB pool + bootstrap check to settle

    try:
        from config.settings import BASE_DIR
        model_file = _os.path.join(str(BASE_DIR), "ml", "models", "price_predictor.joblib")
        meta_file  = _os.path.join(str(BASE_DIR), "ml", "models", "model_meta.json")

        if _os.path.exists(model_file) and _os.path.exists(meta_file):
            logger.info("[AutoTrain] Model already exists — skipping startup train.")
            return

        # Check if there's enough data to train
        from db.connection import get_conn
        from config.settings import TABLE_NAME
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
            row = cur.fetchone()
        conn.close()
        row_count = row[0] if row else 0

        if row_count < 10_000:
            logger.info(f"[AutoTrain] Not enough data ({row_count:,} rows) — skipping.")
            return

        logger.info(f"[AutoTrain] Model not found but DB has {row_count:,} rows — training now…")
        from scheduler.auto_scheduler import trigger_ml_retrain_now
        trigger_ml_retrain_now()

    except Exception as e:
        logger.error(f"[AutoTrain] Startup check failed: {e}")


def _prewarm_caches():
    """Pre-warm in-process caches that are expensive to compute cold."""
    import time as _t
    _t.sleep(2)   # wait for DB pool to settle
    try:
        get_db_stats()
    except Exception:
        pass
    try:
        from db.connection import get_conn
        global _snapshot_cache, _snapshot_cache_ts
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT symbol, trade_date, open, high, low, close, volume
                FROM (
                    SELECT DISTINCT ON (symbol)
                        symbol, trade_date, open, high, low, close, volume
                    FROM {TABLE_NAME}
                    ORDER BY symbol, trade_date DESC
                ) latest
                ORDER BY trade_date DESC
                LIMIT 10
            """)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        conn.close()
        _snapshot_cache = rows
        _snapshot_cache_ts = _time.monotonic()
    except Exception:
        pass
    try:
        global _db_sources_cache, _db_sources_cache_ts
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT source, COUNT(*) as rows, COUNT(DISTINCT symbol) as symbols, "
                f"MIN(trade_date) as from_date, MAX(trade_date) as to_date "
                f"FROM {TABLE_NAME} GROUP BY source ORDER BY rows DESC"
            )
            rows = cur.fetchall()
        conn.close()
        _db_sources_cache = [
            {"source": r[0], "rows": r[1], "symbols": r[2],
             "from_date": str(r[3]) if r[3] else None,
             "to_date":   str(r[4]) if r[4] else None}
            for r in rows
        ]
        _db_sources_cache_ts = _time.monotonic()
    except Exception:
        pass
    try:
        from ml.regime import compute_regime
        global _regime_cache, _regime_cache_ts
        _regime_cache = compute_regime()
        _regime_cache_ts = _time.monotonic()
    except Exception:
        pass
    try:
        global _predict_all_cache, _predict_all_cache_ts
        predictor = get_ml_predictor()
        if predictor.clf is not None:
            regime = _regime_cache.get("regime", "Neutral") if _regime_cache else "Neutral"
            results = predictor.predict_all(regime=regime)
            _predict_all_cache = {
                "results": results,
                "count": len(results),
                "regime": _regime_cache or {},
                "live_count": 0,
                "horizons_available": {
                    "3d": predictor.clf_3d is not None,
                    "5d": predictor.clf_5d is not None,
                    "10d": predictor.clf_10d is not None,
                },
            }
            _predict_all_cache_ts = _time.monotonic()
    except Exception:
        pass
    try:
        # Pre-warm strategy scan cache (runs all 50 strategies once on startup)
        manager = StrategyManager()
        manager.get_all_signals()
    except Exception:
        pass


def _run_db_migrations():
    """Auto-apply any pending SQL migrations on startup."""
    import glob as _glob
    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "db", "migrations"
    )
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            # Ensure migration tracking table exists
            cur.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR(50) PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    description TEXT
                )
            """)
            cur.execute("SELECT version FROM schema_migrations")
            applied = {r[0] for r in cur.fetchall()}

        sql_files = sorted(_glob.glob(os.path.join(migrations_dir, "*.sql")))
        for path in sql_files:
            version = os.path.basename(path).split("_")[0]
            if version in applied:
                continue
            with open(path) as f:
                sql = f.read()
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
            print(f"[Migration] Applied {os.path.basename(path)}")
    except Exception as e:
        print(f"[Migration] Error: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


@app.on_event("shutdown")
async def shutdown_event():
    from scheduler.auto_scheduler import stop_scheduler
    stop_scheduler()

# Gzip compression — compresses responses > 1KB (HTML/JS/JSON)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Enable CORS
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


class ValueScanState:
    is_running = False
    processed = 0
    total = 0
    current_symbol = "Idle"
    started_at: str = None
    error: str = None


value_scan_state = ValueScanState()


class ExecuteCache:
    """Cached result of the last combined signals scan."""
    results: list = []
    count: int = 0
    top_symbols: list = []
    top_results: list = []    # top-5 full trade objects for the Home tab
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

import time as _time

_db_stats_cache_ts: float = 0.0
_DB_STATS_TTL = 60.0   # re-query DB at most once per 60 s

# ── Per-endpoint result caches ───────────────────────────────────────────────
_snapshot_cache: list = []
_snapshot_cache_ts: float = 0.0
_SNAPSHOT_TTL = 60.0        # latest_snapshot changes once per day at most

_db_sources_cache: list = []
_db_sources_cache_ts: float = 0.0
_DB_SOURCES_TTL = 300.0     # db/sources full scan — cache 5 min

_predict_all_cache: dict = {}
_predict_all_cache_ts: float = 0.0
_PREDICT_ALL_TTL = 120.0    # ml/predict/all — cache 2 min

_combined_signals_cache: dict | None = None
_combined_signals_cache_ts: float = 0.0
_COMBINED_SIGNALS_TTL = 300.0   # combined/signals — cache 5 min

def get_db_stats():
    """Fetch database health metrics — cached for 60 s to avoid COUNT(*) on every request."""
    global _db_stats_cache_ts
    now = _time.monotonic()
    if now - _db_stats_cache_ts < _DB_STATS_TTL:
        return   # serve from cached state
    try:
        conn = get_conn()
        cursor = conn.cursor()

        # Table size (fast — metadata only)
        cursor.execute(
            "SELECT pg_total_relation_size(%s) / 1048576.0",
            (TABLE_NAME,),
        )
        row = cursor.fetchone()
        state.db_size_mb = round(float(row[0] or 0), 2)

        # Use pg_class reltuples for fast approximate row count
        # Fall back to exact COUNT only when table is empty / reltuples is 0
        cursor.execute(
            "SELECT reltuples::bigint FROM pg_class WHERE relname = %s",
            (TABLE_NAME,),
        )
        approx = cursor.fetchone()
        approx_rows = int(approx[0]) if approx and approx[0] and approx[0] > 0 else 0

        if approx_rows > 0:
            state.total_db_rows = approx_rows
            # Symbols / date range from a much cheaper recent-sample query
            cursor.execute(
                f"SELECT COUNT(DISTINCT symbol), MIN(trade_date), MAX(trade_date) FROM {TABLE_NAME}"
            )
            syms, d1, d2 = cursor.fetchone()
            state.unique_symbols = syms or 0
            state.min_date = str(d1) if d1 else "N/A"
            state.max_date = str(d2) if d2 else "N/A"
        else:
            # Table just created — full scan is cheap
            cursor.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(trade_date), MAX(trade_date) FROM {TABLE_NAME}"
            )
            rows, syms, d1, d2 = cursor.fetchone()
            state.total_db_rows  = rows or 0
            state.unique_symbols = syms or 0
            state.min_date = str(d1) if d1 else "N/A"
            state.max_date = str(d2) if d2 else "N/A"

        conn.close()
        _db_stats_cache_ts = now   # update cache timestamp only on success
    except Exception as e:
        print(f"DB Stat Error: {e}")


_log_cache_ts: float = 0.0
_LOG_TTL = 5.0   # re-read log at most every 5 s

def parse_log_for_summary():
    """Parses the log file to update the session state — cached for 5 s."""
    global _log_cache_ts
    now = _time.monotonic()
    if now - _log_cache_ts < _LOG_TTL:
        return
    _log_cache_ts = now
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
async def get_latest_snapshot(_: dict = Depends(require_user)):
    global _snapshot_cache, _snapshot_cache_ts
    now = _time.monotonic()
    if _snapshot_cache and now - _snapshot_cache_ts < _SNAPSHOT_TTL:
        return _snapshot_cache
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT symbol, trade_date, open, high, low, close, volume
                FROM (
                    SELECT DISTINCT ON (symbol)
                        symbol, trade_date, open, high, low, close, volume
                    FROM {TABLE_NAME}
                    ORDER BY symbol, trade_date DESC
                ) latest
                ORDER BY trade_date DESC
                LIMIT 10
            """)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        conn.close()
        _snapshot_cache = rows
        _snapshot_cache_ts = now
        return rows
    except Exception as e:
        print(f"Snapshot Error: {e}")
        return _snapshot_cache or []


@app.get("/api/signals")
async def get_signals(strategy: str = "golden_rsi", _: dict = Depends(require_user)):
    """Returns swing trading signals using the selected strategy."""
    try:
        manager = StrategyManager()
        signals = await asyncio.to_thread(manager.get_signals, strategy)
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


# -------------------------------------------------------
# Fyers OAuth — browser-based login without terminal
# -------------------------------------------------------

@app.get("/api/fyers/login_url")
async def fyers_login_url(_: str = Depends(require_admin)):
    """
    Generate the Fyers OAuth URL.
    Frontend opens this in a new tab — after login Fyers redirects to
    /auth/fyers/callback?auth_code=xxx which saves the token automatically.
    Admin only.
    """
    try:
        from fyers_apiv3 import fyersModel
        from config.settings import FYERS_CLIENT_ID, FYERS_SECRET_KEY

        # Build callback URL: use APP_URL env var if set (production),
        # otherwise fall back to localhost
        app_url = os.getenv("APP_URL", "http://localhost:8000").rstrip("/")
        redirect_uri = f"{app_url}/auth/fyers/callback"

        session = fyersModel.SessionModel(
            client_id=FYERS_CLIENT_ID,
            secret_key=FYERS_SECRET_KEY,
            redirect_uri=redirect_uri,
            response_type="code",
            grant_type="authorization_code",
        )
        auth_url = session.generate_authcode()
        return {"url": auth_url, "redirect_uri": redirect_uri}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/auth/fyers/callback")
async def fyers_callback(auth_code: str = None, s: str = None, code: int = None):
    """
    Fyers redirects here after the user logs in.
    Automatically exchanges auth_code → access_token and saves token.json.
    Returns an HTML page that closes itself and notifies the dashboard.
    """
    from fastapi.responses import HTMLResponse

    if not auth_code:
        return HTMLResponse("""
        <html><body style="font-family:sans-serif;text-align:center;padding:60px;background:#0a0f1a;color:#fff;">
        <h2 style="color:#f43f5e;">Login Failed</h2>
        <p>No auth_code received from Fyers.</p>
        <script>setTimeout(()=>window.close(),3000);</script>
        </body></html>
        """, status_code=400)

    try:
        from fyers_apiv3 import fyersModel
        from config.settings import FYERS_CLIENT_ID, FYERS_SECRET_KEY
        from datetime import timedelta

        app_url = os.getenv("APP_URL", "http://localhost:8000").rstrip("/")
        redirect_uri = f"{app_url}/auth/fyers/callback"

        session = fyersModel.SessionModel(
            client_id=FYERS_CLIENT_ID,
            secret_key=FYERS_SECRET_KEY,
            redirect_uri=redirect_uri,
            response_type="code",
            grant_type="authorization_code",
        )
        session.set_token(auth_code)
        response = session.generate_token()

        if "access_token" not in response:
            raise ValueError(f"Token exchange failed: {response}")

        access_token = response["access_token"]
        expiration   = datetime.now() + timedelta(hours=24)

        os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
        with open(TOKEN_PATH, "w") as f:
            json.dump({
                "access_token": access_token,
                "expires_at":   expiration.isoformat(),
                "created_at":   datetime.now().isoformat(),
            }, f, indent=2)

        return HTMLResponse(f"""
        <html><body style="font-family:sans-serif;text-align:center;padding:60px;background:#0a0f1a;color:#fff;">
        <h2 style="color:#10b981;">Fyers Connected</h2>
        <p style="color:#94a3b8;">Token saved. Expires at {expiration.strftime('%Y-%m-%d %H:%M')}.</p>
        <p style="color:#94a3b8;">This window will close automatically.</p>
        <script>
          if (window.opener) {{ window.opener.postMessage('fyers_connected', '*'); }}
          setTimeout(() => window.close(), 2000);
        </script>
        </body></html>
        """)

    except Exception as e:
        return HTMLResponse(f"""
        <html><body style="font-family:sans-serif;text-align:center;padding:60px;background:#0a0f1a;color:#fff;">
        <h2 style="color:#f43f5e;">Token Exchange Failed</h2>
        <p style="color:#94a3b8;">{str(e)}</p>
        <script>setTimeout(()=>window.close(),5000);</script>
        </body></html>
        """, status_code=500)


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
async def get_all_signals(_: dict = Depends(require_user)):
    """Run all strategies and return combined results grouped by strategy."""
    try:
        manager = StrategyManager()
        all_signals = manager.get_all_signals()
        return all_signals
    except Exception as e:
        print(f"All Signals Error: {e}")
        return {}


@app.get("/api/combined/signals")
async def combined_signals(
    trend_period:    str   = "any",
    account_capital: float = 100_000.0,
    risk_pct:        float = 1.5,
    size_mode:       str   = "fixed",    # "fixed" | "kelly"
):
    """
    Combined signals: strategy setup + ML probability filter + quality score + trend filter.

    Flow:
      1. Run all 16 strategies → find stocks with a valid entry setup
      2. Score each stock through the ML model
      3. Keep only stocks above regime-adjusted BUY threshold
      4. Compute Trade Quality Score (0-100) for each
      5. Apply optional trend period filter
      6. Sort by quality_score desc

    trend_period values: any | 1m | 3m | 6m | 1y
    """
    global _regime_cache, _regime_cache_ts, _combined_signals_cache, _combined_signals_cache_ts

    # ── Serve from cache if fresh (default params only) ──────────────────
    _now = _time.monotonic()
    if (trend_period == "any" and
        _combined_signals_cache and
        (_now - _combined_signals_cache_ts) < _COMBINED_SIGNALS_TTL):
        return _combined_signals_cache

    import pandas as pd
    from ml.model import THRESHOLDS
    from ml.regime import compute_regime
    from ml.quality_score import compute_quality_score
    from risk.position_sizer import compute_position_size

    PERIOD_DAYS = {"1m": 20, "3m": 63, "6m": 126, "1y": 252}

    # ── Step 1: Market regime (use in-process cache if fresh) ───────────
    try:
        now_mono = _time.monotonic()
        if _regime_cache and (now_mono - _regime_cache_ts) < _REGIME_TTL:
            regime_data = _regime_cache
        else:
            regime_data = await asyncio.to_thread(compute_regime)
            _regime_cache    = regime_data
            _regime_cache_ts = now_mono
        regime = regime_data.get("regime", "Neutral")
    except Exception:
        regime_data = {}
        regime      = "Neutral"

    buy_thresh, avoid_thresh = THRESHOLDS.get(regime, THRESHOLDS["Neutral"])

    # ── Step 2: Run all strategies (off event loop — CPU/IO bound) ──────
    try:
        manager     = StrategyManager()
        all_signals = await asyncio.to_thread(manager.get_all_signals)
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

    # ── Step 2a: Bulk-fetch candles for all signal symbols ──────────────────
    # One DB query instead of N round-trips; used by both liquidity filter and ML.
    candle_map = await asyncio.to_thread(_bulk_fetch_candles, engine, list(symbol_map.keys()))

    # ── Step 2b: Liquidity pre-filter ───────────────────────────────────────
    # Drop illiquid stocks before running the expensive ML model.
    from risk.liquidity_filter import LiquidityFilter
    _lf = LiquidityFilter()   # uses defaults: ADV ≥ ₹5 Cr, spread ≤ 0.5%, promoter ≤ 75%
    _illiquid_skipped = 0
    for sym in list(symbol_map.keys()):
        df_liq = candle_map.get(sym)
        if df_liq is None or df_liq.empty:
            continue
        liq = _lf.check(df_liq, symbol=sym,
                         close_price=float(df_liq["close"].iloc[-1]) if not df_liq.empty else 0.0)
        if not liq["is_liquid"]:
            del symbol_map[sym]
            _illiquid_skipped += 1
            logger.debug(f"Liquidity filter dropped {sym}: {liq['rejection_reason']}")
    if _illiquid_skipped:
        logger.info(f"Liquidity filter removed {_illiquid_skipped} illiquid symbols")

    # ── Step 4: Score each symbol with the ML model ──────────────────────
    period_days = PERIOD_DAYS.get(trend_period)   # None means no filter
    results = []
    for sym, info in symbol_map.items():
        try:
            df = candle_map.get(sym)
            if df is None or df.empty or len(df) < 260:
                continue

            ml = predictor.predict_symbol(df, sym, regime=regime)
            if "error" in ml:
                continue

            prob = ml.get("buy_probability", 0)

            # Only keep if ML agrees with the strategy (above regime BUY threshold)
            if prob < buy_thresh:
                continue

            # ── Trend period filter ─────────────────────────────────────
            trend_return_pct = None
            if period_days and len(df) >= period_days + 1:
                closes = df["close"]
                trend_return_pct = round(
                    (float(closes.iloc[-1]) / float(closes.iloc[-period_days - 1]) - 1) * 100, 2
                )
                # Skip if stock is down over the selected horizon
                if trend_return_pct <= 0:
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

            # ── Compute volume ratio for quality score ──────────────────
            vol_ratio = None
            try:
                if len(df) >= 21:
                    vol_avg = float(df["volume"].iloc[-21:-1].mean())
                    vol_today = float(df["volume"].iloc[-1])
                    vol_ratio = round(vol_today / vol_avg, 2) if vol_avg > 0 else None
            except Exception:
                pass

            # ── Trade Quality Score ─────────────────────────────────────
            trend_3m = None
            if len(df) >= 64:
                closes = df["close"]
                trend_3m = round(
                    (float(closes.iloc[-1]) / float(closes.iloc[-64]) - 1) * 100, 2
                )

            # Liquidity detail for this symbol (already passed pre-filter,
            # so is_liquid=True; we just need the score for quality weighting)
            liq_detail = _lf.check(
                df, symbol=sym,
                close_price=float(df["close"].iloc[-1]),
            )

            qs = compute_quality_score(
                buy_probability=prob,
                regime=regime,
                trend_return_3m=trend_3m,
                vol_ratio=vol_ratio,
                strategy_count=strategy_count,
                liquidity_score=liq_detail["liquidity_score"],
            )

            # ── Step 5: Meta Filter gate ────────────────────────────────
            # Runs AFTER quality score so we only compute it for trades that
            # passed all earlier filters.  If the meta filter is not trained
            # yet, the trade is kept (graceful degradation).
            from ml.meta_filter_model import get_meta_filter
            meta_filter = get_meta_filter()
            meta_result = {}
            if meta_filter.clf is not None:
                try:
                    meta_result = meta_filter.predict_symbol(df, sym)
                    take_prob   = meta_result.get("take_trade_probability")
                    if take_prob is not None and take_prob < 0.50:
                        # Meta filter says AVOID — drop this trade
                        continue
                except Exception as _mfe:
                    logger.debug(f"Meta filter error for {sym}: {_mfe}")

            results.append({
                "symbol":              sym,
                "buy_probability":     prob,
                "quality_score":       qs["score"],
                "confidence":          qs["confidence"],
                "score_breakdown":     qs["breakdown"],
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
                # Trend context
                "trend_period":        trend_period,
                "trend_return_pct":    trend_return_pct,
                "trend_3m":            trend_3m,
                "vol_ratio":           vol_ratio,
                # Multi-horizon probabilities
                "prob_3d":             ml.get("prob_3d"),
                "prob_5d":             ml.get("prob_5d"),
                "prob_10d":            ml.get("prob_10d"),
                "horizon_signals":     ml.get("horizon_signals"),
                # Stability score
                "stability":           ml.get("stability"),
                "stability_detail":    ml.get("stability_detail"),
                # Meta filter
                "meta_filter":         meta_result,
                # Liquidity
                "liquidity":           liq_detail,
                # Position sizing (Step 3)
                "position_size":       compute_position_size(
                    account_capital    = account_capital,
                    entry_price        = float(info["entry"] or ml.get("price") or 0),
                    stop_loss_price    = float(info["stop_loss"] or 0),
                    risk_per_trade_pct = risk_pct,
                    mode               = size_mode,
                    win_rate           = ml.get("win_rate"),
                    avg_rr             = round(
                        ((info.get("target") or 0) - (info.get("entry") or 0)) /
                        max(((info.get("entry") or 0) - (info.get("stop_loss") or 0)), 0.01),
                        2
                    ) if info.get("target") and info.get("entry") and info.get("stop_loss") else None,
                ),
            })

        except Exception as e:
            print(f"Combined signal error for {sym}: {e}")

    # Sort: primary = quality_score, secondary = ML probability
    results.sort(
        key=lambda x: (x["quality_score"], x["buy_probability"]),
        reverse=True,
    )

    # ── Portfolio heat check ─────────────────────────────────────────────
    # Applied after sort so top-ranked signals are given priority.
    # Augments each result with "heat_check"; does NOT remove any result —
    # UI can grey out rejected ones.
    try:
        from risk.portfolio_heat import PortfolioHeatChecker
        heat_checker = PortfolioHeatChecker(account_capital=account_capital)
        # Build price_map from already-fetched candle data (last 60 closes)
        price_map = {
            sym: candle_map[sym]["close"].reset_index(drop=True)
            for sym in [r["symbol"] for r in results]
            if sym in candle_map and not candle_map[sym].empty
        }
        heat_checker.filter_signals(results, price_map=price_map)
    except Exception as _he:
        logger.warning(f"Portfolio heat check failed: {_he}")

    # ── Event risk check ─────────────────────────────────────────────────
    # Adds "event_risk" key to each result: CLEAR / CAUTION / BLOCK.
    # BLOCK trades stay in results; UI can display a warning.
    try:
        from risk.event_risk import get_event_risk_filter
        erf = get_event_risk_filter()
        signal_syms = [r["symbol"] for r in results]
        ev_map = erf.bulk_check(signal_syms)
        for r in results:
            r["event_risk"] = ev_map.get(r["symbol"], {"status": "CLEAR", "events": [], "nearest_event_days": None})
    except Exception as _eve:
        logger.warning(f"Event risk check failed: {_eve}")
        for r in results:
            if "event_risk" not in r:
                r["event_risk"] = {"status": "CLEAR", "events": [], "nearest_event_days": None}

    # ── AI explanations ──────────────────────────────────────────────────
    # Runs after sort so bullets reference the final ranked position context.
    # explain_batch mutates results in-place and returns the same list.
    try:
        from ml.explainer import explain_batch
        explain_batch(results)
    except Exception as _ee:
        logger.warning(f"Explainer failed: {_ee}")

    total_strategy_signals = sum(len(v) for v in all_signals.values())

    # Update the global cache so /api/execute/poll can serve it
    execute_cache.results       = results
    execute_cache.count         = len(results)
    execute_cache.top_symbols   = [r["symbol"] for r in results[:5]]
    execute_cache.top_results   = results[:5]   # full objects for Home tab
    execute_cache.regime        = regime
    execute_cache.buy_threshold = buy_thresh
    execute_cache.last_checked  = datetime.now()

    payload = {
        "results":                results,
        "count":                  len(results),
        "regime":                 regime_data,
        "buy_threshold":          buy_thresh,
        "symbols_with_signals":   len(symbol_map),
        "ml_confirmed":           len(results),
        "total_strategy_signals": total_strategy_signals,
        "sizing": {
            "account_capital": account_capital,
            "risk_pct":        risk_pct,
            "size_mode":       size_mode,
        },
    }
    if trend_period == "any":
        _combined_signals_cache    = payload
        _combined_signals_cache_ts = _time.monotonic()
    return payload


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
        candle_map = _bulk_fetch_candles(engine, symbols=list(symbol_map.keys()))
        results = []
        for sym, info in symbol_map.items():
            try:
                df = candle_map.get(sym)
                if df is None or df.empty or len(df) < 260:
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
        execute_cache.top_results   = results[:5]
        execute_cache.regime        = regime
        execute_cache.buy_threshold = buy_thresh
        execute_cache.last_checked  = datetime.now()

        # Send Telegram alert if new trades appeared
        if len(results) > 0 and (prev_count == 0 or len(results) != prev_count):
            try:
                from alerts.telegram_bot import TelegramAlert
                TelegramAlert().send_new_signals(results, regime)
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
        "top_results":    execute_cache.top_results,
        "regime":         execute_cache.regime,
        "buy_threshold":  execute_cache.buy_threshold,
        "last_checked":   execute_cache.last_checked.isoformat() if execute_cache.last_checked else None,
        "is_refreshing":  execute_cache.is_refreshing,
        "cache_age_secs": int(cache_age_secs) if cache_age_secs is not None else None,
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
async def ml_train_status(_: dict = Depends(require_user)):
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
async def ml_predict_all(_: dict = Depends(require_user)):
    """
    Run multi-horizon ML predictions for all symbols.

    Each result includes:
      prob_3d / prob_5d / prob_10d — calibrated probabilities per horizon
      buy_probability              — alias for prob_5d (backward compat)
      horizon_signals              — per-horizon signal + label
      expected_return_pct          — 5-day regressor output

    Prices are overlaid with live feed LTP where the feed is running.
    Cached for 2 minutes — predictions don't change tick-by-tick.
    """
    global _predict_all_cache, _predict_all_cache_ts
    now = _time.monotonic()
    if _predict_all_cache and now - _predict_all_cache_ts < _PREDICT_ALL_TTL:
        return _predict_all_cache
    try:
        from ml.regime import compute_regime

        predictor = get_ml_predictor()
        if predictor.clf is None:
            return {"error": "Model not trained yet — POST /api/ml/train first", "results": []}

        def _run():
            regime_data = compute_regime()
            regime      = regime_data.get("regime", "Neutral")
            results     = predictor.predict_all(regime=regime)
            return regime_data, regime, results

        regime_data, regime, results = await asyncio.to_thread(_run)
        results = _overlay_live_prices(results)

        live_count = sum(1 for r in results if r.get("price_source") == "live")
        payload = {
            "results":    results,
            "count":      len(results),
            "regime":     regime_data,
            "live_count": live_count,
            "horizons_available": {
                "3d":  predictor.clf_3d  is not None,
                "5d":  predictor.clf_5d  is not None,
                "10d": predictor.clf_10d is not None,
            },
        }
        _predict_all_cache = payload
        _predict_all_cache_ts = now
        return payload
    except Exception as e:
        print(f"ML Predict Error: {e}")
        return {"error": str(e), "results": []}


@app.get("/api/ml/stability")
async def ml_stability_snapshot(_: dict = Depends(require_user)):
    """
    Return the current stability score for every symbol that has been
    predicted at least twice since server start.

    Stability is derived from the variance of the last 5 prob_5d values:
      HIGH       — variance < 0.0004  (std < 2 %)
      MEDIUM     — variance < 0.0025  (std < 5 %)
      LOW        — variance ≥ 0.0025  (std ≥ 5 %)
      INSUFFICIENT — fewer than 2 predictions recorded
    """
    from ml.prediction_history import get_prediction_history
    history = get_prediction_history()
    snap    = history.snapshot()
    return {
        "tracked_symbols": len(history),
        "scored_symbols":  len(snap),
        "stability":       snap,
    }


@app.get("/api/ml/analytics/weekly")
async def ml_weekly_analytics(_: dict = Depends(require_admin)):
    """
    Weekly performance report: win rates, R:R analysis, model accuracy,
    regime breakdown, and retraining recommendation.
    """
    try:
        from ml.trade_analytics import TradeAnalytics
        return TradeAnalytics().weekly_report()
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/ml/analytics/retrain-export")
async def ml_export_retrain_dataset(_: dict = Depends(require_admin)):
    """
    Export the closed-trade feature matrix as CSV for model retraining.
    Returns the file path and row count.
    """
    try:
        from ml.trade_analytics import TradeAnalytics
        ta   = TradeAnalytics()
        path = ta.export_retrain_dataset()
        return {"status": "ok", "csv_path": path}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/ml/strategy-win-rates")
async def strategy_win_rates(_: dict = Depends(require_user)):
    """Return per-strategy win rates for the last 90 days (used by UI badges)."""
    try:
        from ml.trade_analytics import TradeAnalytics
        data = TradeAnalytics().win_rate_by_strategy(days=90)
        # {strategy_id: {win_rate_pct, total_trades}}
        return {
            row["strategy"].strip(): {
                "win_rate_pct":  float(row["win_rate_pct"] or 0),
                "total_trades":  int(row["total_trades"]   or 0),
            }
            for row in data
        }
    except Exception as e:
        return {"error": str(e)}


# ── Trade Logger endpoints ────────────────────────────────────────────────────

class TradeLogRequest(BaseModel):
    symbol:          str
    entry_price:     float
    stop_loss:       float   = 0.0
    target:          float   = 0.0
    quantity:        int     = 0
    strategies:      list    = []
    buy_probability: float   = 0.0
    quality_score:   float   = 0.0
    regime:          str     = ""
    atr_at_entry:    float   = 0.0
    liquidity_score: int     = 0
    event_risk_status: str   = "CLEAR"
    notes:           str     = ""
    trade_type:      str     = "paper"   # "paper" | "live"


@app.post("/api/trades/log")
async def log_trade(req: TradeLogRequest, user: dict = Depends(require_user)):
    """Log a new trade from the UI signal card (one-tap logger)."""
    engine = get_engine()
    try:
        import json as _json
        with engine.begin() as conn:
            conn.execute(
                sqlalchemy.text("""
                    INSERT INTO trade_journal
                        (symbol, entry_price, stop_loss, target, quantity,
                         strategy_tags, buy_probability, quality_score,
                         regime, atr_at_entry, liquidity_score, event_risk_status,
                         notes, trade_type, strategies_fired, status)
                    VALUES
                        (:symbol, :entry, :sl, :target, :qty,
                         :stags, :prob, :qs,
                         :regime, :atr, :liq, :evr,
                         :notes, :ttype, :sfired, 'open')
                """),
                {
                    "symbol":  req.symbol.upper(),
                    "entry":   req.entry_price,
                    "sl":      req.stop_loss,
                    "target":  req.target,
                    "qty":     req.quantity,
                    "stags":   ",".join(req.strategies),
                    "prob":    req.buy_probability,
                    "qs":      req.quality_score,
                    "regime":  req.regime,
                    "atr":     req.atr_at_entry,
                    "liq":     req.liquidity_score,
                    "evr":     req.event_risk_status,
                    "notes":   req.notes,
                    "ttype":   req.trade_type,
                    "sfired":  _json.dumps(req.strategies),
                },
            )
        return {"status": "ok", "message": f"Trade logged: {req.symbol}"}
    except Exception as e:
        return {"error": str(e)}


class CloseTradeRequest(BaseModel):
    trade_id:    int
    exit_price:  float
    exit_reason: str = "manual"


@app.post("/api/trades/close")
async def close_trade(req: CloseTradeRequest, user: dict = Depends(require_user)):
    """Close an open trade, compute P&L and log exit."""
    engine = get_engine()
    try:
        with engine.begin() as conn:
            row = conn.execute(
                sqlalchemy.text(
                    "SELECT entry_price, quantity, stop_loss, entry_date FROM trade_journal WHERE id=:id"
                ),
                {"id": req.trade_id},
            ).fetchone()
            if not row:
                return {"error": "Trade not found"}

            entry_price, qty, sl, entry_date = row
            pnl_inr = (req.exit_price - float(entry_price)) * (qty or 1)
            pnl_pct = round((req.exit_price / float(entry_price) - 1) * 100, 4)
            status  = "win" if pnl_inr >= 0 else "loss"
            from datetime import date as _date
            held = (date.today() - entry_date).days if entry_date else None

            conn.execute(
                sqlalchemy.text("""
                    UPDATE trade_journal
                    SET exit_price  = :ep,
                        exit_date   = CURRENT_DATE,
                        pnl         = :pnl,
                        pnl_pct     = :pnl_pct,
                        status      = :status,
                        exit_reason = :reason,
                        held_days   = :held
                    WHERE id = :id
                """),
                {
                    "ep":      req.exit_price,
                    "pnl":     round(pnl_inr, 2),
                    "pnl_pct": pnl_pct,
                    "status":  status,
                    "reason":  req.exit_reason,
                    "held":    held,
                    "id":      req.trade_id,
                },
            )
        return {
            "status":   "ok",
            "outcome":  status,
            "pnl_inr":  round(pnl_inr, 2),
            "pnl_pct":  pnl_pct,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/ml/stability/{symbol}")
async def ml_stability_symbol(symbol: str, _: dict = Depends(require_user)):
    """Return the stability score for a single symbol."""
    from ml.prediction_history import get_prediction_history
    result = get_prediction_history().stability(symbol.upper())
    result["symbol"] = symbol.upper()
    return result


@app.get("/api/ml/predict/{symbol}")
async def ml_predict_symbol(symbol: str, _: dict = Depends(require_user)):
    """
    Run multi-horizon ML prediction for a single symbol.

    Response shape:
      {
        "symbol":          "RELIANCE",
        "prob_3d":         0.55,      # Short-term (3-day)
        "prob_5d":         0.68,      # Swing      (5-day)  ← primary
        "prob_10d":        0.72,      # Positional (10-day)
        "buy_probability": 0.68,      # = prob_5d  (backward compat)
        "expected_return_pct": 3.2,   # 5-day regressor
        "horizon_signals": {
          "3d":  {"probability": 0.55, "signal": "NEUTRAL", "label": "Short-term", ...},
          "5d":  {"probability": 0.68, "signal": "BUY",     "label": "Swing",      ...},
          "10d": {"probability": 0.72, "signal": "BUY",     "label": "Positional", ...}
        },
        "signal":          "BUY",     # 5d signal
        "confidence":      "High",
        ...
      }
    """
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

        result["regime"] = regime_data
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


_regime_cache: dict = {}
_regime_cache_ts: float = 0.0
_REGIME_TTL = 300.0   # recompute regime at most every 5 minutes

@app.get("/api/ml/regime")
async def ml_regime(_: dict = Depends(require_user)):
    """
    Compute current market regime from DB breadth data.
    Returns: regime (Bull/Neutral/Bear), breadth_pct, avg_atr_pct, volatility_label.
    Cached for 5 minutes — regime doesn't change tick-by-tick.
    """
    global _regime_cache, _regime_cache_ts
    now = _time.monotonic()
    if _regime_cache and now - _regime_cache_ts < _REGIME_TTL:
        return _regime_cache
    try:
        from ml.regime import compute_regime
        _regime_cache = await asyncio.to_thread(compute_regime)
        _regime_cache_ts = now
        return _regime_cache
    except Exception as e:
        print(f"Regime Error: {e}")
        return {"error": str(e)}


@app.get("/api/ml/reliability")
async def ml_reliability(_: dict = Depends(require_user)):
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
# Meta Filter Model endpoints
# -------------------------------------------------------

class _MetaTrainState:
    is_training: bool = False
    last_trained: str = None
    last_result: dict = {}

_meta_train_state = _MetaTrainState()


def _run_meta_filter_training():
    """Background task: train the meta filter model."""
    from ml.meta_filter_model import MetaFilterModel, reload_meta_filter

    _meta_train_state.is_training = True
    steps = []

    def progress_cb(step: str):
        steps.append(step)
        logger.info(f"[MetaFilter train] {step}")

    try:
        model  = MetaFilterModel()
        result = model.train(progress_cb=progress_cb)
        _meta_train_state.last_result  = result
        _meta_train_state.last_trained = datetime.now().isoformat()
        if "error" not in result:
            reload_meta_filter()
    except Exception as e:
        logger.error(f"[MetaFilter train] Error: {e}")
        _meta_train_state.last_result = {"error": str(e)}
    finally:
        _meta_train_state.is_training = False


@app.post("/api/ml/meta/train")
async def meta_filter_train(
    background_tasks: BackgroundTasks,
    _: str = Depends(require_admin),
):
    """
    Train the meta filter model in the background. Admin only.

    The meta filter learns which trade setups actually hit their target
    before their stop loss using a realistic bar-by-bar simulation —
    distinct from the price-direction model's simple 2% forward return.
    """
    if _meta_train_state.is_training:
        return {"message": "Meta filter training already in progress", "status": "busy"}
    background_tasks.add_task(_run_meta_filter_training)
    return {"message": "Meta filter training started", "status": "started"}


@app.get("/api/ml/meta/status")
async def meta_filter_status():
    """Return meta filter training status and model metrics."""
    from ml.meta_filter_model import get_meta_filter
    model = get_meta_filter()
    return {
        "is_training":  _meta_train_state.is_training,
        "last_trained": _meta_train_state.last_trained,
        "is_trained":   model.is_trained(),
        "meta":         model.get_meta(),
        "last_result":  _meta_train_state.last_result,
    }


class MetaFilterPredictRequest(BaseModel):
    symbol: str


@app.post("/api/ml/meta/predict")
async def meta_filter_predict(body: MetaFilterPredictRequest):
    """
    Predict TAKE / AVOID for a single symbol using the meta filter.

    Loads the latest OHLCV bars from the DB, computes features, and
    returns take_trade_probability + decision.
    """
    import pandas as pd
    from ml.meta_filter_model import get_meta_filter

    symbol = body.symbol.strip().upper()
    try:
        engine = get_engine()
        df     = pd.read_sql(
            f"SELECT trade_date, open, high, low, close, volume "
            f"FROM {TABLE_NAME} WHERE symbol = %(sym)s ORDER BY trade_date ASC",
            engine, params={"sym": symbol},
        )
        if df.empty or len(df) < 260:
            return {
                "symbol": symbol,
                "error":  f"Insufficient data ({len(df)} bars, need ≥260)",
            }
        model  = get_meta_filter()
        result = model.predict_symbol(df, symbol)
        return result
    except Exception as e:
        logger.error(f"meta_filter_predict error for {symbol}: {e}")
        return {"symbol": symbol, "error": str(e)}


# -------------------------------------------------------
# Yahoo Finance backfill endpoints
# -------------------------------------------------------

async def _run_yfinance_task(symbols: list = None):
    yf_state.is_running = True
    yf_state.last_run = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    yf_state.processed = 0
    yf_state.total_new_candles = 0
    yf_state.current_symbol = "Starting…"

    # Default = full NSE universe (all active EQ stocks, ~1 800)
    if symbols is None:
        try:
            from config.nse_indices import fetch_nse_all_symbols
            symbols = fetch_nse_all_symbols()
        except Exception:
            symbols = None   # yfinance_fetcher will fall back to SYMBOL_FILE

    def progress_cb(processed, total, symbol, total_new):
        yf_state.processed = processed
        yf_state.total = total
        yf_state.current_symbol = symbol
        yf_state.total_new_candles = total_new

    try:
        from fetcher.yfinance_fetcher import run_yfinance_backfill
        result = run_yfinance_backfill(symbols=symbols, progress_cb=progress_cb, workers=3)
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
    Fetch full 30-year historical data for all NSE-listed stocks from Yahoo Finance.
    Default: entire NSE universe (~1 800 EQ stocks, fetched live from NSE's official CSV).
    Optional ?index=NIFTY+500 to limit to a specific index.
    Runs in background with 8 parallel workers — poll /api/yfinance/status for progress.
    Admin only.
    """
    if yf_state.is_running:
        return {"message": "Yahoo Finance backfill already running", "status": "busy"}

    symbols = None
    index_label = "NSE All (~1 800 stocks)"
    if index:
        from config.nse_indices import get_index_symbols
        symbols = get_index_symbols(index)
        if not symbols:
            raise HTTPException(status_code=400, detail=f"Unknown index: {index}")
        index_label = f"{index} ({len(symbols)} symbols)"

    background_tasks.add_task(_run_yfinance_task, symbols)
    return {"message": f"Yahoo Finance backfill started for {index_label}", "status": "started"}


@app.get("/api/yfinance/status")
async def yfinance_status(_: dict = Depends(require_user)):
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


# ──────────────────────────────────────────────────────────────────────────────
# Stock Metadata endpoints
# ──────────────────────────────────────────────────────────────────────────────

class _MetaState:
    is_running   = False
    phase        = ""
    processed    = 0
    total        = 0
    current_sym  = ""
    last_result  = None

_meta_state = _MetaState()


async def _run_metadata_task(enrich_yf: bool):
    _meta_state.is_running  = True
    _meta_state.phase       = "starting"
    _meta_state.processed   = 0
    _meta_state.total       = 0
    _meta_state.current_sym = ""

    def _cb(phase, done, total, sym):
        _meta_state.phase       = phase
        _meta_state.processed   = done
        _meta_state.total       = total
        _meta_state.current_sym = sym

    try:
        from fetcher.stock_metadata_fetcher import run_metadata_sync
        result = run_metadata_sync(enrich_yf=enrich_yf, workers=4, progress_cb=_cb)
        _meta_state.last_result = result
        _meta_state.current_sym = "Done"
    except Exception as e:
        logger.error(f"Metadata sync error: {e}")
        _meta_state.last_result = {"error": str(e)}
        _meta_state.current_sym = "Error"
    finally:
        _meta_state.is_running = False


@app.post("/api/stock/metadata/sync")
async def metadata_sync(
    background_tasks: BackgroundTasks,
    enrich_yf: bool = True,
    _: dict = Depends(require_admin),
):
    """
    Phase 1 (~5 s): NSE CSV + index membership for all ~2100 EQ stocks.
    Phase 2 (~15 min, only if enrich_yf=true): yfinance sector/industry for
    symbols still missing a sector after Phase 1.
    """
    if _meta_state.is_running:
        return {"status": "busy", "message": "Metadata sync already running"}
    background_tasks.add_task(_run_metadata_task, enrich_yf)
    return {"status": "started", "message": "Metadata sync started in background"}


@app.get("/api/stock/metadata/status")
async def metadata_status(_: dict = Depends(require_user)):
    pct = round(_meta_state.processed / _meta_state.total * 100, 1) if _meta_state.total else 0
    return {
        "is_running":   _meta_state.is_running,
        "phase":        _meta_state.phase,
        "processed":    _meta_state.processed,
        "total":        _meta_state.total,
        "pct":          pct,
        "current_sym":  _meta_state.current_sym,
        "last_result":  _meta_state.last_result,
    }


@app.get("/api/stock/metadata/sectors")
async def metadata_sectors(_: dict = Depends(require_user)):
    """Return list of all distinct sectors in the metadata table."""
    try:
        from fetcher.stock_metadata_fetcher import get_sectors
        return {"sectors": get_sectors()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stock/metadata/indices")
async def metadata_indices(_: dict = Depends(require_user)):
    """Return list of all distinct index names stored in the metadata table."""
    try:
        from fetcher.stock_metadata_fetcher import get_indices_list
        return {"indices": get_indices_list()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stock/metadata/{symbol}")
async def metadata_one(symbol: str, _: dict = Depends(require_user)):
    """Return full metadata record for one symbol."""
    try:
        from fetcher.stock_metadata_fetcher import get_symbol_metadata
        data = get_symbol_metadata(symbol.upper())
        if not data:
            raise HTTPException(status_code=404, detail=f"{symbol} not found in metadata")
        # Convert psycopg2 arrays to plain lists
        if isinstance(data.get("indices"), list):
            data["indices"] = list(data["indices"])
        if data.get("updated_at"):
            data["updated_at"] = str(data["updated_at"])
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stock/search")
async def stock_search(
    q: str = "",
    sector: str = "",
    index: str = "",
    limit: int = 50,
    _: dict = Depends(require_user),
):
    """
    Search/filter stocks from metadata.
    ?q=REL  — symbol/name prefix search
    ?sector=IT  — filter by sector
    ?index=NIFTY+50  — filter by index membership
    """
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            conditions = ["is_active = TRUE"]
            params: list = []
            if q:
                conditions.append("(symbol ILIKE %s OR company_name ILIKE %s)")
                params += [f"{q}%", f"%{q}%"]
            if sector:
                conditions.append("sector = %s")
                params.append(sector)
            if index:
                conditions.append("%s = ANY(indices)")
                params.append(index)
            where = " AND ".join(conditions)
            cur.execute(
                f"""SELECT symbol, company_name, sector, market_cap_cat, indices
                    FROM stock_metadata WHERE {where}
                    ORDER BY symbol LIMIT %s""",
                params + [limit],
            )
            cols = ["symbol", "company_name", "sector", "market_cap_cat", "indices"]
            results = [dict(zip(cols, row)) for row in cur.fetchall()]
            for r in results:
                if isinstance(r.get("indices"), list):
                    r["indices"] = list(r["indices"])
        conn.close()
        return {"results": results, "count": len(results)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/db/sources")
async def db_sources(_: dict = Depends(require_user)):
    """Return row counts grouped by data source. Cached 5 min (full table scan)."""
    global _db_sources_cache, _db_sources_cache_ts
    now = _time.monotonic()
    if _db_sources_cache and now - _db_sources_cache_ts < _DB_SOURCES_TTL:
        return _db_sources_cache
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT source, COUNT(*) as rows, COUNT(DISTINCT symbol) as symbols, "
                f"MIN(trade_date) as from_date, MAX(trade_date) as to_date "
                f"FROM {TABLE_NAME} GROUP BY source ORDER BY rows DESC"
            )
            rows = cur.fetchall()
        conn.close()
        result = [
            {"source": r[0], "rows": r[1], "symbols": r[2],
             "from_date": str(r[3]) if r[3] else None,
             "to_date":   str(r[4]) if r[4] else None}
            for r in rows
        ]
        _db_sources_cache = result
        _db_sources_cache_ts = now
        return result
    except Exception as e:
        return _db_sources_cache or {"error": str(e)}


# -------------------------------------------------------
# Bootstrap endpoints
# -------------------------------------------------------

@app.get("/api/bootstrap/status")
async def bootstrap_status():
    """Return first-run bootstrap status (data fetch + ML training progress)."""
    from scheduler.bootstrap import get_status
    return get_status()


@app.post("/api/bootstrap/trigger")
async def bootstrap_trigger(_: str = Depends(require_admin)):
    """Manually trigger bootstrap even if DB already has data. Admin only."""
    from scheduler.bootstrap import state as bs, _run_bootstrap
    import threading
    if bs.running:
        return {"message": "Bootstrap already running", "status": "busy"}
    bs.done = False
    bs.step = "checking"
    threading.Thread(target=_run_bootstrap, name="manual-bootstrap", daemon=True).start()
    return {"message": "Bootstrap started", "status": "started"}


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


@app.get("/api/symbols/search")
async def symbol_search(q: str = ""):
    """
    Autocomplete endpoint — returns symbols from the DB that start with or
    contain the query string (case-insensitive). Returns up to 10 results.
    """
    q = q.strip().upper()
    if not q:
        return {"symbols": []}
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            # Prefix matches first (e.g. "REL" → RELIANCE, RELINFRA)
            cur.execute(
                f"SELECT DISTINCT symbol FROM {TABLE_NAME} "
                "WHERE UPPER(symbol) LIKE %s ORDER BY symbol LIMIT 10",
                (f"{q}%",),
            )
            symbols = [r[0] for r in cur.fetchall()]
            # If fewer than 5 prefix hits, also search by contains
            if len(symbols) < 5:
                cur.execute(
                    f"SELECT DISTINCT symbol FROM {TABLE_NAME} "
                    "WHERE UPPER(symbol) LIKE %s AND UPPER(symbol) NOT LIKE %s "
                    "ORDER BY symbol LIMIT 5",
                    (f"%{q}%", f"{q}%"),
                )
                symbols += [r[0] for r in cur.fetchall()]
        conn.close()
        return {"symbols": symbols[:10]}
    except Exception as e:
        logger.error(f"symbol_search error: {e}")
        return {"symbols": []}


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
# Intraday Scanner
# -------------------------------------------------------

_intraday_cache      = None
_intraday_cache_ts   = 0.0
_INTRADAY_CACHE_TTL  = 600.0   # 10 minutes

@app.get("/api/intraday/signals")
async def intraday_signals(
    _r:    dict = Depends(require_user),
    index: str  = None,
    limit: int  = 30,
):
    """
    Return today's intraday trade setups (ORB, Gap Play, Momentum, etc.)
    Cached 10 min — re-runs when cache expires or ?refresh=1 passed.
    """
    global _intraday_cache, _intraday_cache_ts
    import time as _t

    now = _t.monotonic()
    if _intraday_cache and (now - _intraday_cache_ts) < _INTRADAY_CACHE_TTL:
        payload = _intraday_cache
        if limit:
            payload = {**payload, "results": payload["results"][:limit]}
        return payload

    try:
        from strategies.intraday_scanner import intraday_scan
        from ml.regime import compute_regime

        symbols = None
        if index:
            from config.nse_indices import get_index_symbols
            symbols = get_index_symbols(index) or None

        # Use cached predict_all if available, otherwise compute off the event loop
        ml_probs: dict = {}
        regime = "Neutral"
        try:
            predictor = get_ml_predictor()
            if predictor.clf is not None:
                if _predict_all_cache and (_time.monotonic() - _predict_all_cache_ts) < _PREDICT_ALL_TTL:
                    cached_results = _predict_all_cache.get("results", [])
                    regime = _predict_all_cache.get("regime", {}).get("regime", "Neutral")
                else:
                    regime_data = await asyncio.to_thread(compute_regime)
                    regime = regime_data.get("regime", "Neutral")
                    cached_results = await asyncio.to_thread(predictor.predict_all, regime)
                for r in cached_results:
                    sym = r.get("symbol", "")
                    if sym:
                        ml_probs[sym] = r.get("buy_probability") or r.get("prob_5d", 0)
        except Exception as e:
            logger.warning(f"Intraday: ML probs unavailable: {e}")

        results = await asyncio.to_thread(intraday_scan, symbols, ml_probs)

        # Summary counts
        by_type = {}
        for r in results:
            st = r.get("setup_type", "OTHER")
            by_type[st] = by_type.get(st, 0) + 1

        payload = {
            "results":      results,
            "total":        len(results),
            "regime":       regime,
            "by_setup":     by_type,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "index":        index or "All",
        }
        _intraday_cache    = payload
        _intraday_cache_ts = now
        if limit:
            payload = {**payload, "results": payload["results"][:limit]}
        return payload
    except Exception as e:
        logger.error(f"Intraday signals error: {e}", exc_info=True)
        return {"error": str(e), "results": []}


@app.post("/api/intraday/refresh")
async def intraday_refresh(_r: dict = Depends(require_user)):
    """Invalidate intraday cache so next GET fetches fresh results."""
    global _intraday_cache, _intraday_cache_ts
    _intraday_cache    = None
    _intraday_cache_ts = 0.0
    return {"status": "cache_cleared"}


# -------------------------------------------------------
# Positional Scanner
# -------------------------------------------------------

_positional_cache: dict | None = None
_positional_cache_ts: float    = 0.0
_POSITIONAL_CACHE_TTL          = 900.0   # 15 minutes


@app.get("/api/positional/signals")
async def positional_signals(_r: dict = Depends(require_user)):
    """
    Positional / Short-Term (1 month+) trade setups.
    Detectors: consolidation breakout, quality dip (fundamental+technical),
    sector rotation leaders.  Cached 15 min.
    """
    global _positional_cache, _positional_cache_ts
    import time as _t

    now = _t.monotonic()
    if _positional_cache and (now - _positional_cache_ts) < _POSITIONAL_CACHE_TTL:
        return _positional_cache

    try:
        from strategies.positional_scanner import positional_scan
        from ml.regime import compute_regime

        regime_data = {}
        try:
            global _regime_cache, _regime_cache_ts
            if _regime_cache and (_t.monotonic() - _regime_cache_ts) < 1800:
                regime_data = _regime_cache
            else:
                regime_data = await asyncio.to_thread(compute_regime)
        except Exception:
            pass

        data = await asyncio.to_thread(positional_scan)
        data["regime"]       = regime_data.get("regime", "Neutral")
        data["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        _positional_cache    = _sanitize(data)
        _positional_cache_ts = now
        return _positional_cache
    except Exception as e:
        logger.error(f"Positional signals error: {e}", exc_info=True)
        return {"error": str(e), "results": [], "total": 0}


@app.post("/api/positional/refresh")
async def positional_refresh(_r: dict = Depends(require_user)):
    """Invalidate positional cache."""
    global _positional_cache, _positional_cache_ts
    _positional_cache    = None
    _positional_cache_ts = 0.0
    return {"status": "cache_cleared"}


# -------------------------------------------------------
# Swing Scanner (1 week – few weeks)
# -------------------------------------------------------

_swing_cache: dict | None = None
_swing_cache_ts: float    = 0.0
_SWING_CACHE_TTL          = 900.0   # 15 minutes


@app.get("/api/swing/signals")
async def swing_signals(_r: dict = Depends(require_user)):
    """
    Swing trade setups (1 week – few weeks).
    Detectors: MA crossover (20/50 EMA), trend following (HH-HL), S/R bounce.
    Cached 15 min.
    """
    global _swing_cache, _swing_cache_ts
    import time as _t

    now = _t.monotonic()
    if _swing_cache and (now - _swing_cache_ts) < _SWING_CACHE_TTL:
        return _swing_cache

    try:
        from strategies.swing_positional_scanner import swing_scan
        from ml.regime import compute_regime

        regime_data = {}
        try:
            if _regime_cache and (_t.monotonic() - _regime_cache_ts) < 1800:
                regime_data = _regime_cache
            else:
                regime_data = await asyncio.to_thread(compute_regime)
        except Exception:
            pass

        data = await asyncio.to_thread(swing_scan)
        data["regime"]       = regime_data.get("regime", "Neutral")
        data["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        _swing_cache    = _sanitize(data)
        _swing_cache_ts = now
        return _swing_cache
    except Exception as e:
        logger.error(f"Swing signals error: {e}", exc_info=True)
        return {"error": str(e), "results": [], "total": 0}


@app.post("/api/swing/refresh")
async def swing_refresh(_r: dict = Depends(require_user)):
    """Invalidate swing cache."""
    global _swing_cache, _swing_cache_ts
    _swing_cache    = None
    _swing_cache_ts = 0.0
    return {"status": "cache_cleared"}


# -------------------------------------------------------

@app.get("/api/screener/rs")
async def screener_rs(_r: dict = Depends(require_user), index: str = None):
    """Relative Strength scan — stocks ranked by RS vs universe.
    Optional ?index=NIFTY+50 to scope to a specific index.
    """
    from strategies.screener import relative_strength_scan
    symbols = None
    if index:
        from config.nse_indices import get_index_symbols
        symbols = get_index_symbols(index) or None
    results = await asyncio.to_thread(relative_strength_scan, symbols)
    return {"results": results, "index": index or "NIFTY 100"}


@app.get("/api/screener/52w")
async def screener_52w(_r: dict = Depends(require_user), index: str = None):
    """52-week high scan — stocks near breakout levels."""
    from strategies.screener import fiftytwo_week_scan
    symbols = None
    if index:
        from config.nse_indices import get_index_symbols
        symbols = get_index_symbols(index) or None
    results = await asyncio.to_thread(fiftytwo_week_scan, symbols)
    return {"results": results, "index": index or "NIFTY 100"}


@app.get("/api/screener/volume")
@app.get("/api/screener/volume-surge")   # alias for backwards compatibility
async def screener_volume(_r: dict = Depends(require_user), min_multiplier: float = 2.0, index: str = None):
    """Volume spike scan — unusual volume vs 20-day average."""
    from strategies.screener import volume_spike_scan
    symbols = None
    if index:
        from config.nse_indices import get_index_symbols
        symbols = get_index_symbols(index) or None
    results = await asyncio.to_thread(volume_spike_scan, min_multiplier, symbols)
    return {"results": results, "index": index or "NIFTY 100"}


@app.get("/api/sector/heatmap")
async def sector_heatmap(_: dict = Depends(require_user)):
    """Sector performance heatmap — 1D/1W/1M/3M returns by sector."""
    from strategies.screener import sector_heatmap as _heatmap
    return {"sectors": await asyncio.to_thread(_heatmap)}


@app.get("/api/earnings")
async def earnings_calendar(days_ahead: int = 21, index: str = None):
    """Upcoming earnings dates (next N days). Optional ?index= to scope."""
    from strategies.screener import earnings_calendar as _cal
    symbols = None
    if index:
        from config.nse_indices import get_index_symbols
        symbols = get_index_symbols(index) or None
    results = await asyncio.to_thread(_cal, days_ahead, symbols)
    return {"results": results, "days_ahead": days_ahead, "index": index or "NIFTY 100"}


# -------------------------------------------------------
# Undervalued Stock Screener
# -------------------------------------------------------

def _run_value_scan(symbols: list):
    """
    Background task: run the fundamental + technical value scan.
    Must be a plain (non-async) function so FastAPI/Starlette runs it in a
    thread pool — the scan blocks for ~2 minutes and must not freeze the
    event loop (which would break status polling).
    """
    value_scan_state.is_running = True
    value_scan_state.error = None
    value_scan_state.processed = 0
    value_scan_state.total = len(symbols)
    value_scan_state.started_at = datetime.now().isoformat()

    def _progress(processed: int, total: int, symbol: str):
        value_scan_state.processed = processed
        value_scan_state.total = total
        value_scan_state.current_symbol = symbol

    try:
        from strategies.value_screener import undervalued_scan
        undervalued_scan(symbols=symbols, force_refresh=True, progress_cb=_progress)
    except Exception as e:
        logger.error(f"Value scan error: {e}")
        value_scan_state.error = str(e)
    finally:
        value_scan_state.is_running = False
        value_scan_state.current_symbol = "Done"


@app.get("/api/screener/undervalued")
async def screener_undervalued(
    _r: dict = Depends(require_user),
    index: str = "NIFTY 500",
    min_score: float = 0,
    sector: str = None,
):
    """
    Return undervalued stocks sorted by value score (highest first).

    Serves from 24-hour cache. Trigger a refresh via POST /api/screener/undervalued/refresh.
    Each result includes a `buy_signal` dict with verdict, conviction score, and gate details.

    Query params:
      index      — index name to scope (default: NIFTY 500)
      min_score  — filter out stocks below this score (default: 0)
      sector     — filter by sector name (optional)
    """
    from strategies.value_screener import _load_cache, get_cache_info, compute_buy_signal

    cached = _load_cache()
    if cached is None:
        return {
            "results":    [],
            "count":      0,
            "cached_at":  None,
            "cache_info": get_cache_info(),
            "message":    "No scan results yet. Trigger a scan via POST /api/screener/undervalued/refresh",
        }

    # Shallow-copy each result dict so we can attach buy_signal without mutating
    # the cached objects (cache is shared across requests; stale signals would
    # persist across regime changes if we mutated in-place).
    results = [{**r} for r in cached.get("results", [])]

    # Filter by index scope (cache stores full NIFTY_500; slice here)
    if index and index != "NIFTY 500":
        from config.nse_indices import get_index_symbols
        scoped = set(get_index_symbols(index) or [])
        if scoped:
            results = [r for r in results if r["symbol"] in scoped]

    # Apply filters
    if min_score > 0:
        results = [r for r in results if r.get("value_score", 0) >= min_score]
    if sector:
        results = [r for r in results if (r.get("sector") or "").lower() == sector.lower()]

    # ── Buy Signal: regime + ML ──────────────────────────────────────────────
    # Run all blocking I/O (DB + ML) in the thread-pool so we don't hold up
    # the uvicorn event loop for other requests.
    import asyncio as _asyncio

    def _compute_signals_sync(results_snap):
        """Blocking work: regime lookup, ML predictions, signal computation."""
        global _regime_cache, _regime_cache_ts

        # Step 1: get current regime (use cached value; 5-min TTL)
        regime = "Neutral"
        try:
            now = _time.monotonic()
            if _regime_cache and now - _regime_cache_ts < _REGIME_TTL:
                regime = _regime_cache.get("regime", "Neutral")
            else:
                from ml.regime import compute_regime
                _regime_cache = compute_regime()
                _regime_cache_ts = now
                regime = _regime_cache.get("regime", "Neutral")
        except Exception:
            pass

        # Step 2: ML probabilities for top-50 stocks
        ml_prob_map: dict = {}
        try:
            predictor = get_ml_predictor()
            if predictor is not None and getattr(predictor, "clf", None) is not None:
                top_syms = [r.get("symbol", "") for r in results_snap[:50] if r.get("symbol")]
                engine = get_engine()
                candle_map = _bulk_fetch_candles(engine, symbols=top_syms)
                for sym in top_syms:
                    try:
                        df = candle_map.get(sym)
                        if df is None or df.empty or len(df) < 60:
                            continue
                        ml = predictor.predict_symbol(df, sym, regime=regime)
                        if "error" not in ml:
                            ml_prob_map[sym] = ml.get("buy_probability") or ml.get("prob_5d")
                    except Exception:
                        pass
        except Exception:
            pass

        return regime, ml_prob_map

    regime, ml_prob_map = await _asyncio.get_event_loop().run_in_executor(
        None, _compute_signals_sync, results
    )

    # Step 3: attach buy_signal to every result
    for stock in results:
        sym = stock.get("symbol", "")
        ml_prob = ml_prob_map.get(sym)
        try:
            stock["buy_signal"] = compute_buy_signal(stock, regime=regime, ml_prob=ml_prob)
        except Exception:
            stock["buy_signal"] = None

    return {
        "results":           results,
        "count":             len(results),
        "cached_at":         cached.get("cached_at"),
        "scan_duration_s":   cached.get("scan_duration_s"),
        "sector_pe_medians": cached.get("sector_pe_medians", {}),
        "cache_info":        get_cache_info(),
        "regime":            regime,
    }


@app.post("/api/screener/undervalued/refresh")
async def screener_undervalued_refresh(
    background_tasks: BackgroundTasks,
    _r: dict = Depends(require_user),
    index: str = "NIFTY 500",
):
    """
    Trigger a background re-scan of NIFTY 500 (or a scoped index).
    Scans ~500 stocks via yfinance — takes 2-4 minutes.
    Poll /api/screener/undervalued/status for progress.
    """
    if value_scan_state.is_running:
        return {
            "status":    "busy",
            "message":   "Scan already running",
            "processed": value_scan_state.processed,
            "total":     value_scan_state.total,
        }

    from config.nse_indices import get_index_symbols, NIFTY_500
    symbols = get_index_symbols(index) if index != "NIFTY 500" else NIFTY_500
    if not symbols:
        symbols = NIFTY_500

    background_tasks.add_task(_run_value_scan, symbols)
    return {
        "status":  "started",
        "message": f"Scanning {len(symbols)} symbols for {index}",
        "total":   len(symbols),
    }


@app.get("/api/screener/undervalued/status")
async def screener_undervalued_status(_r: dict = Depends(require_user)):
    """Return current scan progress and cache metadata."""
    from strategies.value_screener import get_cache_info
    return {
        "is_running":      value_scan_state.is_running,
        "processed":       value_scan_state.processed,
        "total":           value_scan_state.total,
        "current_symbol":  value_scan_state.current_symbol,
        "started_at":      value_scan_state.started_at,
        "error":           value_scan_state.error,
        "cache":           get_cache_info(),
    }


# -------------------------------------------------------
# AI Analyst — undervalued stock analysis
# -------------------------------------------------------

from fastapi.responses import StreamingResponse as _StreamingResponse


class AIAnalystRequest(BaseModel):
    question: str = ""
    top_n:    int = 20
    min_score: int = 45


@app.post("/api/screener/undervalued/ai-analysis")
async def screener_ai_analysis(
    body: AIAnalystRequest,
    _r: dict = Depends(require_user),
):
    """
    Stream Claude's analysis of the current top undervalued stocks.

    Body:
      question  (str)  – optional natural language question
      top_n     (int)  – how many top stocks to pass to Claude (default 20)
      min_score (int)  – minimum value_score filter before passing to Claude

    Returns a text/event-stream of markdown-formatted analysis chunks.
    """
    from strategies.value_screener import _load_cache, CACHE_FILE
    from ml.ai_analyst import stream_ai_analysis

    # Try fresh cache first; fall back to any existing cache file regardless of TTL
    cache = _load_cache()
    if cache is None and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as _f:
                cache = json.load(_f)
        except Exception:
            cache = {}
    cache  = cache or {}
    stocks = cache.get("results", [])

    # Apply min_score filter
    if body.min_score > 0:
        stocks = [s for s in stocks if s.get("value_score", 0) >= body.min_score]

    def generate():
        for chunk in stream_ai_analysis(
            stocks=stocks,
            user_question=body.question,
            top_n=body.top_n,
        ):
            # SSE format: data: <chunk>\n\n
            # Escape newlines so each SSE message is a single line
            escaped = chunk.replace("\n", "\\n")
            yield f"data: {escaped}\n\n"
        yield "data: [DONE]\n\n"

    return _StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# -------------------------------------------------------
# AI Chat Endpoint
# -------------------------------------------------------

class ChatRequest(BaseModel):
    message:  str
    history:  list = []   # [{role: "user"|"assistant", content: str}]


@app.post("/api/chat")
async def chat_stream(
    body: ChatRequest,
    _r: dict = Depends(require_user),
):
    """
    Stream Claude's response for the AI chatbot.

    Body:
      message  (str)  — the user's latest message
      history  (list) — prior conversation turns [{role, content}]

    Returns text/event-stream.
    Each SSE event is one of:
      data: <text chunk>
      data: __TOOL_CALL__:<tool_name>
      data: [DONE]
    """
    from ml.chat_agent import stream_chat
    from fastapi.responses import StreamingResponse as _StreamingResponse

    def generate():
        try:
            for chunk in stream_chat(
                messages=body.history,
                user_message=body.message,
            ):
                escaped = chunk.replace("\n", "\\n")
                yield f"data: {escaped}\n\n"
        except Exception as e:
            yield f"data: **Error:** {str(e).replace(chr(10), ' ')}\\n\\n\n\n"
        yield "data: [DONE]\n\n"

    return _StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# -------------------------------------------------------
# Portfolio Builder
# -------------------------------------------------------

class PortfolioRequest(BaseModel):
    capital:             float = 100000
    risk_pct:            float = 1.5
    max_positions:       int   = 8
    max_sector_exposure: int   = 2
    max_position_pct:    float = 20.0
    trend_period:        str   = "any"


@app.post("/api/portfolio/build")
async def portfolio_build(
    body: PortfolioRequest,
    _: dict = Depends(require_user),
):
    """
    Build a capital-allocated portfolio from today's best trade signals.
    Runs the full execute pipeline, then applies position sizing + sector rules.
    """
    import pandas as pd
    from ml.model import THRESHOLDS
    from ml.regime import compute_regime
    from ml.quality_score import compute_quality_score
    from risk.portfolio_builder import build_portfolio

    PERIOD_DAYS = {"1m": 20, "3m": 63, "6m": 126, "1y": 252}

    try:
        regime_data = compute_regime()
        regime      = regime_data.get("regime", "Neutral")
    except Exception:
        regime_data = {}
        regime      = "Neutral"

    buy_thresh, _ = THRESHOLDS.get(regime, THRESHOLDS["Neutral"])

    try:
        manager     = StrategyManager()
        all_signals = manager.get_all_signals()
    except Exception as e:
        return {"error": f"Strategy scan failed: {e}"}

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
        return {"error": "ML model not trained — POST /api/ml/train first"}

    engine = get_engine()
    candle_map = _bulk_fetch_candles(engine, symbols=list(symbol_map.keys()))
    period_days = PERIOD_DAYS.get(body.trend_period)
    candidates = []

    for sym, info in symbol_map.items():
        try:
            df = candle_map.get(sym)
            if df is None or df.empty or len(df) < 260:
                continue

            ml = predictor.predict_symbol(df, sym, regime=regime)
            if "error" in ml:
                continue

            prob = ml.get("buy_probability", 0)
            if prob < buy_thresh:
                continue

            # Trend filter
            trend_return_pct = None
            if period_days and len(df) >= period_days + 1:
                closes = df["close"]
                trend_return_pct = round(
                    (float(closes.iloc[-1]) / float(closes.iloc[-period_days - 1]) - 1) * 100, 2
                )
                if trend_return_pct <= 0:
                    continue

            # Quality score
            vol_ratio = None
            if len(df) >= 21:
                vol_avg = float(df["volume"].iloc[-21:-1].mean())
                vol_today = float(df["volume"].iloc[-1])
                vol_ratio = round(vol_today / vol_avg, 2) if vol_avg > 0 else None

            trend_3m = None
            if len(df) >= 64:
                closes = df["close"]
                trend_3m = round((float(closes.iloc[-1]) / float(closes.iloc[-64]) - 1) * 100, 2)

            qs = compute_quality_score(
                buy_probability=prob, regime=regime,
                trend_return_3m=trend_3m, vol_ratio=vol_ratio,
                strategy_count=len(info["strategies"]),
            )

            candidates.append({
                "symbol":          sym,
                "entry":           info["entry"] or ml.get("price"),
                "stop_loss":       info["stop_loss"],
                "target":          info["target"] or ml.get("price_target"),
                "atr":             info["atr"],
                "buy_probability": prob,
                "quality_score":   qs["score"],
                "confidence":      qs["confidence"],
                "strategies":      info["strategies"],
                "strategy_count":  len(info["strategies"]),
            })
        except Exception:
            continue

    candidates.sort(key=lambda x: x["quality_score"], reverse=True)

    result = build_portfolio(
        capital=body.capital,
        trades=candidates,
        risk_pct=body.risk_pct,
        max_positions=body.max_positions,
        max_sector_exposure=body.max_sector_exposure,
        max_position_pct=body.max_position_pct,
    )
    result["regime"] = regime_data
    result["candidates_count"] = len(candidates)
    return result


# -------------------------------------------------------
# Backtesting
# -------------------------------------------------------

@app.get("/api/backtest")
async def run_backtest(
    strategy: str = "golden_rsi",
    symbol:   str = None,
    start:    str = None,
    end:      str = None,
    capital:  float = 100000,
    risk_pct: float = 1.5,
    _: dict = Depends(require_user),
):
    """
    Run a backtest for a strategy.
    symbol: optional — omit to backtest all Nifty symbols.
    start/end: ISO date strings (optional).
    Returns: trades list, metrics, equity curve.
    """
    try:
        from backtesting.engine import BacktestEngine
        from risk.manager import RiskManager

        rm = RiskManager(capital=capital, risk_pct=risk_pct)
        engine_bt = BacktestEngine(risk_manager=rm)
        result = engine_bt.run(
            strategy_id=strategy,
            symbol=symbol.upper() if symbol else None,
            start_date=start,
            end_date=end,
        )
        return result
    except Exception as e:
        return {"error": str(e)}


# -------------------------------------------------------
# Price Action
# -------------------------------------------------------

@app.get("/api/price-action")
async def price_action(
    symbol: str,
    days:   int = 30,
    _: dict = Depends(require_user),
):
    """
    Return OHLCV candles with derived price-action metrics for a symbol.

    Derived columns per bar:
      change_pct      — % change vs previous close
      range_pct       — (high-low)/close × 100  (intraday volatility)
      body_pct        — (close-open)/open × 100  (candle body direction & size)
      upper_wick_pct  — upper wick as % of total range
      lower_wick_pct  — lower wick as % of total range
      gap_pct         — (open - prev_close)/prev_close × 100
      vol_ratio       — volume / 20-day avg volume
      pattern         — simple candlestick pattern label
    Plus summary stats: trend, avg_range, avg_volume, 52w_high/low.
    """
    symbol = symbol.strip().upper()
    days   = max(5, min(days, 365 * 5))   # clamp 5 – 1825 days

    try:
        engine = get_engine()

        # Fetch candles with a 30-day extra buffer for vol_ratio MA
        df = pd.read_sql(
            f"""
            SELECT trade_date, open, high, low, close, volume
            FROM {TABLE_NAME}
            WHERE symbol = %(sym)s
            ORDER BY trade_date ASC
            """,
            engine,
            params={"sym": symbol},
        )

        if df.empty:
            return {"error": f"No data found for {symbol}"}

        df["trade_date"] = pd.to_datetime(df["trade_date"])

        # ── Derived columns ────────────────────────────────────────────
        df["prev_close"]     = df["close"].shift(1)
        df["change_pct"]     = ((df["close"] - df["prev_close"]) / df["prev_close"] * 100).round(2)
        df["gap_pct"]        = ((df["open"]  - df["prev_close"]) / df["prev_close"] * 100).round(2)
        df["range_pct"]      = ((df["high"]  - df["low"])        / df["close"]       * 100).round(2)
        df["body_pct"]       = ((df["close"] - df["open"])       / df["open"]        * 100).round(2)

        total_range = (df["high"] - df["low"]).replace(0, float("nan"))
        body_high   = df[["open", "close"]].max(axis=1)
        body_low    = df[["open", "close"]].min(axis=1)
        df["upper_wick_pct"] = ((df["high"] - body_high) / total_range * 100).round(2)
        df["lower_wick_pct"] = ((body_low  - df["low"])  / total_range * 100).round(2)

        # 20-day rolling average volume for vol_ratio
        df["vol_ma20"]   = df["volume"].rolling(20).mean()
        df["vol_ratio"]  = (df["volume"] / df["vol_ma20"]).round(2)

        # Simple pattern detection
        def _pattern(row):
            if pd.isna(row["prev_close"]):
                return None
            body   = abs(row["body_pct"])
            upper  = row["upper_wick_pct"] or 0
            lower  = row["lower_wick_pct"] or 0
            is_bull = row["close"] >= row["open"]

            if body < 0.1:
                return "Doji"
            if lower >= 60 and body < 30 and is_bull:
                return "Hammer"
            if lower >= 60 and body < 30 and not is_bull:
                return "Hanging Man"
            if upper >= 60 and body < 30 and is_bull:
                return "Inverted Hammer"
            if upper >= 60 and body < 30 and not is_bull:
                return "Shooting Star"
            if body >= 60 and is_bull:
                return "Bullish Marubozu" if upper < 5 and lower < 5 else "Bullish"
            if body >= 60 and not is_bull:
                return "Bearish Marubozu" if upper < 5 and lower < 5 else "Bearish"
            return "Bullish" if is_bull else "Bearish"

        df["pattern"] = df.apply(_pattern, axis=1)

        # Replace Infinity values produced by division (e.g. prev_close=0)
        # Must be done before summary stats so mean/float calls don't produce inf
        df = df.replace([np.inf, -np.inf], np.nan)

        # ── Trim to requested window ───────────────────────────────────
        df = df.tail(days).copy()

        # ── Summary stats ──────────────────────────────────────────────
        last  = df.iloc[-1]
        high_52w = float(df["high"].tail(252).max())
        low_52w  = float(df["low"].tail(252).min())
        close_now = float(last["close"])

        # Simple trend: compare latest close to 20-bar SMA
        sma20 = float(df["close"].tail(20).mean())
        sma50 = float(df["close"].tail(50).mean()) if len(df) >= 50 else None

        # Consecutive green/red streak
        streak = 0
        streak_dir = "green" if df.iloc[-1]["body_pct"] >= 0 else "red"
        for _, row in df.iloc[::-1].iterrows():
            if (row["body_pct"] >= 0) == (streak_dir == "green"):
                streak += 1
            else:
                break

        summary = {
            "symbol":           symbol,
            "last_close":       close_now,
            "change_pct":       float(last["change_pct"]) if not pd.isna(last["change_pct"]) else None,
            "high_52w":         round(high_52w, 2),
            "low_52w":          round(low_52w, 2),
            "pct_from_52w_high": round((close_now - high_52w) / high_52w * 100, 2),
            "pct_from_52w_low":  round((close_now - low_52w)  / low_52w  * 100, 2),
            "sma20":            round(sma20, 2),
            "sma50":            round(sma50, 2) if sma50 else None,
            "trend":            "Above SMA20" if close_now > sma20 else "Below SMA20",
            "avg_range_pct":    round(float(df["range_pct"].mean()), 2),
            "avg_vol_ratio":    round(float(df["vol_ratio"].dropna().mean()), 2),
            "streak":           streak,
            "streak_dir":       streak_dir,
            "total_candles":    len(df),
            "bull_candles":     int((df["body_pct"] >= 0).sum()),
            "bear_candles":     int((df["body_pct"] < 0).sum()),
        }

        # ── Serialise candles ──────────────────────────────────────────
        df["trade_date"] = df["trade_date"].dt.strftime("%Y-%m-%d")
        df = df.drop(columns=["prev_close", "vol_ma20"])
        # Replace NaN AND Infinity (both not JSON-compliant)
        df = df.replace([np.inf, -np.inf], np.nan)
        df = df.where(pd.notna(df), None)

        candles = df.iloc[::-1].to_dict(orient="records")   # newest first

        return _sanitize({"summary": summary, "candles": candles})

    except Exception as e:
        logger.error(f"price_action error for {symbol}: {e}")
        return {"error": str(e)}


# -------------------------------------------------------
# Backtest — Run History
# -------------------------------------------------------

@app.get("/api/backtest/history")
async def backtest_history(
    strategy: str = None,
    symbol:   str = None,
    limit:    int = 20,
    _: dict = Depends(require_user),
):
    """
    Return recent backtest runs, newest first.
    Optionally filter by strategy or symbol.
    """
    try:
        engine = get_engine()
        conditions = []
        params: dict = {"limit": min(limit, 100)}

        if strategy:
            conditions.append("strategy = %(strategy)s")
            params["strategy"] = strategy
        if symbol:
            conditions.append("symbol = %(symbol)s")
            params["symbol"] = symbol.upper()

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = pd.read_sql(
            f"""
            SELECT id, strategy, symbol, period_label,
                   start_date::text, end_date::text,
                   capital, risk_pct,
                   total_trades, win_count, loss_count, win_rate,
                   total_pnl, total_pnl_pct,
                   max_drawdown, max_drawdown_pct,
                   sharpe_ratio, profit_factor,
                   avg_hold_days, best_trade, worst_trade,
                   run_duration_ms,
                   created_at::text
            FROM backtest_runs
            {where}
            ORDER BY created_at DESC
            LIMIT %(limit)s
            """,
            engine,
            params=params,
        )
        return {"runs": rows.to_dict(orient="records")}
    except Exception as e:
        return {"error": str(e), "runs": []}


@app.get("/api/backtest/history/{run_id}/trades")
async def backtest_run_trades(
    run_id: int,
    _: dict = Depends(require_user),
):
    """Return all trades for a specific backtest run."""
    try:
        engine = get_engine()
        trades = pd.read_sql(
            """
            SELECT symbol, entry_date::text, exit_date::text,
                   entry_price, exit_price, stop_loss, target,
                   shares, pnl, pnl_pct, exit_reason, hold_days
            FROM backtest_trades
            WHERE run_id = %(run_id)s
            ORDER BY entry_date ASC
            """,
            engine,
            params={"run_id": run_id},
        )
        run = pd.read_sql(
            "SELECT strategy, symbol, period_label, start_date::text, end_date::text FROM backtest_runs WHERE id = %(id)s",
            engine,
            params={"id": run_id},
        )
        return {
            "run_id": run_id,
            "meta": run.iloc[0].to_dict() if not run.empty else {},
            "trades": trades.to_dict(orient="records"),
        }
    except Exception as e:
        return {"error": str(e)}


# -------------------------------------------------------
# Admin — User Management
# -------------------------------------------------------

class CreateUserRequest(BaseModel):
    username:   str
    email:      str
    password:   str
    mobile:     Optional[str] = None
    plan_type:  str = "1m"    # 1m | 3m | 6m | 12m
    role:       str = "user"


class UpdateUserRequest(BaseModel):
    plan_type:  Optional[str] = None
    is_active:  Optional[bool] = None
    password:   Optional[str] = None


PLAN_DAYS = {"1m": 30, "3m": 90, "6m": 180, "12m": 365}


@app.post("/api/admin/users")
async def admin_create_user(
    body: CreateUserRequest,
    _: str = Depends(require_admin),
):
    """Create a new user with a subscription plan. Admin only."""
    import asyncio
    from datetime import date, timedelta
    from dashboard.auth import hash_password as _hash
    from utils.email_utils import send_welcome_email

    days   = PLAN_DAYS.get(body.plan_type, 30)
    today  = date.today()
    expiry = today + timedelta(days=days)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (username, email, password_hash, role, plan_type, plan_start, plan_expiry, mobile)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """, (
                body.username, body.email, _hash(body.password),
                body.role, body.plan_type, today, expiry, body.mobile,
            ))
            user_id = cur.fetchone()[0]
        conn.commit()
    except Exception as e:
        conn.rollback()
        if "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="Username or email already exists")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

    # Send welcome email in background (don't block the response)
    asyncio.get_event_loop().run_in_executor(None, send_welcome_email,
        body.email, body.username, body.password,
        body.plan_type, str(expiry), body.mobile,
    )

    return {"id": user_id, "username": body.username,
            "plan_expiry": str(expiry), "message": "User created"}


@app.get("/api/admin/users")
async def admin_list_users(_: str = Depends(require_admin)):
    """List all users with subscription status. Admin only."""
    from datetime import date
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, username, email, mobile, role, plan_type,
                       plan_start, plan_expiry, is_active, created_at, last_login
                FROM users ORDER BY created_at DESC
            """)
            cols = [d[0] for d in cur.description]
            users = []
            today = date.today()
            for row in cur.fetchall():
                u = dict(zip(cols, row))
                for k in ("plan_start", "plan_expiry", "created_at", "last_login"):
                    if u.get(k):
                        u[k] = str(u[k])
                expiry = u.get("plan_expiry")
                if expiry:
                    exp_date = date.fromisoformat(expiry[:10])
                    u["days_left"] = (exp_date - today).days
                    u["subscription_status"] = "Active" if exp_date >= today else "Expired"
                else:
                    u["days_left"] = None
                    u["subscription_status"] = "No Plan"
                users.append(u)
        return {"users": users, "total": len(users)}
    finally:
        conn.close()


@app.patch("/api/admin/users/{user_id}")
async def admin_update_user(
    user_id: int,
    body: UpdateUserRequest,
    _: str = Depends(require_admin),
):
    """Update a user's plan, status, or password. Admin only."""
    from datetime import date, timedelta
    from dashboard.auth import hash_password as _hash

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if body.plan_type is not None:
                days   = PLAN_DAYS.get(body.plan_type, 30)
                expiry = date.today() + timedelta(days=days)
                cur.execute(
                    "UPDATE users SET plan_type=%s, plan_start=%s, plan_expiry=%s WHERE id=%s",
                    (body.plan_type, date.today(), expiry, user_id)
                )
            if body.is_active is not None:
                cur.execute("UPDATE users SET is_active=%s WHERE id=%s",
                            (body.is_active, user_id))
            if body.password:
                cur.execute("UPDATE users SET password_hash=%s WHERE id=%s",
                            (_hash(body.password), user_id))
        conn.commit()
        return {"updated": user_id, "message": "User updated"}
    finally:
        conn.close()


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(
    user_id: int,
    _: str = Depends(require_admin),
):
    """Delete a user. Admin only."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id=%s RETURNING id", (user_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="User not found")
        conn.commit()
        return {"deleted": user_id}
    finally:
        conn.close()


@app.get("/api/admin/stats")
async def admin_stats(_: str = Depends(require_admin)):
    """Admin dashboard stats — users, subscriptions, DB health."""
    from datetime import date
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*)                                          AS total_users,
                    COUNT(*) FILTER (WHERE is_active AND plan_expiry >= CURRENT_DATE) AS active_subs,
                    COUNT(*) FILTER (WHERE plan_expiry < CURRENT_DATE)                AS expired_subs,
                    COUNT(*) FILTER (WHERE plan_type='1m')  AS plan_1m,
                    COUNT(*) FILTER (WHERE plan_type='3m')  AS plan_3m,
                    COUNT(*) FILTER (WHERE plan_type='6m')  AS plan_6m,
                    COUNT(*) FILTER (WHERE plan_type='12m') AS plan_12m
                FROM users
            """)
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
            stats = dict(zip(cols, row))

            # DB health
            cur.execute(f"SELECT COUNT(*), COUNT(DISTINCT symbol) FROM {TABLE_NAME}")
            rows, syms = cur.fetchone()
            stats["db_rows"]    = rows
            stats["db_symbols"] = syms

        return stats
    finally:
        conn.close()


# -------------------------------------------------------
# Updated auth login — support both admin and users
# -------------------------------------------------------

@app.post("/api/auth/login")
async def auth_login(body: dict):
    """Login for both admin and regular users."""
    from dashboard.auth import authenticate_user, create_access_token

    username = body.get("username", "").strip()
    password = body.get("password", "")

    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required")

    user = authenticate_user(username, password)
    if not user:
        raise HTTPException(status_code=401, detail="Incorrect username or password")

    # Always use the actual username from DB (login may have used mobile number)
    actual_username = user.get("username", username)
    role  = user.get("role", "user")
    token = create_access_token(actual_username, role=role)

    # Update last_login for DB users
    if role != "admin":
        try:
            conn = get_conn()
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET last_login=NOW() WHERE username=%s", (actual_username,))
            conn.commit()
            conn.close()
        except Exception:
            pass

    return {
        "access_token": token,
        "token_type":   "bearer",
        "role":         role,
        "username":     actual_username,
        "plan_type":    user.get("plan_type"),
        "plan_expiry":  str(user.get("plan_expiry", "")),
    }


@app.get("/api/auth/me")
async def auth_me(current: dict = Depends(require_user)):
    """Return current user info."""
    return current


@app.post("/api/auth/logout")
async def auth_logout(
    credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer(auto_error=False)),
):
    """Revoke the current JWT so it cannot be used again."""
    from dashboard.auth import revoke_token
    if credentials and credentials.credentials:
        revoke_token(credentials.credentials)
    return {"message": "Logged out successfully"}


# -------------------------------------------------------
# Static files
# -------------------------------------------------------

@app.get("/")
async def get_index():
    from fastapi.responses import FileResponse
    return FileResponse(
        os.path.join(os.path.dirname(__file__), "index.html"),
        headers={"Cache-Control": "no-cache"},   # always revalidate HTML
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
