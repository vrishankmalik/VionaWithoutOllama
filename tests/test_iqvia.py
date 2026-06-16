"""Tests for IQVIA parse / collapse / match pipeline.

Verification anchors (computed from the real IQVIA_SAMPLE_progesterone.xlsx):
  DIN 02516187 (SANIS / PROGESTERONE / 100MG):
    Units MAT 12/2025 = 218591, Dollars MAT 12/2025 = 21215081
  DIN 02493578 (AURO / AURO-PROGESTERONE / 100MG):
    Units MAT 12/2025 = 233159, Dollars MAT 12/2025 = 13005865
  DIN 00585092 (PFIZER / DEPO-PROVERA / 150MG -> 150MG/ML in IQVIA):
    Units MAT 12/2025 = 262834, Dollars MAT 12/2025 = 8853659

Ambiguous case:
  IQVIA "PROVERA / PFIZER / 5MG" → two candidate DINs (00030937 PROVERA 5MG
  and 02010739 PROVERA PAK 5MG) → neither DIN should receive data.

Unmatched IQVIA groups:
  "PROGESTERONE / CYTEX / 50MG/ML" and "PROGESTERONE / HIKMA / 50MG/ML"
  have no DIN in the sample Sheet 1 → status = no_din_match.
"""
import io
import pytest
import pandas as pd

from app.enrichment.iqvia import (
    parse_iqvia,
    collapse_iqvia,
    detect_metric_columns,
    match_iqvia_to_sheet1,
    _norm_strength,
    _norm_company,
    _norm_brand,
)

SAMPLE_PATH = "/Users/vmalik/Downloads/IQVIA_SAMPLE_progesterone.xlsx"
COMBINATIONS_PATH = "/Users/vmalik/Downloads/IQVIA_SAMPLE_combinations.xlsx"


@pytest.fixture(scope="module")
def iqvia_raw():
    with open(SAMPLE_PATH, "rb") as fh:
        raw = parse_iqvia(fh.read())
    return raw


@pytest.fixture(scope="module")
def iqvia_collapsed(iqvia_raw):
    return collapse_iqvia(iqvia_raw)


# ── Unit tests: normalisation helpers ────────────────────────────────────────

class TestNormStrength:
    def test_simple(self):
        assert _norm_strength("100 MG") == frozenset({"100MG"})

    def test_strips_space_in_percent(self):
        assert _norm_strength("8 %") == frozenset({"8%"})

    def test_dpd_semicolon_combo(self):
        assert _norm_strength("1 MG; 100 MG") == frozenset({"1MG", "100MG"})

    def test_iqvia_slash_combo(self):
        assert _norm_strength("100MG/1MG") == frozenset({"100MG", "1MG"})

    def test_combo_order_irrelevant(self):
        assert _norm_strength("1MG/100MG") == _norm_strength("100MG/1MG")

    def test_concentration_drops_ml(self):
        # DEPO-PROVERA: IQVIA "150MG/ML" should match DPD "150 MG"
        assert _norm_strength("150MG/ML") == frozenset({"150MG"})
        assert _norm_strength("150MG/ML") == _norm_strength("150 MG")

    def test_concentration_mg_per_g_converts_to_percent(self):
        # 50 MG/G = 5 % w/w; code converts MG/G to % before dropping the denominator.
        assert _norm_strength("50MG/G") == frozenset({"5%"})

    def test_empty(self):
        assert _norm_strength("") == frozenset()
        assert _norm_strength(None) == frozenset()


class TestNormCompany:
    def test_strips_ulc(self):
        assert _norm_company("PFIZER CANADA ULC") == "pfizer"

    def test_strips_inc(self):
        # "pharma" is also stripped, leaving just "auro"
        assert _norm_company("AURO PHARMA INC") == "auro"

    def test_strips_ltd(self):
        assert _norm_company("TEVA CANADA LTD") == "teva"

    def test_pfizer_bare(self):
        assert _norm_company("PFIZER") == "pfizer"

    def test_knight(self):
        # "THERAPEUTICS" is NOT in the strip list — that's fine, sim still works.
        norm = _norm_company("KNIGHT THERAPEUTICS INC.")
        assert "knight" in norm

    def test_same_after_strip(self):
        assert _norm_company("PFIZER CANADA ULC") == _norm_company("PFIZER")


