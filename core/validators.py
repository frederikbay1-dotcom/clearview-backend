"""
ClearView — Data Validation Module
Queries authoritative data sources to validate claims.
Sources: FRED, World Bank, UN Comtrade, World Bank Commodity Prices, REST Countries
"""

import os
import httpx
import logging
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)

FRED_API_KEY = os.getenv("FRED_API_KEY", "")
HTTPX_TIMEOUT = 10.0  # seconds


# ─────────────────────────────────────────────
#  FRED — Federal Reserve Economic Data
# ─────────────────────────────────────────────

# Curated map: common economic topics → FRED series IDs
FRED_SERIES_MAP = {
    "us_gdp":              ("GDP",        "US Real GDP (Billions, Chained 2017 Dollars)"),
    "us_gdp_growth":       ("A191RL1Q225SBEA", "US Real GDP Growth Rate (%)"),
    "us_inflation_cpi":    ("CPIAUCSL",   "US Consumer Price Index (CPI)"),
    "us_inflation_rate":   ("CPILFESL",   "US Core Inflation Rate (%)"),
    "us_unemployment":     ("UNRATE",     "US Unemployment Rate (%)"),
    "us_interest_rate":    ("FEDFUNDS",   "US Federal Funds Rate (%)"),
    "us_trade_balance":    ("BOPGSTB",    "US Trade Balance (Goods & Services, $M)"),
    "us_oil_price_wti":    ("DCOILWTICO", "WTI Crude Oil Price ($/barrel)"),
    "us_oil_price_brent":  ("DCOILBRENTEU","Brent Crude Oil Price ($/barrel)"),
    "us_dollar_index":     ("DTWEXBGS",   "US Dollar Index (Broad)"),
    "us_national_debt":    ("GFDEBTN",    "US National Debt ($M)"),
    "us_money_supply_m2":  ("M2SL",       "US M2 Money Supply ($B)"),
    "global_oil_price":    ("POILBREUSDM","Global Brent Oil Price ($/barrel, monthly)"),
    "us_exports":          ("EXPGS",      "US Exports of Goods & Services ($B)"),
    "us_imports":          ("IMPGS",      "US Imports of Goods & Services ($B)"),
}


async def query_fred(series_id: str, series_label: str, limit: int = 5) -> dict:
    """Fetch most recent observations for a FRED series."""
    if not FRED_API_KEY:
        return {"error": "FRED API key not configured", "available": False}

    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id":    series_id,
        "api_key":      FRED_API_KEY,
        "file_type":    "json",
        "sort_order":   "desc",
        "limit":        limit,
        "observation_start": "2020-01-01",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            obs = [o for o in data.get("observations", []) if o.get("value") != "."]
            if not obs:
                return {"error": "No data returned", "available": False}
            latest = obs[0]
            return {
                "available":    True,
                "source":       "FRED — Federal Reserve Bank of St. Louis",
                "series_id":    series_id,
                "series_label": series_label,
                "latest_value": latest["value"],
                "latest_date":  latest["date"],
                "recent_values": [{"date": o["date"], "value": o["value"]} for o in obs[:3]],
                "url":          f"https://fred.stlouisfed.org/series/{series_id}",
            }
    except Exception as e:
        logger.warning(f"FRED query failed for {series_id}: {e}")
        return {"error": str(e), "available": False}


# ─────────────────────────────────────────────
#  WORLD BANK — Economic Indicators
# ─────────────────────────────────────────────

# Common World Bank indicator codes
WB_INDICATORS = {
    "gdp_current_usd":     ("NY.GDP.MKTP.CD", "GDP (Current USD)"),
    "gdp_growth":          ("NY.GDP.MKTP.KD.ZG", "GDP Growth Rate (%)"),
    "inflation_cpi":       ("FP.CPI.TOTL.ZG", "Inflation Rate, Consumer Prices (%)"),
    "unemployment":        ("SL.UEM.TOTL.ZS", "Unemployment Rate (% of labour force)"),
    "trade_pct_gdp":       ("NE.TRD.GNFS.ZS", "Trade (% of GDP)"),
    "exports_pct_gdp":     ("NE.EXP.GNFS.ZS", "Exports of Goods & Services (% of GDP)"),
    "imports_pct_gdp":     ("NE.IMP.GNFS.ZS", "Imports of Goods & Services (% of GDP)"),
    "current_account_gdp": ("BN.CAB.XOKA.GD.ZS", "Current Account Balance (% of GDP)"),
    "military_expend_gdp": ("MS.MIL.XPND.GD.ZS", "Military Expenditure (% of GDP)"),
    "population":          ("SP.POP.TOTL", "Total Population"),
    "gni_per_capita":      ("NY.GNP.PCAP.CD", "GNI Per Capita (USD)"),
    "debt_pct_gdp":        ("GC.DOD.TOTL.GD.ZS", "Central Government Debt (% of GDP)"),
}

