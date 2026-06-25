"""Acceptance-level IQVIA matching tests: golden accuracy, match-confidence,
status partitioning, IQVIA-absent graceful disable, and a realistic-scale run.

Hermetic + offline. The real, committed IQVIA slice fixture
``tests/fixtures/universe/iqvia_slice.xlsx`` is reused as the matcher input (it
contains the same progesterone/medroxyprogesterone groups as the hand-verified
golden anchors, including the ambiguous PROVERA/PFIZER/5MG case). All anchors are
verbatim from tests/test_iqvia.py's verification header and the task brief; none
are re-derived from the code under test.

Golden MAT 12/2025 per-DIN sums (verbatim anchors):
  DIN 02516187 (SANIS / PROGESTERONE / 100MG)     Dollars 21,215,081  Units 218,591
  DIN 02493578 (AURO  / AURO-PROGESTERONE / 100MG) Dollars 13,005,865  Units 233,159
  DIN 00585092 (PFIZER/ DEPO-PROVERA, 150MG↔150MG/ML) Dollars 8,853,659 Units 262,834

Ambiguous anchor:
  IQVIA PROVERA / PFIZER / 5MG → two candidate DINs (00030937 PROVERA 5MG and
  02010739 PROVERA PAK 5MG) → neither DIN receives data (status='ambiguous').
"""
import time
from pathlib import Path

import pandas as pd
import pytest

from app.enrichment.iqvia import (
    parse_iqvia,
    collapse_iqvia,
    detect_metric_columns,
    match_iqvia_to_sheet1,
)
from app.enrichment.universe import (
    attach_match_confidence,
    build_universe_sheet1,
    _confidence_for,
)
from app.enrichment.screen import build_filtered_workbook, Criterion
from app.models import SearchResponse, SearchMetadata, SourceResult, DrugRecord

# The committed real IQVIA slice used by the universe suite — reused here as the
# canonical matcher input. It holds the anchored progesterone groups + the
# ambiguous PROVERA/5MG case + several DPD-absent groups (CYTEX, HIKMA, etc.).
_IQVIA_SLICE = Path(__file__).parent / "fixtures" / "universe" / "iqvia_slice.xlsx"

# Verbatim golden anchors: din -> (Dollars MAT 12/2025, Units MAT 12/2025).
_GOLDEN = {
    "02516187": (21_215_081, 218_591),   # SANIS / PROGESTERONE / 100MG
    "02493578": (13_005_865, 233_159),   # AURO  / AURO-PROGESTERONE / 100MG
    "00585092": (8_853_659, 262_834),    # PFIZER / DEPO-PROVERA / 150MG↔150MG/ML
}

_DOLLARS = "Dollars MAT 12/2025"
_UNITS = "Units MAT 12/2025"


def _slice_collapsed() -> pd.DataFrame:
    if not _IQVIA_SLICE.is_file():
        pytest.skip(f"committed IQVIA slice missing: {_IQVIA_SLICE}")
    return collapse_iqvia(parse_iqvia(_IQVIA_SLICE.read_bytes()))


def _progesterone_sheet1() -> pd.DataFrame:
    """One row per DIN (din/ingredient/brand_name/company/strength/status) for the
    three anchored DINs plus the two ambiguous PROVERA/5MG DINs."""
    return pd.DataFrame([
        {"din": "02516187", "ingredient": "PROGESTERONE",
         "brand_name": "PROGESTERONE", "company": "SANIS HEALTH INC",
         "strength": "100 MG", "status": "Marketed"},
        {"din": "02493578", "ingredient": "PROGESTERONE",
         "brand_name": "AURO-PROGESTERONE", "company": "AURO PHARMA INC",
         "strength": "100 MG", "status": "Marketed"},
        {"din": "00585092", "ingredient": "MEDROXYPROGESTERONE ACETATE",
         "brand_name": "DEPO-PROVERA", "company": "PFIZER CANADA ULC",
         "strength": "150 MG", "status": "Marketed"},
        # Two PROVERA 5 mg DINs — same company, same strength → genuinely ambiguous.
        {"din": "00030937", "ingredient": "MEDROXYPROGESTERONE ACETATE",
         "brand_name": "PROVERA", "company": "PFIZER CANADA ULC",
         "strength": "5 MG", "status": "Marketed"},
        {"din": "02010739", "ingredient": "MEDROXYPROGESTERONE ACETATE",
         "brand_name": "PROVERA PAK", "company": "PFIZER CANADA ULC",
         "strength": "5 MG", "status": "Marketed"},
    ])


# ── Coverage 1: golden matching on the real committed slice ───────────────────

