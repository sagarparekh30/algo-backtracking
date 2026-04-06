"""
Yahoo Finance historical data fetcher for NSE equity symbols.

Fetches full available history (up to 30 years) for all Nifty 100 stocks.
Uses the same SQLite table as the Fyers backfill — deduplication handled
by the PRIMARY KEY (symbol, trade_date).

Strategy:
  - yfinance  → initial full history  (free, no auth, up to 30 years)
  - Fyers API → daily incremental     (real-time accurate, post market-close)

Usage:
  python fetcher/yfinance_fetcher.py
  python fetcher/yfinance_fetcher.py --symbol RELIANCE   # single symbol
  python fetcher/yfinance_fetcher.py --from 2000-01-01   # from a specific date
"""

import json
import logging
import os
import sys
import time
import argparse
from datetime import datetime, timezone

import yfinance as yf
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

from config.settings import SYMBOL_FILE, TABLE_NAME, LOG_DIR, validate_config
from db.connection import get_conn

# ── Logging ─────────────────────────────────────────────────────────────
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "yfinance_backfill.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

SOURCE_NAME = "YFINANCE"

# ── Symbol mapping ───────────────────────────────────────────────────────
# Most NSE symbols map to SYMBOL.NS on Yahoo Finance.
# Overrides for symbols that don't follow the standard pattern:
SYMBOL_OVERRIDES: dict[str, str] = {
    # Standard .NS suffix works for M&M, BAJAJ-AUTO, L&T, etc.
    # Add exceptions here only if a symbol is genuinely different on Yahoo.
}


def to_yahoo_symbol(nse_symbol: str) -> str:
    """Convert NSE plain symbol to Yahoo Finance ticker (e.g. RELIANCE → RELIANCE.NS)."""
    if nse_symbol in SYMBOL_OVERRIDES:
        return SYMBOL_OVERRIDES[nse_symbol]
    return f"{nse_symbol}.NS"


# ── DB helpers ───────────────────────────────────────────────────────────

def connect_db():
    """Return a new PostgreSQL connection."""
    return get_conn()


def get_last_yf_date(cursor, symbol: str):
    """Return the latest trade_date stored for this symbol from YFINANCE source."""
    cursor.execute(
        f"SELECT MAX(trade_date) FROM {TABLE_NAME} WHERE symbol = %s AND source = %s",
        (symbol, SOURCE_NAME),
    )
    result = cursor.fetchone()
    return str(result[0]) if result and result[0] else None


def get_earliest_any_date(cursor, symbol: str):
    """Return the earliest trade_date stored for this symbol from any source."""
    cursor.execute(
        f"SELECT MIN(trade_date) FROM {TABLE_NAME} WHERE symbol = %s",
        (symbol,),
    )
    result = cursor.fetchone()
    return str(result[0]) if result and result[0] else None


# ── Fetch + validate ─────────────────────────────────────────────────────

