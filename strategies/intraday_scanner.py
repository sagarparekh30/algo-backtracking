"""
strategies/intraday_scanner.py
===============================
Intraday trade setups generated from daily candle data.

Since we have daily OHLCV data, each setup uses yesterday's candles to
project today's likely price behaviour:

  GAP_UP / GAP_DOWN   — stocks gapping significantly at open
  ORB_BREAKOUT        — Opening Range Breakout above yesterday's high
  MOMENTUM            — strong trending stock with tight pullback entry
  REVERSAL            — oversold/overbought at key support/resistance
  VWAP_BOUNCE         — expected VWAP level re-test after morning move
  BREAKOUT            — consolidation breakout with volume confirmation

Each signal includes:
  entry, target1, target2, stop_loss, risk_reward, confidence,
  setup_type, rationale, timeframe, ml_prob
"""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
_MIN_PRICE         = 20       # skip penny stocks below ₹20
_MIN_AVG_VOLUME    = 50_000   # minimum avg daily volume for liquidity
_TARGET1_MULT      = 1.0      # target1 = 1× ATR from entry
_TARGET2_MULT      = 1.8      # target2 = 1.8× ATR from entry
_STOP_MULT         = 0.6      # stop = 0.6× ATR below entry
_MIN_RR            = 1.5      # skip setups where R:R < 1.5
_LOOKBACK          = 60       # candle lookback for indicators


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    """Average True Range over last `period` candles."""
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain  = delta.where(delta > 0, 0.0).ewm(com=period - 1, min_periods=period).mean()
    loss  = (-delta.where(delta < 0, 0.0)).ewm(com=period - 1, min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return float((100 - (100 / (1 + rs))).iloc[-1])


def _confidence(ml_prob: Optional[float], tech_score: int) -> int:
    """Composite confidence 0-100."""
    ml_part   = (ml_prob or 0.5) * 50           # 0-50 from ML
    tech_part = min(tech_score, 5) / 5 * 50     # 0-50 from tech signals
    return round(ml_part + tech_part)


# ── Individual setup detectors ─────────────────────────────────────────────────

def _gap_play(df: pd.DataFrame, sym: str, atr_val: float) -> Optional[dict]:
    """
    Gap-and-Go / Gap-Fill.
    Triggered when today's open gaps > 1% from yesterday's close.
    """
    if len(df) < 3:
        return None
    prev_close = float(df["close"].iloc[-2])
    last_close = float(df["close"].iloc[-1])
    last_open  = float(df["open"].iloc[-1])
    gap_pct    = (last_open - prev_close) / prev_close * 100

    if abs(gap_pct) < 0.8:
        return None

    avg_vol = float(df["volume"].iloc[-20:].mean())
    last_vol = float(df["volume"].iloc[-1])
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    if gap_pct > 0:
        # Gap Up → momentum continuation if volume confirms
        entry   = round(last_close * 1.002, 2)          # slight buffer above close
        stop    = round(last_close - atr_val * _STOP_MULT, 2)
        t1      = round(entry + atr_val * _TARGET1_MULT, 2)
        t2      = round(entry + atr_val * _TARGET2_MULT, 2)
        rr      = round((t1 - entry) / max(entry - stop, 0.01), 2)
        if rr < _MIN_RR:
            return None
        tech = sum([
            gap_pct > 1.5,
            vol_ratio > 1.5,
            last_close > float(_ema(df["close"], 20).iloc[-1]),
            last_close > float(_ema(df["close"], 50).iloc[-1]),
        ])
        return {
            "setup_type": "GAP_UP",
            "setup_label": "Gap Up",
            "entry": entry, "stop_loss": stop,
            "target1": t1, "target2": t2, "risk_reward": rr,
            "gap_pct": round(gap_pct, 2), "vol_ratio": round(vol_ratio, 1),
            "tech_score": tech,
            "timeframe": "Morning (9:15–11:30)",
            "rationale": [
                f"Gapped up {gap_pct:.1f}% from prev close ₹{prev_close:.1f}",
                f"Volume {vol_ratio:.1f}× average — {'strong' if vol_ratio > 1.5 else 'moderate'} participation",
                "Buy above previous close; stop at 0.6× ATR below entry",
            ],
        }
    else:
        # Gap Down → fade/reversal if oversold
        rsi_val = _rsi(df["close"])
        if rsi_val > 45:
            return None          # not oversold enough for reversal
        entry   = round(last_close * 0.998, 2)
        stop    = round(last_close - atr_val * _STOP_MULT * 1.2, 2)
        t1      = round(entry + atr_val * _TARGET1_MULT * 0.8, 2)
        t2      = round(entry + atr_val * _TARGET2_MULT * 0.8, 2)
        rr      = round((t1 - entry) / max(entry - stop, 0.01), 2)
        if rr < _MIN_RR:
            return None
        tech = sum([
            abs(gap_pct) > 1.5,
            rsi_val < 35,
            last_close > float(_ema(df["close"], 200).iloc[-1]) * 0.95,
        ])
        return {
            "setup_type": "GAP_DOWN_REVERSAL",
            "setup_label": "Gap Down Reversal",
            "entry": entry, "stop_loss": stop,
            "target1": t1, "target2": t2, "risk_reward": rr,
            "gap_pct": round(gap_pct, 2), "vol_ratio": round(vol_ratio, 1),
            "tech_score": tech,
            "timeframe": "Mid-morning (10:00–12:00)",
            "rationale": [
                f"Gapped down {abs(gap_pct):.1f}% — potential gap fill",
                f"RSI {rsi_val:.0f} — oversold, reversal setup",
                "Enter near support; stop below gap-down low",
            ],
        }


def _orb_breakout(df: pd.DataFrame, sym: str, atr_val: float) -> Optional[dict]:
    """
    Opening Range Breakout (ORB).
    Yesterday's high becomes the ORB level for today.
    Buy a break above yesterday's high with volume confirmation.
    """
    if len(df) < 5:
        return None
    yesterday_high  = float(df["high"].iloc[-1])
    yesterday_low   = float(df["low"].iloc[-1])
    yesterday_close = float(df["close"].iloc[-1])
    prev_high_5d    = float(df["high"].iloc[-6:-1].max())

    # Only if yesterday's high is within range of recent highs (not extended)
    if yesterday_close > yesterday_high * 0.97:
        # Price closed near high — strong close, ORB applies
        ema20 = float(_ema(df["close"], 20).iloc[-1])
        ema50 = float(_ema(df["close"], 50).iloc[-1])
        rsi_val = _rsi(df["close"])

        if yesterday_close < ema50 * 0.95:
            return None  # too far below trend

        entry  = round(yesterday_high * 1.003, 2)      # 0.3% above yesterday's high
        stop   = round(yesterday_low, 2)               # below yesterday's low
        risk   = entry - stop
        if risk <= 0 or risk > atr_val * 3:
            return None

        t1 = round(entry + atr_val * _TARGET1_MULT, 2)
        t2 = round(entry + atr_val * _TARGET2_MULT, 2)
        rr = round((t1 - entry) / max(risk, 0.01), 2)
        if rr < _MIN_RR:
            return None

        avg_vol   = float(df["volume"].iloc[-20:].mean())
        last_vol  = float(df["volume"].iloc[-1])
        vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

        tech = sum([
            rsi_val > 50,
            yesterday_close > ema20,
            yesterday_close > ema50,
            vol_ratio > 1.2,
        ])
        return {
            "setup_type": "ORB_BREAKOUT",
            "setup_label": "ORB Breakout",
            "entry": entry, "stop_loss": stop,
            "target1": t1, "target2": t2, "risk_reward": rr,
            "orb_level": yesterday_high, "vol_ratio": round(vol_ratio, 1),
            "tech_score": tech,
            "timeframe": "Morning (9:15–11:00)",
            "rationale": [
                f"ORB level: ₹{yesterday_high:.1f} (yesterday's high)",
                f"Strong close {(yesterday_close/yesterday_high*100):.0f}% of range — momentum intact",
                "Buy break above ORB; stop below yesterday's low",
            ],
        }
    return None


def _momentum_play(df: pd.DataFrame, sym: str, atr_val: float) -> Optional[dict]:
    """
    Momentum continuation — stock in strong uptrend, pulled back slightly.
    """
    if len(df) < 21:
        return None

    closes  = df["close"]
    ema20   = float(_ema(closes, 20).iloc[-1])
    ema50   = float(_ema(closes, 50).iloc[-1])
    ema200  = float(_ema(closes, 200).iloc[-1])
    rsi_val = _rsi(closes)
    last    = float(closes.iloc[-1])
    prev    = float(closes.iloc[-2])

    # Trend structure: EMA20 > EMA50 > EMA200
    if not (ema20 > ema50 > ema200 * 0.98):
        return None
    # RSI in momentum zone (50-65)
    if not (48 < rsi_val < 68):
        return None
    # Price pulled back to EMA20 zone
    if not (ema20 * 0.99 < last < ema20 * 1.04):
        return None

    avg_vol   = float(df["volume"].iloc[-20:].mean())
    last_vol  = float(df["volume"].iloc[-1])
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    entry  = round(last * 1.002, 2)
    stop   = round(ema50 * 0.99, 2)
    t1     = round(entry + atr_val * _TARGET1_MULT, 2)
    t2     = round(entry + atr_val * _TARGET2_MULT, 2)
    rr     = round((t1 - entry) / max(entry - stop, 0.01), 2)
    if rr < _MIN_RR or stop >= entry:
        return None

    tech = sum([
        rsi_val > 52,
        vol_ratio > 1.0,
        last > ema200,
        last > ema50,
    ])
    ret_5d = (last / float(closes.iloc[-6]) - 1) * 100 if len(closes) >= 6 else 0

    return {
        "setup_type": "MOMENTUM",
        "setup_label": "Momentum",
        "entry": entry, "stop_loss": stop,
        "target1": t1, "target2": t2, "risk_reward": rr,
        "ema20": round(ema20, 2), "vol_ratio": round(vol_ratio, 1),
        "tech_score": tech,
        "timeframe": "Any time (9:15–3:00)",
        "rationale": [
            f"EMA20 > EMA50 > EMA200 — full trend alignment",
            f"Price pulled back to EMA20 zone at ₹{ema20:.1f} — buy-the-dip",
            f"5-day return {ret_5d:+.1f}% · RSI {rsi_val:.0f} — not overbought",
        ],
    }


def _reversal_play(df: pd.DataFrame, sym: str, atr_val: float) -> Optional[dict]:
    """
    Mean-reversion reversal at key support.
    """
    if len(df) < 21:
        return None

    closes  = df["close"]
    lows    = df["low"]
    highs   = df["high"]
    rsi_val = _rsi(closes)
    last    = float(closes.iloc[-1])
    ema200  = float(_ema(closes, 200).iloc[-1])
    ema50   = float(_ema(closes, 50).iloc[-1])

    # Oversold at support
    if rsi_val > 38:
        return None
    # Not in structural breakdown (> 20% below EMA200)
    if last < ema200 * 0.80:
        return None
    # Near 20-day low support zone
    low_20d = float(lows.iloc[-20:].min())
    if last > low_20d * 1.04:
        return None  # not close enough to support

    # Bullish candle formation check (close > open)
    last_open = float(df["open"].iloc[-1])
    if last < last_open:  # bearish candle — wait for reversal confirmation
        return None

    avg_vol   = float(df["volume"].iloc[-20:].mean())
    last_vol  = float(df["volume"].iloc[-1])
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    entry  = round(last * 1.003, 2)
    stop   = round(low_20d * 0.99, 2)
    t1     = round(entry + atr_val * _TARGET1_MULT * 0.8, 2)
    t2     = round(entry + atr_val * _TARGET2_MULT * 0.8, 2)
    rr     = round((t1 - entry) / max(entry - stop, 0.01), 2)
    if rr < _MIN_RR or stop >= entry:
        return None

    tech = sum([
        rsi_val < 32,
        vol_ratio > 1.2,
        last > ema200 * 0.92,
        last_open < last,  # bullish close
    ])

    return {
        "setup_type": "REVERSAL",
        "setup_label": "Oversold Reversal",
        "entry": entry, "stop_loss": stop,
        "target1": t1, "target2": t2, "risk_reward": rr,
        "support": round(low_20d, 2), "vol_ratio": round(vol_ratio, 1),
        "tech_score": tech,
        "timeframe": "Mid-day (10:30–2:00)",
        "rationale": [
            f"RSI {rsi_val:.0f} — deeply oversold, rebound likely",
            f"20-day support at ₹{low_20d:.1f} — strong floor",
            f"Bullish close (close > open) — buying interest returning",
        ],
    }


def _vwap_bounce(df: pd.DataFrame, sym: str, atr_val: float) -> Optional[dict]:
    """
    VWAP re-test bounce — intraday support.
    Approximate VWAP using typical price weighted by volume over 5 days.
    """
    if len(df) < 6:
        return None

    last5 = df.iloc[-5:]
    tp    = (last5["high"] + last5["low"] + last5["close"]) / 3
    vwap_approx = float((tp * last5["volume"]).sum() / last5["volume"].sum())

    last = float(df["close"].iloc[-1])

    # Price pulled back to near VWAP
    dist_pct = (last - vwap_approx) / vwap_approx * 100
    if not (-1.5 < dist_pct < 1.0):
        return None  # not near VWAP

    rsi_val = _rsi(df["close"])
    ema200  = float(_ema(df["close"], 200).iloc[-1])
    if last < ema200 * 0.95:
        return None  # below trend

    avg_vol   = float(df["volume"].iloc[-20:].mean())
    last_vol  = float(df["volume"].iloc[-1])
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    entry  = round(vwap_approx * 1.001, 2)
    stop   = round(vwap_approx - atr_val * 0.5, 2)
    t1     = round(entry + atr_val * _TARGET1_MULT * 0.8, 2)
    t2     = round(entry + atr_val * _TARGET2_MULT * 0.8, 2)
    rr     = round((t1 - entry) / max(entry - stop, 0.01), 2)
    if rr < _MIN_RR or stop >= entry:
        return None

    tech = sum([
        40 < rsi_val < 65,
        vol_ratio > 1.0,
        last > ema200,
        abs(dist_pct) < 0.8,
    ])

    return {
        "setup_type": "VWAP_BOUNCE",
        "setup_label": "VWAP Bounce",
        "entry": entry, "stop_loss": stop,
        "target1": t1, "target2": t2, "risk_reward": rr,
        "vwap": round(vwap_approx, 2), "vol_ratio": round(vol_ratio, 1),
        "tech_score": tech,
        "timeframe": "Morning (9:30–12:00)",
        "rationale": [
            f"Price near approx. VWAP ₹{vwap_approx:.1f} — institutional support level",
            f"RSI {rsi_val:.0f} — neutral, not overbought",
            "VWAP acts as intraday support — buy bounce, stop below VWAP",
        ],
    }


def _breakout_play(df: pd.DataFrame, sym: str, atr_val: float) -> Optional[dict]:
    """
    Consolidation breakout — price breaks above a tight range with volume.
    """
    if len(df) < 25:
        return None

    closes  = df["close"]
    highs   = df["high"]
    last    = float(closes.iloc[-1])
    ema200  = float(_ema(closes, 200).iloc[-1])
    rsi_val = _rsi(closes)

    if last < ema200 * 0.96:
        return None

    # Consolidation: last 5 days' range tight (< 2× ATR)
    range_5d = float(highs.iloc[-6:-1].max()) - float(df["low"].iloc[-6:-1].min())
    if range_5d > atr_val * 2.5:
        return None  # not tight enough

    # Breakout: today's close > 5-day high
    high_5d = float(highs.iloc[-6:-1].max())
    if last <= high_5d:
        return None

    avg_vol   = float(df["volume"].iloc[-20:].mean())
    last_vol  = float(df["volume"].iloc[-1])
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    if vol_ratio < 1.3:
        return None  # no volume confirmation

    entry  = round(last * 1.002, 2)
    stop   = round(high_5d * 0.995, 2)
    t1     = round(entry + atr_val * _TARGET1_MULT, 2)
    t2     = round(entry + atr_val * _TARGET2_MULT, 2)
    rr     = round((t1 - entry) / max(entry - stop, 0.01), 2)
    if rr < _MIN_RR or stop >= entry:
        return None

    tech = sum([
        rsi_val > 52,
        vol_ratio > 1.5,
        last > ema200,
        range_5d < atr_val * 1.8,
    ])

    return {
        "setup_type": "BREAKOUT",
        "setup_label": "Consolidation Breakout",
        "entry": entry, "stop_loss": stop,
        "target1": t1, "target2": t2, "risk_reward": rr,
        "breakout_level": round(high_5d, 2), "vol_ratio": round(vol_ratio, 1),
        "tech_score": tech,
        "timeframe": "Morning (9:15–11:00)",
        "rationale": [
            f"Breaking above 5-day consolidation at ₹{high_5d:.1f}",
            f"Volume {vol_ratio:.1f}× average — institutional participation",
            "Tight range breakout — low risk, high reward setup",
        ],
    }


# ── Main scan ─────────────────────────────────────────────────────────────────

def intraday_scan(symbols: list[str] = None, ml_probs: dict = None) -> list[dict]:
    """
    Scan all (or given) symbols and return intraday trade setups.

    Args:
        symbols:   Optional list to restrict scan
        ml_probs:  Optional {symbol: prob_5d} from ML predictor

    Returns:
        List of setup dicts, sorted by confidence desc.
    """
    from db.connection import get_engine
    from config.settings import TABLE_NAME

    ml_probs = ml_probs or {}

    engine = get_engine()
    try:
        sym_filter = ""
        params     = {}
        if symbols:
            sym_filter = "AND symbol = ANY(%(syms)s)"
            params["syms"] = symbols

        df_all = pd.read_sql(
            f"""
            SELECT symbol, trade_date, open, high, low, close, volume
            FROM {TABLE_NAME}
            WHERE trade_date >= CURRENT_DATE - INTERVAL '{_LOOKBACK} days'
            {sym_filter}
            ORDER BY symbol, trade_date ASC
            """,
            engine,
            params=params or None,
        )
    except Exception as e:
        logger.error(f"Intraday scan DB error: {e}")
        return []

    if df_all.empty:
        return []

    # Get metadata
    meta_map: dict[str, dict] = {}
    try:
        from db.connection import get_conn
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT symbol, sector, company_name FROM stock_metadata WHERE is_active")
            for sym, sec, name in cur.fetchall():
                meta_map[sym] = {"sector": sec or "—", "company_name": name or sym}
        conn.close()
    except Exception:
        pass

    results = []

    for sym, grp in df_all.groupby("symbol", sort=False):
        grp = grp.reset_index(drop=True)
        if len(grp) < 10:
            continue

        last_price = float(grp["close"].iloc[-1])
        if last_price < _MIN_PRICE:
            continue

        avg_vol = float(grp["volume"].iloc[-20:].mean()) if len(grp) >= 20 else float(grp["volume"].mean())
        if avg_vol < _MIN_AVG_VOLUME:
            continue

        try:
            atr_val = _atr(grp)
            if not atr_val or math.isnan(atr_val) or atr_val <= 0:
                continue
        except Exception:
            continue

        # Run all detectors; take the highest-scoring one per symbol
        setup = None
        for detector in [_orb_breakout, _gap_play, _momentum_play, _breakout_play, _vwap_bounce, _reversal_play]:
            try:
                s = detector(grp, sym, atr_val)
                if s and (setup is None or s["tech_score"] > setup["tech_score"]):
                    setup = s
            except Exception as e:
                logger.debug(f"{sym} detector error: {e}")

        if not setup:
            continue

        ml_prob  = ml_probs.get(sym)
        conf     = _confidence(ml_prob, setup["tech_score"])
        m        = meta_map.get(sym, {})
        atr_pct  = round(atr_val / last_price * 100, 2)

        results.append({
            "symbol":       sym,
            "company_name": m.get("company_name", sym),
            "sector":       m.get("sector", "—"),
            "last_price":   round(last_price, 2),
            "atr":          round(atr_val, 2),
            "atr_pct":      atr_pct,
            "avg_volume":   int(avg_vol),
            **setup,
            "ml_prob":      round(ml_prob * 100, 1) if ml_prob is not None else None,
            "confidence":   conf,
        })

    # Sort: confidence desc, then risk/reward desc
    results.sort(key=lambda r: (-r["confidence"], -r.get("risk_reward", 0)))
    return results
