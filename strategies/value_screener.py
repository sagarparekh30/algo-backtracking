"""
Value Screener — fundamental + technical undervaluation scan for NIFTY 500.

Scoring model (100 pts fundamental + 10 pts technical bonus):

  Valuation (50 pts):
    P/E vs sector median     25 pts  — deeper discount = more points
    P/B ratio                15 pts  — lower P/B = more points
    EV/EBITDA                10 pts  — lower multiple = more points

  Quality (25 pts):
    ROE                      15 pts  — higher return on equity = more points
    Debt/Equity              10 pts  — lower leverage = more points

  Growth (15 pts):
    Revenue growth YoY        8 pts  — faster growth = more points
    EPS growth YoY            7 pts  — faster earnings growth = more points

  Dividend (10 pts):
    Dividend yield           10 pts  — higher yield = more points

  Technical bonus (max 10 pts):
    RSI 30–50                 5 pts  — oversold territory signals reversion opportunity
    Price near SMA200         5 pts  — close to long-term mean = lower entry risk

Grades:
  75–100 → Deep Value ⭐⭐⭐⭐⭐
  60–74  → Good Value ⭐⭐⭐⭐
  45–59  → Moderate Value ⭐⭐⭐
  30–44  → Fair Value ⭐⭐
   0–29  → Avoid / Overvalued ⭐

Data sources:
  Fundamentals  : yfinance (.NS suffix) — P/E, P/B, ROE, D/E, yield, growth
  Technicals    : local PostgreSQL DB — RSI-14, SMA-200

Cache: data/value_cache.json — refreshed automatically after 24 hours.
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

from config.settings import TABLE_NAME, BASE_DIR
from db.connection import get_engine
from strategies.sector_map import get_sector

logger = logging.getLogger(__name__)

CACHE_FILE = os.path.join(str(BASE_DIR), "data", "value_cache.json")
CACHE_TTL_HOURS = 24

# yfinance symbol overrides (same pattern as yfinance_fetcher.py)
_YF_OVERRIDES: dict = {}


def _to_yf(symbol: str) -> str:
    return _YF_OVERRIDES.get(symbol, f"{symbol}.NS")


# ─────────────────────────────────────────────────────────────────────────────
# Data fetching
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_fundamentals(symbol: str) -> dict:
    """
    Fetch fundamental metrics from yfinance for one NSE symbol.

    yfinance notes:
      returnOnEquity  → decimal (0.15 = 15%)
      revenueGrowth   → decimal (0.10 = 10%)
      earningsGrowth  → decimal (0.20 = 20%)
      dividendYield   → decimal (0.02 = 2%)
      debtToEquity    → expressed as % of equity (50 → 0.5x D/E ratio)
    """
    try:
        import yfinance as yf
        import logging as _logging
        # yfinance prints raw HTTP errors to stdout/stderr — silence them
        _logging.getLogger("yfinance").setLevel(_logging.CRITICAL)
        _logging.getLogger("urllib3").setLevel(_logging.CRITICAL)
        info = yf.Ticker(_to_yf(symbol)).info

        de_raw = info.get("debtToEquity")
        # yfinance reports D/E as percentage; divide to get ratio
        debt_equity = round(de_raw / 100, 3) if (de_raw is not None) else None

        return {
            "symbol":          symbol,
            "pe_ratio":        info.get("trailingPE"),
            "pb_ratio":        info.get("priceToBook"),
            "ev_ebitda":       info.get("enterpriseToEbitda"),
            "roe":             round((info.get("returnOnEquity") or 0) * 100, 2),
            "debt_equity":     debt_equity,
            "dividend_yield":  round((info.get("dividendYield") or 0) * 100, 2),
            "revenue_growth":  round((info.get("revenueGrowth") or 0) * 100, 2),
            "eps_growth":      round((info.get("earningsGrowth") or 0) * 100, 2),
            "current_price":   info.get("currentPrice") or info.get("regularMarketPrice"),
            "high_52w":        info.get("fiftyTwoWeekHigh"),
            "low_52w":         info.get("fiftyTwoWeekLow"),
            "market_cap":      info.get("marketCap"),
            "sector":          info.get("sector") or get_sector(symbol),
            "industry":        info.get("industry"),
        }
    except Exception as e:
        logger.debug(f"yfinance fetch error for {symbol}: {e}")
        return {"symbol": symbol, "error": str(e)}


def _get_technical(engine, symbol: str) -> dict:
    """Compute RSI-14 and SMA-200 for the latest bar from local DB."""
    try:
        df = pd.read_sql(
            f"SELECT close FROM {TABLE_NAME} "
            f"WHERE symbol = %s ORDER BY trade_date DESC LIMIT 220",
            engine, params=(symbol,),
        )
        if len(df) < 15:
            return {}

        closes = df["close"].iloc[::-1].astype(float).reset_index(drop=True)

        # RSI-14
        delta = closes.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi_series = 100 - 100 / (1 + rs)
        rsi_val = float(rsi_series.iloc[-1]) if not rsi_series.empty else None

        # SMA-200
        sma200 = (
            float(closes.rolling(200).mean().iloc[-1])
            if len(closes) >= 200 else None
        )

        return {
            "rsi":    round(rsi_val, 1) if rsi_val is not None else None,
            "sma200": round(sma200, 2)  if sma200  is not None else None,
        }
    except Exception as e:
        logger.debug(f"Technical fetch error for {symbol}: {e}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────

def _score_stock(fund: dict, tech: dict, sector_pe_medians: dict) -> dict:
    """
    Score a stock 0–110 (100 fundamental + 10 technical bonus).
    Returns score, grade, and per-pillar breakdown.
    """
    score = 0
    breakdown = {}

    pe  = fund.get("pe_ratio")
    pb  = fund.get("pb_ratio")
    ev  = fund.get("ev_ebitda")
    roe = fund.get("roe") or 0
    de  = fund.get("debt_equity")
    dy  = fund.get("dividend_yield") or 0
    rg  = fund.get("revenue_growth") or 0
    eg  = fund.get("eps_growth") or 0

    sector            = fund.get("sector", "Unknown")
    sector_median_pe  = sector_pe_medians.get(sector)

    # ── P/E vs sector median (25 pts) ────────────────────────────────────
    pe_score = 0
    pe_vs_sector_pct = None
    if pe and pe > 0:
        if sector_median_pe and sector_median_pe > 0:
            pe_vs_sector_pct = round((pe / sector_median_pe - 1) * 100, 1)
            discount = max(0.0, 1.0 - pe / sector_median_pe)
            pe_score = round(min(25, discount * 30), 1)   # 30 so full 25 pts at ~17% discount
        else:
            # No sector median — score by absolute P/E
            if 0 < pe < 10:      pe_score = 22
            elif pe < 15:        pe_score = 17
            elif pe < 20:        pe_score = 12
            elif pe < 25:        pe_score = 7
            elif pe < 35:        pe_score = 3
    breakdown["pe"] = pe_score
    score += pe_score

    # ── P/B ratio (15 pts) ───────────────────────────────────────────────
    pb_score = 0
    if pb is not None and pb > 0:
        if pb < 1.0:    pb_score = 15
        elif pb < 1.5:  pb_score = 12
        elif pb < 2.0:  pb_score = 9
        elif pb < 3.0:  pb_score = 5
        elif pb < 5.0:  pb_score = 2
    breakdown["pb"] = pb_score
    score += pb_score

    # ── EV/EBITDA (10 pts) ───────────────────────────────────────────────
    ev_score = 0
    if ev is not None and 0 < ev < 200:   # filter garbage (negatives, extreme)
        if ev < 8:     ev_score = 10
        elif ev < 12:  ev_score = 7
        elif ev < 18:  ev_score = 4
        elif ev < 25:  ev_score = 2
    breakdown["ev_ebitda"] = ev_score
    score += ev_score

    # ── ROE (15 pts) ─────────────────────────────────────────────────────
    roe_score = 0
    if roe >= 25:    roe_score = 15
    elif roe >= 20:  roe_score = 12
    elif roe >= 15:  roe_score = 9
    elif roe >= 10:  roe_score = 6
    elif roe >= 5:   roe_score = 3
    breakdown["roe"] = roe_score
    score += roe_score

    # ── Debt/Equity (10 pts) ─────────────────────────────────────────────
    de_score = 5   # neutral if unknown
    if de is not None:
        if de < 0:      de_score = 10   # net-cash company
        elif de < 0.3:  de_score = 10
        elif de < 0.7:  de_score = 7
        elif de < 1.0:  de_score = 5
        elif de < 1.5:  de_score = 3
        else:           de_score = 1
    breakdown["debt_equity"] = de_score
    score += de_score

    # ── Revenue growth YoY (8 pts) ───────────────────────────────────────
    rg_score = 0
    if rg > 25:    rg_score = 8
    elif rg > 15:  rg_score = 6
    elif rg > 5:   rg_score = 4
    elif rg > 0:   rg_score = 2
    elif rg > -10: rg_score = 1
    breakdown["revenue_growth"] = rg_score
    score += rg_score

    # ── EPS growth YoY (7 pts) ───────────────────────────────────────────
    eg_score = 0
    if eg > 25:    eg_score = 7
    elif eg > 15:  eg_score = 5
    elif eg > 5:   eg_score = 3
    elif eg > 0:   eg_score = 2
    elif eg > -10: eg_score = 1
    breakdown["eps_growth"] = eg_score
    score += eg_score

    # ── Dividend yield (10 pts) ──────────────────────────────────────────
    dy_score = 0
    if dy >= 5:    dy_score = 10
    elif dy >= 3:  dy_score = 8
    elif dy >= 2:  dy_score = 6
    elif dy >= 1:  dy_score = 3
    breakdown["dividend_yield"] = dy_score
    score += dy_score

    # ── Technical bonus (max 10 pts) ─────────────────────────────────────
    tech_score = 0
    rsi    = tech.get("rsi")
    sma200 = tech.get("sma200")
    price  = fund.get("current_price")

    if rsi is not None:
        if 28 <= rsi <= 45:   tech_score += 5    # oversold → high reversion potential
        elif 45 < rsi <= 55:  tech_score += 3    # neutral
        elif 55 < rsi <= 65:  tech_score += 1

    if sma200 and price and price > 0:
        pct_vs_sma = (price / sma200 - 1) * 100
        if -20 <= pct_vs_sma <= -5:   tech_score += 5   # below SMA200 — potential bounce
        elif -5 < pct_vs_sma <= 5:    tech_score += 3   # just around SMA200
        elif 5 < pct_vs_sma <= 15:    tech_score += 1

    breakdown["technical"] = tech_score
    score += tech_score

    score = min(110, round(score, 1))   # cap at 110 (100 + 10 bonus)

    # Grade (based on fundamental score only, i.e. excluding tech bonus)
    fundamental_score = score - tech_score
    if fundamental_score >= 75:   grade = "Deep Value"
    elif fundamental_score >= 60: grade = "Good Value"
    elif fundamental_score >= 45: grade = "Moderate Value"
    elif fundamental_score >= 30: grade = "Fair Value"
    else:                         grade = "Avoid"

    # 52-week position
    h52w = fund.get("high_52w")
    l52w = fund.get("low_52w")
    pct_from_52w_high = round((price / h52w - 1) * 100, 1) if (h52w and price) else None
    pct_from_52w_low  = round((price / l52w - 1) * 100, 1) if (l52w and price) else None

    return {
        "value_score":        score,
        "grade":              grade,
        "pe_vs_sector_pct":   pe_vs_sector_pct,
        "pct_from_52w_high":  pct_from_52w_high,
        "pct_from_52w_low":   pct_from_52w_low,
        "score_breakdown":    breakdown,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main scan entry point
# ─────────────────────────────────────────────────────────────────────────────

def undervalued_scan(
    symbols: list = None,
    force_refresh: bool = False,
    progress_cb=None,
) -> dict:
    """
    Screen NIFTY 500 (or a custom list) for undervalued stocks.

    Args:
        symbols:       List of NSE symbols. Defaults to NIFTY_500.
        force_refresh: Ignore on-disk cache and run a fresh scan.
        progress_cb:   Optional callable(processed: int, total: int, symbol: str).

    Returns:
        dict with keys:
          results       — list of scored stocks (sorted by value_score DESC)
          cached_at     — ISO timestamp of when results were computed
          count         — number of stocks successfully scored
          scan_duration_s
          sector_pe_medians — {sector: median_pe} used for relative scoring
    """
    if not force_refresh:
        cached = _load_cache()
        if cached:
            logger.info("Value screener: serving from cache")
            return cached

    if symbols is None:
        from config.nse_indices import NIFTY_500
        symbols = NIFTY_500

    started = datetime.now()
    logger.info(f"Value screener: scanning {len(symbols)} symbols …")

    engine    = get_engine()
    fund_data = {}
    total     = len(symbols)

    # ── Step 1: fetch fundamentals from yfinance ──────────────────────────
    for i, sym in enumerate(symbols, 1):
        data = _fetch_fundamentals(sym)
        if "error" not in data:
            fund_data[sym] = data
        if progress_cb:
            progress_cb(i, total, sym)
        time.sleep(0.25)   # polite rate limit

    logger.info(f"Fundamentals fetched: {len(fund_data)}/{total} symbols")

    # ── Step 2: sector P/E medians for relative valuation ─────────────────
    sector_pes: dict = {}
    for data in fund_data.values():
        sector = data.get("sector", "Unknown")
        pe = data.get("pe_ratio")
        if pe and 0 < pe < 150:   # exclude negative / extreme outliers
            sector_pes.setdefault(sector, []).append(pe)

    sector_pe_medians = {
        s: round(float(np.median(pes)), 1)
        for s, pes in sector_pes.items()
        if pes
    }

    # ── Step 3: score each stock ──────────────────────────────────────────
    results = []
    for sym, fund in fund_data.items():
        tech   = _get_technical(engine, sym)
        scored = _score_stock(fund, tech, sector_pe_medians)

        pe_val = fund.get("pe_ratio")
        pb_val = fund.get("pb_ratio")
        ev_val = fund.get("ev_ebitda")
        roe_v  = fund.get("roe")
        de_val = fund.get("debt_equity")
        dy_val = fund.get("dividend_yield")
        rg_val = fund.get("revenue_growth")
        eg_val = fund.get("eps_growth")
        mc_val = fund.get("market_cap")

        results.append({
            "symbol":          sym,
            "sector":          fund.get("sector") or get_sector(sym),
            "industry":        fund.get("industry"),
            "last_price":      fund.get("current_price"),
            "market_cap_cr":   round(mc_val / 1e7, 0) if mc_val else None,
            "high_52w":        fund.get("high_52w"),
            "low_52w":         fund.get("low_52w"),
            # Valuation
            "pe_ratio":        round(pe_val, 1)  if pe_val  is not None else None,
            "pb_ratio":        round(pb_val, 2)  if pb_val  is not None else None,
            "ev_ebitda":       round(ev_val, 1)  if ev_val  is not None else None,
            "sector_median_pe": sector_pe_medians.get(fund.get("sector", "")),
            # Quality
            "roe":             round(roe_v, 1)   if roe_v   is not None else None,
            "debt_equity":     round(de_val, 2)  if de_val  is not None else None,
            # Growth
            "revenue_growth":  round(rg_val, 1)  if rg_val  is not None else None,
            "eps_growth":      round(eg_val, 1)  if eg_val  is not None else None,
            # Income
            "dividend_yield":  round(dy_val, 2)  if dy_val  is not None else None,
            # Technical
            "rsi":             tech.get("rsi"),
            "sma200":          tech.get("sma200"),
            "above_sma200":    (fund.get("current_price") > tech["sma200"])
                               if (tech.get("sma200") and fund.get("current_price")) else None,
            # Score
            **scored,
        })

    results.sort(key=lambda x: x["value_score"], reverse=True)

    duration = round((datetime.now() - started).total_seconds(), 1)
    logger.info(
        f"Value screener complete: {len(results)} stocks scored in {duration}s"
    )

    payload = {
        "results":            results,
        "cached_at":          datetime.now().isoformat(),
        "count":              len(results),
        "scan_duration_s":    duration,
        "sector_pe_medians":  sector_pe_medians,
    }
    _save_cache(payload)
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# Buy Signal — 4-gate decision engine
# ─────────────────────────────────────────────────────────────────────────────

# Verdicts (in order of desirability)
VERDICT_STRONG_BUY   = "STRONG_BUY"
VERDICT_GOOD_BUY     = "GOOD_BUY"
VERDICT_AI_CAUTION   = "AI_CAUTION"
VERDICT_WAIT_ENTRY   = "WAIT_ENTRY"
VERDICT_WAIT_REGIME  = "WAIT_REGIME"
VERDICT_VALUE_TRAP   = "VALUE_TRAP"
VERDICT_AVOID        = "AVOID"

VERDICT_LABELS = {
    VERDICT_STRONG_BUY:  "Strong Buy",
    VERDICT_GOOD_BUY:    "Good Buy",
    VERDICT_AI_CAUTION:  "AI Caution",
    VERDICT_WAIT_ENTRY:  "Wait — Entry",
    VERDICT_WAIT_REGIME: "Wait — Bear",
    VERDICT_VALUE_TRAP:  "Value Trap",
    VERDICT_AVOID:       "Avoid",
}


def compute_buy_signal(stock: dict, regime: str = "Neutral", ml_prob: float = None) -> dict:
    """
    Evaluate a value-screened stock through 4 decision gates and return a buy verdict.

    Args:
        stock     : One result dict from undervalued_scan() — must contain
                    value_score, grade, roe, debt_equity, revenue_growth,
                    rsi, sma200, above_sma200, last_price.
        regime    : Current market regime — "Bull" | "Neutral" | "Bear"
        ml_prob   : Optional ML model 5-day probability (0–1). None = skip Gate 4.

    Returns:
        {
            "verdict":        VERDICT_* constant,
            "verdict_label":  Human-readable label,
            "gates": {
                "g1_quality":   True | False,
                "g2_technical": True | False,
                "g3_regime":    True | False,
                "g4_ml":        True | False | None,
            },
            "gates_passed": int,   # out of applicable gates (3 or 4)
            "conviction":   int,   # 0–100 summary score
            "reasons":      list,  # short strings explaining each gate result
        }
    """
    reasons   = []
    gates     = {}

    score    = stock.get("value_score", 0) or 0
    roe      = stock.get("roe")
    de       = stock.get("debt_equity")
    rev_g    = stock.get("revenue_growth")
    rsi      = stock.get("rsi")
    sma200   = stock.get("sma200")
    price    = stock.get("last_price")
    above_s  = stock.get("above_sma200")

    # ── Gate 1: Quality filter ─────────────────────────────────────────────
    g1_fail_reasons = []

    if score < 60:
        g1_fail_reasons.append(f"Score {score:.0f} below 60 threshold")

    # Value trap check: decent score but poor business quality
    is_value_trap = False
    if score >= 60:
        if roe is not None and roe < 5:
            g1_fail_reasons.append(f"ROE {roe:.1f}% < 5% — may be a value trap")
            is_value_trap = True
        if de is not None and de > 2.0:
            g1_fail_reasons.append(f"D/E {de:.1f}x > 2.0 — high leverage risk")
            is_value_trap = True
        if rev_g is not None and rev_g < -15:
            g1_fail_reasons.append(f"Revenue declining {rev_g:.1f}% — business shrinking")
            is_value_trap = True

    if not g1_fail_reasons:
        # Additional quality nudges (non-blocking but tracked)
        if roe is not None and roe >= 15:
            reasons.append(f"✓ ROE {roe:.1f}% — quality business")
        elif roe is not None and roe >= 10:
            reasons.append(f"✓ ROE {roe:.1f}% — acceptable quality")
        elif roe is not None:
            reasons.append(f"⚠ ROE {roe:.1f}% — borderline quality")
        if de is not None and de <= 0.5:
            reasons.append(f"✓ D/E {de:.2f}x — low debt")
        elif de is not None and de > 1.0:
            reasons.append(f"⚠ D/E {de:.2f}x — moderate debt")
        gates["g1_quality"] = True
    else:
        reasons.extend([f"✗ {r}" for r in g1_fail_reasons])
        gates["g1_quality"] = False

    # ── Gate 2: Technical entry ────────────────────────────────────────────
    g2_fail_reasons = []

    # RSI check — avoid overbought (>70) or extreme capitulation (<22)
    if rsi is not None:
        if rsi > 70:
            g2_fail_reasons.append(f"RSI {rsi:.1f} > 70 — overbought, wait for pullback")
        elif rsi < 22:
            g2_fail_reasons.append(f"RSI {rsi:.1f} < 22 — extreme selling, wait for stabilisation")
        elif rsi <= 45:
            reasons.append(f"✓ RSI {rsi:.1f} — oversold/neutral, good entry zone")
        elif rsi <= 60:
            reasons.append(f"✓ RSI {rsi:.1f} — neutral momentum")
        else:
            reasons.append(f"~ RSI {rsi:.1f} — mildly extended but acceptable")

    # SMA200 check — avoid stocks in structural breakdown (>15% below SMA200)
    if sma200 and price and price > 0:
        pct_vs_sma = (price / sma200 - 1) * 100
        if pct_vs_sma < -20:
            g2_fail_reasons.append(
                f"Price {pct_vs_sma:.1f}% below SMA200 — structural downtrend"
            )
        elif pct_vs_sma < -8:
            reasons.append(f"~ Price {pct_vs_sma:.1f}% below SMA200 — approaching support")
        elif above_s:
            reasons.append(f"✓ Price above SMA200 — uptrend intact")
        else:
            reasons.append(f"~ Price near SMA200 — consolidation zone")

    if not g2_fail_reasons:
        gates["g2_technical"] = True
    else:
        reasons.extend([f"✗ {r}" for r in g2_fail_reasons])
        gates["g2_technical"] = False

    # ── Gate 3: Market regime ──────────────────────────────────────────────
    if regime == "Bull":
        gates["g3_regime"] = True
        reasons.append("✓ Bull market — favourable entry conditions")
    elif regime == "Neutral":
        gates["g3_regime"] = True
        reasons.append("~ Neutral market — selective buys only")
    else:  # Bear
        gates["g3_regime"] = False
        reasons.append("✗ Bear market — avoid new positions, even cheap stocks fall")

    # ── Gate 4: ML model ───────────────────────────────────────────────────
    if ml_prob is None:
        gates["g4_ml"] = None   # model not available, gate skipped
    elif ml_prob >= 0.60:
        gates["g4_ml"] = True
        reasons.append(f"✓ ML probability {ml_prob*100:.0f}% ≥ 60% — AI confirms upside")
    elif ml_prob >= 0.50:
        gates["g4_ml"] = True   # borderline pass
        reasons.append(f"~ ML probability {ml_prob*100:.0f}% — AI borderline positive")
    else:
        gates["g4_ml"] = False
        reasons.append(f"✗ ML probability {ml_prob*100:.0f}% < 50% — AI not confident")

    # ── Verdict resolution ─────────────────────────────────────────────────
    g1 = gates.get("g1_quality",  False)
    g2 = gates.get("g2_technical", False)
    g3 = gates.get("g3_regime",    False)
    g4 = gates.get("g4_ml")   # True | False | None

    applicable = 3 + (0 if g4 is None else 1)
    passed     = sum([
        1 if g1 else 0,
        1 if g2 else 0,
        1 if g3 else 0,
        1 if (g4 is True) else 0,
    ])

    if not g1:
        verdict = VERDICT_VALUE_TRAP if is_value_trap else VERDICT_AVOID
    elif not g3:
        verdict = VERDICT_WAIT_REGIME
    elif not g2:
        verdict = VERDICT_WAIT_ENTRY
    elif g4 is False:
        verdict = VERDICT_AI_CAUTION
    else:
        # g1, g2, g3 all pass; g4 is True or None
        if g4 is True and ml_prob is not None and ml_prob >= 0.60:
            verdict = VERDICT_STRONG_BUY
        elif g4 is None:
            # No ML — go by score strength
            verdict = VERDICT_STRONG_BUY if score >= 70 else VERDICT_GOOD_BUY
        else:
            verdict = VERDICT_GOOD_BUY   # g4 borderline (50–60%)

    # Conviction score 0–100
    base = (passed / max(applicable, 1)) * 70
    # Bonus: score quality
    score_bonus = min(20, max(0, (score - 60) / 2))
    # Bonus: ROE quality
    roe_bonus = min(10, max(0, ((roe or 0) - 10) / 3)) if roe else 0
    conviction = round(min(100, base + score_bonus + roe_bonus))

    return {
        "verdict":       verdict,
        "verdict_label": VERDICT_LABELS[verdict],
        "gates":         gates,
        "gates_passed":  passed,
        "gates_total":   applicable,
        "conviction":    conviction,
        "reasons":       reasons,
        "regime_used":   regime,
        "ml_prob":       round(ml_prob * 100, 1) if ml_prob is not None else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Cache helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_cache() -> dict | None:
    """Return cached results if they exist and are within TTL."""
    try:
        if not os.path.exists(CACHE_FILE):
            return None
        with open(CACHE_FILE) as f:
            data = json.load(f)
        cached_at = datetime.fromisoformat(data["cached_at"])
        if datetime.now() - cached_at < timedelta(hours=CACHE_TTL_HOURS):
            return data
    except Exception:
        pass
    return None


def _save_cache(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"Value cache save error: {e}")


def get_cache_info() -> dict:
    """Return cache metadata without loading the full result set."""
    try:
        if not os.path.exists(CACHE_FILE):
            return {"exists": False, "age_hours": None, "count": 0, "cached_at": None}
        with open(CACHE_FILE) as f:
            data = json.load(f)
        cached_at = datetime.fromisoformat(data["cached_at"])
        age_hours = round((datetime.now() - cached_at).total_seconds() / 3600, 1)
        return {
            "exists":     True,
            "cached_at":  data["cached_at"],
            "age_hours":  age_hours,
            "count":      data.get("count", 0),
            "is_fresh":   age_hours < CACHE_TTL_HOURS,
        }
    except Exception:
        return {"exists": False, "age_hours": None, "count": 0, "cached_at": None}
