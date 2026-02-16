import os, json, hashlib, asyncio, logging
from contextlib import asynccontextmanager
from cachetools import TTLCache
from typing import Optional
import anthropic, httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
FRED_API_KEY      = os.getenv("FRED_API_KEY", "")
_cache: TTLCache  = TTLCache(maxsize=200, ttl=86400)
anthropic_client  = None
HTTPX_TIMEOUT     = 10.0


# ── EIA series (energy bilateral trade) ──
EIA_SERIES = {
    ("india russian oil","india russia crude","indian russian crude"):
        ("PET.MTTIM_NUS-NIN_1.M", "US Petroleum Trade — India context (monthly)"),
    ("us lng export","american lng","us liquefied natural gas export"):
        ("NG.N9130US2.M", "US LNG Exports (Bcf/month)"),
    ("us natural gas export","us gas export"):
        ("NG.N9130US2.M", "US Natural Gas Exports (Bcf/month)"),
    ("europe lng","european lng import"):
        ("NG.N9132EU2.M", "US LNG Exports to Europe (Bcf/month)"),
}

# ── Eurostat datasets ──
EUROSTAT_DATASETS = {
    "eu_gas_supply": ("nrg_t_gasgov", {"geo": "EU"}, "EU Natural Gas Supply by Source (TJ)"),
    "eu_gas_russia": ("nrg_t_gasgov", {"geo": "EU", "partner": "RU"}, "EU Gas Imports from Russia (TJ)"),
    "eu_electricity_price": ("nrg_pc_205", {"geo": "DE", "consom": "4161903"}, "Germany Industrial Electricity Price (€/kWh)"),
    "eu_energy_dependency": ("nrg_ind_id", {"geo": "EU"}, "EU Energy Import Dependency (%)"),
    "de_manufacturing": ("sts_inpr_m", {"geo": "DE", "nace_r2": "C"}, "Germany Manufacturing Output Index"),
}

# ── Country codes ──
COUNTRY_CODES = {
    "united states":"US","usa":"US","us":"US","america":"US",
    "china":"CN","prc":"CN","russia":"RU","india":"IN","germany":"DE",
    "france":"FR","united kingdom":"GB","uk":"GB","britain":"GB",
    "japan":"JP","brazil":"BR","canada":"CA","australia":"AU",
    "south korea":"KR","korea":"KR","saudi arabia":"SA","iran":"IR",
    "turkey":"TR","ukraine":"UA","israel":"IL","pakistan":"PK",
    "indonesia":"ID","mexico":"MX","italy":"IT","spain":"ES",
    "netherlands":"NL","poland":"PL","taiwan":"TW","venezuela":"VE",
    "nigeria":"NG","south africa":"ZA","egypt":"EG",
}

# ── FRED series ──
FRED_SERIES = {
    ("gdp growth","economic growth","gdp rate"):         ("A191RL1Q225SBEA","US Real GDP Growth Rate (%)"),
    ("gdp","gross domestic product","economy size"):     ("GDP","US Real GDP ($B, Chained 2017)"),
    ("inflation","cpi","consumer price","price level"):  ("CPIAUCSL","US Consumer Price Index"),
    ("unemployment","jobless","jobs"):                   ("UNRATE","US Unemployment Rate (%)"),
    ("interest rate","federal funds","fed rate"):        ("FEDFUNDS","US Federal Funds Rate (%)"),
    ("trade balance","trade deficit","trade surplus"):   ("BOPGSTB","US Trade Balance ($M)"),
    ("wti","west texas","oil price us"):                 ("DCOILWTICO","WTI Crude Oil Price ($/barrel)"),
    ("brent","brent crude","global oil price"):          ("DCOILBRENTEU","Brent Crude Oil Price ($/barrel)"),
    ("dollar","usd","dollar index"):                     ("DTWEXBGS","US Dollar Index (Broad)"),
    ("national debt","government debt","federal debt"):  ("GFDEBTN","US National Debt ($M)"),
    ("exports","export"):                                ("EXPGS","US Exports of Goods & Services ($B)"),
    ("imports","import"):                                ("IMPGS","US Imports of Goods & Services ($B)"),
    ("oil price","crude price","petroleum price"):       ("DCOILBRENTEU","Brent Crude Oil Price ($/barrel)"),
    ("urals","russian oil","russian crude"):             ("DCOILBRENTEU","Brent Crude Oil Price ($/barrel, proxy for Urals)"),
    ("energy price","commodity price"):                  ("DCOILWTICO","WTI Crude Oil Price ($/barrel)"),
    ("sanctions","russian export","oil revenue"):        ("DCOILBRENTEU","Brent Crude Oil Price ($/barrel)"),
}