# ISO2 country code map for common countries mentioned in geopolitical articles
COUNTRY_CODES = {
    "united states": "US", "usa": "US", "us": "US", "america": "US",
    "china": "CN", "prc": "CN",
    "russia": "RU",
    "india": "IN",
    "germany": "DE",
    "france": "FR",
    "united kingdom": "GB", "uk": "GB", "britain": "GB",
    "japan": "JP",
    "brazil": "BR",
    "canada": "CA",
    "australia": "AU",
    "south korea": "KR", "korea": "KR",
    "saudi arabia": "SA",
    "iran": "IR",
    "turkey": "TR",
    "ukraine": "UA",
    "israel": "IL",
    "pakistan": "PK",
    "indonesia": "ID",
    "mexico": "MX",
    "italy": "IT",
    "spain": "ES",
    "netherlands": "NL",
    "poland": "PL",
    "taiwan": "TW",
    "north korea": "KP",
    "venezuela": "VE",
    "nigeria": "NG",
    "south africa": "ZA",
    "egypt": "EG",
    "ethiopia": "ET",
    "eurozone": "EMU", "eu": "EMU", "europe": "EMU",
}


async def query_worldbank(country_code: str, indicator_key: str) -> dict:
    """Fetch World Bank indicator data for a country."""
    if indicator_key not in WB_INDICATORS:
        return {"error": f"Unknown indicator: {indicator_key}", "available": False}

    indicator_id, indicator_label = WB_INDICATORS[indicator_key]
    url = f"https://api.worldbank.org/v2/country/{country_code}/indicator/{indicator_id}"
    params = {
        "format":    "json",
        "per_page":  5,
        "mrv":       5,   # most recent values
    }
    try:
        async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            if len(data) < 2 or not data[1]:
                return {"error": "No data returned", "available": False}
            records = [d for d in data[1] if d.get("value") is not None]
            if not records:
                return {"error": "No non-null values found", "available": False}
            latest = records[0]
            return {
                "available":      True,
                "source":         "World Bank Open Data",
                "country":        latest.get("country", {}).get("value", country_code),
                "indicator":      indicator_label,
                "indicator_id":   indicator_id,
                "latest_value":   latest["value"],
                "latest_year":    latest["date"],
                "recent_values":  [{"year": d["date"], "value": d["value"]} for d in records[:3]],
                "url":            f"https://data.worldbank.org/indicator/{indicator_id}?locations={country_code}",
            }
    except Exception as e:
        logger.warning(f"World Bank query failed: {e}")
        return {"error": str(e), "available": False}


# ─────────────────────────────────────────────
#  UN COMTRADE — Trade Flows
# ─────────────────────────────────────────────

UN_COMTRADE_REPORTER_CODES = {
    "US": "842", "CN": "156", "RU": "643", "IN": "356", "DE": "276",
    "FR": "251", "GB": "826", "JP": "392", "BR": "76",  "CA": "124",
    "AU": "36",  "KR": "410", "SA": "682", "IR": "364", "TR": "792",
    "UA": "804", "NG": "566", "ZA": "710", "EG": "818", "NL": "528",
}

UN_COMTRADE_COMMODITY_CODES = {
    "crude_oil":         "2709",
    "natural_gas":       "2711",
    "petroleum_products":"2710",
    "coal":              "2701",
    "iron_steel":        "72",
    "wheat":             "1001",
    "arms_weapons":      "93",
    "semiconductors":    "8542",
    "vehicles":          "87",
    "total_trade":       "TOTAL",
}