class TestNormBrand:
    def test_strips_trailing_strength(self):
        assert _norm_brand("PROVERA 5MG TABLETS") == "provera"

    def test_leaves_pak(self):
        # After stripping "5MG", what's left?
        # "PROVERA PAK 5MG" → trailing "5MG" stripped → "PROVERA PAK"
        assert _norm_brand("PROVERA PAK 5MG") == "provera pak"

    def test_lowercase(self):
        assert _norm_brand("DEPO-PROVERA") == "depo-provera"

    def test_strips_bare_tablets(self):
        # DPD brand "APO-ABACAVIR-LAMIVUDINE TABLETS" — no digit before "TABLETS"
        # so the old digit-requiring pattern left "tablets" in place.
        assert _norm_brand("APO-ABACAVIR-LAMIVUDINE TABLETS") == "apo-abacavir-lamivudine"

    def test_strips_bare_capsules(self):
        assert _norm_brand("JAMP-SOMETHINGCAPS CAPSULES") == "jamp-somethingcaps"

    def test_does_not_strip_mid_word(self):
        # "tablets" mid-name must not be removed
        assert _norm_brand("TABLET-X DRUG") == "tablet-x drug"


class TestApprovedDinExclusion:
    """DINs with DPD status 'Approved' (never marketed) must not appear as IQVIA
    candidates. A never-launched DIN has no sales history and including it creates
    false near-ties against the correctly marketed sibling DIN.

    Regression anchor: DIN 02518287 (APO-ABACAVIR-LAMIVUDINE TABLETS, Approved)
    was scoring 92.6 against the IQVIA group for APO-ABACAVIR-LAMIVUDINE, creating
    a 7.4-point gap vs. the correct DIN 02399539 (Marketed, score 100). The
    TIE_MARGIN of 15 flagged this as ambiguous, so DIN 02399539 received no IQVIA
    data despite being the only marketed match.
    """

    def _abacavir_sheet1(self):
        return pd.DataFrame([
            # DIN 02399539 — APO-ABACAVIR-LAMIVUDINE, APOTEX, Marketed 2016-03-15
            {
                "din": "02399539",
                "ingredient": "ABACAVIR SULFATE; LAMIVUDINE",
                "brand_name": "APO-ABACAVIR-LAMIVUDINE",
                "company": "APOTEX INC",
                "strength": "600 MG; 300 MG",
                "status": "Marketed",
            },
            # DIN 02518287 — APO-ABACAVIR-LAMIVUDINE TABLETS, APOTEX, Approved (never marketed)
            {
                "din": "02518287",
                "ingredient": "ABACAVIR SULFATE; LAMIVUDINE",
                "brand_name": "APO-ABACAVIR-LAMIVUDINE TABLETS",
                "company": "APOTEX INC",
                "strength": "600 MG; 300 MG",
                "status": "Approved",
            },
            # DIN 02454513 — AURO-ABACAVIR/LAMIVUDINE, AURO PHARMA, Marketed
            {
                "din": "02454513",
                "ingredient": "ABACAVIR SULFATE; LAMIVUDINE",
                "brand_name": "AURO-ABACAVIR/LAMIVUDINE",
                "company": "AURO PHARMA INC",
                "strength": "600 MG; 300 MG",
                "status": "Marketed",
            },
        ])

    def _abacavir_iqvia(self):
        """Minimal collapsed IQVIA DataFrame for the APO-ABACAVIR group."""
        return pd.DataFrame([{
            "Combined Molecule": "ABACAVIR/LAMIVUDINE",
            "Product": "APO-ABACAVIR-LAMIVUDINE",
            "Manufacturer": "APOTEX INC",
            "Strength": "0.6GM/300MG",
            "Units MAT 12/2025": 5000,
            "Dollars MAT 12/2025": 249814,
        }])

    def test_marketed_din_gets_iqvia_data(self):
        """DIN 02399539 (Marketed) must receive the $249,814 — not left ambiguous."""
        enriched, _ = match_iqvia_to_sheet1(self._abacavir_sheet1(), self._abacavir_iqvia())
        row = enriched[enriched["din"] == "02399539"]
        assert len(row) == 1
        assert int(row["Dollars MAT 12/2025"].iloc[0]) == 249814

    def test_approved_din_gets_no_iqvia_data(self):
        """DIN 02518287 (Approved, never marketed) must receive no IQVIA data."""
        enriched, _ = match_iqvia_to_sheet1(self._abacavir_sheet1(), self._abacavir_iqvia())
        row = enriched[enriched["din"] == "02518287"]
        assert len(row) == 1
        val = row["Dollars MAT 12/2025"].iloc[0]
        assert val is None or pd.isna(val), (
            f"Approved DIN 02518287 must not receive IQVIA data; got {val!r}"
        )

    def test_approved_din_not_flagged_ambiguous_in_recon(self):
        """The IQVIA group must be matched (not ambiguous) once the Approved DIN is excluded."""
        _, recon = match_iqvia_to_sheet1(self._abacavir_sheet1(), self._abacavir_iqvia())
        abacavir_rows = recon[recon["iqvia_product"] == "APO-ABACAVIR-LAMIVUDINE"]
        assert len(abacavir_rows) == 1
        assert abacavir_rows["status"].iloc[0] == "matched", (
            f"Expected 'matched' but got {abacavir_rows['status'].iloc[0]!r} — "
            "Approved DIN 02518287 must be excluded from candidates"
        )