# ── World Bank indicators ──
WB_INDICATORS = {
    "gdp_growth":       ("NY.GDP.MKTP.KD.ZG","GDP Growth Rate (%)"),
    "gdp_current_usd":  ("NY.GDP.MKTP.CD","GDP (Current USD)"),
    "inflation_cpi":    ("FP.CPI.TOTL.ZG","Inflation Rate (%)"),
    "unemployment":     ("SL.UEM.TOTL.ZS","Unemployment Rate (%)"),
    "exports_pct_gdp":  ("NE.EXP.GNFS.ZS","Exports (% of GDP)"),
    "imports_pct_gdp":  ("NE.IMP.GNFS.ZS","Imports (% of GDP)"),
    "military_expend":  ("MS.MIL.XPND.GD.ZS","Military Expenditure (% of GDP)"),
    "debt_pct_gdp":     ("GC.DOD.TOTL.GD.ZS","Government Debt (% of GDP)"),
    "population":       ("SP.POP.TOTL","Total Population"),
    "energy_imports":   ("EG.IMP.CONS.ZS","Energy Imports (% of energy use)"),
    "oil_rents":        ("NY.GDP.PETR.RT.ZS","Oil Rents (% of GDP)"),
    "trade_pct_gdp":    ("NE.TRD.GNFS.ZS","Trade (% of GDP)"),
}


# ════════════════════════════════════════
#  DATA SOURCE FUNCTIONS
# ════════════════════════════════════════

async def query_fred(series_id: str, series_label: str) -> dict:
    if not FRED_API_KEY:
        return {"available": False, "error": "FRED API key not configured"}
    try:
        async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as client:
            r = await client.get("https://api.stlouisfed.org/fred/series/observations", params={
                "series_id": series_id, "api_key": FRED_API_KEY,
                "file_type": "json", "sort_order": "desc", "limit": 24,
                "observation_start": "2021-01-01",
            })
            r.raise_for_status()
            obs = [o for o in r.json().get("observations", []) if o.get("value") != "."]
            if not obs:
                return {"available": False, "error": "No data returned"}
            latest = obs[0]
            return {
                "available": True,
                "source": "FRED — Federal Reserve Bank of St. Louis",
                "series_label": series_label,
                "latest_value": latest["value"],
                "latest_date": latest["date"],
                "recent_values": [{"date": o["date"], "value": o["value"]} for o in obs[:3]],
                "url": f"https://fred.stlouisfed.org/series/{series_id}",
            }
    except Exception as e:
        logger.warning(f"FRED query failed: {e}")
        return {"available": False, "error": str(e)}


async def query_worldbank(country_code: str, indicator_key: str) -> dict:
    if indicator_key not in WB_INDICATORS:
        return {"available": False, "error": "Unknown indicator"}
    indicator_id, label = WB_INDICATORS[indicator_key]
    try:
        async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as client:
            r = await client.get(
                f"https://api.worldbank.org/v2/country/{country_code}/indicator/{indicator_id}",
                params={"format": "json", "per_page": 4, "mrv": 4}
            )
            r.raise_for_status()
            data = r.json()
            if len(data) < 2 or not data[1]:
                return {"available": False, "error": "No data"}
            records = [d for d in data[1] if d.get("value") is not None]
            if not records:
                return {"available": False, "error": "No values found"}
            latest = records[0]
            return {
                "available": True,
                "source": "World Bank Open Data",
                "country": latest.get("country", {}).get("value", country_code),
                "indicator": label,
                "latest_value": latest["value"],
                "latest_year": latest["date"],
                "recent_values": [{"year": d["date"], "value": d["value"]} for d in records[:3]],
                "url": f"https://data.worldbank.org/indicator/{indicator_id}?locations={country_code}",
            }
    except Exception as e:
        logger.warning(f"World Bank query failed: {e}")
        return {"available": False, "error": str(e)}


async def query_commodity_price(commodity: str = "oil") -> dict:
    """Try FRED first (confirmed working), fall back to World Bank."""
    # FRED series for commodities
    fred_map = {
        "oil":       ("DCOILWTICO",   "WTI Crude Oil Price ($/barrel)"),
        "oil_brent": ("DCOILBRENTEU", "Brent Crude Oil Price ($/barrel)"),
        "gas":       ("MHHNGSP",      "Natural Gas Price ($/mmbtu)"),
    }
    # World Bank series for commodities
    wb_map = {
        "oil":       ("POILWTIUSDM",  "Crude Oil (WTI), $/barrel"),
        "oil_brent": ("POILBREUSDM",  "Crude Oil (Brent), $/barrel"),
        "gas":       ("PNGASUSDM",    "Natural Gas (US), $/mmbtu"),
        "coal":      ("PCOALAUUSDM",  "Coal (Australia), $/mt"),
        "wheat":     ("PWHEAMTUSDM",  "Wheat (US HRW), $/mt"),
    }

    # Try FRED first if we have a key and a matching series
    if FRED_API_KEY and commodity in fred_map:
        series_id, label = fred_map[commodity]
        result = await query_fred(series_id, label)
        if result.get("available"):
            return result

    # Fall back to World Bank
    indicator_id, label = wb_map.get(commodity, wb_map["oil_brent"])
    try:
        async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as client:
            r = await client.get(
                f"https://api.worldbank.org/v2/country/WLD/indicator/{indicator_id}",
                params={"format": "json", "per_page": 6, "mrv": 6}
            )
            r.raise_for_status()
            data = r.json()
            if len(data) < 2 or not data[1]:
                return {"available": False, "error": "No data"}
            records = [d for d in data[1] if d.get("value") is not None]
            if not records:
                return {"available": False, "error": "No values"}
            latest = records[0]
            return {
                "available": True,
                "source": "World Bank Commodity Price Data (Pink Sheet)",
                "commodity": label,
                "latest_value": latest["value"],
                "latest_date": latest["date"],
                "recent_values": [{"date": d["date"], "value": d["value"]} for d in records[:4]],
                "url": "https://www.worldbank.org/en/research/commodity-markets",
            }
    except Exception as e:
        logger.warning(f"Commodity price query failed: {e}")


