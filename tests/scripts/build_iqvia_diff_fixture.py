#!/usr/bin/env python3
"""Build the trimmed IQVIA old/new fixture pair for the diff regression test.

Run from the project root:
    python3 tests/scripts/build_iqvia_diff_fixture.py

Writes two tiny, fully-synthetic extracts under tests/fixtures/iqvia/diff/:

  old_extract.csv   — CSV, YYYY/MM date order, comma-formatted strings + "-"
  new_extract.xlsx  — xlsx, MM/YYYY date order, numeric cells + "-", with a
                      leading "Pivot" sheet (to prove the data sheet is chosen)

The two files deliberately disagree on column names and date order so the test
exercises per-file latest-MAT resolution.  Every value here is hand-chosen; the
expected diff is asserted in tests/test_iqvia_diff.py.  No real IQVIA data.

Coverage built into the pair (latest period: old 2024/06, new 12/2024):
  • MATERIAL MOVE  — ATORVASTATIN / LIPITOR / Pfizer 20MG: $2.0M→$2.5M (+25%).
                     Old manufacturer "PFIZER CANADA ULC" + product
                     "LIPITOR 20MG TABLETS" vs new "PFIZER" / "LIPITOR" — proves
                     identity normalisation folds the two into ONE moved row, not
                     a phantom exit+entrant.  Old is split across two channel rows
                     to prove collapse sums them.
  • BELOW-THRESHOLD— METFORMIN / GLUCOPHAGE 500MG: +$40k (<$100k) and +500 units
                     (<1,000).  Must NOT appear anywhere.
  • ENTRANT        — SEMAGLUTIDE / OZEMPIC 1MG: absent in old, $5.0M in new.
  • EXIT (absent)  — RANITIDINE / ZANTAC 150MG: $0.8M in old, gone from new.
  • EXIT (zero)    — FAMOTIDINE / PEPCID 20MG: present in old; row exists in new
                     but its latest-MAT cells are "-" (0) — proves "present" means
                     non-zero latest sales, not mere row existence.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "iqvia" / "diff"

# The 10 IQVIA identity columns, in real-extract order.
_ID = ["Channel", "Combined Molecule", "Strength", "Pack", "Product",
       "Form 3", "Manufacturer", "Corporation", "Product Form", "Province"]

# Old file: CSV, YYYY/MM order, two periods (latest = 2024/06).
_OLD_METRICS = [
    "Dollars MAT 2023/06", "Units MAT 2023/06", "Ext Units MAT 2023/06",
    "Dollars MAT 2024/06", "Units MAT 2024/06", "Ext Units MAT 2024/06",
]
# New file: xlsx, MM/YYYY order, two periods (latest = 12/2024).
_NEW_METRICS = [
    "Dollars MAT 12/2023", "Units MAT 12/2023", "Ext Units MAT 12/2023",
    "Dollars MAT 12/2024", "Units MAT 12/2024", "Ext Units MAT 12/2024",
]


def _row(channel, molecule, strength, pack, product, form3, mfr, corp, pform, prov, metrics):
    return dict(zip(_ID, [channel, molecule, strength, pack, product, form3, mfr, corp, pform, prov]),
                **dict(zip(_OLD_METRICS if len(metrics) == 6 else _OLD_METRICS, metrics)))


# ── OLD rows (raw, pre-collapse) ──────────────────────────────────────────────
_OLD_ROWS = [
    # LIPITOR — split across two channel rows; latest-MAT sums to $2.0M / 100k.
    ["Drugstore", "ATORVASTATIN", "20MG", "90 TAB", "LIPITOR 20MG TABLETS", "TAB    ORAL",
     "PFIZER CANADA ULC", "PFIZER", "TAB", "ONTARIO",
     "1,000,000", "50,000", "100,000", "1,200,000", "60,000", "120,000"],
    ["Hospital", "ATORVASTATIN", "20MG", "90 TAB", "LIPITOR 20MG TABLETS", "TAB    ORAL",
     "PFIZER CANADA ULC", "PFIZER", "TAB", "QUEBEC",
     "-", "-", "-", "800,000", "40,000", "80,000"],
    # GLUCOPHAGE — below-threshold move target.
    ["Drugstore", "METFORMIN", "500MG", "100 TAB", "GLUCOPHAGE", "TAB    ORAL",
     "MERCK", "MERCK", "TAB", "ONTARIO",
     "950,000", "48,000", "96,000", "1,000,000", "50,000", "100,000"],
    # ZANTAC — exits by being absent from new.
    ["Drugstore", "RANITIDINE", "150MG", "60 TAB", "ZANTAC", "TAB    ORAL",
     "SANOFI", "SANOFI", "TAB", "ONTARIO",
     "820,000", "61,000", "122,000", "800,000", "60,000", "120,000"],
    # PEPCID — exits by going to zero latest-MAT in new.
    ["Drugstore", "FAMOTIDINE", "20MG", "30 TAB", "PEPCID", "TAB    ORAL",
     "JOHNSON & JOHNSON", "JNJ", "TAB", "ONTARIO",
     "310,000", "21,000", "42,000", "300,000", "20,000", "40,000"],
]

# ── NEW rows (raw, pre-collapse) ──────────────────────────────────────────────
_NEW_ROWS = [
    # LIPITOR — trivially different manufacturer/product strings; +25% move.
    ["Drugstore", "ATORVASTATIN", "20MG", "90 TAB", "LIPITOR", "TAB    ORAL",
     "PFIZER", "PFIZER", "TAB", "ONTARIO",
     1500000, 75000, 150000, 2500000, 130000, 260000],
    # GLUCOPHAGE — +$40k / +500u, below both floors.
    ["Drugstore", "METFORMIN", "500MG", "100 TAB", "GLUCOPHAGE", "TAB    ORAL",
     "MERCK", "MERCK", "TAB", "ONTARIO",
     1000000, 50000, 100000, 1040000, 50500, 101000],
    # OZEMPIC — new entrant.
    ["Drugstore", "SEMAGLUTIDE", "1MG", "1 PEN", "OZEMPIC", "INJ    SC",
     "NOVO NORDISK", "NOVO", "PEN", "ONTARIO",
     0, 0, 0, 5000000, 200000, 200000],
    # PEPCID — present as a row but latest-MAT is "-" (0) → not present in new.
    ["Drugstore", "FAMOTIDINE", "20MG", "30 TAB", "PEPCID", "TAB    ORAL",
     "JOHNSON & JOHNSON", "JNJ", "TAB", "ONTARIO",
     250000, 18000, 36000, "-", "-", "-"],
]


def build_old_csv() -> str:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / "old_extract.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(_ID + _OLD_METRICS)
        w.writerows(_OLD_ROWS)
    return str(path)


def build_new_xlsx() -> str:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / "new_extract.xlsx"
    data = pd.DataFrame(_NEW_ROWS, columns=_ID + _NEW_METRICS)
    # A decoy "Pivot" sheet leads the workbook (formulas, no metric columns) so the
    # parser must skip it and choose "data".
    pivot = pd.DataFrame({"Vol CAGR": ["=(I16/R16)^(1/3)-1"]})
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pivot.to_excel(xw, sheet_name="Pivot", index=False)
        data.to_excel(xw, sheet_name="data", index=False)
    return str(path)


if __name__ == "__main__":
    print("Building IQVIA diff fixture pair…")
    print("  ✓", build_old_csv())
    print("  ✓", build_new_xlsx())
