"""
Ingredient name normalization and synonym expansion.
Uses a static map as the primary/fallback, with optional llama3 via Ollama
for more nuanced synonym expansion when Ollama is running.
"""
from __future__ import annotations

import json
import re
from typing import Optional

import httpx

from app.cache import cache_get, cache_set
from app.config import CACHE_TTL, OLLAMA_BASE_URL, OLLAMA_MODEL

# Static synonym map — canonical form → list of synonyms (and vice versa)
_STATIC_SYNONYMS: dict[str, list[str]] = {
    "acetaminophen": ["paracetamol", "apap", "tylenol"],
    "paracetamol": ["acetaminophen", "apap"],
    "ibuprofen": ["advil", "motrin"],
    "metformin": ["metformin hydrochloride", "glucophage"],
    "metformin hydrochloride": ["metformin"],
    "metoprolol": ["metoprolol tartrate", "metoprolol succinate", "lopressor", "toprol"],
    "metoprolol tartrate": ["metoprolol"],
    "metoprolol succinate": ["metoprolol"],
    "amlodipine": ["amlodipine besylate", "norvasc"],
    "amlodipine besylate": ["amlodipine"],
    "atorvastatin": ["atorvastatin calcium", "lipitor"],
    "atorvastatin calcium": ["atorvastatin"],
    "rosuvastatin": ["rosuvastatin calcium", "crestor"],
    "rosuvastatin calcium": ["rosuvastatin"],
    "lisinopril": ["prinivil", "zestril"],
    "omeprazole": ["prilosec", "losec"],
    "esomeprazole": ["esomeprazole magnesium", "nexium"],
    "esomeprazole magnesium": ["esomeprazole"],
    "pantoprazole": ["pantoprazole sodium", "pantoloc", "tecta"],
    "pantoprazole sodium": ["pantoprazole"],
    "fluticasone": ["fluticasone propionate", "fluticasone furoate", "flonase", "advair"],
    "fluticasone propionate": ["fluticasone"],
    "fluticasone furoate": ["fluticasone"],
    "albuterol": ["salbutamol", "ventolin"],
    "salbutamol": ["albuterol"],
    "levothyroxine": ["levothyroxine sodium", "synthroid", "eltroxin"],
    "levothyroxine sodium": ["levothyroxine"],
    "sertraline": ["sertraline hydrochloride", "zoloft"],
    "sertraline hydrochloride": ["sertraline"],
    "escitalopram": ["escitalopram oxalate", "cipralex", "lexapro"],
    "escitalopram oxalate": ["escitalopram"],
    "bupropion": ["bupropion hydrochloride", "wellbutrin", "zyban"],
    "bupropion hydrochloride": ["bupropion"],
    "citalopram": ["citalopram hydrobromide", "celexa"],
    "citalopram hydrobromide": ["citalopram"],
    "gabapentin": ["neurontin"],
    "pregabalin": ["lyrica"],
    "duloxetine": ["duloxetine hydrochloride", "cymbalta"],
    "duloxetine hydrochloride": ["duloxetine"],
    "quetiapine": ["quetiapine fumarate", "seroquel"],
    "quetiapine fumarate": ["quetiapine"],
    "olanzapine": ["zyprexa"],
    "aripiprazole": ["abilify"],
    "risperidone": ["risperdal"],
    "canagliflozin": ["invokana"],
    "empagliflozin": ["jardiance"],
    "dapagliflozin": ["farxiga", "forxiga"],
    "sitagliptin": ["sitagliptin phosphate", "januvia"],
    "sitagliptin phosphate": ["sitagliptin"],
    "liraglutide": ["victoza", "saxenda"],
    "semaglutide": ["ozempic", "wegovy", "rybelsus"],
    "insulin glargine": ["lantus", "basaglar", "toujeo"],
    "insulin aspart": ["novorapid", "novolog"],
    "warfarin": ["warfarin sodium", "coumadin"],
    "warfarin sodium": ["warfarin"],
    "rivaroxaban": ["xarelto"],
    "apixaban": ["eliquis"],
    "dabigatran": ["dabigatran etexilate", "pradaxa"],
    "dabigatran etexilate": ["dabigatran"],
    "amoxicillin": ["amoxil", "trimox"],
    "azithromycin": ["zithromax", "z-pak"],
    "ciprofloxacin": ["ciprofloxacin hydrochloride", "cipro"],
    "ciprofloxacin hydrochloride": ["ciprofloxacin"],
    "tadalafil": ["cialis", "adcirca"],
    "sildenafil": ["sildenafil citrate", "viagra", "revatio"],
    "sildenafil citrate": ["sildenafil"],
    "finasteride": ["propecia", "proscar"],
    "tamsulosin": ["tamsulosin hydrochloride", "flomax"],
    "tamsulosin hydrochloride": ["tamsulosin"],
    "ondansetron": ["ondansetron hydrochloride", "zofran"],
    "ondansetron hydrochloride": ["ondansetron"],
    "methotrexate": ["rheumatrex", "trexall"],
    "adalimumab": ["humira"],
    "trastuzumab": ["herceptin"],
}


def _static_synonyms(term: str) -> list[str]:
    key = term.strip().lower()
    return _STATIC_SYNONYMS.get(key, [])


async def _ollama_synonyms(term: str) -> list[str]:
    """Ask llama3 for synonym/salt-form expansions. Returns [] on any failure."""
    prompt = (
        f"List all common synonyms, salt forms, and brand names for the drug ingredient "
        f"'{term}'. Return ONLY a JSON array of strings, nothing else. Example: "
        f'["synonym1", "salt form", "brand name"]. If none, return [].'
    )
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
                timeout=15.0,
            )
            r.raise_for_status()
            data = r.json()
            text = data.get("response", "").strip()
            # Extract JSON array from response
            m = re.search(r"\[.*?\]", text, re.DOTALL)
            if m:
                candidates = json.loads(m.group(0))
                return [c.strip() for c in candidates if isinstance(c, str) and c.strip()]
    except Exception:
        pass
    return []


async def normalize_ingredient(term: str) -> tuple[str, list[str]]:
    """
    Return (canonical_term, list_of_extra_search_terms).
    canonical_term is what we searched for (unchanged unless we detect a known synonym).
    extra_terms are additional terms to search for in parallel.
    """
    term = term.strip()
    key = term.lower()

    # Fast path: if we have a cached synonym list (including any Ollama results from a
    # prior run), return immediately — avoids a 10-15s Ollama round-trip on every export.
    cached = cache_get("normalize_synonyms", key)
    if cached is not None:
        return term, cached

    # 1. Check static map
    static = _static_synonyms(key)

    # 2. Try Ollama (fire and forget — if it fails, use static only)
    ollama_terms = await _ollama_synonyms(term)

    # Combine and deduplicate
    all_extras = list({t.lower() for t in (static + ollama_terms)} - {key})

    # Cache for the standard TTL.  An empty list is also valid (no synonyms found).
    cache_set("normalize_synonyms", key, all_extras)
    return term, all_extras


async def normalize_query(query: str, field: str) -> tuple[str, list[str]]:
    """
    Normalize a search query. Only meaningful for ingredient searches.
    Returns (query, extra_terms).
    """
    if field != "ingredient":
        return query, []
    return await normalize_ingredient(query)
