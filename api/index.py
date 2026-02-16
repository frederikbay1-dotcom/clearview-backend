import os, json, hashlib, asyncio, logging, re
from contextlib import asynccontextmanager
from cachetools import TTLCache
from typing import Optional
from urllib.parse import urlparse
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
EIA_API_KEY       = os.getenv("EIA_API_KEY", "")
_cache: TTLCache  = TTLCache(maxsize=200, ttl=86400)
anthropic_client  = None
HTTPX_TIMEOUT     = 10.0

# ── Trusted domains by tier ──
TIER2_DOMAINS = [
    "imf.org","iea.org","wto.org","un.org","trade.gov","ustr.gov",
    "treasury.gov","bis.org","ecb.europa.eu","bea.gov","bls.gov",
    "census.gov","europarl.europa.eu","consilium.europa.eu","energy.gov",
    "federalreserve.gov","eia.gov","ec.europa.eu","worldbank.org",
    "data.worldbank.org","oecd.org","opec.org","iea.org",
]
TIER3_DOMAINS = [
    "reuters.com","ft.com","apnews.com","economist.com","bbc.com",
    "bloomberg.com","wsj.com","nytimes.com","theguardian.com",
    "foreignaffairs.com","cfr.org","brookings.edu","chathamhouse.org",
]


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
                "recent_values": [{"date": o["date"], "value": o["value"]} for o in obs[:6]],
                "url": f"https://fred.stlouisfed.org/series/{series_id}",
                "source_tier": "primary_data",
            }
    except Exception as e:
        logger.warning(f"FRED query failed for {series_id}: {e}")
        return {"available": False, "error": str(e)}


async def query_worldbank(country_code: str, indicator_id: str, label: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as client:
            r = await client.get(
                f"https://api.worldbank.org/v2/country/{country_code}/indicator/{indicator_id}",
                params={"format": "json", "per_page": 6, "mrv": 6}
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
                "recent_values": [{"year": d["date"], "value": d["value"]} for d in records[:4]],
                "url": f"https://data.worldbank.org/indicator/{indicator_id}?locations={country_code}",
                "source_tier": "primary_data",
            }
    except Exception as e:
        logger.warning(f"World Bank query failed: {e}")
        return {"available": False, "error": str(e)}


async def query_eurostat(dataset: str, params: dict, label: str) -> dict:
    try:
        base_params = {"format": "JSON", "lang": "EN", "lastTimePeriod": 6}
        base_params.update(params)
        async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as client:
            r = await client.get(
                f"https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{dataset}",
                params=base_params
            )
            r.raise_for_status()
            data = r.json()
            values = data.get("value", {})
            time_dims = data.get("dimension", {}).get("time", {}).get("category", {}).get("index", {})
            if not values or not time_dims:
                return {"available": False, "error": "No Eurostat data"}
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
                "source_tier": "primary_data",
            }
    except Exception as e:
        logger.warning(f"Eurostat query failed for {dataset}: {e}")
        return {"available": False, "error": str(e)}


async def query_web_search(claim_text: str, domains: list, tier_label: str, client_ref) -> dict:
    try:
        msg = await client_ref.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "allowed_domains": domains
            }],
            messages=[{"role": "user", "content": f"""Find evidence to validate or refute this news claim.

CLAIM: {claim_text}

Write 2-3 sentences. No markdown, no asterisks, no headers.
Start with: Supports / Partially supports / Contradicts
Then give specific figures or facts found.
Then note the most important caveat. Under 80 words total."""}]
        )
        text_content = ""
        source_url = ""
        source_name = ""
        for block in msg.content:
            if not hasattr(block, "type"):
                continue
            if block.type == "text":
                raw = block.text.strip().lstrip(",-. ")
                raw = re.sub(r'\*\*([^*]+)\*\*:?\s*', r'\1 ', raw)
                raw = re.sub(r'\*([^*]+)\*', r'\1', raw)
                raw = re.sub(r'\*', '', raw)
                text_content = raw.strip()
            elif hasattr(block, "content") and isinstance(block.content, list):
                for item in block.content:
                    if hasattr(item, "url") and item.url and not source_url:
                        source_url = item.url
                        source_name = urlparse(source_url).netloc.replace("www.", "")
        if not text_content:
            return {"available": False, "error": "No search results"}
        if not source_url:
            urls = re.findall(r'https?://[^\s>"\')\]]+', text_content)
            if urls:
                source_url = urls[0]
                source_name = urlparse(source_url).netloc.replace("www.", "")
        return {
            "available": True,
            "source": source_name or tier_label,
            "series_label": f"Web search",
            "summary": text_content,
            "latest_value": None,
            "latest_date": None,
            "recent_values": [],
            "url": source_url,
            "web_search": True,
            "source_tier": tier_label,
        }
    except Exception as e:
        logger.warning(f"Web search failed: {e}")
        return {"available": False, "error": str(e)}


