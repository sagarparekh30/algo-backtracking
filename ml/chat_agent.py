"""
ml/chat_agent.py
================
Self-contained stock AI chatbot — zero external APIs, no API key required.

Uses your trained ML model + existing screener/analyst tools to answer
natural-language questions about NSE stocks.

Intent routing handles:
  buy / recommend → top buy-signal stocks (ML + value screener)
  analyse <SYMBOL> → deep-dive single stock
  market / regime  → Bull/Neutral/Bear overview + top ML picks
  momentum / rs / breakout → relative strength screener
  52w / 52 week high       → 52W high screener
  volume / spike           → volume spike screener
  sector <X> / index <Y>   → metadata filter
  debt / dividend / growth / smallcap / midcap → filtered value picks
  anything else            → full value report

Streams response in small text chunks so the UI gets a typing effect.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Generator, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt(v, suffix="", decimals=1):
    if v is None:
        return "N/A"
    try:
        return f"{round(float(v), decimals)}{suffix}"
    except Exception:
        return str(v)


def _verdict_label(verdict: str) -> str:
    return {
        "STRONG_BUY":  "🟢 Strong Buy",
        "GOOD_BUY":    "✅ Good Buy",
        "AI_CAUTION":  "⚠️ AI Caution",
        "WAIT_ENTRY":  "⏳ Wait Entry",
        "WAIT_REGIME": "🐻 Wait — Bear",
        "VALUE_TRAP":  "⚑ Value Trap",
        "AVOID":       "🔴 Avoid",
    }.get(verdict, verdict or "—")


# ── Data loaders ──────────────────────────────────────────────────────────────

def _load_value_stocks() -> list[dict]:
    """Load undervalued screener cache."""
    try:
        from strategies.value_screener import _load_cache, CACHE_FILE
        cache = _load_cache()
        if cache is None and os.path.exists(CACHE_FILE):
            with open(CACHE_FILE) as f:
                cache = json.load(f)
        return (cache or {}).get("results", [])
    except Exception as e:
        logger.warning(f"Value cache load failed: {e}")
        return []


def _load_ml_probs(regime: str = "Neutral") -> dict[str, float]:
    """Return {symbol: prob_5d} from ML predictor."""
    try:
        from ml.model import MLPredictor
        pred = MLPredictor()
        pred.load()
        if pred.clf is None:
            return {}
        return {
            r["symbol"]: r.get("buy_probability") or r.get("prob_5d", 0)
            for r in pred.predict_all(regime=regime)
            if r.get("symbol")
        }
    except Exception as e:
        logger.warning(f"ML probs load failed: {e}")
        return {}


def _get_regime() -> tuple[str, dict]:
    """Return (regime_str, regime_dict)."""
    try:
        from ml.regime import compute_regime
        d = compute_regime()
        return d.get("regime", "Neutral"), d
    except Exception:
        return "Neutral", {}


def _attach_signals(stocks: list[dict], ml_probs: dict, regime: str) -> list[dict]:
    """Attach buy signal verdict to each stock dict."""
    try:
        from strategies.value_screener import compute_buy_signal
    except Exception:
        return stocks
    out = []
    for s in stocks:
        sym = s.get("symbol", "")
        ml = ml_probs.get(sym)
        sig = compute_buy_signal(s, regime=regime, ml_prob=ml)
        out.append({**s, "_verdict": sig.get("verdict", ""), "_conviction": sig.get("conviction_score", 0), "_ml_prob": ml})
    return out


# ── Response formatters ────────────────────────────────────────────────────────

def _fmt_stock_row(s: dict, rank: int) -> str:
    sym       = s.get("symbol", "?")
    sector    = s.get("sector") or "—"
    score     = s.get("value_score", 0) or s.get("_conviction", 0)
    verdict   = _verdict_label(s.get("_verdict", ""))
    ml        = s.get("_ml_prob")
    ml_str    = f" · ML {ml*100:.0f}%" if ml is not None else ""
    pe        = s.get("pe_ratio")
    roe       = s.get("roe")
    price     = s.get("last_price")
    price_str = f" · ₹{_fmt(price)}" if price else ""
    return (
        f"**{rank}. {sym}** ({sector}) — {verdict}\n"
        f"   Score {score}/100{ml_str} · P/E {_fmt(pe)}x · ROE {_fmt(roe,'%')}{price_str}"
    )


def _answer_buy_from_ml(ml_probs: dict, regime: str, limit: int = 10, sector_filter: str = None) -> str:
    """Fallback: answer purely from ML predictions when no value cache exists."""
    if not ml_probs:
        return (
            f"**Market regime: {regime}**\n\n"
            "The ML model has no predictions yet. Make sure training has completed "
            "(check **More → Data → Auto-Scheduler** or trigger a retrain from the AI tab)."
        )

    # Get metadata for sector info
    meta_map: dict[str, dict] = {}
    try:
        from db.connection import get_conn
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT symbol, sector, company_name, market_cap_cat FROM stock_metadata WHERE is_active")
            for sym, sector, name, mcap in cur.fetchall():
                meta_map[sym] = {"sector": sector or "—", "company_name": name or sym, "market_cap_cat": mcap or ""}
        conn.close()
    except Exception:
        pass

    # Get latest prices
    prices: dict[str, float] = {}
    try:
        from db.connection import get_conn
        from config.settings import TABLE_NAME
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT DISTINCT ON (symbol) symbol, close
                FROM {TABLE_NAME}
                ORDER BY symbol, trade_date DESC
            """)
            prices = {r[0]: float(r[1]) for r in cur.fetchall()}
        conn.close()
    except Exception:
        pass

    items = sorted(ml_probs.items(), key=lambda x: -x[1])

    if sector_filter:
        items = [(sym, p) for sym, p in items
                 if (meta_map.get(sym, {}).get("sector") or "").lower() == sector_filter.lower()]
        if not items:
            return f"No **{sector_filter}** stocks found in ML predictions. Run metadata sync from Data tab."

    regime_icon = {"Bull": "🐂", "Bear": "🐻", "Neutral": "⚖️"}.get(regime, "")
    threshold = {"Bull": 0.55, "Neutral": 0.60, "Bear": 0.65}.get(regime, 0.60)
    top = [(sym, p) for sym, p in items if p >= threshold][:limit]

    if not top:
        top = items[:limit]  # show best available even if below threshold

    lines = [
        f"## Top {len(top)} ML Picks — {regime_icon} {regime} Market\n",
        f"*Ranked by ML 5-day upside probability · {len(ml_probs)} stocks tracked*\n",
    ]

    for i, (sym, prob) in enumerate(top, 1):
        m = meta_map.get(sym, {})
        sector  = m.get("sector", "—")
        price   = prices.get(sym)
        price_s = f" · ₹{price:.1f}" if price else ""
        bar = "▰" * int(prob * 10) + "▱" * (10 - int(prob * 10))
        signal = "🟢 Strong Buy" if prob >= 0.70 else ("✅ Good Buy" if prob >= 0.60 else "⚠️ Watch")
        lines.append(f"**{i}. {sym}** ({sector}) — {signal}{price_s}")
        lines.append(f"   ML probability: **{prob*100:.0f}%** {bar}")
        lines.append("")

    lines.append("---")
    lines.append(f"> *Tip: Run **Screener → Undervalued → Scan** to add fundamental analysis (P/E, ROE, debt) to these signals.*")
    lines.append("> *Not financial advice. Do your own research.*")
    return "\n".join(lines)


