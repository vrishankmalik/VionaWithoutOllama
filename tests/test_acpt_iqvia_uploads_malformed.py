"""Malformed / edge-case acceptance tests for IQVIA parse, collapse, and the
upload + compare endpoints.

All inputs are constructed in-memory (no real IQVIA files): pivot-only sheets,
header-only tables, truncated/garbage bytes, non-spreadsheet bytes, CSV↔XLSX
equivalence, missing grouping columns, non-numeric metric cells, and one large
~12,000-row synthetic extract whose duration is intentionally kept in its own
test so it surfaces in pytest timing.

Contract under test (real-sales-safety):
  * parse_iqvia raises ValueError on a NON-blank non-numeric metric cell (never
    silent-zero); blank / '-' / nan → 0.
  * collapse_iqvia raises ValueError naming any missing grouping column.
  * upload endpoint: 422 on parse failure or no metric columns (never 500).
  * compare endpoint: 422 when compare_iqvia raises ValueError.
"""
import io as _io

import pandas as pd
import pytest

from app.enrichment.iqvia import parse_iqvia, collapse_iqvia, detect_metric_columns

_ID = ["Channel", "Combined Molecule", "Strength", "Pack", "Product",
       "Form 3", "Manufacturer", "Corporation", "Product Form", "Province"]
_METRICS = [
    "Dollars MAT 2023/06", "Units MAT 2023/06", "Ext Units MAT 2023/06",
    "Dollars MAT 2024/06", "Units MAT 2024/06", "Ext Units MAT 2024/06",
]


def _row(molecule, product, mfr, strength, latest_dollars, latest_units):
    return ["Drugstore", molecule, strength, "30 TAB", product, "TAB ORAL",
            mfr, mfr, "TAB", "ONTARIO",
            0, 0, 0, latest_dollars, latest_units, latest_units]


def _xlsx_bytes(df: pd.DataFrame, sheet_name: str = "data") -> bytes:
    buf = _io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name=sheet_name, index=False)
    return buf.getvalue()


def _csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


# ── Pivot-only file (no metric columns) ───────────────────────────────────────

def test_pivot_only_parse_has_no_metric_columns():
    # A sheet with identity columns but NO 'Dollars/Units MAT …' columns.
    df = pd.DataFrame({"Combined Molecule": ["X"], "Product": ["Y"],
                       "Manufacturer": ["Z"], "Strength": ["10MG"]})
    raw = parse_iqvia(_xlsx_bytes(df, sheet_name="Pivot"))
    assert detect_metric_columns(raw) == []
    # collapse still works (no metric cols to sum) but yields no metric columns.
    collapsed = collapse_iqvia(raw)
    assert detect_metric_columns(collapsed) == []


