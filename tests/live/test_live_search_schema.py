"""Part 1 — Live end-to-end: real DPD/NOC search + Sheet-1 schema, hand-verified.

Anchor (verified live 2026-06-25): the ingredient ``abrocitinib`` resolves to the
single brand CIBINQO, marketed in Canada as exactly three strengths / three DINs:

    02528363  CIBINQO  ABROCITINIB  50 MG
    02528371  CIBINQO  ABROCITINIB  100 MG
    02528398  CIBINQO  ABROCITINIB  200 MG

These tests prove the live pipeline returns those three DINs and ONLY those three
(zero contamination), and that the export Sheet-1 schema is intact end to end.
"""
from __future__ import annotations

import pytest

from app.enrichment.workbook import build_sheet1

pytestmark = pytest.mark.integration

# Hand-verified CIBINQO/abrocitinib DIN set (DPD, live).
_CIBINQO_DINS = {"02528363", "02528371", "02528398"}

# Documented Sheet-1 core columns that must survive end to end (CLAUDE.md).
_REQUIRED_SHEET1_COLS = {
    "din", "ingredient", "brand_name", "company", "strength", "route", "status",
    "dosage_form", "_drug_code",
    "noc_date", "submission_class", "noc_submission_type", "noc_therapeutic_class",
}


def _dpd_source(resp):
    return next((s for s in resp.sources if s.source == "DPD"), None)


def test_abrocitinib_dpd_returns_exactly_three_cibinqo_dins(live_search_abrocitinib):
    """DPD returns the three CIBINQO DINs and nothing else — no contamination."""
    dpd = _dpd_source(live_search_abrocitinib)
    assert dpd is not None and dpd.status == "ok", (
        f"DPD source missing/failed: {getattr(dpd, 'status', None)}"
    )
    dins = {r.din for r in dpd.records if r.din}
    assert dins == _CIBINQO_DINS, (
        f"DPD DIN set drifted from the hand-verified CIBINQO anchor: got {sorted(dins)}"
    )
    # Every returned product really is CIBINQO/abrocitinib (no unrelated drug leaked).
    for r in dpd.records:
        assert "CIBINQO" in (r.brand_name or "").upper(), f"non-CIBINQO brand: {r.brand_name!r}"
        assert "ABROCITINIB" in (r.ingredient or "").upper(), f"non-abrocitinib: {r.ingredient!r}"


def test_abrocitinib_no_cross_source_din_contamination(live_search_abrocitinib):
    """No source emits a DIN outside the verified CIBINQO set.

    NOC may legitimately return several submission rows for the same product, but
    every DIN it attaches must still be one of the three CIBINQO DINs.
    """
    for s in live_search_abrocitinib.sources:
        stray = {r.din for r in s.records if r.din} - _CIBINQO_DINS
        assert not stray, f"{s.source} leaked non-CIBINQO DIN(s): {sorted(stray)}"


def test_abrocitinib_sheet1_schema_and_rows(live_search_abrocitinib):
    """The export Sheet-1 builds from the live result with the documented schema."""
    df = build_sheet1(live_search_abrocitinib, dp_table=None, ingredient_name="abrocitinib")
    assert "din" in df.columns, df.columns.tolist()
    missing = _REQUIRED_SHEET1_COLS - set(df.columns)
    assert not missing, f"Sheet-1 lost required columns: {sorted(missing)}"

    # One row per DIN, exactly the three CIBINQO DINs, sorted ascending (DIN order).
    dins = df["din"].astype(str).tolist()
    assert set(dins) == _CIBINQO_DINS, dins
    assert len(dins) == 3, f"expected one row per DIN, got {len(dins)}"
    assert dins == sorted(dins), f"Sheet-1 must be DIN-sorted ascending: {dins}"

    # NOC actually joined (CIBINQO is post-NOC), so noc_date is populated, not blank.
    noc_dates = [v for v in df["noc_date"].tolist() if v]
    assert noc_dates, "CIBINQO is a post-NOC product — noc_date should be populated"
