"""
Meta Filter Model — ml/meta_filter_model.py

Answers: "Even if this trade looks good on the ML model, should we take it?"

The price-direction model (ml/model.py) labels a bar SUCCESS when the forward
5-day return exceeds 2 %.  That ignores trade mechanics: stop losses can be hit
intra-day before the target is ever reached even when the close is up 2 %+.

This model labels bars using a realistic trade simulation:
  entry     = next-bar open  (no look-ahead)
  stop_loss = entry − ATR(14) × ATR_MULT   (default 1.5)
  target    = entry + risk × RR_MULT        (default 2.0  → 2:1 R:R)

  For bars 2 … FORWARD_DAYS after the signal bar:
    • Both hit on the same day → stop wins (conservative)
    • Target hit first        → SUCCESS (label = 1)
    • Stop hit first          → FAILURE (label = 0)
    • Neither within window   → FAILURE (label = 0)

Features:
  Reuses the same 26 FEATURE_NAMES from ml/features.py — no new engineering.

Model:
  Pipeline: RobustScaler → RandomForestClassifier → CalibratedClassifierCV(isotonic)
  Walk-forward temporal split identical to ml/model.py (oldest 80% train, newest 20% test).

Output:
  { "take_trade_probability": 0.63, "decision": "TAKE" | "AVOID" }

Files saved:
  ml/models/meta_filter.joblib
  ml/models/meta_filter_meta.json
"""

import os
import sys
import json
import logging
import warnings
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

