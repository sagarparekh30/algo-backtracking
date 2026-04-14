"""
Rule-based AI analyst for undervalued stocks.

Generates human-readable insights from screened stock data with zero external
API calls, no API key, and no cost. Works fully offline inside Docker.

Analysis covers:
  - Top picks with specific metric reasoning
  - Sector concentration / diversification
  - Technical trend alignment (RSI + SMA200)
  - Red flags and risk notes
  - Natural-language question answering (keyword-based routing)
"""

from __future__ import annotations
import re
from typing import Generator


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fmt(v, suffix="", decimals=1):
    if v is None:
        return "N/A"
    return f"{round(float(v), decimals)}{suffix}"


def _cap_str(mktcap):
    if mktcap is None:
        return "N/A"
    b = mktcap / 1e9
    if b >= 500:
        return f"₹{b/100:.0f}k Cr (Large Cap)"
    if b >= 50:
        return f"₹{b:.0f}B (Mid Cap)"
    return f"₹{b:.1f}B (Small Cap)"


def _trend(above_sma):
    if above_sma is True:
        return "uptrend ↑ (above SMA200)"
    if above_sma is False:
        return "downtrend ↓ (below SMA200)"
    return "trend unknown"


def _rsi_label(rsi):
    if rsi is None:
        return ""
    if rsi < 35:
        return f"RSI {_fmt(rsi)} — oversold, potential reversal"
    if rsi > 65:
        return f"RSI {_fmt(rsi)} — overbought, wait for pullback"
    return f"RSI {_fmt(rsi)} — neutral"


def _pe_comment(pe, sector_pe):
    if pe is None:
        return ""
    if sector_pe and sector_pe > 0:
        discount = (sector_pe - pe) / sector_pe * 100
        if discount > 30:
            return f"P/E {_fmt(pe)}x is **{discount:.0f}% below** sector median {_fmt(sector_pe)}x — deeply discounted"
        if discount > 10:
            return f"P/E {_fmt(pe)}x vs sector median {_fmt(sector_pe)}x — trading at a discount"
        if discount < -20:
            return f"P/E {_fmt(pe)}x is above sector median {_fmt(sector_pe)}x — premium priced"
        return f"P/E {_fmt(pe)}x near sector median {_fmt(sector_pe)}x"
    if pe < 10:
        return f"P/E {_fmt(pe)}x — very cheap on earnings"
    if pe < 20:
        return f"P/E {_fmt(pe)}x — reasonably priced"
    return f"P/E {_fmt(pe)}x — elevated valuation"


def _debt_comment(de):
    if de is None:
        return ""
    if de < 0.2:
        return "virtually debt-free"
    if de < 0.5:
        return f"low leverage (D/E {_fmt(de,'x',2)})"
    if de < 1.0:
        return f"moderate debt (D/E {_fmt(de,'x',2)})"
    return f"high leverage (D/E {_fmt(de,'x',2)}) — watch debt servicing"


def _roe_comment(roe):
    if roe is None:
        return ""
    if roe > 20:
        return f"strong ROE {_fmt(roe,'%')} — excellent capital efficiency"
    if roe > 12:
        return f"decent ROE {_fmt(roe,'%')}"
    if roe > 0:
        return f"weak ROE {_fmt(roe,'%')} — low returns on equity"
    return f"negative ROE {_fmt(roe,'%')} — loss-making"


# ── Core analysis engine ──────────────────────────────────────────────────────