def _format_data(data: dict) -> str:
    if not data or not data.get("available"):
        return "No data available."
    lines = [f"Source: {data.get('source','')}"]
    if data.get("series_label"):
        lines.append(f"Series: {data['series_label']}")
    if "latest_value" in data and data["latest_value"] is not None:
        lines.append(f"Latest value: {data['latest_value']} (as of {data.get('latest_date') or data.get('latest_year','')})")
    if "indicator" in data:
        lines.append(f"Indicator: {data['indicator']}")
    if "country" in data:
        lines.append(f"Country: {data['country']}")
    if "recent_values" in data and data["recent_values"]:
        lines.append("Recent trend: " + ", ".join(
            f"{v.get('date') or v.get('year')}: {v.get('value')}" for v in data["recent_values"]
        ))
    return "\n".join(lines)


def _infer_status(text: str) -> str:
    t = text.lower()[:100]
    if any(w in t for w in ("contradicts","contradicted","does not support","inconsistent","contrary","refutes","not support")):
        return "contradicted"
    if any(w in t for w in ("partially supports","partially supported","mixed","somewhat","nuanced","partially accurate")):
        return "partially_supported"
    if any(w in t for w in ("supports","corroborates","consistent with","confirms","aligns","validates")):
        return "supported"
    return "insufficient_data"


# ════════════════════════════════════════
#  LLM-DRIVEN ROUTING
# ════════════════════════════════════════

async def execute_validation_query(vq: dict, client_ref) -> dict:
    """Execute a validation query as specified by the LLM."""
    claim_id   = vq.get("claim_id", "")
    claim_text = vq.get("claim_text", "")
    method     = vq.get("method", "web_search")
    params     = vq.get("params", {})
    data       = {"available": False, "error": "No method matched"}

    try:
        if method == "fred":
            series_id    = params.get("series_id", "")
            series_label = params.get("series_label", series_id)
            if series_id:
                data = await query_fred(series_id, series_label)

        elif method == "worldbank":
            country   = params.get("country_code", "")
            indicator = params.get("indicator_id", "")
            label     = params.get("label", indicator)
            if country and indicator:
                data = await query_worldbank(country, indicator, label)

        elif method == "eurostat":
            dataset    = params.get("dataset", "")
            geo        = params.get("geo", "EU")
            label      = params.get("label", dataset)
            extra      = {k: v for k, v in params.items() if k not in ("dataset","geo","label")}
            base_filter = {"geo": geo}
            base_filter.update(extra)
            if dataset:
                data = await query_eurostat(dataset, base_filter, label)

        elif method == "web_search_primary":
            data = await query_web_search(claim_text, TIER2_DOMAINS, "primary_source", client_ref)

        elif method == "web_search_news":
            data = await query_web_search(claim_text, TIER3_DOMAINS, "news_report", client_ref)

    except Exception as e:
        logger.error(f"Validation query error for {claim_id}: {e}")
        data = {"available": False, "error": str(e)}

    # Fallback chain: if method failed, try web search
    if not data.get("available") and method not in ("web_search_primary","web_search_news"):
        logger.info(f"Structured source failed for {claim_id}, falling back to web search")
        data = await query_web_search(claim_text, TIER2_DOMAINS, "primary_source", client_ref)
        if not data.get("available"):
            data = await query_web_search(claim_text, TIER3_DOMAINS, "news_report", client_ref)

    return {"claim_id": claim_id, "claim_text": claim_text, "data": data}


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
    force_refresh: bool = Field(False)
    @validator("article_text")
    def strip_text(cls, v): return v.strip()

@app.get("/api/health")
async def health():
    return {
        "status": "ok", "version": "1.0.0",
        "llm_ready": bool(ANTHROPIC_API_KEY),
        "fred_key": bool(FRED_API_KEY),
        "message": "ClearView API is running"
    }

@app.get("/")
async def root():
    return {"message": "ClearView API", "version": "1.0.0", "health": "/api/health", "docs": "/docs"}

