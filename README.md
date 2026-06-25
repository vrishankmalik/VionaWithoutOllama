# Zydus Drug Intelligence Platform

*Dedicated To Life*

A web application that searches four Canadian government drug databases at the same
time and returns one consolidated result set, viewable in the browser or downloadable
as an Excel workbook. It runs fully offline with deterministic extraction: no GPU, no
local model, and no third-party AI service is required.

## What it does

- **Search by ingredient** across all four databases at once and download a complete,
  two-tab Excel report.
- **Full universe** export of every product in the Drug Product Database, including
  older grandfathered (pre-NOC) products.
- **IQVIA quarter-over-quarter compare**: upload last quarter's IQVIA Canada extract
  and this quarter's, and download an Excel of only what changed (new products,
  products that dropped off, and material moves in dollars or units).
- **Go / No-Go screening**: filter the results against your own criteria, then export
  a filtered Summary and Detail workbook.
- **Enrichment** of every product with patent dates, Product Monograph label fields,
  and data-protection (Register of Innovative Drugs) information.
- **Power BI and Microsoft Fabric** integration through a JSON endpoint and a direct
  OneLake push.

## Data sources

All data comes from public Canadian government databases. Every value is copied
straight from the source; nothing is guessed or generated.

| Source | What it provides | Link |
|---|---|---|
| Drug Product Database (DPD) | Brand, company, ingredient, strength, form, route, status | https://health-products.canada.ca/dpd-bdpp/ |
| Generic Submissions Under Review | Pipeline generic submissions by ingredient and company | https://www.canada.ca/en/health-canada/services/drug-health-product-review-approval/generic-submissions-under-review.html |
| Notice of Compliance (NOC) | Approval dates, manufacturer, submission class | https://health-products.canada.ca/noc-ac/ |
| Patent Register (PR-RDB) | Patent numbers and filing / grant / expiry dates | https://pr-rdb.hc-sc.gc.ca/pr-rdb/ |

## Quick start

```bash
pip install -r requirements.txt
python3 -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
# open http://localhost:8000
```

That is all that is needed for full functionality. Every extraction path is
deterministic by default.

## Using the app

The home page has three tabs:

1. **Search by ingredient(s)** - type one or more ingredients (one per line or
   comma-separated). Press **Download Excel** for the full report, or **Download
   Filtered Excel** to keep only the products that pass your Go / No-Go rules. An
   IQVIA file can be attached to add market size and value/quantity filters.
2. **Full universe** - download the entire Drug Product Database with no per-product
   PDF reading (fast), or filter first and read PDFs only for products that pass.
3. **IQVIA Compare** - upload two IQVIA Canada extracts and download an Excel of only
   what changed since the previous quarter.

Progress for long exports streams live in the page, and a dashboard renders the
finished Sheet 1 and Sheet 2 without any re-fetching.

## Configuration

The application is configured through environment variables. The most commonly used:

| Variable | Default | Description |
|---|---|---|
| `CACHE_DIR` | `/tmp/canadian_drug_db_cache` | SQLite cache directory (use a writable path in hosted environments) |
| `CACHE_TTL` | `14400` | HTTP cache lifetime in seconds (4 hours) |
| `SOURCE_TIMEOUT` | `60.0` | Seconds before a source is marked timed out |
| `DPD_SEMAPHORE` | `10` | Maximum concurrent DPD per-drug-code requests |
| `ENABLE_OCR` | `1` | Set to `0` to disable pdf2image/pytesseract where poppler is not available |
| `CORS_ALLOWED_ORIGINS` | `*` | Comma-separated allowed origins (set explicitly in production) |
| `LLM_PROVIDER` | `none` | `none` or `azure_openai` (see Optional AI assist below) |

Fabric push and Azure OpenAI add a few more variables; see the in-repo `app/config.py`
for the complete list.

## Power BI and Microsoft Fabric

Point Power BI Desktop at the JSON endpoint (**Get Data -> Web -> Advanced**):

```
URL:    http://<host>:8000/api/powerbi
Params: q = alpelisib
        field = ingredient
        allow_partial = true
```

The response has `sheet1` and `sheet2` keys, each with `columns` and `records`. All
enrichment runs automatically; the first call fetches live data and later calls return
from cache in under two seconds.

From a Fabric notebook you can read the same JSON with `pandas`, download the XLSX, or
have the server push the workbook straight to OneLake via `POST /api/fabric/push`.

## Architecture

```
app/
  main.py            FastAPI app, routes, and the single-page web UI
  config.py          All configuration (URLs, timeouts, cache settings)
  sources/           One module per database (DPD, Generic Submissions, NOC, Patent Register)
  enrichment/        Patents, labeling, workbook build, IQVIA, data protection, full universe
  export_job.py      Async multi-product export pipeline with live progress
  cache.py           SQLite disk cache with TTL
```

Plus a deterministic, no-network test suite under `tests/`.

## Optional AI assist

The platform never uses AI to extract data. A pluggable provider slot
(`app/llm/provider.py`) can optionally add ingredient-synonym expansion, a
plain-language summary, and PDF field extraction for niche or newly approved products.
By default this slot is a no-op, and the deterministic paths (static synonym map,
regex extraction, fuzzy matching) handle everything. To enable Azure OpenAI, implement
`AzureOpenAIProvider._chat()` and set `LLM_PROVIDER=azure_openai` with the
`AZURE_OPENAI_*` variables.

## Testing

```bash
# Offline suite (fast, no network)
make test

# Integration suite (live government sites)
make test-live

# Completeness reconciliation against the DPD bulk extract
make reconcile
```

The offline suite passes with no network access and no AI provider configured.

## Data and accuracy

This tool aggregates and reformats public government data. Coverage of regex-extracted
Product Monograph fields varies by document layout. Always verify values against the
official source databases before relying on them for regulatory, clinical, or
commercial decisions.
