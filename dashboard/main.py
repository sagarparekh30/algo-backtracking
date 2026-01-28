import os
import json
import asyncio
import subprocess
import re
import sqlite3
from datetime import datetime
from typing import List, Optional, Dict

from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Import existing settings
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import TOKEN_PATH, LOG_DIR, DB_PATH, TABLE_NAME

app = FastAPI(title="Trading HQ Dashboard")

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

# State Management
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

state = DashboardState()

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

def get_db_stats():
    """Fetch database health metrics."""
    if os.path.exists(DB_PATH):
        state.db_size_mb = round(os.path.getsize(DB_PATH) / (1024 * 1024), 2)
    
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(trade_date), MAX(trade_date) FROM {TABLE_NAME}")
        rows, syms, d1, d2 = cursor.fetchone()
        state.total_db_rows = rows or 0
        state.unique_symbols = syms or 0
        state.min_date = d1 or "N/A"
        state.max_date = d2 or "N/A"
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

@app.get("/api/ui_config")
async def get_ui_config():
    config_path = os.path.join(os.path.dirname(__file__), "ui_config.json")
    if os.path.exists(config_path):
        import json
        with open(config_path, "r") as f:
            return json.load(f)
    return {}

@app.get("/api/latest_snapshot")
async def get_latest_snapshot():
    try:
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # Query 10 most recent records
        cursor.execute(f"""
            SELECT symbol, trade_date, open, high, low, close, volume 
            FROM {TABLE_NAME} 
            ORDER BY ROWID DESC 
            LIMIT 10
        """)
        rows = cursor.fetchall()
        conn.close()
        return [
            {
                "symbol": r[0],
                "date": r[1],
                "open": r[2],
                "high": r[3],
                "low": r[4],
                "close": r[5],
                "volume": r[6]
            } for r in rows
        ]
    except Exception as e:
        print(f"Snapshot Error: {e}")
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
            "python", "-u", BACKFILL_SCRIPT,
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

@app.get("/")
async def get_index():
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
