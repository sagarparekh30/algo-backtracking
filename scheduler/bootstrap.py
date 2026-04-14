"""
First-Run Bootstrap — auto-detected on every app startup.

If the database has fewer than MIN_ROWS_THRESHOLD candles (fresh environment
or empty DB), this module automatically:

  1. Fetches full 30-year history for all 100 symbols via Yahoo Finance
     (no Fyers token required — free, no auth)
  2. Trains the ML model on the full dataset
  3. Sends a Telegram notification when complete

This runs in a background thread so the FastAPI server starts immediately
and the dashboard is usable while bootstrap is in progress.

Progress is accessible via  GET /api/bootstrap/status
"""

import logging
import os
import sys
import threading
from datetime import datetime

import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ── How many rows count as "has data" ────────────────────────────────────
# 1 800 symbols × 250 trading days/yr × 1 yr ≈ 450 000 rows minimum.
# If the DB is below this we treat it as a fresh environment and kick off
# the full NSE-All backfill.
MIN_ROWS_THRESHOLD = 400_000


# ── Shared bootstrap state (read by /api/bootstrap/status) ───────────────
class _BootstrapState:
    running:      bool = False
    done:         bool = False
    step:         str  = "idle"       # idle | checking | fetching | training | complete | error
    started_at:   str  = ""
    completed_at: str  = ""
    error:        str  = ""
    progress:     dict = {}           # live progress from yfinance fetcher
    result:       dict = {}


state = _BootstrapState()


# ── DB row count helper ───────────────────────────────────────────────────

def _count_db_rows() -> int:
    try:
        from db.connection import get_conn
        from config.settings import TABLE_NAME
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
            row = cur.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception:
        return 0


def _needs_bootstrap() -> bool:
    """Return True if the DB has too few rows to be useful."""
    rows = _count_db_rows()
    logger.info(f"[Bootstrap] DB row count: {rows:,} (threshold: {MIN_ROWS_THRESHOLD:,})")
    return rows < MIN_ROWS_THRESHOLD


# ── Core bootstrap pipeline ───────────────────────────────────────────────

def _run_bootstrap():
    state.running    = True
    state.started_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    logger.info("[Bootstrap] Starting first-run data bootstrap...")

    # ── Step 1: Fetch 30-year history via Yahoo Finance ──────────────────
    state.step = "fetching"
    fetch_result = {}
    try:
        from fetcher.yfinance_fetcher import run_yfinance_backfill
        from config.nse_indices import fetch_nse_all_symbols

        all_symbols = fetch_nse_all_symbols()
        logger.info(f"[Bootstrap] NSE symbol list: {len(all_symbols)} symbols")

        def _progress(processed, total, symbol, candles):
            state.progress = {
                "processed": processed,
                "total":     total,
                "symbol":    symbol,
                "candles":   candles,
                "pct":       round(processed / total * 100, 1) if total else 0,
            }

        logger.info("[Bootstrap] Fetching full 30-year history from Yahoo Finance (NSE All)...")
        fetch_result = run_yfinance_backfill(
            symbols=all_symbols,
            progress_cb=_progress,
            workers=3,
        )
        total_inserted = fetch_result.get("total_new_candles", 0)
        logger.info(f"[Bootstrap] Fetch complete. {total_inserted:,} candles inserted.")

    except Exception as e:
        logger.error(f"[Bootstrap] Fetch failed: {e}")
        state.step  = "error"
        state.error = f"Data fetch failed: {e}"
        state.running = False
        return

    # ── Step 2: Train ML model ────────────────────────────────────────────
    state.step = "training"
    train_result = {}
    try:
        logger.info("[Bootstrap] Training ML model on full dataset...")
        from ml.model import MLPredictor
        predictor = MLPredictor()
        meta = predictor.train()

        horizons = meta.get("horizons", {})
        first_h  = next(iter(horizons.values()), {}) if horizons else {}
        auc      = first_h.get("auc_roc") or meta.get("auc_roc")

        train_result = {
            "success":       True,
            "auc_roc":       auc,
            "symbols_used":  meta.get("symbols_used"),
            "train_samples": meta.get("train_samples"),
            "error":         meta.get("error"),
        }
        logger.info(f"[Bootstrap] ML training complete. AUC={auc} symbols={meta.get('symbols_used')}")

    except Exception as e:
        logger.error(f"[Bootstrap] ML training failed: {e}")
        train_result = {"success": False, "error": str(e)}

    # ── Step 3: Done ─────────────────────────────────────────────────────
    state.step         = "complete"
    state.done         = True
    state.running      = False
    state.completed_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    state.result       = {
        "fetch":    fetch_result,
        "training": train_result,
    }

    # ── Step 4: Telegram notification ────────────────────────────────────
    try:
        from alerts.telegram_bot import TelegramAlert
        rows_now = _count_db_rows()
        auc_str  = f"{train_result.get('auc_roc', 0) * 100:.1f}%" if train_result.get("auc_roc") else "N/A"
        syms_fetched = fetch_result.get("total_symbols", 0)
        msg = (
            "✅ *Apex Trading — Bootstrap Complete*\n\n"
            f"📊 *Data loaded:* {rows_now:,} candles across {syms_fetched} symbols\n"
            f"🤖 *Model trained:* AUC {auc_str}\n"
            f"⏱ *Started:* {state.started_at}\n"
            f"✅ *Completed:* {state.completed_at}\n\n"
            "Your dashboard is ready to use\\."
        )
        TelegramAlert()._send(msg)
    except Exception:
        pass

    logger.info(f"[Bootstrap] Completed at {state.completed_at}")


# ── Public API ────────────────────────────────────────────────────────────

def run_bootstrap_if_needed():
    """
    Called once on app startup.
    Checks DB row count; if below threshold, launches bootstrap in background thread.
    Returns immediately — does not block the FastAPI startup.
    """
    if state.running or state.done:
        return

    state.step = "checking"
    if not _needs_bootstrap():
        state.step = "idle"
        logger.info("[Bootstrap] DB already has sufficient data — skipping bootstrap.")
        return

    logger.info("[Bootstrap] Fresh environment detected. Launching bootstrap in background...")
    t = threading.Thread(target=_run_bootstrap, name="apex-bootstrap", daemon=True)
    t.start()


def get_status() -> dict:
    """Return current bootstrap status for the API."""
    return {
        "required":     state.step != "idle" or state.running,
        "running":      state.running,
        "done":         state.done,
        "step":         state.step,
        "started_at":   state.started_at,
        "completed_at": state.completed_at,
        "error":        state.error,
        "progress":     state.progress,
        "result":       state.result,
    }