def _answer_buy(stocks: list[dict], limit: int = 8, sector_filter: str = None) -> str:
    regime, rd = _get_regime()
    ml_probs   = _load_ml_probs(regime)

    # No value cache — fall back to pure ML predictions
    if not stocks:
        return _answer_buy_from_ml(ml_probs, regime, limit=limit, sector_filter=sector_filter)

    filtered = stocks

    if sector_filter:
        filtered = [s for s in filtered if (s.get("sector") or "").lower() == sector_filter.lower()]
        if not filtered:
            return _answer_buy_from_ml(
                {k: v for k, v in ml_probs.items()}, regime, limit=limit, sector_filter=sector_filter
            )

    enriched = _attach_signals(filtered, ml_probs, regime)

    # Sort: STRONG_BUY > GOOD_BUY > AI_CAUTION > rest; then conviction desc
    order = {"STRONG_BUY": 0, "GOOD_BUY": 1, "AI_CAUTION": 2, "WAIT_ENTRY": 3}
    actionable = [s for s in enriched if s.get("_verdict") in order]
    actionable.sort(key=lambda s: (order.get(s["_verdict"], 9), -s.get("_conviction", 0)))

    if not actionable:
        # Has value data but no signals — fall back to ML
        return _answer_buy_from_ml(ml_probs, regime, limit=limit, sector_filter=sector_filter)

    top = actionable[:limit]
    lines = [
        f"## Top {len(top)} Buy Picks — {regime} Market\n",
        f"*Based on ML model + value score · {len(stocks)} stocks scanned*\n",
    ]
    for i, s in enumerate(top, 1):
        lines.append(_fmt_stock_row(s, i))
        lines.append("")

    strong = sum(1 for s in actionable if s["_verdict"] == "STRONG_BUY")
    good   = sum(1 for s in actionable if s["_verdict"] == "GOOD_BUY")
    lines.append(f"---\n🟢 **{strong}** Strong Buy · ✅ **{good}** Good Buy · Regime: **{regime}**")
    lines.append("\n> *Not financial advice. Do your own research before investing.*")
    return "\n".join(lines)


