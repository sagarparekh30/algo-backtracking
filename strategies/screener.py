"""
Stock screener — four specialised scans for swing trading:

  1. relative_strength_scan()  — RS vs Nifty 50 index over 20/50 days
  2. fiftytwo_week_scan()       — Stocks near 52-week highs (breakout candidates)
  3. volume_spike_scan()        — Unusual volume (institutional accumulation signal)
  4. sector_heatmap()           — Sector-wise 1D / 1W / 1M performance
  5. earnings_calendar()        — Upcoming earnings dates via yfinance

All scans read from the local DB — no live API calls required.
"""

import os
import sys
import logging
from datetime import date, timedelta

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

from config.settings import TABLE_NAME
from db.connection import get_engine
from strategies.sector_map import SECTOR_MAP, get_sector

logger = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────────────────

def _load_all(days: int = 300, symbols: list = None) -> pd.DataFrame:
    """Load recent OHLCV for all (or specified) symbols from DB."""
    engine = get_engine()
    cutoff = date.today() - timedelta(days=days)
    if symbols:
        placeholders = ",".join(f"'{s}'" for s in symbols)
        sql = f"""
            SELECT symbol, trade_date, open, high, low, close, volume
            FROM {TABLE_NAME}
            WHERE trade_date >= %(cutoff)s AND symbol IN ({placeholders})
            ORDER BY symbol, trade_date ASC
        """
    else:
        sql = f"""
            SELECT symbol, trade_date, open, high, low, close, volume
            FROM {TABLE_NAME}
            WHERE trade_date >= %(cutoff)s
            ORDER BY symbol, trade_date ASC
        """
    df = pd.read_sql(sql, engine, params={"cutoff": cutoff})
    df["close"]  = pd.to_numeric(df["close"],  errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["high"]   = pd.to_numeric(df["high"],   errors="coerce")
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def _returns(series: pd.Series, n: int) -> float:
    """N-day return % of a close-price series (last n bars)."""
    if len(series) < n + 1:
        return None
    return round((series.iloc[-1] / series.iloc[-(n+1)] - 1) * 100, 2)


# ── 1. Relative Strength Scan ─────────────────────────────────────────────

def relative_strength_scan(symbols: list = None) -> list:
    """
    Rank all stocks by Relative Strength vs the universe average.

    RS_20  = stock 20-day return  / average 20-day return of all stocks in scope
    RS_50  = stock 50-day return  / average 50-day return of all stocks in scope

    Returns list sorted by RS_20 desc — leaders first.
    """
    try:
        df = _load_all(days=90, symbols=symbols)
        results = []

        # Universe returns (proxy for index performance)
        sym_returns_20, sym_returns_50 = {}, {}
        for sym, grp in df.groupby("symbol"):
            closes = grp["close"]
            r20 = _returns(closes, 20)
            r50 = _returns(closes, 50)
            if r20 is not None: sym_returns_20[sym] = r20
            if r50 is not None: sym_returns_50[sym] = r50

        avg_20 = float(np.mean(list(sym_returns_20.values()))) if sym_returns_20 else 0
        avg_50 = float(np.mean(list(sym_returns_50.values()))) if sym_returns_50 else 0

        for sym, r20 in sym_returns_20.items():
            r50  = sym_returns_50.get(sym)
            grp  = df[df["symbol"] == sym]
            last = grp.iloc[-1]

            rs20 = round(r20 / avg_20, 2) if avg_20 != 0 else None
            rs50 = round(r50 / avg_50, 2) if (avg_50 != 0 and r50 is not None) else None

            results.append({
                "symbol":      sym,
                "sector":      get_sector(sym),
                "last_price":  round(float(last["close"]), 2),
                "return_20d":  r20,
                "return_50d":  r50,
                "rs_20":       rs20,
                "rs_50":       rs50,
                "rs_grade":    _rs_grade(rs20),
                "last_date":   str(last["trade_date"].date()),
            })

        results.sort(key=lambda x: (x["rs_20"] or 0), reverse=True)
        return results

    except Exception as e:
        logger.error(f"RS scan error: {e}")
        return []


def _rs_grade(rs: float) -> str:
    if rs is None:       return "—"
    if rs >= 1.5:        return "A+"
    if rs >= 1.2:        return "A"
    if rs >= 1.0:        return "B"
    if rs >= 0.8:        return "C"
    return "D"


# ── 2. 52-Week High Scan ──────────────────────────────────────────────────

def fiftytwo_week_scan(symbols: list = None) -> list:
    """
    Find stocks trading near or at 52-week highs.
    Breakouts above 52W high with volume = strongest momentum signal.

    Returns list sorted by % distance from 52W high (closest first).
    """
    try:
        df = _load_all(days=260, symbols=symbols)   # ~52 weeks of trading days
        results = []

        for sym, grp in df.groupby("symbol"):
            if len(grp) < 50:
                continue

            high_52w = float(grp["high"].max())
            low_52w  = float(grp["low"].min())
            last     = grp.iloc[-1]
            close    = float(last["close"])

            pct_from_high = round((close / high_52w - 1) * 100, 2)
            pct_from_low  = round((close / low_52w  - 1) * 100, 2)

            # Volume confirmation: today vs 20-day avg
            recent_vol = grp["volume"].iloc[-20:].mean()
            today_vol  = float(last["volume"])
            vol_ratio  = round(today_vol / recent_vol, 1) if recent_vol > 0 else None

            # Breakout flag: within 2% of 52W high with above-avg volume
            is_breakout = bool(pct_from_high >= -2.0 and (vol_ratio or 0) >= 1.2)

            results.append({
                "symbol":         sym,
                "sector":         get_sector(sym),
                "last_price":     round(close, 2),
                "high_52w":       round(high_52w, 2),
                "low_52w":        round(low_52w, 2),
                "pct_from_high":  pct_from_high,
                "pct_from_low":   round(pct_from_low, 2),
                "vol_ratio":      vol_ratio,
                "is_breakout":    is_breakout,
                "last_date":      str(last["trade_date"].date()),
            })

        # Sort: breakouts first, then by proximity to 52W high
        results.sort(key=lambda x: (not x["is_breakout"], x["pct_from_high"]), reverse=False)
        return results

    except Exception as e:
        logger.error(f"52W scan error: {e}")
        return []


# ── 3. Volume Spike Scan ──────────────────────────────────────────────────

def volume_spike_scan(min_multiplier: float = 2.0, symbols: list = None) -> list:
    """
    Find stocks with unusual volume in the last session.
    High volume + price up = accumulation (bullish).
    High volume + price down = distribution (bearish — avoid).

    Args:
        min_multiplier: Minimum volume vs 20-day average (default: 2x).
        symbols: Optional list to restrict scan to specific stocks.
    """
    try:
        df = _load_all(days=60, symbols=symbols)
        results = []

        for sym, grp in df.groupby("symbol"):
            if len(grp) < 22:
                continue

            # Use last 20 bars excluding today as baseline
            baseline = grp.iloc[-21:-1]["volume"].mean()
            last     = grp.iloc[-1]
            prev     = grp.iloc[-2]

            today_vol = float(last["volume"])
            ratio     = round(today_vol / baseline, 1) if baseline > 0 else 0

            if ratio < min_multiplier:
                continue

            close    = float(last["close"])
            prev_cls = float(prev["close"])
            chg_pct  = round((close / prev_cls - 1) * 100, 2)

            # Classify signal
            if chg_pct >= 1.0:
                signal = "Accumulation"
            elif chg_pct <= -1.0:
                signal = "Distribution"
            else:
                signal = "Neutral"

            results.append({
                "symbol":      sym,
                "sector":      get_sector(sym),
                "last_price":  round(close, 2),
                "change_pct":  chg_pct,
                "vol_ratio":   ratio,
                "vol_today":   int(today_vol),
                "vol_avg_20d": int(baseline),
                "signal":      signal,
                "last_date":   str(last["trade_date"].date()),
            })

        results.sort(key=lambda x: x["vol_ratio"], reverse=True)
        return results

    except Exception as e:
        logger.error(f"Volume spike scan error: {e}")
        return []


# ── 4. Sector Heatmap ─────────────────────────────────────────────────────

def sector_heatmap() -> list:
    """
    Calculate performance for each sector over 1D / 1W / 1M / 3M periods.
    Returns list of sectors with avg return and top/bottom performers.
    """
    try:
        df = _load_all(days=100)
        sector_data: dict = {}

        for sym, grp in df.groupby("symbol"):
            sector = get_sector(sym)
            closes = grp["close"]

            r1d  = _returns(closes, 1)
            r5d  = _returns(closes, 5)
            r20d = _returns(closes, 20)
            r60d = _returns(closes, 60)

            last_price = round(float(closes.iloc[-1]), 2)

            entry = {
                "symbol": sym, "last_price": last_price,
                "r1d": r1d, "r5d": r5d, "r20d": r20d, "r60d": r60d,
            }
            sector_data.setdefault(sector, []).append(entry)

        results = []
        for sector, stocks in sector_data.items():
            def avg_ret(key):
                vals = [s[key] for s in stocks if s[key] is not None]
                return round(float(np.mean(vals)), 2) if vals else None

            a1d  = avg_ret("r1d")
            a5d  = avg_ret("r5d")
            a20d = avg_ret("r20d")
            a60d = avg_ret("r60d")

            # Top performer in sector (by 20D return)
            valid = [s for s in stocks if s["r20d"] is not None]
            top = sorted(valid, key=lambda x: x["r20d"], reverse=True)

            results.append({
                "sector":        sector,
                "stock_count":   len(stocks),
                "avg_1d":        a1d,
                "avg_1w":        a5d,
                "avg_1m":        a20d,
                "avg_3m":        a60d,
                "top_stock":     top[0]["symbol"]   if top else None,
                "top_stock_ret": top[0]["r20d"]     if top else None,
                "worst_stock":   top[-1]["symbol"]  if top else None,
                "stocks":        stocks,
            })

        results.sort(key=lambda x: (x["avg_1m"] or -999), reverse=True)
        return results

    except Exception as e:
        logger.error(f"Sector heatmap error: {e}")
        return []


# ── 5. Earnings Calendar ─────────────────────────────────────────────────

def earnings_calendar(days_ahead: int = 21, symbols: list = None) -> list:
    """
    Fetch upcoming earnings dates for Nifty stocks via yfinance.
    Flags stocks with results in the next `days_ahead` days as earnings risk.

    Returns list sorted by earnings date.
    """
    try:
        import json

        if symbols is None:
            from config.settings import SYMBOL_FILE
            with open(SYMBOL_FILE) as f:
                symbols = json.load(f)

        import yfinance as yf
        today    = date.today()
        deadline = today + timedelta(days=days_ahead)
        results  = []

        for sym in symbols:
            try:
                ticker = yf.Ticker(f"{sym}.NS")
                cal    = ticker.calendar
                if cal is None or cal.empty:
                    continue

                # yfinance calendar has 'Earnings Date' row
                if "Earnings Date" in cal.index:
                    ed = cal.loc["Earnings Date"]
                    # Can be a single date or a range (two columns)
                    dates = []
                    for val in ed.values:
                        try:
                            d = pd.Timestamp(val).date()
                            if today <= d <= deadline:
                                dates.append(d)
                        except Exception:
                            pass

                    for d in dates:
                        results.append({
                            "symbol":        sym,
                            "sector":        get_sector(sym),
                            "earnings_date": str(d),
                            "days_away":     (d - today).days,
                            "risk_level":    "High" if (d - today).days <= 5 else "Medium",
                        })

            except Exception:
                continue

        results.sort(key=lambda x: x["earnings_date"])
        return results

    except Exception as e:
        logger.error(f"Earnings calendar error: {e}")
        return []