def _analyse_stock(s: dict, rank: int) -> str:
    """Generate a detailed paragraph for a single stock."""
    sym     = s.get("symbol", "?")
    sector  = s.get("sector", "Unknown")
    score   = s.get("value_score", 0)
    grade   = s.get("grade", "")
    pe      = s.get("pe_ratio")
    sector_pe = s.get("sector_pe_median")
    pb      = s.get("pb_ratio")
    roe     = s.get("roe")
    de      = s.get("de_ratio")
    ev      = s.get("ev_ebitda")
    rev_g   = s.get("revenue_growth")
    eps_g   = s.get("eps_growth")
    div     = s.get("dividend_yield")
    rsi     = s.get("rsi")
    above_sma = s.get("above_sma200")
    mktcap  = s.get("market_cap")

    lines = [f"### {rank}. {sym} — {grade} ({score}/100) | {sector}"]
    lines.append(f"*{_cap_str(mktcap)} · {_trend(above_sma)}*\n")

    # Valuation
    val_pts = []
    pe_c = _pe_comment(pe, sector_pe)
    if pe_c:
        val_pts.append(pe_c)
    if pb is not None:
        if pb < 1.0:
            val_pts.append(f"P/B {_fmt(pb)}x — trading below book value (rare)")
        elif pb < 2.0:
            val_pts.append(f"P/B {_fmt(pb)}x — below 2x book")
        else:
            val_pts.append(f"P/B {_fmt(pb)}x")
    if ev is not None:
        if ev < 8:
            val_pts.append(f"EV/EBITDA {_fmt(ev)}x — cheap on enterprise value")
        else:
            val_pts.append(f"EV/EBITDA {_fmt(ev)}x")
    if val_pts:
        lines.append("**Valuation:** " + " · ".join(val_pts))

    # Quality
    qual_pts = []
    roe_c = _roe_comment(roe)
    if roe_c:
        qual_pts.append(roe_c)
    de_c = _debt_comment(de)
    if de_c:
        qual_pts.append(de_c)
    if qual_pts:
        lines.append("**Quality:** " + " · ".join(qual_pts))

    # Growth
    grow_pts = []
    if rev_g is not None:
        if rev_g > 15:
            grow_pts.append(f"strong revenue growth {_fmt(rev_g,'%')}")
        elif rev_g > 0:
            grow_pts.append(f"revenue growth {_fmt(rev_g,'%')}")
        else:
            grow_pts.append(f"revenue declining {_fmt(rev_g,'%')}")
    if eps_g is not None:
        if eps_g > 15:
            grow_pts.append(f"strong EPS growth {_fmt(eps_g,'%')}")
        elif eps_g > 0:
            grow_pts.append(f"EPS growth {_fmt(eps_g,'%')}")
        else:
            grow_pts.append(f"EPS declining {_fmt(eps_g,'%')}")
    if div is not None and div > 0.5:
        grow_pts.append(f"pays {_fmt(div,'%')} dividend yield")
    if grow_pts:
        lines.append("**Growth/Income:** " + " · ".join(grow_pts))

    # Technical
    tech_pts = []
    rsi_c = _rsi_label(rsi)
    if rsi_c:
        tech_pts.append(rsi_c)
    if above_sma is True:
        tech_pts.append("price confirmed above 200-day MA — institutional buying likely")
    elif above_sma is False:
        tech_pts.append("price below 200-day MA — wait for trend reversal before entry")
    if tech_pts:
        lines.append("**Technical:** " + " · ".join(tech_pts))

    # Red flags
    flags = []
    if de is not None and de > 1.5:
        flags.append(f"high debt (D/E {_fmt(de,'x',2)})")
    if roe is not None and roe < 5:
        flags.append(f"very low ROE {_fmt(roe,'%')}")
    if rev_g is not None and rev_g < -10:
        flags.append(f"revenue shrinking {_fmt(rev_g,'%')}")
    if rsi is not None and rsi > 70:
        flags.append(f"overbought RSI {_fmt(rsi)} — short-term pullback risk")
    if pb is not None and pb > 5 and (pe is None or pe > 30):
        flags.append("rich valuation on both P/E and P/B")
    if flags:
        lines.append("**Risks:** " + " · ".join(flags))

    return "\n".join(lines)