async def query_uncomtrade(reporter_iso2: str, partner_iso2: str, commodity_key: str, year: str = "2023") -> dict:
    """Query UN Comtrade for bilateral trade flow data."""
    reporter_code = UN_COMTRADE_REPORTER_CODES.get(reporter_iso2.upper())
    if not reporter_code:
        return {"error": f"Unknown reporter country code: {reporter_iso2}", "available": False}

    commodity_code = UN_COMTRADE_COMMODITY_CODES.get(commodity_key, commodity_key)

    # UN Comtrade v1 public API (no key required for basic queries)
    url = "https://comtradeapi.un.org/public/v1/preview/C/A/HS"
    params = {
        "reporterCode": reporter_code,
        "period":       year,
        "cmdCode":      commodity_code,
        "flowCode":     "M",   # M = imports
        "format":       "JSON",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            records = data.get("data", [])
            if not records:
                return {"error": "No trade data found for these parameters", "available": False}

            # Filter for specific partner if provided
            if partner_iso2 and partner_iso2.upper() != "WLD":
                partner_filtered = [
                    rec for rec in records
                    if str(rec.get("partnerCode", "")) == UN_COMTRADE_REPORTER_CODES.get(partner_iso2.upper(), "")
                ]
                if partner_filtered:
                    records = partner_filtered

            total_value = sum(r.get("primaryValue", 0) for r in records[:5])
            top_records = sorted(records, key=lambda x: x.get("primaryValue", 0), reverse=True)[:3]

            return {
                "available":       True,
                "source":          "UN Comtrade Database",
                "reporter":        reporter_iso2.upper(),
                "partner":         partner_iso2.upper() if partner_iso2 else "World",
                "commodity":       commodity_key,
                "commodity_code":  commodity_code,
                "year":            year,
                "total_value_usd": total_value,
                "top_flows":       [
                    {
                        "partner":    rec.get("partnerDesc", "Unknown"),
                        "value_usd":  rec.get("primaryValue", 0),
                        "flow":       rec.get("flowDesc", "Import"),
                    }
                    for rec in top_records
                ],
                "url": "https://comtradeplus.un.org/",
            }
    except Exception as e:
        logger.warning(f"UN Comtrade query failed: {e}")
        return {"error": str(e), "available": False}


# ─────────────────────────────────────────────
#  WORLD BANK COMMODITY PRICES
# ─────────────────────────────────────────────

async def query_commodity_prices(commodity: str = "oil") -> dict:
    """Fetch commodity price data from World Bank Pink Sheet."""
    # World Bank Commodity Price Data (Pink Sheet) — monthly
    url = "https://api.worldbank.org/v2/en/indicator/PNRGCOAL.CM?downloadformat=json"

    commodity_indicators = {
        "oil":          ("POILWTIUSDM", "Crude Oil (WTI), $/barrel"),
        "oil_brent":    ("POILBREUSDM", "Crude Oil (Brent), $/barrel"),
        "natural_gas":  ("PNGASUSDM",   "Natural Gas (US), $/mmbtu"),
        "coal":         ("PCOALAUUSDM", "Coal (Australia), $/mt"),
        "gold":         ("PGOLDUSDM",   "Gold, $/troy oz"),
        "wheat":        ("PWHEAMTUSDM", "Wheat (US HRW), $/mt"),
    }

    if commodity not in commodity_indicators:
        commodity = "oil"

    indicator_id, label = commodity_indicators[commodity]
    url = f"https://api.worldbank.org/v2/country/WLD/indicator/{indicator_id}"
    params = {"format": "json", "per_page": 6, "mrv": 6}

    try:
        async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            if len(data) < 2 or not data[1]:
                return {"error": "No commodity data", "available": False}
            records = [d for d in data[1] if d.get("value") is not None]
            if not records:
                return {"error": "No non-null values", "available": False}
            latest = records[0]
            return {
                "available":     True,
                "source":        "World Bank Commodity Price Data (Pink Sheet)",
                "commodity":     label,
                "latest_value":  latest["value"],
                "latest_date":   latest["date"],
                "recent_values": [{"date": d["date"], "value": d["value"]} for d in records[:4]],
                "url":           "https://www.worldbank.org/en/research/commodity-markets",
            }
    except Exception as e:
        logger.warning(f"Commodity prices query failed: {e}")
        return {"error": str(e), "available": False}


# ─────────────────────────────────────────────
#  REST COUNTRIES — Geopolitical Facts
# ─────────────────────────────────────────────

async def query_rest_countries(country_name: str) -> dict:
    """Fetch basic geopolitical facts about a country."""
    url = f"https://restcountries.com/v3.1/name/{country_name}"
    params = {"fields": "name,capital,population,region,subregion,borders,area,flags,currencies,languages"}
    try:
        async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            if not data:
                return {"error": "Country not found", "available": False}
            c = data[0]
            return {
                "available":   True,
                "source":      "REST Countries API",
                "name":        c.get("name", {}).get("common", country_name),
                "capital":     c.get("capital", ["Unknown"])[0] if c.get("capital") else "Unknown",
                "population":  c.get("population", 0),
                "region":      c.get("region", "Unknown"),
                "subregion":   c.get("subregion", "Unknown"),
                "borders":     c.get("borders", []),
                "area_km2":    c.get("area", 0),
                "url":         "https://restcountries.com/",
            }
    except Exception as e:
        logger.warning(f"REST Countries query failed for {country_name}: {e}")
        return {"error": str(e), "available": False}


# ─────────────────────────────────────────────
#  SMART QUERY ROUTER
# ─────────────────────────────────────────────

async def route_and_execute_query(validation_query: dict) -> dict:
    """
    Route a validation query to the appropriate data source and execute it.
    Returns a structured result ready for synthesis.
    """
    source      = validation_query.get("suggested_source", "other")
    params_desc = validation_query.get("suggested_parameters", {}).get("description", "")
    claim_id    = validation_query.get("claim_id", "")
    claim_text  = validation_query.get("claim_text", "")

    result = {"claim_id": claim_id, "claim_text": claim_text, "data": None, "source_used": source}

    try:
        if source == "fred":
            # Attempt to match params description to a known FRED series
            matched_series = _match_fred_series(params_desc)
            if matched_series:
                series_id, series_label = matched_series
                result["data"] = await query_fred(series_id, series_label)
            else:
                result["data"] = {"error": "Could not match claim to a known FRED series", "available": False}

        elif source == "worldbank":
            country_code = _extract_country_code(params_desc)
            indicator_key = _match_wb_indicator(params_desc)
            if country_code and indicator_key:
                result["data"] = await query_worldbank(country_code, indicator_key)
            else:
                result["data"] = {"error": f"Could not identify country/indicator from: {params_desc}", "available": False}

        elif source == "uncomtrade":
            reporter, partner, commodity = _parse_trade_query(params_desc)
            if reporter:
                result["data"] = await query_uncomtrade(reporter, partner, commodity)
            else:
                result["data"] = {"error": "Could not parse trade query parameters", "available": False}

        elif source == "worldbank_commodity":
            commodity = _match_commodity(params_desc)
            result["data"] = await query_commodity_prices(commodity)

        elif source == "rest_countries":
            country = _extract_country_name(params_desc)
            if country:
                result["data"] = await query_rest_countries(country)
            else:
                result["data"] = {"error": "Could not identify country", "available": False}

        else:
            result["data"] = {"error": "No suitable data source available for this claim type", "available": False}

    except Exception as e:
        logger.error(f"Query routing error for claim {claim_id}: {e}")
        result["data"] = {"error": str(e), "available": False}

    return result


# ─────────────────────────────────────────────
#  MATCHING HELPERS
# ─────────────────────────────────────────────

def _match_fred_series(description: str) -> Optional[tuple]:
    """Match a natural language description to the best FRED series."""
    desc_lower = description.lower()
    keywords = {
        ("gdp growth", "economic growth", "gdp rate"):               ("A191RL1Q225SBEA", "US Real GDP Growth Rate (%)"),
        ("gdp", "gross domestic product", "economy size"):            ("GDP", "US Real GDP (Billions, Chained 2017 Dollars)"),
        ("inflation", "cpi", "consumer price", "price level"):        ("CPIAUCSL", "US Consumer Price Index"),
        ("unemployment", "jobless", "jobs", "employment rate"):        ("UNRATE", "US Unemployment Rate (%)"),
        ("interest rate", "federal funds", "fed rate", "borrowing"):   ("FEDFUNDS", "US Federal Funds Rate (%)"),
        ("trade balance", "trade deficit", "trade surplus"):           ("BOPGSTB", "US Trade Balance ($M)"),
        ("wti", "west texas", "oil price us"):                         ("DCOILWTICO", "WTI Crude Oil Price ($/barrel)"),
        ("brent", "brent crude", "global oil"):                        ("DCOILBRENTEU", "Brent Crude Oil Price ($/barrel)"),
        ("dollar", "usd", "dollar index", "currency"):                 ("DTWEXBGS", "US Dollar Index (Broad)"),
        ("national debt", "government debt", "federal debt"):           ("GFDEBTN", "US National Debt ($M)"),
        ("money supply", "m2", "monetary"):                            ("M2SL", "US M2 Money Supply ($B)"),
        ("exports", "export"):                                         ("EXPGS", "US Exports of Goods & Services ($B)"),
        ("imports", "import"):                                         ("IMPGS", "US Imports of Goods & Services ($B)"),
    }
    for keys, series in keywords.items():
        if any(k in desc_lower for k in keys):
            return series
    return None


def _extract_country_code(description: str) -> Optional[str]:
    """Extract ISO2 country code from a description string."""
    desc_lower = description.lower()
    for name, code in COUNTRY_CODES.items():
        if name in desc_lower:
            return code
    return None


def _extract_country_name(description: str) -> Optional[str]:
    """Extract country name from description."""
    desc_lower = description.lower()
    for name in COUNTRY_CODES:
        if name in desc_lower:
            return name
    return None


def _match_wb_indicator(description: str) -> Optional[str]:
    """Match description to World Bank indicator key."""
    desc_lower = description.lower()
    mappings = {
        ("gdp growth", "economic growth", "growth rate"):      "gdp_growth",
        ("gdp", "economy size", "output"):                     "gdp_current_usd",
        ("inflation", "price"):                                "inflation_cpi",
        ("unemployment", "employment"):                        "unemployment",
        ("trade", "exports and imports"):                      "trade_pct_gdp",
        ("exports", "export"):                                 "exports_pct_gdp",
        ("imports", "import"):                                 "imports_pct_gdp",
        ("military", "defence", "defense", "arms spending"):   "military_expend_gdp",
        ("debt", "government deficit"):                        "debt_pct_gdp",
        ("population", "people", "demographic"):               "population",
    }
    for keys, indicator in mappings.items():
        if any(k in desc_lower for k in keys):
            return indicator
    return "gdp_growth"   # safe default


def _parse_trade_query(description: str) -> tuple:
    """Parse trade query description into (reporter, partner, commodity)."""
    desc_lower = description.lower()
    reporter = _extract_country_code(description) or "US"
    partner  = "WLD"

    # Look for "from X" or "to X" patterns for partner
    for name, code in COUNTRY_CODES.items():
        if f"from {name}" in desc_lower or f"to {name}" in desc_lower or f"with {name}" in desc_lower:
            partner = code
            break

    commodity = "total_trade"
    commodity_keywords = {
        ("oil", "crude", "petroleum"):     "crude_oil",
        ("gas", "natural gas", "lng"):     "natural_gas",
        ("coal"):                          "coal",
        ("wheat", "grain", "food"):        "wheat",
        ("weapons", "arms", "military"):   "arms_weapons",
        ("semiconductors", "chips"):       "semiconductors",
        ("steel", "iron"):                 "iron_steel",
    }
    for keys, comm in commodity_keywords.items():
        if any(k in desc_lower for k in keys):
            commodity = comm
            break

    return reporter, partner, commodity


def _match_commodity(description: str) -> str:
    """Match description to commodity type."""
    desc_lower = description.lower()
    if any(k in desc_lower for k in ("brent", "global oil")):
        return "oil_brent"
    if any(k in desc_lower for k in ("oil", "crude", "petroleum", "wti")):
        return "oil"
    if any(k in desc_lower for k in ("gas", "natural gas", "lng")):
        return "natural_gas"
    if "coal" in desc_lower:
        return "coal"
    if "gold" in desc_lower:
        return "gold"
    if any(k in desc_lower for k in ("wheat", "grain")):
        return "wheat"
    return "oil"
