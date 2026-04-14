"""
Pre-trade Liquidity Filter — risk/liquidity_filter.py

Runs BEFORE the AI probability filter (Step 2) to drop illiquid NSE stocks
early, saving compute time and protecting against slippage.

Three checks (all must pass for is_liquid=True):
  1. Average Daily Turnover ≥ min_adv_cr  (default ₹5 Cr/day over 20 days)
  2. Bid-ask spread proxy ≤ 0.5%          (estimated from daily high-low range)
  3. Promoter holding ≤ max_promoter_pct  (default 75%) — low float guard

Data source:
  - Volume + OHLC : equity_daily_candles_swing_trading (existing SQLite DB)
  - Promoter data : NSE shareholding JSON  (cached in data/shareholding.json)
                    Falls back gracefully if file is missing.

Usage:
    from risk.liquidity_filter import compute_liquidity, LiquidityFilter

    result = compute_liquidity(df, symbol="RELIANCE", close_price=2450.0)
    # {'liquidity_score': 82, 'is_liquid': True, 'adv_shares': 1_234_567,
    #  'adv_inr_cr': 30.2, 'spread_pct': 0.18, 'promoter_holding_pct': 46.0,
    #  'rejection_reason': None}

    # Bulk usage with a cached shareholding table:
    lf = LiquidityFilter(min_adv_cr=5.0, max_spread_pct=0.5, max_promoter_pct=75.0)
    result = lf.check(df, symbol="INFY", close_price=1800.0)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_MIN_ADV_CR      = 5.0    # minimum ₹ Crore average daily turnover
DEFAULT_MAX_SPREAD_PCT  = 0.5    # max estimated bid-ask spread as % of price
DEFAULT_MAX_PROMOTER    = 75.0   # skip if promoter holding > this %
ADV_WINDOW              = 20     # trading days for ADV calculation

# Path to optional shareholding data (loaded once, cached in-memory)
_SHAREHOLDING_FILE = Path(__file__).parent.parent / "data" / "shareholding.json"
_shareholding_cache: dict | None = None  # {symbol: promoter_holding_pct}


# ── Shareholding loader ───────────────────────────────────────────────────────

def _load_shareholding() -> dict:
    """Load promoter holding % from data/shareholding.json.

    File format (NSE-style, keyed by symbol):
        { "RELIANCE": 50.56, "INFY": 13.07, ... }

    Returns empty dict if file not found — filter degrades gracefully.
    """
    global _shareholding_cache
    if _shareholding_cache is not None:
        return _shareholding_cache

    if _SHAREHOLDING_FILE.exists():
        try:
            with open(_SHAREHOLDING_FILE) as f:
                data = json.load(f)
            # Accept both flat {sym: pct} and nested NSE bhav formats
            if isinstance(data, dict) and all(isinstance(v, (int, float)) for v in data.values()):
                _shareholding_cache = data
            else:
                # Try to flatten nested structure: {sym: {"promoter": pct, ...}}
                flat = {}
                for sym, val in data.items():
                    if isinstance(val, dict):
                        flat[sym] = float(val.get("promoter", val.get("promoter_holding", 0)) or 0)
                    elif isinstance(val, (int, float)):
                        flat[sym] = float(val)
                _shareholding_cache = flat
            logger.debug(f"Loaded shareholding data for {len(_shareholding_cache)} symbols")
        except Exception as e:
            logger.warning(f"Could not load shareholding file: {e}")
            _shareholding_cache = {}
    else:
        _shareholding_cache = {}

    return _shareholding_cache


# ── Core computation ──────────────────────────────────────────────────────────

def compute_liquidity(
    df:               pd.DataFrame,
    symbol:           str   = "",
    close_price:      float = 0.0,
    min_adv_cr:       float = DEFAULT_MIN_ADV_CR,
    max_spread_pct:   float = DEFAULT_MAX_SPREAD_PCT,
    max_promoter_pct: float = DEFAULT_MAX_PROMOTER,
) -> dict:
    """
    Compute liquidity metrics for a single stock.

    Args:
        df:               OHLCV DataFrame (columns: open, high, low, close, volume).
                          Must have at least ADV_WINDOW+1 rows.
        symbol:           NSE symbol (used for shareholding lookup).
        close_price:      Latest close/LTP; falls back to df["close"].iloc[-1] if 0.
        min_adv_cr:       Minimum 20-day average daily turnover in ₹ Crore.
        max_spread_pct:   Maximum acceptable bid-ask spread proxy (% of price).
        max_promoter_pct: Skip if promoter holding exceeds this %.

    Returns:
        {
          "liquidity_score":      int   (0–100),
          "is_liquid":            bool,
          "adv_shares":           int   — 20-day avg daily volume in shares,
          "adv_inr_cr":           float — 20-day avg daily turnover in ₹ Crore,
          "spread_pct":           float — estimated spread as % of close price,
          "promoter_holding_pct": float | None,
          "rejection_reason":     str | None,
        }
    """
    base = {
        "liquidity_score":      0,
        "is_liquid":            False,
        "adv_shares":           0,
        "adv_inr_cr":           0.0,
        "spread_pct":           0.0,
        "promoter_holding_pct": None,
        "rejection_reason":     None,
    }

    if df is None or df.empty:
        base["rejection_reason"] = "no price data"
        return base

    required = {"high", "low", "close", "volume"}
    if not required.issubset(df.columns):
        base["rejection_reason"] = f"missing columns: {required - set(df.columns)}"
        return base

    if len(df) < ADV_WINDOW:
        base["rejection_reason"] = f"insufficient history ({len(df)} rows < {ADV_WINDOW})"
        return base

    # ── 1. Average Daily Turnover ─────────────────────────────────────────
    window = df.iloc[-(ADV_WINDOW):].copy()
    try:
        daily_vols     = window["volume"].astype(float)
        daily_closes   = window["close"].astype(float)
        daily_turnover = daily_vols * daily_closes   # INR value per day

        adv_shares  = int(daily_vols.mean())
        adv_inr     = float(daily_turnover.mean())
        adv_inr_cr  = round(adv_inr / 1e7, 2)       # convert to Crore (1 Cr = 10M)
    except Exception as e:
        base["rejection_reason"] = f"volume calc error: {e}"
        return base

    base["adv_shares"] = adv_shares
    base["adv_inr_cr"] = adv_inr_cr

    # ── 2. Spread proxy (High–Low range as % of close) ────────────────────
    try:
        px = close_price if close_price > 0 else float(df["close"].iloc[-1])
        if px > 0:
            last_high  = float(df["high"].iloc[-1])
            last_low   = float(df["low"].iloc[-1])
            spread_pct = round((last_high - last_low) / px * 100, 4) if px > 0 else 0.0
        else:
            spread_pct = 0.0
    except Exception:
        spread_pct = 0.0

    base["spread_pct"] = spread_pct

    # ── 3. Promoter holding ───────────────────────────────────────────────
    shareholding   = _load_shareholding()
    promoter_pct   = shareholding.get(symbol)
    base["promoter_holding_pct"] = promoter_pct

    # ── Rejection checks ──────────────────────────────────────────────────
    if adv_inr_cr < min_adv_cr:
        base["rejection_reason"] = (
            f"low liquidity: ADV ₹{adv_inr_cr:.1f} Cr < min ₹{min_adv_cr:.1f} Cr"
        )
        return base

    if spread_pct > max_spread_pct:
        base["rejection_reason"] = (
            f"wide spread: {spread_pct:.2f}% > max {max_spread_pct:.2f}%"
        )
        return base

    if promoter_pct is not None and promoter_pct > max_promoter_pct:
        base["rejection_reason"] = (
            f"low float: promoter holding {promoter_pct:.1f}% > max {max_promoter_pct:.1f}%"
        )
        return base

    # ── Liquidity score (0–100) ───────────────────────────────────────────
    # Sub-scores weighted: turnover 50%, spread 30%, float 20%

    # Turnover sub-score: saturates at 10× the minimum
    adv_score  = min(adv_inr_cr / (min_adv_cr * 10.0), 1.0)          # 0–1

    # Spread sub-score: 0% spread = 100, max_spread_pct = 0
    spread_score = max(0.0, 1.0 - spread_pct / max_spread_pct)        # 0–1

    # Float sub-score: promoter ≤ 50% = full marks, scales down to max
    if promoter_pct is not None:
        float_score = max(0.0, 1.0 - max(0.0, promoter_pct - 50.0) / (max_promoter_pct - 50.0))
    else:
        float_score = 0.5   # neutral if unknown

    liquidity_score = int(round(
        adv_score    * 50.0
        + spread_score * 30.0
        + float_score  * 20.0
    ))

    base["liquidity_score"] = liquidity_score
    base["is_liquid"]       = True
    return base


# ── Reusable filter class ─────────────────────────────────────────────────────

class LiquidityFilter:
    """
    Stateless filter object — holds default thresholds so you don't have to
    pass them on every call.

        lf = LiquidityFilter(min_adv_cr=5.0)
        result = lf.check(df, symbol="INFY", close_price=1800.0)
    """

    def __init__(
        self,
        min_adv_cr:       float = DEFAULT_MIN_ADV_CR,
        max_spread_pct:   float = DEFAULT_MAX_SPREAD_PCT,
        max_promoter_pct: float = DEFAULT_MAX_PROMOTER,
    ):
        self.min_adv_cr       = min_adv_cr
        self.max_spread_pct   = max_spread_pct
        self.max_promoter_pct = max_promoter_pct

    def check(
        self,
        df:          pd.DataFrame,
        symbol:      str   = "",
        close_price: float = 0.0,
    ) -> dict:
        return compute_liquidity(
            df           = df,
            symbol       = symbol,
            close_price  = close_price,
            min_adv_cr       = self.min_adv_cr,
            max_spread_pct   = self.max_spread_pct,
            max_promoter_pct = self.max_promoter_pct,
        )
