"""
Trade Outcome Learning Engine — ml/trade_feedback_model.py

Learns from past closed trades in trade_journal to predict the probability
of a new trade succeeding BEFORE entry. Acts as a second-opinion filter
layered on top of the existing ML price-direction model.

Pipeline position:
  strategy scan → ML price model → [Trade Feedback] → quality score → output

Feature set (15 features, expanded from 8):
  ── Core trade parameters (8) ─────────────────────────────────────────
  risk_pct          — (entry - stop_loss) / entry
  reward_pct        — (target - entry) / entry
  rr_ratio          — reward / risk
  buy_probability   — ML model output stored at trade time
  expected_return   — regressor output stored at trade time
  day_of_week       — 0=Mon … 4=Fri (normalised /4)
  strategy_count    — number of strategies that fired (normalised /16)
  trade_type_enc    — 1=live, 0=paper

  ── Contextual features from feature_snapshot (7) ────────────────────
  regime_enc        — market regime: Bull=1.0, Neutral=0.5, Bear=0.0
  volatility_bucket — ATR-based: Low=0.0 / Med=0.33 / High=0.67 / VHigh=1.0
  sector_strength   — sector score (0–15) / 15.0
  trend_strength_3m — trend score (0–20) / 20.0
  entry_type_enc    — Breakout=1.0, Continuation=0.5, Pullback=0.0
  gap_pct_norm      — signal-bar opening gap, normalised to [0,1]
  quality_score_norm— overall quality score / 100.0

Contextual features resolve with priority:
  1. feature_snapshot  (point-in-time values captured at trade entry)
  2. neutral defaults  (safe fallback for pre-migration trades)

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

from config.settings import BASE_DIR
from db.connection import get_conn

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────
MODEL_DIR    = os.path.join(str(BASE_DIR), "ml", "models")
FB_CLF_PATH  = os.path.join(MODEL_DIR, "trade_feedback.joblib")
FB_META_PATH = os.path.join(MODEL_DIR, "trade_feedback_meta.json")

# ── Config ───────────────────────────────────────────────────────────────
MIN_TRADES     = 20       # minimum closed trades required to train
MAX_ADJUSTMENT = 10.0     # max quality score points to add/subtract

# Feature names — order MUST match _build_single_row() return list
FEATURE_NAMES = [
    # ── Core trade parameters (8) ────────────────────────────────────────
    "risk_pct",
    "reward_pct",
    "rr_ratio",
    "buy_probability",
    "expected_return_pct",
    "day_of_week",
    "strategy_count",
    "trade_type_enc",
    # ── Contextual market features (7) ───────────────────────────────────
    "regime_enc",
    "volatility_bucket",
    "sector_strength",
    "trend_strength_3m",
    "entry_type_enc",
    "gap_pct_norm",
    "quality_score_norm",
]

# ── Neutral defaults for contextual features (used when no snapshot) ─────
_REGIME_ENC      = {"Bull": 1.0, "Neutral": 0.5, "Bear": 0.0}
_CTX_DEFAULTS = {
    "regime_enc":        0.5,    # Neutral
    "volatility_bucket": 0.33,   # Medium-low
    "sector_strength":   0.33,   # ~5/15 neutral bucket
    "trend_strength_3m": 0.40,   # ~8/20 neutral bucket
    "entry_type_enc":    0.5,    # Continuation
    "gap_pct_norm":      0.5,    # No gap
    "quality_score_norm":0.5,    # Mid-range
}


# ── Snapshot extraction helpers ──────────────────────────────────────────

def _parse_snap(raw) -> Optional[dict]:
    """Return snapshot dict from raw DB value (dict or JSON string), or None."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return None


def _regime_enc(regime: str) -> float:
    return _REGIME_ENC.get(regime, 0.5)


