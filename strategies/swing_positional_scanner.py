"""
Swing Trading scanner (1 week – few weeks holding horizon).

Three detectors:
  1. trend_following     — Higher High / Higher Low structure + EMA20 support
  2. sr_bounce           — Price pulling back to key support, starting to bounce
  3. ma_crossover        — 20 EMA recently crossed above 50 EMA (fresh signal)

Single bulk DB fetch, in-memory per-symbol processing.
"""

import os
import sys
import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

from config.settings import TABLE_NAME
from db.connection import get_engine
from strategies.sector_map import get_sector

logger = logging.getLogger(__name__)

_LOOKBACK_DAYS = 200   # ~140 trading days — enough for EMA50 + context


# ── helpers ───────────────────────────────────────────────────────────────────

def _ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift(1)).abs()
    lpc = (df["low"]  - df["close"].shift(1)).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()


def _rsi(series: pd.Series, n: int = 14) -> float:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(span=n, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=n, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - 100 / (1 + rs)
    return round(float(rsi.iloc[-1]), 1) if not rsi.empty else 50.0


def _fmt(v) -> float | None:
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return None
    return round(float(v), 2)


def _score(rsi, rr, vol_ratio, ema_aligned: bool) -> int:
    s = 50
    if rr:
        s += min(rr * 5, 20)
    if vol_ratio:
        s += min((vol_ratio - 1) * 10, 15)
    if rsi:
        s += max(0, 10 - abs(rsi - 57))
    if ema_aligned:
        s += 5
    return min(100, max(0, int(s)))


def _load_bulk() -> dict:
    engine = get_engine()
    cutoff = date.today() - timedelta(days=_LOOKBACK_DAYS)
    df_all = pd.read_sql(
        f"""
        SELECT symbol, trade_date, open, high, low, close, volume
        FROM {TABLE_NAME}
        WHERE trade_date >= %(cutoff)s
        ORDER BY symbol, trade_date ASC
        """,
        engine,
        params={"cutoff": cutoff},
    )
    if df_all.empty:
        return {}
    for col in ("open", "high", "low", "close", "volume"):
        df_all[col] = pd.to_numeric(df_all[col], errors="coerce")
    df_all["trade_date"] = pd.to_datetime(df_all["trade_date"])
    return {sym: grp.reset_index(drop=True) for sym, grp in df_all.groupby("symbol", sort=False)}


def _load_metadata() -> dict:
    try:
        from db.connection import get_conn
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, company_name, sector, market_cap_cat "
                "FROM stock_metadata WHERE symbol IS NOT NULL"
            )
            rows = cur.fetchall()
        conn.close()
        return {r[0]: {"company_name": r[1], "sector": r[2], "market_cap_cat": r[3]} for r in rows}
    except Exception:
        return {}


def _resolve_sector(sym: str, meta: dict) -> str:
    s = (meta.get(sym, {}).get("sector") or "").strip()
    return s if s else (get_sector(sym) or "")


# ── Detector 1: Trend Following (HH–HL) ──────────────────────────────────────

def _trend_following(symbol: str, df: pd.DataFrame, meta: dict) -> dict | None:
    """
    Higher High / Higher Low structure.
    - Look at pivots over last 30 bars split into two halves
    - Recent half's max high > earlier half's max high  (Higher High)
    - Recent half's min low  > earlier half's min low   (Higher Low)
    - Close above EMA20 (price riding the trend)
    - EMA20 > EMA50 (trend confirmed on both timeframes)
    - RSI 50–68 (trending, not overbought)
    - Price within 4% of EMA20 (entry near trend support)
    """
    if len(df) < 55:
        return None

    close  = df["close"]
    price  = close.iloc[-1]
    ema20  = _ema(close, 20).iloc[-1]
    ema50  = _ema(close, 50).iloc[-1]

    if price < ema20 * 0.97:
        return None
    if ema20 < ema50 * 0.99:
        return None

    # Higher High / Higher Low check over last 30 bars
    recent = df.iloc[-15:]
    prev   = df.iloc[-30:-15]
    if len(recent) < 10 or len(prev) < 10:
        return None

    hh = recent["high"].max() > prev["high"].max() * 1.005
    hl = recent["low"].min()  > prev["low"].min()  * 1.005
    if not (hh and hl):
        return None

    rsi = _rsi(close)
    if not (48 <= rsi <= 70):
        return None

    # Entry near EMA20 (not too extended)
    dist = (price - ema20) / ema20
    if dist > 0.04:
        return None

    volume    = df["volume"]
    avg_vol   = volume.iloc[-20:].mean()
    vol_ratio = float(volume.iloc[-1] / avg_vol) if avg_vol > 0 else 1.0

    atr14     = _atr(df).iloc[-1]
    stop_loss = round(ema20 * 0.97, 2)
    target    = round(price + 2.5 * atr14, 2)
    rr        = round((target - price) / (price - stop_loss), 2) if price > stop_loss else None

    info = meta.get(symbol, {})
    return {
        "symbol":       symbol,
        "company_name": info.get("company_name", ""),
        "sector":       _resolve_sector(symbol, meta),
        "market_cap":   info.get("market_cap_cat", ""),
        "price":        _fmt(price),
        "stop_loss":    _fmt(stop_loss),
        "target":       _fmt(target),
        "risk_reward":  _fmt(rr),
        "rsi":          rsi,
        "vol_ratio":    _fmt(vol_ratio),
        "ema20":        _fmt(ema20),
        "ema50":        _fmt(ema50),
        "setup_type":   "TREND_FOLLOWING",
        "setup_label":  "Trend Ride",
        "score":        _score(rsi, rr, vol_ratio, True),
        "holding":      "1–3 weeks",
        "rationale":    [
            f"Higher High ({_fmt(recent['high'].max())}) + Higher Low confirmed",
            f"EMA20 ({_fmt(ema20)}) > EMA50 ({_fmt(ema50)}) — dual trend",
            f"Price {round(dist*100,1)}% above EMA20 — near support entry",
            f"RSI {rsi} — trending, not overbought",
        ],
    }


