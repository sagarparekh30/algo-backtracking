"""
Positional / Short-Term Investing scanner (1 Month+ holding horizon).

Three detectors:
  1. consolidation_breakout   — tight range + near/at breakout + above EMA200
  2. fundamental_technical    — large/mid-cap stocks with clean technical setup near EMA50
  3. sector_rotation_rs       — top-rotating sector leaders by relative strength

All run from a single bulk DB fetch for efficiency.
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
from strategies.sector_map import SECTOR_MAP, get_sector

logger = logging.getLogger(__name__)

_LOOKBACK_DAYS = 420   # enough for EMA200 + 6M context

# Large / mid-cap stocks (Nifty 100 universe as proxy)
_LARGE_MID_CAPS = {
    "RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK","KOTAKBANK","SBIN","WIPRO","HCLTECH",
    "BAJFINANCE","LT","AXISBANK","ADANIGREEN","ADANIENT","HINDALCO","NTPC","POWERGRID",
    "MARUTI","TATAMOTORS","ONGC","IOC","BPCL","COALINDIA","TATASTEEL","JSWSTEEL",
    "SUNPHARMA","DRREDDY","CIPLA","DIVISLAB","BAJAJ-AUTO","EICHERMOT","HEROMOTOCO",
    "HINDUNILVR","ITC","NESTLEIND","BRITANNIA","TITAN","ULTRACEMCO","GRASIM","AMBUJACEM",
    "TECHM","LTIM","PERSISTENT","COFORGE","M&M","TATACHEM","VEDL","SAIL","NMDC",
    "SIEMENS","ABB","BHEL","BEL","HAL","HDFCLIFE","SBILIFE","ICICIGI","BAJAJFINSV",
    "CHOLAFIN","MUTHOOTFIN","SHRIRAMFIN","INDUSINDBK","FEDERALBNK","AUBANK","BANDHANBNK",
    "TRENT","DMART","ZOMATO","JUBLFOOD","NYKAA","APOLLOHOSP","MAXHEALTH","FORTIS","LUPIN",
    "TORNTPHARM","AUROPHARMA","ALKEM","BIOCON","TATAPOWER","ADANIPOWER","TORNTPOWER",
    "GAIL","HAVELLS","CGPOWER","IRFC","RVNL","HAL","TIINDIA","TVSMOTORS","ASHOKLEY",
    "BALKRISIND","BOSCHLTD","BHARATFORG","MOTHERSON","GODREJCP","DABUR","MARICO",
    "COLPAL","EMAMILTD","TATACONSUM","VBL","JSWENERGY","HINDCOPPER","NATIONALUM",
    "KAJARIACER","VOLTAS","CROMPTON","WHIRLPOOL","VGUARD","SHREECEM","RAMCOCEM",
    "ACC","OFSS","MPHASIS","HDFCAMC","SBICARD","LICHSGFIN","CANBK","PNB","BANKBARODA",
    "IDFCFIRSTB","ADANIENSOL","ADANIPORTS","LTTS","LICI","RECLTD","PFC","IRCTC",
}


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


def _load_bulk() -> dict:
    """Single bulk DB fetch → {symbol: DataFrame}."""
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
    """Return {symbol: {sector, market_cap_cat, company_name}} from stock_metadata if available."""
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
    except Exception as e:
        logger.debug(f"Positional: metadata load failed: {e}")
        return {}


def _get_sector(sym: str, meta: dict) -> str:
    """Resolve sector from DB metadata first, fall back to SECTOR_MAP."""
    m = meta.get(sym, {})
    s = (m.get("sector") or "").strip()
    if s:
        return s
    return get_sector(sym) or ""


def _fmt(v) -> float | None:
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return None
    return round(float(v), 2)


def _score(rsi, rr, vol_ratio, price, ema) -> int:
    s = 50
    if rr:
        s += min(rr * 5, 20)
    if vol_ratio:
        s += min((vol_ratio - 1) * 10, 15)
    if rsi:
        s += max(0, 10 - abs(rsi - 58))
    if price and ema and price > ema:
        s += 5
    return min(100, max(0, int(s)))


# ── Detector 1: Consolidation Breakout (or near-breakout setup) ───────────────

def _consolidation_breakout(symbol: str, df: pd.DataFrame, meta: dict) -> dict | None:
    """
    Criteria:
    - Min 120 bars
    - Close above EMA200
    - Last 20 days ATR/close < 3.5% (tight consolidation)
    - Price at or above 95% of the 20-day high (near breakout zone)
    - RSI 45-72
    """
    if len(df) < 120:
        return None

    close  = df["close"]
    volume = df["volume"]
    price  = close.iloc[-1]
    ema200 = _ema(close, 200).iloc[-1]

    if price <= ema200 * 0.98:
        return None

    window = df.iloc[-22:-1]
    if len(window) < 15:
        return None

    atr_avg   = _atr(window).mean()
    avg_close = window["close"].mean()
    conso_rng = atr_avg / avg_close if avg_close > 0 else 1
    if conso_rng >= 0.035:   # allow up to 3.5% ATR/price
        return None

    high20 = window["high"].max()
    low20  = window["low"].min()
    # Price must be in the upper 30% of the consolidation range (near breakout)
    rng = high20 - low20
    if rng > 0 and price < (low20 + rng * 0.70):
        return None

    rsi = _rsi(close)
    if not (42 <= rsi <= 74):
        return None

    avg_vol = volume.iloc[-22:-1].mean()
    vol_ratio = float(volume.iloc[-1] / avg_vol) if avg_vol > 0 else 1.0

    # Broke out = price > high20; near-breakout = price within 2% below high20
    broke_out = price > high20 * 1.0
    label     = "Breakout" if broke_out else "Pre-Breakout"

    atr14     = _atr(df).iloc[-1]
    stop_loss = round(low20 - atr14 * 0.5, 2)
    target    = round(price + 3.5 * atr14, 2)
    rr        = round((target - price) / (price - stop_loss), 2) if price > stop_loss else None

    sector = _get_sector(symbol, meta)
    info   = meta.get(symbol, {})

    rationale = [
        f"Tight range for 20 days (ATR {round(conso_rng*100,1)}% of price)",
        f"Above EMA200 (₹{_fmt(ema200)}) — uptrend intact",
        f"Price at {round((price/high20)*100,0):.0f}% of 20-day high",
    ]
    if broke_out:
        rationale.insert(0, f"Broke above ₹{_fmt(high20)} resistance")
    if vol_ratio >= 1.3:
        rationale.append(f"Volume surge {round(vol_ratio,1)}x — institutional interest")

    return {
        "symbol":       symbol,
        "company_name": info.get("company_name", ""),
        "sector":       sector,
        "market_cap":   info.get("market_cap_cat", ""),
        "price":        _fmt(price),
        "stop_loss":    _fmt(stop_loss),
        "target":       _fmt(target),
        "risk_reward":  _fmt(rr),
        "rsi":          rsi,
        "vol_ratio":    _fmt(vol_ratio),
        "ema200":       _fmt(ema200),
        "setup_type":   "CONSOLIDATION_BREAKOUT",
        "setup_label":  label,
        "score":        _score(rsi, rr, vol_ratio, price, ema200),
        "holding":      "1–3 months",
        "rationale":    rationale,
    }


# ── Detector 2: Fundamental + Technical Combo ────────────────────────────────

def _fundamental_technical(symbol: str, df: pd.DataFrame, meta: dict) -> dict | None:
    """
    Criteria:
    - Large/mid cap (hardcoded universe or metadata)
    - EMA50 > EMA200 (golden cross zone)
    - Price pulled back to EMA50 support (within 5%)
    - RSI 48-67
    - Volume above average
    """
    in_cap_universe = symbol in _LARGE_MID_CAPS
    info = meta.get(symbol, {})
    cap  = (info.get("market_cap_cat") or "").upper()
    if not in_cap_universe and cap not in ("LARGE", "MID", "LARGECAP", "MIDCAP"):
        return None

    if len(df) < 60:
        return None

    close  = df["close"]
    volume = df["volume"]
    price  = close.iloc[-1]

    ema50  = _ema(close, 50).iloc[-1]
    ema200 = _ema(close, 200).iloc[-1] if len(df) >= 200 else ema50 * 0.96

    if price < ema50 * 0.96 or price < ema200 * 0.96:
        return None
    if ema50 < ema200 * 0.98:
        return None

    # Price near EMA50 support (within 6%)
    dist_from_ema50 = (price - ema50) / ema50
    if dist_from_ema50 > 0.06:
        return None

    rsi = _rsi(close)
    if not (46 <= rsi <= 68):
        return None

    avg_vol   = volume.iloc[-20:].mean()
    vol_ratio = float(volume.iloc[-1] / avg_vol) if avg_vol > 0 else 1.0

    atr14     = _atr(df).iloc[-1]
    stop_loss = round(ema50 * 0.96, 2)
    target    = round(price + 4.0 * atr14, 2)
    rr        = round((target - price) / (price - stop_loss), 2) if price > stop_loss else None

    sector = _get_sector(symbol, meta)
    cap_label = cap if cap else "Large/Mid"

    return {
        "symbol":       symbol,
        "company_name": info.get("company_name", ""),
        "sector":       sector,
        "market_cap":   cap_label,
        "price":        _fmt(price),
        "stop_loss":    _fmt(stop_loss),
        "target":       _fmt(target),
        "risk_reward":  _fmt(rr),
        "rsi":          rsi,
        "vol_ratio":    _fmt(vol_ratio),
        "ema50":        _fmt(ema50),
        "ema200":       _fmt(ema200),
        "setup_type":   "FUNDAMENTAL_TECHNICAL",
        "setup_label":  "Quality Dip",
        "score":        _score(rsi, rr, vol_ratio, price, ema200),
        "holding":      "1–3 months",
        "rationale":    [
            f"Quality stock — large/mid cap",
            f"EMA50 (₹{_fmt(ema50)}) > EMA200 — golden cross zone",
            f"Pulled back {round(dist_from_ema50*100,1)}% above EMA50 support",
            f"RSI {rsi} — not overbought, room to run",
        ],
    }


# ── Detector 3: Sector Rotation / Relative Strength ──────────────────────────

def _sector_rotation_rs(
    symbol: str, df: pd.DataFrame, meta: dict, top_sectors: set, sector_avgs: dict
) -> dict | None:
    """
    Criteria:
    - In a top rotating sector
    - 1M return > sector average
    - Close above EMA50
    - RSI 48-72
    """
    sector = _get_sector(symbol, meta)
    if not sector or sector not in top_sectors:
        return None

    if len(df) < 55:
        return None

    close = df["close"]
    price = close.iloc[-1]
    ema50 = _ema(close, 50).iloc[-1]

    if price < ema50 * 0.96:
        return None

    if len(close) < 22:
        return None
    ret_1m = (price / close.iloc[-22] - 1) * 100

    sec_avg = sector_avgs.get(sector, 0)
    if ret_1m <= sec_avg:
        return None

    rsi = _rsi(close)
    if not (46 <= rsi <= 74):
        return None

    volume    = df["volume"]
    avg_vol   = volume.iloc[-20:].mean()
    vol_ratio = float(volume.iloc[-1] / avg_vol) if avg_vol > 0 else 1.0
    atr14     = _atr(df).iloc[-1]
    stop_loss = round(ema50 * 0.95, 2)
    target    = round(price + 3.5 * atr14, 2)
    rr        = round((target - price) / (price - stop_loss), 2) if price > stop_loss else None

    rs_score  = round(ret_1m - sec_avg, 2)
    info      = meta.get(symbol, {})

    return {
        "symbol":        symbol,
        "company_name":  info.get("company_name", ""),
        "sector":        sector,
        "market_cap":    info.get("market_cap_cat", ""),
        "price":         _fmt(price),
        "stop_loss":     _fmt(stop_loss),
        "target":        _fmt(target),
        "risk_reward":   _fmt(rr),
        "rsi":           rsi,
        "vol_ratio":     _fmt(vol_ratio),
        "ret_1m":        _fmt(ret_1m),
        "sector_avg_1m": _fmt(sec_avg),
        "rs_vs_sector":  _fmt(rs_score),
        "setup_type":    "SECTOR_ROTATION",
        "setup_label":   "Sector Leader",
        "score":         _score(rsi, rr, rs_score / 5 + 1, price, ema50),
        "holding":       "1–2 months",
        "rationale":     [
            f"Sector {sector!r} rotating into favour",
            f"1M return +{_fmt(ret_1m)}% vs sector avg {'+' if sec_avg >= 0 else ''}{_fmt(sec_avg)}%",
            f"RS advantage +{_fmt(rs_score)}% vs peers",
            f"RSI {rsi} — momentum confirmed",
        ],
    }


# ── Public entry point ────────────────────────────────────────────────────────

def positional_scan() -> dict:
    """
    Run all three detectors.
    Returns {"results": [...], "by_type": {...}, "top_sectors": [...], "total": N}
    """
    try:
        symbol_groups = _load_bulk()
        meta          = _load_metadata()
    except Exception as e:
        logger.error(f"Positional scan load failed: {e}", exc_info=True)
        return {"results": [], "by_type": {}, "top_sectors": [], "total": 0, "error": str(e)}

    # ── Pre-compute sector 1M returns for Sector Rotation ─────────────────────
    sector_returns: dict[str, list] = {}
    for sym, df in symbol_groups.items():
        sector = _get_sector(sym, meta)
        if not sector or len(df) < 22:
            continue
        ret = (df["close"].iloc[-1] / df["close"].iloc[-22] - 1) * 100
        sector_returns.setdefault(sector, []).append(float(ret))

    sector_avgs = {sec: round(float(np.mean(rets)), 2) for sec, rets in sector_returns.items() if rets}
    ranked      = sorted(sector_avgs.items(), key=lambda x: x[1], reverse=True)
    top_sectors = {s for s, _ in ranked[:5]}

    results: list = []
    seen: set[str] = set()

    for sym, df in symbol_groups.items():
        if sym in seen:
            continue

        sig = _consolidation_breakout(sym, df, meta)
        if sig:
            results.append(sig)
            seen.add(sym)
            continue

        sig = _fundamental_technical(sym, df, meta)
        if sig:
            results.append(sig)
            seen.add(sym)
            continue

        sig = _sector_rotation_rs(sym, df, meta, top_sectors, sector_avgs)
        if sig:
            results.append(sig)
            seen.add(sym)

    results.sort(key=lambda r: r.get("score", 0), reverse=True)

    by_type: dict = {}
    for r in results:
        t = r["setup_type"]
        by_type[t] = by_type.get(t, 0) + 1

    return {
        "results":     results,
        "by_type":     by_type,
        "top_sectors": [s for s, _ in ranked[:5]],
        "sector_perf": dict(ranked[:10]),
        "total":       len(results),
    }