def _volatility_bucket(atr_norm: Optional[float]) -> float:
    """Bucket normalised ATR into 4 levels: 0.0 / 0.33 / 0.67 / 1.0."""
    if atr_norm is None:
        return _CTX_DEFAULTS["volatility_bucket"]
    if atr_norm < 0.012:
        return 0.0    # very low volatility
    if atr_norm < 0.020:
        return 0.33   # medium-low
    if atr_norm < 0.030:
        return 0.67   # medium-high
    return 1.0        # high volatility


def _entry_type_enc(bb_position: Optional[float],
                    vol_ratio: Optional[float]) -> float:
    """
    Classify the trade setup from Bollinger position and volume:
      Breakout     (1.0): closing near band top with above-average volume
      Pullback     (0.0): closing near band bottom (potential support bounce)
      Continuation (0.5): mid-band, ambiguous
    """
    if bb_position is None:
        return _CTX_DEFAULTS["entry_type_enc"]
    if bb_position >= 0.75 and (vol_ratio or 0.0) >= 1.2:
        return 1.0   # breakout
    if bb_position <= 0.30:
        return 0.0   # pullback / support test
    return 0.5       # continuation


def _gap_pct_norm(gap_pct_raw: Optional[float]) -> float:
    """
    Normalise the signal-bar opening gap percentage to [0, 1].
    gap_pct_raw is a decimal fraction (e.g. 0.02 = 2 % gap up).
    Clipped to ±10 % then mapped:  -10% → 0.0,  0% → 0.5,  +10% → 1.0
    """
    if gap_pct_raw is None:
        return _CTX_DEFAULTS["gap_pct_norm"]
    clipped = max(-0.10, min(0.10, gap_pct_raw))
    return round((clipped + 0.10) / 0.20, 4)


def _extract_ctx_from_snap(snap: dict) -> dict:
    """
    Extract all 7 contextual features from a stored feature_snapshot dict.
    Falls back to neutral defaults for any missing keys.
    """
    features       = snap.get("features") or {}
    score_breakdown = snap.get("score_breakdown") or {}

    regime_raw      = snap.get("regime", "Neutral")
    atr_norm_raw    = features.get("atr_norm")
    bb_position_raw = features.get("bb_position")
    vol_ratio_raw   = features.get("vol_ratio")
    sector_score    = score_breakdown.get("sector")     # 0–15
    trend_score     = score_breakdown.get("trend_strength")  # 0–20
    quality_score   = snap.get("quality_score")         # 0–100
    gap_pct_raw     = snap.get("gap_pct")               # decimal fraction or None

    return {
        "regime_enc":        _regime_enc(regime_raw),
        "volatility_bucket": _volatility_bucket(
            float(atr_norm_raw) if atr_norm_raw is not None else None
        ),
        "sector_strength":   round(float(sector_score) / 15.0, 4)
                             if sector_score is not None else _CTX_DEFAULTS["sector_strength"],
        "trend_strength_3m": round(float(trend_score) / 20.0, 4)
                             if trend_score is not None else _CTX_DEFAULTS["trend_strength_3m"],
        "entry_type_enc":    _entry_type_enc(
            float(bb_position_raw) if bb_position_raw is not None else None,
            float(vol_ratio_raw)   if vol_ratio_raw   is not None else None,
        ),
        "gap_pct_norm":      _gap_pct_norm(
            float(gap_pct_raw) if gap_pct_raw is not None else None
        ),
        "quality_score_norm": round(float(quality_score) / 100.0, 4)
                              if quality_score is not None else _CTX_DEFAULTS["quality_score_norm"],
    }


