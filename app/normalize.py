"""
Ingredient name normalization and synonym expansion.

Primary path: static synonym map (always active, no network required).
Optional add-on: configured LLM provider via app/llm/provider.py.
  - NullProvider (default): only static map is used.
  - AzureOpenAIProvider: extends static results with LLM expansions.
"""
from __future__ import annotations

from app.cache import cache_get, cache_set
from app.llm.provider import get_llm_provider

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
    "dupilumab": ["dupixent"],
    "nivolumab": ["opdivo"],
    "pembrolizumab": ["keytruda"],
    "abrocitinib": ["cibinqo"],
    "apremilast": ["otezla"],
    "osimertinib": ["tagrisso"],
    "venetoclax": ["venclexta"],
    "sacubitril": ["entresto"],
    "lecanemab": ["leqembi"],
    "alpelisib": ["piqray"],
    "linagliptin": ["trajenta"],
}


def _static_synonyms(term: str) -> list[str]:
    key = term.strip().lower()
    return _STATIC_SYNONYMS.get(key, [])


async def normalize_ingredient(term: str) -> tuple[str, list[str]]:
    """
    Return (canonical_term, list_of_extra_search_terms).
    canonical_term is what we searched for (unchanged unless we detect a known synonym).
    extra_terms are additional terms to search for in parallel.

    Static map always runs. LLM provider expansion is an optional add-on:
    with NullProvider (the default) only the static map is used.
    """
    term = term.strip()
    key = term.lower()

    # Fast path: cached synonym list from a prior run
    cached = cache_get("normalize_synonyms", key)
    if cached is not None:
        return term, cached

    # 1. Static map (always active)
    static = _static_synonyms(key)

    # 2. Optional LLM provider expansion (no-op with NullProvider)
    provider = get_llm_provider()
    llm_terms = await provider.expand_synonyms(term)

    # Combine and deduplicate
    all_extras = list({t.lower() for t in (static + llm_terms)} - {key})

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
