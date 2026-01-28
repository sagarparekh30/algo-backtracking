# Testing Guide

Follow these steps to test your refactored algorithmic trading codebase.

## ✅ Pre-Test Checklist

- [x] Virtual environment activated
- [x] Dependencies installed (`fyers-apiv3`, `python-dateutil`)
- [x] Environment variables set (`FYERS_CLIENT_ID`, `FYERS_SECRET_KEY`)
- [ ] Fresh access token generated

---

## 🧪 Test 1: Configuration Validation

Test that the configuration validation works correctly:

```bash
python -c "from config.settings import validate_config; validate_config(); print('✅ Config OK')"
```

**Expected Output:**
```
✅ Config OK
```

---

## 🧪 Test 2: Login Flow (Token Generation)

Run the login script to generate a fresh access token:

```bash
python login.py
```

**What to expect:**
1. Browser opens automatically to FYERS login page
2. Log in with your FYERS credentials
3. After successful login, you'll be redirected to Google
4. Copy the **entire URL** from your browser address bar
5. Paste it when prompted in the terminal

**Expected Output:**
```
2026-01-28 16:21:00 - __main__ - INFO - Starting FYERS login process
2026-01-28 16:21:00 - __main__ - INFO - Opening browser for FYERS login...

After login, paste the FULL redirected URL here:
[paste URL here]

2026-01-28 16:21:05 - __main__ - INFO - Successfully extracted auth code
2026-01-28 16:21:06 - __main__ - INFO - Access token generated successfully
2026-01-28 16:21:06 - __main__ - INFO - Access token saved to /path/to/auth/token.json
2026-01-28 16:21:06 - __main__ - INFO - Token expires at: 2026-01-29 16:21:06
2026-01-28 16:21:06 - __main__ - INFO - Login completed successfully

✅ Login completed successfully.
```

**Check the logs:**
```bash
cat logs/login.log
```

**Verify token file:**
```bash
cat auth/token.json
```

Should contain:
```json
{
  "access_token": "...",
  "expires_at": "2026-01-29T16:21:06",
  "created_at": "2026-01-28T16:21:06"
}
```

---

## 🧪 Test 3: Token Validation

Test that the token loading and validation works:

```bash
python -c "from fetcher.backfill_fyers_equity import load_access_token; token = load_access_token(); print('✅ Token loaded and validated successfully')"
```

**Expected Output:**
```
2026-01-28 16:22:00 - __main__ - INFO - Token valid for 23.9 more hours
✅ Token loaded and validated successfully
```

---

## 🧪 Test 4: Symbol Loading

Test that symbols are loaded correctly (with dummy symbols filtered):

```bash
python -c "from fetcher.backfill_fyers_equity import load_symbols; symbols = load_symbols(); print(f'✅ Loaded {len(symbols)} symbols'); print('Sample:', symbols[:5])"
```

**Expected Output:**
```
2026-01-28 16:22:00 - __main__ - INFO - Loaded 99 symbols from /path/to/config/nifty_100_11Jan26.json
✅ Loaded 99 symbols
Sample: ['ABB', 'ADANIENSOL', 'ADANIENT', 'ADANIGREEN', 'ADANIPORTS']
```

Note: Should be 99 symbols (not 100) because we removed DUMMYHDLVR.

---

## 🧪 Test 5: Data Validation

Test the candle data validation function:

```bash
python -c "
from fetcher.backfill_fyers_equity import validate_candle_data

# Valid candle
valid = [1706400000, 100.0, 105.0, 98.0, 103.0, 1000000]
print('Valid candle:', validate_candle_data('TEST', valid))

# Invalid candle (high < open)
invalid = [1706400000, 100.0, 95.0, 98.0, 103.0, 1000000]
print('Invalid candle:', validate_candle_data('TEST', invalid))
"
```

**Expected Output:**
```
Valid candle: True
2026-01-28 16:22:00 - __main__ - WARNING - Invalid OHLC relationship for TEST: O=100.0, H=95.0, L=98.0, C=103.0
Invalid candle: False
```

---

## 🧪 Test 6: Dry Run (Single Symbol Test)

Before running the full backfill, test with a single symbol to verify everything works:

Create a test script:

```bash
cat > test_single_symbol.py << 'EOF'
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

# Validate config
validate_config()

# Load token
access_token = load_access_token()

# Create FYERS client
fyers = fyersModel.FyersModel(
    client_id=FYERS_CLIENT_ID,
    token=access_token,
    log_path=LOG_DIR
)

# Test with single symbol
symbol = "RELIANCE"
fyers_symbol = f"NSE:{symbol}-EQ"

# Fetch last 7 days
end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
start_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

logger.info(f"Testing with {fyers_symbol} from {start_date} to {end_date}")

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
    logger.info(f"✅ Fetched {len(candles)} candles")
    
    valid_count = 0
    for candle in candles:
        if validate_candle_data(symbol, candle):
            valid_count += 1
            ts, o, h, l, c, v = candle
            trade_date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            logger.info(f"  {trade_date}: O={o:.2f}, H={h:.2f}, L={l:.2f}, C={c:.2f}, V={v}")
    
    logger.info(f"✅ All {valid_count}/{len(candles)} candles validated successfully!")
else:
    logger.error(f"❌ API Error: {response}")
EOF

python test_single_symbol.py
```

