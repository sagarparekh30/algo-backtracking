"""
Auto-Scheduler — runs inside the FastAPI process using APScheduler.

Jobs:
  1. Daily data update    — weekdays at 4:00 PM IST (after market close)
                            Fetches new candles for all 99 symbols (incremental).

  2. Weekly ML retrain    — every Sunday at 10:00 PM IST
                            Full walk-forward retrain on all available history.

Both schedules are configurable via .env:
  SCHEDULER_DATA_UPDATE_TIME = "16:00"    # HH:MM IST
  SCHEDULER_ML_RETRAIN_DAY   = "sun"      # mon/tue/wed/thu/fri/sat/sun
  SCHEDULER_ML_RETRAIN_TIME  = "22:00"    # HH:MM IST
  SCHEDULER_ENABLED          = "true"

Usage (called once from dashboard/main.py on startup):
  from scheduler.auto_scheduler import start_scheduler, get_scheduler_status
"""

import logging
import subprocess
import sys
import os
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

from config.settings import (
    SCHEDULER_ENABLED,
    SCHEDULER_DATA_UPDATE_TIME,
    SCHEDULER_ML_RETRAIN_DAY,
    SCHEDULER_ML_RETRAIN_TIME,
    LOG_DIR,
)

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# ── Shared state (read by the API) ──────────────────────────────────────
class SchedulerState:
    scheduler: BackgroundScheduler = None
    last_data_update:  str = "Never"
    last_data_result:  dict = {}
    last_ml_retrain:   str = "Never"
    last_ml_result:    dict = {}
    data_update_running: bool = False
    ml_retrain_running:  bool = False


_state = SchedulerState()


# ── Job functions ────────────────────────────────────────────────────────

def _job_daily_data_update():
    """
    Incremental data fetch job — runs after market close every weekday.
    Calls the backfill script as a subprocess (same as the dashboard button).
    """
    if _state.data_update_running:
        logger.warning("Daily data update already running — skipping.")
        return

    _state.data_update_running = True
    started = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    logger.info(f"[Scheduler] Daily data update started at {started}")

    try:
        base_dir     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        backfill_script = os.path.join(base_dir, "fetcher", "backfill_fyers_equity.py")

        result = subprocess.run(
            [sys.executable, backfill_script],
            capture_output=True,
            text=True,
            timeout=3600,   # 1 hour max
            cwd=base_dir,
        )

        total_candles = 0
        for line in (result.stdout + result.stderr).splitlines():
            if "Total candles inserted:" in line:
                try:
                    total_candles = int(line.split(":")[-1].strip())
                except ValueError:
                    pass

        success = result.returncode == 0
        _state.last_data_update = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
        _state.last_data_result = {
            "success":       success,
            "total_candles": total_candles,
            "started_at":    started,
            "completed_at":  _state.last_data_update,
        }
        logger.info(f"[Scheduler] Daily data update done. candles={total_candles} success={success}")

    except subprocess.TimeoutExpired:
        _state.last_data_result = {"success": False, "error": "Timed out after 1 hour"}
        logger.error("[Scheduler] Daily data update timed out.")
    except Exception as e:
        _state.last_data_result = {"success": False, "error": str(e)}
        logger.error(f"[Scheduler] Daily data update error: {e}")
    finally:
        _state.data_update_running = False


def _job_weekly_ml_retrain():
    """
    Full ML retrain job — runs every Sunday night.
    Trains on all available history in the DB (up to 10 years).
    """
    if _state.ml_retrain_running:
        logger.warning("[Scheduler] ML retrain already running — skipping.")
        return

    _state.ml_retrain_running = True
    started = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    logger.info(f"[Scheduler] Weekly ML retrain started at {started}")

    try:
        from ml.model import MLPredictor
        predictor = MLPredictor()
        meta = predictor.train()

        _state.last_ml_retrain = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
        _state.last_ml_result  = {
            "started_at":   started,
            "completed_at": _state.last_ml_retrain,
            "auc_roc":      meta.get("auc_roc"),
            "accuracy":     meta.get("accuracy"),
            "symbols_used": meta.get("symbols_used"),
            "train_samples":meta.get("train_samples"),
            "error":        meta.get("error"),
        }
        logger.info(f"[Scheduler] ML retrain done. AUC={meta.get('auc_roc')} acc={meta.get('accuracy')}")

    except Exception as e:
        _state.last_ml_result = {"success": False, "error": str(e), "started_at": started}
        logger.error(f"[Scheduler] ML retrain error: {e}")
    finally:
        _state.ml_retrain_running = False


