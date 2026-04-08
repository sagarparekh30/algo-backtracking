"""
AI Explanation Engine — ml/explainer.py

Converts raw ML outputs into human-readable reasoning for each trade signal.
Operates entirely on data that is already computed by the combined/signals
pipeline — no additional DB queries or model calls.

Input:  a single result dict from /api/combined/signals
Output: {
    "headline":  str               — one-line verdict
    "bullets":   [str, ...]        — ordered evidence points (positive)
    "cautions":  [str, ...]        — risks and warnings
    "summary":   str               — two-sentence synthesis
    "score_narrative": str         — explains the 0-100 quality score
}

Design principles:
  • Rules over LLM — deterministic, auditable, no external API calls
  • Priority-ordered bullets — most important evidence first
  • Cautions are generated independently from bullets (don't suppress signal)
  • All fields always present — empty lists, not missing keys
"""

from __future__ import annotations

from typing import Optional

# ── Strategy → readable label ──────────────────────────────────────────────
_STRATEGY_LABELS: dict[str, str] = {
    "golden_rsi":            "Golden RSI crossover",
    "macd_breakout":         "MACD breakout",
    "bb_squeeze":            "Bollinger Band squeeze breakout",
    "ema_crossover":         "EMA crossover",
    "vwap_reversal":         "VWAP reversal",
    "orb_breakout":          "Opening range breakout",
    "momentum_burst":        "Momentum burst",
    "mean_reversion":        "Mean reversion setup",
    "volume_climax":         "Volume climax",
    "support_bounce":        "Support bounce",
    "trend_continuation":    "Trend continuation",
    "rsi_divergence":        "RSI divergence",
    "bollinger_breakout":    "Bollinger Band breakout",
    "gap_fill":              "Gap fill setup",
    "moving_average_ribbon": "Moving average ribbon alignment",
    "supertrend":            "Supertrend signal",
}

# ── Quality score tier labels ──────────────────────────────────────────────
def _quality_tier(score: int) -> str:
    if score >= 75:  return "HIGH"
    if score >= 50:  return "MEDIUM"
    return "LOW"


# ── Internal bullet builders ───────────────────────────────────────────────

def _trend_bullet(trend_3m: Optional[float]) -> Optional[str]:
    if trend_3m is None:
        return None
    direction = "uptrend" if trend_3m >= 0 else "downtrend"
    sign      = "+" if trend_3m >= 0 else ""
    strength  = (
        "Strong"    if abs(trend_3m) >= 15 else
        "Moderate"  if abs(trend_3m) >= 7  else
        "Mild"
    )
    return f"{strength} {direction}: 3M return {sign}{trend_3m:.1f}%"


def _volume_bullet(vol_ratio: Optional[float]) -> Optional[str]:
    if vol_ratio is None:
        return None
    if vol_ratio >= 3.0:
        return f"Heavy volume surge: {vol_ratio:.1f}x the 20-day average — institutional participation likely"
    if vol_ratio >= 2.0:
        return f"Significant volume surge: {vol_ratio:.1f}x average — strong buying pressure"
    if vol_ratio >= 1.5:
        return f"Above-average volume: {vol_ratio:.1f}x average — confirms the move"
    if vol_ratio >= 1.0:
        return f"Normal volume: {vol_ratio:.1f}x average"
    return f"Below-average volume ({vol_ratio:.1f}x) — move lacks confirmation"


def _strategy_bullet(strategies: list[str]) -> Optional[str]:
    if not strategies:
        return None
    labels = [_STRATEGY_LABELS.get(s, s.replace("_", " ").title()) for s in strategies]
    if len(labels) == 1:
        return f"Pattern confirmed: {labels[0]}"
    if len(labels) == 2:
        return f"Two patterns aligned: {labels[0]} + {labels[1]}"
    first_two = ", ".join(labels[:2])
    return (
        f"{len(labels)} patterns aligned: {first_two} "
        f"and {len(labels) - 2} more — high-conviction convergence"
    )


