"""
Trade Analytics & ML Retraining Pipeline — ml/trade_analytics.py

Phase 2 — Weekly analytics:
  - Win rate by strategy combination
  - Average R:R achieved vs predicted
  - Model accuracy (stocks with prob > 0.70)
  - Regime accuracy

Phase 3 — Retraining trigger:
  - If last 50 closed trades show model accuracy < 55% → flag for retraining
  - Export retrain_dataset.csv (features + outcome label)

Phase 4 — Weekly report dict for API endpoint

Usage:
    from ml.trade_analytics import TradeAnalytics

    ta = TradeAnalytics()
    report = ta.weekly_report()
    # {"win_rate_overall": 62.5, "strategy_breakdown": [...], ...}

    if ta.should_retrain():
        path = ta.export_retrain_dataset()
        # → "data/retrain_dataset.csv"
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

RETRAIN_WINDOW    = 50    # last N closed trades for accuracy check
RETRAIN_THRESHOLD = 0.55  # if accuracy < this, flag for retraining
RETRAIN_CSV_PATH  = Path(__file__).parent.parent / "data" / "retrain_dataset.csv"


class TradeAnalytics:
    """
    Loads closed trades from the database and computes performance analytics.
    All DB queries use parameterised SQL via psycopg2.
    """

    def __init__(self):
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from db.connection import get_conn
        self._get_conn = get_conn

    # ── Phase 2: Analytics queries ────────────────────────────────────────

    def win_rate_by_strategy(self, days: int = 90) -> list[dict]:
        """
        Win rate per strategy over the last `days` calendar days.
        Returns list of {strategy, total_trades, wins, win_rate_pct,
                          avg_win_pct, avg_loss_pct, last_trade_date}
        """
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT strategy, total_trades, wins, win_rate_pct,
                           avg_win_pct, avg_loss_pct, last_trade_date
                    FROM strategy_win_rates
                    ORDER BY win_rate_pct DESC NULLS LAST
                """)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]

    def avg_rr_analysis(self) -> dict:
        """
        Compares predicted R:R (from target/stop_loss) vs achieved R:R.
        """
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        ROUND(AVG(
                            CASE WHEN stop_loss > 0 AND target > 0
                            THEN (target - entry_price) / NULLIF(entry_price - stop_loss, 0)
                            END
                        ), 2) AS avg_predicted_rr,
                        ROUND(AVG(
                            CASE WHEN stop_loss > 0 AND pnl IS NOT NULL AND entry_price > 0
                            THEN ABS(pnl_pct) / NULLIF(
                                ABS(entry_price - stop_loss) / entry_price * 100, 0)
                            END
                        ), 2) AS avg_achieved_rr,
                        COUNT(*) AS total_closed
                    FROM trade_journal
                    WHERE status IN ('win', 'loss', 'stopped')
                      AND entry_date >= CURRENT_DATE - INTERVAL '90 days'
                """)
                row = cur.fetchone()
                if row:
                    return {
                        "avg_predicted_rr": float(row[0] or 0),
                        "avg_achieved_rr":  float(row[1] or 0),
                        "total_closed":     int(row[2] or 0),
                    }
                return {"avg_predicted_rr": 0, "avg_achieved_rr": 0, "total_closed": 0}

    def model_accuracy(self, prob_threshold: float = 0.70) -> dict:
        """
        Did stocks with AI prob > threshold actually win more than stocks below?
        """
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        SUM(CASE WHEN buy_probability >= %s AND status='win' THEN 1 ELSE 0 END)  AS high_prob_wins,
                        SUM(CASE WHEN buy_probability >= %s                  THEN 1 ELSE 0 END)  AS high_prob_total,
                        SUM(CASE WHEN buy_probability <  %s AND status='win' THEN 1 ELSE 0 END)  AS low_prob_wins,
                        SUM(CASE WHEN buy_probability <  %s                  THEN 1 ELSE 0 END)  AS low_prob_total
                    FROM trade_journal
                    WHERE status IN ('win', 'loss', 'stopped')
                      AND buy_probability IS NOT NULL
                      AND entry_date >= CURRENT_DATE - INTERVAL '90 days'
                """, (prob_threshold, prob_threshold, prob_threshold, prob_threshold))
                row = cur.fetchone()
                hp_w, hp_t, lp_w, lp_t = (int(x or 0) for x in row)
                return {
                    "threshold":          prob_threshold,
                    "high_prob_win_rate": round(hp_w / hp_t * 100, 1) if hp_t else None,
                    "low_prob_win_rate":  round(lp_w / lp_t * 100, 1) if lp_t else None,
                    "high_prob_trades":   hp_t,
                    "low_prob_trades":    lp_t,
                    "edge":               round((hp_w/hp_t - lp_w/lp_t)*100, 1)
                                          if hp_t and lp_t else None,
                }

    def regime_accuracy(self) -> list[dict]:
        """
        Signal quality broken down by market regime (Bull/Neutral/Bear).
        """
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COALESCE(regime, 'Unknown') AS regime,
                        COUNT(*) AS total,
                        SUM(CASE WHEN status='win' THEN 1 ELSE 0 END) AS wins,
                        ROUND(100.0 * SUM(CASE WHEN status='win' THEN 1 ELSE 0 END)
                              / NULLIF(COUNT(*), 0), 1) AS win_rate_pct,
                        ROUND(AVG(pnl_pct), 2) AS avg_pnl_pct
                    FROM trade_journal
                    WHERE status IN ('win', 'loss', 'stopped')
                      AND entry_date >= CURRENT_DATE - INTERVAL '90 days'
                    GROUP BY 1
                    ORDER BY win_rate_pct DESC NULLS LAST
                """)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ── Phase 3: Retraining trigger ───────────────────────────────────────

    def should_retrain(self) -> bool:
        """
        Returns True if last RETRAIN_WINDOW closed trades show model accuracy
        below RETRAIN_THRESHOLD.
        """
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT status
                    FROM trade_journal
                    WHERE status IN ('win', 'loss', 'stopped')
                      AND buy_probability IS NOT NULL
                    ORDER BY exit_date DESC NULLS LAST, id DESC
                    LIMIT %s
                """, (RETRAIN_WINDOW,))
                rows = cur.fetchall()

        if len(rows) < RETRAIN_WINDOW:
            return False   # not enough data

        wins     = sum(1 for r in rows if r[0] == 'win')
        accuracy = wins / len(rows)
        logger.info(f"Model accuracy on last {len(rows)} trades: {accuracy:.1%}")
        return accuracy < RETRAIN_THRESHOLD

    def export_retrain_dataset(self) -> str:
        """
        Export closed trades as CSV for model retraining.

        Columns: same features used at signal time + hit_target (0/1).
        Returns the CSV file path.
        """
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        symbol,
                        entry_price,
                        stop_loss,
                        target,
                        buy_probability,
                        quality_score,
                        regime,
                        strategies_fired,
                        held_days,
                        atr_at_entry,
                        liquidity_score,
                        CASE WHEN status = 'win' THEN 1 ELSE 0 END AS hit_target
                    FROM trade_journal
                    WHERE status IN ('win', 'loss', 'stopped')
                      AND buy_probability IS NOT NULL
                    ORDER BY entry_date DESC
                """)
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]

        RETRAIN_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(RETRAIN_CSV_PATH, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(cols)
            writer.writerows(rows)

        logger.info(f"Retraining dataset exported: {RETRAIN_CSV_PATH} ({len(rows)} rows)")
        return str(RETRAIN_CSV_PATH)

    # ── Phase 4: Weekly report ────────────────────────────────────────────

    def weekly_report(self) -> dict:
        """
        Compile a complete weekly performance report.

        Returns:
            {
              "generated_at":         str,
              "win_rate_overall":      float | None,
              "total_closed_trades":   int,
              "avg_pnl_pct":           float | None,
              "strategy_breakdown":    [...],
              "rr_analysis":           {...},
              "model_accuracy":        {...},
              "regime_accuracy":       [...],
              "model_drift_score":     float,   # 0–100; high = more drift
              "retrain_recommended":   bool,
            }
        """
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT
                            COUNT(*) AS total,
                            SUM(CASE WHEN status='win' THEN 1 ELSE 0 END) AS wins,
                            ROUND(AVG(pnl_pct), 2) AS avg_pnl
                        FROM trade_journal
                        WHERE status IN ('win', 'loss', 'stopped')
                          AND entry_date >= CURRENT_DATE - INTERVAL '7 days'
                    """)
                    r = cur.fetchone()
                    total = int(r[0] or 0)
                    wins  = int(r[1] or 0)
                    wr    = round(wins / total * 100, 1) if total else None
                    avg_p = float(r[2] or 0) if r[2] is not None else None
        except Exception as e:
            logger.warning(f"weekly_report overall query failed: {e}")
            total, wins, wr, avg_p = 0, 0, None, None

        try:
            strat_breakdown = self.win_rate_by_strategy(days=7)
        except Exception:
            strat_breakdown = []

        try:
            rr = self.avg_rr_analysis()
        except Exception:
            rr = {}

        try:
            ma = self.model_accuracy()
        except Exception:
            ma = {}

        try:
            ra = self.regime_accuracy()
        except Exception:
            ra = []

        retrain = False
        try:
            retrain = self.should_retrain()
        except Exception:
            pass

        # Model drift score: 0 = perfect, 100 = completely wrong
        hp_wr = ma.get("high_prob_win_rate")
        drift = round(max(0, 55 - (hp_wr or 50)), 1) * 2   # 0–100 scale

        return {
            "generated_at":       datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "win_rate_overall":   wr,
            "total_closed_trades": total,
            "avg_pnl_pct":        avg_p,
            "strategy_breakdown": strat_breakdown,
            "rr_analysis":        rr,
            "model_accuracy":     ma,
            "regime_accuracy":    ra,
            "model_drift_score":  min(100, drift),
            "retrain_recommended": retrain,
        }