def _answer_stock(symbol: str, stocks: list[dict]) -> str:
    symbol = symbol.upper().strip()
    regime, _ = _get_regime()
    ml_probs  = _load_ml_probs(regime)

    # Try screener data first
    s = next((x for x in stocks if x.get("symbol") == symbol), None)

    # Metadata
    meta = {}
    try:
        from fetcher.stock_metadata_fetcher import get_symbol_metadata
        meta = get_symbol_metadata(symbol) or {}
    except Exception:
        pass

    # Candle data
    from db.connection import get_conn
    from config.settings import TABLE_NAME
    price_data = {}
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT close FROM {TABLE_NAME} WHERE symbol=%s ORDER BY trade_date DESC LIMIT 60",
                (symbol,)
            )
            prices = [r[0] for r in cur.fetchall()]
        conn.close()
        if prices:
            sma200_len = min(200, len(prices))
            sma200 = sum(prices[:sma200_len]) / sma200_len
            gains, losses = [], []
            for i in range(min(14, len(prices) - 1)):
                d = float(prices[i]) - float(prices[i+1])
                (gains if d > 0 else losses).append(abs(d))
            avg_g = sum(gains) / 14 if gains else 0.0001
            avg_l = sum(losses) / 14 if losses else 0.0001
            rsi = round(100 - (100 / (1 + avg_g / avg_l)), 1)
            ret20 = round((float(prices[0]) / float(prices[min(19, len(prices)-1)]) - 1) * 100, 2) if len(prices) >= 20 else None
            price_data = {
                "price":       round(float(prices[0]), 2),
                "sma200":      round(sma200, 2),
                "above_sma200": float(prices[0]) > sma200,
                "rsi":         rsi,
                "ret20":       ret20,
            }
    except Exception as e:
        logger.warning(f"Candle fetch for {symbol}: {e}")

    if not s and not price_data and not meta:
        return f"**{symbol}** not found in the database. The symbol may not exist or candle data hasn't been fetched yet."

    ml  = ml_probs.get(symbol)
    lines = [f"## {symbol} — Analysis\n"]

    company = meta.get("company_name") or (s or {}).get("company_name", symbol)
    sector  = meta.get("sector") or (s or {}).get("sector", "Unknown")
    indices = meta.get("indices") or []
    mcap    = meta.get("market_cap_cat", "")
    lines.append(f"**{company}** · {sector}" + (f" · {mcap}" if mcap else "") + "\n")

    if indices:
        lines.append(f"📊 Indices: {', '.join(indices[:5])}\n")

    if price_data:
        trend = "↑ above SMA200" if price_data.get("above_sma200") else "↓ below SMA200"
        lines.append(f"**Price:** ₹{_fmt(price_data.get('price'))} · {trend}")
        lines.append(f"**Technical:** RSI {_fmt(price_data.get('rsi'))} · 20D return {_fmt(price_data.get('ret20'),'%')}")

    if ml is not None:
        conf = "High" if ml >= 0.65 else ("Moderate" if ml >= 0.50 else "Low")
        lines.append(f"**ML Model:** {ml*100:.0f}% 5-day upside probability — {conf} confidence")

    if s:
        lines.append(f"\n**Valuation:** P/E {_fmt(s.get('pe_ratio'),'x')} · P/B {_fmt(s.get('pb_ratio'),'x')} · EV/EBITDA {_fmt(s.get('ev_ebitda'),'x')}")
        lines.append(f"**Quality:** ROE {_fmt(s.get('roe'),'%')} · D/E {_fmt(s.get('debt_equity'),'x',2)}")
        lines.append(f"**Growth:** Rev {_fmt(s.get('revenue_growth'),'%')} · EPS {_fmt(s.get('eps_growth'),'%')}")
        lines.append(f"**Value Score:** {s.get('value_score', 'N/A')}/100 ({s.get('grade', '')})")

        try:
            from strategies.value_screener import compute_buy_signal
            sig = compute_buy_signal(s, regime=regime, ml_prob=ml)
            verdict = _verdict_label(sig.get("verdict", ""))
            lines.append(f"\n**Signal:** {verdict} (conviction {sig.get('conviction_score',0)}/100)")
            for reason in sig.get("reasons", [])[:5]:
                lines.append(f"- {reason}")
        except Exception:
            pass
    else:
        lines.append("\n*Full fundamental data not available — run Undervalued scan to include this stock.*")

    lines.append("\n> *Not financial advice.*")
    return "\n".join(lines)