def _ml_bullet(prob: float) -> str:
    pct = int(round(prob * 100))
    if pct >= 75:
        tier = "very high"
    elif pct >= 65:
        tier = "high"
    elif pct >= 55:
        tier = "moderate"
    else:
        tier = "marginal"
    return f"ML confidence: {pct}% ({tier}) — model expects a bullish 5-day move"


def _horizon_bullet(prob_3d: Optional[float],
                    prob_5d: float,
                    prob_10d: Optional[float]) -> Optional[str]:
    """Emit a bullet only when all three horizons are available and agree."""
    if prob_3d is None or prob_10d is None:
        return None
    p3  = int(round(prob_3d * 100))
    p5  = int(round(prob_5d * 100))
    p10 = int(round(prob_10d * 100))

    # All three rising = positional momentum building
    if p3 <= p5 <= p10:
        return (
            f"Multi-horizon agreement — confidence grows with time: "
            f"Short-term {p3}% → Swing {p5}% → Positional {p10}%"
        )
    # All three above 55%
    if p3 >= 55 and p5 >= 55 and p10 >= 55:
        return (
            f"All three horizons bullish: "
            f"Short-term {p3}% · Swing {p5}% · Positional {p10}%"
        )
    # Mixed
    return (
        f"Horizon breakdown — Short-term {p3}% · Swing {p5}% · Positional {p10}%"
    )


def _stability_bullet(stability: Optional[str],
                      stab_detail: Optional[dict]) -> Optional[str]:
    if not stability or stability == "INSUFFICIENT":
        return None
    n   = (stab_detail or {}).get("n_predictions", 0)
    std = (stab_detail or {}).get("std")
    std_str = f", std={std:.3f}" if std is not None else ""
    labels = {"HIGH": "consistent", "MEDIUM": "moderately stable", "LOW": "inconsistent"}
    desc   = labels.get(stability, stability.lower())
    return (
        f"Prediction stability: {stability} — signal has been {desc} "
        f"across {n} recent run{'s' if n != 1 else ''}{std_str}"
    )


def _rr_bullet(entry: Optional[float],
               stop_loss: Optional[float],
               target: Optional[float]) -> Optional[str]:
    if not all([entry, stop_loss, target]):
        return None
    try:
        risk   = abs(float(entry) - float(stop_loss))
        reward = abs(float(target) - float(entry))
        if risk <= 0:
            return None
        rr = reward / risk
        rating = "excellent" if rr >= 2.5 else "good" if rr >= 2.0 else "acceptable" if rr >= 1.5 else "tight"
        return (
            f"Risk:Reward = 1:{rr:.1f} ({rating}) — "
            f"risking ₹{risk:.2f} to target ₹{reward:.2f}"
        )
    except Exception:
        return None


def _regime_bullet(regime: str) -> str:
    narratives = {
        "Bull": "Bull market regime — macro tailwind increases probability of follow-through",
        "Bear": "Bear market regime — macro headwind; model threshold is higher for this signal",
        "Neutral": "Neutral market regime — stock-specific factors dominate",
    }
    return narratives.get(regime, f"Market regime: {regime}")


def _score_narrative(score: int, breakdown: Optional[dict]) -> str:
    if not breakdown:
        return f"Quality score: {score}/100."
    parts = []
    ml_pts     = breakdown.get("ml_probability", 0)
    trend_pts  = breakdown.get("trend_strength", 0)
    vol_pts    = breakdown.get("volume", 0)
    sector_pts = breakdown.get("sector", 0)
    regime_pts = breakdown.get("regime", 0)
    strat_pts  = breakdown.get("multi_strategy", 0)
    parts.append(f"ML signal {ml_pts}/40")
    parts.append(f"trend {trend_pts}/20")
    parts.append(f"volume {vol_pts}/15")
    parts.append(f"sector {sector_pts}/15")
    parts.append(f"regime {regime_pts}/10")
    if strat_pts > 0:
        parts.append(f"multi-strategy bonus +{strat_pts}")
    return f"Quality score {score}/100 = {' + '.join(parts)}."