@app.post("/api/analyse")
async def analyse(request: AnalyseRequest):
    if not anthropic_client:
        raise HTTPException(status_code=503, detail="Anthropic API key not configured")

    cache_key = hashlib.sha256(request.article_text.strip().encode()).hexdigest()[:16]
    if cache_key in _cache and not request.force_refresh:
        r = _cache[cache_key].copy(); r["from_cache"] = True; return JSONResponse(r)

    # ── Step 1: LLM Analysis + Routing Instructions ──
    prompt = f"""Analyse this news article. Return ONLY valid JSON, no markdown.

HEADLINE: {request.headline or '(none)'}
SOURCE: {request.source or '(unknown)'}
TEXT: {request.article_text[:10000]}

Return this exact schema:
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
    {{"id":"A1","text":"assumption text","underlies_claim":"C1","explanation":"why this matters"}}
  ],
  "logical_flags": [
    {{"type":"inferential_gap|correlation_causation|other","description":"plain english","location":"claim id"}}
  ],
  "validation_queries": [
    {{
      "claim_id": "C1",
      "claim_text": "exact claim text",
      "method": "fred|worldbank|eurostat|web_search_primary|web_search_news",
      "params": {{}}
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

VALIDATION QUERY RULES — choose the best method for each checkable claim:

method = "fred": For US macroeconomic data. params must include:
  "series_id": exact FRED series ID (e.g. "UNRATE","CPIAUCSL","FEDFUNDS","GDP","DCOILBRENTEU","DCOILWTICO","BOPGSTB","A191RL1Q225SBEA")
  "series_label": human readable name

method = "worldbank": For country-level economic indicators. params must include:
  "country_code": ISO2 code (e.g. "CN","IN","DE","RU","US")
  "indicator_id": World Bank indicator (e.g. "NY.GDP.MKTP.KD.ZG","FP.CPI.TOTL.ZG","SL.UEM.TOTL.ZS","NE.EXP.GNFS.ZS","EG.IMP.CONS.ZS")
  "label": human readable name

method = "eurostat": For EU/European statistical data. params must include:
  "dataset": Eurostat dataset code (e.g. "nrg_t_gasgov","sts_inpr_m","nrg_pc_205","nrg_ind_id")
  "geo": country/region code (e.g. "EU","DE","FR")
  "label": human readable name

method = "web_search_primary": For any claim needing official reports, bilateral trade data, policy documents, sanctions data, or specific statistics not in the above. Use this for geopolitical claims, bilateral trade, energy dependency percentages, spending figures.

method = "web_search_news": Only for qualitative claims about events, statements, or developments where no statistical data exists.

IMPORTANT:
- Generate validation_queries for ALL is_checkable claims (aim for 4-6 queries)
- Prefer fred/worldbank/eurostat over web search when a matching series exists
- For European energy/gas/electricity/manufacturing claims: always use eurostat
- For US macro claims: always use fred with the exact series ID
- For bilateral trade, sanctions, geopolitical facts: use web_search_primary
- Extract 5-12 claims. Be politically neutral."""

    try:
        msg = await anthropic_client.messages.create(
            model="claude-sonnet-4-5-20250929", max_tokens=4096,
            system="You are ClearView's analysis engine. Expert in critical thinking, argument analysis, and knowing which statistical databases contain which data. Always respond with valid JSON only. Never use markdown code fences.",
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

    # ── Step 2: Execute Validation Queries in Parallel ──
    validation_queries = analysis.get("validation_queries", [])
    logger.info(f"Executing {len(validation_queries)} LLM-specified validation queries")

    tasks = [execute_validation_query(q, anthropic_client) for q in validation_queries]
    raw_results = []
    if tasks:
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        raw_results = [r for r in gathered if isinstance(r, dict)]

    # ── Step 3: Synthesise Results in Parallel ──
    async def synthesise_one(vr: dict) -> dict:
        data = vr.get("data", {})
        if not data.get("available"):
            return {
                "claim_id": vr["claim_id"],
                "status": "insufficient_data",
                "summary": "No suitable data found in available sources.",
                "source_name": "", "source_url": "", "source_tier": "",
            }

        # Web search results already have a summary
        if data.get("web_search") and data.get("summary"):
            return {
                "claim_id":    vr["claim_id"],
                "status":      _infer_status(data["summary"]),
                "summary":     data["summary"],
                "source_name": data.get("source", ""),
                "source_url":  data.get("url", ""),
                "source_tier": data.get("source_tier", "news_report"),
                "raw_data":    {},
            }

        # Structured data — synthesise with LLM
        try:
            synth = await anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=200,
                messages=[{"role": "user", "content": f"""Validate this news claim using authoritative data.

CLAIM: {vr['claim_text']}
DATA SOURCE: {data.get('series_label') or data.get('indicator') or data.get('source','')}
DATA:
{_format_data(data)}

Write 2-3 sentences. No markdown, no asterisks, no headers.
Start with: Supports / Partially supports / Contradicts
Then give the specific numbers from the data.
Then note the most important caveat. Under 70 words."""}]
            )
            summary_text = synth.content[0].text.strip()
            summary_text = re.sub(r'\*\*([^*]+)\*\*:?\s*', r'\1 ', summary_text)
            summary_text = re.sub(r'\*', '', summary_text).strip()
            return {
                "claim_id":    vr["claim_id"],
                "status":      _infer_status(summary_text),
                "summary":     summary_text,
                "source_name": data.get("source", ""),
                "source_url":  data.get("url", ""),
                "source_date": data.get("latest_date") or data.get("latest_year") or "",
                "source_tier": data.get("source_tier", "primary_data"),
                "raw_data": {k: v for k, v in data.items() if k in [
                    "latest_value","latest_date","latest_year",
                    "indicator","country","series_label","recent_values"
                ]},
            }
        except Exception as e:
            logger.warning(f"Synthesis failed for {vr['claim_id']}: {e}")
            return {
                "claim_id": vr["claim_id"], "status": "insufficient_data",
                "summary": "Data retrieved but synthesis failed.",
                "source_name": data.get("source",""), "source_url": data.get("url",""),
                "source_tier": data.get("source_tier",""),
            }

    synth_tasks = await asyncio.gather(*[synthesise_one(vr) for vr in raw_results], return_exceptions=True)
    validation_summaries = [r for r in synth_tasks if isinstance(r, dict)]

    # ── Step 4: Assemble ──
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
