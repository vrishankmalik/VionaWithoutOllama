import os

# Ingredient-combination grouping
COMBINATION_SEPARATOR = " + "
# Salt-form normalization (default off — keeps "DIPHENHYDRAMINE HCL" as-is)
NORMALIZE_SALT_FORMS = bool(int(os.getenv("NORMALIZE_SALT_FORMS", "0")))

# LLM provider — see app/llm/provider.py
# LLM_PROVIDER = none (default) | azure_openai
# No localhost dependency; all optional config via env vars.

# Concurrency / timeouts
DPD_SEMAPHORE = int(os.getenv("DPD_SEMAPHORE", "20"))
SOURCE_TIMEOUT = float(os.getenv("SOURCE_TIMEOUT", "60.0"))  # seconds per source (interactive UI)
# Per-source budget for the async export/enrichment job. The export runs in the
# background with a progress bar, so it can afford a longer wait than the
# interactive search — this lets big ingredients (e.g. metformin, 242 DINs)
# finish their full uncapped DPD fetch in one pass instead of timing out.
EXPORT_SOURCE_TIMEOUT = float(os.getenv("EXPORT_SOURCE_TIMEOUT", "300.0"))  # seconds
# Cap DPD results for the INTERACTIVE search when an ingredient matches hundreds
# of products (a human is waiting; the UI shows a "first N of M" notice).
DPD_MAX_RESULTS = int(os.getenv("DPD_MAX_RESULTS", "150"))
# Export/enrichment path: effectively uncapped so competitor/approval counts are
# complete. A high ceiling remains only as a runaway-query backstop.
DPD_EXPORT_MAX_RESULTS = int(os.getenv("DPD_EXPORT_MAX_RESULTS", "5000"))

# Cache
CACHE_DIR = os.getenv("CACHE_DIR", "/tmp/canadian_drug_db_cache")
CACHE_TTL = int(os.getenv("CACHE_TTL", str(60 * 60 * 4)))  # 4 hours default

# Base URLs
DPD_BASE = "https://health-products.canada.ca/api/drug"
GENERIC_SUBS_URL = (
    "https://www.canada.ca/en/health-canada/services/drug-health-product-review-approval"
    "/generic-submissions-under-review.html"
)
NOC_BASE = "https://health-products.canada.ca/noc-ac"
PATENT_BASE = "https://pr-rdb.hc-sc.gc.ca/pr-rdb"

# HTTP
USER_AGENT = (
    "Mozilla/5.0 (compatible; CanadaDrugAggregator/1.0; "
    "+https://github.com/local/canadian-drug-db)"
)
HTTP_TIMEOUT = 20.0  # seconds per individual HTTP request

# OCR for scanned product monograph PDFs (requires pdf2image + pytesseract + poppler)
ENABLE_OCR = bool(int(os.getenv("ENABLE_OCR", "1")))

# Concurrent PDF downloads + labeling enrichments in the async export job
LABELING_SEMAPHORE = int(os.getenv("LABELING_SEMAPHORE", "8"))

# Enrichment store TTLs: records older than these are re-fetched on the next export.
# HTTP caches (DPD API, Patent.zip) are unaffected — only the stored results are refreshed.
LABELING_STORE_TTL = int(os.getenv("LABELING_STORE_TTL", str(2 * 60 * 60)))   # 2 hours
PATENT_STORE_TTL   = int(os.getenv("PATENT_STORE_TTL",   str(4 * 60 * 60)))   # 4 hours

# Workbook column pruning: drop Sheet 1 columns whose non-empty fill rate is at or below
# this threshold.  0.0 = strict (only truly-all-empty columns dropped).
# 0.02 = drop any column filled in ≤2% of rows — removes single-stray-value patent groups
# (1/223 ≈ 0.45%) while preserving columns with meaningful coverage (≥5 rows ≈ 2.2%).
WORKBOOK_MIN_FILL_RATE = float(os.getenv("WORKBOOK_MIN_FILL_RATE", "0.02"))

# ── IQVIA quarter-over-quarter comparison thresholds ──────────────────────────
# A MAT (Moving Annual Total) drifts on nearly every row every period, so an
# "any metric differs" diff flags ~80% of shared rows.  A move is reported only
# when it clears BOTH an absolute floor AND a percent floor on at least one
# genuinely independent metric (Dollars or Units).  The percent floor removes
# rolling-total drift; the absolute floor removes tiny-base percent explosions.
# Entrants/exits are never thresholded — appearing/disappearing is always material.
IQVIA_DIFF_DOLLARS_ABS = float(os.getenv("IQVIA_DIFF_DOLLARS_ABS", "100000"))  # $100k
IQVIA_DIFF_UNITS_ABS   = float(os.getenv("IQVIA_DIFF_UNITS_ABS",   "1000"))    # 1,000 units
IQVIA_DIFF_PCT         = float(os.getenv("IQVIA_DIFF_PCT",         "0.10"))    # 10%

# ── Integration: CORS ─────────────────────────────────────────────────────────
# Comma-separated list of allowed origins for CORS.  Default "*" allows Power BI
# Service, Fabric notebooks, and any other browser-based consumer to call the API.
# Restrict to specific origins in production (e.g. "https://app.powerbi.com").
CORS_ALLOWED_ORIGINS: list[str] = [
    o.strip()
    for o in os.getenv("CORS_ALLOWED_ORIGINS", "*").split(",")
    if o.strip()
]

# ── Integration: Microsoft Fabric / Azure Data Lake ───────────────────────────
# Set these to enable /api/fabric/push — writes the finished XLSX directly into
# an Azure Data Lake Storage Gen2 / OneLake path.
# Requires: pip install azure-storage-blob azure-identity
FABRIC_ONELAKE_URL = os.getenv("FABRIC_ONELAKE_URL", "")        # e.g. https://<account>.dfs.core.windows.net
FABRIC_CONTAINER   = os.getenv("FABRIC_CONTAINER", "")          # container / filesystem name
FABRIC_FOLDER      = os.getenv("FABRIC_FOLDER", "canadian-drug-db")  # folder inside container
# If blank, uses DefaultAzureCredential (Managed Identity on Fabric).
FABRIC_STORAGE_KEY = os.getenv("FABRIC_STORAGE_KEY", "")
