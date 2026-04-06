"""
Technical indicators as pure functions operating on pandas Series/DataFrame.
All functions are vectorized using pandas for performance.
"""

import pandas as pd
import numpy as np


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Compute Relative Strength Index (RSI).

    Args:
        series: Close price series
        period: Lookback period (default 14)

    Returns:
        RSI Series (0–100)
    """
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def ema(series: pd.Series, span: int) -> pd.Series:
    """
    Compute Exponential Moving Average (EMA).

    Args:
        series: Price series
        span: EMA span

    Returns:
        EMA Series
    """
    return series.ewm(span=span, adjust=False).mean()


def sma(series: pd.Series, window: int) -> pd.Series:
    """
    Compute Simple Moving Average (SMA).

    Args:
        series: Price series
        window: Rolling window size

    Returns:
        SMA Series
    """
    return series.rolling(window=window).mean()


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9
) -> dict:
    """
    Compute MACD indicator.

    Args:
        series: Close price series
        fast: Fast EMA period (default 12)
        slow: Slow EMA period (default 26)
        signal: Signal line EMA period (default 9)

    Returns:
        dict with keys: macd (Series), signal (Series), histogram (Series)
    """
    fast_ema = series.ewm(span=fast, adjust=False).mean()
    slow_ema = series.ewm(span=slow, adjust=False).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return {
        "macd": macd_line,
        "signal": signal_line,
        "histogram": histogram,
    }


def bollinger_bands(
    series: pd.Series,
    window: int = 20,
    num_std: float = 2.0
) -> dict:
    """
    Compute Bollinger Bands.

    Args:
        series: Close price series
        window: Rolling window (default 20)
        num_std: Number of standard deviations (default 2)

    Returns:
        dict with keys: upper (Series), middle (Series), lower (Series)
    """
    middle = series.rolling(window=window).mean()
    std = series.rolling(window=window).std(ddof=0)
    upper = middle + num_std * std
    lower = middle - num_std * std
    return {
        "upper": upper,
        "middle": middle,
        "lower": lower,
    }


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Compute Average True Range (ATR).

    Expects columns: high, low, close.

    Args:
        df: DataFrame with OHLC data
        period: ATR period (default 14)

    Returns:
        ATR Series
    """
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.ewm(com=period - 1, min_periods=period).mean()


def stochastic(df: pd.DataFrame, period: int = 14, smooth: int = 3) -> dict:
    """
    Compute Stochastic Oscillator (%K and %D).

    Args:
        df: DataFrame with high, low, close columns
        period: Lookback period (default 14)
        smooth: Smoothing period for %D (default 3)

    Returns:
        dict with keys: k (Series), d (Series)
    """
    lowest_low   = df["low"].rolling(period).min()
    highest_high = df["high"].rolling(period).max()
    denom = (highest_high - lowest_low).replace(0, np.nan)
    k = 100 * (df["close"] - lowest_low) / denom
    d = k.rolling(smooth).mean()
    return {"k": k, "d": d}


def adx(df: pd.DataFrame, period: int = 14) -> dict:
    """
    Compute Average Directional Index (ADX) and Directional Indicators (+DI, -DI).

    Args:
        df: DataFrame with high, low, close columns
        period: ADX period (default 14)

    Returns:
        dict with keys: adx (Series), plus_di (Series), minus_di (Series)
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_high  = high.shift(1)
    prev_low   = low.shift(1)
    prev_close = close.shift(1)

    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)

    up_move   = high - prev_high
    down_move = prev_low - low

    plus_dm  = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)

    atr_s     = tr.ewm(com=period - 1, min_periods=period).mean()
    plus_di   = 100 * plus_dm.ewm(com=period - 1, min_periods=period).mean() / atr_s.replace(0, np.nan)
    minus_di  = 100 * minus_dm.ewm(com=period - 1, min_periods=period).mean() / atr_s.replace(0, np.nan)

    dx        = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_line  = dx.ewm(com=period - 1, min_periods=period).mean()

    return {"adx": adx_line, "plus_di": plus_di, "minus_di": minus_di}


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> dict:
    """
    Compute Supertrend indicator.

    Args:
        df: DataFrame with high, low, close columns
        period: ATR period (default 10)
        multiplier: ATR multiplier (default 3.0)

    Returns:
        dict with keys:
            trend (Series): 1 = uptrend, -1 = downtrend
            line  (Series): Supertrend value (support in uptrend, resistance in downtrend)
    """
    atr_val = atr(df, period)
    hl2     = (df["high"] + df["low"]) / 2
    upper   = hl2 + multiplier * atr_val
    lower   = hl2 - multiplier * atr_val

    trend_arr = [0]   * len(df)
    line_arr  = [0.0] * len(df)

    closes = df["close"].values
    upper_v = upper.values
    lower_v = lower.values

    for i in range(1, len(df)):
        if i < period:
            trend_arr[i] = 1
            line_arr[i]  = lower_v[i]
            continue

        pt = trend_arr[i - 1]
        ps = line_arr[i - 1]
        c  = closes[i]

        if pt == 1:
            new_lower = max(lower_v[i], ps)
            if c >= new_lower:
                trend_arr[i] = 1
                line_arr[i]  = new_lower
            else:
                trend_arr[i] = -1
                line_arr[i]  = upper_v[i]
        else:
            new_upper = min(upper_v[i], ps)
            if c <= new_upper:
                trend_arr[i] = -1
                line_arr[i]  = new_upper
            else:
                trend_arr[i] = 1
                line_arr[i]  = lower_v[i]

    return {
        "trend": pd.Series(trend_arr, index=df.index, dtype=int),
        "line":  pd.Series(line_arr,  index=df.index, dtype=float),
    }


def cci(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """
    Compute Commodity Channel Index (CCI).

    Args:
        df: DataFrame with high, low, close columns
        period: CCI period (default 20)

    Returns:
        CCI Series
    """
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    sma_tp = tp.rolling(period).mean()
    mad    = tp.rolling(period).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - sma_tp) / (0.015 * mad.replace(0, np.nan))


def keltner_channels(df: pd.DataFrame, period: int = 20, multiplier: float = 1.5) -> dict:
    """
    Compute Keltner Channels.

    Args:
        df: DataFrame with high, low, close columns
        period: EMA period (default 20)
        multiplier: ATR multiplier (default 1.5)

    Returns:
        dict with keys: upper (Series), middle (Series), lower (Series)
    """
    middle = df["close"].ewm(span=period, adjust=False).mean()
    atr_v  = atr(df, period)
    return {
        "upper":  middle + multiplier * atr_v,
        "middle": middle,
        "lower":  middle - multiplier * atr_v,
    }


def volume_surge(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """
    Compute volume surge ratio: current volume / rolling mean volume.

    Expects column: volume.

    Args:
        df: DataFrame with volume column
        window: Rolling window for average volume (default 20)

    Returns:
        Volume ratio Series (values > 1.0 mean above-average volume)
    """
    avg_vol = df["volume"].rolling(window=window).mean()
    return df["volume"] / avg_vol.replace(0, np.nan)
