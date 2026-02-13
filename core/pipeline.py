"""
ClearView — Analysis Pipeline
Orchestrates the full LLM analysis + data validation flow.
"""

import json
import logging
import asyncio
import hashlib
from typing import Optional
from cachetools import TTLCache

import anthropic

from core.prompts import SYSTEM_PROMPT, ANALYSIS_PROMPT, VALIDATION_SYNTHESIS_PROMPT
from core.validators import route_and_execute_query

logger = logging.getLogger(__name__)

# ── In-memory cache: results valid for 24 hours, max 200 entries ──
_analysis_cache: TTLCache = TTLCache(maxsize=200, ttl=86400)


def _content_hash(text: str) -> str:
    """Create a short hash of article content for caching."""
    return hashlib.sha256(text.strip().encode()).hexdigest()[:16]


def _format_data_for_synthesis(data: dict) -> str:
    """Convert raw API data into a readable summary for the synthesis prompt."""
    if not data or not data.get("available"):
        return "No data available."

    lines = []
    source = data.get("source", "Unknown source")
    lines.append(f"Source: {source}")

    if "latest_value" in data and "latest_date" in data:
        lines.append(f"Most recent value: {data['latest_value']} (as of {data['latest_date']})")
    if "latest_year" in data:
        lines.append(f"Most recent year: {data['latest_year']}")
    if "indicator" in data:
        lines.append(f"Indicator: {data['indicator']}")
    if "country" in data:
        lines.append(f"Country: {data['country']}")
    if "recent_values" in data:
        vals = data["recent_values"]
        if vals:
            lines.append("Recent trend: " + ", ".join(
                f"{v.get('date') or v.get('year')}: {v.get('value')}" for v in vals
            ))
    if "total_value_usd" in data:
        lines.append(f"Total trade value (USD): {data['total_value_usd']:,.0f}")
    if "top_flows" in data:
        for flow in data["top_flows"][:2]:
            lines.append(f"  {flow.get('partner', 'Unknown')}: USD {flow.get('value_usd', 0):,.0f} ({flow.get('flow', '')})")
    if "commodity" in data and "latest_value" in data:
        lines.append(f"Commodity: {data['commodity']}")
    if "population" in data:
        lines.append(f"Population: {data['population']:,}")
    if "region" in data:
        lines.append(f"Region: {data['region']} / {data.get('subregion', '')}")

    return "\n".join(lines)


