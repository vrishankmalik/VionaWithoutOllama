"""Data protection fields from the Register of Innovative Drugs.

Scrapes the "Products for Human Use - Active Data Protection Period" table (#a1)
from the Health Canada Register of Innovative Drugs and matches each row to a
DIN by normalised ingredient + manufacturer.

Matching order:
  1. Ingredient prefilter (normalised substring)
  2. Exact normalised manufacturer → return if unique match
  3. LLM provider confirmation among the shortlist (optional — NullProvider by default)
  4. Fuzzy manufacturer fallback (difflib, cutoff 0.8) — active path when no provider

With NullProvider (the default), step 3 is a no-op and step 4 always handles
ambiguous matches.  Set LLM_PROVIDER=azure_openai (and implement
AzureOpenAIProvider._chat) to enable step 3.
"""
from __future__ import annotations

import logging
import re
from difflib import get_close_matches
from functools import lru_cache
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from app.cache import cache_get, cache_set
from app.config import HTTP_TIMEOUT, USER_AGENT
from app.llm.provider import get_llm_provider

logger = logging.getLogger(__name__)

_REGISTER_URL = (
    "https://www.canada.ca/en/health-canada/services/drugs-health-products"
    "/drug-products/applications-submissions/register-innovative-drugs.html"
)

# Corporate suffixes stripped during manufacturer normalisation
_CORP_SUFFIX_RE = re.compile(
    r"\b(inc|ltd|llc|ulc|corp|corporation|gmbh|limited|canada|"
    r"pharmaceuticals|pharmaceutical|pharma|laboratories|laboratory|labs|"
    r"biotechnology|biosciences|therapeutics|sciences|healthcare|health)\b\.?",
    re.IGNORECASE,
)

# Strength + unit tokens stripped from ingredient names before matching.
_STRENGTH_RE = re.compile(
    r"\b\d+(\.\d+)?\s*(mg\/ml|mg\/l|mcg\/ml|mcg|μg|ug|mmol|meq|mg|ml|iu|g|l)\b",
    re.IGNORECASE,
)


# ── Normalisation ─────────────────────────────────────────────────────────────

# Memoized: both are pure string→string transforms called once per DIN AND once
# per register row per DIN. At full-universe scale (~13.5k DINs × a few-hundred-row
# register) the register strings repeat across every DIN, so caching collapses
# millions of identical normalizations to a few hundred. Behaviour is unchanged.
@lru_cache(maxsize=None)
def _normalize_ingredient_dp(s: str) -> str:
    """Strip strength tokens, parentheticals, and casefold for ingredient matching."""
    s = s.lower().strip()
    s = _STRENGTH_RE.sub("", s)
    s = re.sub(r"\(.*?\)", "", s)
    return re.sub(r"\s+", " ", s).strip()


@lru_cache(maxsize=None)
def _normalize_manufacturer(s: str) -> str:
    """Strip corporate suffixes, punctuation, and casefold for manufacturer matching."""
    s = s.lower().strip()
    s = _CORP_SUFFIX_RE.sub("", s)
    s = re.sub(r"[.,;']", "", s)
    return re.sub(r"\s+", " ", s).strip()


# ── Table parsing ─────────────────────────────────────────────────────────────

def _find_active_table(soup: BeautifulSoup) -> Optional[object]:
    """Locate the 'Products for Human Use - Active Data Protection Period' table.

    Strategy (tried in order):
      1. id="a1" anchor → next <table> sibling
      2. Any heading that contains both "active data protection" AND "human use"
      3. Any heading that contains "active data protection" (ignoring veterinary sections)
      4. Column-header heuristic
    """
    el = soup.find(id="a1")
    if el:
        if el.name == "table":
            return el
        tbl = el.find_next("table")
        if tbl:
            return tbl

    for heading in soup.find_all(["h2", "h3", "h4", "h5"]):
        text = heading.get_text(" ", strip=True).lower()
        if "active data protection" in text and "human use" in text:
            tbl = heading.find_next("table")
            if tbl:
                return tbl

    for heading in soup.find_all(["h2", "h3", "h4", "h5"]):
        text = heading.get_text(" ", strip=True).lower()
        if "active data protection" in text and "veterinar" not in text and "animal" not in text:
            tbl = heading.find_next("table")
            if tbl:
                return tbl

    for tbl in soup.find_all("table"):
        headers = [th.get_text(" ", strip=True).lower() for th in tbl.find_all("th")]
        if any("medicinal" in h for h in headers) and any("data protection" in h for h in headers):
            return tbl

    return None