class TestGoldenMatching:
    """Each anchored DIN's latest-MAT Dollars/Units must equal the hand-verified
    anchors EXACTLY after collapse + match against the real slice."""

    @pytest.fixture(scope="class")
    def enriched(self):
        enr, _ = match_iqvia_to_sheet1(_progesterone_sheet1(), _slice_collapsed())
        return enr

    @pytest.mark.parametrize("din", sorted(_GOLDEN))
    def test_dollars_exact(self, enriched, din):
        row = enriched[enriched["din"] == din]
        assert len(row) == 1
        assert int(row[_DOLLARS].iloc[0]) == _GOLDEN[din][0]

    @pytest.mark.parametrize("din", sorted(_GOLDEN))
    def test_units_exact(self, enriched, din):
        row = enriched[enriched["din"] == din]
        assert int(row[_UNITS].iloc[0]) == _GOLDEN[din][1]

    def test_row_count_unchanged(self, enriched):
        assert len(enriched) == len(_progesterone_sheet1())


# ── Coverage 2: match-confidence column ───────────────────────────────────────

class TestMatchConfidence:
    """attach_match_confidence derives the 'iqvia_match_confidence' column from the
    reconciliation output only. Exact-brand matches read 'exact'; a fuzzy
    house-brand match below LOW_CONFIDENCE_SCORE reads 'low', and low_count (the
    KPI) equals the number of 'low' rows."""

    def test_unit_confidence_mapping(self):
        # Verbatim contract from _confidence_for — pinned, not re-derived.
        assert _confidence_for("matched", 100.0, "exact-brand match; score=100") == "exact"
        assert _confidence_for("matched", 90.0, "generic-label alias aggregated onto exact-brand DIN X") == "exact"
        assert _confidence_for("matched", 90.0, "score=90") == "high"
        assert _confidence_for("matched", 70.0, "score=70") == "low"
        assert _confidence_for("low_score", 60.0, "Top score 60") == "low"
        assert _confidence_for("din_no_iqvia_match", None, "") == "none"

    def test_exact_brand_dins_labeled_exact(self):
        enr, recon = match_iqvia_to_sheet1(_progesterone_sheet1(), _slice_collapsed())
        df, low = attach_match_confidence(enr, recon)
        assert "iqvia_match_confidence" in df.columns
        for din in ("02516187", "02493578", "00585092"):
            label = df[df["din"] == din]["iqvia_match_confidence"].iloc[0]
            assert label == "exact", f"DIN {din} confidence={label!r}, expected exact"
        # No fuzzy/low rows in this all-exact set.
        assert low == 0

    def test_fuzzy_house_brand_labeled_low_and_counted(self):
        """A genuine fuzzy match (brand spelled differently, same company) against
        the real REDDY-PROGESTERONE / DR REDDYS slice group scores ~82 (in the
        65–85 band) → 'low'; low_count must equal the number of 'low' rows."""
        sheet1 = pd.DataFrame([
            # Fuzzy: brand "REDDY PROG" ≠ slice "REDDY-PROGESTERONE" so it is NOT
            # reserved as exact-brand; company matches → fuzzy match in the low band.
            {"din": "09999991", "ingredient": "PROGESTERONE",
             "brand_name": "REDDY PROG", "company": "DR REDDYS LAB INC",
             "strength": "100 MG", "status": "Marketed"},
            # Exact-brand control row (AURO) to confirm 'low' counts only the fuzzy one.
            {"din": "02493578", "ingredient": "PROGESTERONE",
             "brand_name": "AURO-PROGESTERONE", "company": "AURO PHARMA INC",
             "strength": "100 MG", "status": "Marketed"},
        ])
        enr, recon = match_iqvia_to_sheet1(sheet1, _slice_collapsed())
        # The fuzzy DIN must have actually matched (carrying metrics) yet be 'low'.
        fuzzy_row = recon[recon["din"] == "09999991"]
        assert len(fuzzy_row) == 1
        assert fuzzy_row["status"].iloc[0] == "matched"
        assert float(fuzzy_row["top_score"].iloc[0]) < 85.0

        df, low = attach_match_confidence(enr, recon)
        assert df[df["din"] == "09999991"]["iqvia_match_confidence"].iloc[0] == "low"
        assert df[df["din"] == "02493578"]["iqvia_match_confidence"].iloc[0] == "exact"
        # KPI: low_count == number of 'low'-labeled rows.
        assert low == int((df["iqvia_match_confidence"] == "low").sum())
        assert low == 1


# ── Coverage 3: status partitioning at scale ──────────────────────────────────