def _sector_summary(stocks: list[dict]) -> str:
    """Summarise sector distribution of top stocks."""
    sector_scores: dict[str, list[float]] = {}
    for s in stocks:
        sec = s.get("sector") or "Unknown"
        sector_scores.setdefault(sec, []).append(s.get("value_score", 0))

    rows = []
    for sec, scores in sorted(sector_scores.items(), key=lambda x: -len(x[1])):
        avg = sum(scores) / len(scores)
        rows.append(f"- **{sec}** — {len(scores)} stocks, avg score {avg:.0f}/100")

    lines = ["## Sector Distribution\n"]
    lines += rows

    # Concentration warning
    dominant = max(sector_scores.items(), key=lambda x: len(x[1]))
    if len(dominant[1]) > len(stocks) * 0.4:
        lines.append(
            f"\n> **Note:** {dominant[0]} is heavily represented ({len(dominant[1])} of {len(stocks)} stocks). "
            "Consider sector diversification when building a portfolio."
        )
    return "\n".join(lines)


def _top_picks(stocks: list[dict], n: int = 5) -> str:
    """Detailed analysis of top N stocks."""
    lines = [f"## Top {min(n, len(stocks))} Picks\n"]
    for i, s in enumerate(stocks[:n], 1):
        lines.append(_analyse_stock(s, i))
        lines.append("")
    return "\n".join(lines)


def _risk_stocks(stocks: list[dict]) -> str:
    """Find stocks in top 10 that have notable red flags."""
    flags = []
    for s in stocks[:10]:
        sym  = s.get("symbol", "?")
        de   = s.get("de_ratio")
        roe  = s.get("roe")
        rev_g = s.get("revenue_growth")
        above = s.get("above_sma200")
        issues = []
        if de is not None and de > 1.5:
            issues.append(f"high debt D/E {_fmt(de,'x',2)}")
        if roe is not None and roe < 0:
            issues.append(f"loss-making ROE {_fmt(roe,'%')}")
        if rev_g is not None and rev_g < -5:
            issues.append(f"revenue declining {_fmt(rev_g,'%')}")
        if above is False:
            issues.append("below SMA200")
        if issues:
            flags.append(f"- **{sym}** ({s.get('grade','')}, score {s.get('value_score',0)}): {', '.join(issues)}")

    if not flags:
        return "## Risk Check\n\nNo major red flags in the top 10 stocks."
    lines = ["## Stocks to Watch Carefully\n"]
    lines += flags
    return "\n".join(lines)


def _full_report(stocks: list[dict]) -> str:
    """Generate a complete analysis report."""
    if not stocks:
        return "No screened stocks available. Please run the undervalued scan first (tap **Scan**)."

    deep  = [s for s in stocks if s.get("grade") == "Deep Value"]
    good  = [s for s in stocks if s.get("grade") == "Good Value"]
    total = len(stocks)

    header = (
        f"## APEX Value Screen — Analysis Report\n\n"
        f"Analysed **{total} stocks** · "
        f"**{len(deep)} Deep Value** · **{len(good)} Good Value**\n\n"
        f"*Scores based on: P/E vs sector median, P/B, EV/EBITDA, ROE, D/E, "
        f"revenue & EPS growth, dividend yield, RSI-14, SMA-200.*\n"
    )

    disclaimer = (
        "\n---\n"
        "> **Disclaimer:** This is algorithmic analysis based on publicly available "
        "fundamental and technical data. It is not financial advice. Always do your own "
        "due diligence before investing. Past performance does not guarantee future results."
    )

    return "\n\n".join([
        header,
        _top_picks(stocks, n=5),
        _sector_summary(stocks),
        _risk_stocks(stocks),
        disclaimer,
    ])


# ── Question answering ────────────────────────────────────────────────────────

