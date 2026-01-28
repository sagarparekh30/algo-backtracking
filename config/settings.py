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

DB_PATH = os.getenv(
    "DB_PATH",
    str(BASE_DIR / "data" / "marketdata.db")
)

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

# Used only for one-time historical backfill
LOOKBACK_YEARS = int(os.getenv("LOOKBACK_YEARS", "2"))

# Used for daily runs (catch-up safe)
DAILY_LOOKBACK_DAYS = int(os.getenv("DAILY_LOOKBACK_DAYS", "10"))

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