"""Tests that the Sheet 1 DIN universe is sourced from NOC, not from DPD.

Rule: build_sheet1 includes only DINs that appear in NOC results.
DPD records for the same ingredient may carry additional DINs that are
loosely matched (e.g. icosapent → 6 fish-oil products in DPD) — those are
excluded and surfaced in build_exclusion_list instead.
"""
from __future__ import annotations

import pytest

from app.models import DrugRecord, SearchMetadata, SearchResponse, SourceResult


# ── helpers ──────────────────────────────────────────────────────────────────

def _meta(q: str = "test") -> SearchMetadata:
    return SearchMetadata(
        query=q,
        field="ingredient",
        timestamp="2026-01-01T00:00:00+00:00",
        normalized_terms=[q],
        per_source_status={"DPD": "ok", "NOC": "ok"},
    )


def _dpd(din: str, brand: str = "BRAND", ingredient: str = "TEST ING", company: str = "CO") -> DrugRecord:
    return DrugRecord(
        source="DPD", din=din, brand_name=brand, company=company,
        ingredient=ingredient, strength="100 mg", dosage_form="Tablet",
        route="Oral", status="Marketed",
        source_specific={"drug_code": 99001},
    )


def _noc(din: str, brand: str = "BRAND", sub_type: str = "NDS") -> DrugRecord:
    return DrugRecord(
        source="NOC", din=din, brand_name=brand,
        source_specific={
            "noc_date": "2020-01-01",
            "submission_type": sub_type,
            "submission_class": "New",
            "reason_for_supplement": None,
            "therapeutic_class": "Test",
        },
    )


def _noc_no_din(brand: str = "BRAND") -> DrugRecord:
    """NOC record with no DIN attached."""
    return DrugRecord(
        source="NOC", din=None, brand_name=brand,
        source_specific={
            "noc_date": "2020-01-01",
            "submission_type": "NDS",
            "submission_class": "New",
            "reason_for_supplement": None,
            "therapeutic_class": "Test",
        },
    )


def _response(dpd_records=(), noc_records=(), query="test") -> SearchResponse:
    sources = [
        SourceResult(source="DPD", status="ok", records=list(dpd_records)),
        SourceResult(source="NOC", status="ok", records=list(noc_records)),
    ]
    return SearchResponse(metadata=_meta(query), sources=sources)


# ── DIN universe tests ────────────────────────────────────────────────────────

def test_noc_din_is_included(tmp_path):
    """A DIN present in NOC appears in Sheet 1."""
    import app.enrichment.store as store_mod
    store_mod.reset_for_testing(str(tmp_path / "enrich.db"))

    from app.enrichment.workbook import build_sheet1

    resp = _response(
        dpd_records=[_dpd("02495244")],
        noc_records=[_noc("02495244")],
    )
    df = build_sheet1(resp)
    assert "02495244" in df["din"].values


def test_dpd_only_din_excluded(tmp_path):
    """A DIN present in DPD but absent from NOC is NOT in Sheet 1."""
    import app.enrichment.store as store_mod
    store_mod.reset_for_testing(str(tmp_path / "enrich.db"))

    from app.enrichment.workbook import build_sheet1

    resp = _response(
        dpd_records=[_dpd("02495244"), _dpd("02248423", ingredient="EICOSAPENTAENOIC ACID 119 MG")],
        noc_records=[_noc("02495244")],
    )
    df = build_sheet1(resp)
    assert "02495244" in df["din"].values
    assert "02248423" not in df["din"].values, "DPD-only DIN must not appear in sheet"


def test_zero_noc_dins_empty_sheet(tmp_path):
    """When NOC returns no DINs, Sheet 1 is empty."""
    import app.enrichment.store as store_mod
    store_mod.reset_for_testing(str(tmp_path / "enrich.db"))

    from app.enrichment.workbook import build_sheet1

    resp = _response(
        dpd_records=[_dpd("02248423")],
        noc_records=[],  # no NOC results at all
    )
    df = build_sheet1(resp)
    assert df.empty, "Sheet 1 must be empty when NOC has no DINs"


def test_multi_din_noc_approval_all_included(tmp_path):
    """When a single NOC approval covers multiple DINs, all are included."""
    import app.enrichment.store as store_mod
    store_mod.reset_for_testing(str(tmp_path / "enrich.db"))

    from app.enrichment.workbook import build_sheet1

    resp = _response(
        dpd_records=[_dpd("02000001"), _dpd("02000002"), _dpd("02000003")],
        noc_records=[_noc("02000001"), _noc("02000002"), _noc("02000003")],
    )
    df = build_sheet1(resp)
    assert set(df["din"].values) == {"02000001", "02000002", "02000003"}


def test_noc_entry_missing_din_not_in_sheet(tmp_path):
    """An NOC record with no DIN cannot produce a Sheet 1 row."""
    import app.enrichment.store as store_mod
    store_mod.reset_for_testing(str(tmp_path / "enrich.db"))

    from app.enrichment.workbook import build_sheet1

    resp = _response(
        dpd_records=[_dpd("02495244")],
        noc_records=[_noc("02495244"), _noc_no_din("VASCEPA")],
    )
    df = build_sheet1(resp)
    # Only the DIN-carrying NOC record appears; the no-DIN entry is not a row.
    assert list(df["din"].values) == ["02495244"]