def _answer_market() -> str:
    regime, rd = _get_regime()
    ml_probs = _load_ml_probs(regime)

    top_picks = sorted(ml_probs.items(), key=lambda x: -x[1])[:10]

    regime_icon = {"Bull": "🐂", "Bear": "🐻", "Neutral": "⚖️"}.get(regime, "")
    breadth  = rd.get("breadth_pct")
    vol_lbl  = rd.get("volatility_label", "")
    avg_atr  = rd.get("avg_atr_pct")

    lines = [f"## Market Overview — {regime_icon} {regime}\n"]

    if breadth is not None:
        lines.append(f"**Breadth:** {breadth:.1f}% of stocks above SMA200")
    if avg_atr:
        lines.append(f"**Volatility:** {vol_lbl} (avg ATR {_fmt(avg_atr,'%')})")

    if regime == "Bull":
        lines.append("\n✅ Bull market — favourable for new positions across sectors.")
    elif regime == "Bear":
        lines.append("\n⚠️ Bear market — avoid new positions; even cheap stocks tend to fall with the tide.")
    else:
        lines.append("\n⚖️ Neutral market — selective buys only; prioritise Strong Buy signals with high ML probability.")

    if top_picks:
        lines.append(f"\n## Top {len(top_picks)} ML Predictions\n")
        for i, (sym, prob) in enumerate(top_picks, 1):
            bar = "▰" * int(prob * 10) + "▱" * (10 - int(prob * 10))
            lines.append(f"**{i}. {sym}** — {prob*100:.0f}% · {bar}")

    lines.append("\n> *ML probabilities are 5-day price-up predictions from your trained Random Forest model.*")
    return "\n".join(lines)


