"""
Event Risk Filter — risk/event_risk.py

Screens signal stocks for upcoming corporate events within the trade window
(entry_date + look_ahead_days, default 12 days).

Event severity levels:
  BLOCK   — Results, Board Meeting, Rights Issue, AGM
  CAUTION — Ex-dividend, Bonus record date, Stock split
  CLEAR   — No events in window

Data source:
  NSE corporate actions CSV endpoint (public, no auth required).
  Downloaded once daily and cached at data/nse_events_cache.json.
  Cache is refreshed automatically if older than 23 hours (i.e., fresh each morning).

Usage:
    from risk.event_risk import EventRiskFilter

    erf = EventRiskFilter()                  # loads/refreshes cache
    result = erf.check("RELIANCE")
    # {"status": "BLOCK", "events": [...], "nearest_event_days": 5}

    result = erf.check("TCS", look_ahead_days=10)
    # {"status": "CLEAR", "events": [], "nearest_event_days": None}
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

NSE_CORP_ACTIONS_URL = (
    "https://www.nseindia.com/api/corporates-corporateActions"
    "?index=equities&from_date={from_date}&to_date={to_date}"
)

CACHE_FILE    = Path(__file__).parent.parent / "data" / "nse_events_cache.json"
CACHE_MAX_AGE = 23 * 3600   # seconds — refresh if older than 23h

DEFAULT_LOOK_AHEAD = 12     # trading days to look ahead

# Event keyword → severity mapping (lowercase match)
_BLOCK_KEYWORDS = {
    "financial results", "board meeting", "agm", "egm",
    "rights issue", "rights entitlement", "open offer",
    "buyback", "merger", "amalgamation", "de-merger", "scheme",
}
_CAUTION_KEYWORDS = {
    "dividend", "ex-dividend", "ex-date",
    "bonus", "record date",
    "split", "sub-division",
}

# NSE user-agent header (required to avoid 403)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nseindia.com/",
}


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _cache_is_fresh() -> bool:
    if not CACHE_FILE.exists():
        return False
    age = (datetime.now().timestamp() - CACHE_FILE.stat().st_mtime)
    return age < CACHE_MAX_AGE


def _load_cache() -> dict[str, list[dict]]:
    """Load {symbol: [event, ...]} from disk cache."""
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(data: dict[str, list[dict]]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.warning(f"Could not save NSE events cache: {e}")


# ── NSE fetch ─────────────────────────────────────────────────────────────────

def _fetch_nse_events(look_ahead_days: int = 30) -> dict[str, list[dict]]:
    """
    Fetch corporate actions from NSE for the next `look_ahead_days` days.

    Returns {symbol: [{"date": "DD-MMM-YYYY", "purpose": "...", "severity": ...}]}
    Falls back to empty dict on any network error.
    """
    today     = date.today()
    end_date  = today + timedelta(days=look_ahead_days)
    url = NSE_CORP_ACTIONS_URL.format(
        from_date = today.strftime("%d-%m-%Y"),
        to_date   = end_date.strftime("%d-%m-%Y"),
    )

    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)

        # NSE returns {"data": [...]} or just [...]
        rows = data.get("data", data) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return {}

        result: dict[str, list[dict]] = {}
        for row in rows:
            sym = (row.get("symbol") or row.get("Symbol") or "").strip().upper()
            if not sym:
                continue
            purpose = (row.get("purpose") or row.get("Purpose") or
                       row.get("subject") or "").strip()
            ev_date_str = (row.get("exDate") or row.get("exdate") or
                           row.get("date") or row.get("Date") or "").strip()
            if not ev_date_str:
                continue

            severity = _classify_event(purpose)
            event    = {
                "date":     ev_date_str,
                "purpose":  purpose,
                "severity": severity,
            }
            result.setdefault(sym, []).append(event)

        logger.info(f"Fetched NSE corporate actions: {len(result)} symbols, "
                    f"{sum(len(v) for v in result.values())} events")
        return result

    except Exception as e:
        logger.warning(f"NSE corporate actions fetch failed: {e}")
        return {}


def _classify_event(purpose: str) -> str:
    """Return BLOCK, CAUTION, or CLEAR based on event purpose text."""
    low = purpose.lower()
    for kw in _BLOCK_KEYWORDS:
        if kw in low:
            return "BLOCK"
    for kw in _CAUTION_KEYWORDS:
        if kw in low:
            return "CAUTION"
    return "CAUTION"   # unknown event type → treat as caution


def _parse_event_date(date_str: str) -> Optional[date]:
    """Parse NSE date strings: DD-MMM-YYYY, DD/MM/YYYY, YYYY-MM-DD."""
    for fmt in ("%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


# ── Main filter class ─────────────────────────────────────────────────────────

class EventRiskFilter:
    """
    Checks a signal stock for upcoming corporate events.

    On first use (or when cache is stale) fetches from NSE and caches to disk.
    Subsequent calls in the same process use the in-memory event map.
    """

    def __init__(self, refresh: bool = False):
        """
        Args:
            refresh: Force a fresh fetch from NSE even if cache is valid.
        """
        self._events: dict[str, list[dict]] = {}
        self._load(force=refresh)

    def _load(self, force: bool = False) -> None:
        """Load events from cache or fetch from NSE."""
        if not force and _cache_is_fresh():
            self._events = _load_cache()
            logger.debug(f"EventRiskFilter loaded from cache ({len(self._events)} symbols)")
        else:
            logger.info("EventRiskFilter: fetching fresh NSE corporate actions…")
            fetched = _fetch_nse_events(look_ahead_days=30)
            if fetched:
                self._events = fetched
                _save_cache(fetched)
            else:
                # Network failed — fall back to stale cache if it exists
                self._events = _load_cache() or {}
                logger.warning("NSE fetch failed; using stale cache or empty")

    def refresh(self) -> None:
        """Force a fresh fetch from NSE and update cache."""
        self._load(force=True)

    def check(
        self,
        symbol:           str,
        look_ahead_days:  int          = DEFAULT_LOOK_AHEAD,
        entry_date:       Optional[date] = None,
    ) -> dict:
        """
        Check whether `symbol` has a corporate event within the trade window.

        Args:
            symbol:           NSE symbol (e.g. "RELIANCE").
            look_ahead_days:  Days from entry to look ahead (default 12).
            entry_date:       Trade entry date (defaults to today).

        Returns:
            {
              "status":             "BLOCK" | "CAUTION" | "CLEAR",
              "events":             [{date, purpose, severity, days_away}, ...],
              "nearest_event_days": int | None,
            }
        """
        sym  = symbol.upper()
        from_date = entry_date or date.today()
        to_date   = from_date + timedelta(days=look_ahead_days)

        raw_events = self._events.get(sym, [])
        in_window  = []

        for ev in raw_events:
            ev_date = _parse_event_date(ev.get("date", ""))
            if ev_date is None:
                continue
            if from_date <= ev_date <= to_date:
                days_away = (ev_date - from_date).days
                in_window.append({
                    "date":       ev.get("date"),
                    "purpose":    ev.get("purpose"),
                    "severity":   ev.get("severity"),
                    "days_away":  days_away,
                })

        # Sort by date
        in_window.sort(key=lambda e: e["days_away"])

        if not in_window:
            return {"status": "CLEAR", "events": [], "nearest_event_days": None}

        # Overall status = worst severity in window
        if any(e["severity"] == "BLOCK" for e in in_window):
            status = "BLOCK"
        else:
            status = "CAUTION"

        nearest_days = in_window[0]["days_away"] if in_window else None

        return {
            "status":             status,
            "events":             in_window,
            "nearest_event_days": nearest_days,
        }

    def bulk_check(
        self,
        symbols:          list[str],
        look_ahead_days:  int          = DEFAULT_LOOK_AHEAD,
        entry_date:       Optional[date] = None,
    ) -> dict[str, dict]:
        """
        Check multiple symbols in one call.

        Returns {symbol: check_result}
        """
        return {sym: self.check(sym, look_ahead_days=look_ahead_days,
                                 entry_date=entry_date)
                for sym in symbols}


# ── Module-level singleton ────────────────────────────────────────────────────

_erf_instance: Optional[EventRiskFilter] = None


def get_event_risk_filter(refresh: bool = False) -> EventRiskFilter:
    """Return module-level singleton, refreshing if needed."""
    global _erf_instance
    if _erf_instance is None or refresh:
        _erf_instance = EventRiskFilter(refresh=refresh)
    return _erf_instance
