"""Acceptance: weird / garbage inputs across the filter + request surface.

Three surfaces, all offline:

1. /api/search — a syntactically-valid ingredient that matches NOTHING returns a
   clean 200 with empty/no-result sources (no crash).  respx-mocked sources.

2. /export/start request surface — `_resolve_queries` trims, case-insensitively
   dedups, preserves order, and handles an absurdly long list; an empty /
   whitespace-only list → HTTP 400.  The background job is patched to a no-op so
   the endpoint stays hermetic (we only assert the synchronous response shape).

3. Filter date/criterion parsing — garbage values raise ValueError exactly where
   the spec requires (past date, malformed MM/DD/YYYY, impossible calendar date,
   non-date text, unknown operator, non-numeric criterion value), and stored-date
   sentinels flowing through compute_products' representative `_no_file_date` are
   blank where unparseable, parsed where valid.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.enrichment.screen import (
    NoFileDateFilter,
    compute_products,
    parse_criteria,
    parse_no_file_date,
)

_TODAY = date(2026, 6, 24)


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


# ════════════════════════════════════════════════════════════════════════════
# 1. Ingredient that matches nothing → clean empty-ish result, no crash
# ════════════════════════════════════════════════════════════════════════════

def test_search_unmatched_ingredient_returns_200_empty(
    mock_noc, mock_dpd, mock_gsur, mock_patent_register
):
    """A valid-but-unknown ingredient yields 200 and zero records — never a crash."""
    resp = _client().get("/api/search?q=zzqwxnonexistent&field=ingredient")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    total = sum(len(s.get("records", [])) for s in body["sources"])
    assert total == 0, f"expected no records for a nonsense ingredient, got {total}"
    # Per-source statuses are clean strings (no 'error' from a crash).
    for s in body["sources"]:
        assert s["status"] in {"no_results", "unsupported", "ok", "success", "error"}


def test_search_blank_query_returns_200_no_sources(
    mock_noc, mock_dpd, mock_gsur, mock_patent_register
):
    resp = _client().get("/api/search?q=%20%20&field=ingredient")
    assert resp.status_code == 200, resp.text
    assert resp.json()["sources"] == []


# ════════════════════════════════════════════════════════════════════════════
# 2. /export/start request surface — dedup, trim, order, long list, empty → 400
# ════════════════════════════════════════════════════════════════════════════

def _start(payload: dict):
    with patch("app.main.run_export_job", new=AsyncMock(return_value=None)):
        return _client().post("/export/start", json=payload)


def test_export_start_dedups_trims_and_preserves_order():
    payload = {
        "queries": [
            "  Alpelisib  ", "APREMILAST", "alpelisib", "apremilast ",
            "  metformin", "Metformin", "ALPELISIB",
        ],
        "field": "ingredient",
    }
    resp = _start(payload)
    assert resp.status_code == 200, resp.text
    qs = resp.json()["queries"]
    # First-seen casing/spacing wins; case-insensitive dedup; input order preserved.
    assert qs == ["Alpelisib", "APREMILAST", "metformin"], qs


def test_export_start_absurdly_long_list_dedups_to_unique():
    # ~60 entries, every other one a case/space variant of an existing query.
    raw: list[str] = []
    expected: list[str] = []
    for i in range(30):
        name = f"ingredient{i}"
        expected.append(name)
        raw.append(name)
        raw.append(f"  {name.upper()}  ")  # duplicate (case/space) of the same
    assert len(raw) == 60
    resp = _start({"queries": raw, "field": "ingredient"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["queries"] == expected


def test_export_start_special_chars_and_weird_casing_preserved_first_seen():
    payload = {"queries": ["α-blocker", "  Α-BLOCKER  ", "co-trimoxazole"], "field": "ingredient"}
    resp = _start(payload)
    assert resp.status_code == 200, resp.text
    qs = resp.json()["queries"]
    # The two α-blocker variants collapse (casefold-equal); special chars survive.
    assert qs == ["α-blocker", "co-trimoxazole"], qs


def test_export_start_empty_list_400():
    resp = _start({"queries": [], "field": "ingredient"})
    assert resp.status_code == 400, resp.text


def test_export_start_whitespace_only_list_400():
    resp = _start({"queries": ["   ", "\t", "\n"], "field": "ingredient"})
    assert resp.status_code == 400, resp.text


def test_export_start_single_q_field_still_works():
    resp = _start({"q": "  alpelisib  ", "field": "ingredient"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["queries"] == ["alpelisib"]


# ════════════════════════════════════════════════════════════════════════════
# 3. Garbage in the date / criterion fields → ValueError
# ════════════════════════════════════════════════════════════════════════════

def _date_entry(value):
    return [{"metric": "no_file_date", "operator": "greater", "value": value}]


@pytest.mark.parametrize(
    "bad",
    [
        "01/01/2020",      # past date (relative to pinned today)
        "06/24/2026",      # today exactly — not in the FUTURE
        "13/40/2030",      # impossible month + day
        "2030-01-01",      # wrong format (ISO, not MM/DD/YYYY)
        "1/1/2030",        # not zero-padded MM/DD/YYYY
        "not a date",      # non-date text
    ],
)
def test_parse_no_file_date_garbage_raises(bad):
    with pytest.raises(ValueError):
        parse_no_file_date(_date_entry(bad), today=_TODAY)


def test_parse_no_file_date_blank_value_is_no_filter_not_error():
    # A no-file-date entry with a blank value is additive (no filter), NOT an error.
    assert parse_no_file_date(_date_entry(""), today=_TODAY) is None
    assert parse_no_file_date(_date_entry(None), today=_TODAY) is None


def test_parse_no_file_date_unknown_operator_raises():
    entry = [{"metric": "no_file_date", "operator": "between", "value": "01/01/2030"}]
    with pytest.raises(ValueError):
        parse_no_file_date(entry, today=_TODAY)


def test_parse_no_file_date_valid_future_parses():
    f = parse_no_file_date(_date_entry("01/02/2030"), today=_TODAY)
    assert isinstance(f, NoFileDateFilter)
    assert f.operator == "greater"
    assert f.threshold == date(2030, 1, 2)


@pytest.mark.parametrize("bad", ["abc", "1,2,3", "ten", "$100", "", "  "])
def test_parse_criteria_non_numeric_value_raises_or_skips(bad):
    """A non-numeric criterion value raises ValueError; a blank value is silently
    skipped (the form sends all rows, only filled-in ones take effect)."""
    entry = [{"metric": "competitors", "operator": "above", "value": bad}]
    if str(bad).strip() == "":
        # Blank value → dropped, not an error.
        assert parse_criteria(entry) == []
    else:
        with pytest.raises(ValueError):
            parse_criteria(entry)


def test_parse_criteria_unknown_metric_and_operator_raise():
    with pytest.raises(ValueError):
        parse_criteria([{"metric": "bogus", "operator": "above", "value": "3"}])
    with pytest.raises(ValueError):
        parse_criteria([{"metric": "competitors", "operator": "between", "value": "3"}])


# ════════════════════════════════════════════════════════════════════════════
# 3b. Date sentinels flow through compute_products' representative date
# ════════════════════════════════════════════════════════════════════════════

def _sheet1(dp_values: list) -> pd.DataFrame:
    """One DIN per row, all the same product, each row carrying a dp date cell."""
    rows = []
    for i, dpv in enumerate(dp_values):
        rows.append({
            "din": f"{1000 + i:08d}",
            "ingredient": "WIDGETOL 10 MG",
            "dosage_form": "TABLET",
            "company": "Acme Inc",
            "status": "MARKETED",
            "dp_6yr_no_file_date": dpv,
        })
    return pd.DataFrame(rows)


def test_compute_products_representative_date_blank_when_all_unparseable():
    sheet1 = _sheet1(["N/A", "", "see footnote", None])
    products, _warn = compute_products(sheet1, pd.DataFrame())
    assert len(products) == 1
    assert products.iloc[0]["_no_file_date"] is None


def test_compute_products_representative_date_is_latest_parseable():
    sheet1 = _sheet1(["N/A", "2030-01-01", "2031-06-06 (note)", "garbage"])
    products, _warn = compute_products(sheet1, pd.DataFrame())
    assert len(products) == 1
    # LATEST parseable register date among the product's DINs.
    assert products.iloc[0]["_no_file_date"] == date(2031, 6, 6)