# ── Bare-number strength inference ───────────────────────────────────────────
# IQVIA encodes "160MG/12.5MG" as "160/12.5MG" — the unit is omitted on every
# component except the last.  _norm_strength must infer the unit from the
# last-component token and apply it to all bare-number tokens.

class TestNormStrengthBareNumber:
    def test_hctz_combo_two_components(self):
        # "160/12.5MG" → "DIOVAN HCT" style: valsartan 160mg + HCTZ 12.5mg
        assert _norm_strength("160/12.5MG") == frozenset({"160MG", "12.5MG"})

    def test_hctz_combo_three_components(self):
        # Hypothetical triple: "5/160/12.5MG" → 5MG + 160MG + 12.5MG
        assert _norm_strength("5/160/12.5MG") == frozenset({"5MG", "160MG", "12.5MG"})

    def test_high_dose_combo(self):
        # "320/25MG" (valsartan 320mg / HCTZ 25mg)
        assert _norm_strength("320/25MG") == frozenset({"320MG", "25MG"})

    def test_already_explicit_unchanged(self):
        # When every component already has its unit, no inference runs.
        assert _norm_strength("160MG/12.5MG") == frozenset({"160MG", "12.5MG"})

    def test_bare_number_matches_explicit(self):
        # "160/12.5MG" and "160MG/12.5MG" must produce identical frozensets.
        assert _norm_strength("160/12.5MG") == _norm_strength("160MG/12.5MG")

    def test_bare_number_with_mg_unit(self):
        # Bare-number inference works when the anchoring unit is MG (common IQVIA pattern).
        # "5/160MG" → both components get MG → {"5MG", "160MG"}
        assert _norm_strength("5/160MG") == frozenset({"5MG", "160MG"})

    def test_no_inference_without_unit(self):
        # If there is no unit anywhere, bare numbers stay as-is (can't infer).
        result = _norm_strength("160/12")
        # Both tokens have no unit — neither should gain a fabricated unit.
        # The frozenset should not contain tokens with "MG", "MCG", etc.
        for token in result:
            assert not any(u in token for u in ("MG", "MCG", "ML", "IU", "%")), (
                f"Fabricated unit in token {token!r} with no source unit"
            )