def test_pivot_only_upload_is_422(client):
    df = pd.DataFrame({"Combined Molecule": ["X"], "Product": ["Y"],
                       "Manufacturer": ["Z"], "Strength": ["10MG"]})
    resp = client.post(
        "/api/iqvia/upload",
        files={"file": ("pivot.xlsx", _xlsx_bytes(df),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert resp.status_code == 422
    assert "metric" in resp.text.lower()


def test_pivot_only_compare_is_422(client):
    df = pd.DataFrame({"Combined Molecule": ["X"], "Product": ["Y"],
                       "Manufacturer": ["Z"], "Strength": ["10MG"]})
    pivot = _xlsx_bytes(df)
    good = _xlsx_bytes(pd.DataFrame([_row("MOL", "BRAND", "ACME", "10MG", 1, 1)],
                                    columns=_ID + _METRICS))
    resp = client.post(
        "/api/iqvia/compare",
        files={
            "old_file": ("pivot.xlsx", pivot,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "new_file": ("good.xlsx", good,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        },
    )
    assert resp.status_code == 422


# ── 0-row file (headers only, valid metric columns) ───────────────────────────

def test_zero_row_file_parses_and_collapses_empty():
    df = pd.DataFrame(columns=_ID + _METRICS)
    raw = parse_iqvia(_xlsx_bytes(df))
    assert len(raw) == 0
    assert detect_metric_columns(raw) == _METRICS
    collapsed = collapse_iqvia(raw)
    assert len(collapsed) == 0


def test_zero_row_compare_yields_empty_signals():
    from app.enrichment.iqvia_diff import compare_iqvia
    empty = _xlsx_bytes(pd.DataFrame(columns=_ID + _METRICS))
    d = compare_iqvia(empty, empty)
    assert d.entrants.empty and d.exits.empty and d.moves.empty


# ── Truncated / garbage bytes ─────────────────────────────────────────────────

def test_truncated_xlsx_magic_upload_is_422_not_500(client):
    # PK\x03\x04 magic → routed to the xlsx reader, which fails on the garbage tail.
    resp = client.post(
        "/api/iqvia/upload",
        files={"file": ("corrupt.xlsx", b"PK\x03\x04corrupted-not-a-zip",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert resp.status_code == 422
    assert resp.status_code != 500


def test_truncated_xlsx_parse_raises():
    with pytest.raises(Exception):
        parse_iqvia(b"PK\x03\x04corrupted-not-a-zip")


# ── Non-spreadsheet bytes → treated as CSV ────────────────────────────────────

def test_non_spreadsheet_bytes_treated_as_csv_no_metric_cols():
    # No xlsx/xls magic → decoded as CSV. Single garbage line → no metric columns.
    raw = parse_iqvia(b"this is not a spreadsheet")
    assert detect_metric_columns(raw) == []


def test_non_spreadsheet_bytes_compare_is_value_error():
    from app.enrichment.iqvia_diff import compare_iqvia
    good = _xlsx_bytes(pd.DataFrame([_row("MOL", "BRAND", "ACME", "10MG", 1, 1)],
                                    columns=_ID + _METRICS))
    with pytest.raises(ValueError):
        compare_iqvia(b"this is not a spreadsheet", good)


def test_non_spreadsheet_bytes_upload_is_422(client):
    # .xls extension so the endpoint accepts it, but bytes carry no metric columns.
    resp = client.post(
        "/api/iqvia/upload",
        files={"file": ("junk.xls", b"this is not a spreadsheet",
                        "application/vnd.ms-excel")},
    )
    assert resp.status_code == 422


# ── CSV vs XLSX equivalence ───────────────────────────────────────────────────

def test_csv_and_xlsx_collapse_to_equal_metrics():
    rows = [
        _row("ATORVASTATIN", "LIPITOR", "PFIZER", "20MG", 1_200_000, 60_000),
        _row("ATORVASTATIN", "LIPITOR", "PFIZER", "20MG", 800_000, 40_000),
        _row("METFORMIN", "GLUCOPHAGE", "MERCK", "500MG", 1_000_000, 50_000),
    ]
    df = pd.DataFrame(rows, columns=_ID + _METRICS)
    c_csv = collapse_iqvia(parse_iqvia(_csv_bytes(df)))
    c_xlsx = collapse_iqvia(parse_iqvia(_xlsx_bytes(df)))

    key = ["Combined Molecule", "Product", "Manufacturer", "Strength"]
    mc = detect_metric_columns(c_csv)
    a = c_csv.sort_values(key).reset_index(drop=True)[key + mc]
    b = c_xlsx.sort_values(key).reset_index(drop=True)[key + mc]
    pd.testing.assert_frame_equal(a, b)
    # LIPITOR's two rows summed: latest Dollars 1.2M + 0.8M = 2.0M.
    lip = a[a["Product"] == "LIPITOR"].iloc[0]
    assert int(lip["Dollars MAT 2024/06"]) == 2_000_000


# ── Wrong-shape: missing a grouping column ────────────────────────────────────

def test_collapse_missing_manufacturer_raises_naming_column():
    cols = [c for c in _ID if c != "Manufacturer"] + _METRICS
    df = pd.DataFrame(
        [["Drugstore", "MOL", "10MG", "30 TAB", "BRAND", "TAB ORAL",
          "ACME", "TAB", "ONTARIO", 0, 0, 0, 1, 1, 1]],
        columns=cols,
    )
    raw = parse_iqvia(_csv_bytes(df))
    with pytest.raises(ValueError, match="Manufacturer"):
        collapse_iqvia(raw)


# ── Non-numeric metric cells → loud ValueError ────────────────────────────────

@pytest.mark.parametrize("bad", ["N/A", "1.2K", "*", "<10"])
def test_non_numeric_metric_cell_raises_listing_value(bad):
    # Build all metric cells as strings (object dtype) so the poisoned value can
    # be placed without pandas refusing an int64 column assignment.
    rows = [_row("MOL", "BRAND", "ACME", "10MG", "1000000", "50000")]
    df = pd.DataFrame(rows, columns=_ID + _METRICS, dtype=object)
    # Poison the latest Dollars cell with a non-blank, non-numeric value.
    df.loc[0, "Dollars MAT 2024/06"] = bad
    with pytest.raises(ValueError) as exc:
        parse_iqvia(_csv_bytes(df))
    assert bad in str(exc.value)


def test_blank_and_dash_metric_cells_become_zero():
    rows = [_row("MOL", "BRAND", "ACME", "10MG", "0", "0")]
    df = pd.DataFrame(rows, columns=_ID + _METRICS, dtype=object)
    df.loc[0, "Dollars MAT 2024/06"] = "-"
    df.loc[0, "Units MAT 2024/06"] = ""
    raw = parse_iqvia(_csv_bytes(df))
    assert int(raw.loc[0, "Dollars MAT 2024/06"]) == 0
    assert int(raw.loc[0, "Units MAT 2024/06"]) == 0


# ── HUGE file (~12,000 rows) — kept separate so its duration is visible ────────

def test_large_synthetic_extract_parses_and_collapses():
    # ROW COUNT: 12,000 raw rows (4,000 distinct products × 3 channel duplicates).
    # Each distinct product's 3 rows collapse to 1; latest Dollars sums to 3,000.
    n_products = 4_000
    channels = ["Drugstore", "Hospital", "Mail Order"]
    rows = []
    for i in range(n_products):
        for ch in channels:
            rows.append([ch, f"MOL{i}", "10MG", "30 TAB", f"BRAND{i}", "TAB ORAL",
                         f"MFR{i}", f"MFR{i}", "TAB", "ONTARIO",
                         0, 0, 0, 1_000, 50, 50])
    df = pd.DataFrame(rows, columns=_ID + _METRICS)
    assert len(df) == 12_000

    raw = parse_iqvia(_csv_bytes(df))
    assert len(raw) == 12_000
    collapsed = collapse_iqvia(raw)
    assert len(collapsed) == n_products
    # Each product's 3 channel rows summed: latest Dollars 1,000 × 3 = 3,000.
    assert int(collapsed["Dollars MAT 2024/06"].iloc[0]) == 3_000
    # Total latest Dollars conserved across collapse: 12,000 rows × 1,000.
    assert int(collapsed["Dollars MAT 2024/06"].sum()) == 12_000 * 1_000
