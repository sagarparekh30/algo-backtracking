# Algorithmic Trading - FYERS Data Backfill

A Python-based system for fetching and storing historical equity market data from FYERS API for algorithmic trading strategies.

## 📋 Features

- **Automated Authentication**: OAuth-based login flow with token management
- **Historical Data Backfill**: Fetch multi-year historical daily candles for Nifty 100 stocks
- **Robust Error Handling**: Retry logic with exponential backoff for API failures
- **Data Validation**: Automatic validation of OHLC data integrity
- **Progress Tracking**: Resume capability for interrupted backfill operations
- **Comprehensive Logging**: Detailed logs for monitoring and debugging
- **Token Expiration Management**: Automatic detection of expired tokens
- **Timezone Aware**: Proper UTC timezone handling for timestamps

## 🏗️ Project Structure

```
algotrading/
├── auth/
│   └── token.json          # Access token (auto-generated, gitignored)
├── config/
│   ├── settings.py         # Centralized configuration
│   └── nifty_100_11Jan26.json  # Symbol list
├── data/
│   └── marketdata.db       # SQLite database
├── fetcher/
│   └── backfill_fyers_equity.py  # Main backfill script
├── logs/                   # Application logs (auto-created)
├── login.py                # Authentication script
├── requirements.txt        # Python dependencies
└── .gitignore             # Git ignore rules
```

## 🚀 Setup

### 1. Prerequisites

- Python 3.8+
- FYERS trading account with API credentials

### 2. Installation

```bash
# Clone or navigate to the project directory
cd algotrading

# Create virtual environment
python -m venv venv

# Activate virtual environment
# On macOS/Linux:
source venv/bin/activate
# On Windows:
# venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configuration

Set the following environment variables:

```bash
export FYERS_CLIENT_ID="your_client_id"
export FYERS_SECRET_KEY="your_secret_key"
```

**Optional environment variables:**

```bash
export FYERS_REDIRECT_URI="https://www.google.com"  # Default
export LOOKBACK_YEARS="2"                            # Default
export DAILY_LOOKBACK_DAYS="10"                      # Default
export TABLE_NAME="equity_daily_candles_swing_trading"  # Default
```

## 📖 Usage

### Step 1: Login to FYERS

Run the login script to authenticate and generate an access token:

```bash
python login.py
```

This will:
1. Open your browser for FYERS login
2. Prompt you to paste the redirected URL
3. Generate and save an access token to `auth/token.json`
4. Token is valid for 24 hours

### Step 2: Run Backfill

Execute the backfill script to fetch historical data:

```bash
python fetcher/backfill_fyers_equity.py
```

This will:
- Validate configuration and token
- Fetch daily candles for all symbols in the Nifty 100 list
- Store data in SQLite database with automatic deduplication
- Log progress and errors to `logs/backfill.log`
- Save progress for resume capability

## 🗄️ Database Schema

```sql
CREATE TABLE equity_daily_candles_swing_trading (
    symbol TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume INTEGER,
    source TEXT NOT NULL,
    PRIMARY KEY (symbol, trade_date)
);
```

## 📊 Data Validation

The system automatically validates:
- ✅ No negative or zero prices
- ✅ High >= max(Open, Close)
- ✅ Low <= min(Open, Close)
- ✅ No negative volume
- ✅ Valid timestamp conversion

Invalid candles are logged and skipped.

## 🔄 Error Handling

- **Retry Logic**: Up to 3 attempts with exponential backoff (1s, 2s, 4s)
- **Token Expiration**: Automatic detection with helpful error messages
- **Progress Tracking**: Resume from last successful symbol/chunk on failure
- **Symbol-Level Isolation**: Failures don't stop the entire backfill

## 📝 Logging

Logs are stored in the `logs/` directory:

- `login.log` - Authentication process logs
- `backfill.log` - Backfill process logs
- Console output for real-time monitoring

Log format:
```
2026-01-28 16:00:00 - module_name - INFO - Message
```

## 🛠️ Customization

### Change Symbol List

Edit `config/nifty_100_11Jan26.json` to add/remove symbols:

```json
{
  "index": "NIFTY 100",
  "exchange": "NSE",
  "data_source": "FYERS",
  "symbols": ["RELIANCE", "TCS", "INFY", ...]
}
```

### Adjust Lookback Period

```bash
export LOOKBACK_YEARS="5"  # Fetch 5 years of data
```

### Change Database Location

```bash
export DB_PATH="/path/to/your/database.db"
```

## ⚠️ Important Notes

1. **API Rate Limits**: The script includes a 0.3s delay between requests to respect FYERS rate limits
2. **Token Validity**: FYERS tokens expire after 24 hours - re-run `login.py` when needed
3. **Data Deduplication**: The database uses composite primary key to prevent duplicate entries
4. **Timezone**: All timestamps are stored in UTC

## 🐛 Troubleshooting

### "Access token expired"
```bash
python login.py  # Generate a new token
```

### "FYERS_CLIENT_ID not set"
```bash
export FYERS_CLIENT_ID="your_client_id"
export FYERS_SECRET_KEY="your_secret_key"
```

### Check logs for detailed errors
```bash
tail -f logs/backfill.log
```

## 📄 License

This project is for personal use. Ensure compliance with FYERS API terms of service.

## 🤝 Contributing

This is a personal project, but suggestions and improvements are welcome!

---

**Last Updated**: January 2026