# ── Scheduler lifecycle ──────────────────────────────────────────────────

def start_scheduler():
    """
    Start the APScheduler background scheduler.
    Called once from the FastAPI startup event.
    """
    if not SCHEDULER_ENABLED:
        logger.info("[Scheduler] Auto-scheduler disabled (SCHEDULER_ENABLED=false).")
        return

    scheduler = BackgroundScheduler(timezone=IST)

    # ── Job 1: Daily data update (Mon–Fri after market close) ──────────
    data_h, data_m = SCHEDULER_DATA_UPDATE_TIME.split(":")
    scheduler.add_job(
        _job_daily_data_update,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=int(data_h),
            minute=int(data_m),
            timezone=IST,
        ),
        id="daily_data_update",
        name="Daily Data Update",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # ── Job 2: Weekly ML retrain ────────────────────────────────────────
    ml_h, ml_m = SCHEDULER_ML_RETRAIN_TIME.split(":")
    scheduler.add_job(
        _job_weekly_ml_retrain,
        trigger=CronTrigger(
            day_of_week=SCHEDULER_ML_RETRAIN_DAY,
            hour=int(ml_h),
            minute=int(ml_m),
            timezone=IST,
        ),
        id="weekly_ml_retrain",
        name="Weekly ML Retrain",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=7200,
    )

    scheduler.start()
    _state.scheduler = scheduler

    logger.info(
        f"[Scheduler] Started. "
        f"Data update: Mon–Fri {SCHEDULER_DATA_UPDATE_TIME} IST | "
        f"ML retrain: {SCHEDULER_ML_RETRAIN_DAY.capitalize()} {SCHEDULER_ML_RETRAIN_TIME} IST"
    )


def stop_scheduler():
    """Gracefully shut down the scheduler (called on app shutdown)."""
    if _state.scheduler and _state.scheduler.running:
        _state.scheduler.shutdown(wait=False)
        logger.info("[Scheduler] Stopped.")


def get_scheduler_status() -> dict:
    """Return current scheduler status for the API."""
    jobs = []
    if _state.scheduler:
        for job in _state.scheduler.get_jobs():
            nxt = job.next_run_time
            jobs.append({
                "id":           job.id,
                "name":         job.name,
                "next_run_ist": nxt.astimezone(IST).strftime("%Y-%m-%d %H:%M IST") if nxt else "N/A",
            })

    return {
        "enabled":               SCHEDULER_ENABLED,
        "running":               bool(_state.scheduler and _state.scheduler.running),
        "jobs":                  jobs,
        "data_update": {
            "schedule":          f"Mon–Fri {SCHEDULER_DATA_UPDATE_TIME} IST",
            "last_run":          _state.last_data_update,
            "last_result":       _state.last_data_result,
            "currently_running": _state.data_update_running,
        },
        "ml_retrain": {
            "schedule":          f"{SCHEDULER_ML_RETRAIN_DAY.capitalize()} {SCHEDULER_ML_RETRAIN_TIME} IST",
            "last_run":          _state.last_ml_retrain,
            "last_result":       _state.last_ml_result,
            "currently_running": _state.ml_retrain_running,
        },
    }


def trigger_data_update_now():
    """Manually trigger data update outside the schedule."""
    if _state.scheduler:
        _state.scheduler.add_job(
            _job_daily_data_update,
            id="manual_data_update",
            name="Manual Data Update",
            replace_existing=True,
        )
    else:
        import threading
        threading.Thread(target=_job_daily_data_update, daemon=True).start()


def trigger_ml_retrain_now():
    """Manually trigger ML retrain outside the schedule."""
    if _state.scheduler:
        _state.scheduler.add_job(
            _job_weekly_ml_retrain,
            id="manual_ml_retrain",
            name="Manual ML Retrain",
            replace_existing=True,
        )
    else:
        import threading
        threading.Thread(target=_job_weekly_ml_retrain, daemon=True).start()