def _parse_data_protection_table(table) -> list[dict]:
    """Parse table rows → list of dicts with normalised column keys."""
    header_cells = table.find_all("th")
    headers = [h.get_text(" ", strip=True).lower() for h in header_cells]

    col: dict[str, int] = {}
    for i, h in enumerate(headers):
        if "medicinal ingredient" in h and h.startswith("medicinal ingredient"):
            col["medicinal_ingredient"] = i
        elif "submission number" in h:
            col["submission_number"] = i
        elif "innovative drug" in h:
            col["innovative_drug"] = i
        elif "manufacturer" in h:
            col["manufacturer"] = i
        elif "no file" in h or ("6 year" in h and "no" in h):
            col["no_file_date"] = i
        elif "pediatric" in h:
            col["pediatric_extension"] = i
        elif "data protection" in h and ("end" in h or "ends" in h):
            col["data_protection_ends"] = i
        elif "notice of compliance" in h:
            col["noc_date"] = i

    if not col:
        col = {
            "medicinal_ingredient": 0,
            "submission_number": 1,
            "innovative_drug": 2,
            "manufacturer": 3,
            "noc_date": 5,
            "no_file_date": 6,
            "pediatric_extension": 7,
            "data_protection_ends": 8,
        }

    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue

        def _get(key: str) -> str:
            idx = col.get(key)
            return cells[idx].get_text(" ", strip=True) if idx is not None and idx < len(cells) else ""

        entry = {
            "medicinal_ingredient": _get("medicinal_ingredient"),
            "submission_number": _get("submission_number"),
            "innovative_drug": _get("innovative_drug"),
            "manufacturer": _get("manufacturer"),
            "noc_date": _get("noc_date"),
            "no_file_date": _get("no_file_date"),
            "pediatric_extension": _get("pediatric_extension"),
            "data_protection_ends": _get("data_protection_ends"),
        }
        if entry["medicinal_ingredient"]:
            rows.append(entry)

    return rows


# ── Fetch ─────────────────────────────────────────────────────────────────────