class TestStatusPartitioning:
    """Against the real slice the reconciliation must surface the ambiguous
    PROVERA/5MG outcome (no DIN assigned) plus the unmatched partitions, with no
    metric leakage onto any unmatched DIN."""

    @pytest.fixture(scope="class")
    def result(self):
        return match_iqvia_to_sheet1(_progesterone_sheet1(), _slice_collapsed())

    def test_provera_5mg_ambiguous_no_din(self, result):
        _, recon = result
        ambig = recon[
            (recon["status"] == "ambiguous")
            & (recon["iqvia_product"] == "PROVERA")
            & (recon["iqvia_strength"] == "5MG")
        ]
        assert len(ambig) == 1
        # Ambiguous group assigns to no DIN.
        assert str(ambig["din"].iloc[0] or "").strip() == ""

    def test_both_provera_dins_blank(self, result):
        enr, _ = result
        for din in ("00030937", "02010739"):
            val = enr[enr["din"] == din][_DOLLARS].iloc[0]
            assert val is None or pd.isna(val), f"ambiguous DIN {din} must stay blank"

    def test_unmatched_partitions_present(self, result):
        _, recon = result
        statuses = set(recon["status"].unique())
        # IQVIA groups with no DIN in Sheet 1 (CYTEX/HIKMA progesterone 50MG/ML etc.)
        assert "no_din_match" in statuses
        # The 5 Sheet-1 DINs include unmatched ones (the two ambiguous PROVERA DINs),
        # so at least one din_no_iqvia_match row is expected too.
        assert "din_no_iqvia_match" in statuses

    def test_no_metric_leak_on_unmatched_dins(self, result):
        enr, recon = result
        mc = detect_metric_columns(_slice_collapsed())
        matched_dins = set(
            recon[recon["status"] == "matched"]["din"].astype(str).str.strip()
        ) - {""}
        for _, r in enr.iterrows():
            din = str(r.get("din", "") or "").strip()
            if din in matched_dins:
                continue
            for col in mc:
                val = r[col]
                assert val is None or pd.isna(val), (
                    f"unmatched DIN {din} leaked metric {col!r}={val!r} (must be None/NaN)"
                )


# ── Coverage 4: IQVIA-absent graceful disable ─────────────────────────────────

def _dpd_response(records: list[DrugRecord]) -> SearchResponse:
    from datetime import datetime, timezone
    return SearchResponse(
        metadata=SearchMetadata(
            query="x", field="ingredient",
            timestamp=datetime.now(timezone.utc).isoformat(),
        ),
        sources=[SourceResult(source="DPD", status="ok", records=records)],
    )


class TestIqviaAbsentGracefulDisable:
    """With no IQVIA frame the universe sheet must carry NO metric columns and a
    zero KPI; the screen's IQVIA criteria must raise (never silently pass on
    zeros)."""

    def _universe_no_iqvia(self):
        resp = _dpd_response([
            DrugRecord(
                source="DPD", ingredient="PROGESTERONE", brand_name="PROGESTERONE",
                company="SANIS HEALTH INC", din="02516187", strength="100 MG",
                dosage_form="Capsule", status="Marketed",
                source_specific={"drug_code": 1},
            ),
        ])
        return build_universe_sheet1(resp, iqvia_df=None)

    def test_no_metric_columns(self):
        df, recon, low = self._universe_no_iqvia()
        assert not detect_metric_columns(df), "no IQVIA → no MAT metric columns"

    def test_low_count_zero_and_recon_empty(self):
        df, recon, low = self._universe_no_iqvia()
        assert low == 0
        assert recon.empty

    def test_confidence_column_all_none(self):
        df, _, _ = self._universe_no_iqvia()
        # The confidence column still exists but no row is fuzzy/low.
        assert "iqvia_match_confidence" in df.columns
        assert (df["iqvia_match_confidence"] != "low").all()

    @pytest.mark.parametrize("metric", ["value", "quantity", "quantity_ext"])
    def test_screen_iqvia_criterion_raises_without_columns(self, metric):
        # Sheet 1 with NO Dollars/Units/Ext Units MAT columns at all.
        sheet1 = pd.DataFrame([{
            "din": "02516187", "ingredient": "PROGESTERONE",
            "brand_name": "PROGESTERONE", "company": "SANIS HEALTH INC",
            "strength": "100 MG", "dosage_form": "Capsule", "status": "Marketed",
        }])
        sheet2 = pd.DataFrame(columns=[
            "ingredient_name", "medicinal_ingredient", "company",
            "therapeutic_area", "year_month_accepted", "status",
        ])
        with pytest.raises(ValueError, match="IQVIA"):
            build_filtered_workbook(sheet1, sheet2, [Criterion(metric, "above", 0)])

    def test_screen_non_iqvia_criterion_does_not_raise(self):
        """A non-IQVIA criterion (competitors) must NOT be blocked by the guard."""
        sheet1 = pd.DataFrame([{
            "din": "02516187", "ingredient": "PROGESTERONE",
            "brand_name": "PROGESTERONE", "company": "SANIS HEALTH INC",
            "strength": "100 MG", "dosage_form": "Capsule", "status": "Marketed",
        }])
        sheet2 = pd.DataFrame(columns=[
            "ingredient_name", "medicinal_ingredient", "company",
            "therapeutic_area", "year_month_accepted", "status",
        ])
        xlsx, summary, detail, warnings = build_filtered_workbook(
            sheet1, sheet2, [Criterion("competitors", "above", 0)]
        )
        assert isinstance(xlsx, (bytes, bytearray)) and len(xlsx) > 0


