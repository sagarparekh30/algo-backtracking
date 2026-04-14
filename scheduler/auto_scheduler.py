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
    SCHEDULER_ML_DAILY_TIME,
    SCHEDULER_ML_RETRAIN_DAY,
    SCHEDULER_ML_RETRAIN_TIME,
    LOG_DIR,
)

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# ── Shared state (read by the API) ──────────────────────────────────────
class SchedulerState:
    scheduler: BackgroundScheduler = None
    last_data_update:      str = "Never"
    last_data_result:      dict = {}
    last_ml_retrain:       str = "Never"
    last_ml_result:        dict = {}
    last_daily_ml:         str = "Never"
    last_daily_ml_result:  dict = {}
    last_yf_full:          str = "Never"
    last_yf_full_result:   dict = {}
    data_update_running:   bool = False
    ml_retrain_running:    bool = False
    daily_ml_running:      bool = False
    yf_full_running:       bool = False


_state = SchedulerState()


# ── Helper: run combined signals + Telegram alert ───────────────────────

def _run_execute_alert():
    """
    Run all strategies + ML scoring after daily data update, then send a
    Telegram alert if any high-conviction trades are found.

    Mirrors the logic inside /api/combined/signals but runs synchronously
    in the scheduler thread so no FastAPI context is needed.
    """
    try:
        from ml.regime import MarketRegime
        from ml.model import MLPredictor
        from strategies.swing_executor import SwingExecutor
        from alerts.telegram_bot import TelegramAlert

        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # 1. Detect regime
        try:
            regime_obj = MarketRegime()
            regime_info = regime_obj.get_regime()
            regime = regime_info.get("regime", "Neutral")
        except Exception:
            regime = "Neutral"

        buy_thresh = {"Bull": 0.55, "Neutral": 0.60, "Bear": 0.65}.get(regime, 0.60)

        # 2. Collect strategy signals
        executor = SwingExecutor(base_dir)
        all_signals: dict = {}
        strategies = [
            "golden_rsi", "macd_breakout", "bb_squeeze", "ema_crossover",
            "vwap_reversal", "orb_breakout", "momentum_burst", "mean_reversion",
            "volume_climax", "support_bounce", "trend_continuation",
            "rsi_divergence", "bollinger_breakout", "gap_fill",
            "moving_average_ribbon", "supertrend",
        ]
        for strat in strategies:
            try:
                sigs = executor.run_strategy(strat)
                if sigs:
                    all_signals[strat] = sigs
            except Exception:
                pass

        if not all_signals:
            logger.info("[Scheduler] Execute alert: no strategy signals found.")
            return

        # 3. Flatten + ML score
        try:
            predictor = MLPredictor()
            model_loaded = predictor.load()
        except Exception:
            model_loaded = False

        symbol_map: dict = {}
        for strat, sigs in all_signals.items():
            for sig in sigs:
                sym = sig.get("symbol")
                if not sym:
                    continue
                if sym not in symbol_map:
                    symbol_map[sym] = {**sig, "strategies": [strat], "strategy_count": 1}
                else:
                    symbol_map[sym]["strategies"].append(strat)
                    symbol_map[sym]["strategy_count"] += 1

        results = []
        for sym, sig in symbol_map.items():
            prob = 0.5
            if model_loaded:
                try:
                    pred = predictor.predict(sym)
                    prob = pred.get("buy_probability", 0.5)
                except Exception:
                    pass

            if prob >= buy_thresh:
                risk = abs(sig.get("price", 0) - sig.get("stop_loss", 0))
                reward = abs(sig.get("target", sig.get("price", 0)) - sig.get("price", 0))
                conf = (
                    "High" if prob >= 0.75
                    else "Medium" if prob >= 0.60
                    else "Low"
                )
                results.append({
                    **sig,
                    "buy_probability": prob,
                    "confidence": conf,
                    "risk_reward": round(reward / risk, 2) if risk > 0 else None,
                })

        results.sort(key=lambda x: (x["buy_probability"], x["strategy_count"]), reverse=True)

        if not results:
            logger.info(f"[Scheduler] Execute alert: 0 trades passed {int(buy_thresh*100)}% threshold.")
            return

        logger.info(f"[Scheduler] Execute alert: {len(results)} trades found. Sending Telegram.")
        TelegramAlert().send_execute_alert(results, regime, int(buy_thresh * 100))

    except Exception as e:
        logger.error(f"[Scheduler] _run_execute_alert error: {e}")


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

        # After Fyers fetch: run yfinance supplement → retrain ML → send alert
        if success:
            import threading

            def _post_fetch_pipeline():
                # Step 1: yfinance incremental (fills gaps, keeps 30yr data fresh)
                try:
                    _run_yfinance_incremental()
                except Exception as e:
                    logger.error(f"[Scheduler] yfinance incremental failed: {e}")

                # Step 2: retrain ML on the now-updated full dataset
                _job_daily_ml_retrain()

                # Step 3: Telegram trade alert
                try:
                    _run_execute_alert()
                except Exception as e:
                    logger.error(f"[Scheduler] Execute alert error: {e}")

            threading.Thread(
                target=_post_fetch_pipeline,
                name="post-fetch-pipeline",
                daemon=True,
            ).start()
            logger.info("[Scheduler] Post-fetch pipeline started: yfinance → retrain → alert")

    except subprocess.TimeoutExpired:
        _state.last_data_result = {"success": False, "error": "Timed out after 1 hour"}
        logger.error("[Scheduler] Daily data update timed out.")
    except Exception as e:
        _state.last_data_result = {"success": False, "error": str(e)}
        logger.error(f"[Scheduler] Daily data update error: {e}")
    finally:
        _state.data_update_running = False


