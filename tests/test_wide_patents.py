"""Tests for patent aggregation (latest-expiry collapse) and Patent.zip parsing."""
from __future__ import annotations

import io
import zipfile


# ── _split_merged_patent_number ───────────────────────────────────────────────

def test_split_merged_14_digit_token():
    from app.enrichment.patents import _split_merged_patent_number
    result = _split_merged_patent_number("26458103022097")
    assert result == ["2645810", "3022097"], f"Expected split, got: {result}"


def test_split_clean_7_digit_patent():
    from app.enrichment.patents import _split_merged_patent_number
    result = _split_merged_patent_number("2709025")
    assert result == ["2709025"]


def test_split_handles_ca_prefix():
    from app.enrichment.patents import _split_merged_patent_number
    result = _split_merged_patent_number("CA 2709025")
    assert result == ["2709025"]


def test_split_empty_string():
    from app.enrichment.patents import _split_merged_patent_number
    result = _split_merged_patent_number("")
    assert result == []


# ── _parse_patent_zip_by_din ──────────────────────────────────────────────────

def _make_patent_zip(drug_rows: list, patent_rows: list) -> bytes:
    drugs_header = (
        "DRUG_ID,MEDICINAL_INGREDIENT_E,BRAND_NAME_E,ROUTE_OF_ADMINISTRATION_E,"
        "STRENGTH_PER_UNIT_E,HUMAN_OR_VET_E,THERAPEUTIC_CLASS,DOSAGE_FORM_E,DIN\n"
    )
    drugs_csv = drugs_header + "".join(
        "%s,Ing,Brand,Oral,100mg,Human,Test,Tablet,%s\n" % (did, din)
        for did, din in drug_rows
    )
    patent_header = (
        "DRUG_ID,FORM_ID,PATENT_NUMBER,CATEGORY,FILING_DATE,DATE_GRANTED,"
        "EXPIRATION_DATE,SERVICE_COMPANY_NAME_E,FIRST_NAME,LAST_NAME,"
        "POSITION_TITLE,ADDRESS,CITY_NAME_E,PROVINCE_NAME_E,POSTAL_CODE\n"
    )
    patent_csv = patent_header + "".join(
        "%s,999,%s,C,%s,%s,%s,Co,,,,,Toronto,ONTARIO,M5V1A1\n" % (did, pn, fd, gd, ed)
        for did, pn, fd, gd, ed in patent_rows
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("drugs_e.txt", drugs_csv)
        zf.writestr("patent-service_e.txt", patent_csv)
    return buf.getvalue()


def test_parse_patent_zip_by_din_basic():
    from app.enrichment.patents import _parse_patent_zip_by_din

    zip_bytes = _make_patent_zip(
        drug_rows=[("1", "02322285"), ("2", "02498014")],
        patent_rows=[
            ("1", "2645810", "12/10/2008", "08/26/2014", "12/10/2028"),
            ("1", "3022097", "06/01/2015", "01/01/2020", "06/01/2035"),
            ("2", "2709025", "12/10/2008", "08/26/2014", "12/10/2028"),
        ],
    )
    result = _parse_patent_zip_by_din(zip_bytes)

    assert "02322285" in result
    assert set(result["02322285"]) == {"2645810", "3022097"}
    assert "02498014" in result
    assert result["02498014"] == ["2709025"]


def test_parse_patent_zip_by_din_pads_din_to_8_digits():
    from app.enrichment.patents import _parse_patent_zip_by_din

    zip_bytes = _make_patent_zip(
        drug_rows=[("1", "2322285")],
        patent_rows=[("1", "9999999", "", "", "")],
    )
    result = _parse_patent_zip_by_din(zip_bytes)
    assert "02322285" in result


def test_parse_patent_zip_by_din_defensive_split_on_merged():
    from app.enrichment.patents import _parse_patent_zip_by_din

    zip_bytes = _make_patent_zip(
        drug_rows=[("1", "02322285")],
        patent_rows=[("1", "26458103022097", "", "", "")],
    )
    result = _parse_patent_zip_by_din(zip_bytes)
    assert "02322285" in result
    assert set(result["02322285"]) == {"2645810", "3022097"}


def test_parse_patent_zip_by_din_empty_zip():
    from app.enrichment.patents import _parse_patent_zip_by_din

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("README.txt", "no patent files here")
    result = _parse_patent_zip_by_din(buf.getvalue())
    assert result == {}


def test_parse_patent_zip_by_din_empty_bytes():
    from app.enrichment.patents import _parse_patent_zip_by_din
    assert _parse_patent_zip_by_din(b"") == {}


# ── _aggregate_patents_latest ─────────────────────────────────────────────────

def test_aggregate_latest_selects_latest_expiry(tmp_path):
    import datetime
    import app.enrichment.store as store_mod
    store_mod.reset_for_testing(str(tmp_path / "enrich.db"))
    store_mod.upsert_patent("02322285", "2645810", "2000-01-01", "2005-01-01", "2020-01-01")
    store_mod.upsert_patent("02322285", "3022097", "2015-06-01", "2020-01-01", "2035-06-01")

    from app.enrichment.workbook import _aggregate_patents_latest
    # Use as_of 2019-01-01 so both patents are active (2020 and 2035 expiries are both future).
    agg = _aggregate_patents_latest("02322285", as_of=datetime.date(2019, 1, 1))

    assert agg["patent_count"] == 2
    assert agg["patent_number"] == "3022097", (
        f"Expected 3022097 (latest expiry 2035), got {agg['patent_number']!r}"
    )
    assert agg["patent_grant_date"] == "2020-01-01"
    assert agg["patent_expiry_date"] == "2035-06-01"


def test_aggregate_latest_no_patents(tmp_path):
    import app.enrichment.store as store_mod
    store_mod.reset_for_testing(str(tmp_path / "enrich.db"))

    from app.enrichment.workbook import _aggregate_patents_latest
    agg = _aggregate_patents_latest("99999999")

    assert agg["patent_count"] == 0
    assert agg["patent_number"] is None
    assert agg["patent_grant_date"] is None
    assert agg["patent_expiry_date"] is None


def test_aggregate_latest_single_patent(tmp_path):
    import datetime
    import app.enrichment.store as store_mod
    store_mod.reset_for_testing(str(tmp_path / "enrich.db"))
    store_mod.upsert_patent("00000001", "1111111", "2000-01-01", "2005-01-01", "2020-01-01")

    from app.enrichment.workbook import _aggregate_patents_latest
    # as_of 2019 so the 2020 expiry is still active.
    agg = _aggregate_patents_latest("00000001", as_of=datetime.date(2019, 1, 1))

    assert agg["patent_count"] == 1
    assert agg["patent_number"] == "1111111"
    assert agg["patent_expiry_date"] == "2020-01-01"


def test_aggregate_latest_tiebreak_uses_highest_patent_number(tmp_path):
    """When two patents share the same expiry date, highest patent_number wins."""
    import app.enrichment.store as store_mod
    store_mod.reset_for_testing(str(tmp_path / "enrich.db"))
    store_mod.upsert_patent("00000001", "2000001", "2000-01-01", "2005-01-01", "2030-01-01")
    store_mod.upsert_patent("00000001", "9000001", "2001-01-01", "2006-01-01", "2030-01-01")

    from app.enrichment.workbook import _aggregate_patents_latest
    agg = _aggregate_patents_latest("00000001")

    assert agg["patent_number"] == "9000001", (
        f"Tiebreak should pick higher patent_number, got {agg['patent_number']!r}"
    )


def test_aggregate_latest_missing_expiry_cannot_win(tmp_path):
    """A patent with no expiry_date (treated as min-date) must not beat one with a real date.

    as_of 2024 ensures the 2025-expiry patent is active and the None-expiry one
    is excluded (min-date < as_of), so the real-date patent must be selected.
    """
    import datetime
    import app.enrichment.store as store_mod
    store_mod.reset_for_testing(str(tmp_path / "enrich.db"))
    store_mod.upsert_patent("00000001", "1111111", "2000-01-01", "2005-01-01", None)
    store_mod.upsert_patent("00000001", "2222222", "2001-01-01", "2006-01-01", "2025-01-01")

    from app.enrichment.workbook import _aggregate_patents_latest
    # as_of 2024 so 2025-expiry is active; None-expiry maps to date.min and is excluded.
    agg = _aggregate_patents_latest("00000001", as_of=datetime.date(2024, 1, 1))

    assert agg["patent_number"] == "2222222", (
        f"Patent with real expiry must win over None, got {agg['patent_number']!r}"
    )


# ── build_sheet1 uses latest-expiry columns ───────────────────────────────────

def test_latest_patent_columns_in_sheet1(tmp_path):
    """build_sheet1 uses patent_number/grant_date/expiry_date; old wide columns absent."""
    import app.enrichment.store as store_mod
    store_mod.reset_for_testing(str(tmp_path / "enrich.db"))
    store_mod.upsert_patent("02498014", "2709025", "2008-12-10", "2014-08-26", "2028-12-10")
    store_mod.upsert_patent("02498014", "3022097", "2015-01-01", "2020-03-01", "2035-01-01")

    from tests.test_build_workbook import _dpd, _make_response
    from app.enrichment.workbook import build_sheet1

    response = _make_response(dpd_records=[_dpd("02498014")])
    df = build_sheet1(response)

    assert "patent_number" in df.columns
    assert "patent_grant_date" in df.columns
    assert "patent_expiry_date" in df.columns
    assert "patent_1_number" not in df.columns
    assert "patent_2_number" not in df.columns
    assert "patent_numbers" not in df.columns
    assert "all_patents_detail" not in df.columns
    assert df.loc[df["din"] == "02498014", "patent_count"].iloc[0] == 2
    assert df.loc[df["din"] == "02498014", "patent_number"].iloc[0] == "3022097"


# ── Change 2: no *_url or *_page columns ─────────────────────────────────────

def test_columns_no_url_or_page(tmp_path):
    """Sheet 1 must not contain any column whose name ends in _url or _page."""
    import app.enrichment.store as store_mod
    store_mod.reset_for_testing(str(tmp_path / "enrich.db"))

    from tests.test_build_workbook import _dpd, _noc, _make_response
    from app.enrichment.workbook import build_sheet1

    response = _make_response(dpd_records=[_dpd("02498014")], noc_records=[_noc("02498014")])
    df = build_sheet1(response)

    for col in df.columns:
        assert not col.endswith("_url"), f"URL column should not appear in output: {col!r}"
        assert not col.endswith("_page"), f"Page citation column should not appear in output: {col!r}"


def test_drug_code_present(tmp_path):
    import time
    import app.enrichment.store as store_mod
    store_mod.reset_for_testing(str(tmp_path / "enrich.db"))

    store_mod.upsert_labeling("02498014", {
        "needs_ocr": 0, "has_unverified": 0, "drug_code": 99001,
        "fetched_at": time.time(),
    })

    from app.models import DrugRecord
    from tests.test_build_workbook import _make_response
    from app.enrichment.workbook import build_sheet1

    rec = DrugRecord(
        source="DPD", din="02498014", brand_name="PIQRAY",
        company="Novartis", ingredient="alpelisib", strength="50 mg",
        source_specific={"drug_code": 99001},
    )
    response = _make_response(dpd_records=[rec])
    df = build_sheet1(response)

    assert "_drug_code" in df.columns, "_drug_code must be present when DPD provides drug_code"
    assert "needs_ocr" not in df.columns, "needs_ocr removed from workbook schema"
    assert "_last_update" not in df.columns, "_last_update removed from workbook schema"


def test_patent_number_cell_within_8_chars(tmp_path):
    """patent_number cell must be ≤ 8 characters (no merged 14-digit tokens)."""
    import app.enrichment.store as store_mod
    store_mod.reset_for_testing(str(tmp_path / "enrich.db"))
    store_mod.upsert_patent("02322285", "2645810", "2000-01-01", "2005-01-01", "2020-01-01")
    store_mod.upsert_patent("02322285", "3022097", "2015-01-01", "2020-01-01", "2035-01-01")

    from tests.test_build_workbook import _dpd, _make_response
    from app.enrichment.workbook import build_sheet1

    response = _make_response(dpd_records=[_dpd("02322285")])
    df = build_sheet1(response)

    if "patent_number" in df.columns:
        for val in df["patent_number"].dropna():
            assert len(str(val)) <= 8, f"Patent number {val!r} exceeds 8 chars"
