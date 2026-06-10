# Zydus Drug Intelligence Platform

A local/cloud web application that searches four Canadian government drug databases simultaneously and returns consolidated results viewable in a web UI or downloadable as XLSX. Ollama/llama3-free and structured for Microsoft Fabric or any hosted environment.

## Development Rules

**Scope discipline — change only what is described.**
When implementing a task, modify only the files and lines that the task explicitly requires. Do not rename variables, reformat unrelated code, add comments to unchanged functions, or alter logic outside the described scope — even if the surrounding code looks improvable. Side-effect changes are the leading cause of regressions in this codebase.

## Architecture Overview

```
app/
  main.py                # FastAPI app, routes, embedded HTML UI
  config.py              # All configuration (URLs, timeouts, TTLs, Fabric vars)
  consistency.py         # Cross-source DIN consistency checker (warnings, not errors)
  llm/
    __init__.py
    provider.py          # Pluggable LLM provider: NullProvider (default) + AzureOpenAIProvider stub
  sources/
    dpd.py               # Drug Product Database — official REST API only (no scraping)
    generic_submissions.py  # Static HTML table, httpx + BeautifulSoup
    noc.py               # NOC — official JSON API, no CSRF
    patent_register.py   # Patent Register — JSP form POST, SSL workaround
  normalize.py           # Static synonym map + optional LLM expansion
  match.py               # Optional AI summary generation (NullProvider → no-op)
  cache.py               # SQLite disk cache with TTL
  models.py              # Shared Pydantic result schema
  grouping.py            # Ingredient-combination grouping logic
  jobs.py                # In-memory job state (multi-product, SSE events, dashboard snapshot)
  export_job.py          # Async multi-product export pipeline (search → patents → labeling → workbook)
  enrichment/
    store.py             # SQLite enrichment store (patents + labeling, $CACHE_DIR/enrichment.db)
    patents.py           # enrich-patents: Patent.zip join + CPD date fetch
    labeling.py          # enrich-labeling: per-strength PDF field extraction (cite-or-blank)
    workbook.py          # build-workbook: two-tab enriched XLSX, multi-product blocks
    data_protection.py   # Register of Innovative Drugs lookup
tests/
  reconciliation/        # Completeness tests against DPD bulk extract (integration, slow)
    downloader.py        # Download + cache allfiles.zip with freshness check
    dpd_parser.py        # Parse drug.txt/ingred.txt, build DIN sets
    test_reconciliation.py  # Hard-fail if extract − pipeline > 0.5%
  test_cross_source_consistency.py  # DIN-keyed ingredient/brand agreement across sources
  test_fuzzy_precision.py           # Precision ≥ 0.95 on labeled fuzzy_pairs.csv
  test_enrich_patents.py            # Patent detail parsing + discrepancy resolution tests
  test_enrich_labeling.py           # Alpelisib PIQRAY golden test + no-fabrication assertion
  test_build_workbook.py            # Two-tab schema: NOC N/A exclusion, DIN sort, GSUR standalone
  fixtures/
    fuzzy_pairs.csv
    labeling/piqray_pages.json
    patent_register/detail_2709025.html
    cpd/summary_2630344.html
```

## Running

```bash
cd "/Users/vmalik/Desktop/Health Canada (WITHOUT OLLAMA)"
pip install -r requirements.txt
python3 -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
# open http://localhost:8000
```

No Ollama, no local model, no GPU needed. All extraction is deterministic by default.