def fetch_yahoo(yahoo_symbol: str, start: str = None, end: str = None) -> pd.DataFrame:
    """
    Fetch OHLCV data from Yahoo Finance.

    Args:
        yahoo_symbol: e.g. "RELIANCE.NS"
        start: ISO date string (optional, defaults to max available)
        end:   ISO date string (optional, defaults to today)

    Returns:
        DataFrame with columns: Date, Open, High, Low, Close, Volume
        Empty DataFrame on failure.
    """
    try:
        ticker = yf.Ticker(yahoo_symbol)
        kwargs = {"auto_adjust": True}   # adjust for splits/dividends
        if start:
            kwargs["start"] = start
        else:
            kwargs["period"] = "max"
        if end:
            kwargs["end"] = end

        df = ticker.history(**kwargs)

        if df.empty:
            return pd.DataFrame()

        df = df.reset_index()
        df = df[["Date", "Open", "High", "Low", "Close", "Volume"]].copy()
        df["Date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
        df = df.rename(columns={"Date": "trade_date", "Open": "open", "High": "high",
                                  "Low": "low", "Close": "close", "Volume": "volume"})
        df = df.dropna(subset=["open", "high", "low", "close"])

        # Basic OHLC validation
        valid_mask = (
            (df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0) & (df["close"] > 0) &
            (df["high"] >= df["open"]) & (df["high"] >= df["close"]) &
            (df["low"]  <= df["open"]) & (df["low"]  <= df["close"]) &
            (df["volume"] >= 0)
        )
        invalid = (~valid_mask).sum()
        if invalid > 0:
            logger.debug(f"Dropping {invalid} invalid rows for {yahoo_symbol}")
        df = df[valid_mask].reset_index(drop=True)

        return df

    except Exception as e:
        logger.error(f"Yahoo fetch error for {yahoo_symbol}: {e}")
        return pd.DataFrame()


# ── Insert ───────────────────────────────────────────────────────────────

def insert_df(cursor, symbol: str, df: pd.DataFrame) -> int:
    """Insert rows into DB, skipping duplicates. Returns count inserted."""
    if df.empty:
        return 0
    inserted = 0
    for _, row in df.iterrows():
        try:
            cursor.execute(
                f"""INSERT INTO {TABLE_NAME}
                    (symbol, trade_date, open, high, low, close, volume, source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (symbol, trade_date) DO NOTHING""",
                (
                    symbol,
                    row["trade_date"],
                    round(float(row["open"]),  4),
                    round(float(row["high"]),  4),
                    round(float(row["low"]),   4),
                    round(float(row["close"]), 4),
                    int(row["volume"]),
                    SOURCE_NAME,
                ),
            )
            if cursor.rowcount > 0:
                inserted += 1
        except Exception as e:
            logger.debug(f"Insert error for {symbol} {row.get('trade_date')}: {e}")
    return inserted


# ── Main backfill ────────────────────────────────────────────────────────

def run_yfinance_backfill(
    symbols: list[str] = None,
    start_date: str = None,
    progress_cb=None,
) -> dict:
    """
    Fetch full history for all (or specified) symbols from Yahoo Finance
    and store in the SQLite database.

    Args:
        symbols:     List of NSE plain symbols. Defaults to all from SYMBOL_FILE.
        start_date:  Fetch from this ISO date. None = maximum available history.
        progress_cb: Optional callable(processed, total, symbol, candles) for UI updates.

    Returns:
        Summary dict.
    """
    if symbols is None:
        with open(SYMBOL_FILE) as f:
            data = json.load(f)
        symbols = [s for s in data["symbols"] if not s.startswith("DUMMY")]

    total         = len(symbols)
    total_new     = 0
    failed        = []

    logger.info("=" * 60)
    logger.info(f"Yahoo Finance backfill: {total} symbols | start={start_date or 'max'}")
    logger.info("=" * 60)

    conn   = connect_db()
    cursor = conn.cursor()

    for idx, symbol in enumerate(symbols, 1):
        yahoo_sym = to_yahoo_symbol(symbol)

        # Determine fetch start: use last yfinance date if we have any
        fetch_start = start_date
        if fetch_start is None:
            earliest = get_earliest_any_date(cursor, symbol)
            if earliest:
                # We have some Fyers data — fetch from the very beginning
                # so Yahoo fills in everything before Fyers' 10-year window
                pass   # fetch_start stays None → period='max'

        try:
            logger.info(f"[{idx}/{total}] Fetching {yahoo_sym} (start={fetch_start or 'max'})")
            df = fetch_yahoo(yahoo_sym, start=fetch_start)

            if df.empty:
                logger.warning(f"  {yahoo_sym}: No data returned — skipping")
                failed.append(symbol)
            else:
                inserted = insert_df(cursor, symbol, df)
                conn.commit()
                total_new += inserted
                logger.info(
                    f"  {yahoo_sym}: {len(df)} rows fetched | {inserted} new candles inserted"
                    f" | range {df['trade_date'].iloc[0]} → {df['trade_date'].iloc[-1]}"
                )

        except Exception as e:
            logger.error(f"  {symbol}: ERROR — {e}")
            failed.append(symbol)

        if progress_cb:
            progress_cb(idx, total, symbol, total_new)

        time.sleep(0.5)   # be polite to Yahoo

    conn.close()

    summary = {
        "source":        SOURCE_NAME,
        "total_symbols": total,
        "successful":    total - len(failed),
        "failed":        len(failed),
        "failed_list":   failed,
        "total_new_candles": total_new,
        "completed_at":  datetime.now().isoformat(),
    }
    logger.info("=" * 60)
    logger.info(f"Done. New candles: {total_new} | Failed: {len(failed)}")
    if failed:
        logger.warning(f"Failed: {failed}")
    logger.info("=" * 60)
    return summary


# ── CLI entry point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Yahoo Finance historical backfill for NSE")
    parser.add_argument("--symbol", help="Single symbol to fetch (e.g. RELIANCE)")
    parser.add_argument("--from",   dest="from_date", help="Start date YYYY-MM-DD (default: max available)")
    args = parser.parse_args()

    validate_config()

    syms = [args.symbol.upper()] if args.symbol else None
    run_yfinance_backfill(symbols=syms, start_date=args.from_date)
