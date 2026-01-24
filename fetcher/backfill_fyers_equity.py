import json
import time
import sqlite3
from datetime import datetime, timedelta

from fyers_apiv3 import fyersModel

from config.settings import (
    DB_PATH,
    SYMBOL_FILE,
    LOOKBACK_YEARS,
    FYERS_CLIENT_ID,
    TOKEN_PATH
)

# ===============================
# CONSTANTS
# ===============================

SOURCE_NAME = "FYERS"
EXCHANGE = "NSE"
TIMEFRAME = "D"          # Daily candles
MAX_CHUNK_DAYS = 365     # FYERS limit for 1D candles

# ===============================
# HELPERS
# ===============================

def load_symbols():
    with open(SYMBOL_FILE, "r") as f:
        data = json.load(f)
    return data["symbols"]


def get_date_range():
    end_date = datetime.today()
    start_date = end_date - timedelta(days=LOOKBACK_YEARS * 365)
    return start_date, end_date


def generate_date_chunks(start_date, end_date, chunk_days=365):
    chunks = []
    current_start = start_date

    while current_start < end_date:
        current_end = min(
            current_start + timedelta(days=chunk_days),
            end_date
        )

        chunks.append((
            current_start.strftime("%Y-%m-%d"),
            current_end.strftime("%Y-%m-%d")
        ))

        current_start = current_end + timedelta(days=1)

    return chunks


def connect_db():
    return sqlite3.connect(DB_PATH)


def insert_candle(cursor, row):
    sql = """
    INSERT OR IGNORE INTO equity_daily_candles_swing_trading
    (symbol, trade_date, open, high, low, close, volume, source)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """
    cursor.execute(sql, row)

# ===============================
# MAIN BACKFILL
# ===============================

def main():
    # Load symbols
    symbols = load_symbols()
    start_dt, end_dt = get_date_range()

    # Load access token
    with open(TOKEN_PATH) as f:
        ACCESS_TOKEN = json.load(f)["access_token"]

    print(f"Backfill range : {start_dt.date()} → {end_dt.date()}")
    print(f"Symbols        : {len(symbols)}")

    # Create FYERS client
    fyers = fyersModel.FyersModel(
        client_id=FYERS_CLIENT_ID,
        token=ACCESS_TOKEN,
        log_path=""
    )

    # DB connection
    conn = connect_db()
    cursor = conn.cursor()

    # Date chunks (FYERS safe)
    date_chunks = generate_date_chunks(
        start_dt,
        end_dt,
        MAX_CHUNK_DAYS
    )

    print(f"Date chunks    : {len(date_chunks)}")

    # -------------------------------
    # SYMBOL LOOP
    # -------------------------------

    for idx, symbol in enumerate(symbols, start=1):
        fyers_symbol = f"{EXCHANGE}:{symbol}-EQ"
        print(f"\n[{idx}/{len(symbols)}] {fyers_symbol}")

        try:
            # -------------------------------
            # DATE CHUNK LOOP
            # -------------------------------

            for chunk_from, chunk_to in date_chunks:
                print(f"  Fetching {chunk_from} → {chunk_to}")

                payload = {
                    "symbol": fyers_symbol,
                    "resolution": TIMEFRAME,
                    "date_format": "1",
                    "range_from": chunk_from,
                    "range_to": chunk_to,
                    "cont_flag": "1"
                }

                response = fyers.history(payload)

                if response.get("s") != "ok":
                    print(f"  ❌ FYERS error for {symbol} [{chunk_from}]")
                    continue

                candles = response.get("candles", [])

                for candle in candles:
                    ts, o, h, l, c, v = candle
                    trade_date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

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

                conn.commit()
                time.sleep(0.3)  # rate-limit safety

            print("  ✅ Completed")

        except Exception as e:
            print(f"  ❌ Error for {symbol}: {e}")

    conn.close()
    print("\n✅ FULL BACKFILL COMPLETED SUCCESSFULLY")

# ===============================
# ENTRY POINT
# ===============================

if __name__ == "__main__":
    main()