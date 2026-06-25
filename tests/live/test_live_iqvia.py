"""Part 1 — Live: real IQVIA extract matched onto the live universe (no PDF).

Golden MAT anchor (hand-verified, same as tests/test_iqvia.py):
    SANIS PROGESTERONE 100MG, DIN 02516187
        Units MAT 12/2025   = 218,591
        Dollars MAT 12/2025 = 21,215,081

These tests prove the real IQVIA.xlsx reproduces that anchor when matched against
the LIVE DPD universe, that the match is made on DPD-native identity fields only
(brand / company / strength), and that NO Product-Monograph PDF data is involved
in the sizing match.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

_SANIS_DIN = "02516187"
_UNITS_COL = "Units MAT 12/2025"
_DOLLARS_COL = "Dollars MAT 12/2025"

# Labeling (PDF) columns that MUST stay blank on the no-PDF universe — their
# absence proves the IQVIA match has no PDF dependency.
_PDF_COLS = ("active_ingredient", "nonmedicinal_ingredients", "pack_size",
             "pack_style", "color", "shape", "size_mm", "weight", "ph")


def test_iqvia_golden_anchor_reproduces(live_universe_iqvia_sheet):
    df, _recon, _low = live_universe_iqvia_sheet
    assert _UNITS_COL in df.columns and _DOLLARS_COL in df.columns, (
        f"latest-MAT columns missing — got {[c for c in df.columns if 'MAT' in c]}"
    )
    row = df[df["din"].astype(str) == _SANIS_DIN]
    assert len(row) == 1, f"SANIS PROGESTERONE DIN {_SANIS_DIN} missing from live universe"
    assert int(row[_UNITS_COL].iloc[0]) == 218591, row[_UNITS_COL].iloc[0]
    assert int(row[_DOLLARS_COL].iloc[0]) == 21215081, row[_DOLLARS_COL].iloc[0]
    assert row["iqvia_match_confidence"].iloc[0] in ("exact", "high"), (
        row["iqvia_match_confidence"].iloc[0]
    )


def test_iqvia_match_has_no_pdf_dependency(live_universe_iqvia_sheet):
    """The matched anchor row carries IQVIA sizing but NO PDF-derived fields —
    the sizing match is DPD-native (brand/company/strength), never PM-PDF."""
    df, _recon, _low = live_universe_iqvia_sheet
    row = df[df["din"].astype(str) == _SANIS_DIN]
    assert len(row) == 1
    for col in _PDF_COLS:
        if col not in df.columns:
            continue
        val = row[col].iloc[0]
        assert not str(val or "").strip(), (
            f"PDF column {col!r} is populated on the no-PDF universe — match is not DPD-native"
        )


def test_iqvia_low_confidence_band_alive(live_universe_iqvia_sheet):
    """At full market scale the fuzzy (house-brand) match band must fire — proof the
    matcher is not silently stamping everything exact, nor flagging everything fuzzy."""
    df, _recon, low = live_universe_iqvia_sheet
    n = len(df)
    dist = df["iqvia_match_confidence"].value_counts(dropna=False).to_dict()
    assert low > 0, f"low-confidence band is dead at full scale (dist={dist})"
    assert low < n * 0.15, f"low-confidence exploded: {low}/{n} (>15%) — matcher regression (dist={dist})"
