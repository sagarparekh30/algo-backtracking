"""
Daily pipeline runner — runs the complete trading workflow:
  1. Data backfill (fetch today's candles via Fyers)
  2. Strategy scans (all 6 strategies)
  3. Telegram morning report with all signals
  4. Full logging of every step

Usage:
  python scheduler/daily_runner.py
  (or import run_daily_pipeline and call from dashboard)
"""

import os
import sys
import logging
import subprocess
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

from config.settings import LOG_DIR

# -------------------------------------------------------
# Logging
# -------------------------------------------------------

os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "daily_runner.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("daily_runner")

# Path to the backfill script
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKFILL_SCRIPT = os.path.join(BASE_DIR, "fetcher", "backfill_fyers_equity.py")

# -------------------------------------------------------
# Individual pipeline steps
# -------------------------------------------------------


def _step_backfill() -> dict:
    """
    Run the FYERS equity backfill script as a subprocess.

    Returns:
        dict with keys: success (bool), total_candles (int), message (str)
    """
    logger.info("=== Step 1: Data Backfill ===")
    try:
        result = subprocess.run(
            [sys.executable, BACKFILL_SCRIPT],
            capture_output=True,
            text=True,
            timeout=1800,  # 30 minutes max
            cwd=BASE_DIR,
        )
        output = result.stdout + result.stderr

        # Parse candle count from log output
        total_candles = 0
        for line in output.splitlines():
            if "Total candles inserted:" in line:
                try:
                    total_candles = int(line.split(":")[-1].strip())
                except ValueError:
                    pass

        if result.returncode == 0:
            logger.info(f"Backfill completed. New candles: {total_candles}")
            return {"success": True, "total_candles": total_candles, "message": "OK"}
        else:
            logger.error(f"Backfill failed with return code {result.returncode}")
            logger.error(output[-2000:])  # last 2000 chars
            return {
                "success": False,
                "total_candles": 0,
                "message": f"Return code {result.returncode}",
            }
    except subprocess.TimeoutExpired:
        logger.error("Backfill timed out after 30 minutes.")
        return {"success": False, "total_candles": 0, "message": "Timeout"}
    except Exception as e:
        logger.error(f"Backfill exception: {e}")
        return {"success": False, "total_candles": 0, "message": str(e)}


def _step_scan_strategies() -> dict:
    """
    Run all 6 strategies and collect signals.

    Returns:
        dict: {strategy_id: [signal, ...]}
    """
    logger.info("=== Step 2: Strategy Scans ===")
    try:
        from strategies.swing_executor import StrategyManager, STRATEGY_DESCRIPTIONS

        mgr = StrategyManager()
        all_signals = {}

        for strategy_id in STRATEGY_DESCRIPTIONS:
            try:
                signals = mgr.get_signals(strategy_id)
                all_signals[strategy_id] = signals
                logger.info(f"  {strategy_id}: {len(signals)} signal(s)")
            except Exception as e:
                logger.error(f"  {strategy_id} scan error: {e}")
                all_signals[strategy_id] = []

        total = sum(len(v) for v in all_signals.values())
        logger.info(f"Scan complete. Total signals: {total}")
        return all_signals
    except Exception as e:
        logger.error(f"Strategy scan step failed: {e}")
        return {}


def _step_send_alerts(all_signals: dict, backfill_result: dict) -> bool:
    """
    Send Telegram morning report.

    Args:
        all_signals: dict from _step_scan_strategies.
        backfill_result: dict from _step_backfill.

    Returns:
        True if alert sent successfully.
    """
    logger.info("=== Step 3: Telegram Alerts ===")
    try:
        from alerts.telegram_bot import TelegramAlert

        bot = TelegramAlert()

        # Flatten all signals into a single list for the report
        flat_signals = []
        for sigs in all_signals.values():
            flat_signals.extend(sigs)

        backfill_status = {
            "symbols": backfill_result.get("total_candles", 0),
            "candles": backfill_result.get("total_candles", 0),
        }

        ok = bot.send_morning_report(flat_signals, backfill_status=backfill_status)
        if ok:
            logger.info("Morning report sent via Telegram.")
        else:
            logger.info("Telegram not configured or send failed (non-fatal).")
        return ok
    except Exception as e:
        logger.error(f"Telegram alert step failed: {e}")
        return False


# -------------------------------------------------------
# Main pipeline
# -------------------------------------------------------


def run_daily_pipeline() -> dict:
    """
    Execute the complete daily trading pipeline.

    Returns:
        Summary dict with keys: started_at, completed_at, backfill,
        total_signals, signals_by_strategy, telegram_sent.
    """
    started_at = datetime.now().isoformat()
    logger.info("=" * 60)
    logger.info(f"Daily Pipeline Started at {started_at}")
    logger.info("=" * 60)

    summary = {
        "started_at": started_at,
        "completed_at": None,
        "backfill": {},
        "total_signals": 0,
        "signals_by_strategy": {},
        "telegram_sent": False,
    }

    try:
        # Step 1: Backfill
        backfill_result = _step_backfill()
        summary["backfill"] = backfill_result

        # Step 2: Strategy scans
        all_signals = _step_scan_strategies()
        summary["signals_by_strategy"] = {
            k: len(v) for k, v in all_signals.items()
        }
        summary["total_signals"] = sum(len(v) for v in all_signals.values())

        # Step 3: Telegram
        telegram_sent = _step_send_alerts(all_signals, backfill_result)
        summary["telegram_sent"] = telegram_sent

    except Exception as e:
        logger.error(f"Daily pipeline unexpected error: {e}", exc_info=True)

    completed_at = datetime.now().isoformat()
    summary["completed_at"] = completed_at

    logger.info("=" * 60)
    logger.info(f"Daily Pipeline Completed at {completed_at}")
    logger.info(f"Summary: {summary}")
    logger.info("=" * 60)

    return summary


# -------------------------------------------------------
# Entry point
# -------------------------------------------------------

if __name__ == "__main__":
    run_daily_pipeline()