# ── TradeFeedbackModel ───────────────────────────────────────────────────

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

        Trades with feature_snapshot use all 15 features.
        Trades without snapshot use 8 core features + neutral defaults for the
        7 contextual ones — same vector length, lower information content.
        The model sees both types during training, which prevents overfitting
        to the contextual features while they're still sparse.

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

        snap_count = int(df["feature_snapshot"].notna().sum()) if "feature_snapshot" in df.columns else 0

        if progress_cb:
            progress_cb(
                f"Building features from {len(df)} trades "
                f"({snap_count} with snapshot, "
                f"{len(df) - snap_count} legacy)…"
            )

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
            progress_cb(
                f"Training Random Forest + calibration "
                f"({len(X_train)} train / {len(X_test)} test)…"
            )

        pipeline = Pipeline([
            ("scaler", RobustScaler()),
            ("clf", CalibratedClassifierCV(
                RandomForestClassifier(
                    n_estimators=150,       # bumped from 100 — more features need more trees
                    max_depth=7,            # slightly deeper for 15 features
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
            "trained_at":        datetime.now().isoformat(),
            "feature_count":     len(FEATURE_NAMES),
            "feature_names":     FEATURE_NAMES,
            "total_trades":      int(len(X)),
            "snap_trades":       snap_count,
            "legacy_trades":     int(len(X)) - snap_count,
            "train_samples":     int(len(X_train)),
            "test_samples":      int(len(X_test)),
            "win_rate_pct":      round(pos_rate, 2),
            "accuracy":          round(report.get("accuracy", 0), 4),
            "precision_win":     round(report.get("1", {}).get("precision", 0), 4),
            "recall_win":        round(report.get("1", {}).get("recall", 0), 4),
            "f1_win":            round(report.get("1", {}).get("f1-score", 0), 4),
            "auc_roc":           auc,
            "top_features":      [
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
            f"AUC={auc} Accuracy={meta['accuracy']} Trades={len(X)} "
            f"Features={len(FEATURE_NAMES)}"
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
        # ── Contextual params (all optional — default to neutral) ────────
        snapshot: Optional[dict] = None,   # full feature_snapshot dict
        regime: str = "Neutral",           # used if no snapshot
        atr_norm: Optional[float] = None,  # used if no snapshot
        bb_position: Optional[float] = None,
        vol_ratio: Optional[float] = None,
        sector_score: Optional[float] = None,  # raw score 0–15
        trend_score: Optional[float] = None,   # raw score 0–20
        gap_pct: Optional[float] = None,       # decimal fraction
        quality_score: Optional[int] = None,   # 0–100
    ) -> dict:
        """
        Predict the probability this trade will succeed based on historical outcomes.

        Contextual features are resolved in this order:
          1. snapshot dict (if passed — most accurate, point-in-time)
          2. explicit keyword params (regime, atr_norm, …)
          3. neutral defaults

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
            # Resolve contextual features
            snap = _parse_snap(snapshot)
            if snap is not None:
                ctx = _extract_ctx_from_snap(snap)
            else:
                ctx = {
                    "regime_enc":        _regime_enc(regime),
                    "volatility_bucket": _volatility_bucket(atr_norm),
                    "sector_strength":   round(float(sector_score) / 15.0, 4)
                                         if sector_score is not None
                                         else _CTX_DEFAULTS["sector_strength"],
                    "trend_strength_3m": round(float(trend_score) / 20.0, 4)
                                         if trend_score is not None
                                         else _CTX_DEFAULTS["trend_strength_3m"],
                    "entry_type_enc":    _entry_type_enc(bb_position, vol_ratio),
                    "gap_pct_norm":      _gap_pct_norm(gap_pct),
                    "quality_score_norm":round(float(quality_score) / 100.0, 4)
                                         if quality_score is not None
                                         else _CTX_DEFAULTS["quality_score_norm"],
                }

            row = self._build_single_row(
                entry_price=entry_price,
                stop_loss=stop_loss,
                target=target,
                buy_probability=buy_probability,
                expected_return_pct=expected_return_pct,
                strategy_tags=strategy_tags,
                trade_type=trade_type,
                day_of_week=day_of_week,
                ctx=ctx,
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
                "win":            counts.get("win", 0),
                "loss":           counts.get("loss", 0),
                "stopped":        counts.get("stopped", 0),
                "open":           counts.get("open", 0),
                "closed":         closed,
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
        """Fetch closed trades from trade_journal, including feature snapshots."""
        try:
            conn = get_conn()
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        entry_price, stop_loss, target,
                        buy_probability, expected_return_pct,
                        strategy_tags, trade_type,
                        entry_date, status,
                        feature_snapshot
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
        """
        Build X (features) and y (labels) from closed trades DataFrame.

        Feature resolution per trade:
          • snapshot present  → all 15 features at full fidelity
          • no snapshot       → 8 core features + neutral defaults for 7 contextual ones

        This means legacy trades still contribute to training (they teach
        the core 8 features) without polluting the 7 contextual slots with
        incorrect values.
        """
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

                # ── Resolve contextual features from snapshot ─────────────
                snap = _parse_snap(row.get("feature_snapshot"))

                if snap is not None:
                    # Point-in-time values from snapshot
                    buy_prob      = float(snap.get("ml_prob") or
                                          row.get("buy_probability") or 0.0)
                    exp_ret       = float(snap.get("expected_return_pct") or
                                          row.get("expected_return_pct") or 0.0)
                    strategies    = snap.get("strategies") or []
                    strategy_tags = ",".join(strategies) if strategies else str(row.get("strategy_tags") or "")
                    ctx           = _extract_ctx_from_snap(snap)
                else:
                    # Fallback: derive from existing columns
                    buy_prob      = float(row.get("buy_probability") or 0.0)
                    exp_ret       = float(row.get("expected_return_pct") or 0.0)
                    strategy_tags = str(row.get("strategy_tags") or "")
                    ctx           = dict(_CTX_DEFAULTS)   # all neutral defaults

                feat = self._build_single_row(
                    entry_price=entry,
                    stop_loss=sl,
                    target=target,
                    buy_probability=buy_prob,
                    expected_return_pct=exp_ret,
                    strategy_tags=strategy_tags,
                    trade_type=str(row.get("trade_type") or "paper"),
                    day_of_week=dow,
                    ctx=ctx,
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
        ctx: Optional[dict] = None,
    ) -> Optional[list]:
        """
        Compute the 15-element feature vector for a single trade.

        Args:
            ctx: pre-resolved contextual feature dict (keys match the 7
                 contextual FEATURE_NAMES).  If None, neutral defaults apply.
        """
        try:
            if entry_price <= 0 or stop_loss <= 0:
                return None

            # ── Core trade parameters (8) ─────────────────────────────────
            risk_pct   = abs(entry_price - stop_loss) / entry_price
            reward_pct = abs(target - entry_price) / entry_price if target and entry_price > 0 else 0.0
            rr_ratio   = reward_pct / risk_pct if risk_pct > 0 else 0.0

            strat_count = (
                len([s for s in strategy_tags.split(",") if s.strip()])
                if strategy_tags else 1
            )

            _ctx = ctx or _CTX_DEFAULTS

            return [
                # ── Core (8) ────────────────────────────────────────────────
                min(risk_pct, 0.5),                                          # risk_pct
                min(reward_pct, 1.0),                                        # reward_pct
                min(rr_ratio, 10.0),                                         # rr_ratio
                float(buy_probability or 0.0),                               # buy_probability
                float(expected_return_pct or 0.0),                          # expected_return_pct
                float(day_of_week if day_of_week is not None else 2) / 4.0, # day_of_week
                min(strat_count, 16) / 16.0,                                # strategy_count
                1.0 if trade_type == "live" else 0.0,                       # trade_type_enc
                # ── Contextual (7) ──────────────────────────────────────────
                float(_ctx.get("regime_enc",        _CTX_DEFAULTS["regime_enc"])),
                float(_ctx.get("volatility_bucket", _CTX_DEFAULTS["volatility_bucket"])),
                float(_ctx.get("sector_strength",   _CTX_DEFAULTS["sector_strength"])),
                float(_ctx.get("trend_strength_3m", _CTX_DEFAULTS["trend_strength_3m"])),
                float(_ctx.get("entry_type_enc",    _CTX_DEFAULTS["entry_type_enc"])),
                float(_ctx.get("gap_pct_norm",      _CTX_DEFAULTS["gap_pct_norm"])),
                float(_ctx.get("quality_score_norm",_CTX_DEFAULTS["quality_score_norm"])),
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