async def query_eia(series_id: str, series_label: str) -> dict:
    """EIA — US Energy Information Administration. Free, no key needed for many series."""
    try:
        async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as client:
            r = await client.get(
                f"https://api.eia.gov/v2/seriesid/{series_id}",
                params={"api_key": os.getenv("EIA_API_KEY", ""), "data[0]": "value",
                        "sort[0][column]": "period", "sort[0][direction]": "desc", "length": 12}
            )
            r.raise_for_status()
            data = r.json()
            records = data.get("response", {}).get("data", [])
            if not records:
                return {"available": False, "error": "No EIA data"}
            latest = records[0]
            return {
                "available": True,
                "source": "EIA — US Energy Information Administration",
                "series_label": series_label,
                "latest_value": latest.get("value"),
                "latest_date": latest.get("period"),
                "recent_values": [{"date": r.get("period"), "value": r.get("value")} for r in records[:6]],
                "url": f"https://www.eia.gov/opendata/",
            }
    except Exception as e:
        logger.warning(f"EIA query failed for {series_id}: {e}")
        return {"available": False, "error": str(e)}


async def query_eurostat(dataset: str, params: dict, label: str) -> dict:
    """Eurostat — EU statistical office. Free, no key needed."""
    try:
        base_params = {"format": "JSON", "lang": "EN", "lastTimePeriod": 4}
        base_params.update(params)
        async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as client:
            r = await client.get(
                f"https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{dataset}",
                params=base_params
            )
            r.raise_for_status()
            data = r.json()
            # Extract values from Eurostat JSON-stat format
            values = data.get("value", {})
            time_dims = data.get("dimension", {}).get("time", {}).get("category", {}).get("index", {})
            if not values or not time_dims:
                return {"available": False, "error": "No Eurostat data"}
            # Get the most recent values
            sorted_times = sorted(time_dims.items(), key=lambda x: x[1], reverse=True)
            recent = []
            for time_label, time_idx in sorted_times[:4]:
                val = values.get(str(time_idx))
                if val is not None:
                    recent.append({"date": time_label, "value": val})
            if not recent:
                return {"available": False, "error": "No values found"}
            return {
                "available": True,
                "source": "Eurostat — European Union Statistical Office",
                "series_label": label,
                "latest_value": recent[0]["value"],
                "latest_date": recent[0]["date"],
                "recent_values": recent,
                "url": f"https://ec.europa.eu/eurostat/databrowser/view/{dataset}",
            }
    except Exception as e:
        logger.warning(f"Eurostat query failed for {dataset}: {e}")
        return {"available": False, "error": str(e)}


async def query_oecd(dataset: str, filter_expr: str, label: str) -> dict:
    """OECD Data API. Free, no key needed."""
    try:
        async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as client:
            r = await client.get(
                f"https://stats.oecd.org/SDMX-JSON/data/{dataset}/{filter_expr}/all",
                params={"startTime": "2020", "lastNObservations": 4, "contentType": "json"},
                headers={"Accept": "application/json"}
            )
            r.raise_for_status()
            data = r.json()
            obs = data.get("dataSets", [{}])[0].get("observations", {})
            time_periods = list(data.get("structure", {}).get("dimensions", {})
                               .get("observation", [{}])[0].get("values", []))
            if not obs or not time_periods:
                return {"available": False, "error": "No OECD data"}
            recent = []
            for key, val_list in sorted(obs.items(), key=lambda x: x[0], reverse=True)[:4]:
                idx = int(key.split(":")[0]) if ":" in key else int(key)
                if idx < len(time_periods) and val_list[0] is not None:
                    recent.append({"date": time_periods[idx].get("id",""), "value": val_list[0]})
            if not recent:
                return {"available": False, "error": "No values"}
            return {
                "available": True,
                "source": "OECD Statistics",
                "series_label": label,
                "latest_value": recent[0]["value"],
                "latest_date": recent[0]["date"],
                "recent_values": recent[:3],
                "url": "https://stats.oecd.org/",
            }
    except Exception as e:
        logger.warning(f"OECD query failed: {e}")
        return {"available": False, "error": str(e)}