async def run_analysis(
    article_text: str,
    headline: str = "",
    source: str = "",
    anthropic_client: anthropic.AsyncAnthropic = None,
) -> dict:
    """
    Full ClearView analysis pipeline:
    1. Check cache
    2. LLM epistemic analysis (claim extraction, argument map, assumptions)
    3. Data validation (parallel API queries)
    4. Synthesis of validation results
    5. Return structured output
    """

    # ── Step 0: Cache check ──
    cache_key = _content_hash(article_text)
    if cache_key in _analysis_cache:
        logger.info(f"Cache hit for article hash {cache_key}")
        cached = _analysis_cache[cache_key].copy()
        cached["from_cache"] = True
        return cached

    if not anthropic_client:
        raise ValueError("Anthropic client is required")

    # ── Step 1: LLM Epistemic Analysis ──
    logger.info("Starting LLM epistemic analysis...")
    analysis_prompt = ANALYSIS_PROMPT.format(
        headline=headline or "(no headline provided)",
        source=source or "(source not specified)",
        article_text=article_text[:12000],  # cap at ~12k chars for context safety
    )

    try:
        message = await anthropic_client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": analysis_prompt}],
        )
        raw_response = message.content[0].text

        # Strip markdown code fences if present
        cleaned = raw_response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        cleaned = cleaned.strip().rstrip("```").strip()

        llm_analysis = json.loads(cleaned)

    except json.JSONDecodeError as e:
        logger.error(f"LLM returned invalid JSON: {e}\nRaw: {raw_response[:500]}")
        return {
            "status":  "error",
            "error":   "Analysis engine returned malformed output. Please try again.",
            "details": str(e),
        }
    except Exception as e:
        logger.error(f"LLM analysis failed: {e}")
        return {"status": "error", "error": str(e)}

    # ── Step 2: Parallel Data Validation ──
    validation_queries = llm_analysis.get("validation_queries", [])
    logger.info(f"Running {len(validation_queries)} validation queries in parallel...")

    validation_tasks = [
        route_and_execute_query(q)
        for q in validation_queries
        if q.get("suggested_source") not in ("other", None)
    ]

    raw_validation_results = []
    if validation_tasks:
        raw_validation_results = await asyncio.gather(*validation_tasks, return_exceptions=True)
        # Filter out exceptions — degrade gracefully
        raw_validation_results = [
            r for r in raw_validation_results
            if isinstance(r, dict)
        ]

    # ── Step 3: Synthesise Validation Results with LLM ──
    logger.info("Synthesising validation results...")
    validation_summaries = []

    synthesis_tasks = []
    for vr in raw_validation_results:
        data = vr.get("data", {})
        if data and data.get("available"):
            synthesis_tasks.append({
                "claim_id":    vr["claim_id"],
                "claim_text":  vr["claim_text"],
                "data":        data,
                "source_name": data.get("source", "Unknown"),
                "data_date":   data.get("latest_date") or data.get("latest_year") or "Unknown",
            })

    # Run synthesis calls (sequentially to avoid rate limits; typically 2-4 calls)
    for task in synthesis_tasks:
        try:
            synth_prompt = VALIDATION_SYNTHESIS_PROMPT.format(
                claim_text=task["claim_text"],
                data_summary=_format_data_for_synthesis(task["data"]),
                source_name=task["source_name"],
                data_date=task["data_date"],
            )
            synth_msg = await anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001",  # cheaper model for synthesis
                max_tokens=256,
                messages=[{"role": "user", "content": synth_prompt}],
            )
            summary_text = synth_msg.content[0].text.strip()

            # Determine validation status from synthesis text
            status = _infer_validation_status(summary_text)

            validation_summaries.append({
                "claim_id":        task["claim_id"],
                "status":          status,
                "summary":         summary_text,
                "source_name":     task["source_name"],
                "source_date":     task["data_date"],
                "source_url":      task["data"].get("url", ""),
                "raw_data":        _safe_raw_data(task["data"]),
            })
        except Exception as e:
            logger.warning(f"Synthesis failed for claim {task['claim_id']}: {e}")
            validation_summaries.append({
                "claim_id":    task["claim_id"],
                "status":      "insufficient_data",
                "summary":     "Data was retrieved but could not be synthesised.",
                "source_name": task.get("source_name", ""),
                "source_date": "",
                "source_url":  "",
            })

    # Mark unvalidated checkable claims
    validated_ids = {v["claim_id"] for v in validation_summaries}
    for q in validation_queries:
        if q["claim_id"] not in validated_ids:
            validation_summaries.append({
                "claim_id":    q["claim_id"],
                "status":      "insufficient_data",
                "summary":     "No suitable data found in available sources for this claim.",
                "source_name": "",
                "source_date": "",
                "source_url":  "",
            })

    # ── Step 4: Assemble Final Result ──
    result = {
        "status":               "success",
        "from_cache":           False,
        "article_hash":         cache_key,
        "thesis":               llm_analysis.get("thesis", ""),
        "claims":               llm_analysis.get("claims", []),
        "argument_map":         llm_analysis.get("argument_map", {}),
        "implicit_assumptions": llm_analysis.get("implicit_assumptions", []),
        "logical_flags":        llm_analysis.get("logical_flags", []),
        "validation_results":   validation_summaries,
        "summary":              _build_summary(llm_analysis, validation_summaries),
    }

    # ── Step 5: Cache and return ──
    _analysis_cache[cache_key] = result
    logger.info(f"Analysis complete. Hash: {cache_key}")
    return result


def _infer_validation_status(synthesis_text: str) -> str:
    """Infer validation status from synthesis text keywords."""
    text = synthesis_text.lower()
    if any(w in text for w in ("contradicts", "contradicted", "does not support", "inconsistent with", "contrary to", "refutes")):
        return "contradicted"
    if any(w in text for w in ("partially", "mixed", "somewhat", "partially supports", "nuanced", "complex picture")):
        return "partially_supported"
    if any(w in text for w in ("supports", "corroborates", "consistent with", "confirms", "aligns with", "validates")):
        return "supported"
    return "insufficient_data"


def _safe_raw_data(data: dict) -> dict:
    """Return a safe subset of raw data for the frontend."""
    safe_keys = ["source", "latest_value", "latest_date", "latest_year",
                 "indicator", "country", "commodity", "url", "recent_values",
                 "total_value_usd", "series_label"]
    return {k: v for k, v in data.items() if k in safe_keys}


def _build_summary(llm_analysis: dict, validation_summaries: list) -> dict:
    """Build the top-level Quick Summary stats."""
    claims = llm_analysis.get("claims", [])
    raw_summary = llm_analysis.get("summary", {})

    validated     = [v for v in validation_summaries if v["status"] == "supported"]
    partial       = [v for v in validation_summaries if v["status"] == "partially_supported"]
    contradicted  = [v for v in validation_summaries if v["status"] == "contradicted"]
    insuff        = [v for v in validation_summaries if v["status"] == "insufficient_data"]

    return {
        "total_claims":          raw_summary.get("total_claims", len(claims)),
        "explicit_facts":        raw_summary.get("explicit_facts", 0),
        "implicit_assumptions":  raw_summary.get("implicit_assumptions", len(llm_analysis.get("implicit_assumptions", []))),
        "normative_claims":      raw_summary.get("normative_claims", 0),
        "hedged_claims":         raw_summary.get("hedged_claims", 0),
        "checkable_claims":      raw_summary.get("checkable_claims", len(llm_analysis.get("validation_queries", []))),
        "logical_flags_count":   raw_summary.get("logical_flags_count", len(llm_analysis.get("logical_flags", []))),
        "validated_count":       len(validated),
        "partial_count":         len(partial),
        "contradicted_count":    len(contradicted),
        "insufficient_count":    len(insuff),
    }