def _answer_screener(stype: str) -> str:
    try:
        from strategies.screener import relative_strength_scan, fiftytwo_week_scan, volume_spike_scan
        if stype == "rs":
            rows = relative_strength_scan()[:12]
            title = "Relative Strength — Outperformers"
            key   = "rs_score"
        elif stype == "52w":
            rows = fiftytwo_week_scan()[:12]
            title = "52-Week Highs — Near Breakout"
            key   = "pct_from_high"
        else:
            rows = volume_spike_scan()[:12]
            title = "Volume Spikes — Unusual Activity"
            key   = "volume_multiplier"

        if not rows:
            return f"No results for **{title}** screener. Make sure candle data is loaded."

        lines = [f"## {title}\n"]
        for i, r in enumerate(rows, 1):
            sym    = r.get("symbol", "?")
            sector = r.get("sector") or "—"
            val    = r.get(key)
            val_str = f"{_fmt(val)}x" if stype == "volume" else (_fmt(val, "%") if key == "pct_from_high" else _fmt(val))
            ret20  = r.get("return_20d") or r.get("ret_20d")
            ret_str = f" · 20D {_fmt(ret20,'%')}" if ret20 is not None else ""
            lines.append(f"**{i}. {sym}** ({sector}) — {val_str}{ret_str}")
        return "\n".join(lines)
    except Exception as e:
        return f"Screener error: {e}"


def _answer_sector_index(sector: str = None, index: str = None) -> str:
    try:
        from db.connection import get_conn
        conn = get_conn()
        with conn.cursor() as cur:
            if index:
                cur.execute(
                    "SELECT symbol, company_name, sector, market_cap_cat FROM stock_metadata "
                    "WHERE %s = ANY(indices) AND is_active ORDER BY symbol LIMIT 25",
                    (index,)
                )
            else:
                cur.execute(
                    "SELECT symbol, company_name, sector, market_cap_cat FROM stock_metadata "
                    "WHERE LOWER(sector) = LOWER(%s) AND is_active ORDER BY symbol LIMIT 25",
                    (sector,)
                )
            rows = cur.fetchall()
        conn.close()
        label = index or sector
        if not rows:
            return f"No stocks found for **{label}**. Run the metadata sync from Data tab to populate stock info."
        lines = [f"## {label} Stocks ({len(rows)} found)\n"]
        for sym, name, sec, mcap in rows:
            lines.append(f"- **{sym}** — {name or sym} ({mcap or sec or '—'})")
        return "\n".join(lines)
    except Exception as e:
        return f"Search error: {e}"


# ── Intent detection ───────────────────────────────────────────────────────────

_SECTOR_KEYWORDS = {
    "it":       "IT",        "technology": "IT",    "tech":      "IT",
    "bank":     "Banking",   "banking":    "Banking",
    "pharma":   "Pharma",    "healthcare": "Pharma",
    "auto":     "Auto",      "automobile": "Auto",
    "fmcg":     "FMCG",      "consumer goods": "FMCG",
    "finance":  "Finance",   "financial":  "Finance", "nbfc": "Finance",
    "metal":    "Metals",    "metals":     "Metals",  "steel": "Metals",
    "energy":   "Energy",    "oil":        "Energy",  "power": "Energy",
    "realty":   "Realty",    "real estate":"Realty",
    "consumer": "Consumer",
    "infra":    "Industrials","infrastructure":"Industrials",
    "media":    "Media",
}

_INDEX_KEYWORDS = {
    "nifty 50":       "NIFTY 50",
    "nifty50":        "NIFTY 50",
    "nifty 100":      "NIFTY 100",
    "nifty 200":      "NIFTY 200",
    "nifty 500":      "NIFTY 500",
    "nifty it":       "NIFTY IT",
    "nifty bank":     "NIFTY Bank",
    "nifty pharma":   "NIFTY Pharma",
    "nifty auto":     "NIFTY Auto",
    "nifty fmcg":     "NIFTY FMCG",
    "nifty metal":    "NIFTY Metal",
    "nifty energy":   "NIFTY Energy",
    "nifty realty":   "NIFTY Realty",
    "nifty infra":    "NIFTY Infrastructure",
    "midcap":         "NIFTY Midcap 100",
    "smallcap":       "NIFTY Smallcap 100",
}


