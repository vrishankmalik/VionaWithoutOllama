"""Regression tests for the DPD result-cap bug and its blind spots.

Background — the bug these guard against
----------------------------------------
``search_dpd`` capped results at ``DPD_MAX_RESULTS`` (150) by slicing a *set*:

    codes_to_fetch = list(all_drug_codes)[:DPD_MAX_RESULTS]   # <- set, arbitrary order

Two defects compounded:
  1. The export/enrichment path (which computes competitor / approval counts)
     inherited the interactive 150 cap, so any ingredient with >150 DINs
     (metformin = 242) had ~38% of its products silently dropped.
  2. Slicing a ``set`` keeps an ARBITRARY subset, so the dropped products varied
     run-to-run — competitor counts were not even reproducible.

Why the old suite missed it
---------------------------
  * Every completeness test (test_tier4 acetaminophen, the reconciliation suite)
    monkeypatched DPD_MAX_RESULTS up to 5000/9999 BEFORE running — they tested
    "complete if the cap is raised", never the production default.
  * The determinism test (test_cache_determinism) used a mock fixture SMALLER
    than the cap, so set-slicing never triggered.
  * No test fed real search volume into the competitor screen, and none checked
    counts against the bulk-extract ground truth.

These tests close all three gaps. The offline ones run by default and would have
caught the bug at the unit level; the integration ones (opt-in via
``-m integration``) cross-check the live export path against the authoritative
DPD bulk extract and surface any OTHER molecule where the two diverge.
"""
from __future__ import annotations

import random

import pandas as pd
import pytest

import app.sources.dpd as dpd
from app.config import DPD_EXPORT_MAX_RESULTS
from app.models import DrugRecord, SearchMetadata, SearchResponse
from app.sources.dpd import search_dpd


# ── Offline harness: drive search_dpd with a synthetic code list ──────────────

@pytest.fixture
def no_cache_dpd(monkeypatch):
    noop = lambda *a, **k: None
    monkeypatch.setattr(dpd, "cache_get", noop)
    monkeypatch.setattr(dpd, "cache_set", noop)


async def _run_search(monkeypatch, codes, **kwargs):
    """Run search_dpd over an exact set of drug codes, capturing which codes the
    per-code builder was actually asked to fetch (i.e. which survived the cap)."""
    fetched: list[int] = []

    async def fake_codes(client, ingredient):
        return [{"drug_code": c, "ingredient_name": "TESTMOL",
                 "strength": "10", "strength_unit": "MG"} for c in codes]

    async def fake_build(client, sem, drug_code, ingredient_rows):
        fetched.append(drug_code)
        return DrugRecord(
            source="DPD", din=str(drug_code).zfill(8), ingredient="TESTMOL",
            dosage_form="Tablet", company=f"CO{drug_code}", status="Marketed",
        )

    monkeypatch.setattr(dpd, "_fetch_drug_codes_by_ingredient", fake_codes)
    monkeypatch.setattr(dpd, "_build_record_for_code", fake_build)
    result = await search_dpd("testmol", field="ingredient", **kwargs)
    return result, fetched


# ── Gap 1: the cap, when it applies, must keep a DETERMINISTIC subset ──────────

async def test_capped_subset_is_deterministic_and_sorted(no_cache_dpd, monkeypatch):
    """With >cap codes, the kept subset must be the SAME every run (and be the
    deterministic sorted-smallest-N), not an arbitrary slice of a set.

    Fails on the old ``list(set)[:cap]`` implementation, which kept a hash-order
    subset that was neither sorted nor stable.
    """
    monkeypatch.setattr(dpd, "DPD_MAX_RESULTS", 50)
    codes = list(range(90_000, 90_300))           # 300 distinct codes
    random.Random(1).shuffle(codes)               # arbitrary insertion order

    res1, kept1 = await _run_search(monkeypatch, list(codes))
    res2, kept2 = await _run_search(monkeypatch, list(reversed(codes)))

    # Same subset regardless of insertion order, both runs.
    assert sorted(kept1) == sorted(kept2), "capped subset is not reproducible"
    # And it is specifically the deterministic sorted-smallest-N.
    assert sorted(kept1) == sorted(set(codes))[:50]
    # Truncation is still signalled to callers.
    assert res1.total_matches == 300
    assert res1.count == 50


# ── Gap 2: the export path must be able to bypass the interactive cap ──────────

async def test_export_max_results_bypasses_default_cap(no_cache_dpd, monkeypatch):
    """search_dpd(max_results=...) overrides DPD_MAX_RESULTS so the export path
    fetches every product, while the interactive default stays capped."""
    monkeypatch.setattr(dpd, "DPD_MAX_RESULTS", 50)
    codes = list(range(90_000, 90_300))           # 300 codes

    capped, kept_capped = await _run_search(monkeypatch, list(codes))
    full, kept_full = await _run_search(monkeypatch, list(codes), max_results=5000)

    assert len(kept_capped) == 50
    assert capped.total_matches == 300            # capped → exposed
    assert len(kept_full) == 300                  # uncapped → complete
    assert full.total_matches is None             # not capped → no truncation flag


async def test_default_cap_still_applies_without_override(no_cache_dpd, monkeypatch):
    """Sanity: omitting max_results keeps the interactive cap (no accidental
    global uncapping that would hammer the API on broad UI searches)."""
    monkeypatch.setattr(dpd, "DPD_MAX_RESULTS", 25)
    codes = list(range(70_000, 70_120))           # 120 codes
    res, kept = await _run_search(monkeypatch, list(codes))
    assert len(kept) == 25
    assert res.total_matches == 120


# ════════════════════════════════════════════════════════════════════════════
# Integration: cross-check the live export path against the DPD bulk extract.
# These find OTHER molecules/forms where the two pipelines disagree.
# ════════════════════════════════════════════════════════════════════════════

