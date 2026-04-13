"""
NSE Index definitions — symbol lists for all major Nifty indices.

Used by:
  - Data tab backfill selector (pick which index to download)
  - Screener tab (scan a specific index)
  - ML training (optionally train on a broader universe)
"""

# ── Broad Market Indices ─────────────────────────────────────────────────

NIFTY_50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BHARTIARTL", "BPCL",
    "BRITANNIA", "CIPLA", "COALINDIA", "DIVISLAB", "DRREDDY",
    "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK",
    "HDFCLIFE", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK",
    "INFY", "ITC", "JSWSTEEL", "KOTAKBANK", "LT",
    "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
    "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATASTEEL", "TCS",
    "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
]

NIFTY_100 = [
    "ABB", "ADANIENSOL", "ADANIENT", "ADANIGREEN", "ADANIPORTS",
    "ADANIPOWER", "AMBUJACEM", "APOLLOHOSP", "ASIANPAINT", "DMART",
    "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BAJAJHLDNG",
    "BANKBARODA", "BEL", "BPCL", "BHARTIARTL", "BOSCHLTD",
    "BRITANNIA", "CGPOWER", "CANBK", "CHOLAFIN", "CIPLA",
    "COALINDIA", "DLF", "DIVISLAB", "DRREDDY", "EICHERMOT",
    "ETERNAL", "GAIL", "GODREJCP", "GRASIM", "HCLTECH",
    "HDFCBANK", "HDFCLIFE", "HAVELLS", "HINDALCO", "HAL",
    "HINDUNILVR", "HINDZINC", "HYUNDAI", "ICICIBANK", "ICICIGI",
    "ITC", "INDHOTEL", "IOC", "IRFC", "NAUKRI",
    "INFY", "INDIGO", "JSWENERGY", "JSWSTEEL", "JINDALSTEL",
    "JIOFIN", "KOTAKBANK", "LTIM", "LT", "LICI",
    "LODHA", "M&M", "MARUTI", "MAXHEALTH", "MAZDOCK",
    "NTPC", "NESTLEIND", "ONGC", "PIDILITIND", "PFC",
    "POWERGRID", "PNB", "RECLTD", "RELIANCE", "SBILIFE",
    "MOTHERSON", "SHREECEM", "SHRIRAMFIN", "SIEMENS", "SOLARINDS",
    "SBIN", "SUNPHARMA", "TVSMOTOR", "TCS", "TATACONSUM",
    "TATAPOWER", "TATASTEEL", "TECHM", "TITAN", "TORNTPHARM",
    "TRENT", "ULTRACEMCO", "UNITDSPR", "VBL", "VEDL",
    "WIPRO", "ZYDUSLIFE",
]

NIFTY_200 = NIFTY_100 + [
    # Midcap additions to reach ~200
    "AARTIIND", "ABCAPITAL", "ABFRL", "ACC", "APLAPOLLO",
    "ASTRAL", "AUROPHARMA", "BALKRISIND", "BANDHANBNK", "BATAINDIA",
    "BERGEPAINT", "BIOCON", "COLPAL", "CROMPTON", "CUMMINSIND",
    "DABUR", "DEEPAKNTR", "EMAMILTD", "FEDERALBNK", "FLUOROCHEM",
    "FORTIS", "GLENMARK", "GMRAIRPORT", "GODREJPROP",
    "HFCL", "IDFCFIRSTB", "INDIAMART", "INDUSTOWER", "JKCEMENT",
    "JUBLFOOD", "KAJARIACER", "LALPATHLAB", "LICHSGFIN", "LUPIN",
    "MARICO", "METROPOLIS", "MFSL", "MGL", "MUTHOOTFIN",
    "NATIONALUM", "NMDC", "OBEROIRLTY", "OFSS", "PAGEIND",
    "PERSISTENT", "PETRONET", "PHOENIXLTD", "PIIND", "POLYCAB",
    "RAMCOCEM", "SAIL", "SBICARD", "STARHEALTH", "SUNDARMFIN",
    "SUPREMEIND", "TORNTPOWER", "VEDL", "VOLTAS", "WHIRLPOOL",
    "ZOMATO", "ZYDUSLIFE",
]

