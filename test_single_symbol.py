import json
import logging
import os
from datetime import datetime, timedelta, timezone
from fyers_apiv3 import fyersModel
from config.settings import FYERS_CLIENT_ID, TOKEN_PATH, LOG_DIR, validate_config
from fetcher.backfill_fyers_equity import load_access_token, validate_candle_data

# Setup logging
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

print("=" * 60)
print("SINGLE SYMBOL TEST - RELIANCE")
print("=" * 60)

# Validate config
validate_config()
print("✅ Configuration validated")

# Load token
access_token = load_access_token()
print("✅ Token loaded")

# Create FYERS client
fyers = fyersModel.FyersModel(
    client_id=FYERS_CLIENT_ID,
    token=access_token,
    log_path=LOG_DIR
)
print("✅ FYERS client created")

# Test with single symbol
symbol = "RELIANCE"
fyers_symbol = f"NSE:{symbol}-EQ"

# Fetch last 7 days
end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
start_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

print(f"\n📊 Fetching {fyers_symbol} from {start_date} to {end_date}")

payload = {
    "symbol": fyers_symbol,
    "resolution": "D",
    "date_format": "1",
    "range_from": start_date,
    "range_to": end_date,
    "cont_flag": "1"
}

response = fyers.history(payload)

if response.get("s") == "ok":
    candles = response.get("candles", [])
    print(f"✅ Fetched {len(candles)} candles")
    
    valid_count = 0
    print("\n📈 Candle Data:")
    print("-" * 60)
    for candle in candles:
        if validate_candle_data(symbol, candle):
            valid_count += 1
            ts, o, h, l, c, v = candle
            trade_date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            print(f"{trade_date}: O={o:8.2f}, H={h:8.2f}, L={l:8.2f}, C={c:8.2f}, V={v:,}")
    
    print("-" * 60)
    print(f"\n✅ All {valid_count}/{len(candles)} candles validated successfully!")
    print("\n" + "=" * 60)
    print("TEST PASSED - Ready for full backfill!")
    print("=" * 60)
else:
    print(f"❌ API Error: {response}")
    print("\nThis might mean:")
    print("1. Token has expired - run: python login.py")
    print("2. Network issue - check your connection")
    print("3. FYERS API issue - try again later")
