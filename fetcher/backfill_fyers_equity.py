import json
import time
import sqlite3
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Tuple, Optional

from fyers_apiv3 import fyersModel

from config.settings import (
    DB_PATH,
    SYMBOL_FILE,
    LOOKBACK_YEARS,
    FYERS_CLIENT_ID,
    TOKEN_PATH,
    TABLE_NAME,
    LOG_DIR,
    validate_config
)

# ===============================
# LOGGING SETUP
# ===============================

os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'backfill.log')),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ===============================
# CONSTANTS
# ===============================

SOURCE_NAME = "FYERS"
EXCHANGE = "NSE"
TIMEFRAME = "D"          # Daily candles
MAX_CHUNK_DAYS = 365     # FYERS limit for 1D candles
MAX_RETRIES = 3          # Number of retry attempts
RETRY_DELAY = 1          # Initial retry delay in seconds

# ===============================
# DATA VALIDATION
# ===============================

def validate_candle_data(symbol: str, candle: list) -> bool:
    """Validate that candle data is reasonable."""
    try:
        ts, o, h, l, c, v = candle
        
        # Check for negative or zero prices
        if any(price <= 0 for price in [o, h, l, c]):
            logger.warning(f"Invalid prices for {symbol}: O={o}, H={h}, L={l}, C={c}")
            return False
        
        # Check OHLC relationships
        if h < max(o, c) or l > min(o, c):
            logger.warning(f"Invalid OHLC relationship for {symbol}: O={o}, H={h}, L={l}, C={c}")
            return False
        
        # Check for negative volume
        if v < 0:
            logger.warning(f"Negative volume for {symbol}: {v}")
            return False
        
        return True
        
    except Exception as e:
        logger.error(f"Error validating candle for {symbol}: {e}")
        return False

# ===============================
# TOKEN MANAGEMENT
# ===============================

def load_access_token() -> str:
    """Load and validate access token."""
    try:
        with open(TOKEN_PATH) as f:
            token_data = json.load(f)
        
        access_token = token_data.get("access_token")
        if not access_token:
            raise ValueError("No access_token found in token file")
        
        # Check expiration if available
        expires_at = token_data.get("expires_at")
        if expires_at:
            expiry_time = datetime.fromisoformat(expires_at)
            if datetime.now() >= expiry_time:
                logger.error("Access token has expired. Please run login.py again.")
                raise RuntimeError("Access token expired")
            else:
                time_remaining = expiry_time - datetime.now()
                logger.info(f"Token valid for {time_remaining.total_seconds() / 3600:.1f} more hours")
        
        return access_token
        
    except FileNotFoundError:
        logger.error(f"Token file not found at {TOKEN_PATH}. Please run login.py first.")
        raise
    except Exception as e:
        logger.error(f"Error loading token: {e}")
        raise

# ===============================
# HELPERS
# ===============================

def load_symbols() -> List[str]:
    """Load symbols from configuration file."""
    with open(SYMBOL_FILE, "r") as f:
        data = json.load(f)
    symbols = data["symbols"]
    
    # Filter out test/dummy symbols
    symbols = [s for s in symbols if not s.startswith("DUMMY")]
    
    logger.info(f"Loaded {len(symbols)} symbols from {SYMBOL_FILE}")
    return symbols


def get_date_range() -> Tuple[datetime, datetime]:
    """Calculate the date range for backfill."""
    end_date = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start_date = end_date - timedelta(days=LOOKBACK_YEARS * 365)
    return start_date, end_date


def generate_date_chunks(start_date: datetime, end_date: datetime, chunk_days: int = 365) -> List[Tuple[str, str]]:
    """Generate date chunks for API requests."""
    chunks = []
    current_start = start_date

    while current_start < end_date:
        # Ensure we don't go past end_date
        current_end = min(
            current_start + timedelta(days=chunk_days - 1),  # Fixed edge case
            end_date
        )

        chunks.append((
            current_start.strftime("%Y-%m-%d"),
            current_end.strftime("%Y-%m-%d")
        ))

        current_start = current_end + timedelta(days=1)

    return chunks


def connect_db() -> sqlite3.Connection:
    """Connect to the database and apply optimizations."""
    conn = sqlite3.connect(DB_PATH)
    # Performance optimizations for SQLite
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
    return conn


def insert_candle(cursor: sqlite3.Cursor, row: tuple) -> None:
    """Insert a candle into the database."""
    sql = f"""
    INSERT OR IGNORE INTO {TABLE_NAME}
    (symbol, trade_date, open, high, low, close, volume, source)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """
    cursor.execute(sql, row)