## Configuration (environment variables)

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `none` | `none` \| `azure_openai` — see LLM section |
| `DPD_SEMAPHORE` | `10` | Max concurrent DPD per-drug-code requests |
| `SOURCE_TIMEOUT` | `60.0` | Seconds before a source is marked timed-out |
| `CACHE_DIR` | `/tmp/canadian_drug_db_cache` | SQLite disk cache directory |
| `CACHE_TTL` | `14400` | HTTP cache TTL in seconds (4 h default) |
| `LABELING_STORE_TTL` | `7200` | Labeling enrichment re-fetch interval (2 h) |
| `PATENT_STORE_TTL` | `14400` | Patent enrichment re-fetch interval (4 h) |
| `WORKBOOK_MIN_FILL_RATE` | `0.02` | Drop Sheet 1 columns with ≤2 % fill rate |
| `ENABLE_OCR` | `1` | `0` disables pdf2image/pytesseract (safe on Fabric) |
| `LABELING_SEMAPHORE` | `8` | Concurrent PDF downloads per export job |
| `CORS_ALLOWED_ORIGINS` | `*` | Comma-separated allowed origins for CORS |
| `FABRIC_ONELAKE_URL` | — | Azure ADLS Gen2 / OneLake endpoint for `/api/fabric/push` |
| `FABRIC_CONTAINER` | — | Container / filesystem name for Fabric push |
| `FABRIC_FOLDER` | `canadian-drug-db` | Folder inside the container |
| `FABRIC_STORAGE_KEY` | — | Storage account key; blank → DefaultAzureCredential |
| `AZURE_OPENAI_ENDPOINT` | — | Required when `LLM_PROVIDER=azure_openai` |
| `AZURE_OPENAI_API_KEY` | — | Required when `LLM_PROVIDER=azure_openai` |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-4o` | Azure OpenAI deployment name |
| `AZURE_OPENAI_API_VERSION` | `2024-02-01` | Azure OpenAI API version |

## Data Sources

### 1. Drug Product Database (DPD)
- **Method:** Official REST API — no scraping
- **Base URL:** `https://health-products.canada.ca/api/drug/`
- **Key API behaviour:** `/drugproduct/?id=<code>` returns a **dict**, not a list. `/status/?id=<code>` also returns a dict. All other enrichment endpoints (`/form/`, `/route/`, `/schedule/`) return lists.
- **Supports:** ingredient, brand, company, DIN
- **Rate limiting:** semaphore-capped (`DPD_SEMAPHORE`, default 10)

### 2. Generic Submissions Under Review
- **Method:** httpx + BeautifulSoup table parse
- **URL:** `https://www.canada.ca/en/health-canada/services/drug-health-product-review-approval/generic-submissions-under-review.html`
- **Table columns:** Medicinal Ingredient(s) | Company Name | Therapeutic Area | Year/Month Accepted
- **Note:** Company "Not available" for pre-April 2024 entries.
- **Supports:** ingredient, company only (brand/DIN → unsupported)

### 3. Notice of Compliance (NOC)
- **Method:** Official JSON API — no form posts, no CSRF
- **API base:** `https://health-products.canada.ca/api/notice-of-compliance/`
- **Ingredient search flow:**
  1. `GET /medicinalingredient/?type=json&lang=en` — full list (~93 k rows, cached per TTL)
  2. Filter in memory where `noc_pi_medic_ingr_name` contains the queried term
  3. Expand each matched product to capture all co-ingredients (combination products)
  4. For each unique `noc_number` (capped at 200): concurrently fetch
     - `GET /drugproduct/?id=<n>` → `(noc_br_product_id, noc_br_din, noc_br_brandname)`
     - `GET /noticeofcompliancemain/?id=<n>` → `noc_date, noc_manufacturer_name, noc_submission_class`
  5. Join `noc_pi_din_product_id == noc_br_product_id` to attach DIN
  6. Emit one `DrugRecord` per `(noc_number, product_id)`
- **Supports:** ingredient only — brand/company/DIN return `status="unsupported"`

### 4. Patent Register (PR-RDB)
- **Method:** Session cookie (JSESSIONID), POST to `/pr-rdb/search`
- **URL:** `https://pr-rdb.hc-sc.gc.ca/pr-rdb/`
- **SSL note:** Server certificate does not chain to trusted CA — `verify=False` is intentional
- **Ingredient field:** dropdown select; our code fetches it and does substring + fuzzy matching
- **Supports:** ingredient, brand, DIN (not company)

## LLM Provider

LLM is **never used for data extraction**. It is used only for:

