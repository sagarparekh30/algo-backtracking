"""
Position Sizing Module — risk/position_sizer.py

Two sizing modes for NSE swing trading:

  1. Fixed Fractional (default)
     rupee_risk    = capital × risk_pct / 100
     shares        = rupee_risk / (entry - stop_loss)

  2. Kelly Criterion (optional)
     kelly_f       = win_rate − (1 − win_rate) / avg_rr
     half_kelly    = kelly_f / 2          (half-Kelly — standard conservative choice)
     position_val  = capital × half_kelly
     shares        = position_val / entry

     Half-Kelly is used because full Kelly maximises long-run growth but causes
     very large drawdowns that most traders cannot tolerate psychologically.

Hard cap: position value is capped at max_position_pct (default 10%) of capital
regardless of which formula is used.

Usage:
    from risk.position_sizer import compute_position_size

    result = compute_position_size(
        account_capital=500_000,
        entry_price=2_450.0,
        stop_loss_price=2_380.0,
    )
    # {'shares': 107, 'position_value_inr': 262150.0, 'risk_inr': 7490.0,
    #  'kelly_fraction': None, 'size_mode': 'fixed', ...}
"""

from __future__ import annotations
import math


# ── Constants ─────────────────────────────────────────────────────────────

DEFAULT_RISK_PCT       = 1.5    # % of capital to risk per trade
DEFAULT_MAX_POS_PCT    = 10.0   # hard cap: max % of capital in one position
MIN_HALF_KELLY         = 0.02   # floor for Kelly so we never bet < 2% of capital


# ── Main function ──────────────────────────────────────────────────────────

def compute_position_size(
    account_capital:    float,
    entry_price:        float,
    stop_loss_price:    float,
    risk_per_trade_pct: float = DEFAULT_RISK_PCT,
    mode:               str   = "fixed",   # "fixed" | "kelly"
    win_rate:           float = None,       # required for kelly mode (0–1)
    avg_rr:             float = None,       # required for kelly mode (e.g. 2.0)
    max_position_pct:   float = DEFAULT_MAX_POS_PCT,
) -> dict:
    """
    Calculate how many shares to buy for a single trade.

    Args:
        account_capital:    Total trading capital in INR.
        entry_price:        Planned entry price per share (INR).
        stop_loss_price:    Stop-loss price per share (INR).
        risk_per_trade_pct: % of capital to risk (default 1.5%).
        mode:               "fixed" (default) or "kelly".
        win_rate:           Historical win rate 0–1 (required for kelly mode).
        avg_rr:             Average risk-reward ratio (required for kelly mode).
        max_position_pct:   Hard cap on position size as % of capital (default 10%).

    Returns:
        {
          "shares":             int   — number of whole shares to buy,
          "position_value_inr": float — total capital deployed (shares × entry),
          "risk_inr":           float — max rupee risk (shares × risk_per_share),
          "risk_per_share":     float — entry − stop_loss,
          "pct_of_capital":     float — position_value / capital × 100,
          "kelly_fraction":     float | None — raw Kelly fraction (None if mode=fixed),
          "half_kelly":         float | None — half-Kelly fraction actually used,
          "size_mode":          str   — "fixed" or "kelly",
          "capped":             bool  — True if hard cap was applied,
          "error":              str | None — reason string for invalid inputs,
        }
    """
    base = {
        "shares":             0,
        "position_value_inr": 0.0,
        "risk_inr":           0.0,
        "risk_per_share":     0.0,
        "pct_of_capital":     0.0,
        "kelly_fraction":     None,
        "half_kelly":         None,
        "size_mode":          mode,
        "capped":             False,
        "error":              None,
    }

    # ── Edge case validation ───────────────────────────────────────────────
    if account_capital <= 0:
        base["error"] = "account_capital must be greater than 0"
        return base

    if entry_price <= 0:
        base["error"] = "entry_price must be greater than 0"
        return base

    if stop_loss_price <= 0:
        base["error"] = "stop_loss_price must be greater than 0"
        return base

    if stop_loss_price >= entry_price:
        base["error"] = (
            "stop_loss_price must be below entry_price — "
            f"got entry={entry_price}, sl={stop_loss_price}"
        )
        return base

    risk_per_share = entry_price - stop_loss_price
    base["risk_per_share"] = round(risk_per_share, 4)

    max_position_value = account_capital * (max_position_pct / 100.0)

    # ── Fixed Fractional ──────────────────────────────────────────────────
    if mode == "fixed" or mode not in ("fixed", "kelly"):
        rupee_risk = account_capital * (risk_per_trade_pct / 100.0)
        raw_shares = rupee_risk / risk_per_share
        shares = max(0, math.floor(raw_shares))

    # ── Kelly Criterion ───────────────────────────────────────────────────
    else:  # mode == "kelly"
        if win_rate is None or avg_rr is None:
            base["error"] = "win_rate and avg_rr are required for kelly mode"
            return base

        if not (0 < win_rate < 1):
            base["error"] = f"win_rate must be between 0 and 1, got {win_rate}"
            return base

        if avg_rr <= 0:
            base["error"] = f"avg_rr must be positive, got {avg_rr}"
            return base

        # Kelly formula: f = p - (1-p)/R
        kelly_f = win_rate - (1.0 - win_rate) / avg_rr
        base["kelly_fraction"] = round(kelly_f, 4)

        if kelly_f <= 0:
            # Negative Kelly = negative expected value — do not trade
            base["error"] = (
                f"Kelly fraction is {kelly_f:.4f} — negative EV at this "
                f"win_rate={win_rate}, avg_rr={avg_rr}. Skip this trade."
            )
            return base

        # Half-Kelly (industry standard for drawdown management)
        half_k = kelly_f / 2.0
        half_k = max(half_k, MIN_HALF_KELLY)   # floor at 2%
        base["half_kelly"] = round(half_k, 4)

        position_value_kelly = account_capital * half_k
        shares = max(0, math.floor(position_value_kelly / entry_price))

    if shares <= 0:
        base["error"] = "Computed 0 shares — risk or capital too small for this trade"
        return base

    position_value = shares * entry_price

    # ── Hard cap: 10% of capital ──────────────────────────────────────────
    capped = False
    if position_value > max_position_value:
        shares        = max(0, math.floor(max_position_value / entry_price))
        position_value = shares * entry_price
        capped        = True

    if shares <= 0:
        base["error"] = "0 shares after applying max position cap"
        return base

    risk_inr    = shares * risk_per_share
    pct_of_cap  = round(position_value / account_capital * 100, 2)

    return {
        "shares":             shares,
        "position_value_inr": round(position_value, 2),
        "risk_inr":           round(risk_inr, 2),
        "risk_per_share":     round(risk_per_share, 4),
        "pct_of_capital":     pct_of_cap,
        "kelly_fraction":     base["kelly_fraction"],
        "half_kelly":         base["half_kelly"],
        "size_mode":          mode,
        "capped":             capped,
        "error":              None,
    }