warnings.filterwarnings(
    "ignore",
    message=".*sklearn.utils.parallel.delayed.*",
    category=UserWarning,
    module="sklearn",
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

from config.settings import TABLE_NAME, BASE_DIR
from db.connection import get_engine
from ml.features import compute_features, FEATURE_NAMES

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────
MODEL_DIR    = os.path.join(str(BASE_DIR), "ml", "models")
MF_CLF_PATH  = os.path.join(MODEL_DIR, "meta_filter.joblib")
MF_META_PATH = os.path.join(MODEL_DIR, "meta_filter_meta.json")

# ── Hyper-parameters ────────────────────────────────────────────────────
MIN_ROWS      = 260    # minimum bars per symbol to include in training
WF_TEST_RATIO = 0.20   # last 20 % of each symbol's data → test set

# ── Trade simulation parameters ─────────────────────────────────────────
FORWARD_DAYS  = 5      # max bars to wait for an exit
ATR_MULT      = 1.5    # stop  = entry − ATR × ATR_MULT
RR_MULT       = 2.0    # target = entry + risk × RR_MULT  (2:1 R:R)

# ── Decision threshold ───────────────────────────────────────────────────
TAKE_THRESHOLD = 0.50  # probability must exceed this to TAKE the trade


# ── Label simulation ─────────────────────────────────────────────────────

def _simulate_labels(
    df: pd.DataFrame,
    atr_values: np.ndarray,
    atr_mult: float = ATR_MULT,
    rr_mult: float = RR_MULT,
    forward_days: int = FORWARD_DAYS,
) -> np.ndarray:
    """
    Simulate trade outcomes bar-by-bar and return an integer label array.

    Args:
        df:          OHLCV DataFrame (sorted oldest → newest).
        atr_values:  ATR(14) series aligned with df index.
        atr_mult:    Stop-loss distance multiplier.
        rr_mult:     Target multiplier (multiples of risk).
        forward_days: Max hold window to check for exit.

    Returns:
        numpy array of int32 with same length as df.
        Bars where simulation is impossible (insufficient forward data,
        invalid ATR, etc.) get label = -1 and must be filtered out.
    """
    n      = len(df)
    opens  = df["open"].values.astype(float)
    highs  = df["high"].values.astype(float)
    lows   = df["low"].values.astype(float)
    labels = np.full(n, -1, dtype=np.int32)

    # Signal bar i → entry at bar i+1 → check bars i+2 … i+forward_days+1
    # Require at least forward_days+2 bars beyond the signal bar.
    for i in range(n - forward_days - 2):
        atr = float(atr_values[i])
        if atr <= 0 or np.isnan(atr):
            continue

        entry = opens[i + 1]
        if entry <= 0:
            continue

        risk      = atr * atr_mult
        stop_loss = entry - risk
        target    = entry + risk * rr_mult

        if stop_loss >= entry or target <= entry:
            continue

        outcome = 0   # default: failure (no exit within window)

        for d in range(2, forward_days + 2):
            j = i + d
            if j >= n:
                break

            hit_stop   = lows[j]  <= stop_loss
            hit_target = highs[j] >= target

            if hit_stop and hit_target:
                # Both hit on same daily candle — conservative: stop wins
                outcome = 0
                break
            elif hit_target:
                outcome = 1
                break
            elif hit_stop:
                outcome = 0
                break
            # else: still in trade — continue

        labels[i] = outcome

    return labels


# ── MetaFilterModel ──────────────────────────────────────────────────────

class MetaFilterModel:
    """
    Second-stage gate on top of the ML price model.

    Uses the same 26 features but a different label (realistic trade
    simulation vs. simple forward return) to learn which setups tend to
    hit target before stop — the question the trader actually cares about.
    """

    def __init__(self):
        self.clf  = None
        self.meta: dict = {}

    # ─────────────────────────────────────────────────────────────────────
    # Training
    # ─────────────────────────────────────────────────────────────────────

    def train(
        self,
        atr_mult: float = ATR_MULT,
        rr_mult: float = RR_MULT,
        forward_days: int = FORWARD_DAYS,
        progress_cb=None,
    ) -> dict:
        """
        Train the meta filter on all symbols in the DB.

        Args:
            atr_mult:    Stop-loss ATR multiplier (default 1.5).
            rr_mult:     Risk:reward multiplier   (default 2.0 → 2:1 R:R).
            forward_days: Days to wait for exit   (default 5).
            progress_cb: Optional callable(step: str) for progress updates.

        Returns:
            Metrics dict or {"error": str}.
        """
        try:
            import joblib
            from sklearn.ensemble import RandomForestClassifier
            from sklearn.calibration import CalibratedClassifierCV
            from sklearn.preprocessing import RobustScaler
            from sklearn.pipeline import Pipeline
            from sklearn.metrics import classification_report, roc_auc_score
        except ImportError as e:
            return {"error": f"Missing dependency: {e}. Run: pip install scikit-learn joblib"}

        os.makedirs(MODEL_DIR, exist_ok=True)

        if progress_cb:
            progress_cb("Loading symbols from DB…")

        try:
            engine  = get_engine()
            symbols = pd.read_sql(
                f"SELECT DISTINCT symbol FROM {TABLE_NAME}", engine
            )["symbol"].tolist()
        except Exception as e:
            return {"error": f"DB error: {e}"}

        total     = len(symbols)
        processed = 0

        X_train_all, y_train_all = [], []
        X_test_all,  y_test_all  = [], []

        for sym in symbols:
            try:
                engine = get_engine()
                df     = pd.read_sql(
                    f"SELECT trade_date, open, high, low, close, volume "
                    f"FROM {TABLE_NAME} WHERE symbol = %(sym)s ORDER BY trade_date ASC",
                    engine, params={"sym": sym},
                )

                if len(df) < MIN_ROWS + forward_days + 2:
                    continue

                # Compute 26 features
                feats = compute_features(df)

                # Derive ATR from the normalised feature: atr_norm = ATR/close
                # so ATR = atr_norm × close
                atr_vals = (feats["atr_norm"] * df["close"]).values

                # Simulate labels
                raw_labels = _simulate_labels(
                    df, atr_vals, atr_mult, rr_mult, forward_days
                )

                # Build combined frame — only rows where label is valid (0 or 1)
                feat_df = feats[FEATURE_NAMES].copy()
                feat_df["_y"] = raw_labels
                feat_df = feat_df[feat_df["_y"] >= 0].dropna()

                if len(feat_df) < 80:
                    continue

                # ── Walk-forward temporal split ───────────────────────────
                cut        = int(len(feat_df) * (1 - WF_TEST_RATIO))
                train_part = feat_df.iloc[:cut]
                test_part  = feat_df.iloc[cut:]

                X_train_all.append(train_part[FEATURE_NAMES].values)
                y_train_all.append(train_part["_y"].values)
                X_test_all.append(test_part[FEATURE_NAMES].values)
                y_test_all.append(test_part["_y"].values)

                processed += 1
                if progress_cb:
                    progress_cb(f"Processed {processed}/{total}: {sym}")

            except Exception as e:
                logger.error(f"[MetaFilter] Skipping {sym}: {e}")

        if not X_train_all:
            return {"error": "No usable training data — run a data backfill first."}

        X_train = np.vstack(X_train_all)
        y_train = np.concatenate(y_train_all).astype(int)
        X_test  = np.vstack(X_test_all)
        y_test  = np.concatenate(y_test_all).astype(int)

        success_rate = y_train.mean() * 100
        logger.info(
            f"[MetaFilter] Walk-forward split: {len(X_train):,} train | "
            f"{len(X_test):,} test | Success rate: {success_rate:.1f}%"
        )

        if progress_cb:
            progress_cb(
                f"Training RandomForest on {len(X_train):,} samples "
                f"({success_rate:.1f}% success rate)…"
            )

        pipeline = Pipeline([
            ("scaler", RobustScaler()),
            ("clf", CalibratedClassifierCV(
                RandomForestClassifier(
                    n_estimators=100,
                    max_depth=8,
                    min_samples_leaf=12,
                    max_features="sqrt",
                    class_weight="balanced",
                    random_state=42,
                    n_jobs=2,
                ),
                method="isotonic",
                cv=3,
            )),
        ])
        pipeline.fit(X_train, y_train)

        # ── Evaluate ────────────────────────────────────────────────────
        y_proba = pipeline.predict_proba(X_test)[:, 1]
        y_pred  = (y_proba >= TAKE_THRESHOLD).astype(int)
        report  = classification_report(y_test, y_pred, output_dict=True, zero_division=0)

        try:
            auc = round(roc_auc_score(y_test, y_proba), 4)
        except Exception:
            auc = 0.0

        # ── Feature importances ─────────────────────────────────────────
        try:
            cal_step    = pipeline.named_steps["clf"]
            importances = np.mean(
                [cc.estimator.feature_importances_
                 for cc in cal_step.calibrated_classifiers_],
                axis=0,
            )
            top_features = sorted(
                zip(FEATURE_NAMES, importances),
                key=lambda x: x[1], reverse=True,
            )[:10]
        except Exception:
            top_features = []

        if progress_cb:
            progress_cb("Saving meta filter model…")

        import joblib
        joblib.dump(pipeline, MF_CLF_PATH)

        meta = {
            "trained_at":        datetime.now().isoformat(),
            "validation_method": "walk_forward_temporal",
            "wf_test_ratio_pct": WF_TEST_RATIO * 100,
            "symbols_used":      processed,
            "train_samples":     int(len(X_train)),
            "test_samples":      int(len(X_test)),
            "success_rate_pct":  round(success_rate, 2),
            # Simulation config
            "atr_mult":          atr_mult,
            "rr_mult":           rr_mult,
            "forward_days":      forward_days,
            "take_threshold":    TAKE_THRESHOLD,
            # Classifier metrics
            "accuracy":          round(report.get("accuracy", 0), 4),
            "precision_take":    round(report.get("1", {}).get("precision", 0), 4),
            "recall_take":       round(report.get("1", {}).get("recall", 0), 4),
            "f1_take":           round(report.get("1", {}).get("f1-score", 0), 4),
            "auc_roc":           auc,
            "top_features":      [
                {"name": n, "importance": round(float(v), 4)}
                for n, v in top_features
            ],
        }

        with open(MF_META_PATH, "w") as fp:
            json.dump(meta, fp, indent=2)

        self.clf  = pipeline
        self.meta = meta

        logger.info(
            f"[MetaFilter] Training complete. AUC={auc} "
            f"Accuracy={meta['accuracy']} Symbols={processed}"
        )
        return meta

    # ─────────────────────────────────────────────────────────────────────
    # Inference
    # ─────────────────────────────────────────────────────────────────────

    def load(self) -> bool:
        """Load saved model from disk. Returns True on success."""
        try:
            import joblib
            if not os.path.exists(MF_CLF_PATH):
                return False
            self.clf = joblib.load(MF_CLF_PATH)
            if os.path.exists(MF_META_PATH):
                with open(MF_META_PATH) as fp:
                    self.meta = json.load(fp)
            return True
        except Exception as e:
            logger.error(f"[MetaFilter] Load error: {e}")
            return False

    def predict_symbol(self, df: pd.DataFrame, symbol: str) -> dict:
        """
        Predict whether to TAKE or AVOID a trade on the latest bar.

        Args:
            df:     OHLCV DataFrame sorted oldest → newest (≥260 bars).
            symbol: Symbol name (for logging).

        Returns:
            {
              "take_trade_probability": float,
              "decision":              "TAKE" | "AVOID",
              "symbol":                str,
            }
            On error: { "symbol": str, "error": str }
        """
        if self.clf is None:
            if not self.load():
                return {
                    "symbol":                symbol,
                    "take_trade_probability": None,
                    "decision":              "UNAVAILABLE",
                    "note":                  "Meta filter not trained — POST /api/ml/meta/train",
                }

        try:
            feats  = compute_features(df)
            latest = feats.iloc[-1:][FEATURE_NAMES].values

            if np.any(np.isnan(latest)):
                return {"symbol": symbol, "error": "Insufficient history for meta filter"}

            proba    = float(self.clf.predict_proba(latest)[0][1])
            decision = "TAKE" if proba >= TAKE_THRESHOLD else "AVOID"

            return {
                "symbol":                 symbol,
                "take_trade_probability": round(proba, 4),
                "decision":               decision,
            }

        except Exception as e:
            logger.error(f"[MetaFilter] predict_symbol error for {symbol}: {e}")
            return {"symbol": symbol, "error": str(e)}

    def is_trained(self) -> bool:
        return os.path.exists(MF_CLF_PATH)

    def get_meta(self) -> dict:
        if self.meta:
            return self.meta
        if os.path.exists(MF_META_PATH):
            with open(MF_META_PATH) as fp:
                self.meta = json.load(fp)
            return self.meta
        return {}


# ── Module-level singleton ────────────────────────────────────────────────

_meta_filter: Optional[MetaFilterModel] = None


def get_meta_filter() -> MetaFilterModel:
    """Return the shared singleton, loading from disk on first call."""
    global _meta_filter
    if _meta_filter is None:
        _meta_filter = MetaFilterModel()
        _meta_filter.load()
    return _meta_filter


def reload_meta_filter() -> MetaFilterModel:
    """Force a fresh load from disk (call after retraining)."""
    global _meta_filter
    m = MetaFilterModel()
    m.load()
    _meta_filter = m
    return _meta_filter
