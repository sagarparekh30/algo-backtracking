"""
Market Regime Detection — purely from your own price database.

Computes three breadth/volatility metrics across all symbols:
  breadth_pct   : % of stocks currently above their SMA50 (0–100)
  avg_atr_norm  : median normalised ATR across all stocks (volatility proxy)
  regime        : Bull / Neutral / Bear

No external API needed — derived entirely from the SQLite DB.
"""

import os
import sys
import logging

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

from config.settings import TABLE_NAME
from db.connection import get_engine

logger = logging.getLogger(__name__)

# Regime thresholds
BULL_BREADTH  = 60   # ≥ 60% above SMA50  → Bull
BEAR_BREADTH  = 40   # ≤ 40% above SMA50  → Bear
HIGH_VOL_ATR  = 0.025  # avg ATR/price ≥ 2.5% → high volatility


def compute_regime() -> dict:
    """
    Compute current market regime from the database.

    Returns:
        dict with keys:
            regime          : "Bull" | "Neutral" | "Bear"
            breadth_pct     : float — % of stocks above SMA50
            above_sma50     : int   — count above SMA50
            below_sma50     : int   — count below SMA50
            avg_atr_pct     : float — median ATR as % of price (volatility)
            volatility_label: "High" | "Normal" | "Low"
            symbols_checked : int
            as_of_date      : str   — most recent trade date in DB
    """
    try:
        engine = get_engine()
        # Single bulk query — last 300 trading days for all symbols at once.
        # Replaces 2092 individual per-symbol queries.
        df_all = pd.read_sql(
            f"""
            SELECT symbol, trade_date, high, low, close
            FROM {TABLE_NAME}
            WHERE trade_date >= CURRENT_DATE - INTERVAL '420 days'
            ORDER BY symbol, trade_date ASC
            """,
            engine,
        )

        above, below = 0, 0
        atr_ratios = []
        as_of_date = "N/A"

        for sym, df in df_all.groupby("symbol", sort=False):
            try:
                df = df.reset_index(drop=True)
                if len(df) < 55:
                    continue

                close = df["close"]
                high  = df["high"]
                low   = df["low"]

                # SMA50
                sma50_val = close.rolling(50).mean().iloc[-1]
                last_close = float(close.iloc[-1])
                if last_close > sma50_val:
                    above += 1
                else:
                    below += 1

                # ATR (simplified: mean of TR over last 14 bars)
                prev_close = close.shift(1)
                tr = pd.concat([
                    high - low,
                    (high - prev_close).abs(),
                    (low  - prev_close).abs(),
                ], axis=1).max(axis=1)
                atr14 = tr.rolling(14).mean().iloc[-1]
                if last_close > 0:
                    atr_ratios.append(atr14 / last_close)

                if as_of_date == "N/A":
                    as_of_date = str(df["trade_date"].iloc[-1])

            except Exception:
                continue

        total = above + below
        breadth_pct = round((above / total * 100) if total > 0 else 0, 1)
        avg_atr_pct = round(float(np.median(atr_ratios) * 100) if atr_ratios else 0, 2)

        # Regime label
        if breadth_pct >= BULL_BREADTH:
            regime = "Bull"
        elif breadth_pct <= BEAR_BREADTH:
            regime = "Bear"
        else:
            regime = "Neutral"

        # Volatility label
        if avg_atr_pct >= HIGH_VOL_ATR * 100:
            vol_label = "High"
        elif avg_atr_pct < 1.0:
            vol_label = "Low"
        else:
            vol_label = "Normal"

        return {
            "regime":           regime,
            "breadth_pct":      breadth_pct,
            "above_sma50":      above,
            "below_sma50":      below,
            "avg_atr_pct":      avg_atr_pct,
            "volatility_label": vol_label,
            "symbols_checked":  total,
            "as_of_date":       as_of_date,
        }

    except Exception as e:
        logger.error(f"compute_regime error: {e}")
        return {
            "regime": "Unknown", "breadth_pct": 0, "above_sma50": 0,
            "below_sma50": 0, "avg_atr_pct": 0, "volatility_label": "Unknown",
            "symbols_checked": 0, "as_of_date": "N/A",
            "error": str(e),
        }