def _summary(
    quality_score: int,
    prob: float,
    regime: str,
    trend_3m: Optional[float],
    vol_ratio: Optional[float],
    n_strategies: int,
    stability: Optional[str],
    expected_return_pct: Optional[float],
) -> str:
    tier = _quality_tier(quality_score)

    # Dominant positive factor
    if trend_3m is not None and trend_3m >= 10 and (vol_ratio or 0) >= 2.0:
        driver = "strong momentum backed by above-average volume"
    elif trend_3m is not None and trend_3m >= 10:
        driver = "strong 3-month uptrend"
    elif (vol_ratio or 0) >= 2.0:
        driver = "a significant volume surge"
    elif n_strategies >= 3:
        driver = f"convergence of {n_strategies} technical patterns"
    elif prob >= 0.70:
        driver = "high ML conviction"
    else:
        driver = "a technical setup"

    # Regime colour
    regime_phrase = {
        "Bull":    "in a supportive bull market",
        "Bear":    "despite bearish market conditions",
        "Neutral": "in a neutral market",
    }.get(regime, "")

    # Stability qualifier
    stab_phrase = ""
    if stability == "HIGH":
        stab_phrase = " The signal has been consistent across recent predictions."
    elif stability == "LOW":
        stab_phrase = " Note: prediction stability is low — monitor closely before entry."

    # Return expectation
    ret_phrase = ""
    if expected_return_pct is not None:
        ret_phrase = f" Model projects a {expected_return_pct:+.1f}% move over 5 days."

    first_sentence = (
        f"This is a {tier.lower()}-conviction setup driven by {driver} "
        f"{regime_phrase}.".strip()
    )
    return first_sentence + ret_phrase + stab_phrase


# ── Caution builder ────────────────────────────────────────────────────────

def _build_cautions(
    stability:   Optional[str],
    vol_ratio:   Optional[float],
    n_strategies: int,
    prob_3d:     Optional[float],
    prob:        float,
    regime:      str,
    atr:         Optional[float],
    entry:       Optional[float],
) -> list[str]:
    cautions = []

    if stability == "LOW":
        cautions.append(
            "Prediction stability is LOW — the model has given inconsistent signals "
            "recently. Consider waiting one more session for confirmation."
        )
    elif stability == "INSUFFICIENT":
        cautions.append(
            "Insufficient prediction history — this signal is new. "
            "No stability assessment is possible yet."
        )

    if n_strategies == 1:
        cautions.append(
            "Only one technical pattern confirmed. "
            "Signals with 2+ pattern convergence have historically higher accuracy."
        )

    if vol_ratio is not None and vol_ratio < 1.0:
        cautions.append(
            f"Volume is below average ({vol_ratio:.1f}x). "
            "Breakouts on thin volume have higher failure rates."
        )

    if prob_3d is not None and prob_3d < 0.50:
        cautions.append(
            f"Short-term (3d) model is neutral at {int(prob_3d*100)}%. "
            "The opportunity may need 5–10 days to develop — not suitable for quick trades."
        )

    if regime == "Bear" and prob < 0.65:
        cautions.append(
            "Bear market regime: only high-conviction signals (≥65%) clear the "
            "adjusted threshold. This trade is close to the edge."
        )

    if atr is not None and entry is not None and entry > 0:
        try:
            atr_pct = float(atr) / float(entry) * 100
            if atr_pct > 3.0:
                cautions.append(
                    f"High ATR ({atr_pct:.1f}% of price) — stop distances will be wide. "
                    "Size position accordingly to keep risk per trade within limits."
                )
        except Exception:
            pass

    return cautions