# The user's screening panel + a couple of high-volume controls.
_PANEL = ["amlodipine", "metformin", "valsartan", "tenofovir", "hydrochlorothiazide"]

# Bulk extract is up to ~24h staler than the live REST API, so a 1-company
# difference per product is acceptable jitter. A larger gap is a real defect
# (the cap dropped 5 marketed competitors off metformin ER, far past this).
_FRESHNESS_TOL = 1


def _competitor_map(sheet1_df: pd.DataFrame) -> dict[tuple, int]:
    """(ingredient, dosage_form) → competitor count, via the real screen logic.

    The product key is compared as-is across the two DPD pipelines: the screen's
    ``_dosage_form_key`` canonicalizes case, so the bulk extract's ``TABLET`` and
    the live REST API's ``Tablet`` collapse to the same key. No case-folding
    workaround here — if the keys didn't genuinely match, the parity check below
    would (correctly) fail.
    """
    from app.enrichment.screen import compute_products
    products, _ = compute_products(sheet1_df, pd.DataFrame())
    return {
        (r["ingredient"], r["dosage_form"]): int(r["competitors"])
        for _, r in products.iterrows()
    }


async def _export_sheet1(molecule: str) -> pd.DataFrame:
    from app.enrichment.workbook import build_sheet1
    res = await search_dpd(molecule, field="ingredient", max_results=DPD_EXPORT_MAX_RESULTS)
    assert res.status == "ok", f"{molecule}: DPD search {res.status} ({res.error_message})"
    assert res.total_matches is None, (
        f"{molecule}: export search was CAPPED (total_matches={res.total_matches}); "
        f"the export path must be uncapped."
    )
    resp = SearchResponse(
        metadata=SearchMetadata(query=molecule, field="ingredient",
                                timestamp="t", normalized_terms=[molecule]),
        sources=[res],
    )
    return build_sheet1(resp, ingredient_name=molecule, include_dpd_only=True)


async def _bulk_sheet1_for(molecule: str) -> pd.DataFrame:
    from app.enrichment.universe import get_universe, build_universe_response, build_universe_sheet1
    bundle = await get_universe()
    resp = build_universe_response(bundle)
    sheet1, _, _ = build_universe_sheet1(resp, dp_table=bundle.dp_table)
    mask = sheet1["ingredient"].astype(str).str.upper().str.contains(molecule.upper())
    return sheet1[mask].copy()


@pytest.mark.integration
async def test_metformin_er_competitor_count_is_complete(no_cache):
    """The exact regression: metformin extended-release tablet must surface its
    full marketed-competitor count (7 in DPD) and clear a 'competitors > 2'
    screen. Before the fix the cap left it at 2 and it was silently excluded.

    ``no_cache`` forces a fully-live fetch — a completeness check must not read
    the shared on-disk HTTP cache, which a running app on older code could have
    populated with capped/partial results.
    """
    export = _competitor_map(await _export_sheet1("metformin"))
    er = {k: v for k, v in export.items()
          if "METFORMIN" in k[0].upper() and "+" not in k[0]
          and ("EXTEND" in k[1].upper() or "RELEASE" in k[1].upper())}
    assert er, "metformin extended-release product missing from export entirely"
    (key, competitors), = er.items()
    assert competitors > 2, (
        f"metformin ER competitors={competitors} — should be >2 (cap regression). {key}"
    )


@pytest.mark.integration
@pytest.mark.parametrize("molecule", _PANEL)
async def test_export_path_not_capped(no_cache, molecule):
    """Every panel molecule must come back UNCAPPED through the export path —
    a direct guard on the production default that the old suite never tested.
    ``no_cache`` → live fetch, immune to a shared cache populated by older code."""
    res = await search_dpd(molecule, field="ingredient", max_results=DPD_EXPORT_MAX_RESULTS)
    assert res.status == "ok", res.error_message
    assert res.total_matches is None, (
        f"{molecule}: export search capped at {res.count} of {res.total_matches}."
    )


@pytest.mark.integration
async def test_export_vs_bulk_extract_competitor_parity(no_cache):
    """Cross-check live export competitor counts against the authoritative bulk
    extract for the whole panel. The export must not UNDER-count any shared
    product beyond freshness jitter. Divergences are printed so this doubles as
    a discovery probe for problems beyond metformin.

    ``no_cache`` → fully-live export fetches, immune to a shared on-disk cache
    that older/capped code could have polluted.
    """
    violations: list[str] = []
    notes: list[str] = []

    for molecule in _PANEL:
        export = _competitor_map(await _export_sheet1(molecule))
        bulk = _competitor_map(await _bulk_sheet1_for(molecule))

        shared = set(export) & set(bulk)
        for key in sorted(shared):
            e, b = export[key], bulk[key]
            if b - e > _FRESHNESS_TOL:
                violations.append(
                    f"UNDERCOUNT {molecule}: {key} export={e} bulk={b} (gap {b - e})"
                )
            elif e != b:
                notes.append(f"  ~ {molecule}: {key} export={e} bulk={b}")

        only_bulk = sorted(set(bulk) - set(export))
        only_exp = sorted(set(export) - set(bulk))
        if only_bulk:
            notes.append(f"  bulk-only products for {molecule}: {len(only_bulk)} "
                         f"(e.g. {only_bulk[:3]})")
        if only_exp:
            notes.append(f"  export-only products for {molecule}: {len(only_exp)} "
                         f"(e.g. {only_exp[:3]})")

    if notes:
        print("\nParity notes (within tolerance / informational):")
        print("\n".join(notes))

    assert not violations, "Export under-counts competitors vs bulk extract:\n" + "\n".join(violations)
