"""
ML price prediction — four improvements over baseline:

  1. Walk-forward validation  — temporal train/test split per symbol
                                (train on oldest 80%, test on most-recent 20%)
  2. Price target prediction  — RandomForestRegressor predicts actual 5-day return %
  3. Calibrated certainty     — CalibratedClassifierCV (isotonic) so probabilities
                                match real hit rates; reliability buckets reported
  4. Market regime overlay    — regime() adjusts BUY threshold dynamically
                                (more cautious in Bear, more aggressive in Bull)

Model files saved to ml/models/:
  price_predictor.joblib   — calibrated classifier pipeline
  price_regressor.joblib   — return-% regressor pipeline
  model_meta.json          — training metrics + calibration data
"""

import os
import sys
import json
import logging

import numpy as np
import pandas as pd
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

from config.settings import TABLE_NAME, BASE_DIR
from db.connection import get_conn, get_engine
from ml.features import compute_features, FEATURE_NAMES

logger = logging.getLogger(__name__)

# ── Paths ───────────────────────────────────────────────────────────────
MODEL_DIR       = os.path.join(str(BASE_DIR), "ml", "models")
CLF_PATH        = os.path.join(MODEL_DIR, "price_predictor.joblib")
REG_PATH        = os.path.join(MODEL_DIR, "price_regressor.joblib")
META_PATH       = os.path.join(MODEL_DIR, "model_meta.json")

# ── Hyper-parameters ────────────────────────────────────────────────────
FORWARD_DAYS    = 5      # predict N-bar forward return
TARGET_RETURN   = 0.02   # 2% gain = positive class
MIN_ROWS        = 260    # minimum bars per symbol for training
WF_TEST_RATIO   = 0.20   # last 20% of each symbol's data → test set

# ── Dynamic thresholds (adjusted by regime) ─────────────────────────────
THRESHOLDS = {
    "Bull":    (0.55, 0.42),   # (buy_min, avoid_max) — easier in bull market
    "Neutral": (0.60, 0.40),
    "Bear":    (0.65, 0.38),   # stricter in bear market
}