# ════════════════════════════════════════
#  SMART ROUTING
# ════════════════════════════════════════

def _match_fred(desc: str) -> Optional[tuple]:
    desc_lower = desc.lower()
    for keys, series in FRED_SERIES.items():
        if any(k in desc_lower for k in keys):
            return series
    return None

def _match_country(desc: str) -> Optional[str]:
    desc_lower = desc.lower()
    for name, code in COUNTRY_CODES.items():
        if name in desc_lower:
            return code
    return None

def _match_wb_indicator(desc: str) -> str:
    desc_lower = desc.lower()
    if any(k in desc_lower for k in ("gdp growth","growth rate","economic growth")): return "gdp_growth"
    if any(k in desc_lower for k in ("gdp","economy size","output")): return "gdp_current_usd"
    if any(k in desc_lower for k in ("inflation","price index")): return "inflation_cpi"
    if any(k in desc_lower for k in ("unemployment","employment")): return "unemployment"
    if any(k in desc_lower for k in ("export",)): return "exports_pct_gdp"
    if any(k in desc_lower for k in ("energy import","oil import","crude import")): return "energy_imports"
    if any(k in desc_lower for k in ("import",)): return "imports_pct_gdp"
    if any(k in desc_lower for k in ("oil rent","oil revenue","petro")): return "oil_rents"
    if any(k in desc_lower for k in ("trade",)): return "trade_pct_gdp"
    if any(k in desc_lower for k in ("military","defence","defense")): return "military_expend"
    if any(k in desc_lower for k in ("debt","deficit")): return "debt_pct_gdp"
    return "gdp_growth"

def _match_commodity(desc: str) -> str:
    desc_lower = desc.lower()
    if "brent" in desc_lower: return "oil_brent"
    if any(k in desc_lower for k in ("oil","crude","petroleum","urals","russian oil","energy")): return "oil_brent"
    if any(k in desc_lower for k in ("gas","natural gas","lng")): return "gas"
    if "coal" in desc_lower: return "coal"
    if "wheat" in desc_lower: return "wheat"
    return "oil_brent"

def _infer_source_from_desc(desc: str, domain: str) -> str:
    """Fallback: infer best source from claim description and domain when LLM says 'other'."""
    desc_lower = desc.lower()
    domain_lower = (domain or "").lower()

    # EU/European energy stats → Eurostat (check claim text too)
    eu_keywords = ("european union","eu member","eu gas","eu supply","eu import","eu energy","eu depend",
                   "germany","german","france","french","italy","spain","norway","dutch","netherlands",
                   "europe slashes","europe cut","european commission","eu spent","eu subsid")
    energy_keywords = ("gas","electricity","energy","manufacturing","industrial","lng","supply","depend",
                       "import","subsid","price","cost","billion","trillion")
    if any(k in desc_lower for k in eu_keywords) or any(k in desc_lower for k in ("eu ","europe ")):
        if any(k in desc_lower for k in energy_keywords):
            return "eurostat"
    # Also check if domain is energy and claim mentions Europe
    if domain_lower == "energy" and any(k in desc_lower for k in ("europe","eu","german","norway","russia")):
        return "eurostat"

    # US LNG exports or bilateral energy trade → EIA
    if any(k in desc_lower for k in ("lng export","us lng","american lng","us gas export","us energy export")):
        return "eia"

    # Energy/commodity price claims → commodity prices
    if any(k in desc_lower for k in ("oil","crude","petroleum","urals","brent","wti","coal","energy price","commodity")):
        return "worldbank_commodity"
    if domain_lower == "energy" and not any(k in desc_lower for k in ("european","eu ","germany","german")):
        return "worldbank_commodity"

    # US economic claims → FRED
    if any(k in desc_lower for k in ("us ","united states","american","federal","dollar","gdp","inflation","unemployment","interest rate","trade balance","manufacturing employment")):
        return "fred"

    # Country-level economic claims → World Bank
    if _match_country(desc) and not any(k in desc.lower() for k in ("oil","crude","energy","gas","coal","petroleum","urals")):
        return "worldbank"
    if domain_lower == "economics":
        return "fred"

    return "skip"