def _detect_intent(q: str) -> dict:
    """Return intent dict from user question."""
    ql = q.lower()

    # Specific symbol mention (2-15 uppercase letters after whitespace/start)
    symbols = re.findall(r'\b([A-Z][A-Z0-9&]{1,14})\b', q.upper())
    # Filter out common English words that look like symbols
    ignore = {"AI","ML","NSE","BSE","PE","PB","ROE","RSI","SMA","EPS","IPO","FII","DII","ETF","MF"}
    symbols = [s for s in symbols if s not in ignore]

    # Index match
    for kw, idx in _INDEX_KEYWORDS.items():
        if kw in ql:
            # Don't treat it as pure index query if also asking buy/recommend
            if any(w in ql for w in ["buy","recommend","pick","best","should i","invest"]):
                # Buy recommendations filtered by index — handled as buy with index hint
                return {"intent": "buy_index", "index": idx}
            return {"intent": "index", "index": idx}

    # Sector match
    for kw, sec in _SECTOR_KEYWORDS.items():
        if kw in ql:
            if any(w in ql for w in ["buy","recommend","pick","best","should i","invest"]):
                return {"intent": "buy_sector", "sector": sec}
            return {"intent": "sector", "sector": sec}

    # Market overview
    if any(w in ql for w in ["market","regime","bull","bear","outlook","breadth","breadth","volatility","overview"]):
        return {"intent": "market"}

    # Screener — relative strength / momentum
    if any(w in ql for w in ["relative strength","rs score","momentum","outperform","rs "]):
        return {"intent": "screener_rs"}

    # Screener — 52W
    if re.search(r'52[\s\-]*w|52[\s\-]*week|52week|near high|breakout', ql):
        return {"intent": "screener_52w"}

    # Screener — volume
    if any(w in ql for w in ["volume","spike","surge","unusual","accumulation"]):
        return {"intent": "screener_vol"}

    # Buy / recommend
    if any(w in ql for w in ["buy","recommend","pick","best","should i","invest","top stock","which stock","suggest"]):
        # Sub-intents from the value screener analyst keywords
        if any(w in ql for w in ["debt","debt-free","low debt"]):
            return {"intent": "buy_debt"}
        if any(w in ql for w in ["dividend","yield","passive","income"]):
            return {"intent": "buy_dividend"}
        if any(w in ql for w in ["growth","revenue","earnings"]):
            return {"intent": "buy_growth"}
        if any(w in ql for w in ["small","smallcap","small cap"]):
            return {"intent": "buy_smallcap"}
        if any(w in ql for w in ["mid","midcap","mid cap"]):
            return {"intent": "buy_midcap"}
        if any(w in ql for w in ["large","largecap","blue chip","bluechip","nifty50"]):
            return {"intent": "buy_largecap"}
        if any(w in ql for w in ["uptrend","sma","technical","above 200"]):
            return {"intent": "buy_uptrend"}
        return {"intent": "buy"}

    # Specific stock analysis
    if symbols and any(w in ql for w in ["analyse","analysis","tell me about","what about","how is","should i buy","details","info"]):
        return {"intent": "stock", "symbol": symbols[0]}

    # Symbol alone or with minimal context
    if len(symbols) == 1 and len(ql.split()) <= 5:
        return {"intent": "stock", "symbol": symbols[0]}

    # Risk / avoid
    if any(w in ql for w in ["risk","risky","avoid","red flag","dangerous","caution","warning"]):
        return {"intent": "risk"}

    # Fallback — full report
    return {"intent": "full"}


# ── Intent handlers ────────────────────────────────────────────────────────────

