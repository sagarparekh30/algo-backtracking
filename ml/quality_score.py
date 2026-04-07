"""
Trade Quality Score (0–100)

Combines five dimensions into a single score:
  1. ML probability       (0–40 pts) — core signal strength
  2. Trend strength       (0–20 pts) — stock's own momentum (3M return)
  3. Volume confirmation  (0–15 pts) — vol spike vs 20-day avg
  4. Sector strength      (0–15 pts) — sector's 1M performance
  5. Market regime        (0–10 pts) — macro environment bonus

Score → Confidence label:
  75–100 : High
  50–74  : Medium
  0–49   : Low
"""

from __future__ import annotations
from typing import Optional


# ── Scoring constants ────────────────────────────────────────────────────

_REGIME_BONUS = {"Bull": 10, "Neutral": 5, "Bear": 0}


def compute_quality_score(
    buy_probability: float,
    regime: str = "Neutral",
    trend_return_3m: Optional[float] = None,
    vol_ratio: Optional[float] = None,
    sector_return_1m: Optional[float] = None,
    strategy_count: int = 1,
) -> dict:
    """
    Compute a 0–100 trade quality score.

    Args:
        buy_probability:  ML classifier output  (0.0 – 1.0)
        regime:           "Bull" | "Neutral" | "Bear"
        trend_return_3m:  Stock's 63-day return %  (None = skip)
        vol_ratio:        Today's volume / 20-day avg (None = skip)
        sector_return_1m: Sector's 20-day avg return % (None = skip)
        strategy_count:   Number of strategies that fired (bonus multiplier)

    Returns:
        {"score": int, "confidence": str, "breakdown": dict}
    """

    # ── 1. ML probability (0–40) ─────────────────────────────────────────
    # Maps 0.50 → 0, 0.75 → 20, 1.00 → 40
    ml_raw = max(0.0, (buy_probability - 0.5) * 2)   # normalise above 50%
    ml_score = min(40, int(ml_raw * 40))

    # ── 2. Trend strength (0–20) ──────────────────────────────────────────
    if trend_return_3m is None:
        trend_score = 8   # neutral when no data
    elif trend_return_3m >= 20:
        trend_score = 20
    elif trend_return_3m >= 10:
        trend_score = 16
    elif trend_return_3m >= 5:
        trend_score = 12
    elif trend_return_3m >= 2:
        trend_score = 8
    elif trend_return_3m >= 0:
        trend_score = 4
    else:
        trend_score = 0   # negative trend — penalise

    # ── 3. Volume confirmation (0–15) ─────────────────────────────────────
    if vol_ratio is None:
        vol_score = 5   # neutral
    elif vol_ratio >= 4.0:
        vol_score = 15
    elif vol_ratio >= 3.0:
        vol_score = 12
    elif vol_ratio >= 2.0:
        vol_score = 9
    elif vol_ratio >= 1.5:
        vol_score = 6
    elif vol_ratio >= 1.0:
        vol_score = 3
    else:
        vol_score = 0

    # ── 4. Sector strength (0–15) ─────────────────────────────────────────
    if sector_return_1m is None:
        sector_score = 5   # neutral
    elif sector_return_1m >= 8:
        sector_score = 15
    elif sector_return_1m >= 4:
        sector_score = 11
    elif sector_return_1m >= 1:
        sector_score = 7
    elif sector_return_1m >= 0:
        sector_score = 3
    else:
        sector_score = 0

    # ── 5. Market regime bonus (0–10) ─────────────────────────────────────
    regime_score = _REGIME_BONUS.get(regime, 5)

    # ── Multi-strategy bonus (up to +5) ───────────────────────────────────
    strat_bonus = min(5, (strategy_count - 1) * 2)

    total = ml_score + trend_score + vol_score + sector_score + regime_score + strat_bonus
    total = min(100, total)

    if total >= 75:
        confidence = "High"
    elif total >= 50:
        confidence = "Medium"
    else:
        confidence = "Low"

    return {
        "score":      total,
        "confidence": confidence,
        "breakdown": {
            "ml_probability":  ml_score,
            "trend_strength":  trend_score,
            "volume":          vol_score,
            "sector":          sector_score,
            "regime":          regime_score,
            "multi_strategy":  strat_bonus,
        },
    }
