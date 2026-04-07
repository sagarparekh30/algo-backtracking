"""
Nifty 100 sector classifications.
Used by the screener for sector heatmap and sector-relative analysis.
"""

# Maps each Nifty 100 symbol to its NSE sector
SECTOR_MAP = {
    # ── Information Technology ──────────────────────────────────
    "TCS":          "IT",
    "INFY":         "IT",
    "WIPRO":        "IT",
    "HCLTECH":      "IT",
    "TECHM":        "IT",
    "LTIM":         "IT",
    "PERSISTENT":   "IT",
    "COFORGE":      "IT",
    "MPHASIS":      "IT",
    "OFSS":         "IT",

    # ── Banking ─────────────────────────────────────────────────
    "HDFCBANK":     "Banking",
    "ICICIBANK":    "Banking",
    "KOTAKBANK":    "Banking",
    "AXISBANK":     "Banking",
    "SBIN":         "Banking",
    "BANKBARODA":   "Banking",
    "PNB":          "Banking",
    "CANBK":        "Banking",
    "INDUSINDBK":   "Banking",
    "FEDERALBNK":   "Banking",
    "IDFCFIRSTB":   "Banking",
    "BANDHANBNK":   "Banking",
    "AUBANK":       "Banking",

    # ── Finance / NBFC ──────────────────────────────────────────
    "BAJFINANCE":   "Finance",
    "BAJAJFINSV":   "Finance",
    "CHOLAFIN":     "Finance",
    "LICHSGFIN":    "Finance",
    "MUTHOOTFIN":   "Finance",
    "SHRIRAMFIN":   "Finance",
    "HDFCAMC":      "Finance",
    "ICICIGI":      "Finance",
    "HDFCLIFE":     "Finance",
    "SBILIFE":      "Finance",
    "SBICARD":      "Finance",

    # ── Energy / Oil & Gas ──────────────────────────────────────
    "RELIANCE":     "Energy",
    "ONGC":         "Energy",
    "IOC":          "Energy",
    "BPCL":         "Energy",
    "GAIL":         "Energy",
    "POWERGRID":    "Energy",
    "NTPC":         "Energy",
    "TATAPOWER":    "Energy",
    "ADANIGREEN":   "Energy",
    "ADANIENSOL":   "Energy",
    "ADANIPOWER":   "Energy",
    "TORNTPOWER":   "Energy",

    # ── FMCG ────────────────────────────────────────────────────
    "HINDUNILVR":   "FMCG",
    "ITC":          "FMCG",
    "NESTLEIND":    "FMCG",
    "BRITANNIA":    "FMCG",
    "DABUR":        "FMCG",
    "MARICO":       "FMCG",
    "GODREJCP":     "FMCG",
    "COLPAL":       "FMCG",
    "EMAMILTD":     "FMCG",
    "TATACONSUM":   "FMCG",
    "VBL":          "FMCG",

    # ── Automobiles ─────────────────────────────────────────────
    "MARUTI":       "Auto",
    "TATAMOTORS":   "Auto",
    "M&M":          "Auto",
    "BAJAJ-AUTO":   "Auto",
    "EICHERMOT":    "Auto",
    "HEROMOTOCO":   "Auto",
    "TVSMOTORS":    "Auto",
    "ASHOKLEY":     "Auto",
    "BALKRISIND":   "Auto",
    "MOTHERSON":    "Auto",
    "BOSCHLTD":     "Auto",
    "BHARATFORG":   "Auto",

    # ── Pharma / Healthcare ─────────────────────────────────────
    "SUNPHARMA":    "Pharma",
    "DRREDDY":      "Pharma",
    "CIPLA":        "Pharma",
    "DIVISLAB":     "Pharma",
    "BIOCON":       "Pharma",
    "AUROPHARMA":   "Pharma",
    "ALKEM":        "Pharma",
    "TORNTPHARM":   "Pharma",
    "LUPIN":        "Pharma",
    "MAXHEALTH":    "Pharma",
    "APOLLOHOSP":   "Pharma",
    "FORTIS":       "Pharma",

    # ── Metals & Mining ─────────────────────────────────────────
    "TATASTEEL":    "Metals",
    "JSWSTEEL":     "Metals",
    "HINDALCO":     "Metals",
    "VEDL":         "Metals",
    "COALINDIA":    "Metals",
    "NMDC":         "Metals",
    "SAIL":         "Metals",
    "NATIONALUM":   "Metals",
    "HINDCOPPER":   "Metals",
    "JSWENERGY":    "Metals",

    # ── Capital Goods / Industrials ─────────────────────────────
    "LT":           "Capital Goods",
    "SIEMENS":      "Capital Goods",
    "ABB":          "Capital Goods",
    "BHEL":         "Capital Goods",
    "CUMMINSIND":   "Capital Goods",
    "THERMAX":      "Capital Goods",
    "HAVELLS":      "Capital Goods",
    "CGPOWER":      "Capital Goods",
    "BEL":          "Capital Goods",
    "HAL":          "Capital Goods",
    "IRFC":         "Capital Goods",
    "RVNL":         "Capital Goods",
    "TIINDIA":      "Capital Goods",

    # ── Consumer Durables ───────────────────────────────────────
    "TITAN":        "Consumer",
    "VOLTAS":       "Consumer",
    "CROMPTON":     "Consumer",
    "KAJARIACER":   "Consumer",
    "WHIRLPOOL":    "Consumer",
    "VGUARD":       "Consumer",
    "DMART":        "Consumer",
    "TRENT":        "Consumer",
    "NYKAA":        "Consumer",
    "ZOMATO":       "Consumer",
    "JUBLFOOD":     "Consumer",

    # ── Cement ──────────────────────────────────────────────────
    "ULTRACEMCO":   "Cement",
    "GRASIM":       "Cement",
    "AMBUJACEM":    "Cement",
    "ACC":          "Cement",
    "SHREECEM":     "Cement",
    "RAMCOCEM":     "Cement",
    "JKCEMENT":     "Cement",

    # ── Telecom ─────────────────────────────────────────────────
    "BHARTIARTL":   "Telecom",
    "IDEA":         "Telecom",

    # ── Real Estate ─────────────────────────────────────────────
    "DLF":          "Real Estate",
    "GODREJPROP":   "Real Estate",
    "OBEROIRLTY":   "Real Estate",
    "PHOENIXLTD":   "Real Estate",
    "PRESTIGE":     "Real Estate",

    # ── Conglomerate / Others ───────────────────────────────────
    "ADANIENT":     "Conglomerate",
    "ADANIPORTS":   "Conglomerate",
    "ATGL":         "Conglomerate",
    "ASIANPAINT":   "Conglomerate",
    "PIDILITIND":   "Conglomerate",
    "BERGEPAINT":   "Conglomerate",
    "PAGEIND":      "Conglomerate",
    "MCDOWELL-N":   "Conglomerate",
    "UBL":          "Conglomerate",
    "UNITDSPR":     "Conglomerate",
    "POLYCAB":      "Conglomerate",
    "DIXON":        "Conglomerate",
}


def get_sector(symbol: str) -> str:
    """Return sector for a symbol, defaulting to 'Others'."""
    return SECTOR_MAP.get(symbol.upper(), "Others")


def get_symbols_by_sector() -> dict:
    """Return {sector: [symbols]} grouped dict."""
    result: dict = {}
    for sym, sec in SECTOR_MAP.items():
        result.setdefault(sec, []).append(sym)
    return result
