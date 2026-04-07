"""
Trade Outcome Learning Engine — ml/trade_feedback_model.py

Learns from past closed trades in trade_journal to predict the probability
of a new trade succeeding BEFORE entry. Acts as a second-opinion filter
layered on top of the existing ML price-direction model.

Pipeline position:
  strategy scan → ML price model → [Trade Feedback] → quality score → output

Features extracted from trade parameters at entry time:
  risk_pct          — (entry - stop_loss) / entry
  reward_pct        — (target - entry) / entry
  rr_ratio          — reward / risk
  buy_probability   — ML model output stored at trade time
  expected_return   — regressor output stored at trade time
  day_of_week       — 0=Mon … 4=Fri
  strategy_count    — number of strategies that fired
  trade_type_enc    — 1=live, 0=paper

Labels:
  1 = win    (status = 'win')
  0 = loss   (status = 'loss' or 'stopped')

Output:
  feedback_probability  — 0.0–1.0 (calibrated)
  feedback_signal       — 'boost' | 'neutral' | 'penalise'
  adjustment            — float added to quality score (-10 to +10 pts)
"""

import os
import sys
import json
import logging
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

from config.settings import BASE_DIR
from db.connection import get_conn

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────
MODEL_DIR    = os.path.join(str(BASE_DIR), "ml", "models")
FB_CLF_PATH  = os.path.join(MODEL_DIR, "trade_feedback.joblib")
FB_META_PATH = os.path.join(MODEL_DIR, "trade_feedback_meta.json")

# ── Config ───────────────────────────────────────────────────────────────
MIN_TRADES = 20           # minimum closed trades required to train
MAX_ADJUSTMENT = 10.0     # max quality score points to add/subtract

FEATURE_NAMES = [
    "risk_pct",
    "reward_pct",
    "rr_ratio",
    "buy_probability",
    "expected_return_pct",
    "day_of_week",
    "strategy_count",
    "trade_type_enc",
]


