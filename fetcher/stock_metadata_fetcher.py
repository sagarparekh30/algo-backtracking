"""
fetcher/stock_metadata_fetcher.py
==================================
Populates the `stock_metadata` table with:
  - Company name, ISIN, face value  — from NSE's official EQUITY_L.csv (fast, seconds)
  - Index membership                 — derived from config/nse_indices.INDEX_REGISTRY (instant)
  - Market-cap category              — inferred from index membership
  - Sector                          — from static SECTOR_MAP, then yfinance enrichment
  - Industry                        — from yfinance (optional, slow)

Two-phase execution:
  Phase 1 (fast, ~5s): NSE CSV + index memberships → writes basic rows for all ~2100 symbols
  Phase 2 (slow, 15-30 min): yfinance enrichment for sector/industry on symbols still missing sector

Usage:
    from fetcher.stock_metadata_fetcher import run_metadata_sync
    result = run_metadata_sync(enrich_yf=True, workers=4, progress_cb=None)
"""

from __future__ import annotations

import csv
import io
import logging
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable, Optional

import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

logger = logging.getLogger(__name__)

# ── NSE EQUITY_L.csv URL ─────────────────────────────────────────────────────
_EQUITY_CSV_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
_CSV_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer":         "https://www.nseindia.com/",
}

# ── Index → sector mapping ───────────────────────────────────────────────────
_INDEX_SECTOR: dict[str, str] = {
    "NIFTY IT":                   "IT",
    "NIFTY Bank":                 "Banking",
    "NIFTY Pharma":               "Pharma",
    "NIFTY Auto":                 "Auto",
    "NIFTY FMCG":                 "FMCG",
    "NIFTY Metal":                "Metals",
    "NIFTY Energy":               "Energy",
    "NIFTY Realty":               "Realty",
    "NIFTY Financial Services":   "Finance",
    "NIFTY Infrastructure":       "Infra",
    "NIFTY Consumption":          "Consumer",
}

# Priority order for sector inference (more specific wins over broader)
_SECTOR_INDEX_PRIORITY = [
    "NIFTY IT", "NIFTY Bank", "NIFTY Pharma", "NIFTY Auto", "NIFTY FMCG",
    "NIFTY Metal", "NIFTY Energy", "NIFTY Realty", "NIFTY Financial Services",
    "NIFTY Infrastructure", "NIFTY Consumption",
]

# Market-cap category by index membership
_MCAP_RULES = [
    ("Large Cap",  ["NIFTY 50", "NIFTY 100"]),
    ("Mid Cap",    ["NIFTY 200", "NIFTY Midcap 100"]),
    ("Small Cap",  ["NIFTY Smallcap 100"]),
]

# yfinance sector → our sector label
_YF_SECTOR_MAP: dict[str, str] = {
    "Technology":             "IT",
    "Financial Services":     "Finance",
    "Healthcare":             "Pharma",
    "Consumer Cyclical":      "Consumer",
    "Consumer Defensive":     "FMCG",
    "Industrials":            "Industrials",
    "Basic Materials":        "Metals",
    "Energy":                 "Energy",
    "Real Estate":            "Realty",
    "Utilities":              "Energy",
    "Communication Services": "Media",
}


# ── DB connection helper ──────────────────────────────────────────────────────

def _get_conn():
    from db.connection import get_conn as _db_get_conn
    return _db_get_conn()


# ── Phase 1: NSE CSV parse ────────────────────────────────────────────────────