def get_last_date(cursor: sqlite3.Cursor, symbol: str) -> Optional[datetime]:
    """Query the database for the last trade date of a specific symbol."""
    sql = f"SELECT MAX(trade_date) FROM {TABLE_NAME} WHERE symbol = ?"
    cursor.execute(sql, (symbol,))
    result = cursor.fetchone()
    
    if result and result[0]:
        return datetime.strptime(result[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return None


def fetch_with_retry(fyers: fyersModel.FyersModel, payload: dict, symbol: str, chunk_from: str) -> Optional[dict]:
    """Fetch data with exponential backoff retry logic."""
    for attempt in range(MAX_RETRIES):
        try:
            response = fyers.history(payload)
            
            if response.get("s") == "ok":
                return response
            else:
                logger.warning(f"FYERS returned non-ok status for {symbol} [{chunk_from}]: {response.get('message', 'Unknown error')}")
                
        except Exception as e:
            logger.warning(f"Attempt {attempt + 1}/{MAX_RETRIES} failed for {symbol} [{chunk_from}]: {e}")
        
        # Exponential backoff
        if attempt < MAX_RETRIES - 1:
            delay = RETRY_DELAY * (2 ** attempt)
            logger.info(f"Retrying in {delay} seconds...")
            time.sleep(delay)
    
    logger.error(f"All retry attempts failed for {symbol} [{chunk_from}]")
    return None


def save_progress(symbol: str, chunk_from: str, progress_file: str = "backfill_progress.json") -> None:
    """Save progress to resume from failures."""
    progress_path = os.path.join(LOG_DIR, progress_file)
    progress_data = {
        "last_symbol": symbol,
        "last_chunk": chunk_from,
        "timestamp": datetime.now().isoformat()
    }
    
    with open(progress_path, "w") as f:
        json.dump(progress_data, f, indent=2)

# ===============================
# MAIN BACKFILL
# ===============================

def main():
    """Main backfill process."""
    try:
        # Validate configuration
        validate_config()
        logger.info("=" * 60)
        logger.info("Starting FYERS equity backfill process")
        logger.info("=" * 60)
        
        # Load symbols and date range
        symbols = load_symbols()
        start_dt, end_dt = get_date_range()
        
        logger.info(f"Backfill range : {start_dt.date()} → {end_dt.date()}")
        logger.info(f"Symbols        : {len(symbols)}")
        logger.info(f"Table name     : {TABLE_NAME}")
        
        # Load access token with validation
        access_token = load_access_token()
        
        # Create FYERS client
        fyers = fyersModel.FyersModel(
            client_id=FYERS_CLIENT_ID,
            token=access_token,
            log_path=LOG_DIR  # Properly manage FYERS logs
        )
        
        # DB connection
        conn = connect_db()
        cursor = conn.cursor()
        
        # Date chunks (FYERS safe) - Moved inside symbol loop
        
        # Statistics
        total_candles = 0
        failed_symbols = []
        
        # -------------------------------
        # SYMBOL LOOP
        # -------------------------------
        
        for idx, symbol in enumerate(symbols, start=1):
            fyers_symbol = f"{EXCHANGE}:{symbol}-EQ"
            
            # Incremental Backfill Check
            last_date = get_last_date(cursor, symbol)
            
            if last_date:
                symbol_start_dt = last_date + timedelta(days=1)
                if symbol_start_dt >= end_dt:
                    logger.info(f"[{idx}/{len(symbols)}] {fyers_symbol} is already up to date ({last_date.date()})")
                    continue
                logger.info(f"[{idx}/{len(symbols)}] Incremental update for {fyers_symbol}: {symbol_start_dt.date()} → {end_dt.date()}")
            else:
                symbol_start_dt = start_dt
                logger.info(f"[{idx}/{len(symbols)}] Full backfill for {fyers_symbol}: {symbol_start_dt.date()} → {end_dt.date()}")
            
            symbol_candles = 0
            
            try:
                # Calculate chunks for this specific symbol
                symbol_date_chunks = generate_date_chunks(symbol_start_dt, end_dt, MAX_CHUNK_DAYS)
                
                # -------------------------------
                # DATE CHUNK LOOP
                # -------------------------------
                
                for chunk_from, chunk_to in symbol_date_chunks:
                    logger.debug(f"  Fetching {chunk_from} → {chunk_to}")
                    
                    payload = {
                        "symbol": fyers_symbol,
                        "resolution": TIMEFRAME,
                        "date_format": "1",
                        "range_from": chunk_from,
                        "range_to": chunk_to,
                        "cont_flag": "1"
                    }
                    
                    # Fetch with retry logic
                    api_start = time.time()
                    response = fetch_with_retry(fyers, payload, symbol, chunk_from)
                    api_duration = time.time() - api_start
                    
                    if not response:
                        continue
                    
                    candles = response.get("candles", [])
                    
                    db_start = time.time()
                    for candle in candles:
                        # Validate candle data
                        if not validate_candle_data(symbol, candle):
                            continue
                        
                        ts, o, h, l, c, v = candle
                        
                        # Use UTC timezone
                        trade_date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                        
                        row = (
                            symbol,
                            trade_date,
                            o,
                            h,
                            l,
                            c,
                            int(v),
                            SOURCE_NAME
                        )
                        
                        insert_candle(cursor, row)
                        symbol_candles += 1
                    
                    conn.commit()
                    db_duration = time.time() - db_start
                    
                    logger.info(f"  Chunk {chunk_from} → {chunk_to}: API={api_duration:.2f}s, DB={db_duration:.2f}s, Candles={len(candles)}")
                    
                    save_progress(symbol, chunk_from)
                    time.sleep(0.3)  # Rate-limit safety
                
                total_candles += symbol_candles
                logger.info(f"  ✅ Completed - {symbol_candles} candles inserted")
                
            except Exception as e:
                logger.error(f"  ❌ Error for {symbol}: {e}", exc_info=True)
                failed_symbols.append(symbol)
        
        conn.close()
        
        # Final summary
        logger.info("=" * 60)
        logger.info("BACKFILL COMPLETED")
        logger.info("=" * 60)
        logger.info(f"Total candles inserted: {total_candles}")
        logger.info(f"Successful symbols: {len(symbols) - len(failed_symbols)}/{len(symbols)}")
        
        if failed_symbols:
            logger.warning(f"Failed symbols ({len(failed_symbols)}): {', '.join(failed_symbols)}")
        else:
            logger.info("All symbols processed successfully!")
            
    except Exception as e:
        logger.error(f"Backfill process failed: {e}", exc_info=True)
        raise

# ===============================
# ENTRY POINT
# ===============================

if __name__ == "__main__":
    main()