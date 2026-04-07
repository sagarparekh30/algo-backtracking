import os
from pathlib import Path

# =========================================================
# ENVIRONMENT
# =========================================================
# This tells the system WHERE it is running.
# Default is 'local'. Later you can set ENV=cloud

ENV = os.getenv("ENV", "local")

# =========================================================
# PROJECT BASE DIRECTORY
# =========================================================
# This finds the ROOT of your project reliably,
# no matter where the script is run from.

BASE_DIR = Path(__file__).resolve().parents[1]

# =========================================================
# PATHS (all cloud-migratable)
# =========================================================
# If ENV variables are present → use them
# Otherwise → fall back to sensible local defaults

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://trading:trading@localhost:5432/trading"
)

# Legacy alias — kept so any remaining references don't crash at import time.
# All modules should use DATABASE_URL directly.
DB_PATH = DATABASE_URL

TOKEN_PATH = os.getenv(
    "TOKEN_PATH",
    str(BASE_DIR / "auth" / "token.json")
)

SYMBOL_FILE = os.getenv(
    "SYMBOL_FILE",
    str(BASE_DIR / "config" / "nifty_100_11Jan26.json")
)

# =========================================================
# FYERS CONFIG (NEVER hardcode secrets)
# =========================================================

FYERS_CLIENT_ID = os.getenv("FYERS_CLIENT_ID")
FYERS_SECRET_KEY = os.getenv("FYERS_SECRET_KEY")

FYERS_REDIRECT_URI = os.getenv(
    "FYERS_REDIRECT_URI",
    "https://www.google.com"
)

# =========================================================
# FETCH SETTINGS
# =========================================================
# These control HOW MUCH data is fetched,
# not WHERE or HOW.

# Historical backfill — Fyers provides up to ~10 years of daily data.
# NSE was founded in 1994, so 10 years is the practical maximum from Fyers.
# Set LOOKBACK_YEARS=10 for maximum history; reduce if you want faster initial sync.
LOOKBACK_YEARS = int(os.getenv("LOOKBACK_YEARS", "10"))

# Used for daily incremental runs (catch-up safe)
DAILY_LOOKBACK_DAYS = int(os.getenv("DAILY_LOOKBACK_DAYS", "10"))

# =========================================================
# SCHEDULER SETTINGS
# =========================================================

# Daily data update — time in IST (HH:MM), runs after market close
SCHEDULER_DATA_UPDATE_TIME = os.getenv("SCHEDULER_DATA_UPDATE_TIME", "16:00")

# Weekly ML retrain — day (mon/tue/.../sun) + time in IST
SCHEDULER_ML_RETRAIN_DAY  = os.getenv("SCHEDULER_ML_RETRAIN_DAY",  "sun")
SCHEDULER_ML_RETRAIN_TIME = os.getenv("SCHEDULER_ML_RETRAIN_TIME", "22:00")

# Set to "false" to disable auto-scheduler entirely
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "true").lower() == "true"

# =========================================================
# DATABASE SETTINGS
# =========================================================

TABLE_NAME = os.getenv(
    "TABLE_NAME",
    "equity_daily_candles_swing_trading"
)

# =========================================================
# LOGGING SETTINGS
# =========================================================

LOG_DIR = os.getenv(
    "LOG_DIR",
    str(BASE_DIR / "logs")
)

# =========================================================
# VALIDATION
# =========================================================

# =========================================================
# ADMIN AUTH
# =========================================================
# Generate a bcrypt hash for your password with:
#   python -c "import bcrypt; print(bcrypt.hashpw(b'your_password', bcrypt.gensalt()).decode())"
# Then set ADMIN_PASSWORD_HASH in .env

ADMIN_USERNAME     = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")   # bcrypt hash — MUST be set in .env
SECRET_KEY         = os.getenv("SECRET_KEY", "change-me-in-production-use-a-long-random-string")
TOKEN_EXPIRE_HOURS = int(os.getenv("TOKEN_EXPIRE_HOURS", "12"))

# =========================================================
# TELEGRAM
# =========================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# =========================================================
# RISK MANAGEMENT
# =========================================================

INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "500000"))
RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "1.0"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "10"))
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
ATR_MULTIPLIER = float(os.getenv("ATR_MULTIPLIER", "2.0"))
REWARD_RISK_RATIO = float(os.getenv("REWARD_RISK_RATIO", "2.0"))
MAX_HOLD_DAYS = int(os.getenv("MAX_HOLD_DAYS", "20"))

# =========================================================
# BACKTEST
# =========================================================

# Optional override: set to ISO date string e.g. "2023-01-01"
BACKTEST_START_DATE = os.getenv("BACKTEST_START_DATE")

# =========================================================
# VALIDATION
# =========================================================

def validate_config():
    """Validate that required configuration is present."""
    errors = []
    
    if not FYERS_CLIENT_ID:
        errors.append("FYERS_CLIENT_ID not set in environment")
    
    if not FYERS_SECRET_KEY:
        errors.append("FYERS_SECRET_KEY not set in environment")
    
    if errors:
        raise RuntimeError(
            "Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors)
        )
    
    return True