# ── Parsing and collapsing ────────────────────────────────────────────────────

class TestParseIqvia:
    def test_shape(self, iqvia_raw):
        assert len(iqvia_raw) == 392  # known sample size

    def test_metric_cols_detected(self, iqvia_raw):
        mc = detect_metric_columns(iqvia_raw)
        assert len(mc) == 12  # 4 years × 3 metrics
        assert "Units MAT 12/2025" in mc
        assert "Dollars MAT 12/2025" in mc

    def test_dash_converted_to_zero(self, iqvia_raw):
        # The raw file has '-' in many cells; they must be numeric after parsing.
        # Actual negative values (returns/corrections) are preserved as-is.
        mc = detect_metric_columns(iqvia_raw)
        for col in mc:
            assert iqvia_raw[col].dtype in ("int64", "int32", "float64")
        # Spot-check: a known all-zero row should not have NaN
        assert iqvia_raw[mc[0]].isna().sum() == 0


class TestCollapseIqvia:
    def test_row_count(self, iqvia_collapsed):
        # 22 unique (molecule, product, manufacturer, strength) groups
        assert len(iqvia_collapsed) == 22

    def test_sanis_units(self, iqvia_collapsed):
        row = iqvia_collapsed[
            (iqvia_collapsed["Product"] == "PROGESTERONE") &
            (iqvia_collapsed["Manufacturer"] == "SANIS HEALTH INC")
        ]
        assert len(row) == 1
        assert int(row["Units MAT 12/2025"].iloc[0]) == 218591

    def test_sanis_dollars(self, iqvia_collapsed):
        row = iqvia_collapsed[
            (iqvia_collapsed["Product"] == "PROGESTERONE") &
            (iqvia_collapsed["Manufacturer"] == "SANIS HEALTH INC")
        ]
        assert int(row["Dollars MAT 12/2025"].iloc[0]) == 21215081

    def test_auro_units(self, iqvia_collapsed):
        row = iqvia_collapsed[iqvia_collapsed["Product"] == "AURO-PROGESTERONE"]
        assert len(row) == 1
        assert int(row["Units MAT 12/2025"].iloc[0]) == 233159

    def test_auro_dollars(self, iqvia_collapsed):
        row = iqvia_collapsed[iqvia_collapsed["Product"] == "AURO-PROGESTERONE"]
        assert int(row["Dollars MAT 12/2025"].iloc[0]) == 13005865

    def test_depo_provera_units(self, iqvia_collapsed):
        # Combines SYRINGE + VIAL × Drugstore + Hospital (36 raw rows → 1 collapsed)
        row = iqvia_collapsed[iqvia_collapsed["Product"] == "DEPO-PROVERA"]
        assert len(row) == 1
        assert int(row["Units MAT 12/2025"].iloc[0]) == 262834

    def test_depo_provera_dollars(self, iqvia_collapsed):
        row = iqvia_collapsed[iqvia_collapsed["Product"] == "DEPO-PROVERA"]
        assert int(row["Dollars MAT 12/2025"].iloc[0]) == 8853659


# ── Matching ──────────────────────────────────────────────────────────────────

def _make_sheet1(rows: list[dict]) -> pd.DataFrame:
    """Build a minimal Sheet 1 DataFrame for matching tests."""
    return pd.DataFrame(rows)


