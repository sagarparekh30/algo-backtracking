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

# Silence yfinance's verbose HTTP 404/error output that pollutes Docker logs
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("urllib3").setLevel(logging.CRITICAL)

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
    """
    Bulk-insert rows into DB using executemany, skipping duplicates.
    Returns count of newly inserted rows.

    ~50-100× faster than row-by-row inserts for large DataFrames.
    """
    if df.empty:
        return 0

    rows = [
        (
            symbol,
            row["trade_date"],
            round(float(row["open"]),  4),
            round(float(row["high"]),  4),
            round(float(row["low"]),   4),
            round(float(row["close"]), 4),
            int(row["volume"]),
            SOURCE_NAME,
        )
        for _, row in df.iterrows()
        if all(pd.notna(row[c]) for c in ["open", "high", "low", "close"])
    ]
    if not rows:
        return 0

    try:
        cursor.executemany(
            f"""INSERT INTO {TABLE_NAME}
                (symbol, trade_date, open, high, low, close, volume, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, trade_date) DO NOTHING""",
            rows,
        )
        return cursor.rowcount if cursor.rowcount >= 0 else len(rows)
    except Exception as e:
        logger.debug(f"Bulk insert error for {symbol}: {e}")
        return 0


# ── Parallel fetch worker ─────────────────────────────────────────────────

_RATE_LIMIT_DELAY  = 60    # seconds to wait after a rate-limit hit
_MAX_RETRIES       = 3     # per-symbol retries on rate-limit


def _fetch_one(args):
    """
    Worker function executed by each thread in the pool.
    Retries up to _MAX_RETRIES times on rate-limit errors with backoff.

    Args:
        args: (symbol, fetch_start) tuple

    Returns:
        (symbol, df_or_None, error_or_None)
    """
    symbol, fetch_start = args
    yahoo_sym = to_yahoo_symbol(symbol)

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            df = fetch_yahoo(yahoo_sym, start=fetch_start)
            if df.empty:
                return symbol, None, "no_data"
            return symbol, df, None
        except Exception as e:
            err_str = str(e)
            if "Too Many Requests" in err_str or "Rate limit" in err_str.lower():
                if attempt < _MAX_RETRIES:
                    wait = _RATE_LIMIT_DELAY * attempt   # 60s, 120s
                    logger.warning(f"  {yahoo_sym}: rate limited (attempt {attempt}), waiting {wait}s")
                    time.sleep(wait)
                    continue
            return symbol, None, err_str

    return symbol, None, f"rate_limited after {_MAX_RETRIES} retries"


# ── Main backfill ────────────────────────────────────────────────────────

def run_yfinance_backfill(
    symbols: list = None,
    start_date: str = None,
    progress_cb=None,
    workers: int = 3,
) -> dict:
    """
    Fetch full history for all (or specified) symbols from Yahoo Finance
    and store in the database.

    Uses a thread pool for parallel downloads, then writes to DB serially.
    Default 3 workers — Yahoo Finance is strict about rate limits; keep
    this value ≤ 5 to avoid 429 errors.  Each worker retries up to
    _MAX_RETRIES times with exponential backoff on rate-limit responses.

    Args:
        symbols:     List of NSE plain symbols. Defaults to fetch_nse_all_symbols().
        start_date:  Fetch from this ISO date. None = maximum available history.
        progress_cb: Optional callable(processed, total, symbol, candles) for UI updates.
        workers:     Parallel Yahoo Finance download threads (default 3, max ~5).

    Returns:
        Summary dict.
    """
    import concurrent.futures

    if symbols is None:
        try:
            from config.nse_indices import fetch_nse_all_symbols
            symbols = fetch_nse_all_symbols()
            logger.info(f"[YF] Using dynamic NSE All list: {len(symbols)} symbols")
        except Exception:
            with open(SYMBOL_FILE) as f:
                data = json.load(f)
            symbols = [s for s in data["symbols"] if not s.startswith("DUMMY")]

    total     = len(symbols)
    total_new = 0
    failed    = []

    logger.info("=" * 60)
    logger.info(f"Yahoo Finance backfill: {total} symbols | start={start_date or 'max'} | workers={workers}")
    logger.info("=" * 60)

    # Build work list: (symbol, fetch_start)
    work = [(symbol, start_date) for symbol in symbols]

    # ── Streaming download + write phase ──────────────────────────────────
    # Downloads happen in worker threads; DB writes happen in the main thread
    # as each download completes.  This keeps memory flat (one DF at a time)
    # instead of buffering all 2 000+ DataFrames before writing.
    logger.info(f"[YF] Starting parallel download with {workers} workers …")

    conn   = connect_db()
    cursor = conn.cursor()
    done_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, w): w[0] for w in work}
        for future in concurrent.futures.as_completed(futures):
            symbol, df, err = future.result()
            done_count += 1

            if err:
                logger.warning(f"  [{done_count}/{total}] {symbol}: {err}")
                failed.append(symbol)
            else:
                try:
                    inserted = insert_df(cursor, symbol, df)
                    conn.commit()
                    total_new += inserted
                    if inserted > 0:
                        logger.info(
                            f"  [{done_count}/{total}] {symbol}: "
                            f"{len(df)} rows | {inserted} new "
                            f"| {df['trade_date'].iloc[0]} → {df['trade_date'].iloc[-1]}"
                        )
                except Exception as e:
                    conn.rollback()
                    logger.error(f"  {symbol}: DB write error — {e}")
                    failed.append(symbol)

            if progress_cb:
                progress_cb(done_count, total, symbol, total_new)

    conn.close()

    summary = {
        "source":            SOURCE_NAME,
        "total_symbols":     total,
        "successful":        total - len(failed),
        "failed":            len(failed),
        "failed_list":       failed[:50],   # cap list size in response
        "total_new_candles": total_new,
        "completed_at":      datetime.now().isoformat(),
    }
    logger.info("=" * 60)
    logger.info(f"Done. New candles: {total_new} | Successful: {total - len(failed)}/{total} | Failed: {len(failed)}")
    if failed:
        logger.warning(f"Failed symbols ({len(failed)}): {failed[:20]}{'…' if len(failed) > 20 else ''}")
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
