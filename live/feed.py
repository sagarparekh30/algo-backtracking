"""
Live market data feed using Fyers API.

Two modes:
  - WebSocket (real-time): streams tick data via FyersDataSocket
  - REST polling (fallback): polls fyers.quotes() every 3 seconds

The feed stores latest tick data in a thread-safe dict.
The dashboard reads from this dict on every /api/ltp request.
"""

import os
import sys
import json
import time
import logging
import threading
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

from config.settings import FYERS_CLIENT_ID, TOKEN_PATH, LOG_DIR

logger = logging.getLogger(__name__)

EXCHANGE = "NSE"
SUFFIX   = "-EQ"


def _fyers_symbol(symbol: str) -> str:
    """Convert plain symbol to Fyers format: RELIANCE → NSE:RELIANCE-EQ"""
    symbol = symbol.strip().upper()
    if symbol.startswith("NSE:") or symbol.startswith("BSE:"):
        return symbol
    return f"{EXCHANGE}:{symbol}{SUFFIX}"


def _plain_symbol(fyers_sym: str) -> str:
    """NSE:RELIANCE-EQ → RELIANCE"""
    return fyers_sym.replace(f"{EXCHANGE}:", "").replace(SUFFIX, "")


class LiveFeed:
    """
    Manages a live market data connection to Fyers.

    Usage:
        feed = LiveFeed()
        feed.start(["RELIANCE", "HDFCBANK"])   # starts background thread
        prices = feed.get_all()                # thread-safe read
        feed.stop()
    """

    def __init__(self):
        self._prices: dict = {}        # symbol (plain) → tick dict
        self._lock   = threading.Lock()
        self._thread: threading.Thread = None
        self._stop_event = threading.Event()
        self._socket = None
        self._fyers  = None
        self._symbols: list = []
        self.mode    = "stopped"       # "websocket" | "polling" | "stopped"
        self.connected = False
        self.started_at: str = None
        self.error: str = None

    # ──────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────

    def start(self, symbols: list, prefer_websocket: bool = True):
        """
        Start the live feed for the given symbols.

        Args:
            symbols: List of plain symbols e.g. ["RELIANCE", "HDFCBANK"]
            prefer_websocket: Try WebSocket first; fall back to REST polling.
        """
        if self._thread and self._thread.is_alive():
            # Already running — just subscribe new symbols
            self.subscribe(symbols)
            return

        self._symbols = [s.strip().upper() for s in symbols]
        self._stop_event.clear()
        self.error = None

        try:
            self._fyers = self._create_fyers_client()
        except Exception as e:
            self.error = str(e)
            logger.error(f"LiveFeed: cannot create Fyers client — {e}")
            return

        if prefer_websocket:
            self._thread = threading.Thread(
                target=self._run_websocket, daemon=True, name="live-feed-ws"
            )
        else:
            self._thread = threading.Thread(
                target=self._run_polling, daemon=True, name="live-feed-poll"
            )

        self._thread.start()
        self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"LiveFeed started ({('websocket' if prefer_websocket else 'polling')}) for {self._symbols}")

    def stop(self):
        """Gracefully stop the feed."""
        self._stop_event.set()
        if self._socket:
            try:
                self._socket.close_connection()
            except Exception:
                pass
        self.mode = "stopped"
        self.connected = False
        logger.info("LiveFeed stopped.")

    def subscribe(self, symbols: list):
        """Add more symbols to an already-running feed."""
        new = [s.strip().upper() for s in symbols if s.strip().upper() not in self._symbols]
        if not new:
            return
        self._symbols.extend(new)
        if self._socket and self.connected:
            try:
                fyers_syms = [_fyers_symbol(s) for s in new]
                self._socket.subscribe(symbols=fyers_syms, data_type="SymbolUpdate")
                logger.info(f"LiveFeed: subscribed to {fyers_syms}")
            except Exception as e:
                logger.error(f"LiveFeed subscribe error: {e}")

    def get_all(self) -> dict:
        """Return a copy of all latest prices (thread-safe)."""
        with self._lock:
            return dict(self._prices)

    def get(self, symbol: str) -> dict:
        """Return latest price for a single symbol."""
        with self._lock:
            return self._prices.get(symbol.upper(), {})

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        return {
            "running": self.is_running(),
            "mode": self.mode,
            "connected": self.connected,
            "symbols": self._symbols,
            "started_at": self.started_at,
            "error": self.error,
            "price_count": len(self._prices),
        }

    # ──────────────────────────────────────────────────────────────────
    # Fyers client
    # ──────────────────────────────────────────────────────────────────

    def _create_fyers_client(self):
        from fyers_apiv3 import fyersModel

        with open(TOKEN_PATH) as f:
            token_data = json.load(f)

        access_token = token_data.get("access_token")
        if not access_token:
            raise ValueError("No access_token in token.json — run login.py first")

        expires_at = token_data.get("expires_at")
        if expires_at:
            expiry = datetime.fromisoformat(expires_at)
            if datetime.now() >= expiry:
                raise RuntimeError("Access token has expired — run login.py to refresh")

        fyers = fyersModel.FyersModel(
            client_id=FYERS_CLIENT_ID,
            token=access_token,
            log_path=LOG_DIR,
        )
        fyers._access_token = access_token  # store for WebSocket
        return fyers

    # ──────────────────────────────────────────────────────────────────
    # WebSocket mode
    # ──────────────────────────────────────────────────────────────────

    def _run_websocket(self):
        self.mode = "websocket"
        try:
            from fyers_apiv3 import fyersModel

            access_token = self._fyers._access_token
            full_token = f"{FYERS_CLIENT_ID}:{access_token}"

            def on_connect(soc):
                self.connected = True
                fyers_syms = [_fyers_symbol(s) for s in self._symbols]
                soc.subscribe(symbols=fyers_syms, data_type="SymbolUpdate")
                logger.info(f"WebSocket connected. Subscribed to {fyers_syms}")

            def on_ticks(ticks):
                if not ticks:
                    return
                if isinstance(ticks, dict):
                    ticks = [ticks]
                for tick in ticks:
                    self._process_ws_tick(tick)

            def on_close(soc):
                self.connected = False
                logger.warning("WebSocket connection closed.")

            def on_error(err):
                self.connected = False
                self.error = str(err)
                logger.error(f"WebSocket error: {err}")

            self._socket = fyersModel.FyersDataSocket(
                access_token=full_token,
                log_path=LOG_DIR,
                litemode=False,
                write_to_file=False,
                reconnect=True,
                on_connect=on_connect,
                on_close=on_close,
                on_error=on_error,
                on_ticks=on_ticks,
            )

            self._socket.connect()   # blocks until stop_event or error

        except Exception as e:
            logger.error(f"WebSocket mode failed: {e} — falling back to REST polling")
            self.error = str(e)
            self.mode = "stopped"
            self.connected = False
            # Fall back to polling
            self._run_polling()

    def _process_ws_tick(self, tick: dict):
        """Parse a WebSocket tick and store in _prices."""
        try:
            raw_sym = tick.get("symbol", "")
            symbol = _plain_symbol(raw_sym) if raw_sym else None
            if not symbol:
                return

            ltp         = tick.get("ltp") or tick.get("last_traded_price", 0)
            prev_close  = tick.get("prev_close_price") or tick.get("prev_close", 0)
            change      = tick.get("ch") or tick.get("change", 0)
            change_pct  = tick.get("chp") or tick.get("change_percent", 0)

            entry = {
                "symbol":     symbol,
                "ltp":        round(float(ltp), 2),
                "open":       round(float(tick.get("open_price") or tick.get("open", 0)), 2),
                "high":       round(float(tick.get("high_price") or tick.get("high", 0)), 2),
                "low":        round(float(tick.get("low_price") or tick.get("low", 0)), 2),
                "prev_close": round(float(prev_close), 2),
                "change":     round(float(change), 2),
                "change_pct": round(float(change_pct), 4),
                "volume":     int(tick.get("volume", 0) or 0),
                "updated_at": datetime.now().strftime("%H:%M:%S"),
                "source":     "websocket",
            }

            with self._lock:
                self._prices[symbol] = entry

        except Exception as e:
            logger.debug(f"_process_ws_tick error: {e}")

    # ──────────────────────────────────────────────────────────────────
    # REST polling mode (fallback)
    # ──────────────────────────────────────────────────────────────────

    def _run_polling(self):
        self.mode = "polling"
        self.connected = True
        logger.info(f"REST polling started for {self._symbols}")

        while not self._stop_event.is_set():
            try:
                if self._symbols:
                    fyers_syms = ",".join(_fyers_symbol(s) for s in self._symbols)
                    response = self._fyers.quotes({"symbols": fyers_syms})
                    if response.get("s") == "ok":
                        for item in response.get("d", []):
                            self._process_rest_quote(item)
            except Exception as e:
                logger.error(f"REST polling error: {e}")
                self.error = str(e)

            self._stop_event.wait(timeout=3)  # poll every 3 seconds

        self.connected = False
        logger.info("REST polling stopped.")

    def _process_rest_quote(self, item: dict):
        """Parse a REST quote item and store in _prices."""
        try:
            raw_sym = item.get("n", "")
            symbol = _plain_symbol(raw_sym) if raw_sym else None
            if not symbol:
                return

            v = item.get("v", {})
            ltp        = v.get("ltp", 0) or 0
            prev_close = v.get("prev_close_price", 0) or 0
            change     = v.get("ch", 0) or 0
            change_pct = v.get("chp", 0) or 0

            entry = {
                "symbol":     symbol,
                "ltp":        round(float(ltp), 2),
                "open":       round(float(v.get("open_price", 0) or 0), 2),
                "high":       round(float(v.get("high_price", 0) or 0), 2),
                "low":        round(float(v.get("low_price", 0) or 0), 2),
                "prev_close": round(float(prev_close), 2),
                "change":     round(float(change), 2),
                "change_pct": round(float(change_pct), 4),
                "volume":     int(v.get("volume", 0) or 0),
                "updated_at": datetime.now().strftime("%H:%M:%S"),
                "source":     "rest",
            }

            with self._lock:
                self._prices[symbol] = entry

        except Exception as e:
            logger.debug(f"_process_rest_quote error: {e}")


# ── Singleton instance shared across the FastAPI app ──────────────────
live_feed = LiveFeed()