class MLPredictor:
    """Train and run the ML price prediction suite."""

    def __init__(self):
        self.clf   = None   # calibrated classifier
        self.reg   = None   # return regressor
        self.meta: dict = {}

    # ────────────────────────────────────────────────────────────────────
    # Training
    # ────────────────────────────────────────────────────────────────────

    def train(self, progress_cb=None) -> dict:
        """
        Train classifier + regressor using walk-forward temporal splits.

        Returns:
            dict with training metrics or {"error": str}.
        """
        try:
            import joblib
            from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
            from sklearn.calibration import CalibratedClassifierCV
            from sklearn.metrics import (
                classification_report, roc_auc_score,
                mean_absolute_error, r2_score,
            )
            from sklearn.preprocessing import RobustScaler
            from sklearn.pipeline import Pipeline
        except ImportError as e:
            return {"error": f"Missing dependency: {e}. Run: pip install scikit-learn joblib"}

        os.makedirs(MODEL_DIR, exist_ok=True)
        logger.info("ML Training [walk-forward] starting …")

        # ── Collect temporal splits ─────────────────────────────────────
        X_train_all, y_clf_train_all, y_reg_train_all = [], [], []
        X_test_all,  y_clf_test_all,  y_reg_test_all  = [], [], []

        try:
            engine  = get_engine()
            symbols = pd.read_sql(
                f"SELECT DISTINCT symbol FROM {TABLE_NAME}", engine
            )["symbol"].tolist()
        except Exception as e:
            return {"error": f"DB error: {e}"}

        total     = len(symbols)
        processed = 0

        for sym in symbols:
            try:
                engine = get_engine()
                df     = pd.read_sql(
                    f"SELECT trade_date, open, high, low, close, volume "
                    f"FROM {TABLE_NAME} WHERE symbol = %(sym)s ORDER BY trade_date ASC",
                    engine, params={"sym": sym},
                )

                if len(df) < MIN_ROWS + FORWARD_DAYS:
                    continue

                feats      = compute_features(df)
                fwd_return = df["close"].shift(-FORWARD_DAYS) / df["close"] - 1
                y_clf      = (fwd_return >= TARGET_RETURN).astype(int)
                y_reg      = fwd_return * 100   # as % for regression

                combined = feats.copy()
                combined["_yc"] = y_clf
                combined["_yr"] = y_reg
                combined = combined.dropna().iloc[:-FORWARD_DAYS]

                if len(combined) < 80:
                    continue

                # ── TEMPORAL SPLIT (walk-forward) ───────────────────────
                cut = int(len(combined) * (1 - WF_TEST_RATIO))
                train_part = combined.iloc[:cut]
                test_part  = combined.iloc[cut:]

                X_train_all.append(train_part[FEATURE_NAMES].values)
                y_clf_train_all.append(train_part["_yc"].values)
                y_reg_train_all.append(train_part["_yr"].values)

                X_test_all.append(test_part[FEATURE_NAMES].values)
                y_clf_test_all.append(test_part["_yc"].values)
                y_reg_test_all.append(test_part["_yr"].values)

                processed += 1
                if progress_cb:
                    progress_cb(processed, total, sym)

            except Exception as e:
                logger.error(f"Training: skipping {sym} — {e}")

        if not X_train_all:
            return {"error": "No usable training data — run a backfill first."}

        X_train   = np.vstack(X_train_all)
        y_clf_tr  = np.concatenate(y_clf_train_all)
        y_reg_tr  = np.concatenate(y_reg_train_all)
        X_test    = np.vstack(X_test_all)
        y_clf_te  = np.concatenate(y_clf_test_all)
        y_reg_te  = np.concatenate(y_reg_test_all)

        pos_rate = y_clf_tr.mean() * 100
        logger.info(
            f"Walk-forward split: {len(X_train):,} train | {len(X_test):,} test "
            f"(most-recent 20% per symbol) | Positive rate: {pos_rate:.1f}%"
        )

        # ── 1. Build calibrated classifier pipeline ─────────────────────
        # CalibratedClassifierCV wraps the RF *before* fitting and uses
        # internal cv=5 folds — compatible with scikit-learn >= 1.4.
        calibrated_clf = Pipeline([
            ("scaler", RobustScaler()),
            ("clf", CalibratedClassifierCV(
                RandomForestClassifier(
                    n_estimators=100,   # reduced from 300 — saves ~3x memory
                    max_depth=8,        # reduced from 10
                    min_samples_leaf=12,
                    max_features="sqrt",
                    class_weight="balanced",
                    random_state=42,
                    n_jobs=2,           # limit cores to avoid OOM
                ),
                method="isotonic",
                cv=3,                   # reduced from 5 — saves memory
            )),
        ])
        calibrated_clf.fit(X_train, y_clf_tr)

        # ── 2. Evaluate on held-out walk-forward test set ──────────────
        y_proba_test  = calibrated_clf.predict_proba(X_test)[:, 1]
        y_pred_test   = (y_proba_test >= 0.60).astype(int)
        clf_report    = classification_report(y_clf_te, y_pred_test, output_dict=True)

        try:
            auc = round(roc_auc_score(y_clf_te, y_proba_test), 4)
        except Exception:
            auc = 0.0

        # ── 4. Calibration reliability buckets ─────────────────────────
        reliability = _compute_reliability(y_proba_test, y_clf_te)

        # ── 5. Train return regressor ───────────────────────────────────
        reg_pipeline = Pipeline([
            ("scaler", RobustScaler()),
            ("reg", RandomForestRegressor(
                n_estimators=80,    # reduced from 200
                max_depth=6,        # reduced from 8
                min_samples_leaf=12,
                max_features="sqrt",
                random_state=42,
                n_jobs=2,
            )),
        ])
        reg_pipeline.fit(X_train, y_reg_tr)

        y_reg_pred = reg_pipeline.predict(X_test)
        mae  = round(mean_absolute_error(y_reg_te, y_reg_pred), 3)
        r2   = round(r2_score(y_reg_te, y_reg_pred), 4)

        # ── 6. Feature importances ─────────────────────────────────────
        # calibrated_clf is Pipeline(scaler, CalibratedClassifierCV(RF))
        # Average importances across the calibrated estimators' folds.
        try:
            cal_step = calibrated_clf.named_steps["clf"]   # CalibratedClassifierCV
            # Each calibrated_classifiers_ item is a _CalibratedClassifier;
            # the actual RF lives at .estimator inside each one.
            importances = np.mean(
                [cc.estimator.feature_importances_
                 for cc in cal_step.calibrated_classifiers_],
                axis=0,
            )
        except Exception as e:
            logger.warning(f"Feature importance extraction failed: {e}")
            importances = np.zeros(len(FEATURE_NAMES))
        top_features = sorted(
            zip(FEATURE_NAMES, importances),
            key=lambda x: x[1], reverse=True,
        )[:10]

        # ── 7. Save models ─────────────────────────────────────────────
        import joblib
        joblib.dump(calibrated_clf, CLF_PATH)
        joblib.dump(reg_pipeline,   REG_PATH)

        meta = {
            "trained_at":          datetime.now().isoformat(),
            "validation_method":   "walk_forward_temporal",
            "wf_test_ratio_pct":   WF_TEST_RATIO * 100,
            "symbols_used":        processed,
            "train_samples":       int(len(X_train)),
            "test_samples":        int(len(X_test)),
            "positive_rate_pct":   round(pos_rate, 2),
            # Classifier metrics (on held-out temporal test set)
            "accuracy":            round(clf_report["accuracy"], 4),
            "precision_buy":       round(clf_report.get("1", {}).get("precision", 0), 4),
            "recall_buy":          round(clf_report.get("1", {}).get("recall", 0), 4),
            "f1_buy":              round(clf_report.get("1", {}).get("f1-score", 0), 4),
            "auc_roc":             auc,
            # Calibration
            "reliability_buckets": reliability,
            # Regressor metrics
            "regressor_mae_pct":   mae,
            "regressor_r2":        r2,
            # Config
            "forward_days":        FORWARD_DAYS,
            "target_return_pct":   TARGET_RETURN * 100,
            "top_features":        [
                {"name": n, "importance": round(float(v), 4)}
                for n, v in top_features
            ],
        }

        with open(META_PATH, "w") as fp:
            json.dump(meta, fp, indent=2)

        self.clf  = calibrated_clf
        self.reg  = reg_pipeline
        self.meta = meta

        logger.info(
            f"Training complete. AUC={auc}  Accuracy={meta['accuracy']}  "
            f"Regressor MAE={mae}%  R²={r2}"
        )
        return meta

    # ────────────────────────────────────────────────────────────────────
    # Inference
    # ────────────────────────────────────────────────────────────────────

    def load(self) -> bool:
        """Load saved models from disk. Returns True on success."""
        try:
            import joblib
            if not os.path.exists(CLF_PATH):
                return False
            self.clf = joblib.load(CLF_PATH)
            if os.path.exists(REG_PATH):
                self.reg = joblib.load(REG_PATH)
            if os.path.exists(META_PATH):
                with open(META_PATH) as fp:
                    self.meta = json.load(fp)
            return True
        except Exception as e:
            logger.error(f"Model load error: {e}")
            return False

    def predict_symbol(self, df: pd.DataFrame, symbol: str,
                       regime: str = "Neutral") -> dict:
        """
        Predict buy probability + price target for the latest bar.

        Args:
            df:     OHLCV DataFrame, sorted oldest → newest.
            symbol: Symbol name.
            regime: Market regime ("Bull" | "Neutral" | "Bear")
                    Adjusts the BUY/AVOID thresholds dynamically.

        Returns:
            dict with: symbol, buy_probability, signal, confidence,
                        price, expected_return_pct, price_target,
                        regime_adjusted (bool)
        """
        if self.clf is None:
            if not self.load():
                return {"symbol": symbol,
                        "error": "Model not trained — POST /api/ml/train first"}

        try:
            feats  = compute_features(df)
            latest = feats.iloc[-1:][FEATURE_NAMES].values

            if np.any(np.isnan(latest)):
                return {"symbol": symbol,
                        "error": "Insufficient history for all features"}

            # ── Calibrated buy probability ─────────────────────────────
            proba = float(self.clf.predict_proba(latest)[0][1])

            # ── Regime-adjusted thresholds ─────────────────────────────
            buy_thresh, avoid_thresh = THRESHOLDS.get(
                regime, THRESHOLDS["Neutral"]
            )

            if proba >= buy_thresh:
                signal = "BUY"
            elif proba <= avoid_thresh:
                signal = "AVOID"
            else:
                signal = "NEUTRAL"

            # ── Predicted return % (regression) ───────────────────────
            exp_return_pct = None
            price_target   = None
            if self.reg is not None:
                exp_return_pct = round(float(self.reg.predict(latest)[0]), 2)
                last_price     = float(df["close"].iloc[-1])
                price_target   = round(last_price * (1 + exp_return_pct / 100), 2)

            return {
                "symbol":            symbol,
                "buy_probability":   round(proba, 4),
                "signal":            signal,
                "confidence":        _confidence_label(proba),
                "price":             round(float(df["close"].iloc[-1]), 2),
                "expected_return_pct": exp_return_pct,
                "price_target":      price_target,
                "regime":            regime,
                "buy_threshold_used": buy_thresh,
            }

        except Exception as e:
            logger.error(f"predict_symbol error for {symbol}: {e}")
            return {"symbol": symbol, "error": str(e)}

    def predict_all(self, regime: str = "Neutral") -> list:
        """
        Run predictions for every symbol, regime-adjusted thresholds.

        Returns list sorted by buy_probability descending.
        """
        if self.clf is None:
            if not self.load():
                return []

        results = []
        try:
            engine  = get_engine()
            symbols = pd.read_sql(
                f"SELECT DISTINCT symbol FROM {TABLE_NAME}", engine
            )["symbol"].tolist()

            for sym in symbols:
                df = pd.read_sql(
                    f"SELECT trade_date, open, high, low, close, volume "
                    f"FROM {TABLE_NAME} WHERE symbol = %(sym)s ORDER BY trade_date ASC",
                    engine, params={"sym": sym},
                )
                if len(df) >= MIN_ROWS:
                    r = self.predict_symbol(df, sym, regime=regime)
                    if "error" not in r:
                        results.append(r)

        except Exception as e:
            logger.error(f"predict_all error: {e}")

        results.sort(key=lambda r: r.get("buy_probability", 0), reverse=True)
        return results

    # ────────────────────────────────────────────────────────────────────
    # Utilities
    # ────────────────────────────────────────────────────────────────────

    def is_trained(self) -> bool:
        return os.path.exists(CLF_PATH)

    def get_meta(self) -> dict:
        if self.meta:
            return self.meta
        if os.path.exists(META_PATH):
            with open(META_PATH) as fp:
                self.meta = json.load(fp)
            return self.meta
        return {}


# ── Helpers ─────────────────────────────────────────────────────────────

def _confidence_label(proba: float) -> str:
    if proba >= 0.80:  return "Very High"
    if proba >= 0.65:  return "High"
    if proba >= 0.55:  return "Moderate"
    if proba >= 0.42:  return "Low"
    return "Very Low"


def _compute_reliability(y_proba: np.ndarray, y_true: np.ndarray,
                         n_bins: int = 5) -> list:
    """
    Build reliability / calibration buckets.

    Each bucket shows: "when the model said X%, the actual hit rate was Y%"

    Returns:
        List of dicts: {predicted_pct, actual_pct, count}
    """
    bins   = np.linspace(0, 1, n_bins + 1)
    result = []
    for i in range(n_bins):
        lo, hi = bins[i], bins[i + 1]
        mask   = (y_proba >= lo) & (y_proba < hi)
        count  = int(mask.sum())
        if count == 0:
            continue
        actual_rate = float(y_true[mask].mean()) * 100
        mid_pct     = round((lo + hi) / 2 * 100, 0)
        result.append({
            "predicted_pct": mid_pct,
            "actual_pct":    round(actual_rate, 1),
            "count":         count,
        })
    return result
