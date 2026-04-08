"""
Prediction History — ml/prediction_history.py

Tracks the last N probability predictions per symbol in memory and derives
a Stability Score from their variance.

Low variance across recent predictions → model is consistent → HIGH stability
High variance                          → model is flipping   → LOW stability

Stability thresholds (prob_5d values, typically 0.40–0.85):
  HIGH   : variance < 0.0004  (std < 0.020 — probabilities barely move)
  MEDIUM : variance < 0.0025  (std < 0.050 — moderate drift)
  LOW    : variance ≥ 0.0025  (std ≥ 0.050 — inconsistent signals)

"INSUFFICIENT" is returned when fewer than MIN_SAMPLES predictions exist.
This prevents false confidence from tiny windows.

Storage: in-memory only — intentionally reset on server restart so stale
history from prior market regimes cannot inflate a symbol's stability.
"""

from __future__ import annotations

import math
import logging
from collections import defaultdict, deque
from typing import Optional

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────
WINDOW       = 5      # rolling window of predictions to keep per symbol
MIN_SAMPLES  = 2      # minimum predictions needed before scoring

# Variance → label boundaries (tuned for calibrated prob values in [0,1])
_THRESHOLDS = [
    (0.0004, "HIGH"),    # std < 0.020
    (0.0025, "MEDIUM"),  # std < 0.050
]
# anything above 0.0025 → LOW


class PredictionHistory:
    """
    Thread-safe-enough rolling history of per-symbol ML probabilities.

    FastAPI runs on a single-process event loop so standard Python dict
    access is safe without explicit locking.
    """

    def __init__(self, window: int = WINDOW):
        self._window  = window
        self._history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=self._window)
        )

    # ─────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────

    def record(self, symbol: str, probability: float) -> None:
        """Append a new probability observation for `symbol`."""
        if probability is None or not math.isfinite(probability):
            return
        self._history[symbol.upper()].append(float(probability))

    def stability(self, symbol: str) -> dict:
        """
        Compute the stability score for `symbol`.

        Returns:
            {
              "stability":     "HIGH" | "MEDIUM" | "LOW" | "INSUFFICIENT",
              "variance":      float | None,
              "std":           float | None,
              "n_predictions": int,
              "history":       [float, ...]   # most-recent first
            }
        """
        sym     = symbol.upper()
        history = list(self._history[sym])   # oldest → newest
        n       = len(history)

        if n < MIN_SAMPLES:
            return {
                "stability":     "INSUFFICIENT",
                "variance":      None,
                "std":           None,
                "n_predictions": n,
                "history":       history[::-1],
            }

        # Population variance (ddof=0) — we're describing this specific window,
        # not estimating a population parameter, so ddof=0 is correct.
        mean     = sum(history) / n
        variance = sum((x - mean) ** 2 for x in history) / n
        std      = math.sqrt(variance)

        label = "LOW"   # default if above all thresholds
        for threshold, name in _THRESHOLDS:
            if variance < threshold:
                label = name
                break

        return {
            "stability":     label,
            "variance":      round(variance, 6),
            "std":           round(std, 4),
            "n_predictions": n,
            "history":       list(reversed(history)),   # newest first
        }

    def clear(self, symbol: str | None = None) -> None:
        """Clear history for one symbol or all symbols."""
        if symbol is not None:
            self._history.pop(symbol.upper(), None)
        else:
            self._history.clear()

    def snapshot(self) -> dict:
        """Return a dict of all tracked symbols and their stability scores."""
        return {
            sym: self.stability(sym)
            for sym in self._history
            if len(self._history[sym]) >= MIN_SAMPLES
        }

    def __len__(self) -> int:
        return len(self._history)


# ── Module-level singleton ────────────────────────────────────────────────

_history: Optional[PredictionHistory] = None


def get_prediction_history() -> PredictionHistory:
    """Return the shared process-wide singleton."""
    global _history
    if _history is None:
        _history = PredictionHistory()
    return _history