MAX_TRAIN_RETRIES  = 3      # max attempts before giving up
TRAIN_RETRY_DELAY  = 300    # seconds between retries (5 min)


def _run_yfinance_incremental():
    """
    Run a yfinance incremental fetch for all NSE symbols — fills gaps and
    keeps the 30-year history current.  Called after every Fyers daily fetch.
    Only fetches the last 30 days so it completes quickly.
    """
    try:
        from datetime import date, timedelta as td
        from fetcher.yfinance_fetcher import run_yfinance_backfill
        from config.nse_indices import fetch_nse_all_symbols

        all_symbols = fetch_nse_all_symbols()
        # Only fetch the last 30 days incrementally (fast); full history
        # is already in DB from bootstrap or a previous full refresh run.
        start = (date.today() - td(days=30)).isoformat()
        logger.info(f"[Scheduler] yfinance incremental fetch start={start} ({len(all_symbols)} symbols)")
        result = run_yfinance_backfill(symbols=all_symbols, start_date=start, workers=3)
        logger.info(
            f"[Scheduler] yfinance incremental done. "
            f"new={result.get('total_new_candles',0)} failed={result.get('failed',0)}"
        )
        return result
    except Exception as e:
        logger.error(f"[Scheduler] yfinance incremental error: {e}")
        return {"error": str(e)}


