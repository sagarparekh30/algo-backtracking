"""
Watchlist management — stores a list of symbols the user wants to track live.
Persisted to data/watchlist.json.
"""

import json
import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import BASE_DIR

logger = logging.getLogger(__name__)

WATCHLIST_PATH = os.path.join(str(BASE_DIR), "data", "watchlist.json")

DEFAULT_WATCHLIST = [
    "RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK",
    "HINDUNILVR", "SBIN", "BHARTIARTL", "KOTAKBANK", "ITC"
]


def load_watchlist() -> list:
    try:
        if os.path.exists(WATCHLIST_PATH):
            with open(WATCHLIST_PATH) as f:
                data = json.load(f)
                return data.get("symbols", DEFAULT_WATCHLIST)
    except Exception as e:
        logger.error(f"load_watchlist error: {e}")
    return list(DEFAULT_WATCHLIST)


def save_watchlist(symbols: list):
    try:
        os.makedirs(os.path.dirname(WATCHLIST_PATH), exist_ok=True)
        with open(WATCHLIST_PATH, "w") as f:
            json.dump({"symbols": symbols}, f, indent=2)
    except Exception as e:
        logger.error(f"save_watchlist error: {e}")


def add_symbol(symbol: str) -> list:
    symbol = symbol.strip().upper()
    symbols = load_watchlist()
    if symbol and symbol not in symbols:
        symbols.append(symbol)
        save_watchlist(symbols)
    return symbols


def remove_symbol(symbol: str) -> list:
    symbol = symbol.strip().upper()
    symbols = load_watchlist()
    symbols = [s for s in symbols if s != symbol]
    save_watchlist(symbols)
    return symbols