def _answer_question(stocks: list[dict], question: str) -> str:
    """Keyword-based routing for natural-language questions."""
    q = question.lower()

    # Best picks / recommendations
    if any(w in q for w in ["best", "top", "recommend", "buy", "pick", "invest"]):
        n = 3
        m = re.search(r'\b(\d+)\b', q)
        if m:
            n = min(int(m.group(1)), 10)
        return _top_picks(stocks, n=n)

    # Sector questions
    if any(w in q for w in ["sector", "industry", "banking", "it", "pharma", "finance",
                              "auto", "fmcg", "energy", "metal", "infra"]):
        # Check if asking about a specific sector
        for keyword, sector_substr in [
            ("bank", "Bank"), ("it", "IT"), ("pharma", "Pharma"),
            ("finance", "Finance"), ("auto", "Auto"), ("fmcg", "FMCG"),
            ("energy", "Energy"), ("metal", "Metal"), ("infra", "Infra"),
            ("consumer", "Consumer"), ("cement", "Cement"),
        ]:
            if keyword in q:
                matched = [s for s in stocks if sector_substr.lower() in (s.get("sector") or "").lower()]
                if matched:
                    lines = [f"## {sector_substr} Sector Stocks\n"]
                    for i, s in enumerate(matched[:5], 1):
                        lines.append(_analyse_stock(s, i))
                        lines.append("")
                    return "\n".join(lines)
        return _sector_summary(stocks)

    # Debt / leverage
    if any(w in q for w in ["debt", "leverage", "debt-free", "balance sheet", "d/e"]):
        low_debt = [s for s in stocks if s.get("de_ratio") is not None and s["de_ratio"] < 0.3]
        low_debt.sort(key=lambda s: s.get("value_score", 0), reverse=True)
        if not low_debt:
            return "No debt-free or very low debt stocks found in the current screened results."
        lines = [f"## Low-Debt / Debt-Free Stocks ({len(low_debt)} found)\n"]
        for i, s in enumerate(low_debt[:5], 1):
            lines.append(
                f"**{i}. {s['symbol']}** ({s.get('sector','')}, score {s.get('value_score',0)}) — "
                f"D/E {_fmt(s.get('de_ratio'),'x',2)} · ROE {_fmt(s.get('roe'),'%')} · "
                f"P/E {_fmt(s.get('pe_ratio'),'x')}"
            )
        return "\n".join(lines)

    # Dividend
    if any(w in q for w in ["dividend", "income", "yield", "passive"]):
        div_stocks = [s for s in stocks if s.get("dividend_yield") and s["dividend_yield"] > 1.0]
        div_stocks.sort(key=lambda s: s.get("dividend_yield", 0), reverse=True)
        if not div_stocks:
            return "No high-dividend stocks found with yield > 1% in the current screened results."
        lines = [f"## High-Dividend Stocks ({len(div_stocks)} found)\n"]
        for i, s in enumerate(div_stocks[:8], 1):
            lines.append(
                f"**{i}. {s['symbol']}** ({s.get('sector','')}) — "
                f"Yield {_fmt(s.get('dividend_yield'),'%')} · Score {s.get('value_score',0)} · "
                f"P/E {_fmt(s.get('pe_ratio'),'x')}"
            )
        return "\n".join(lines)

    # Smallcap / midcap / largecap
    if any(w in q for w in ["small", "smallcap", "small cap"]):
        filtered = [s for s in stocks if s.get("market_cap") and s["market_cap"] < 50e9]
        filtered.sort(key=lambda s: s.get("value_score", 0), reverse=True)
        if not filtered:
            return "No small-cap stocks found in current results."
        lines = ["## Small-Cap Value Stocks\n"]
        for i, s in enumerate(filtered[:5], 1):
            lines.append(f"**{i}. {s['symbol']}** — Score {s.get('value_score',0)} · {_cap_str(s.get('market_cap'))}")
        return "\n".join(lines)

    if any(w in q for w in ["mid", "midcap", "mid cap"]):
        filtered = [s for s in stocks if s.get("market_cap") and 50e9 <= s["market_cap"] < 500e9]
        filtered.sort(key=lambda s: s.get("value_score", 0), reverse=True)
        if not filtered:
            return "No mid-cap stocks found in current results."
        lines = ["## Mid-Cap Value Stocks\n"]
        for i, s in enumerate(filtered[:5], 1):
            lines.append(f"**{i}. {s['symbol']}** — Score {s.get('value_score',0)} · {_cap_str(s.get('market_cap'))}")
        return "\n".join(lines)

    if any(w in q for w in ["large", "largecap", "large cap", "bluechip", "blue chip"]):
        filtered = [s for s in stocks if s.get("market_cap") and s["market_cap"] >= 500e9]
        filtered.sort(key=lambda s: s.get("value_score", 0), reverse=True)
        if not filtered:
            return "No large-cap stocks found in current results."
        lines = ["## Large-Cap Value Stocks\n"]
        for i, s in enumerate(filtered[:5], 1):
            lines.append(f"**{i}. {s['symbol']}** — Score {s.get('value_score',0)} · {_cap_str(s.get('market_cap'))}")
        return "\n".join(lines)

    # Risk / dangerous
    if any(w in q for w in ["risk", "risky", "avoid", "red flag", "dangerous", "caution", "warning"]):
        return _risk_stocks(stocks)

    # Technical / uptrend / downtrend
    if any(w in q for w in ["uptrend", "sma", "technical", "rsi", "momentum", "breakout"]):
        uptrend = [s for s in stocks if s.get("above_sma200") is True]
        uptrend.sort(key=lambda s: s.get("value_score", 0), reverse=True)
        if not uptrend:
            return "No stocks in the list are currently above their 200-day SMA."
        lines = [f"## Undervalued Stocks in Uptrend ({len(uptrend)})\n",
                 "*These combine value + technical strength — the ideal setup.*\n"]
        for i, s in enumerate(uptrend[:8], 1):
            lines.append(
                f"**{i}. {s['symbol']}** — Score {s.get('value_score',0)} · "
                f"RSI {_fmt(s.get('rsi'))} · P/E {_fmt(s.get('pe_ratio'),'x')}"
            )
        return "\n".join(lines)

    # Growth
    if any(w in q for w in ["growth", "revenue", "earnings", "eps"]):
        growth = [s for s in stocks if s.get("revenue_growth") and s["revenue_growth"] > 10]
        growth.sort(key=lambda s: s.get("revenue_growth", 0), reverse=True)
        if not growth:
            return "No high-growth stocks (revenue > 10%) found in current results."
        lines = ["## High-Growth Undervalued Stocks\n"]
        for i, s in enumerate(growth[:5], 1):
            lines.append(
                f"**{i}. {s['symbol']}** ({s.get('sector','')}) — "
                f"Rev growth {_fmt(s.get('revenue_growth'),'%')} · "
                f"EPS growth {_fmt(s.get('eps_growth'),'%')} · Score {s.get('value_score',0)}"
            )
        return "\n".join(lines)

    # Specific symbol lookup
    words = re.findall(r'\b[A-Z]{2,}\b', question.upper())
    for sym in words:
        match = next((s for s in stocks if s.get("symbol") == sym), None)
        if match:
            return _analyse_stock(match, 1)

    # Default — full report
    return _full_report(stocks)


# ── Public interface ──────────────────────────────────────────────────────────

def stream_ai_analysis(
    stocks: list[dict],
    user_question: str = "",
    top_n: int = 20,
) -> Generator[str, None, None]:
    """
    Generate analysis of the top undervalued stocks.

    Yields text in small chunks to simulate streaming in the UI.
    No external API, no API key, zero cost.

    Args:
        stocks:        Full sorted list of screened stocks (value_score desc).
        user_question: Optional natural-language question.
        top_n:         How many top stocks to consider (default 20).
    """
    top = stocks[:top_n]

    if user_question.strip():
        text = _answer_question(top, user_question.strip())
    else:
        text = _full_report(top)

    # Yield in ~80-char chunks to give a streaming feel in the UI
    chunk_size = 80
    for i in range(0, len(text), chunk_size):
        yield text[i : i + chunk_size]