# ── Detector 2: Support & Resistance Bounce ───────────────────────────────────

def _sr_bounce(symbol: str, df: pd.DataFrame, meta: dict) -> dict | None:
    """
    Price has pulled back to a key support zone and is bouncing.
    - Identify support: average of last 3 significant swing lows (60-bar window)
    - Price within 2.5% of support from above
    - RSI 38–55 and rising (coming off oversold, not still falling)
    - EMA50 not in a severe downtrend (price > EMA50 * 0.95)
    - Volume picking up (today >= 0.9x avg — confirmation developing)
    """
    if len(df) < 60:
        return None

    close  = df["close"]
    price  = close.iloc[-1]
    ema50  = _ema(close, 50).iloc[-1]

    if price < ema50 * 0.93:
        return None

    # Find swing lows: local minima in last 60 bars (window of 5)
    lows = df["low"].iloc[-60:].values
    swing_lows = []
    for i in range(2, len(lows) - 2):
        if lows[i] == min(lows[i-2:i+3]):
            swing_lows.append(lows[i])

    if len(swing_lows) < 2:
        return None

    # Support = median of the lowest 3 swing lows
    swing_lows.sort()
    support = float(np.median(swing_lows[:min(3, len(swing_lows))]))

    dist_to_support = (price - support) / support
    if not (-0.005 <= dist_to_support <= 0.025):
        return None

    rsi = _rsi(close)
    if not (36 <= rsi <= 57):
        return None

    # RSI must be rising (today > 3 bars ago) — bounce confirmation
    rsi_series = 100 - 100 / (1 + (
        close.diff().clip(lower=0).ewm(span=14, adjust=False).mean() /
        (-close.diff().clip(upper=0)).ewm(span=14, adjust=False).mean().replace(0, np.nan)
    ))
    if len(rsi_series) >= 4 and rsi_series.iloc[-1] < rsi_series.iloc[-4]:
        return None

    volume    = df["volume"]
    avg_vol   = volume.iloc[-20:].mean()
    vol_ratio = float(volume.iloc[-1] / avg_vol) if avg_vol > 0 else 1.0

    atr14     = _atr(df).iloc[-1]
    stop_loss = round(support * 0.975, 2)
    target    = round(price + 3.0 * atr14, 2)
    rr        = round((target - price) / (price - stop_loss), 2) if price > stop_loss else None

    info = meta.get(symbol, {})
    return {
        "symbol":       symbol,
        "company_name": info.get("company_name", ""),
        "sector":       _resolve_sector(symbol, meta),
        "market_cap":   info.get("market_cap_cat", ""),
        "price":        _fmt(price),
        "stop_loss":    _fmt(stop_loss),
        "target":       _fmt(target),
        "risk_reward":  _fmt(rr),
        "rsi":          rsi,
        "vol_ratio":    _fmt(vol_ratio),
        "support":      _fmt(support),
        "ema50":        _fmt(ema50),
        "setup_type":   "SR_BOUNCE",
        "setup_label":  "S/R Bounce",
        "score":        _score(rsi, rr, vol_ratio, price > ema50),
        "holding":      "1–2 weeks",
        "rationale":    [
            f"Price at support zone ₹{_fmt(support)} ({round(dist_to_support*100,1)}% above)",
            f"RSI {rsi} bouncing — oversold recovery",
            f"Stop below support at ₹{_fmt(stop_loss)}",
            f"EMA50 at ₹{_fmt(ema50)} — macro structure intact",
        ],
    }


# ── Detector 3: MA Crossover (20 EMA / 50 EMA) ───────────────────────────────

