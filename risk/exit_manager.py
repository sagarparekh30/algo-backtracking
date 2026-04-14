"""
Dynamic Exit Manager — risk/exit_manager.py

Replaces the static SL + single-target system with adaptive exit logic.

Four exit rules evaluated in priority order each day:
  1. Volatility expansion exit — ATR doubled vs entry → tighten SL to 0.5× ATR
  2. Tiered profit booking      — at T1 (1.5× R), exit 40%; trail remainder
  3. Trailing SL update         — once price moves 1.5× ATR in favour, move SL
                                   to breakeven; after 2.5× ATR, trail at 1 ATR
                                   below highest close
  4. Time-based stop            — if not moved 0.5× ATR in 5 days → full exit

Usage:
    from risk.exit_manager import evaluate_exit, process_open_trades

    trade = {
        "symbol":          "RELIANCE",
        "entry_price":     2450.0,
        "original_sl":     2380.0,
        "current_sl":      2380.0,   # may have been updated from previous days
        "atr_at_entry":    35.0,
        "current_price":   2530.0,
        "current_atr":     40.0,     # today's ATR (optional, defaults to atr_at_entry)
        "highest_close":   2535.0,   # highest close since entry
        "days_held":       3,
        "position_size":   50,       # shares held
        "partial_exited":  False,    # True once T1 partial exit has happened
    }
    result = evaluate_exit(trade)
    # {"action": "partial_exit", "new_sl": 2450.0, "exit_qty": 20,
    #  "exit_price": 2530.0, "reason": "target1_partial"}
"""

from __future__ import annotations

import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

BREAKEVEN_TRIGGER_ATR  = 1.5   # ATR multiples above entry → move SL to breakeven
TRAIL_TRIGGER_ATR      = 2.5   # ATR multiples above entry → start trailing SL
TRAIL_SL_ATR           = 1.0   # trail SL = highest_close − TRAIL_SL_ATR × ATR
T1_MULTIPLE            = 1.5   # R multiples for Target 1
T1_EXIT_PCT            = 0.40  # exit 40% at T1
TIME_STOP_DAYS         = 5     # max days without meaningful move
TIME_STOP_ATR          = 0.5   # minimum favourable move to stay in (× ATR)
VOL_EXPANSION_FACTOR   = 2.0   # ATR doubled vs entry = volatility spike
VOL_TIGHT_SL_ATR       = 0.5   # tighten SL to this × current ATR on vol expansion


# ── Main evaluation ───────────────────────────────────────────────────────────