1. **Ingredient synonym expansion** — `normalize.py` calls `provider.expand_synonyms(term)` as an add-on to the static synonym map.
2. **Plain-language summary** — `match.py`, only if `?summary=true`; labeled as AI-generated.
3. **PDF appearance field extraction** — `labeling.py` tries provider first; falls back to regex.
4. **Data-protection manufacturer matching** — `data_protection.py` tries provider; falls back to difflib.

### Default: NullProvider (no LLM)

All four call sites return empty / `None` with `NullProvider`. The deterministic paths (static synonym map, regex extraction, difflib fuzzy matching) handle everything. No network calls, no latency, no cost.

### Enabling Azure OpenAI

1. Implement `AzureOpenAIProvider._chat()` in [app/llm/provider.py](app/llm/provider.py) (docstring shows two complete patterns — openai SDK or plain httpx).
2. Set `LLM_PROVIDER=azure_openai` and the four `AZURE_OPENAI_*` env vars.

| Method | NullProvider | Configured provider |
|---|---|---|
| `expand_synonyms(name)` | `[]` — static map only | Adds LLM synonyms |
| `summarize_results(q, srcs)` | `None` | Returns AI Summary |
| `extract_appearance_fields(text, page, group)` | `{}` — regex active | Structured fields |
| `confirm_innovative_drug_match(ing, co, sl)` | `None` — difflib active | Picks best row |

### Adding a different provider

1. Create a class with the four async methods (see `LLMProvider` Protocol in `app/llm/provider.py`).
2. Add a branch in `get_llm_provider()` for a new `LLM_PROVIDER` value.
3. That's it — no call-site changes.

## Multi-Product Export

The export UI accepts multiple ingredients (one per line or comma-separated). Each ingredient becomes one colour-coded horizontal block in the two-tab XLSX:

- **Search** — previews the first ingredient; all N are exported.
- **`/export/start`** — accepts `queries: ["alpelisib", "apremilast", ...]`; each product is searched and enriched; side-by-side blocks in both sheets.
- **Progress** — SSE stream at `/export/stream/{job_id}` gives per-stage progress (Search 0–20 %, Patents 20–50 %, Labeling 50–85 %, Workbook 85–100 %).
- **Dashboard** — after export completes, the in-page dashboard shows KPI cards and both sheet tables; data is read from the job snapshot (`/api/export-data/{job_id}`), never re-scraped.

## Caching

| Cache | Location | Key |
|---|---|---|
| HTTP (DPD, NOC, GSUR, Patent.zip) | `$CACHE_DIR/cache.db` | `sha256(source:query)` |
| Enrichment (patents + labeling) | `$CACHE_DIR/enrichment.db` | DIN primary key |

TTLs: HTTP cache 4 h (`CACHE_TTL`), labeling 2 h (`LABELING_STORE_TTL`), patents 4 h (`PATENT_STORE_TTL`).

To reset all caches: delete `/tmp/canadian_drug_db_cache/`.  
To reset only labeling: `POST /api/reset-labeling-cache`.

## Tests

```bash
# Offline suite (fast, no network, no LLM) — run before every commit
make test

# Integration suite (live government sites)
make test-live

# Completeness reconciliation against DPD nightly bulk extract (slow, nightly)
make reconcile
```

All tests pass with `LLM_PROVIDER` unset and no Ollama installed.

Three additional accuracy checks:
1. **Completeness reconciliation** (`make reconcile`) — hard-fails if pipeline misses >0.5 % of DPD bulk extract DINs.
2. **Cross-source consistency** (`test_cross_source_consistency.py`) — ingredient sets and brand names must agree across sources for every shared DIN.
3. **Fuzzy precision** (`test_fuzzy_precision.py`) — Patent Register ingredient matcher precision ≥ 0.95 on `tests/fixtures/fuzzy_pairs.csv`.

## Enrichment Pipeline

All enrichment commands share `$CACHE_DIR/enrichment.db`. Run in order, or use the Export button / `/api/powerbi` endpoint (which runs all stages automatically).

### `enrich-patents`

`python -m app.enrichment.patents --dins DIN1 DIN2 ...`