def _ma_crossover(symbol: str, df: pd.DataFrame, meta: dict) -> dict | None:
    """
    20 EMA crossed above 50 EMA recently (within last 12 bars) = fresh signal.
    OR 20 EMA is above 50 EMA and price just reclaimed 20 EMA from below.
    - Price above both EMAs
    - RSI 50–68
    - Not too far extended (within 5% of 20 EMA)
    """
    if len(df) < 55:
        return None

    close = df["close"]
    price = close.iloc[-1]

    ema20_series = _ema(close, 20)
    ema50_series = _ema(close, 50)

    ema20 = ema20_series.iloc[-1]
    ema50 = ema50_series.iloc[-1]

    if price < ema20 * 0.97 or price < ema50 * 0.97:
        return None
    if ema20 < ema50:
        return None

    # Check for fresh crossover: 20 EMA crossed above 50 EMA within last 12 bars
    cross_window = min(12, len(df) - 1)
    crossed_recently = False
    for i in range(1, cross_window + 1):
        if ema20_series.iloc[-i-1] <= ema50_series.iloc[-i-1] and ema20_series.iloc[-i] > ema50_series.iloc[-i]:
            crossed_recently = True
            bars_ago = i
            break

    # Also allow: 20>50 and price recently reclaimed 20 EMA (price was below ema20 within last 5 bars)
    price_reclaimed = False
    if not crossed_recently and len(close) >= 6:
        was_below = any(close.iloc[-6:-1].values < ema20_series.iloc[-6:-1].values * 1.005)
        price_reclaimed = was_below and price >= ema20 * 0.99

    if not (crossed_recently or price_reclaimed):
        return None

    rsi = _rsi(close)
    if not (48 <= rsi <= 70):
        return None

    dist = (price - ema20) / ema20
    if dist > 0.05:
        return None

    volume    = df["volume"]
    avg_vol   = volume.iloc[-20:].mean()
    vol_ratio = float(volume.iloc[-1] / avg_vol) if avg_vol > 0 else 1.0

    atr14     = _atr(df).iloc[-1]
    stop_loss = round(ema50 * 0.975, 2)
    target    = round(price + 2.5 * atr14, 2)
    rr        = round((target - price) / (price - stop_loss), 2) if price > stop_loss else None

    if crossed_recently:
        signal_desc = f"20 EMA crossed above 50 EMA {bars_ago} bar{'s' if bars_ago > 1 else ''} ago"
        label = "EMA Crossover"
    else:
        signal_desc = "Price reclaimed 20 EMA — momentum resuming"
        label = "EMA Reclaim"

    info = meta.get(symbol, {})
    return {
        "symbol":       symbol,
        "company_name": info.get("company_name", ""),
        "sector":       _resolve_sector(symbol, meta),
        "market_cap":   info.get("market_cap_cat", ""),
        "price":        _fmt(price),
        "stop_loss":    _fmt(stop_loss),
        "target":       _fmt(target),
        "risk_reward":  _fmt(rr),
        "rsi":          rsi,
        "vol_ratio":    _fmt(vol_ratio),
        "ema20":        _fmt(ema20),
        "ema50":        _fmt(ema50),
        "setup_type":   "MA_CROSSOVER",
        "setup_label":  label,
        "score":        _score(rsi, rr, vol_ratio, True),
        "holding":      "1–3 weeks",
        "rationale":    [
            signal_desc,
            f"EMA20 ({_fmt(ema20)}) > EMA50 ({_fmt(ema50)}) — trend shift",
            f"RSI {rsi} — momentum building",
            f"Stop at EMA50 ({_fmt(stop_loss)})",
        ],
    }


# ── Public entry point ────────────────────────────────────────────────────────

def swing_scan() -> dict:
    """
    Run all three swing detectors.
    Returns {"results": [...], "by_type": {...}, "total": N}
    """
    try:
        symbol_groups = _load_bulk()
        meta          = _load_metadata()
    except Exception as e:
        logger.error(f"Swing scan load failed: {e}", exc_info=True)
        return {"results": [], "by_type": {}, "total": 0, "error": str(e)}

    results: list  = []
    seen: set[str] = set()

    for sym, df in symbol_groups.items():
        if sym in seen:
            continue

        sig = _ma_crossover(sym, df, meta)
        if sig:
            results.append(sig)
            seen.add(sym)
            continue

        sig = _trend_following(sym, df, meta)
        if sig:
            results.append(sig)
            seen.add(sym)
            continue

        sig = _sr_bounce(sym, df, meta)
        if sig:
            results.append(sig)
            seen.add(sym)

    results.sort(key=lambda r: r.get("score", 0), reverse=True)

    by_type: dict = {}
    for r in results:
        t = r["setup_type"]
        by_type[t] = by_type.get(t, 0) + 1

    return {
        "results": results,
        "by_type": by_type,
        "total":   len(results),
    }