# ── Public API ─────────────────────────────────────────────────────────────

def explain(result: dict) -> dict:
    """
    Generate a structured AI explanation for a single combined/signals result.

    Args:
        result: One element from the /api/combined/signals results list.

    Returns:
        {
          "headline":       str,
          "bullets":        [str, ...],
          "cautions":       [str, ...],
          "summary":        str,
          "score_narrative":str,
        }
    """
    # ── Extract all inputs ─────────────────────────────────────────────
    symbol        = result.get("symbol", "")
    prob          = float(result.get("buy_probability") or result.get("prob_5d") or 0.0)
    quality_score = int(result.get("quality_score") or 0)
    breakdown     = result.get("score_breakdown") or {}
    strategies    = result.get("strategies") or []
    regime        = result.get("regime") or "Neutral"
    trend_3m      = result.get("trend_3m")
    vol_ratio     = result.get("vol_ratio")
    prob_3d       = result.get("prob_3d")
    prob_5d       = result.get("prob_5d") or prob
    prob_10d      = result.get("prob_10d")
    stability     = result.get("stability")
    stab_detail   = result.get("stability_detail") or {}
    entry         = result.get("entry") or result.get("price")
    stop_loss     = result.get("stop_loss")
    target        = result.get("target")
    atr           = result.get("atr")
    exp_return    = result.get("expected_return_pct")
    confidence    = result.get("confidence") or "Moderate"
    n_strategies  = len(strategies)

    tier = _quality_tier(quality_score)

    # ── Headline ───────────────────────────────────────────────────────
    headline = (
        f"{symbol} is rated {tier} quality ({quality_score}/100) "
        f"with {confidence.lower()} ML confidence ({int(prob * 100)}%)"
    )

    # ── Positive bullets (priority-ordered) ───────────────────────────
    bullets: list[str] = []

    b = _strategy_bullet(strategies)
    if b:
        bullets.append(b)

    b = _trend_bullet(trend_3m)
    if b:
        bullets.append(b)

    b = _volume_bullet(vol_ratio)
    if b:
        bullets.append(b)

    bullets.append(_ml_bullet(prob_5d))

    b = _horizon_bullet(prob_3d, prob_5d, prob_10d)
    if b:
        bullets.append(b)

    b = _stability_bullet(stability, stab_detail)
    if b:
        bullets.append(b)

    b = _rr_bullet(entry, stop_loss, target)
    if b:
        bullets.append(b)

    bullets.append(_regime_bullet(regime))

    # ── Cautions ───────────────────────────────────────────────────────
    cautions = _build_cautions(
        stability=stability,
        vol_ratio=vol_ratio,
        n_strategies=n_strategies,
        prob_3d=prob_3d,
        prob=prob,
        regime=regime,
        atr=atr,
        entry=entry,
    )

    # ── Summary + score narrative ──────────────────────────────────────
    summary = _summary(
        quality_score=quality_score,
        prob=prob,
        regime=regime,
        trend_3m=trend_3m,
        vol_ratio=vol_ratio,
        n_strategies=n_strategies,
        stability=stability,
        expected_return_pct=exp_return,
    )

    score_narrative = _score_narrative(quality_score, breakdown)

    return {
        "headline":        headline,
        "bullets":         bullets,
        "cautions":        cautions,
        "summary":         summary,
        "score_narrative": score_narrative,
    }


def explain_batch(results: list[dict]) -> list[dict]:
    """
    Add an 'explanation' key to each result dict in-place and return the list.
    Failures are caught per-symbol so one bad result never blocks the rest.
    """
    for r in results:
        try:
            r["explanation"] = explain(r)
        except Exception as exc:
            r["explanation"] = {
                "headline":        r.get("symbol", "Unknown"),
                "bullets":         [],
                "cautions":        [f"Explanation unavailable: {exc}"],
                "summary":         "",
                "score_narrative": "",
            }
    return results
