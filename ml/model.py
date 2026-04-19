"""
ML price prediction — multi-horizon ensemble

Three calibrated classifiers, one shared feature set:

  Horizon  Label threshold  Trade style    File
  ────────────────────────────────────────────────────────────────
  3-day    return ≥ 1.5 %   Short-term     price_model_3d.joblib
  5-day    return ≥ 2.0 %   Swing          price_model_5d.joblib
  10-day   return ≥ 3.0 %   Positional     price_model_10d.joblib

One return regressor trained on 5-day targets for price_target output.

Validation: walk-forward temporal split — train on oldest 80 %,
test on most-recent 20 % of each symbol's history. No data leakage.

Backward compatibility:
  • self.clf  → property alias for self.clf_5d (existing callers unaffected)
  • price_predictor.joblib saved alongside price_model_5d.joblib
  • predict_symbol() returns buy_probability = prob_5d + all existing fields
"""

import os
import sys
import json
import logging
import warnings

import numpy as np
import pandas as pd
from datetime import datetime

# sklearn 1.3+ warns when RandomForest uses joblib.delayed internally instead
# of sklearn.utils.parallel.delayed.  This is a library-internal issue, not
# our code — suppress it so it doesn't flood Docker logs.
warnings.filterwarnings(
    "ignore",
    message=".*sklearn.utils.parallel.delayed.*",
    category=UserWarning,
    module="sklearn",
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

from config.settings import TABLE_NAME, BASE_DIR
from db.connection import get_conn, get_engine
from ml.features import compute_features, FEATURE_NAMES
from ml.prediction_history import get_prediction_history

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────
MODEL_DIR     = os.path.join(str(BASE_DIR), "ml", "models")
CLF_3D_PATH   = os.path.join(MODEL_DIR, "price_model_3d.joblib")
CLF_5D_PATH   = os.path.join(MODEL_DIR, "price_model_5d.joblib")
CLF_10D_PATH  = os.path.join(MODEL_DIR, "price_model_10d.joblib")
REG_PATH      = os.path.join(MODEL_DIR, "price_regressor.joblib")
META_PATH     = os.path.join(MODEL_DIR, "model_meta.json")
# Kept for backward compatibility with any cached file references
CLF_PATH      = os.path.join(MODEL_DIR, "price_predictor.joblib")

# ── Horizon config ───────────────────────────────────────────────────────
# Maps holding period (bars) → minimum return threshold for positive class
HORIZONS = {
    3:  {"target_return": 0.015, "path": CLF_3D_PATH,  "label": "Short-term"},
    5:  {"target_return": 0.020, "path": CLF_5D_PATH,  "label": "Swing"},
    10: {"target_return": 0.030, "path": CLF_10D_PATH, "label": "Positional"},
}

# ── Shared config ────────────────────────────────────────────────────────
MIN_ROWS      = 260    # minimum bars per symbol for training
WF_TEST_RATIO = 0.20   # last 20 % of each symbol's data → test set

# ── Dynamic thresholds (adjusted by regime) ──────────────────────────────
# buy_threshold applies to the primary (5d) classifier for combined/signals
THRESHOLDS = {
    "Bull":    (0.55, 0.42),
    "Neutral": (0.60, 0.40),
    "Bear":    (0.65, 0.38),
}


class MLPredictor:
    """
    Train and run the multi-horizon ML price prediction suite.

    Attributes set after training / loading:
      clf_3d, clf_5d, clf_10d — calibrated sklearn Pipeline objects
      reg                     — 5-day return regressor
      meta                    — training metrics dict
    """

    def __init__(self):
        self.clf_3d  = None
        self.clf_5d  = None
        self.clf_10d = None
        self.reg     = None
        self.meta: dict = {}

    @property
    def clf(self):
        """Backward-compat alias — existing code uses predictor.clf for the 5d model."""
        return self.clf_5d

    # ─────────────────────────────────────────────────────────────────────
    # Training
    # ─────────────────────────────────────────────────────────────────────

    def train(self, progress_cb=None) -> dict:
        """
        Train three classifiers (3d/5d/10d) + one return regressor using
        walk-forward temporal splits over all symbols in the DB.

        Features are computed once per symbol; labels are derived independently
        per horizon to keep the data loop efficient.

        Args:
            progress_cb: optional callable(processed: int, total: int, sym: str).

        Returns:
            dict with per-horizon metrics or {"error": str}.
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
        logger.info("ML Training [multi-horizon walk-forward] starting …")

        # ── Accumulate per-horizon splits ────────────────────────────────
        # {days: {"X_train": [], "y_train": [], "X_test": [], "y_test": []}}
        splits = {h: {"X_tr": [], "y_tr": [], "X_te": [], "y_te": []}
                  for h in HORIZONS}
        # Regressor uses 5-day returns
        reg_splits = {"X_tr": [], "y_tr": [], "X_te": [], "y_te": []}

        try:
            engine  = get_engine()
            symbols = pd.read_sql(
                f"SELECT DISTINCT symbol FROM {TABLE_NAME}", engine
            )["symbol"].tolist()
        except Exception as e:
            return {"error": f"DB error: {e}"}

        total     = len(symbols)
        processed = 0
        max_horizon = max(HORIZONS.keys())   # 10

        for sym in symbols:
            try:
                engine = get_engine()
                df     = pd.read_sql(
                    f"SELECT trade_date, open, high, low, close, volume "
                    f"FROM {TABLE_NAME} WHERE symbol = %(sym)s ORDER BY trade_date ASC",
                    engine, params={"sym": sym},
                )

                if len(df) < MIN_ROWS + max_horizon:
                    continue

                # ── Compute features once ────────────────────────────────
                feats = compute_features(df)

                symbol_ok = False

                for h, cfg in HORIZONS.items():
                    fwd_return = df["close"].shift(-h) / df["close"] - 1
                    y_clf      = (fwd_return >= cfg["target_return"]).astype(int)

                    combined = feats.copy()
                    combined["_yc"] = y_clf
                    if h == 5:
                        combined["_yr"] = fwd_return * 100   # regression label
                    combined = combined.dropna().iloc[:-h]   # trim last h bars (no label)

                    if len(combined) < 80:
                        continue

                    cut        = int(len(combined) * (1 - WF_TEST_RATIO))
                    train_part = combined.iloc[:cut]
                    test_part  = combined.iloc[cut:]

                    splits[h]["X_tr"].append(train_part[FEATURE_NAMES].values)
                    splits[h]["y_tr"].append(train_part["_yc"].values)
                    splits[h]["X_te"].append(test_part[FEATURE_NAMES].values)
                    splits[h]["y_te"].append(test_part["_yc"].values)

                    if h == 5 and "_yr" in train_part.columns:
                        reg_splits["X_tr"].append(train_part[FEATURE_NAMES].values)
                        reg_splits["y_tr"].append(train_part["_yr"].values)
                        reg_splits["X_te"].append(test_part[FEATURE_NAMES].values)
                        reg_splits["y_te"].append(test_part["_yr"].values)
                        symbol_ok = True

                if symbol_ok:
                    processed += 1
                    if progress_cb:
                        progress_cb(processed, total, sym)

            except Exception as e:
                logger.error(f"Training: skipping {sym} — {e}")

        if not any(splits[h]["X_tr"] for h in HORIZONS):
            return {"error": "No usable training data — run a backfill first."}

        logger.info(
            f"Walk-forward split complete: {processed} symbols | "
            f"~{len(splits[5]['X_tr'])} symbol-datasets per horizon"
        )

        # ── Build calibrated classifier factory ──────────────────────────
        def _make_clf_pipeline() -> "Pipeline":
            return Pipeline([
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

        # ── Train one classifier per horizon ─────────────────────────────
        horizon_meta = {}
        trained_clfs = {}

        for h, cfg in HORIZONS.items():
            if not splits[h]["X_tr"]:
                logger.warning(f"No data for {h}d horizon — skipping.")
                continue

            X_train = np.vstack(splits[h]["X_tr"])
            y_train = np.concatenate(splits[h]["y_tr"])
            X_test  = np.vstack(splits[h]["X_te"])
            y_test  = np.concatenate(splits[h]["y_te"])

            pos_rate = float(y_train.mean() * 100)
            logger.info(
                f"[{h}d] {len(X_train):,} train | {len(X_test):,} test | "
                f"Positive rate: {pos_rate:.1f}% | Threshold: {cfg['target_return']*100:.1f}%"
            )

            if progress_cb:
                progress_cb(processed, total, f"Training {h}d classifier…")

            clf = _make_clf_pipeline()
            clf.fit(X_train, y_train)

            # Evaluate
            y_proba = clf.predict_proba(X_test)[:, 1]
            y_pred  = (y_proba >= 0.60).astype(int)
            report  = classification_report(y_test, y_pred, output_dict=True, zero_division=0)

            try:
                auc = round(roc_auc_score(y_test, y_proba), 4)
            except Exception:
                auc = 0.0

            # Feature importances (averaged across calibrated folds)
            try:
                cal_step    = clf.named_steps["clf"]
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

            import joblib
            joblib.dump(clf, cfg["path"])
            trained_clfs[h] = clf

            horizon_meta[f"{h}d"] = {
                "horizon_days":    h,
                "target_return_pct": cfg["target_return"] * 100,
                "label":           cfg["label"],
                "train_samples":   int(len(X_train)),
                "test_samples":    int(len(X_test)),
                "positive_rate_pct": round(pos_rate, 2),
                "accuracy":        round(report.get("accuracy", 0), 4),
                "precision_buy":   round(report.get("1", {}).get("precision", 0), 4),
                "recall_buy":      round(report.get("1", {}).get("recall", 0), 4),
                "f1_buy":          round(report.get("1", {}).get("f1-score", 0), 4),
                "auc_roc":         auc,
                "reliability":     _compute_reliability(y_proba, y_test),
                "top_features":    [
                    {"name": n, "importance": round(float(v), 4)}
                    for n, v in top_features
                ],
            }
            logger.info(
                f"[{h}d] Training complete — AUC={auc} "
                f"Accuracy={horizon_meta[f'{h}d']['accuracy']}"
            )

        # Backward-compat copy: price_predictor.joblib = 5d model
        if 5 in trained_clfs:
            import joblib
            joblib.dump(trained_clfs[5], CLF_PATH)

        # ── Train return regressor (5d) ───────────────────────────────────
        mae = r2 = 0.0
        reg_pipeline = None
        if reg_splits["X_tr"]:
            if progress_cb:
                progress_cb(processed, total, "Training return regressor…")

            from sklearn.ensemble import RandomForestRegressor
            X_tr_r = np.vstack(reg_splits["X_tr"])
            y_tr_r = np.concatenate(reg_splits["y_tr"])
            X_te_r = np.vstack(reg_splits["X_te"])
            y_te_r = np.concatenate(reg_splits["y_te"])

            reg_pipeline = Pipeline([
                ("scaler", RobustScaler()),
                ("reg", RandomForestRegressor(
                    n_estimators=80,
                    max_depth=6,
                    min_samples_leaf=12,
                    max_features="sqrt",
                    random_state=42,
                    n_jobs=2,
                )),
            ])
            reg_pipeline.fit(X_tr_r, y_tr_r)

            from sklearn.metrics import mean_absolute_error, r2_score
            y_reg_pred = reg_pipeline.predict(X_te_r)
            mae = round(mean_absolute_error(y_te_r, y_reg_pred), 3)
            r2  = round(r2_score(y_te_r, y_reg_pred), 4)

            import joblib
            joblib.dump(reg_pipeline, REG_PATH)
            logger.info(f"Regressor (5d) — MAE={mae}%  R²={r2}")

        # ── Save combined meta ────────────────────────────────────────────
        meta = {
            "trained_at":        datetime.now().isoformat(),
            "validation_method": "walk_forward_temporal",
            "wf_test_ratio_pct": WF_TEST_RATIO * 100,
            "symbols_used":      processed,
            "horizons":          horizon_meta,
            # Top-level convenience fields (5d model — primary horizon)
            "auc_roc":           horizon_meta.get("5d", {}).get("auc_roc", 0.0),
            "accuracy":          horizon_meta.get("5d", {}).get("accuracy", 0.0),
            "train_samples":     horizon_meta.get("5d", {}).get("train_samples", 0),
            "test_samples":      horizon_meta.get("5d", {}).get("test_samples", 0),
            "regressor_mae_pct": mae,
            "regressor_r2":      r2,
            # Legacy keys kept for existing dashboard reads
            "forward_days":      5,
            "target_return_pct": 2.0,
            "reliability_buckets": horizon_meta.get("5d", {}).get("reliability", []),
            "top_features":      horizon_meta.get("5d", {}).get("top_features", []),
        }

        with open(META_PATH, "w") as fp:
            json.dump(meta, fp, indent=2)

        self.clf_3d  = trained_clfs.get(3)
        self.clf_5d  = trained_clfs.get(5)
        self.clf_10d = trained_clfs.get(10)
        self.reg     = reg_pipeline
        self.meta    = meta

        return meta

    # ─────────────────────────────────────────────────────────────────────
    # Inference
    # ─────────────────────────────────────────────────────────────────────

    def load(self) -> bool:
        """
        Load all available horizon models from disk.
        Returns True if at least the 5d model loaded successfully
        (required for combined/signals compatibility).
        """
        try:
            import joblib

            # Try new named paths first; fall back to legacy path for 5d
            if os.path.exists(CLF_5D_PATH):
                self.clf_5d = joblib.load(CLF_5D_PATH)
            elif os.path.exists(CLF_PATH):
                self.clf_5d = joblib.load(CLF_PATH)

            if os.path.exists(CLF_3D_PATH):
                self.clf_3d = joblib.load(CLF_3D_PATH)

            if os.path.exists(CLF_10D_PATH):
                self.clf_10d = joblib.load(CLF_10D_PATH)

            if os.path.exists(REG_PATH):
                self.reg = joblib.load(REG_PATH)

            if os.path.exists(META_PATH):
                with open(META_PATH) as fp:
                    self.meta = json.load(fp)

            return self.clf_5d is not None

        except Exception as e:
            logger.error(f"Model load error: {e}")
            return False

    def predict_symbol(self, df: pd.DataFrame, symbol: str,
                       regime: str = "Neutral") -> dict:
        """
        Predict buy probabilities across all three horizons for the latest bar.

        Args:
            df:     OHLCV DataFrame sorted oldest → newest (≥260 bars).
            symbol: Symbol name.
            regime: Market regime adjusts BUY/AVOID thresholds for 5d signal.

        Returns dict with:
            symbol, prob_3d, prob_5d, prob_10d,
            buy_probability (= prob_5d, backward compat),
            horizon_signals, signal (5d), confidence,
            price, expected_return_pct, price_target,
            regime, buy_threshold_used
        """
        if self.clf_5d is None:
            if not self.load():
                return {"symbol": symbol,
                        "error": "Model not trained — POST /api/ml/train first"}

        try:
            feats  = compute_features(df)
            latest = feats.iloc[-1:][FEATURE_NAMES].values

            if np.any(np.isnan(latest)):
                return {"symbol": symbol,
                        "error": "Insufficient history for all features"}

            # ── Per-horizon probabilities ──────────────────────────────────
            prob_3d  = (round(float(self.clf_3d.predict_proba(latest)[0][1]), 4)
                        if self.clf_3d  is not None else None)
            prob_5d  =  round(float(self.clf_5d.predict_proba(latest)[0][1]), 4)
            prob_10d = (round(float(self.clf_10d.predict_proba(latest)[0][1]), 4)
                        if self.clf_10d is not None else None)

            # ── Regime-adjusted thresholds (primary = 5d) ─────────────────
            buy_thresh, avoid_thresh = THRESHOLDS.get(regime, THRESHOLDS["Neutral"])

            def _signal(prob, buy_t, avoid_t):
                if prob is None:      return "UNAVAILABLE"
                if prob >= buy_t:     return "BUY"
                if prob <= avoid_t:   return "AVOID"
                return "NEUTRAL"

            # ── Horizon-specific signals ───────────────────────────────────
            # 3d uses slightly looser thresholds (shorter hold → faster signal)
            # 10d uses slightly stricter (longer hold → higher conviction needed)
            horizon_signals = {
                "3d": {
                    "probability": prob_3d,
                    "signal":      _signal(prob_3d,
                                           buy_thresh - 0.05,
                                           avoid_thresh + 0.02),
                    "label":       "Short-term",
                    "target_return_pct": HORIZONS[3]["target_return"] * 100,
                },
                "5d": {
                    "probability": prob_5d,
                    "signal":      _signal(prob_5d, buy_thresh, avoid_thresh),
                    "label":       "Swing",
                    "target_return_pct": HORIZONS[5]["target_return"] * 100,
                },
                "10d": {
                    "probability": prob_10d,
                    "signal":      _signal(prob_10d,
                                           buy_thresh + 0.05,
                                           avoid_thresh - 0.02),
                    "label":       "Positional",
                    "target_return_pct": HORIZONS[10]["target_return"] * 100,
                },
            }

            # Primary signal: 5d (used by combined/signals, quality score, etc.)
            primary_signal = horizon_signals["5d"]["signal"]

            # ── Predicted return % (5d regressor) ────────────────────────
            exp_return_pct = None
            price_target   = None
            if self.reg is not None:
                exp_return_pct = round(float(self.reg.predict(latest)[0]), 2)
                last_price     = float(df["close"].iloc[-1])
                price_target   = round(last_price * (1 + exp_return_pct / 100), 2)

            # ── Stability score — record then read ────────────────────────
            # Record prob_5d (primary horizon) into the rolling window and
            # immediately derive the stability label from the updated history.
            hist = get_prediction_history()
            hist.record(symbol, prob_5d)
            stab = hist.stability(symbol)

            return {
                "symbol":              symbol,
                # ── Multi-horizon probabilities ──────────────────────────
                "prob_3d":             prob_3d,
                "prob_5d":             prob_5d,
                "prob_10d":            prob_10d,
                "horizon_signals":     horizon_signals,
                # ── Legacy field (= prob_5d) ─────────────────────────────
                "buy_probability":     prob_5d,
                # ── Primary (5d) signal ──────────────────────────────────
                "signal":              primary_signal,
                "confidence":          _confidence_label(prob_5d),
                # ── Stability ────────────────────────────────────────────
                "stability":           stab["stability"],
                "stability_detail":    stab,
                # ── Price info ───────────────────────────────────────────
                "price":               round(float(df["close"].iloc[-1]), 2),
                "expected_return_pct": exp_return_pct,
                "price_target":        price_target,
                # ── Regime context ───────────────────────────────────────
                "regime":              regime,
                "buy_threshold_used":  buy_thresh,
            }

        except Exception as e:
            logger.error(f"predict_symbol error for {symbol}: {e}")
            return {"symbol": symbol, "error": str(e)}

    def predict_all(self, regime: str = "Neutral") -> list:
        """
        Run multi-horizon predictions for every symbol in the DB.
        Returns list sorted by prob_5d descending.
        """
        if self.clf_5d is None:
            if not self.load():
                return []

        results = []
        try:
            engine = get_engine()
            # Single bulk query with last 730 days — replaces N+1 per-symbol queries.
            df_all = pd.read_sql(
                f"""
                SELECT symbol, trade_date, open, high, low, close, volume
                FROM {TABLE_NAME}
                WHERE trade_date >= CURRENT_DATE - INTERVAL '730 days'
                ORDER BY symbol, trade_date ASC
                """,
                engine,
            )
            if df_all.empty:
                return []

            for sym, df in df_all.groupby("symbol", sort=False):
                df = df.reset_index(drop=True)
                if len(df) >= MIN_ROWS:
                    r = self.predict_symbol(df, sym, regime=regime)
                    if "error" not in r:
                        results.append(r)

        except Exception as e:
            logger.error(f"predict_all error: {e}")

        results.sort(key=lambda r: r.get("prob_5d", 0), reverse=True)
        return results

    # ─────────────────────────────────────────────────────────────────────
    # Utilities
    # ─────────────────────────────────────────────────────────────────────

    def is_trained(self) -> bool:
        """True if at least the primary (5d) model is on disk."""
        return os.path.exists(CLF_5D_PATH) or os.path.exists(CLF_PATH)

    def get_meta(self) -> dict:
        if self.meta:
            return self.meta
        if os.path.exists(META_PATH):
            with open(META_PATH) as fp:
                self.meta = json.load(fp)
            return self.meta
        return {}


# ── Helpers ──────────────────────────────────────────────────────────────

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
    Each bucket: "when the model predicted X%, the actual hit rate was Y%"
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
