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
    ("brent","brent crude","global oil"):                ("DCOILBRENTEU","Brent Crude Oil Price ($/barrel)"),
    ("dollar","usd","dollar index"):                     ("DTWEXBGS","US Dollar Index (Broad)"),
    ("national debt","government debt","federal debt"):  ("GFDEBTN","US National Debt ($M)"),
    ("exports","export"):                                ("EXPGS","US Exports of Goods & Services ($B)"),
    ("imports","import"):                                ("IMPGS","US Imports of Goods & Services ($B)"),
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
                "file_type": "json", "sort_order": "desc", "limit": 4,
                "observation_start": "2020-01-01",
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
        return {"available": False, "error": str(e)}


async def query_commodity_price(commodity: str = "oil") -> dict:
    commodity_map = {
        "oil":       ("POILWTIUSDM", "Crude Oil (WTI), $/barrel"),
        "oil_brent": ("POILBREUSDM", "Crude Oil (Brent), $/barrel"),
        "gas":       ("PNGASUSDM",   "Natural Gas (US), $/mmbtu"),
        "coal":      ("PCOALAUUSDM", "Coal (Australia), $/mt"),
        "wheat":     ("PWHEAMTUSDM", "Wheat (US HRW), $/mt"),
    }
    indicator_id, label = commodity_map.get(commodity, commodity_map["oil"])
    try:
        async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as client:
            r = await client.get(
                f"https://api.worldbank.org/v2/country/WLD/indicator/{indicator_id}",
                params={"format": "json", "per_page": 4, "mrv": 4}
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
                "source": "World Bank Commodity Price Data",
                "commodity": label,
                "latest_value": latest["value"],
                "latest_date": latest["date"],
                "recent_values": [{"date": d["date"], "value": d["value"]} for d in records[:3]],
                "url": "https://www.worldbank.org/en/research/commodity-markets",
            }
    except Exception as e:
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
    if any(k in desc_lower for k in ("inflation","price")): return "inflation_cpi"
    if any(k in desc_lower for k in ("unemployment","employment")): return "unemployment"
    if any(k in desc_lower for k in ("export",)): return "exports_pct_gdp"
    if any(k in desc_lower for k in ("import",)): return "imports_pct_gdp"
    if any(k in desc_lower for k in ("military","defence","defense")): return "military_expend"
    if any(k in desc_lower for k in ("debt","deficit")): return "debt_pct_gdp"
    return "gdp_growth"

def _match_commodity(desc: str) -> str:
    desc_lower = desc.lower()
    if "brent" in desc_lower: return "oil_brent"
    if any(k in desc_lower for k in ("oil","crude","petroleum")): return "oil"
    if any(k in desc_lower for k in ("gas","natural gas")): return "gas"
    if "coal" in desc_lower: return "coal"
    if "wheat" in desc_lower: return "wheat"
    return "oil"

async def route_query(validation_query: dict) -> dict:
    source   = validation_query.get("suggested_source", "other")
    desc     = validation_query.get("suggested_parameters", {}).get("description", "")
    claim_id = validation_query.get("claim_id", "")
    claim_text = validation_query.get("claim_text", "")
    data     = {"available": False, "error": "No suitable source"}

    try:
        if source == "fred":
            match = _match_fred(desc)
            if match:
                data = await query_fred(match[0], match[1])
        elif source == "worldbank":
            country = _match_country(desc)
            indicator = _match_wb_indicator(desc)
            if country:
                data = await query_worldbank(country, indicator)
        elif source == "worldbank_commodity":
            data = await query_commodity_price(_match_commodity(desc))
    except Exception as e:
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
    return {"status":"ok","version":"1.0.0","llm_ready":bool(ANTHROPIC_API_KEY),"fred_key":bool(FRED_API_KEY),"message":"ClearView API is running" if ANTHROPIC_API_KEY else "WARNING: API key missing"}

@app.get("/")
async def root():
    return {"message":"ClearView API","version":"1.0.0","health":"/api/health","docs":"/docs"}

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
  "claims": [{{"id":"C1","text":"claim text","type":"explicit_fact|implicit_assumption|normative|hedged","is_checkable":true,"domain":"economics|geopolitics|energy|other"}}],
  "argument_map": {{"conclusion":"thesis restatement","nodes":[{{"id":"C1","label":"short label","type":"premise|conclusion|assumption"}}],"edges":[{{"from":"C1","to":"C2","relation":"supports|contradicts|assumes"}}]}},
  "implicit_assumptions": [{{"id":"A1","text":"assumption","underlies_claim":"C1","explanation":"why this matters"}}],
  "logical_flags": [{{"type":"inferential_gap|correlation_causation|other","description":"plain english description","location":"which claim"}}],
  "validation_queries": [{{"claim_id":"C1","claim_text":"claim text","query_description":"what to look up","suggested_source":"fred|worldbank|worldbank_commodity|other","suggested_parameters":{{"description":"natural language description of data needed"}}}}],
  "summary": {{"total_claims":0,"explicit_facts":0,"implicit_assumptions":0,"normative_claims":0,"hedged_claims":0,"checkable_claims":0,"logical_flags_count":0}}
}}

Rules: Extract 5-12 claims. Identify 2-5 implicit assumptions. Only generate validation_queries for is_checkable claims. Be politically neutral."""

    try:
        msg = await anthropic_client.messages.create(
            model="claude-sonnet-4-5-20250929", max_tokens=4096,
            system="You are ClearView's analysis engine. Expert in critical thinking and argument analysis. Always respond with valid JSON only.",
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
    tasks = [route_query(q) for q in validation_queries if q.get("suggested_source") not in ("other", None)]
    raw_results = []
    if tasks:
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        raw_results = [r for r in raw_results if isinstance(r, dict)]

    # ── Step 3: Synthesise with LLM ──
    validation_summaries = []
    for vr in raw_results:
        data = vr.get("data", {})
        if not data.get("available"):
            validation_summaries.append({
                "claim_id": vr["claim_id"], "status": "insufficient_data",
                "summary": "No suitable data found in available sources.",
                "source_name": "", "source_url": "",
            })
            continue
        try:
            synth = await anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=200,
                messages=[{"role": "user", "content": f"""You are synthesising data to validate a news claim.

CLAIM: {vr['claim_text']}

DATA:
{_format_data(data)}

Write 2-3 plain English sentences: what does the data show, does it support/partially support/contradict the claim, and any caveats. No jargon. No labels. Just the summary."""}]
            )
            summary_text = synth.content[0].text.strip()
            validation_summaries.append({
                "claim_id":    vr["claim_id"],
                "status":      _infer_status(summary_text),
                "summary":     summary_text,
                "source_name": data.get("source", ""),
                "source_url":  data.get("url", ""),
                "source_date": data.get("latest_date") or data.get("latest_year") or "",
                "raw_data": {k: v for k, v in data.items() if k in ["latest_value","latest_date","latest_year","indicator","country","commodity","series_label","recent_values"]},
            })
        except Exception as e:
            validation_summaries.append({
                "claim_id": vr["claim_id"], "status": "insufficient_data",
                "summary": "Data retrieved but synthesis failed.",
                "source_name": data.get("source",""), "source_url": data.get("url",""),
            })

    # Mark remaining checkable claims with no data
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