async def fetch_data_protection_table() -> list[dict]:
    """Fetch and parse the active data protection register. Cached 24 h."""
    cached = cache_get("data_protection", "active_v2")
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await client.get(
                _REGISTER_URL,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
                timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            html = r.text
    except Exception as exc:
        logger.warning("Data protection register fetch failed: %s", exc)
        return []

    soup = BeautifulSoup(html, "html.parser")
    table = _find_active_table(soup)
    if table is None:
        logger.warning("Data protection active table (#a1) not found in register page")
        return []

    rows = _parse_data_protection_table(table)
    if not rows:
        logger.error(
            "Data protection register: parsed 0 active rows — table selector or "
            "column layout may have changed. NOT caching. URL: %s",
            _REGISTER_URL,
        )
        return []
    logger.info("Data protection register: %d active rows loaded", len(rows))
    print(f"[data_protection] Active row count: {len(rows)}")
    cache_set("data_protection", "active_v2", rows, ttl=60 * 60 * 24)
    return rows


# ── Field extraction ──────────────────────────────────────────────────────────

def _extract_dp_fields(row: dict) -> dict:
    """Extract the 3 output columns from a matched register row.

    Normalises pediatric_extension to "Yes" or "No".
    "N/A", blank, "-", or any unrecognised value → "No".
    """
    ped = row.get("pediatric_extension", "").strip().upper()
    if ped in ("YES", "Y", "1", "OUI"):
        ped = "Yes"
    else:
        ped = "No"

    return {
        "dp_6yr_no_file_date": row.get("no_file_date") or "",
        "pediatric_extension": ped,
        "data_protection_ends": row.get("data_protection_ends") or "",
    }


# ── Deterministic match (always active) ──────────────────────────────────────

def _match_data_protection_deterministic(
    dpd_ingredient: str,
    dpd_company: str,
    dp_table: list[dict],
) -> dict:
    """Ingredient-prefilter → exact manufacturer → fuzzy manufacturer (cutoff 0.8).

    Returns {} when there is no match, or when a match is ambiguous.
    This is the active path when no LLM provider is configured.
    """
    if not dp_table:
        return {}

    ing_norm = _normalize_ingredient_dp(dpd_ingredient or "")
    if not ing_norm:
        return {}

    shortlist = [
        r for r in dp_table
        if ing_norm in _normalize_ingredient_dp(r.get("medicinal_ingredient", ""))
        or _normalize_ingredient_dp(r.get("medicinal_ingredient", "")) in ing_norm
    ]
    if not shortlist:
        return {}

    mfr_norm = _normalize_manufacturer(dpd_company or "")

    exact = [r for r in shortlist if _normalize_manufacturer(r.get("manufacturer", "")) == mfr_norm]
    if len(exact) == 1:
        return _extract_dp_fields(exact[0])
    if len(exact) > 1:
        logger.info(
            "Ambiguous DP match for ingredient=%r company=%r — %d exact hits; leaving blank",
            dpd_ingredient, dpd_company, len(exact),
        )
        return {}

    if not mfr_norm:
        return {}
    mfr_options = [_normalize_manufacturer(r.get("manufacturer", "")) for r in shortlist]
    fuzzy = get_close_matches(mfr_norm, mfr_options, n=1, cutoff=0.8)
    if fuzzy:
        for row in shortlist:
            if _normalize_manufacturer(row.get("manufacturer", "")) == fuzzy[0]:
                return _extract_dp_fields(row)

    return {}


# ── Public async entry point ──────────────────────────────────────────────────

async def match_data_protection(
    dpd_ingredient: str,
    dpd_company: str,
    dp_table: Optional[list[dict]] = None,
) -> dict:
    """Match a DIN's ingredient+company to the active data protection register.

    Returns {dp_6yr_no_file_date, pediatric_extension, data_protection_ends}
    or empty dict when no confident match is found.

    Matching order:
      1. Ingredient prefilter
      2. Exact manufacturer
      3. LLM provider confirmation (optional, NullProvider → skip)
      4. Fuzzy manufacturer fallback (difflib, cutoff 0.8)
    """
    if dp_table is None:
        dp_table = await fetch_data_protection_table()
    if not dp_table:
        return {}

    ing_norm = _normalize_ingredient_dp(dpd_ingredient or "")
    if not ing_norm:
        return {}

    shortlist = [
        r for r in dp_table
        if ing_norm in _normalize_ingredient_dp(r.get("medicinal_ingredient", ""))
        or _normalize_ingredient_dp(r.get("medicinal_ingredient", "")) in ing_norm
    ]
    if not shortlist:
        return {}

    mfr_norm = _normalize_manufacturer(dpd_company or "")

    # Exact manufacturer match
    exact = [r for r in shortlist if _normalize_manufacturer(r.get("manufacturer", "")) == mfr_norm]
    if len(exact) == 1:
        return _extract_dp_fields(exact[0])
    if len(exact) > 1:
        logger.info("Ambiguous DP match for %r / %r; leaving blank", dpd_ingredient, dpd_company)
        return {}

    # LLM provider confirmation (no-op with NullProvider)
    provider = get_llm_provider()
    llm_row = await provider.confirm_innovative_drug_match(ing_norm, dpd_company, shortlist)
    if llm_row is not None:
        return _extract_dp_fields(llm_row)

    # Deterministic fuzzy fallback (always active)
    return _match_data_protection_deterministic(dpd_ingredient, dpd_company, dp_table)
