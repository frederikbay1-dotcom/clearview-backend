"""
ClearView — Prompt Templates
All LLM prompts are defined here for easy iteration and testing.
"""

SYSTEM_PROMPT = """You are ClearView's analysis engine — an expert in critical thinking, 
argument analysis, and empirical reasoning. Your role is to analyse news articles with 
intellectual rigour and complete neutrality.

You identify:
- What an article explicitly claims
- What logic connects those claims to the article's conclusion
- What assumptions the article relies on but never states
- Which claims can be checked against data

You never issue verdicts. You surface evidence and structure so readers can think for themselves.
You are equally rigorous with articles from all political perspectives.

Always respond with valid JSON matching the schema requested. No prose outside the JSON."""


ANALYSIS_PROMPT = """Analyse the following news article and return a structured JSON analysis.

ARTICLE HEADLINE: {headline}
ARTICLE SOURCE: {source}

ARTICLE TEXT:
{article_text}

Return ONLY valid JSON matching this exact schema — no markdown, no prose:

{{
  "thesis": "The article's central conclusion or argument in one sentence",
  
  "claims": [
    {{
      "id": "C1",
      "text": "Exact or close paraphrase of the claim",
      "type": "explicit_fact | implicit_assumption | normative | hedged",
      "source_hint": "Brief quote or location hint from article (first few words of relevant sentence)",
      "is_checkable": true,
      "domain": "economics | geopolitics | energy | health | other"
    }}
  ],
  
  "argument_map": {{
    "conclusion": "Restate the thesis",
    "nodes": [
      {{
        "id": "C1",
        "label": "Short label (5-8 words)",
        "type": "premise | conclusion | assumption"
      }}
    ],
    "edges": [
      {{
        "from": "C1",
        "to": "C2",
        "relation": "supports | contradicts | assumes"
      }}
    ]
  }},
  
  "implicit_assumptions": [
    {{
      "id": "A1",
      "text": "The assumption stated clearly",
      "underlies_claim": "C1",
      "explanation": "Why this assumption is required for the argument to hold, and why it is not self-evident"
    }}
  ],
  
  "logical_flags": [
    {{
      "type": "inferential_gap | correlation_causation | cherry_picked_data | false_dichotomy | appeal_to_authority | other",
      "description": "Plain-language description of the logical issue",
      "location": "Which claim or part of the argument this applies to"
    }}
  ],
  
  "validation_queries": [
    {{
      "claim_id": "C1",
      "claim_text": "The claim to validate",
      "query_description": "What data would validate or contradict this claim",
      "suggested_source": "fred | worldbank | uncomtrade | worldbank_commodity | rest_countries | other",
      "suggested_parameters": {{
        "description": "Natural language description of the specific data series or parameters needed"
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

IMPORTANT RULES:
- Extract ALL significant claims — aim for 5-12 per article
- Identify 2-5 implicit assumptions — these are the most valuable output
- Be specific in assumption explanations — generic observations are not useful  
- Logical flags should only be included when genuinely present — do not manufacture them
- validation_queries should only be generated for is_checkable: true claims
- Maintain complete political neutrality — apply identical rigour regardless of article perspective
- All text should be in plain English accessible to an intelligent non-specialist"""


VALIDATION_SYNTHESIS_PROMPT = """You are synthesising data validation results for a news article claim.

CLAIM: {claim_text}

RETRIEVED DATA:
{data_summary}

DATA SOURCE: {source_name}
DATA DATE: {data_date}

Write a plain-language validation summary (2-3 sentences) that:
1. States clearly what the data shows
2. States whether it supports, partially supports, or contradicts the claim
3. Notes any important caveats (e.g. timing differences, definitional issues)

Be precise but accessible. Do not use jargon. Do not editorialize.
Return ONLY the summary text — no JSON, no labels."""