class TestMatchIqvia:
    @pytest.fixture(scope="class")
    def sheet1_progesterone(self):
        """Minimal Sheet 1 representing a progesterone search result."""
        return _make_sheet1([
            # DIN 02516187 — SANIS HEALTH INC / PROGESTERONE / 100MG
            {
                "din": "02516187",
                "ingredient": "PROGESTERONE",
                "brand_name": "PROGESTERONE",
                "company": "SANIS HEALTH INC",
                "strength": "100 MG",
                "dosage_form": "Capsule",
            },
            # DIN 02493578 — AURO PHARMA INC / AURO-PROGESTERONE / 100MG
            {
                "din": "02493578",
                "ingredient": "PROGESTERONE",
                "brand_name": "AURO-PROGESTERONE",
                "company": "AURO PHARMA INC",
                "strength": "100 MG",
                "dosage_form": "Capsule",
            },
            # DIN 00585092 — PFIZER CANADA ULC / DEPO-PROVERA / 150 MG (injectable)
            {
                "din": "00585092",
                "ingredient": "MEDROXYPROGESTERONE ACETATE",
                "brand_name": "DEPO-PROVERA",
                "company": "PFIZER CANADA ULC",
                "strength": "150 MG",
                "dosage_form": "Injection",
            },
            # DIN 00030937 — PFIZER CANADA ULC / PROVERA 5MG TABLETS (ambiguous)
            {
                "din": "00030937",
                "ingredient": "MEDROXYPROGESTERONE ACETATE",
                "brand_name": "PROVERA 5MG TABLETS",
                "company": "PFIZER CANADA ULC",
                "strength": "5 MG",
                "dosage_form": "Tablet",
            },
            # DIN 02010739 — PFIZER CANADA ULC / PROVERA PAK 5MG (ambiguous)
            {
                "din": "02010739",
                "ingredient": "MEDROXYPROGESTERONE ACETATE",
                "brand_name": "PROVERA PAK 5MG",
                "company": "PFIZER CANADA ULC",
                "strength": "5 MG",
                "dosage_form": "Tablet",
            },
            # DIN 00262056 — veterinary; no IQVIA entry expected
            {
                "din": "00262056",
                "ingredient": "PROGESTERONE",
                "brand_name": "SYNOVEX S",
                "company": "ZOETIS CANADA INC",
                "strength": "200 MG",
                "dosage_form": "Implant",
            },
        ])

    def test_sanis_units_in_sheet1(self, sheet1_progesterone, iqvia_collapsed):
        enriched, _ = match_iqvia_to_sheet1(sheet1_progesterone, iqvia_collapsed)
        row = enriched[enriched["din"] == "02516187"]
        assert len(row) == 1
        assert int(row["Units MAT 12/2025"].iloc[0]) == 218591

    def test_sanis_dollars_in_sheet1(self, sheet1_progesterone, iqvia_collapsed):
        enriched, _ = match_iqvia_to_sheet1(sheet1_progesterone, iqvia_collapsed)
        row = enriched[enriched["din"] == "02516187"]
        assert int(row["Dollars MAT 12/2025"].iloc[0]) == 21215081

    def test_auro_units_in_sheet1(self, sheet1_progesterone, iqvia_collapsed):
        enriched, _ = match_iqvia_to_sheet1(sheet1_progesterone, iqvia_collapsed)
        row = enriched[enriched["din"] == "02493578"]
        assert int(row["Units MAT 12/2025"].iloc[0]) == 233159

    def test_auro_dollars_in_sheet1(self, sheet1_progesterone, iqvia_collapsed):
        enriched, _ = match_iqvia_to_sheet1(sheet1_progesterone, iqvia_collapsed)
        row = enriched[enriched["din"] == "02493578"]
        assert int(row["Dollars MAT 12/2025"].iloc[0]) == 13005865

    def test_depo_provera_units_in_sheet1(self, sheet1_progesterone, iqvia_collapsed):
        enriched, _ = match_iqvia_to_sheet1(sheet1_progesterone, iqvia_collapsed)
        row = enriched[enriched["din"] == "00585092"]
        assert int(row["Units MAT 12/2025"].iloc[0]) == 262834

    def test_depo_provera_dollars_in_sheet1(self, sheet1_progesterone, iqvia_collapsed):
        enriched, _ = match_iqvia_to_sheet1(sheet1_progesterone, iqvia_collapsed)
        row = enriched[enriched["din"] == "00585092"]
        assert int(row["Dollars MAT 12/2025"].iloc[0]) == 8853659

    def test_provera_5mg_ambiguous_din1(self, sheet1_progesterone, iqvia_collapsed):
        """DIN 00030937 (PROVERA 5MG) must remain blank — PROVERA 5MG is ambiguous."""
        enriched, _ = match_iqvia_to_sheet1(sheet1_progesterone, iqvia_collapsed)
        row = enriched[enriched["din"] == "00030937"]
        assert len(row) == 1
        val = row["Units MAT 12/2025"].iloc[0]
        assert val is None or (pd.isna(val))

    def test_provera_5mg_ambiguous_din2(self, sheet1_progesterone, iqvia_collapsed):
        """DIN 02010739 (PROVERA PAK 5MG) must remain blank — PROVERA 5MG is ambiguous."""
        enriched, _ = match_iqvia_to_sheet1(sheet1_progesterone, iqvia_collapsed)
        row = enriched[enriched["din"] == "02010739"]
        assert len(row) == 1
        val = row["Units MAT 12/2025"].iloc[0]
        assert val is None or (pd.isna(val))

    def test_synovex_no_iqvia_data(self, sheet1_progesterone, iqvia_collapsed):
        """Veterinary implant DIN 00262056 has no IQVIA match — cells must be None."""
        enriched, _ = match_iqvia_to_sheet1(sheet1_progesterone, iqvia_collapsed)
        row = enriched[enriched["din"] == "00262056"]
        assert len(row) == 1
        val = row["Units MAT 12/2025"].iloc[0]
        assert val is None or (pd.isna(val))

    def test_one_row_per_din(self, sheet1_progesterone, iqvia_collapsed):
        """Sheet 1 row count must not change after enrichment."""
        enriched, _ = match_iqvia_to_sheet1(sheet1_progesterone, iqvia_collapsed)
        assert len(enriched) == len(sheet1_progesterone)

    def test_reconciliation_contains_provera_ambiguous(self, sheet1_progesterone, iqvia_collapsed):
        _, recon = match_iqvia_to_sheet1(sheet1_progesterone, iqvia_collapsed)
        ambig = recon[
            (recon["status"] == "ambiguous") &
            (recon["iqvia_product"] == "PROVERA") &
            (recon["iqvia_strength"] == "5MG")
        ]
        assert len(ambig) >= 1

    def test_reconciliation_cytex_unmatched(self, sheet1_progesterone, iqvia_collapsed):
        """CYTEX PROGESTERONE 50MG/ML has no DIN in Sheet 1 → no_din_match."""
        _, recon = match_iqvia_to_sheet1(sheet1_progesterone, iqvia_collapsed)
        cytex = recon[
            (recon["status"] == "no_din_match") &
            (recon["iqvia_manufacturer"].str.upper() == "CYTEX")
        ]
        assert len(cytex) >= 1

    def test_reconciliation_hikma_unmatched(self, sheet1_progesterone, iqvia_collapsed):
        """HIKMA PROGESTERONE 50MG/ML has no DIN in Sheet 1 → no_din_match."""
        _, recon = match_iqvia_to_sheet1(sheet1_progesterone, iqvia_collapsed)
        hikma = recon[
            (recon["status"] == "no_din_match") &
            (recon["iqvia_manufacturer"].str.upper().str.contains("HIKMA"))
        ]
        assert len(hikma) >= 1

    def test_no_fabrication_zeros(self, sheet1_progesterone, iqvia_collapsed):
        """Unmatched DINs must have None/NaN, never 0, in metric columns."""
        enriched, _ = match_iqvia_to_sheet1(sheet1_progesterone, iqvia_collapsed)
        mc = detect_metric_columns(iqvia_collapsed)
        for col in mc:
            for din in ["00030937", "02010739", "00262056"]:
                row = enriched[enriched["din"] == din]
                val = row[col].iloc[0]
                # Must be None or NaN, NOT 0
                assert val is None or pd.isna(val), (
                    f"DIN {din} column {col!r} = {val!r} — should be None/NaN, not 0"
                )