- Primary source: `Patent.zip` two-file join (`drugs_e.txt` → DIN map, `patent-service_e.txt` → dates). Dates are MM/DD/YYYY converted to ISO.
- Fallback: CPD summary page (`brevets-patents.ic.gc.ca`). **CPD is currently broken** (308 redirect to bare IP) — ZIP dates are authoritative. Code detects any 3xx and skips CPD without blocking.
- **Canary verified:** LEQEMBI/lecanemab DIN 02562383 → patent 2630344 → filing 2007-03-23, grant 2015-04-28, expiry 2027-03-23.

### `enrich-labeling`

`python -m app.enrichment.labeling --drug-code CODE --din DIN --strength "50 mg"`

**Cite-or-blank rule:** every extracted value records its page number. Absent fields store `"Not stated"` — never inferred. `_page` is NULL for Not-stated values.

**Sections read:**
- §6 Dosage Forms/Composition/Packaging: `active_ingredient`, `excipients_core`, `excipients_coating`, `preservatives`, `pack_size`, `pack_style`, `colour`, `shape`, `size_mm`, `weight`.
- §13 Pharmaceutical Information: `ph`.

**Per-strength matching:** the DIN's strength (from DPD) selects the matching Description block in §6 — one row per DIN.

**Scanned PDFs:** all fields → `"needs OCR / manual check"`, `needs_ocr=1`.

**Golden accuracy fixture:** `tests/fixtures/labeling/piqray_pages.json` + `TestPiqrayGolden` asserts exact match on all 31 fields and enforces the no-fabrication rule.

### `build-workbook`

`python -m app.enrichment.workbook --q alpelisib --field ingredient`

Or via API: `GET /api/export?q=alpelisib&field=ingredient` (synchronous) or `/export/start` (async with SSE progress).

**Sheet 1 — "DPD + NOC + Patents"** (one row per DIN, sorted ascending by DIN):
- DPD: brand, company, ingredient, strength, form, route, status, drug_code.
- NOC: noc_date, reason_for_supplement, submission_class, noc_submission_type, noc_therapeutic_class (joined by DIN). **NOC rows with blank / "Not Applicable" / "N/A" DIN are excluded.**
- Patents: patent_count, patent_1_number/filing_date/grant_date/expiry_date, … per group.
- Labeling: all 11 label fields + `_page` citation columns, needs_ocr flag.
- Data protection: dp_6yr_no_file_date, pediatric_extension, data_protection_ends.

**Sheet 2 — "Generic Submissions"** (standalone, never joined):
- GSUR records filtered to the queried ingredient.

**Column pruning:** Sheet 1 columns with ≤ `WORKBOOK_MIN_FILL_RATE` (2 %) non-empty rows are dropped. Columns in `_NEVER_DROP_COLS` are always kept. Patent group columns are evaluated and dropped together.

### Enrichment store schema

`$CACHE_DIR/enrichment.db`:

```sql
patents(din, patent_number, filing_date, grant_date, expiry_date, detail_url, fetched_at)
patent_discrepancies(din, patent_number, field, website_value, zip_value, logged_at)
labeling(din, drug_code, pdf_url,
         active_ingredient, active_ingredient_page,
         excipients_core, excipients_core_page,
         excipients_coating, excipients_coating_page,
         preservatives, preservatives_page,
         pack_size, pack_size_page,
         pack_style, pack_style_page,
         colour, colour_page,
         shape, shape_page,
         size_mm, size_mm_page,
         weight, weight_page,
         ph, ph_page,
         needs_ocr, has_unverified, fetched_at)
```

## Power BI Integration

### Web Connector (recommended)

In Power BI Desktop: **Get Data → Web → Advanced**

```
URL:    http://<host>:8000/api/powerbi
Params: q = alpelisib
        field = ingredient
        allow_partial = true
```

The response is a JSON object with `sheet1` and `sheet2` keys, each containing `columns` (list of strings) and `records` (list of dicts). Use **Transform Data → Expand** to convert `records` into a table.

All enrichment (patents, labeling, data protection) runs automatically; first call fetches live data, subsequent calls use the SQLite cache and return in < 2 s.

### Scheduled Refresh on Power BI Service