async def route_query(validation_query: dict) -> dict:
    source     = validation_query.get("suggested_source", "other")
    desc       = validation_query.get("suggested_parameters", {}).get("description", "")
    claim_id   = validation_query.get("claim_id", "")
    claim_text = validation_query.get("claim_text", "")
    domain     = validation_query.get("domain", "")
    data       = {"available": False, "error": "No suitable source"}

    combined = (desc + " " + claim_text).lower()

    # Hard override 1: European energy/economic claims → Eurostat regardless of LLM suggestion
    eu_terms = ("european union","eu member","eu gas","eu supply","eu import","eu depend","eu energy",
                "eu electricity","eu spent","eu subsid","eu natural gas","eu shrink","eu reduc",
                "germany","german industrial","german manufactur","german electricity",
                "norway gas","europe gas","europe energy","europe electric","europe manufactur",
                "europe slash","europe cut","european commission","russia gas to europe",
                "russian gas depend","lng to europe","europe lng")
    if any(k in combined for k in eu_terms):
        logger.info(f"EU override for claim {claim_id} -> eurostat")
        combined_lower = combined
        if any(k in combined_lower for k in ("electricity","power price","industrial price")):
            ds = EUROSTAT_DATASETS["eu_electricity_price"]
        elif any(k in combined_lower for k in ("manufactur","industrial output","factory","production index")):
            ds = EUROSTAT_DATASETS["de_manufacturing"]
        elif any(k in combined_lower for k in ("depend","import share","import percent","supply share")):
            ds = EUROSTAT_DATASETS["eu_energy_dependency"]
        elif any(k in combined_lower for k in ("russia","russian")):
            ds = EUROSTAT_DATASETS["eu_gas_russia"]
        else:
            ds = EUROSTAT_DATASETS["eu_gas_supply"]
        data = await query_eurostat(ds[0], ds[1], ds[2])
        return {"claim_id": claim_id, "claim_text": claim_text, "data": data}

    # Override: energy domain or oil/crude keywords → commodity prices
    # BUT skip if it's a European/EU claim — those go to Eurostat
    is_european = any(k in combined for k in (
        "european","eu gas","eu import","eu supply","eu depend","eu member","eu spent","eu subsid",
        "eurozone","germany","german","france","french","italy","spain","norway","dutch","netherlands",
        "lng import to europe","europe lng","europe slash","europe cut","european commission",
        "eu electricity","eu energy","eu natural gas"
    ))
    is_energy = domain == "energy" or any(k in combined for k in ("oil","crude","petroleum","urals","brent","wti","energy price","gas price","discount"))
    if is_energy and not is_european:
        logger.info(f"Energy override for claim {claim_id} -> worldbank_commodity")
        data = await query_commodity_price(_match_commodity(combined))
        return {"claim_id": claim_id, "claim_text": claim_text, "data": data}

    # If LLM said 'other', try to infer the right source
    if source == "other" or not source:
        source = _infer_source_from_desc(combined, domain)

    logger.info(f"Routing claim {claim_id} to source: {source} | desc: {desc[:60]}")

    if source == "skip":
        return {"claim_id": claim_id, "claim_text": claim_text, "data": {"available": False, "error": "No suitable source"}}

    try:
        if source == "fred":
            match = _match_fred(desc + " " + claim_text)
            if match:
                data = await query_fred(match[0], match[1])
            else:
                # Fallback: try commodity if FRED match fails and it's energy-related
                if any(k in (desc + claim_text).lower() for k in ("oil","crude","energy")):
                    data = await query_commodity_price("oil_brent")
                else:
                    data = {"available": False, "error": "Could not match to a FRED series"}

        elif source == "worldbank":
            combined = (desc + " " + claim_text).lower()
            # Don't use World Bank country indicators for energy/oil claims
            if any(k in combined for k in ("oil","crude","petroleum","urals","energy","gas","coal","commodity","price")):
                data = await query_commodity_price(_match_commodity(combined))
            else:
                country = _match_country(combined)
                indicator = _match_wb_indicator(combined)
                if country:
                    data = await query_worldbank(country, indicator)
                else:
                    data = {"available": False, "error": "Could not identify country"}

        elif source == "worldbank_commodity":
            commodity = _match_commodity(desc + " " + claim_text)
            data = await query_commodity_price(commodity)

        elif source == "eia":
            combined_lower = combined.lower()
            matched = None
            for keys, series in EIA_SERIES.items():
                if any(k in combined_lower for k in keys):
                    matched = series
                    break
            if matched:
                data = await query_eia(matched[0], matched[1])
            else:
                # Default to LNG exports for energy trade claims
                data = await query_eia("NG.N9130US2.M", "US LNG Exports (Bcf/month)")

        elif source == "eurostat":
            combined_lower = combined.lower()
            if any(k in combined_lower for k in ("electricity price","industrial electricity","power price")):
                ds = EUROSTAT_DATASETS["eu_electricity_price"]
            elif any(k in combined_lower for k in ("manufacturing","industrial output","factory")):
                ds = EUROSTAT_DATASETS["de_manufacturing"]
            elif any(k in combined_lower for k in ("energy depend","energy import")):
                ds = EUROSTAT_DATASETS["eu_energy_dependency"]
            elif any(k in combined_lower for k in ("russia","russian gas")):
                ds = EUROSTAT_DATASETS["eu_gas_russia"]
            else:
                ds = EUROSTAT_DATASETS["eu_gas_supply"]
            data = await query_eurostat(ds[0], ds[1], ds[2])

    except Exception as e:
        logger.error(f"Route query error for {claim_id}: {e}")
        data = {"available": False, "error": str(e)}

    return {"claim_id": claim_id, "claim_text": claim_text, "data": data}