def test_noc_entry_missing_din_logged(tmp_path, caplog):
    """An NOC record with no DIN emits a warning log."""
    import logging
    import app.enrichment.store as store_mod
    store_mod.reset_for_testing(str(tmp_path / "enrich.db"))

    from app.enrichment.workbook import build_sheet1

    resp = _response(
        dpd_records=[],
        noc_records=[_noc_no_din("VASCEPA")],
    )
    with caplog.at_level(logging.WARNING, logger="app.enrichment.workbook"):
        build_sheet1(resp)

    assert any("no DIN" in msg for msg in caplog.messages), (
        "Expected a warning about the NOC entry missing a DIN"
    )


# ── Exclusion list tests ──────────────────────────────────────────────────────

def test_exclusion_list_contains_dpd_only_dins(tmp_path):
    """build_exclusion_list returns the 6 DPD-only DINs for icosapent (mocked)."""
    import app.enrichment.store as store_mod
    store_mod.reset_for_testing(str(tmp_path / "enrich.db"))

    from app.enrichment.workbook import build_exclusion_list

    dpd_dins = ["02495244", "02248423", "02336014", "02047497", "02242996", "02248743", "02248744"]
    resp = _response(
        dpd_records=[_dpd(d, ingredient=f"FISH OIL {d}") for d in dpd_dins],
        noc_records=[_noc("02495244")],  # only 1 legitimate NOC DIN
        query="icosapent",
    )
    df = build_exclusion_list(resp, ingredient_name="icosapent")
    assert set(df["din"].values) == set(dpd_dins) - {"02495244"}, (
        "Exclusion list must contain exactly the 6 non-NOC DINs"
    )
    assert len(df) == 6
    for _, row in df.iterrows():
        assert "icosapent" in row["reason"]


def test_exclusion_list_empty_when_all_dins_in_noc(tmp_path):
    """build_exclusion_list is empty when every DPD DIN is also in NOC."""
    import app.enrichment.store as store_mod
    store_mod.reset_for_testing(str(tmp_path / "enrich.db"))

    from app.enrichment.workbook import build_exclusion_list

    resp = _response(
        dpd_records=[_dpd("02000001"), _dpd("02000002")],
        noc_records=[_noc("02000001"), _noc("02000002")],
    )
    df = build_exclusion_list(resp)
    assert df.empty


def test_exclusion_list_columns():
    """Exclusion list has the required columns."""
    from app.enrichment.workbook import build_exclusion_list

    resp = _response(
        dpd_records=[_dpd("02248423", brand="HERBALIFELINE", company="HERBALIFE")],
        noc_records=[],
    )
    df = build_exclusion_list(resp, ingredient_name="icosapent")
    assert list(df.columns) == ["din", "brand_name", "company", "ingredient", "reason"]
    assert df.iloc[0]["din"] == "02248423"
    assert df.iloc[0]["brand_name"] == "HERBALIFELINE"
    assert df.iloc[0]["company"] == "HERBALIFE"


# ── Icosapent regression ──────────────────────────────────────────────────────

_ICOSAPENT_LEGITIMATE_DIN = "02495244"
_ICOSAPENT_EXCLUDED_DINS = {
    "02248423",  # HERBALIFELINE
    "02336014",  # ALLERDERM EFA-Z PLUS
    "02047497",  # PRECIOUS PETS OIL SUPPLEMENT
    "02242996",  # FLEX 500
    "02248743",  # OMEGA-3
    "02248744",  # OMEGA-3 FACTORS WITH VITAMIN E
}


def test_icosapent_regression(tmp_path):
    """Icosapent: exactly 1 NOC DIN in sheet, 6 DPD-only DINs in exclusion list."""
    import app.enrichment.store as store_mod
    store_mod.reset_for_testing(str(tmp_path / "enrich.db"))

    from app.enrichment.workbook import build_sheet1, build_exclusion_list

    all_dpd_dins = {_ICOSAPENT_LEGITIMATE_DIN} | _ICOSAPENT_EXCLUDED_DINS
    resp = _response(
        dpd_records=[_dpd(d) for d in sorted(all_dpd_dins)],
        noc_records=[_noc(_ICOSAPENT_LEGITIMATE_DIN)],
        query="icosapent",
    )

    sheet1 = build_sheet1(resp)
    assert list(sheet1["din"].values) == [_ICOSAPENT_LEGITIMATE_DIN], (
        f"Sheet 1 must contain exactly 1 DIN, got: {list(sheet1['din'].values)}"
    )

    excl = build_exclusion_list(resp, ingredient_name="icosapent")
    assert set(excl["din"].values) == _ICOSAPENT_EXCLUDED_DINS, (
        f"Exclusion list must contain exactly the 6 loose DINs, got: {set(excl['din'].values)}"
    )