**Expected Output:**
```
2026-01-28 16:22:00 - INFO - Token valid for 23.9 more hours
2026-01-28 16:22:00 - INFO - Testing with NSE:RELIANCE-EQ from 2026-01-21 to 2026-01-28
2026-01-28 16:22:01 - INFO - ✅ Fetched 5 candles
2026-01-28 16:22:01 - INFO -   2026-01-21: O=1250.50, H=1265.75, L=1245.00, C=1260.25, V=5234567
2026-01-28 16:22:01 - INFO -   2026-01-22: O=1260.00, H=1270.50, L=1255.00, C=1268.75, V=4567890
...
2026-01-28 16:22:01 - INFO - ✅ All 5/5 candles validated successfully!
```

---

## 🧪 Test 7: Full Backfill (Production Run)

Once the single symbol test passes, run the full backfill:

```bash
python fetcher/backfill_fyers_equity.py
```

**Monitor in real-time:**
```bash
# In another terminal
tail -f logs/backfill.log
```

**Expected Output:**
```
2026-01-28 16:22:00 - __main__ - INFO - ============================================================
2026-01-28 16:22:00 - __main__ - INFO - Starting FYERS equity backfill process
2026-01-28 16:22:00 - __main__ - INFO - ============================================================
2026-01-28 16:22:00 - __main__ - INFO - Loaded 99 symbols from /path/to/config/nifty_100_11Jan26.json
2026-01-28 16:22:00 - __main__ - INFO - Backfill range : 2024-01-28 → 2026-01-28
2026-01-28 16:22:00 - __main__ - INFO - Symbols        : 99
2026-01-28 16:22:00 - __main__ - INFO - Table name     : equity_daily_candles_swing_trading
2026-01-28 16:22:00 - __main__ - INFO - Token valid for 23.9 more hours
2026-01-28 16:22:00 - __main__ - INFO - Date chunks    : 3
2026-01-28 16:22:00 - __main__ - INFO - [1/99] Processing NSE:ABB-EQ
2026-01-28 16:22:01 - __main__ - INFO -   ✅ Completed - 500 candles inserted
...
```

**This will take some time** (approximately 30-45 minutes for 99 symbols with rate limiting).

---

## 🧪 Test 8: Verify Database

After backfill completes, verify the data:

```bash
sqlite3 data/marketdata.db << 'EOF'
-- Count total records
SELECT COUNT(*) as total_candles FROM equity_daily_candles_swing_trading;

-- Count by symbol
SELECT symbol, COUNT(*) as candle_count 
FROM equity_daily_candles_swing_trading 
GROUP BY symbol 
ORDER BY candle_count DESC 
LIMIT 10;

-- Check date range
SELECT 
    MIN(trade_date) as earliest_date,
    MAX(trade_date) as latest_date
FROM equity_daily_candles_swing_trading;

-- Sample data
SELECT * FROM equity_daily_candles_swing_trading 
WHERE symbol = 'RELIANCE' 
ORDER BY trade_date DESC 
LIMIT 5;
EOF
```

---

## 🧪 Test 9: Verify Logging

Check that all logs are being created properly:

```bash
ls -lh logs/
cat logs/backfill.log | tail -20
```

---

## 🧪 Test 10: Test Error Handling

Test token expiration detection (optional):

```bash
# Manually edit token to be expired
python -c "
import json
from datetime import datetime, timedelta

with open('auth/token.json', 'r') as f:
    data = json.load(f)

data['expires_at'] = (datetime.now() - timedelta(hours=1)).isoformat()

with open('auth/token.json', 'w') as f:
    json.dump(data, f, indent=2)

print('Token manually expired for testing')
"

# Try to run backfill - should fail with clear error
python fetcher/backfill_fyers_equity.py
```

**Expected Output:**
```
2026-01-28 16:22:00 - __main__ - ERROR - Access token has expired. Please run login.py again.
RuntimeError: Access token expired
```

Then regenerate token:
```bash
python login.py
```

---

## 📊 Success Criteria

✅ **All tests should pass with:**
- Configuration validation working
- Token generation and validation working
- Symbol loading (99 symbols, no DUMMYHDLVR)
- Data validation catching invalid candles
- Single symbol test fetching data successfully
- Full backfill completing without errors
- Database populated with data
- Logs created in `logs/` directory
- Progress tracking file created

---

## 🐛 Troubleshooting

**Issue**: Token expired during backfill
- **Solution**: Run `python login.py` to get a fresh token

**Issue**: API rate limit errors
- **Solution**: The script has 0.3s delays and retry logic - just let it continue

**Issue**: Some symbols fail
- **Solution**: Check `logs/backfill.log` for details - individual symbol failures won't stop the entire process

**Issue**: Database locked
- **Solution**: Make sure no other process is accessing the database

---

## 🎯 Quick Test Command

Run all basic tests in sequence:

```bash
echo "Test 1: Config validation" && \
python -c "from config.settings import validate_config; validate_config(); print('✅ OK')" && \
echo -e "\nTest 2: Symbol loading" && \
python -c "from fetcher.backfill_fyers_equity import load_symbols; print(f'✅ {len(load_symbols())} symbols')" && \
echo -e "\nTest 3: Data validation" && \
python -c "from fetcher.backfill_fyers_equity import validate_candle_data; print('✅ OK' if validate_candle_data('TEST', [1706400000, 100, 105, 98, 103, 1000000]) else '❌ FAIL')" && \
echo -e "\n✅ All basic tests passed! Ready to run login.py"
```