def evaluate_exit(trade: dict) -> dict:
    """
    Evaluate exit logic for a single open trade.

    Required keys in `trade`:
        entry_price    float  — trade entry price
        original_sl    float  — original stop-loss (used to compute R)
        current_sl     float  — current stop-loss (may be updated from prior days)
        atr_at_entry   float  — ATR on the day of entry
        current_price  float  — latest close price
        days_held      int    — calendar/trading days since entry

    Optional keys:
        current_atr    float  — today's ATR; defaults to atr_at_entry
        highest_close  float  — highest close since entry; defaults to current_price
        position_size  int    — total shares; used to compute exit_qty
        partial_exited bool   — True if T1 partial exit already taken

    Returns:
        {
          "action":      "hold" | "update_sl" | "partial_exit" | "full_exit",
          "new_sl":      float,           — updated SL price
          "exit_qty":    int,             — shares to exit (0 for hold/update_sl)
          "exit_price":  float | None,    — suggested exit price
          "reason":      str,             — human-readable reason code
        }
    """
    entry          = float(trade["entry_price"])
    original_sl    = float(trade["original_sl"])
    current_sl     = float(trade.get("current_sl", original_sl))
    atr_entry      = float(trade["atr_at_entry"])
    current_price  = float(trade["current_price"])
    days_held      = int(trade.get("days_held", 0))
    current_atr    = float(trade.get("current_atr", atr_entry))
    highest_close  = float(trade.get("highest_close", current_price))
    position_size  = int(trade.get("position_size", 0))
    partial_exited = bool(trade.get("partial_exited", False))

    risk_per_share = entry - original_sl   # R (positive for long)
    if risk_per_share <= 0:
        return _make(action="hold", new_sl=current_sl, exit_qty=0,
                     exit_price=None, reason="invalid_risk_per_share")

    upside = current_price - entry         # positive = trade in profit

    # ── Rule 1: Volatility expansion exit ────────────────────────────────
    if current_atr > 0 and current_atr >= VOL_EXPANSION_FACTOR * atr_entry:
        tight_sl = round(current_price - VOL_TIGHT_SL_ATR * current_atr, 2)
        new_sl   = max(current_sl, tight_sl)   # never lower our SL
        if current_price <= new_sl:
            return _make("full_exit", new_sl=new_sl, exit_qty=position_size,
                         exit_price=current_price, reason="vol_expansion_sl_hit")
        return _make("update_sl", new_sl=new_sl, exit_qty=0,
                     exit_price=None, reason="vol_expansion_tighten")

    # ── Rule 2: Tiered profit booking (T1) ───────────────────────────────
    t1_price = entry + T1_MULTIPLE * risk_per_share
    if not partial_exited and current_price >= t1_price:
        exit_qty = max(1, math.floor(position_size * T1_EXIT_PCT))
        # Also move SL to breakeven
        new_sl = max(current_sl, entry)
        return _make("partial_exit", new_sl=new_sl, exit_qty=exit_qty,
                     exit_price=current_price, reason="target1_partial")

    # ── Rule 3: Trailing SL update ────────────────────────────────────────
    if upside >= TRAIL_TRIGGER_ATR * atr_entry:
        # Full trail: SL = highest_close − 1 ATR
        trail_sl = round(highest_close - TRAIL_SL_ATR * current_atr, 2)
        new_sl   = max(current_sl, trail_sl)
        if current_price <= new_sl:
            return _make("full_exit", new_sl=new_sl, exit_qty=position_size,
                         exit_price=current_price, reason="trailing_sl_hit")
        if new_sl > current_sl:
            return _make("update_sl", new_sl=new_sl, exit_qty=0,
                         exit_price=None, reason="trailing_sl_raised")

    elif upside >= BREAKEVEN_TRIGGER_ATR * atr_entry:
        # Move SL to breakeven
        new_sl = max(current_sl, entry)
        if current_price <= new_sl:
            return _make("full_exit", new_sl=new_sl, exit_qty=position_size,
                         exit_price=current_price, reason="breakeven_sl_hit")
        if new_sl > current_sl:
            return _make("update_sl", new_sl=new_sl, exit_qty=0,
                         exit_price=None, reason="moved_to_breakeven")

    # ── Check if current SL is hit ────────────────────────────────────────
    if current_price <= current_sl:
        return _make("full_exit", new_sl=current_sl, exit_qty=position_size,
                     exit_price=current_price, reason="sl_hit")

    # ── Rule 4: Time-based stop ───────────────────────────────────────────
    if days_held >= TIME_STOP_DAYS:
        min_move = TIME_STOP_ATR * atr_entry
        if upside < min_move:
            return _make("full_exit", new_sl=current_sl, exit_qty=position_size,
                         exit_price=current_price, reason="time_stop")

    return _make("hold", new_sl=current_sl, exit_qty=0,
                 exit_price=None, reason="hold")


# ── Batch processor ───────────────────────────────────────────────────────────

def process_open_trades(open_trades: list[dict]) -> list[dict]:
    """
    Evaluate exit logic for a list of open trades.

    Each trade dict is augmented in-place with a "exit_decision" key
    containing the evaluate_exit() result.

    Args:
        open_trades: List of trade dicts (see evaluate_exit() for required keys).

    Returns:
        The same list, with "exit_decision" added to each trade.
    """
    for trade in open_trades:
        try:
            decision = evaluate_exit(trade)
        except Exception as e:
            logger.warning(f"Exit eval error for {trade.get('symbol', '?')}: {e}")
            decision = _make("hold", new_sl=trade.get("current_sl", 0),
                             exit_qty=0, exit_price=None,
                             reason=f"eval_error: {e}")
        trade["exit_decision"] = decision

    return open_trades


# ── Helper ────────────────────────────────────────────────────────────────────

def _make(
    action:     str,
    new_sl:     float,
    exit_qty:   int,
    exit_price: Optional[float],
    reason:     str,
) -> dict:
    return {
        "action":     action,
        "new_sl":     round(new_sl, 2),
        "exit_qty":   exit_qty,
        "exit_price": round(exit_price, 2) if exit_price is not None else None,
        "reason":     reason,
    }
