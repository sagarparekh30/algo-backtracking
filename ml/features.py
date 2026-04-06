"""
Feature engineering for ML price prediction.

Extracts 26 technical indicator features from OHLCV data.
All features are normalised (price-scale independent) so the model
generalises across different price levels.
"""

import numpy as np
import pandas as pd
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.indicators import (
    rsi, ema, sma, macd, bollinger_bands, atr,
    stochastic, adx, volume_surge,
)

# Ordered list — must stay in sync with compute_features() output
FEATURE_NAMES = [
    # Momentum / returns
    "ret_1d", "ret_5d", "ret_10d", "ret_20d",
    # RSI
    "rsi_14",
    # MACD
    "macd_hist_norm", "macd_line_norm",
    # Bollinger Bands
    "bb_position", "bb_width",
    # Volatility
    "atr_norm",
    # Volume
    "vol_ratio",
    # Stochastic
    "stoch_k", "stoch_d",
    # ADX / Directional
    "adx_val", "plus_di", "minus_di",
    # EMA ribbon distances (% from close)
    "dist_ema8", "dist_ema21", "dist_ema55",
    # SMA trend distances
    "dist_sma50", "dist_sma200",
    # Candle anatomy
    "body_ratio", "upper_wick_ratio", "lower_wick_ratio",
    # Intraday range metrics
    "hi_lo_range_norm", "close_vs_high",
]


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute ML feature matrix for every bar in df.

    Args:
        df: OHLCV DataFrame with columns open, high, low, close, volume.
            Index can be anything; rows must be sorted oldest → newest.

    Returns:
        DataFrame with columns = FEATURE_NAMES, same index as df.
        Rows with insufficient lookback will contain NaN.
    """
    f = pd.DataFrame(index=df.index)

    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    open_  = df["open"]

    # ── Momentum / returns ─────────────────────────────────────────────
    f["ret_1d"]  = close.pct_change(1)
    f["ret_5d"]  = close.pct_change(5)
    f["ret_10d"] = close.pct_change(10)
    f["ret_20d"] = close.pct_change(20)

    # ── RSI (0 → 1) ────────────────────────────────────────────────────
    f["rsi_14"] = rsi(close, 14) / 100.0

    # ── MACD (normalised by close price) ───────────────────────────────
    md = macd(close, fast=12, slow=26, signal=9)
    f["macd_hist_norm"] = md["histogram"] / close
    f["macd_line_norm"] = md["macd"]      / close

    # ── Bollinger Bands ─────────────────────────────────────────────────
    bb       = bollinger_bands(close, window=20, num_std=2)
    bb_range = (bb["upper"] - bb["lower"]).replace(0, np.nan)
    f["bb_position"] = (close - bb["lower"]) / bb_range   # 0 = lower, 1 = upper
    f["bb_width"]    = bb_range / close                    # band width as % of price

    # ── ATR (normalised) ────────────────────────────────────────────────
    f["atr_norm"] = atr(df, 14) / close

    # ── Volume surge ratio ──────────────────────────────────────────────
    vol = volume_surge(df, window=20)
    f["vol_ratio"] = vol.clip(upper=10)   # cap extreme spikes

    # ── Stochastic (0 → 1) ─────────────────────────────────────────────
    st = stochastic(df, period=14, smooth=3)
    f["stoch_k"] = st["k"] / 100.0
    f["stoch_d"] = st["d"] / 100.0

    # ── ADX / Directional indicators (0 → 1) ───────────────────────────
    adx_data = adx(df, period=14)
    f["adx_val"]  = adx_data["adx"]      / 100.0
    f["plus_di"]  = adx_data["plus_di"]  / 100.0
    f["minus_di"] = adx_data["minus_di"] / 100.0

    # ── EMA ribbon (% distance from close) ─────────────────────────────
    f["dist_ema8"]   = (close - ema(close, 8))   / close
    f["dist_ema21"]  = (close - ema(close, 21))  / close
    f["dist_ema55"]  = (close - ema(close, 55))  / close

    # ── SMA trend distances ─────────────────────────────────────────────
    f["dist_sma50"]  = (close - sma(close, 50))  / close
    f["dist_sma200"] = (close - sma(close, 200)) / close

    # ── Candle anatomy ──────────────────────────────────────────────────
    hi_body    = pd.concat([close, open_], axis=1).max(axis=1)
    lo_body    = pd.concat([close, open_], axis=1).min(axis=1)
    body       = (close - open_).abs()
    total_rng  = (high - low).replace(0, np.nan)

    f["body_ratio"]       = body / total_rng
    f["upper_wick_ratio"] = (high - hi_body) / total_rng
    f["lower_wick_ratio"] = (lo_body - low)  / total_rng

    # ── Intraday range metrics ──────────────────────────────────────────
    f["hi_lo_range_norm"] = total_rng / close
    f["close_vs_high"]    = (high - close) / total_rng   # 0 = closed at high

    return f[FEATURE_NAMES]