Deploy the app to an Azure VM or App Service. Power BI Service needs a **Data Gateway** (or public HTTPS endpoint) to reach it. Use `CORS_ALLOWED_ORIGINS=https://app.powerbi.com` for production.

### M query snippet (Power Query)

```m
let
    Source = Json.Document(
        Web.Contents("http://localhost:8000/api/powerbi",
            [Query = [q = "alpelisib", field = "ingredient", allow_partial = "true"]])
    ),
    Sheet1 = Source[sheet1],
    Records = Sheet1[records],
    Table = Table.FromList(Records, Splitter.SplitByNothing(), null, null, ExtraValues.Error),
    Expanded = Table.ExpandRecordColumn(Table, "Column1", Sheet1[columns])
in
    Expanded
```

## Microsoft Fabric Integration

### From a Fabric Notebook (Python)

```python
import requests, pandas as pd

BASE = "http://<your-host>:8000"

# Option 1: Get JSON directly (uses cache)
resp = requests.get(f"{BASE}/api/powerbi", params={"q": "alpelisib", "field": "ingredient"})
data = resp.json()
df_sheet1 = pd.DataFrame(data["sheet1"]["records"], columns=data["sheet1"]["columns"])
df_sheet2 = pd.DataFrame(data["sheet2"]["records"], columns=data["sheet2"]["columns"])

# Option 2: Download XLSX and save to OneLake
xlsx = requests.get(f"{BASE}/api/export", params={"q": "alpelisib"}).content
with open("/lakehouse/default/Files/alpelisib.xlsx", "wb") as f:
    f.write(xlsx)

# Option 3: Push directly from the API to OneLake (server-side upload)
# Requires FABRIC_ONELAKE_URL + FABRIC_CONTAINER set on the server
r = requests.post(f"{BASE}/api/fabric/push", params={"q": "alpelisib"})
print(r.json())  # {"status": "ok", "path": "https://..."}
```

### Fabric Data Factory / Pipelines

Use a **Web Activity** to call `POST /api/fabric/push?q=<ingredient>`. The API writes the enriched XLSX directly to the configured OneLake path. Set FABRIC_ONELAKE_URL, FABRIC_CONTAINER, and either FABRIC_STORAGE_KEY or a Managed Identity on the host VM.

### Deploying on Fabric Spark / Lakehouse

The app is a plain Python FastAPI service. Deploy it on an Azure VM or Azure Container Apps with these settings:
- `CACHE_DIR` → a writable path in the container (e.g. `/home/user/.cache/canadian_drug_db`)
- `ENABLE_OCR=0` → disables pdf2image/pytesseract if poppler is not available
- `CORS_ALLOWED_ORIGINS=https://app.powerbi.com,https://*.fabric.microsoft.com`

## API Reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/search` | Live search across all four sources |
| `GET` | `/api/export` | Synchronous two-tab XLSX download |
| `POST` | `/export/start` | Start async export job → job_id |
| `GET` | `/export/stream/{job_id}` | SSE progress stream |
| `GET` | `/export/result/{job_id}` | Download finished XLSX |
| `GET` | `/api/export-data/{job_id}` | Job's Sheet 1+2 as JSON (dashboard) |
| `GET` | `/api/powerbi` | **Power BI / Fabric JSON endpoint** — search + enrich + return flat JSON |
| `POST` | `/api/fabric/push` | Push XLSX to Azure Data Lake / OneLake |
| `POST` | `/api/reset-labeling-cache` | Drop labeling table, forcing re-extraction |

## Dependencies

```
fastapi uvicorn httpx beautifulsoup4 openpyxl pandas pydantic python-multipart pdfplumber
```

Optional (LLM provider):
```
openai  # or httpx (already installed) — used inside AzureOpenAIProvider._chat()
```

Optional (Fabric push):
```
azure-storage-blob azure-identity
```

Optional (OCR):
```
pdf2image pytesseract  # also needs poppler in PATH
```

Optional (tests):
```
pytest pytest-asyncio anyio
```

## Accuracy Notes (NullProvider / no LLM)

