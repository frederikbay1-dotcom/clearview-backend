# ClearView Backend API

News intelligence backend — claim extraction, argument mapping, and empirical data validation.

Built with Python + FastAPI, deployed on Vercel.

---

## What This Does

Exposes two API endpoints:

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/analyse` | POST | Full analysis pipeline: claims, argument map, assumptions, data validation |
| `/api/health` | GET | Health check — confirms API keys are configured |

---

## Local Development Setup

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/clearview-backend.git
cd clearview-backend
```

### 2. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate        # Mac/Linux
# venv\Scripts\activate         # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Set up environment variables

```bash
cp .env.example .env
```

Edit `.env` and add your keys:
```
ANTHROPIC_API_KEY=sk-ant-...your key here...
FRED_API_KEY=your_fred_key_here    # free at https://fred.stlouisfed.org/docs/api/api_key.html
```

### 5. Run locally

```bash
uvicorn api.index:app --reload --port 8000
```

Visit `http://localhost:8000/docs` — you'll see the interactive API documentation.

---

## Deploy to Vercel

### Step 1: Push to GitHub

```bash
git init
git add .
git commit -m "Initial ClearView backend"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/clearview-backend.git
git push -u origin main
```

### Step 2: Connect to Vercel

1. Go to [vercel.com](https://vercel.com) and sign in
2. Click **"Add New Project"**
3. Import your `clearview-backend` GitHub repository
4. Vercel will auto-detect the `vercel.json` configuration
5. Click **"Deploy"** — your first deploy will fail (no API keys yet — that's fine)

### Step 3: Add Environment Variables in Vercel

1. In your Vercel project dashboard, go to **Settings → Environment Variables**
2. Add the following:

| Name | Value | Environment |
|---|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-...your key...` | Production, Preview, Development |
| `FRED_API_KEY` | `your fred key` | Production, Preview, Development |

3. Go to **Deployments** and click **"Redeploy"** on your latest deployment

### Step 4: Test your live API

Once deployed, your API will be live at:
```
https://clearview-backend-YOUR_USERNAME.vercel.app/api/health
```

Visit that URL in your browser — you should see:
```json
{
  "status": "ok",
  "version": "1.0.0",
  "llm_ready": true,
  "fred_key": true,
  "message": "ClearView API is running"
}
```

---

## Testing the Analysis Endpoint

### Using the interactive docs (easiest)

Visit `https://your-vercel-url.vercel.app/docs` and use the built-in Swagger UI.

### Using curl

```bash
curl -X POST https://your-vercel-url.vercel.app/api/analyse \
  -H "Content-Type: application/json" \
  -d '{
    "article_text": "India has significantly reduced its purchases of Russian crude oil in recent months, according to government officials. The shift follows sustained pressure from Western nations and reflects a broader recalibration of New Delhi's energy policy. Indian refiners, who had dramatically increased Russian crude imports since 2022 taking advantage of discounted Urals prices, are now reportedly scaling back as the discount has narrowed. The government has insisted that India's energy decisions are driven purely by economic considerations, not political pressure. Analysts note that Indian crude imports from Russia rose from under 1% of total imports in early 2022 to over 40% by mid-2023.",
    "headline": "India Cuts Russian Oil Imports Amid Western Pressure",
    "source": "Financial Times"
  }'
```

---

## API Response Schema

```json
{
  "status": "success",
  "from_cache": false,
  "article_hash": "abc123...",
  "thesis": "The article's central conclusion in one sentence",
  "claims": [
    {
      "id": "C1",
      "text": "The claim text",
      "type": "explicit_fact | implicit_assumption | normative | hedged",
      "source_hint": "First few words of relevant sentence",
      "is_checkable": true,
      "domain": "economics | geopolitics | energy | other"
    }
  ],
  "argument_map": {
    "conclusion": "Article thesis",
    "nodes": [{"id": "C1", "label": "Short label", "type": "premise | conclusion | assumption"}],
    "edges": [{"from": "C1", "to": "C2", "relation": "supports | contradicts | assumes"}]
  },
  "implicit_assumptions": [
    {
      "id": "A1",
      "text": "The assumption",
      "underlies_claim": "C1",
      "explanation": "Why this matters"
    }
  ],
  "logical_flags": [
    {
      "type": "inferential_gap | correlation_causation | ...",
      "description": "Plain-language description",
      "location": "Which claim this applies to"
    }
  ],
  "validation_results": [
    {
      "claim_id": "C1",
      "status": "supported | partially_supported | contradicted | insufficient_data",
      "summary": "Plain-language validation summary",
      "source_name": "FRED — Federal Reserve Bank of St. Louis",
      "source_date": "2024-01-01",
      "source_url": "https://fred.stlouisfed.org/series/...",
      "raw_data": {}
    }
  ],
  "summary": {
    "total_claims": 8,
    "explicit_facts": 5,
    "implicit_assumptions": 3,
    "checkable_claims": 4,
    "logical_flags_count": 1,
    "validated_count": 2,
    "partial_count": 1,
    "contradicted_count": 0,
    "insufficient_count": 1
  }
}
```

---

## Connecting to Lovable Frontend

Once deployed, give your Lovable project the base URL:
```
https://clearview-backend-YOUR_USERNAME.vercel.app
```

The two endpoints Lovable needs to call:
- `POST /api/analyse` — for article analysis
- `GET /api/health` — to check the service is up

---

## Project Structure

```
clearview-backend/
├── api/
│   ├── __init__.py
│   └── index.py          ← FastAPI app — Vercel entry point
├── core/
│   ├── __init__.py
│   ├── prompts.py        ← All LLM prompts
│   ├── validators.py     ← FRED, World Bank, UN Comtrade integrations
│   └── pipeline.py       ← Main analysis orchestration
├── .env.example          ← Copy to .env — never commit .env
├── .gitignore
├── requirements.txt
├── vercel.json           ← Vercel deployment configuration
└── README.md
```

---

## Data Sources

| Source | Domain | API | Cost |
|---|---|---|---|
| FRED (St. Louis Fed) | US Economics | REST + free key | Free |
| World Bank Open Data | Global Economics | REST, no key | Free |
| UN Comtrade | Trade Flows | REST, free tier | Free |
| World Bank Commodity Prices | Energy/Commodities | REST, no key | Free |
| REST Countries | Geopolitical Facts | REST, no key | Free |

---

## Getting a Free FRED API Key

1. Go to https://fred.stlouisfed.org/docs/api/api_key.html
2. Create a free account
3. Request an API key (instant approval)
4. Add it to your `.env` and Vercel environment variables
