import os, json, hashlib, asyncio, logging
from contextlib import asynccontextmanager
from cachetools import TTLCache
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
FRED_API_KEY = os.getenv("FRED_API_KEY", "")
_cache: TTLCache = TTLCache(maxsize=200, ttl=86400)
anthropic_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global anthropic_client
    if ANTHROPIC_API_KEY:
        anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
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
    return {"status": "ok", "version": "1.0.0", "llm_ready": bool(ANTHROPIC_API_KEY), "fred_key": bool(FRED_API_KEY), "message": "ClearView API is running" if ANTHROPIC_API_KEY else "WARNING: API key missing"}

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
  "summary": {{"total_claims":0,"explicit_facts":0,"implicit_assumptions":0,"normative_claims":0,"hedged_claims":0,"checkable_claims":0,"logical_flags_count":0}}
}}

Rules: Extract 5-12 claims. Identify 2-5 implicit assumptions. Be politically neutral."""

    try:
        msg = await anthropic_client.messages.create(
            model="claude-sonnet-4-5-20250929", max_tokens=4096,
            system="You are ClearView's analysis engine. Expert in critical thinking and argument analysis. Always respond with valid JSON only.",
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"): raw = raw.split("```")[1]; raw = raw[4:] if raw.startswith("json") else raw
        analysis = json.loads(raw.strip().rstrip("```").strip())
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Analysis returned malformed output: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    result = {
        "status": "success", "from_cache": False, "article_hash": cache_key,
        "thesis": analysis.get("thesis", ""),
        "claims": analysis.get("claims", []),
        "argument_map": analysis.get("argument_map", {}),
        "implicit_assumptions": analysis.get("implicit_assumptions", []),
        "logical_flags": analysis.get("logical_flags", []),
        "validation_results": [],
        "summary": analysis.get("summary", {})
    }
    _cache[cache_key] = result
    return JSONResponse(result)