def _handle(intent: dict, q: str) -> str:
    stocks = _load_value_stocks()
    k = intent["intent"]

    if k == "market":
        return _answer_market()

    if k == "stock":
        return _answer_stock(intent["symbol"], stocks)

    if k == "screener_rs":
        return _answer_screener("rs")

    if k == "screener_52w":
        return _answer_screener("52w")

    if k == "screener_vol":
        return _answer_screener("volume")

    if k == "sector":
        return _answer_sector_index(sector=intent.get("sector"))

    if k == "index":
        return _answer_sector_index(index=intent.get("index"))

    if k in ("buy", "buy_sector"):
        return _answer_buy(stocks, sector_filter=intent.get("sector"))

    if k == "buy_index":
        # Get symbols for the index, filter stocks to those
        idx = intent.get("index", "")
        try:
            from fetcher.stock_metadata_fetcher import get_symbols_for_index
            idx_syms = set(get_symbols_for_index(idx))
            filtered = [s for s in stocks if s.get("symbol") in idx_syms]
        except Exception:
            filtered = stocks
        if not filtered:
            filtered = stocks
        return _answer_buy(filtered, limit=8)

    if k == "buy_debt":
        filtered = [s for s in stocks if s.get("debt_equity") is not None and s["debt_equity"] < 0.3]
        filtered.sort(key=lambda s: s.get("value_score", 0), reverse=True)
        regime, _ = _get_regime()
        ml_probs  = _load_ml_probs(regime)
        enriched  = _attach_signals(filtered[:15], ml_probs, regime)
        if not enriched:
            return "No low-debt stocks in the current screener results."
        lines = [f"## Low-Debt / Debt-Free Stocks ({len(enriched)} found)\n"]
        for i, s in enumerate(enriched[:8], 1):
            lines.append(_fmt_stock_row(s, i))
            lines.append("")
        return "\n".join(lines)

    if k == "buy_dividend":
        filtered = [s for s in stocks if s.get("dividend_yield") and s["dividend_yield"] > 1.0]
        filtered.sort(key=lambda s: s.get("dividend_yield", 0), reverse=True)
        if not filtered:
            return "No high-dividend stocks (yield > 1%) in current screener results."
        lines = [f"## High-Dividend Stocks ({len(filtered)} found)\n"]
        for i, s in enumerate(filtered[:8], 1):
            sym = s.get("symbol","?")
            lines.append(f"**{i}. {sym}** ({s.get('sector','')}) — Yield {_fmt(s.get('dividend_yield'),'%')} · Score {s.get('value_score',0)} · P/E {_fmt(s.get('pe_ratio'),'x')}")
        return "\n".join(lines)

    if k == "buy_growth":
        filtered = [s for s in stocks if s.get("revenue_growth") and s["revenue_growth"] > 10]
        filtered.sort(key=lambda s: s.get("revenue_growth", 0), reverse=True)
        if not filtered:
            return "No high-growth stocks (revenue > 10%) in current screener results."
        lines = [f"## High-Growth Undervalued Stocks ({len(filtered)} found)\n"]
        for i, s in enumerate(filtered[:8], 1):
            sym = s.get("symbol","?")
            lines.append(f"**{i}. {sym}** ({s.get('sector','')}) — Rev {_fmt(s.get('revenue_growth'),'%')} · EPS {_fmt(s.get('eps_growth'),'%')} · Score {s.get('value_score',0)}")
        return "\n".join(lines)

    if k == "buy_smallcap":
        filtered = [s for s in stocks if s.get("market_cap_cr") and s["market_cap_cr"] < 5000]
        filtered.sort(key=lambda s: s.get("value_score", 0), reverse=True)
        regime, _ = _get_regime()
        ml_probs  = _load_ml_probs(regime)
        enriched  = _attach_signals(filtered[:15], ml_probs, regime)
        if not enriched:
            return "No small-cap stocks in current screener results."
        lines = [f"## Small-Cap Value Picks ({len(enriched)} found)\n"]
        for i, s in enumerate(enriched[:8], 1):
            lines.append(_fmt_stock_row(s, i))
            lines.append("")
        return "\n".join(lines)

    if k == "buy_midcap":
        filtered = [s for s in stocks if s.get("market_cap_cr") and 5000 <= s["market_cap_cr"] < 20000]
        filtered.sort(key=lambda s: s.get("value_score", 0), reverse=True)
        regime, _ = _get_regime()
        ml_probs  = _load_ml_probs(regime)
        enriched  = _attach_signals(filtered[:15], ml_probs, regime)
        if not enriched:
            return "No mid-cap stocks in current screener results."
        lines = [f"## Mid-Cap Value Picks ({len(enriched)} found)\n"]
        for i, s in enumerate(enriched[:8], 1):
            lines.append(_fmt_stock_row(s, i))
            lines.append("")
        return "\n".join(lines)

    if k == "buy_largecap":
        filtered = [s for s in stocks if s.get("market_cap_cr") and s["market_cap_cr"] >= 20000]
        filtered.sort(key=lambda s: s.get("value_score", 0), reverse=True)
        regime, _ = _get_regime()
        ml_probs  = _load_ml_probs(regime)
        enriched  = _attach_signals(filtered[:15], ml_probs, regime)
        if not enriched:
            return "No large-cap stocks in current screener results."
        lines = [f"## Large-Cap / Blue-Chip Value Picks ({len(enriched)} found)\n"]
        for i, s in enumerate(enriched[:8], 1):
            lines.append(_fmt_stock_row(s, i))
            lines.append("")
        return "\n".join(lines)

    if k == "buy_uptrend":
        filtered = [s for s in stocks if s.get("above_sma200") is True]
        filtered.sort(key=lambda s: s.get("value_score", 0), reverse=True)
        regime, _ = _get_regime()
        ml_probs  = _load_ml_probs(regime)
        enriched  = _attach_signals(filtered[:15], ml_probs, regime)
        if not enriched:
            return "No undervalued stocks in uptrend (above SMA200) in current results."
        lines = [f"## Undervalued in Uptrend ({len(enriched)} found)\n",
                 "*Value + technical strength — the ideal setup.*\n"]
        for i, s in enumerate(enriched[:8], 1):
            lines.append(_fmt_stock_row(s, i))
            lines.append("")
        return "\n".join(lines)

    if k == "risk":
        if not stocks:
            return "No screener data available. Run the Undervalued scan first."
        flags = []
        for s in stocks[:15]:
            sym = s.get("symbol","?")
            de  = s.get("debt_equity")
            roe = s.get("roe")
            rev = s.get("revenue_growth")
            issues = []
            if de is not None and de > 1.5:  issues.append(f"high debt D/E {_fmt(de,'x',2)}")
            if roe is not None and roe < 0:   issues.append(f"loss-making ROE {_fmt(roe,'%')}")
            if rev is not None and rev < -5:  issues.append(f"revenue declining {_fmt(rev,'%')}")
            if s.get("above_sma200") is False: issues.append("below SMA200")
            if issues:
                flags.append(f"- **{sym}** ({s.get('grade','')}, score {s.get('value_score',0)}): {', '.join(issues)}")
        if not flags:
            return "## Risk Check\n\nNo major red flags found in the top stocks."
        lines = ["## Stocks with Risk Flags\n"] + flags
        return "\n".join(lines)

    # full report
    if not stocks:
        return (
            "No screener data available.\n\n"
            "Go to **Screener → Undervalued** and tap **Scan** to populate the data, "
            "then come back and ask your question."
        )
    try:
        from ml.ai_analyst import stream_ai_analysis
        # Use existing analyst for full report
        chunks = list(stream_ai_analysis(stocks, user_question=q, top_n=20))
        return "".join(chunks)
    except Exception as e:
        return f"Analysis error: {e}"


# ── Public streaming interface ─────────────────────────────────────────────────

def stream_chat(
    messages: list[dict],
    user_message: str,
) -> Generator[str, None, None]:
    """
    Analyse the user question, call the right data functions, and stream
    the response back as text chunks (~80 chars each).

    No external API. No API key. Fully offline.

    Args:
        messages:     Conversation history (not used for routing, kept for future use)
        user_message: The user's question

    Yields:
        Text chunks to stream to the client.
    """
    q = user_message.strip()
    if not q:
        yield "Please ask me a question about stocks!"
        return

    try:
        intent  = _detect_intent(q)
        text    = _handle(intent, q)
    except Exception as e:
        logger.error(f"Chat error: {e}", exc_info=True)
        text = f"Sorry, I encountered an error: {e}\n\nMake sure the Undervalued scan has been run at least once."

    # Stream in ~80-char chunks for typing effect
    chunk_size = 80
    for i in range(0, len(text), chunk_size):
        yield text[i: i + chunk_size]