# ── Coverage 5: realistic-scale matching (timing surfaced in its own test) ─────

# Row count: 4,000 synthetic DINs in Sheet 1; 1,000 collapsed IQVIA groups (every
# 4th DIN). Kept in a dedicated test so its wall-clock duration is visible in -q
# timing output and not blended with the small hermetic cases above.
_SCALE_N = 4000


def _molname(i: int) -> str:
    """Distinct 6-letter alpha token per i (no digits) → a unique molecule whose
    >=4-letter run is unique, so _molecule_overlap pairs exactly one DIN per group."""
    s, x = "", i
    for _ in range(6):
        s += chr(ord("A") + x % 26)
        x //= 26
    return "MOL" + s


def _build_scale_inputs():
    s1_rows, iq_rows = [], []
    for i in range(_SCALE_N):
        din = f"{90_000_000 + i:08d}"
        brand = f"ZBRAND{i}"
        company = f"ZCOMPANY{i} INC"
        strength = f"{i + 1}MG"          # unique strength per row
        molecule = _molname(i)           # unique molecule token per row
        s1_rows.append({
            "din": din, "ingredient": molecule, "brand_name": brand,
            "company": company, "strength": strength, "status": "Marketed",
        })
        if i % 4 == 0:                    # known matching subset: 1,000 groups
            iq_rows.append({
                "Combined Molecule": molecule, "Product": brand,
                "Manufacturer": company, "Strength": strength,
                "Dollars MAT 12/2025": 1000 + i, "Units MAT 12/2025": 10 + i,
            })
    return pd.DataFrame(s1_rows), pd.DataFrame(iq_rows)


class TestMatchingScale:
    @pytest.fixture(scope="class")
    def scale_run(self):
        sheet1, iqvia = _build_scale_inputs()
        t0 = time.perf_counter()
        enriched, recon = match_iqvia_to_sheet1(sheet1, iqvia)
        elapsed = time.perf_counter() - t0
        return sheet1, iqvia, enriched, recon, elapsed

    def test_completes_in_reasonable_time(self, scale_run):
        _, iqvia, enriched, _, elapsed = scale_run
        # (a) it completes and produces one enriched row per Sheet-1 DIN.
        assert len(enriched) == _SCALE_N
        # Generous ceiling — the prototype ran ~1.2s; this only catches pathology.
        assert elapsed < 30.0, f"scale match took {elapsed:.2f}s (>30s)"

    def test_known_matches_attach_correct_metrics(self, scale_run):
        _, _, enriched, _, _ = scale_run
        # i=0 → din 90000000, dollars 1000; i=4 → din 90000004, dollars 1004.
        assert int(enriched[enriched["din"] == "90000000"][_DOLLARS].iloc[0]) == 1000
        assert int(enriched[enriched["din"] == "90000004"][_DOLLARS].iloc[0]) == 1004
        # A non-matching DIN (i=1) stays blank.
        v = enriched[enriched["din"] == "90000001"][_DOLLARS].iloc[0]
        assert v is None or pd.isna(v)

    def test_all_groups_match(self, scale_run):
        _, iqvia, _, recon, _ = scale_run
        matched = recon[recon["status"] == "matched"]
        assert len(matched) == len(iqvia)

    def test_recon_row_count_is_bounded(self, scale_run):
        """Every IQVIA group yields exactly one recon row, plus one recon row per
        DIN that received no group. matched=groups (1,000), so unmatched DINs =
        N - 1,000 = 3,000, and total recon rows = 1,000 + 3,000 = N."""
        sheet1, iqvia, _, recon, _ = scale_run
        n_matched = int((recon["status"] == "matched").sum())
        n_unmatched_din = _SCALE_N - n_matched
        assert len(recon) == len(iqvia) + n_unmatched_din
        assert len(recon) == _SCALE_N