def _fetch_nse_csv() -> list[dict]:
    """
    Download EQUITY_L.csv and return a list of dicts per EQ-series symbol.
    Fields: symbol, company_name, series, isin, face_value
    """
    req = urllib.request.Request(_EQUITY_CSV_URL, headers=_CSV_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")

    rows = []
    reader = csv.DictReader(io.StringIO(raw))
    for row in reader:
        # Header names have leading spaces in some NSE versions
        series  = (row.get("SERIES")       or row.get(" SERIES")       or "").strip()
        symbol  = (row.get("SYMBOL")       or row.get(" SYMBOL")       or "").strip()
        name    = (row.get("NAME OF COMPANY") or row.get(" NAME OF COMPANY") or "").strip()
        isin    = (row.get("ISIN NUMBER")  or row.get(" ISIN NUMBER")  or "").strip()
        fv_raw  = (row.get("FACE VALUE")   or row.get(" FACE VALUE")   or "").strip()

        if series != "EQ" or not symbol or symbol.startswith("-"):
            continue

        try:
            fv = float(fv_raw) if fv_raw else None
        except ValueError:
            fv = None

        rows.append({
            "symbol":       symbol,
            "company_name": name or None,
            "isin":         isin or None,
            "face_value":   fv,
            "series":       "EQ",
        })

    logger.info(f"NSE CSV: {len(rows)} EQ symbols parsed")
    return rows


def _build_index_map() -> dict[str, list[str]]:
    """
    Returns {symbol: [index_name, ...]} for all symbols in INDEX_REGISTRY.
    Also includes "NSE All" for every symbol.
    """
    from config.nse_indices import INDEX_REGISTRY
    sym_indices: dict[str, list[str]] = {}
    for index_name, info in INDEX_REGISTRY.items():
        for sym in info["symbols"]:
            sym_indices.setdefault(sym, []).append(index_name)
    return sym_indices


def _infer_sector(sym: str, sym_indices: list[str]) -> Optional[str]:
    """Infer sector from index membership using priority order."""
    from strategies.sector_map import SECTOR_MAP
    # 1. Static SECTOR_MAP (covers Nifty 100 very well)
    if sym in SECTOR_MAP:
        return SECTOR_MAP[sym]
    # 2. From thematic/sector index membership
    for idx_name in _SECTOR_INDEX_PRIORITY:
        if idx_name in sym_indices:
            return _INDEX_SECTOR[idx_name]
    return None


def _infer_mcap(sym_indices: list[str]) -> str:
    for cat, indices in _MCAP_RULES:
        if any(i in sym_indices for i in indices):
            return cat
    return "Micro Cap"


# ── Phase 2: yfinance enrichment ──────────────────────────────────────────────

def _yf_enrich_one(symbol: str) -> dict:
    """Fetch sector/industry from yfinance for one symbol. Returns {} on error."""
    try:
        import yfinance as yf
        import logging as _l
        _l.getLogger("yfinance").setLevel(_l.CRITICAL)
        _l.getLogger("urllib3").setLevel(_l.CRITICAL)
        info = yf.Ticker(f"{symbol}.NS").info
        yf_sector   = info.get("sector") or ""
        yf_industry = info.get("industry") or ""
        sector      = _YF_SECTOR_MAP.get(yf_sector) or yf_sector or None
        return {"sector": sector, "industry": yf_industry or None}
    except Exception:
        return {}


# ── Main entry point ──────────────────────────────────────────────────────────

def run_metadata_sync(
    enrich_yf:   bool = True,
    workers:     int  = 4,
    progress_cb: Optional[Callable] = None,
) -> dict:
    """
    Full metadata sync.

    Args:
        enrich_yf:   If True, run yfinance Phase 2 for symbols missing sector.
        workers:     Parallel workers for yfinance enrichment.
        progress_cb: Callable(phase, processed, total, symbol) for progress updates.

    Returns:
        dict with counts and duration.
    """
    started = datetime.now()

    # ── Phase 1 ───────────────────────────────────────────────────────────────
    logger.info("Metadata sync Phase 1: NSE CSV + index membership")
    if progress_cb:
        progress_cb("csv", 0, 1, "Downloading NSE EQUITY_L.csv…")

    try:
        csv_rows = _fetch_nse_csv()
    except Exception as e:
        logger.error(f"NSE CSV download failed: {e}")
        csv_rows = []

    index_map   = _build_index_map()   # {symbol: [indices]}
    csv_by_sym  = {r["symbol"]: r for r in csv_rows}

    # Union of CSV symbols + index-known symbols
    all_symbols = sorted(set(list(csv_by_sym.keys()) + list(index_map.keys())))
    logger.info(f"Total symbols to process: {len(all_symbols)}")

    rows_to_upsert = []
    missing_sector = []

    for sym in all_symbols:
        csv_data    = csv_by_sym.get(sym, {})
        sym_indices = index_map.get(sym, [])
        sector      = _infer_sector(sym, sym_indices)
        mcap        = _infer_mcap(sym_indices)

        row = {
            "symbol":       sym,
            "company_name": csv_data.get("company_name"),
            "sector":       sector,
            "industry":     None,
            "indices":      sym_indices,
            "market_cap_cat": mcap,
            "isin":         csv_data.get("isin"),
            "face_value":   csv_data.get("face_value"),
            "series":       csv_data.get("series", "EQ"),
            "is_active":    True,
        }
        rows_to_upsert.append(row)
        if not sector:
            missing_sector.append(sym)

    logger.info(f"Phase 1: {len(rows_to_upsert)} rows ready, {len(missing_sector)} missing sector")

    # ── Bulk upsert Phase 1 ───────────────────────────────────────────────────
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO stock_metadata
                  (symbol, company_name, sector, industry, indices,
                   market_cap_cat, isin, face_value, series, is_active)
                VALUES %s
                ON CONFLICT (symbol) DO UPDATE SET
                  company_name    = EXCLUDED.company_name,
                  indices         = EXCLUDED.indices,
                  market_cap_cat  = EXCLUDED.market_cap_cat,
                  isin            = COALESCE(EXCLUDED.isin, stock_metadata.isin),
                  face_value      = COALESCE(EXCLUDED.face_value, stock_metadata.face_value),
                  series          = EXCLUDED.series,
                  is_active       = EXCLUDED.is_active,
                  sector          = COALESCE(stock_metadata.sector, EXCLUDED.sector),
                  updated_at      = NOW()
                """,
                [
                    (
                        r["symbol"], r["company_name"], r["sector"], r["industry"],
                        r["indices"], r["market_cap_cat"], r["isin"], r["face_value"],
                        r["series"], r["is_active"],
                    )
                    for r in rows_to_upsert
                ],
                page_size=500,
            )
        conn.commit()
        logger.info(f"Phase 1 upsert complete: {len(rows_to_upsert)} rows")
    finally:
        conn.close()

    if progress_cb:
        progress_cb("csv", len(all_symbols), len(all_symbols), "Phase 1 complete")

    yf_processed = 0
    yf_updated   = 0

    # ── Phase 2: yfinance enrichment ─────────────────────────────────────────
    if enrich_yf and missing_sector:
        logger.info(f"Phase 2: yfinance enrichment for {len(missing_sector)} symbols")
        total_yf = len(missing_sector)

        conn = _get_conn()
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_yf_enrich_one, sym): sym for sym in missing_sector}
                for future in as_completed(futures):
                    sym = futures[future]
                    yf_processed += 1
                    try:
                        data = future.result()
                        if data.get("sector") or data.get("industry"):
                            with conn.cursor() as cur:
                                cur.execute(
                                    """
                                    UPDATE stock_metadata
                                       SET sector   = COALESCE(sector,   %s),
                                           industry = COALESCE(industry, %s),
                                           updated_at = NOW()
                                     WHERE symbol = %s
                                    """,
                                    (data.get("sector"), data.get("industry"), sym),
                                )
                            yf_updated += 1
                        # Commit every 50 updates
                        if yf_processed % 50 == 0:
                            conn.commit()
                    except Exception:
                        pass

                    if progress_cb:
                        progress_cb("yfinance", yf_processed, total_yf, sym)

                    time.sleep(0.15)   # polite rate limiting

            conn.commit()
        finally:
            conn.close()

        logger.info(f"Phase 2 complete: {yf_updated}/{yf_processed} symbols enriched with sector")

    duration = round((datetime.now() - started).total_seconds(), 1)
    return {
        "phase1_total":    len(rows_to_upsert),
        "phase2_total":    yf_processed,
        "phase2_updated":  yf_updated,
        "missing_sector":  len(missing_sector),
        "duration_s":      duration,
    }


# ── Convenience query helpers ────────────────────────────────────────────────

def get_sectors() -> list[str]:
    """Return all distinct non-null sectors, sorted."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT sector FROM stock_metadata WHERE sector IS NOT NULL ORDER BY sector")
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def get_indices_list() -> list[str]:
    """Return all distinct index names across all symbols."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT unnest(indices) AS idx FROM stock_metadata ORDER BY idx"
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def get_symbol_metadata(symbol: str) -> Optional[dict]:
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM stock_metadata WHERE symbol = %s", (symbol,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def get_symbols_for_sector(sector: str) -> list[str]:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol FROM stock_metadata WHERE sector = %s AND is_active ORDER BY symbol",
                (sector,)
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def get_symbols_for_index(index_name: str) -> list[str]:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol FROM stock_metadata WHERE %s = ANY(indices) AND is_active ORDER BY symbol",
                (index_name,)
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    def cb(phase, done, total, sym):
        pct = int(done / total * 100) if total else 0
        print(f"[{phase}] {done}/{total} ({pct}%) — {sym}")

    result = run_metadata_sync(enrich_yf=True, workers=4, progress_cb=cb)
    print("\n✓ Done:", result)
