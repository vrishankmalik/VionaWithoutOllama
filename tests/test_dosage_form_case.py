"""Guards for dosage-form case canonicalization at the product-key boundary.

The two DPD sources format dosage forms with different case — the bulk extract
yields ``TABLET`` while the live REST API yields ``Tablet``. The competitor
product key (``screen._dosage_form_key``) canonicalizes case (upper, modifiers
preserved) so case-equivalent forms collapse into ONE product instead of
splitting and under-counting competitors — the same failure class as the result
cap, by a different mechanism.

These tests pin both halves of the fix:
  1. case-equivalent forms collapse into one product (and release-type modifiers
     still keep ER distinct);
  2. the dosage-form dropdown's base→raw map and filter still match BOTH case
     variants, so canonicalizing the key did not desync the filter.
"""
from __future__ import annotations

import pandas as pd

from app.enrichment.screen import (
    apply_dosage_form_filter,
    build_dosage_form_map,
    compute_products,
)


def test_case_equivalent_forms_collapse_to_one_product():
    """A bulk-sourced ``TABLET`` row and a REST-sourced ``Tablet`` row for the
    same ingredient land in ONE product with both companies counted once — not
    two products of one competitor each. The ER modifier stays a separate product.
    """
    s1 = pd.DataFrame([
        {"din": "1", "ingredient": "METFORMIN HYDROCHLORIDE", "dosage_form": "TABLET",
         "company": "APOTEX INC", "status": "marketed"},            # bulk extract (UPPER)
        {"din": "2", "ingredient": "METFORMIN HYDROCHLORIDE", "dosage_form": "Tablet",
         "company": "TEVA CANADA LIMITED", "status": "marketed"},   # REST API (Title)
        {"din": "3", "ingredient": "METFORMIN HYDROCHLORIDE",
         "dosage_form": "Tablet (Extended-Release)",
         "company": "APOTEX INC", "status": "marketed"},            # modifier → distinct
    ])
    products, _ = compute_products(s1, pd.DataFrame())

    tablet = products[products["dosage_form"] == "TABLET"]
    assert len(tablet) == 1, "TABLET and Tablet must collapse into ONE product"
    assert tablet.iloc[0]["competitors"] == 2, (
        "both companies must be counted once in the single product, not split "
        "across a TABLET and a Tablet product (1 each)"
    )

    forms = set(products["dosage_form"])
    assert forms == {"TABLET", "TABLET (EXTENDED-RELEASE)"}, (
        "case folds, but the release-type modifier must remain a distinct product"
    )


def test_dropdown_base_matches_both_case_variants():
    """The dropdown base→raw map and the filter both canonicalize via
    ``base_dosage_form``, so selecting the base form still matches BOTH the
    ``TABLET`` and ``Tablet`` raw variants (and the ER variant) — the key fix did
    not desync the filter from the dropdown.
    """
    raws = ["TABLET", "Tablet", "Tablet (Extended-Release)", "Capsule"]

    base_map = build_dosage_form_map(raws)
    assert "TABLET" in base_map
    assert set(base_map["TABLET"]) == {"TABLET", "Tablet", "Tablet (Extended-Release)"}

    df = pd.DataFrame({"dosage_form": raws})
    kept = apply_dosage_form_filter(df, ["TABLET"])
    assert set(kept["dosage_form"]) == {"TABLET", "Tablet", "Tablet (Extended-Release)"}, (
        "selecting base TABLET must match both case variants and the ER form, "
        "but not Capsule"
    )