NIFTY_MIDCAP_100 = [
    "AARTIIND", "ABCAPITAL", "ABFRL", "ACC", "APLAPOLLO",
    "ASTRAL", "AUROPHARMA", "BALKRISIND", "BANDHANBNK", "BATAINDIA",
    "BERGEPAINT", "BIOCON", "COLPAL", "CROMPTON", "CUMMINSIND",
    "DABUR", "DEEPAKNTR", "EMAMILTD", "FEDERALBNK", "FLUOROCHEM",
    "FORTIS", "GLENMARK", "GMRAIRPORT", "GODREJPROP", "GSFC",
    "HFCL", "IDFCFIRSTB", "INDIAMART", "INDUSTOWER", "JKCEMENT",
    "JUBLFOOD", "KAJARIACER", "LALPATHLAB", "LICHSGFIN", "LUPIN",
    "MARICO", "METROPOLIS", "MFSL", "MGL", "MUTHOOTFIN",
    "NATIONALUM", "NMDC", "OBEROIRLTY", "OFSS", "PAGEIND",
    "PERSISTENT", "PETRONET", "PHOENIXLTD", "PIIND", "POLYCAB",
    "RAMCOCEM", "SAIL", "SBICARD", "STARHEALTH", "SUNDARMFIN",
    "SUPREMEIND", "TORNTPOWER", "VOLTAS", "WHIRLPOOL", "ZOMATO",
    "COFORGE", "DIXON", "ELGIEQUIP", "ESCORTS", "EXIDEIND",
    "GNFC", "GRINDWELL", "IIFL", "INDUSINDBK", "INTELLECT",
    "JUBLPHARMA", "KANSAINER", "LINDEINDIA", "LTTS", "MCDOWELL-N",
    "MEDANTA", "NYKAA", "PGHH", "RADICO", "RAJESHEXPO",
    "SUNTV", "TATAELXSI", "THERMAX", "TIINDIA", "TRIDENT",
    "TRITURBINE", "TTKPRESTIG", "UBL", "UNITDSPR", "VAKRANGEE",
    "VBL", "VGUARD", "VINATIORGA", "WELCORP", "ZEEL",
    "3MINDIA", "AAVAS", "ANGELONE", "APTUS", "ARVINDFASN",
]

NIFTY_SMALLCAP_100 = [
    "AARTIDRUGS", "ACRYSIL", "AEGISLOG", "ALOKINDS", "ANANTRAJ",
    "ANGELONE", "APTUS", "ARVINDFASN", "ASAHIINDIA", "ASHIANA",
    "BAJAJHFL", "BALAXI", "BBLIMITED", "BCG", "BEML",
    "BHARAT26", "BIRLACORPN", "BLKASHYAP", "BSOFT", "CAMPUS",
    "CANFINHOME", "CEATLTD", "CENTURYTEX", "CESC", "CHOICEIN",
    "CLEAN", "CMSINFO", "CONCOR", "COSMOFILMS", "DATAMATICS",
    "DCBBANK", "DELTACORP", "DHANI", "ECLERX", "EQUITASBNK",
    "ESABINDIA", "FINCABLES", "FINPIPE", "FIVESTAR", "GABRIEL",
    "GALAXYSURF", "GATEWAY", "GHCL", "GPIL", "GREENPLY",
    "HAPPSTMNDS", "HBLPOWER", "HDFCAMC", "HEMIPROP", "HINDPETRO",
    "IDFC", "IFBIND", "IGPL", "IMAGICAA", "INDIACEM",
    "INDOSTAR", "INOXWIND", "ISGEC", "ITI", "JKLAKSHMI",
    "JMFINANCL", "JPPOWER", "JSWHL", "JTEKTINDIA", "JUNIPERHOTEL",
    "KFINTECH", "KIMS", "KNRCON", "KRBL", "KSCL",
    "LAURUSLABS", "LGBBROSLTD", "LLOYDSME", "LOKESHM", "MAHINDCIE",
    "MANAPPURAM", "MANGCMFT", "MASFIN", "MCX", "MOLDTKPAC",
    "MOTILALOFS", "MSTCLTD", "NATCOPHARM", "NAVINFLUOR", "NBCC",
    "NESCO", "NETWORK18", "NILKAMAL", "NLCINDIA", "NSLNISP",
    "OLECTRA", "ORIENTBELL", "PATELENG", "PDSL", "PENIND",
    "PNBHOUSING", "POWERMECH", "PRISM", "PVRINOX", "RADICO",
]

# ── Sector Indices ───────────────────────────────────────────────────────

NIFTY_BANK = [
    "HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK", "SBIN",
    "INDUSINDBK", "FEDERALBNK", "BANDHANBNK", "IDFCFIRSTB", "AUBANK",
    "PNB", "BANKBARODA",
]

NIFTY_IT = [
    "TCS", "INFY", "HCLTECH", "WIPRO", "TECHM",
    "LTIM", "PERSISTENT", "COFORGE", "MPHASIS", "OFSS",
]