- **DPD, NOC, GSUR, Patent Register searches** — fully deterministic. Live comparison against the Ollama version across 20 ingredients confirmed the static-map approach is **more precise**: the Ollama version hallucinated drug-class peers as synonyms (e.g. dabigatran/apixaban for rivaroxaban; ertugliflozin for canagliflozin), producing false positives. The static map never did.
- **Synonym map coverage** — 85 entries covering salt forms, brand names, and INN variants. Brand name coverage includes all commonly searched biologics and small molecules (dupilumab→dupixent, nivolumab→opdivo, pembrolizumab→keytruda, osimertinib→tagrisso, venetoclax→venclexta, lecanemab→leqembi, etc.). See `app/normalize.py` `_STATIC_SYNONYMS` for the full list.
- **Excipients, preservatives** — regex extraction; ~90 % coverage on common PM layouts.
- **Appearance fields (colour, shape, size_mm, weight)** — regex; colour + shape well-covered; size_mm/weight ~60–70 % of PMs.
- **pH** — regex; ~80 % coverage for liquid/solution PMs.
- **Data-protection matching** — difflib fuzzy at cutoff 0.8; handles most manufacturer name variations; ambiguous shortlists return blank rather than guessing.

To add LLM-assisted synonym expansion: implement `AzureOpenAIProvider._chat()` and set `LLM_PROVIDER=azure_openai`. Note: the static map already covers the correct synonyms; LLM expansion is only useful for niche or newly-approved ingredients not yet in the map.

## Known Limitations / Gotchas

- **Patent Register SSL:** `verify=False` is intentional — the PR-RDB server certificate does not chain to a trusted CA.
- **CPD broken since June 2026:** `brevets-patents.ic.gc.ca` returns a 308 redirect to a bare IP. Code detects any 3xx with `follow_redirects=False` and uses Patent.zip dates instead.
- **NOC brand/company/DIN searches unsupported:** the JSON API only exposes ingredient search; these return `status="unsupported"`.
- **DPD concurrency:** 242 drug codes for "metformin" all queried behind a semaphore of 10. First load 5–15 s; cached after.
- **Patent Register generics:** Long-off-patent generics (e.g. metformin) return `no_results` — truthful, not a bug.
- **error vs no_results:** `status="error"` = fetch failed; `status="no_results"` = source responded but found nothing. Never conflate. Export refuses to build (HTTP 409) on `error`; pass `allow_partial=true` to override.

## Register of Innovative Drugs (data protection)

The `id="a1"` attribute is on the `<table>` element itself. Column "Drug(s) Containing the Medicinal Ingredient / Variations" (col 4) also contains "medicinal ingredient" — the parser guards against it by requiring the header to **start with** "Medicinal Ingredient".

`pediatric_extension` output is `"Yes"` or `"No"` only. "N/A", blank, "-", or any unrecognised value → `"No"`.

## pack_style extraction rules

`_extract_pack_style_from_pdf` returns a normalised container vocab label (e.g. "Vial", "Blister") — never raw captured text. Hard-reject guard: any captured block containing "the following", "dosage strengths", "dosage form", or any line ending with ":" is discarded.

## Patent.zip bulk extract

URL: `https://pr-rdb.hc-sc.gc.ca/patent/Patent.zip` (note: `/patent/` path)

Two-file join:
- `drugs_e.txt`: `DRUG_ID → DIN` mapping
- `patent-service_e.txt`: `DRUG_ID → PATENT_NUMBER + FILING_DATE + DATE_GRANTED + EXPIRATION_DATE`

DIN-to-patent mapping requires joining on `DRUG_ID`. Dates are MM/DD/YYYY → ISO.

## Ingredient-Combination Grouping

Results group by each product's **full active-ingredient combination**. Implemented in `app/grouping.py`.

Grouping key: normalize (strip, uppercase, collapse whitespace), deduplicate, sort alphabetically → joined with `COMBINATION_SEPARATOR` (` + ` from `config.py`).

`NORMALIZE_SALT_FORMS` env var (default `0`) — when on, strips salt forms before matching.