# ── Company normalization: corporation / incorporated / limited ───────────────

class TestNormCompanyExtended:
    def test_strips_corporation(self):
        assert _norm_company("JAMP PHARMA CORPORATION") == "jamp"

    def test_strips_incorporated(self):
        assert _norm_company("SOME DRUG INCORPORATED") == "some drug"

    def test_strips_limited(self):
        assert _norm_company("TEVA CANADA LIMITED") == "teva"

    def test_strips_labs(self):
        assert _norm_company("PENDOPHARM LABS INC") == "pendopharm"

    def test_corporation_equals_corp(self):
        assert _norm_company("JAMP PHARMA CORP") == _norm_company("JAMP PHARMA CORPORATION")

    # ── French / Quebec legal suffixes ────────────────────────────────────────
    # "PRO DOC LIMITÉE / S.E.C." is the DPD-registered form; IQVIA writes "PRO DOC".
    # Without unicode normalisation + French suffix stripping the company_sim
    # drops from 100 to ~54, causing scores of ~77 instead of 100.

    def test_strips_limitee_accented(self):
        assert _norm_company("PRO DOC LIMITÉE") == "pro doc"

    def test_strips_limitee_plain(self):
        assert _norm_company("PRO DOC LIMITEE") == "pro doc"

    def test_strips_ltee(self):
        assert _norm_company("PRO DOC LTÉE") == "pro doc"

    def test_strips_sec_abbreviation(self):
        # "S.E.C." (société en commandite) — dots stripped first, then "sec" removed.
        assert _norm_company("PRO DOC LIMITÉE / S.E.C.") == "pro doc"

    def test_strips_ampersand(self):
        # "&" must be removed so "Smith & Nephew" and "Smith Nephew" normalise alike.
        assert _norm_company("SMITH & NEPHEW INC") == "smith nephew"

    def test_unicode_normalisation_general(self):
        # "é" must fold to "e" so accented and unaccented versions compare equal.
        # "PRO DOC LIMITÉE" (accented) vs "PRO DOC LIMITEE" (plain) must normalise identically.
        assert _norm_company("PRO DOC LIMITÉE") == _norm_company("PRO DOC LIMITEE")

    def test_limitee_equals_limited(self):
        # French "Limitée" and English "Limited" must reduce to the same root.
        assert _norm_company("PRO DOC LIMITÉE") == _norm_company("PRO DOC LIMITED")