NIFTY_PHARMA = [
    "SUNPHARMA", "DIVISLAB", "CIPLA", "DRREDDY", "AUROPHARMA",
    "TORNTPHARM", "ALKEM", "LUPIN", "BIOCON", "ZYDUSLIFE",
    "GLENMARK", "LALPATHLAB", "METROPOLIS", "NATCOPHARM", "LAURUSLABS",
]

NIFTY_AUTO = [
    "MARUTI", "TATAMOTORS", "M&M", "BAJAJ-AUTO", "EICHERMOT",
    "HEROMOTOCO", "TVSMOTOR", "ASHOKLEY", "BALKRISIND", "MOTHERSON",
    "BOSCHLTD", "BHARATFORG", "ESCORTS", "EXIDEIND", "CEATLTD",
]

NIFTY_FMCG = [
    "HINDUNILVR", "ITC", "NESTLEIND", "BRITANNIA", "DABUR",
    "MARICO", "GODREJCP", "COLPAL", "TATACONSUM", "VBL",
    "EMAMILTD", "RADICO", "MCDOWELL-N", "UNITDSPR", "PGHH",
]

NIFTY_METAL = [
    "TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "COALINDIA",
    "NMDC", "SAIL", "NATIONALUM", "HINDCOPPER", "JINDALSTEL",
    "WELCORP", "APLAPOLLO", "GPIL",
]

NIFTY_ENERGY = [
    "RELIANCE", "ONGC", "BPCL", "IOC", "GAIL",
    "NTPC", "POWERGRID", "TATAPOWER", "ADANIGREEN", "ADANIPOWER",
    "ADANIENSOL", "TORNTPOWER", "JSWENERGY", "PFC", "RECLTD",
]

NIFTY_REALTY = [
    "DLF", "GODREJPROP", "OBEROIRLTY", "PHOENIXLTD", "LODHA",
    "PRESTIGE", "BRIGADE", "SOBHA", "MAHINDCIE", "ANANTRAJ",
]

NIFTY_FINANCIAL_SERVICES = [
    "HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK", "SBIN",
    "BAJFINANCE", "BAJAJFINSV", "SHRIRAMFIN", "CHOLAFIN", "HDFCLIFE",
    "SBILIFE", "ICICIGI", "PFC", "RECLTD", "IRFC",
    "LICHSGFIN", "MUTHOOTFIN", "SBICARD", "JIOFIN", "ABCAPITAL",
]

NIFTY_INFRA = [
    "LT", "NTPC", "POWERGRID", "BHARTIARTL", "ADANIPORTS",
    "GAIL", "IRFC", "BEL", "HAL", "SIEMENS",
    "CONCOR", "GMRAIRPORT", "INDUSTOWER", "NBCC", "RVNL",
]

NIFTY_CONSUMPTION = [
    "HINDUNILVR", "ITC", "TITAN", "TRENT", "DMART",
    "NESTLEIND", "BRITANNIA", "JUBLFOOD", "ZOMATO", "NYKAA",
    "BATAINDIA", "PAGEIND", "TATACONSUM", "VBL", "MARICO",
    "MCDOWELL-N", "RADICO", "INDHOTEL", "BERGEPAINT", "CROMPTON",
]

# ── NIFTY 500 Extra — stocks not already in NIFTY_200/MIDCAP_100/SMALLCAP_100 ──
_NIFTY_500_EXTRA = [
    # IT & Technology
    "KPITTECH", "CYIENT", "TANLA", "MASTEK", "STLTECH",
    "LATENTVIEW", "AFFLE", "NAZARA", "ZENSARTECH", "INTELLECT",
    "CARTRADE", "ROUTE",
    # Pharma & Healthcare
    "ALKEM", "ABBOTINDIA", "AJANTPHARM", "APLLTD", "ERIS",
    "SYNGENE", "GRANULES", "JBCHEPHARM", "SUVENPHAR", "SEQUENT",
    # Banking & Finance
    "RBLBANK", "YESBANK", "CUB", "UJJIVANSFB", "UTIAMC",
    "IIFL", "CREDITACC",
    # Auto & Engineering
    "MRF", "FORCEMOT", "SCHAEFFLER", "GREAVESCOT", "RATNAMANI",
    "CRAFTSMAN", "KAYNES", "LAXMIMACH", "HONAUT", "POWERINDIA",
    # Consumer & Retail
    "SHOPERSTOP", "WESTLIFE", "DEVYANI", "SAPPHIRE", "RELAXO",
    "VAIBHAVGBL", "VENKEYS", "INDIGOPNTS",
    # Metals, Chemicals & Materials
    "HINDCOPPER", "MIDHANI", "WELSPUNIND", "FINEORG",
    "NOCIL", "CARBORUNIV", "NAVINFLUOR", "DALBHARAT",
    # Real Estate
    "PRESTIGE", "BRIGADE", "SOBHA", "SUNTECK", "KOLTEPATIL",
    "GODREJIND",
    # Energy & Oil
    "MRPL", "CHENNPETRO", "RPPOWER", "SJVN", "GESHIP",
    # Logistics & Transport
    "VRL", "REDINGTON", "IRCTC", "RAILTEL", "RVNL", "IRCON",
    # Media & Entertainment
    "TVTODAY",
    # Industrials & Miscellaneous
    "CASTROLIND", "COCHINSHIP", "ENGINERSIN", "ETHOSLTD",
    "JKPAPER", "KPRMILL", "MANINFRA", "POLYMED",
    "PRINCEPIPE", "RAYMOND", "RITES", "TINPLATE",
    "TATAINVEST", "TATATECH", "TVSSCS", "UGROCAP",
    "JUBLINGREA", "CONCORD", "HIRECT",
    # PSU & Defence
    "BEML", "MIDHANI", "COCHINSHIP",
]