def _run_ml_retrain(label: str) -> dict:
    """
    Core ML retrain logic with automatic retry on failure.

    Attempts up to MAX_TRAIN_RETRIES times.  On each failure it waits
    TRAIN_RETRY_DELAY seconds then tries again from scratch — the model
    trains on ALL data in the DB (up to 30 years if yfinance bootstrap ran).
    """
    started = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    last_error = ""

    for attempt in range(1, MAX_TRAIN_RETRIES + 1):
        try:
            if attempt > 1:
                logger.info(
                    f"[Scheduler] {label} ML retrain — attempt {attempt}/{MAX_TRAIN_RETRIES} "
                    f"(previous error: {last_error})"
                )
                import time as _t
                _t.sleep(TRAIN_RETRY_DELAY)

            logger.info(f"[Scheduler] {label} ML retrain attempt {attempt} starting …")
            from ml.model import MLPredictor
            predictor = MLPredictor()
            meta      = predictor.train()

            # Check if training itself reported an error
            if meta.get("error"):
                last_error = meta["error"]
                logger.warning(f"[Scheduler] {label} attempt {attempt} returned error: {last_error}")
                continue   # retry

            completed = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
            horizons  = meta.get("horizons", {})
            first_h   = next(iter(horizons.values()), {}) if horizons else {}
            auc       = first_h.get("auc_roc") or meta.get("auc_roc")

            logger.info(
                f"[Scheduler] {label} ML retrain done (attempt {attempt}). "
                f"AUC={auc} symbols={meta.get('symbols_used')}"
            )
            return {
                "success":       True,
                "attempt":       attempt,
                "started_at":    started,
                "completed_at":  completed,
                "auc_roc":       auc,
                "symbols_used":  meta.get("symbols_used"),
                "train_samples": meta.get("train_samples"),
            }

        except Exception as e:
            last_error = str(e)
            logger.error(
                f"[Scheduler] {label} ML retrain attempt {attempt} failed: {e}",
                exc_info=True,
            )

    # All attempts exhausted
    logger.error(
        f"[Scheduler] {label} ML retrain FAILED after {MAX_TRAIN_RETRIES} attempts. "
        f"Last error: {last_error}"
    )
    return {
        "success":    False,
        "attempts":   MAX_TRAIN_RETRIES,
        "error":      last_error,
        "started_at": started,
    }


def _job_daily_ml_retrain():
    """
    Daily ML retrain — runs every weekday after the data update.
    Trains on all available history (30yr if yfinance bootstrap ran).
    Retries up to MAX_TRAIN_RETRIES times on failure.
    """
    if _state.daily_ml_running or _state.ml_retrain_running:
        logger.warning("[Scheduler] Daily ML retrain skipped — another retrain already running.")
        return

    _state.daily_ml_running = True
    try:
        result = _run_ml_retrain("Daily")
        _state.last_daily_ml        = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
        _state.last_daily_ml_result = result
        # Reload the in-process predictor so new predictions use the fresh model
        if result.get("success"):
            try:
                from dashboard.main import reload_ml_predictor
                reload_ml_predictor()
            except Exception:
                pass
    finally:
        _state.daily_ml_running = False


def _job_weekly_ml_retrain():
    """
    Full deep retrain — runs every Sunday night on all available history.
    Retries up to MAX_TRAIN_RETRIES times on failure.
    """
    if _state.ml_retrain_running:
        logger.warning("[Scheduler] Weekly ML retrain already running — skipping.")
        return

    _state.ml_retrain_running = True
    try:
        result = _run_ml_retrain("Weekly")
        _state.last_ml_retrain = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
        _state.last_ml_result  = result
        if result.get("success"):
            try:
                from dashboard.main import reload_ml_predictor
                reload_ml_predictor()
            except Exception:
                pass
    finally:
        _state.ml_retrain_running = False