# ── Claimed-DIN exclusion: DINs matched to an earlier IQVIA group must not ───
# reappear as candidates for later groups, preventing false near-ties.         ─

class TestClaimedDinExclusion:
    """Regression for DIN 02497654 (JAMP ABACAVIR / LAMIVUDINE, JAMP PHARMA
    CORPORATION).  Before the fix, the alphabetically-earlier APO group claimed
    DIN 02399539 first.  02399539 then still appeared in JAMP's candidate list
    (score 62.6), creating a near-tie gap of 5 against the correct JAMP DIN
    (score 68) — below TIE_MARGIN=15 → falsely flagged ambiguous.

    After the fix, claimed DINs are excluded from later groups' candidate lists,
    leaving 02497654 as the sole candidate → unambiguously matched.
    """

    @pytest.fixture(scope="class")
    def abacavir_sheet1(self):
        return pd.DataFrame([
            {"din": "02399539", "ingredient": "ABACAVIR SULFATE; LAMIVUDINE",
             "brand_name": "APO-ABACAVIR-LAMIVUDINE",    "company": "APOTEX INC",
             "strength": "600 MG; 300 MG", "status": "Marketed"},
            {"din": "02454513", "ingredient": "ABACAVIR SULFATE; LAMIVUDINE",
             "brand_name": "AURO-ABACAVIR/LAMIVUDINE",   "company": "AURO PHARMA INC",
             "strength": "600 MG; 300 MG", "status": "Marketed"},
            {"din": "02497654", "ingredient": "ABACAVIR SULFATE; LAMIVUDINE",
             "brand_name": "JAMP ABACAVIR / LAMIVUDINE", "company": "JAMP PHARMA CORPORATION",
             "strength": "600 MG; 300 MG", "status": "Marketed"},
            {"din": "02518287", "ingredient": "ABACAVIR SULFATE; LAMIVUDINE",
             "brand_name": "APO-ABACAVIR-LAMIVUDINE TABLETS", "company": "APOTEX INC",
             "strength": "600 MG; 300 MG", "status": "Approved"},
        ])

    @pytest.fixture(scope="class")
    def combinations_iqvia(self):
        with open(COMBINATIONS_PATH, "rb") as f:
            return collapse_iqvia(parse_iqvia(f.read()))

    def test_jamp_din_gets_iqvia_data(self, abacavir_sheet1, combinations_iqvia):
        """DIN 02497654 (JAMP ABACAVIR / LAMIVUDINE) must be matched — not ambiguous."""
        enriched, _ = match_iqvia_to_sheet1(abacavir_sheet1, combinations_iqvia)
        row = enriched[enriched["din"] == "02497654"]
        assert len(row) == 1
        dollars = row["Dollars MAT 12/2022"].iloc[0]
        assert dollars is not None and not pd.isna(dollars), (
            "DIN 02497654 got no IQVIA data — claimed-DIN exclusion may not be working"
        )

    def test_jamp_not_ambiguous_in_recon(self, abacavir_sheet1, combinations_iqvia):
        """The JAMP IQVIA group must have status='matched', not 'ambiguous'."""
        _, recon = match_iqvia_to_sheet1(abacavir_sheet1, combinations_iqvia)
        jamp_rows = recon[recon["iqvia_product"] == "JAMP ABACAVIR/LAMIVUDINE"]
        assert len(jamp_rows) == 1
        assert jamp_rows["status"].iloc[0] == "matched", (
            f"JAMP group status = {jamp_rows['status'].iloc[0]!r}; "
            "expected 'matched' — claimed DIN 02399539 must not pollute JAMP's candidate list"
        )

    def test_apo_din_matched(self, abacavir_sheet1, combinations_iqvia):
        """DIN 02399539 (APO) must be matched to its own IQVIA group."""
        enriched, _ = match_iqvia_to_sheet1(abacavir_sheet1, combinations_iqvia)
        row = enriched[enriched["din"] == "02399539"]
        assert not pd.isna(row["Dollars MAT 12/2022"].iloc[0])

    def test_auro_din_matched(self, abacavir_sheet1, combinations_iqvia):
        """DIN 02454513 (AURO) must be matched to its own IQVIA group."""
        enriched, _ = match_iqvia_to_sheet1(abacavir_sheet1, combinations_iqvia)
        row = enriched[enriched["din"] == "02454513"]
        assert not pd.isna(row["Dollars MAT 12/2022"].iloc[0])

    def test_all_three_matched_in_recon(self, abacavir_sheet1, combinations_iqvia):
        """All three marketed abacavir/lamivudine DINs must each match a distinct group."""
        _, recon = match_iqvia_to_sheet1(abacavir_sheet1, combinations_iqvia)
        matched = recon[recon["status"] == "matched"]
        matched_dins = set(matched["din"].tolist())
        for expected_din in ("02399539", "02454513", "02497654"):
            assert expected_din in matched_dins, (
                f"DIN {expected_din} not in matched set {matched_dins}"
            )

    def test_no_cross_din_data_bleed(self, abacavir_sheet1, combinations_iqvia):
        """Each DIN must receive only its own IQVIA group's data — no zeros, no bleeds."""
        enriched, _ = match_iqvia_to_sheet1(abacavir_sheet1, combinations_iqvia)
        for din in ("02399539", "02454513", "02497654"):
            row = enriched[enriched["din"] == din]
            dollars = row["Dollars MAT 12/2022"].iloc[0]
            assert dollars is not None and not pd.isna(dollars) and dollars > 0, (
                f"DIN {din} has bad Dollars value: {dollars!r}"
            )