class TradeFeedbackModel:
    """
    Learns from past trade journal outcomes to predict trade success probability.
    Runs independently of the price-direction ML model.
    """

    def __init__(self):
        self.clf  = None
        self.meta: dict = {}

    # ─────────────────────────────────────────────────────────────────────
    # Training
    # ─────────────────────────────────────────────────────────────────────

    def train(self, progress_cb=None) -> dict:
        """
        Train classifier from closed trades in trade_journal.

        Args:
            progress_cb: optional callable(step: str) for progress updates.

        Returns:
            metrics dict or {"error": str}.
        """
        try:
            import joblib
            from sklearn.ensemble import RandomForestClassifier
            from sklearn.calibration import CalibratedClassifierCV
            from sklearn.preprocessing import RobustScaler
            from sklearn.pipeline import Pipeline
            from sklearn.metrics import classification_report, roc_auc_score
            from sklearn.model_selection import train_test_split
        except ImportError as e:
            return {"error": f"Missing dependency: {e}. Run: pip install scikit-learn joblib"}

        os.makedirs(MODEL_DIR, exist_ok=True)

        if progress_cb:
            progress_cb("Loading trade history…")

        df = self._load_closed_trades()
        if df is None or len(df) < MIN_TRADES:
            count = 0 if df is None else len(df)
            return {
                "error": (
                    f"Not enough closed trades to train. "
                    f"Need ≥{MIN_TRADES}, have {count}. "
                    f"Paper-trade or live-trade more to generate feedback data."
                )
            }

        if progress_cb:
            progress_cb(f"Building features from {len(df)} trades…")

        X, y = self._build_feature_matrix(df)
        if X is None or len(X) < MIN_TRADES:
            return {"error": "Not enough valid feature rows after processing."}

        pos_rate = float(y.mean() * 100)
        logger.info(f"[TradeFeedback] {len(X)} trades | Win rate: {pos_rate:.1f}%")

        # Stratified split (handles class imbalance)
        stratify = y if len(np.unique(y)) > 1 else None
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42, stratify=stratify
        )

        if progress_cb:
            progress_cb("Training Random Forest + calibration…")

        pipeline = Pipeline([
            ("scaler", RobustScaler()),
            ("clf", CalibratedClassifierCV(
                RandomForestClassifier(
                    n_estimators=100,
                    max_depth=6,
                    min_samples_leaf=3,
                    max_features="sqrt",
                    class_weight="balanced",
                    random_state=42,
                    n_jobs=2,
                ),
                method="isotonic",
                cv=min(3, len(np.unique(y_train))),
            )),
        ])
        pipeline.fit(X_train, y_train)

        # Evaluate
        y_proba = pipeline.predict_proba(X_test)[:, 1]
        y_pred  = (y_proba >= 0.5).astype(int)
        report  = classification_report(y_test, y_pred, output_dict=True, zero_division=0)

        try:
            auc = round(roc_auc_score(y_test, y_proba), 4)
        except Exception:
            auc = 0.0

        # Feature importances
        try:
            cal_step   = pipeline.named_steps["clf"]
            importances = np.mean(
                [cc.estimator.feature_importances_
                 for cc in cal_step.calibrated_classifiers_],
                axis=0,
            )
            top_features = sorted(
                zip(FEATURE_NAMES, importances),
                key=lambda x: x[1], reverse=True,
            )
        except Exception:
            top_features = []

        if progress_cb:
            progress_cb("Saving model…")

        import joblib
        joblib.dump(pipeline, FB_CLF_PATH)

        meta = {
            "trained_at":       datetime.now().isoformat(),
            "total_trades":     int(len(X)),
            "train_samples":    int(len(X_train)),
            "test_samples":     int(len(X_test)),
            "win_rate_pct":     round(pos_rate, 2),
            "accuracy":         round(report.get("accuracy", 0), 4),
            "precision_win":    round(report.get("1", {}).get("precision", 0), 4),
            "recall_win":       round(report.get("1", {}).get("recall", 0), 4),
            "f1_win":           round(report.get("1", {}).get("f1-score", 0), 4),
            "auc_roc":          auc,
            "top_features":     [
                {"name": n, "importance": round(float(v), 4)}
                for n, v in top_features
            ],
        }

        with open(FB_META_PATH, "w") as fp:
            json.dump(meta, fp, indent=2)

        self.clf  = pipeline
        self.meta = meta

        logger.info(
            f"[TradeFeedback] Training complete. "
            f"AUC={auc} Accuracy={meta['accuracy']} Trades={len(X)}"
        )
        return meta

    # ─────────────────────────────────────────────────────────────────────
    # Inference
    # ─────────────────────────────────────────────────────────────────────

    def load(self) -> bool:
        """Load saved model from disk. Returns True on success."""
        try:
            import joblib
            if not os.path.exists(FB_CLF_PATH):
                return False
            self.clf = joblib.load(FB_CLF_PATH)
            if os.path.exists(FB_META_PATH):
                with open(FB_META_PATH) as fp:
                    self.meta = json.load(fp)
            return True
        except Exception as e:
            logger.error(f"[TradeFeedback] Load error: {e}")
            return False

    def predict(
        self,
        entry_price: float,
        stop_loss: float,
        target: float,
        buy_probability: float = 0.0,
        expected_return_pct: float = 0.0,
        strategy_tags: str = "",
        trade_type: str = "paper",
        day_of_week: Optional[int] = None,
    ) -> dict:
        """
        Predict the probability this trade will succeed based on historical outcomes.

        Returns:
            {
              feedback_probability: float | None,
              feedback_signal:      "boost" | "neutral" | "penalise" | "unavailable",
              adjustment:           float  (quality score delta, -10 to +10)
            }
        """
        if self.clf is None:
            if not self.load():
                return {
                    "feedback_probability": None,
                    "feedback_signal":      "unavailable",
                    "adjustment":           0.0,
                    "note":                 "Trade feedback model not trained yet.",
                }

        try:
            row = self._build_single_row(
                entry_price=entry_price,
                stop_loss=stop_loss,
                target=target,
                buy_probability=buy_probability,
                expected_return_pct=expected_return_pct,
                strategy_tags=strategy_tags,
                trade_type=trade_type,
                day_of_week=day_of_week,
            )
            if row is None:
                return {
                    "feedback_probability": None,
                    "feedback_signal":      "error",
                    "adjustment":           0.0,
                }

            proba = float(self.clf.predict_proba(np.array([row]))[0][1])

            # Map probability → quality score adjustment (-10 to +10 pts)
            # Centre: 0.5 → 0 adjustment
            # 0.8 → +6,  0.2 → -6
            adjustment = round(((proba - 0.5) / 0.5) * MAX_ADJUSTMENT, 2)
            adjustment = max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, adjustment))

            if proba >= 0.65:
                signal = "boost"
            elif proba <= 0.40:
                signal = "penalise"
            else:
                signal = "neutral"

            return {
                "feedback_probability": round(proba, 4),
                "feedback_signal":      signal,
                "adjustment":           adjustment,
            }

        except Exception as e:
            logger.error(f"[TradeFeedback] predict error: {e}")
            return {
                "feedback_probability": None,
                "feedback_signal":      "error",
                "adjustment":           0.0,
            }

    def is_trained(self) -> bool:
        return os.path.exists(FB_CLF_PATH)

    def get_meta(self) -> dict:
        if self.meta:
            return self.meta
        if os.path.exists(FB_META_PATH):
            with open(FB_META_PATH) as fp:
                self.meta = json.load(fp)
            return self.meta
        return {}

    def get_trade_stats(self) -> dict:
        """Return counts of win/loss/open trades from trade_journal."""
        try:
            conn = get_conn()
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT status, COUNT(*) FROM trade_journal GROUP BY status
                """)
                rows = cur.fetchall()
            conn.close()
            counts = {r[0]: int(r[1]) for r in rows}
            closed = counts.get("win", 0) + counts.get("loss", 0) + counts.get("stopped", 0)
            return {
                "win":     counts.get("win", 0),
                "loss":    counts.get("loss", 0),
                "stopped": counts.get("stopped", 0),
                "open":    counts.get("open", 0),
                "closed":  closed,
                "ready_to_train": closed >= MIN_TRADES,
                "min_required":   MIN_TRADES,
            }
        except Exception as e:
            logger.error(f"[TradeFeedback] get_trade_stats error: {e}")
            return {"error": str(e)}

    # ─────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────

    def _load_closed_trades(self) -> Optional[pd.DataFrame]:
        """Fetch closed trades from trade_journal."""
        try:
            conn = get_conn()
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        entry_price, stop_loss, target,
                        buy_probability, expected_return_pct,
                        strategy_tags, trade_type,
                        entry_date, status
                    FROM trade_journal
                    WHERE status IN ('win', 'loss', 'stopped')
                      AND entry_price IS NOT NULL
                      AND stop_loss  IS NOT NULL
                """)
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            conn.close()

            return pd.DataFrame(rows) if rows else None

        except Exception as e:
            logger.error(f"[TradeFeedback] _load_closed_trades error: {e}")
            return None

    def _build_feature_matrix(self, df: pd.DataFrame):
        """Build X (features) and y (labels) from closed trades DataFrame."""
        records, labels = [], []

        for _, row in df.iterrows():
            try:
                entry  = float(row["entry_price"])
                sl     = float(row["stop_loss"])
                target = float(row["target"]) if row.get("target") else entry

                label = 1 if row["status"] == "win" else 0

                dow = None
                if row.get("entry_date"):
                    try:
                        dow = pd.to_datetime(row["entry_date"]).dayofweek
                    except Exception:
                        pass

                feat = self._build_single_row(
                    entry_price=entry,
                    stop_loss=sl,
                    target=target,
                    buy_probability=float(row.get("buy_probability") or 0.0),
                    expected_return_pct=float(row.get("expected_return_pct") or 0.0),
                    strategy_tags=str(row.get("strategy_tags") or ""),
                    trade_type=str(row.get("trade_type") or "paper"),
                    day_of_week=dow,
                )
                if feat is None:
                    continue

                records.append(feat)
                labels.append(label)
            except Exception:
                continue

        if not records:
            return None, None

        return np.array(records, dtype=np.float32), np.array(labels, dtype=np.int32)

    def _build_single_row(
        self,
        entry_price: float,
        stop_loss: float,
        target: float,
        buy_probability: float,
        expected_return_pct: float,
        strategy_tags: str,
        trade_type: str,
        day_of_week: Optional[int],
    ) -> Optional[list]:
        """Compute feature vector for a single trade."""
        try:
            if entry_price <= 0 or stop_loss <= 0:
                return None

            risk_pct   = abs(entry_price - stop_loss) / entry_price
            reward_pct = abs(target - entry_price) / entry_price if target and entry_price > 0 else 0.0
            rr_ratio   = reward_pct / risk_pct if risk_pct > 0 else 0.0

            strat_count = (
                len([s for s in strategy_tags.split(",") if s.strip()])
                if strategy_tags else 1
            )

            return [
                min(risk_pct, 0.5),                       # risk_pct      (capped 50%)
                min(reward_pct, 1.0),                     # reward_pct    (capped 100%)
                min(rr_ratio, 10.0),                      # rr_ratio      (capped 10x)
                float(buy_probability or 0.0),            # buy_probability
                float(expected_return_pct or 0.0),        # expected_return_pct
                float(day_of_week if day_of_week is not None else 2) / 4.0,   # day_of_week (normalised)
                min(strat_count, 16) / 16.0,              # strategy_count (normalised)
                1.0 if trade_type == "live" else 0.0,     # trade_type_enc
            ]
        except Exception:
            return None


# ── Module-level singleton ────────────────────────────────────────────────

_feedback_model: Optional[TradeFeedbackModel] = None


def get_feedback_model() -> TradeFeedbackModel:
    """Return the shared singleton, loading from disk on first call."""
    global _feedback_model
    if _feedback_model is None:
        _feedback_model = TradeFeedbackModel()
        _feedback_model.load()
    return _feedback_model


def reload_feedback_model() -> TradeFeedbackModel:
    """Force a fresh load from disk (call after retraining)."""
    global _feedback_model
    m = TradeFeedbackModel()
    m.load()
    _feedback_model = m
    return _feedback_model