def _job_weekly_yfinance_full():
    """
    Weekly full yfinance history refresh — runs Sunday morning (06:00 IST).
    Fetches up to 30 years of candles for all NIFTY 500 symbols and inserts
    any missing rows.  Runs BEFORE the weekly deep ML retrain so the model
    always trains on the most complete dataset available.
    """
    if _state.yf_full_running:
        logger.warning("[Scheduler] Weekly yfinance full refresh already running — skipping.")
        return

    _state.yf_full_running = True
    started = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    logger.info("[Scheduler] Weekly yfinance full history refresh starting …")

    try:
        from fetcher.yfinance_fetcher import run_yfinance_backfill
        from config.nse_indices import fetch_nse_all_symbols

        all_symbols = fetch_nse_all_symbols()
        logger.info(f"[Scheduler] Weekly yfinance full: {len(all_symbols)} symbols, period=max (30yr)")
        result = run_yfinance_backfill(symbols=all_symbols, workers=3)   # period='max' = 30yr
        _state.last_yf_full        = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
        _state.last_yf_full_result = {
            "success":       True,
            "started_at":    started,
            "completed_at":  _state.last_yf_full,
            "new_candles":   result.get("total_new_candles", 0),
            "failed":        result.get("failed", 0),
        }
        logger.info(
            f"[Scheduler] Weekly yfinance full done. "
            f"new={result.get('total_new_candles',0)} failed={result.get('failed',0)}"
        )
    except Exception as e:
        logger.error(f"[Scheduler] Weekly yfinance full error: {e}")
        _state.last_yf_full_result = {"success": False, "error": str(e), "started_at": started}
    finally:
        _state.yf_full_running = False


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

    # ── Job 2: Daily ML retrain (Mon–Fri after data update) ────────────
    if SCHEDULER_ML_DAILY_TIME:
        dml_h, dml_m = SCHEDULER_ML_DAILY_TIME.split(":")
        scheduler.add_job(
            _job_daily_ml_retrain,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour=int(dml_h),
                minute=int(dml_m),
                timezone=IST,
            ),
            id="daily_ml_retrain",
            name="Daily ML Retrain",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )

    # ── Job 3: Weekly yfinance full history refresh (Sunday 06:00 IST) ───
    # Runs BEFORE the deep ML retrain so training uses the freshest 30yr data
    scheduler.add_job(
        _job_weekly_yfinance_full,
        trigger=CronTrigger(
            day_of_week=SCHEDULER_ML_RETRAIN_DAY,
            hour=6,
            minute=0,
            timezone=IST,
        ),
        id="weekly_yfinance_full",
        name="Weekly yfinance Full History Refresh",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=7200,
    )

    # ── Job 4: Weekly deep ML retrain (Sunday night) ─────────────────────
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
        f"yfinance supplement: daily after fetch | "
        f"Daily ML retrain: Mon–Fri {SCHEDULER_ML_DAILY_TIME} IST (retry up to {MAX_TRAIN_RETRIES}x) | "
        f"Weekly yfinance full: {SCHEDULER_ML_RETRAIN_DAY.capitalize()} 06:00 IST | "
        f"Weekly deep retrain: {SCHEDULER_ML_RETRAIN_DAY.capitalize()} {SCHEDULER_ML_RETRAIN_TIME} IST"
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
            "schedule":          f"Mon–Fri {SCHEDULER_DATA_UPDATE_TIME} IST (after market close)",
            "last_run":          _state.last_data_update,
            "last_result":       _state.last_data_result,
            "currently_running": _state.data_update_running,
        },
        "daily_ml_retrain": {
            "schedule":          f"Mon–Fri {SCHEDULER_ML_DAILY_TIME} IST (auto after data fetch)",
            "last_run":          _state.last_daily_ml,
            "last_result":       _state.last_daily_ml_result,
            "currently_running": _state.daily_ml_running,
        },
        "weekly_yfinance_full": {
            "schedule":          f"{SCHEDULER_ML_RETRAIN_DAY.capitalize()} 06:00 IST (30yr history refresh)",
            "last_run":          _state.last_yf_full,
            "last_result":       _state.last_yf_full_result,
            "currently_running": _state.yf_full_running,
        },
        "weekly_ml_retrain": {
            "schedule":          f"{SCHEDULER_ML_RETRAIN_DAY.capitalize()} {SCHEDULER_ML_RETRAIN_TIME} IST (deep retrain, {MAX_TRAIN_RETRIES} retries)",
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
            _job_daily_ml_retrain,
            id="manual_ml_retrain",
            name="Manual ML Retrain",
            replace_existing=True,
        )
    else:
        import threading
        threading.Thread(target=_job_daily_ml_retrain, daemon=True).start()