def _format_data(data: dict) -> str:
    if not data or not data.get("available"):
        return "No data available."
    lines = [f"Source: {data.get('source','')}"]
    if "latest_value" in data and "latest_date" in data:
        lines.append(f"Most recent value: {data['latest_value']} (as of {data['latest_date']})")
    if "latest_year" in data:
        lines.append(f"Most recent year: {data['latest_year']}")
    if "indicator" in data:
        lines.append(f"Indicator: {data['indicator']}")
    if "country" in data:
        lines.append(f"Country: {data['country']}")
    if "commodity" in data:
        lines.append(f"Commodity: {data['commodity']}")
    if "recent_values" in data:
        lines.append("Recent trend: " + ", ".join(
            f"{v.get('date') or v.get('year')}: {v.get('value')}" for v in data["recent_values"]
        ))
    return "\n".join(lines)


def _infer_status(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ("contradicts","contradicted","does not support","inconsistent","contrary","refutes")):
        return "contradicted"
    if any(w in t for w in ("partially","mixed","somewhat","nuanced","complex")):
        return "partially_supported"
    if any(w in t for w in ("supports","corroborates","consistent with","confirms","aligns","validates")):
        return "supported"
    return "insufficient_data"




# ── Trusted source lists by tier ──
TIER1_DOMAINS = [
    "fred.stlouisfed.org","worldbank.org","data.worldbank.org",
    "ec.europa.eu/eurostat","eia.gov","iea.org",
    "federalreserve.gov","ecb.europa.eu","bis.org",
    "imf.org","oecd.org","un.org","wto.org","opec.org",
]

TIER2_DOMAINS = [
    "imf.org","iea.org","wto.org","un.org","trade.gov",
    "ustr.gov","treasury.gov","bis.org","ecb.europa.eu",
    "bea.gov","bls.gov","census.gov","europarl.europa.eu",
    "consilium.europa.eu","energy.gov","state.gov",
]

TIER3_DOMAINS = [
    "reuters.com","ft.com","apnews.com","economist.com",
    "bbc.com","bloomberg.com","wsj.com","nytimes.com",
    "theguardian.com","foreignaffairs.com","cfr.org",
    "brookings.edu","chathamhouse.org","piie.com",
]

async def query_web_search(claim_text: str, tier: int, anthropic_client_ref) -> dict:
    """Use Claude's web search tool restricted to trusted domains by tier."""
    if tier == 2:
        domains = TIER2_DOMAINS
        source_type = "Primary Source"
        tier_label = "primary_source"
    else:
        domains = TIER3_DOMAINS
        source_type = "News/Analysis"
        tier_label = "news_report"

    domain_list = ", ".join(domains[:8])  # Top 8 domains for the search

    try:
        search_prompt = f"""Search for evidence to validate or refute this specific claim from a news article:

CLAIM: {claim_text}

Search only trusted sources. Look for:
1. Official statistics or reports that directly address this claim
2. Recent data (2022-2026) that confirms or contradicts the specific figures or facts stated
3. The most authoritative source available

Provide a 2-3 sentence assessment: what did you find, does it support or contradict the claim, and cite the specific source."""

        msg = await anthropic_client_ref.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "allowed_domains": domains
            }],
            messages=[{"role": "user", "content": search_prompt}]
        )

        # Extract text and source from response
        text_content = ""
        source_url = ""
        source_name = ""

        for block in msg.content:
            if hasattr(block, "type"):
                if block.type == "text":
                    text_content = block.text.strip()
                elif block.type == "tool_result":
                    # Extract URL from search results
                    if hasattr(block, "content"):
                        for item in block.content:
                            if hasattr(item, "url"):
                                source_url = item.url
                                # Extract domain as source name
                                from urllib.parse import urlparse
                                source_name = urlparse(source_url).netloc.replace("www.", "")
                                break

        if not text_content:
            return {"available": False, "error": "No search results"}

        return {
            "available": True,
            "source": source_name or source_type,
            "source_tier": tier_label,
            "series_label": f"Web search — {source_type}",
            "summary": text_content,
            "latest_value": None,
            "latest_date": None,
            "recent_values": [],
            "url": source_url,
            "web_search": True,
        }

    except Exception as e:
        logger.warning(f"Web search failed (tier {tier}): {e}")
        return {"available": False, "error": str(e)}