# NIFTY 500 = union of all broad-market lists + extra, deduped, ordered
NIFTY_500 = list(dict.fromkeys(
    NIFTY_200 + NIFTY_MIDCAP_100 + NIFTY_SMALLCAP_100 + _NIFTY_500_EXTRA
))

# ── Master registry ──────────────────────────────────────────────────────

INDEX_REGISTRY = {
    # Broad market
    "NIFTY 50":              {"symbols": NIFTY_50,                  "category": "Broad Market", "description": "Top 50 large-cap stocks"},
    "NIFTY 100":             {"symbols": NIFTY_100,                 "category": "Broad Market", "description": "Top 100 large-cap stocks"},
    "NIFTY 200":             {"symbols": NIFTY_200,                 "category": "Broad Market", "description": "Top 200 large & mid-cap stocks"},
    "NIFTY 500":             {"symbols": NIFTY_500,                 "category": "Broad Market", "description": "Top 500 large, mid & small-cap stocks"},
    "NIFTY Midcap 100":      {"symbols": NIFTY_MIDCAP_100,          "category": "Broad Market", "description": "Top 100 mid-cap stocks"},
    "NIFTY Smallcap 100":    {"symbols": NIFTY_SMALLCAP_100,        "category": "Broad Market", "description": "Top 100 small-cap stocks"},
    # Sector
    "NIFTY Bank":            {"symbols": NIFTY_BANK,                "category": "Sector",       "description": "Banking sector — 12 stocks"},
    "NIFTY IT":              {"symbols": NIFTY_IT,                  "category": "Sector",       "description": "Information Technology — 10 stocks"},
    "NIFTY Pharma":          {"symbols": NIFTY_PHARMA,              "category": "Sector",       "description": "Pharmaceutical sector — 15 stocks"},
    "NIFTY Auto":            {"symbols": NIFTY_AUTO,                "category": "Sector",       "description": "Automobile sector — 15 stocks"},
    "NIFTY FMCG":            {"symbols": NIFTY_FMCG,                "category": "Sector",       "description": "Fast-moving consumer goods — 15 stocks"},
    "NIFTY Metal":           {"symbols": NIFTY_METAL,               "category": "Sector",       "description": "Metals & Mining — 13 stocks"},
    "NIFTY Energy":          {"symbols": NIFTY_ENERGY,              "category": "Sector",       "description": "Energy & Power — 15 stocks"},
    "NIFTY Realty":          {"symbols": NIFTY_REALTY,              "category": "Sector",       "description": "Real Estate — 10 stocks"},
    "NIFTY Financial Services": {"symbols": NIFTY_FINANCIAL_SERVICES, "category": "Sector",    "description": "Financial services & NBFCs — 20 stocks"},
    "NIFTY Infrastructure":  {"symbols": NIFTY_INFRA,               "category": "Sector",       "description": "Infrastructure & Capital Goods — 15 stocks"},
    "NIFTY Consumption":     {"symbols": NIFTY_CONSUMPTION,         "category": "Sector",       "description": "Consumer discretionary — 20 stocks"},
}


def get_index_symbols(index_name: str) -> list:
    """Return symbol list for a given index name."""
    entry = INDEX_REGISTRY.get(index_name)
    return entry["symbols"] if entry else []


def list_indices() -> list:
    """Return all indices with metadata (no symbol lists)."""
    return [
        {
            "name":        name,
            "category":    info["category"],
            "description": info["description"],
            "count":       len(info["symbols"]),
        }
        for name, info in INDEX_REGISTRY.items()
    ]
