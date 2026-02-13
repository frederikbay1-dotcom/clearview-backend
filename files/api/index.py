"""
ClearView Backend API
FastAPI application — entry point for Vercel deployment.

Endpoints:
  POST /api/analyse   — Full article analysis pipeline
  GET  /api/health    — Health check + API status
"""

import os
import logging
from contextlib import asynccontextmanager

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator

from core.pipeline import run_analysis

# ── Environment ──
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ENVIRONMENT       = os.getenv("ENVIRONMENT", "production")

# ── Anthropic client (shared across requests) ──
anthropic_client: anthropic.AsyncAnthropic | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global anthropic_client
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY is not set — analysis will fail")
    else:
        anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        logger.info("Anthropic client initialised")
    yield
    logger.info("Shutting down")


# ── App ──
app = FastAPI(
    title="ClearView API",
    description="News intelligence backend — claim extraction, argument mapping, and data validation",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS: allow Lovable frontend and local development ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://*.lovable.app",
        "https://*.lovableproject.com",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8080",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / Response Models ──

class AnalyseRequest(BaseModel):
    article_text: str = Field(..., min_length=200, max_length=15000,
                               description="The full text of the news article to analyse")
    headline: str     = Field("", max_length=300,
                               description="Optional: the article headline")
    source: str       = Field("", max_length=200,
                               description="Optional: the publication name (e.g. 'The Guardian')")

    @validator("article_text")
    def strip_article(cls, v):
        return v.strip()

    class Config:
        json_schema_extra = {
            "example": {
                "article_text": "India has significantly reduced its purchases of Russian crude oil following...",
                "headline":     "India Cuts Russian Oil Imports Amid Western Pressure",
                "source":       "Financial Times",
            }
        }


class HealthResponse(BaseModel):
    status:    str
    version:   str
    llm_ready: bool
    fred_key:  bool
    message:   str


# ── Routes ──

@app.get("/api/health", response_model=HealthResponse, tags=["System"])
async def health_check():
    """Check service health and API key status."""
    return HealthResponse(
        status    = "ok",
        version   = "1.0.0",
        llm_ready = bool(ANTHROPIC_API_KEY),
        fred_key  = bool(os.getenv("FRED_API_KEY")),
        message   = "ClearView API is running" if ANTHROPIC_API_KEY
                    else "WARNING: ANTHROPIC_API_KEY not configured",
    )


@app.post("/api/analyse", tags=["Analysis"])
async def analyse_article(request: AnalyseRequest):
    """
    Full ClearView analysis pipeline.

    Takes article text and returns:
    - Extracted claims (explicit + implicit)
    - Argument map (nodes + edges)
    - Surfaced implicit assumptions
    - Logical flags
    - Data validation results (FRED, World Bank, UN Comtrade)
    - Quick summary stats
    """
    if not anthropic_client:
        raise HTTPException(
            status_code=503,
            detail="Analysis service unavailable: Anthropic API key not configured.",
        )

    logger.info(f"Analysis request received — {len(request.article_text)} chars, source: '{request.source}'")

    result = await run_analysis(
        article_text    = request.article_text,
        headline        = request.headline,
        source          = request.source,
        anthropic_client= anthropic_client,
    )

    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("error", "Analysis failed"))

    return JSONResponse(content=result)


@app.get("/", tags=["System"])
async def root():
    return {"message": "ClearView API — see /docs for usage", "version": "1.0.0"}


# ── Global error handler ──
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"status": "error", "error": "An unexpected error occurred. Please try again."},
    )


# ── Local dev entry point ──
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.index:app", host="0.0.0.0", port=8000, reload=True)