# ════════════════════════════════════════
#  FASTAPI APP
# ════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    global anthropic_client
    if ANTHROPIC_API_KEY:
        anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        logger.info("Anthropic client initialised")
    yield

app = FastAPI(title="ClearView API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class AnalyseRequest(BaseModel):
    article_text: str = Field(..., min_length=200, max_length=15000)
    headline: str = Field("", max_length=300)
    source: str = Field("", max_length=200)
    @validator("article_text")
    def strip_text(cls, v): return v.strip()

@app.get("/api/health")
async def health():
    return {
        "status": "ok", "version": "1.0.0",
        "llm_ready": bool(ANTHROPIC_API_KEY),
        "fred_key": bool(FRED_API_KEY),
        "message": "ClearView API is running" if ANTHROPIC_API_KEY else "WARNING: API key missing"
    }

@app.get("/")
async def root():
    return {"message": "ClearView API", "version": "1.0.0", "health": "/api/health", "docs": "/docs"}

@app.post("/api/analyse")
async def analyse(request: AnalyseRequest):
    if not anthropic_client:
        raise HTTPException(status_code=503, detail="Anthropic API key not configured")

    cache_key = hashlib.sha256(request.article_text.strip().encode()).hexdigest()[:16]
    if cache_key in _cache:
        r = _cache[cache_key].copy(); r["from_cache"] = True; return JSONResponse(r)

    # ── Step 1: LLM Analysis ──
    prompt = f"""Analyse this news article and return ONLY valid JSON — no markdown, no prose.

HEADLINE: {request.headline or '(none)'}
SOURCE: {request.source or '(unknown)'}
TEXT: {request.article_text[:10000]}

Return this exact JSON schema:
{{
  "thesis": "Central conclusion in one sentence",
  "claims": [
    {{
      "id": "C1",
      "text": "claim text",
      "type": "explicit_fact|implicit_assumption|normative|hedged",
      "is_checkable": true,
      "domain": "economics|geopolitics|energy|other"
    }}
  ],
  "argument_map": {{
    "conclusion": "thesis restatement",
    "nodes": [{{"id":"C1","label":"short label","type":"premise|conclusion|assumption"}}],
    "edges": [{{"from":"C1","to":"C2","relation":"supports|contradicts|assumes"}}]
  }},
  "implicit_assumptions": [
    {{"id":"A1","text":"assumption","underlies_claim":"C1","explanation":"why this matters"}}
  ],
  "logical_flags": [
    {{"type":"inferential_gap|correlation_causation|other","description":"plain english description","location":"which claim"}}
  ],
  "validation_queries": [
    {{
      "claim_id": "C1",
      "claim_text": "exact claim text",
      "domain": "economics|geopolitics|energy|other",
      "query_description": "what data would validate this",
      "suggested_source": "fred|worldbank|worldbank_commodity",
      "suggested_parameters": {{
        "description": "specific data needed — name the country, indicator, or commodity explicitly"
      }}
    }}
  ],
  "summary": {{
    "total_claims": 0,
    "explicit_facts": 0,
    "implicit_assumptions": 0,
    "normative_claims": 0,
    "hedged_claims": 0,
    "checkable_claims": 0,
    "logical_flags_count": 0
  }}
}}

RULES:
- Extract 5-12 claims. Identify 2-5 implicit assumptions. Be politically neutral.
- Only generate validation_queries for claims where is_checkable is true.
- For oil price, crude oil, energy commodity, Urals, Brent, WTI claims: ALWAYS use suggested_source = "worldbank_commodity"
- For US economic data (GDP, unemployment, inflation, interest rates, trade, Fed): ALWAYS use suggested_source = "fred"
- For country-level economic data (GDP, trade, energy imports, oil rents for any specific country): ALWAYS use suggested_source = "worldbank"
- For claims about a country importing energy (e.g. India importing Russian oil): use suggested_source = "worldbank" with description = "energy imports percentage for [country]"
- For claims about specific statistics (percentages, dollar amounts, growth rates): ALWAYS generate a validation_query even if the source is indirect
- In suggested_parameters.description: be specific — name the exact country, commodity, or indicator needed.
- For EU/European energy claims (Russian gas dependency, EU gas supply, electricity prices, German manufacturing, EU energy imports): ALWAYS use suggested_source = "eurostat"
- For US LNG export claims or US bilateral energy trade: use suggested_source = "eia"
- IMPORTANT: if the claim mentions EU, European, Germany, German, France, Norway, or any European country in an energy context, use "eurostat" not "fred" or "worldbank_commodity"
- Never use suggested_source = "other" — always pick the closest available source.
- Generate validation_queries for AT LEAST 3 claims per article if possible."""

    try:
        msg = await anthropic_client.messages.create(
            model="claude-sonnet-4-5-20250929", max_tokens=4096,
            system="You are ClearView's analysis engine. Expert in critical thinking and argument analysis. Always respond with valid JSON only. Never use markdown code fences.",
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            raw = raw[4:] if raw.startswith("json") else raw
        analysis = json.loads(raw.strip().rstrip("```").strip())
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Analysis returned malformed output: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # ── Step 2: Parallel Data Validation ──
    validation_queries = analysis.get("validation_queries", [])
    logger.info(f"Running {len(validation_queries)} validation queries")

    tasks = [route_query(q) for q in validation_queries]
    raw_results = []
    if tasks:
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        raw_results = [r for r in raw_results if isinstance(r, dict)]

    # ── Step 3: Synthesise with LLM (parallel) ──
    async def synthesise_one(vr: dict) -> dict:
        data = vr.get("data", {})

        # If structured API failed, try tiered web search
        if not data.get("available"):
            logger.info(f"Structured API failed for {vr['claim_id']}, trying web search tier 2")
            data = await query_web_search(vr["claim_text"], tier=2, anthropic_client_ref=anthropic_client)
            if not data.get("available"):
                logger.info(f"Tier 2 failed for {vr['claim_id']}, trying tier 3")
                data = await query_web_search(vr["claim_text"], tier=3, anthropic_client_ref=anthropic_client)
            if not data.get("available"):
                return {
                    "claim_id": vr["claim_id"],
                    "status": "insufficient_data",
                    "summary": "No suitable data found in available sources.",
                    "source_name": "", "source_url": "",
                }

        # If this was a web search result, it already has a summary — use it directly
        if data.get("web_search") and data.get("summary"):
            summary_text = data["summary"]
            return {
                "claim_id":    vr["claim_id"],
                "status":      _infer_status(summary_text),
                "summary":     summary_text,
                "source_name": data.get("source", ""),
                "source_url":  data.get("url", ""),
                "source_date": "",
                "source_tier": data.get("source_tier", "news_report"),
                "raw_data":    {},
            }
        try:
            synth = await anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=200,
                messages=[{"role": "user", "content": f"""You are a fact-checker validating a specific news claim against authoritative data.

CLAIM: {vr['claim_text']}

DATA SOURCE: {data.get("series_label") or data.get("indicator") or data.get("commodity") or "Economic indicator"}
DATA:
{_format_data(data)}

Write 2-3 sharp, direct sentences. No markdown whatsoever — no asterisks, no bold, no bullet points, no headers.

Start with a plain verdict word: Supports, Partially supports, or Contradicts.
Then give the specific numbers that justify your verdict.
Then note the single most important caveat.

Rules:
- No asterisks or bold formatting of any kind
- The series_label in the data tells you what the numbers represent — always use it. Never confuse a price ($/barrel) with a volume (barrels/day)
- If the claim mentions a specific number, compare it directly to the data value
- If data covers a price series, describe the price trend and what it implies for the claim
- Commit to a verdict — never say only "cannot validate" if the data is clearly relevant
- Keep total length under 65 words"""}]
            )
            summary_text = synth.content[0].text.strip()
            return {
                "claim_id":    vr["claim_id"],
                "status":      _infer_status(summary_text),
                "summary":     summary_text,
                "source_name": data.get("source", ""),
                "source_url":  data.get("url", ""),
                "source_date": data.get("latest_date") or data.get("latest_year") or "",
                "source_tier": "primary_data",
                "raw_data": {k: v for k, v in data.items() if k in [
                    "latest_value","latest_date","latest_year",
                    "indicator","country","commodity","series_label","recent_values"
                ]},
            }
        except Exception as e:
            logger.warning(f"Synthesis failed for {vr['claim_id']}: {e}")
            return {
                "claim_id": vr["claim_id"], "status": "insufficient_data",
                "summary": "Data retrieved but synthesis failed.",
                "source_name": data.get("source",""), "source_url": data.get("url",""),
            }

    synth_results = await asyncio.gather(*[synthesise_one(vr) for vr in raw_results], return_exceptions=True)
    validation_summaries = [r for r in synth_results if isinstance(r, dict)]

    # Mark remaining checkable claims as insufficient
    validated_ids = {v["claim_id"] for v in validation_summaries}
    for q in validation_queries:
        if q["claim_id"] not in validated_ids:
            validation_summaries.append({
                "claim_id": q["claim_id"], "status": "insufficient_data",
                "summary": "No suitable data found in available sources for this claim.",
                "source_name": "", "source_url": "",
            })

    # ── Step 4: Assemble result ──
    result = {
        "status": "success", "from_cache": False, "article_hash": cache_key,
        "thesis":               analysis.get("thesis", ""),
        "claims":               analysis.get("claims", []),
        "argument_map":         analysis.get("argument_map", {}),
        "implicit_assumptions": analysis.get("implicit_assumptions", []),
        "logical_flags":        analysis.get("logical_flags", []),
        "validation_results":   validation_summaries,
        "summary":              analysis.get("summary", {}),
    }
    _cache[cache_key] = result
    return JSONResponse(result)
