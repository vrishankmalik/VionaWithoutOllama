"""Permanent regression tests for the Full-universe feature (options 3 & 4).

Fully offline — synthetic DPD/NOC/GSUR records, a synthetic collapsed IQVIA frame,
and monkeypatched loaders/labeling.  No network, no PDFs.

Locks the guarantees agreed for this additive feature:
  1. build_sheet1 default flag is unchanged (options 1 & 2 frozen); the index HTML
     still wires the Search tab to the same endpoints/params.
  2. include_dpd_only=True keeps grandfathered / DPD-only DINs, with NOC / patent /
     data-protection blank but full DPD identity + IQVIA sizing retained.
  3. build_sheet1 default still yields DPD ∩ NOC (DPD-only dropped).
  4. All six criteria compute on the no-PDF layer; the filter stage triggers no
     PM PDF fetch.
  5. Option 4 enriches ONLY survivor DINs (filtered-out DINs are never fetched).
  6. The universe build is cached for 4 h: option 3 → option 4 within the window
     builds once; after TTL it rebuilds; reset_universe_cache() invalidates it.
  7. The IQVIA match-confidence column populates and the low-confidence KPI counts
     the fuzzy house-brand (PRO DOC / JAMP / Pharmascience) matches.
"""
from __future__ import annotations

import asyncio

import pandas as pd

from app.enrichment import universe as U
from app.enrichment.workbook import build_sheet1
from app.models import DrugRecord, SearchMetadata, SearchResponse, SourceResult

_DOLLARS = "Dollars MAT 12/2025"
_UNITS = "Units MAT 12/2025"
_EXT = "Ext Units MAT 12/2025"


# ── builders ──────────────────────────────────────────────────────────────────
def _dpd(din, ingredient, brand, company, strength, status, form, code):
    return DrugRecord(
        source="DPD", din=din, ingredient=ingredient, brand_name=brand,
        company=company, strength=strength, status=status, dosage_form=form,
        route="Oral", all_ingredients=[ingredient.split(" ")[0]],
        source_specific={"drug_code": code},
    )


def _noc(din):
    return DrugRecord(
        source="NOC", din=din,
        source_specific={
            "submission_type": "New Drug Submission (NDS)",
            "noc_date": "2020-01-01", "submission_class": "NDS",
            "reason_for_supplement": "", "therapeutic_class": "X",
        },
    )


def _response(dpd_records, noc_records=(), gsur_records=()):
    return SearchResponse(
        metadata=SearchMetadata(query="Full DPD Universe", field="ingredient", timestamp="t"),
        sources=[
            SourceResult(source="DPD", status="ok", records=list(dpd_records)),
            SourceResult(source="NOC", status="ok", records=list(noc_records)),
            SourceResult(source="GenericSubmissions", status="ok", records=list(gsur_records)),
        ],
    )


def _iqvia_collapsed(rows):
    """rows: list of (molecule, product, manufacturer, strength, dollars, units, ext)."""
    return pd.DataFrame(
        [{"Combined Molecule": m, "Product": p, "Manufacturer": mf, "Strength": s,
          _DOLLARS: d, _UNITS: u, _EXT: e} for (m, p, mf, s, d, u, e) in rows]
    )


# ── 1. Options 1 & 2 frozen ───────────────────────────────────────────────────
def test_default_flag_identical_and_drops_dpd_only():
    resp = _response(
        dpd_records=[
            _dpd("11111111", "MOLX 100 MG", "BRANDX", "ALPHA INC", "100 MG", "Marketed", "Tablet", 1),
            _dpd("22222222", "ASA 81 MG", "ASPIRIN", "BAYER INC", "81 MG", "Marketed", "Tablet", 2),
        ],
        noc_records=[_noc("11111111")],
    )
    default = build_sheet1(resp)
    explicit_false = build_sheet1(resp, include_dpd_only=False)
    # Default == explicit-False, byte-for-byte (frame equality).
    pd.testing.assert_frame_equal(default, explicit_false)
    # Default keeps only the DPD ∩ NOC DIN; the DPD-only DIN is dropped.
    assert set(default["din"]) == {"11111111"}


def test_index_html_wires_frozen_and_universe_endpoints():
    from app.main import _HTML_UI as html
    # Frozen Search-tab wiring is intact.
    for needle in ("doExport(false)", "doExport(true)", "/export/start",
                   "filter_criteria", "queries, field"):
        assert needle in html, f"frozen wiring missing: {needle!r}"
    # New Full-universe tab wiring is present and distinct.
    for needle in ("/universe/start", "doUniverse('full')", "doUniverse('filter_enrich')"):
        assert needle in html, f"universe wiring missing: {needle!r}"


# ── 2. Universe keeps DPD-only rows; NOC/patent/DP blank; DPD + IQVIA retained ──
def test_universe_includes_dpd_only_with_blank_noc_and_iqvia_sizing():
    resp = _response(
        dpd_records=[
            _dpd("11111111", "PROGESTERONE 100 MG", "PROGESTERONE", "SANIS HEALTH INC",
                 "100 MG", "Marketed", "Capsule", 1),
            # Grandfathered / DPD-only (no NOC record at all).
            _dpd("22222222", "ASA 81 MG", "ASPIRIN", "BAYER INC", "81 MG", "Marketed", "Tablet", 2),
        ],
        noc_records=[_noc("11111111")],
    )
    iq = _iqvia_collapsed([("PROGESTERONE", "PROGESTERONE", "SANIS HEALTH", "100 MG",
                            21215081, 218591, 100)])
    df, recon, low = U.build_universe_sheet1(resp, iq)

    by_din = {r["din"]: r for _, r in df.iterrows()}
    assert set(by_din) == {"11111111", "22222222"}, "DPD-only DIN must be kept"

    grand = by_din["22222222"]
    # Full DPD identity retained.
    assert grand["brand_name"] == "ASPIRIN" and grand["company"] == "BAYER INC"
    assert grand["status"] == "Marketed"
    # NOC blank (cite-or-blank — no sentinel string).
    for col in U._NOC_COLS:
        assert (grand[col] in ("", None)), f"{col} should be blank for DPD-only row"
    # Patent + data-protection blank.
    assert grand.get("patent_number") in (None, "")
    assert grand.get("data_protection_ends") in (None, "")
    # IQVIA sizing attaches to the matched DIN via DPD-native keys only.
    assert by_din["11111111"][_DOLLARS] == 21215081


# ── 3. Scoped bypass: default flag still DPD ∩ NOC ─────────────────────────────
def test_scoped_bypass_default_is_intersection_only():
    resp = _response(
        dpd_records=[
            _dpd("11111111", "MOLX 100 MG", "BRANDX", "ALPHA INC", "100 MG", "Marketed", "Tablet", 1),
            _dpd("22222222", "MOLY 50 MG", "BRANDY", "BETA INC", "50 MG", "Marketed", "Tablet", 2),
        ],
        noc_records=[_noc("11111111")],
    )
    assert set(build_sheet1(resp)["din"]) == {"11111111"}
    assert set(build_sheet1(resp, include_dpd_only=True)["din"]) == {"11111111", "22222222"}


# ── 4. Six criteria compute on the no-PDF layer; filter triggers no PDF fetch ──
def test_six_criteria_compute_no_pdf(monkeypatch):
    from app.enrichment import screen, labeling

    called = {"pdf": False}

    async def _boom(*a, **k):  # any PM PDF fetch during filtering is a bug
        called["pdf"] = True
        return {}

    monkeypatch.setattr(labeling, "enrich_labeling_batch_fast", _boom)

    resp = _response(
        dpd_records=[
            _dpd("11111111", "PROGESTERONE 100 MG", "PROGESTERONE", "SANIS HEALTH INC",
                 "100 MG", "Marketed", "Capsule", 1),
            _dpd("22222222", "PROGESTERONE 100 MG", "AURO-PROGESTERONE", "AURO PHARMA INC",
                 "100 MG", "Marketed", "Capsule", 2),
        ],
    )
    iq = _iqvia_collapsed([
        ("PROGESTERONE", "PROGESTERONE", "SANIS HEALTH", "100 MG", 21215081, 218591, 100),
        ("PROGESTERONE", "AURO-PROGESTERONE", "AURO PHARMA", "100 MG", 13005865, 233159, 200),
    ])
    sheet1, _recon, _low = U.build_universe_sheet1(resp, iq)
    sheet2 = pd.DataFrame([{"medicinal_ingredient": "progesterone", "company": "GenA"}])

    products, _w = screen.compute_products(sheet1, sheet2)
    row = products[products["dosage_form"] == "CAPSULE"].iloc[0]  # key is case-canonical
    # All six computed, including the three IQVIA-sized ones — no PDF involved.
    assert row["competitors"] == 2
    assert row["filings"] == 1
    assert row["approvals"] == 2
    assert row["value_sizeable"] == 21215081 + 13005865
    assert row["quantity_sizeable"] == 218591 + 233159
    assert row["quantity_ext_sizeable"] == 100 + 200

    criteria = screen.parse_criteria([{"metric": "value", "operator": "above", "value": 1000}])
    xlsx, summary, detail, _warn = screen.build_filtered_workbook(sheet1, sheet2, criteria)
    assert xlsx and len(summary) == 1
    assert called["pdf"] is False, "filter stage must not fetch any PM PDF"


# ── 5. Option 4 enriches ONLY survivors ───────────────────────────────────────
def test_option4_enriches_only_survivors(monkeypatch):
    from app import universe_job
    from app.jobs import create_job

    bundle = U.UniverseBundle(
        dpd_records=[
            _dpd("10000001", "MOLX 100 MG", "BRANDA1", "COMPANY ALPHA INC", "100 MG", "Marketed", "Tablet", 5001),
            _dpd("10000002", "MOLX 100 MG", "BRANDA2", "COMPANY BETA INC", "100 MG", "Marketed", "Tablet", 5002),
            _dpd("20000003", "MOLY 50 MG", "BRANDB", "COMPANY GAMMA INC", "50 MG", "Marketed", "Capsule", 5003),
        ],
        gsur_records=[],
    )

    async def _fake_universe(force_refresh=False):
        return bundle

    seen: dict[str, tuple] = {}

    async def _fake_enrich(din_map, enable_ocr=None, concurrency=8, on_progress=None):
        seen.update(din_map)
        return {}

    monkeypatch.setattr(universe_job, "get_universe", _fake_universe)
    monkeypatch.setattr(universe_job, "enrich_labeling_batch_fast", _fake_enrich)
    monkeypatch.setattr(universe_job, "_resolve_iqvia", lambda job: None)

    # competitors > 1 → only the (MOLX, Tablet) product (2 marketed companies) passes.
    job = create_job("u4", "Full universe", "ingredient",
                     filter_criteria=[{"metric": "competitors", "operator": "above", "value": 1}])
    asyncio.run(universe_job.run_universe_filter_enrich_job(job, enable_ocr=False))

    assert job.status == "complete", job.error
    assert set(seen) == {"10000001", "10000002"}, "only survivor DINs may be PM-enriched"
    assert "20000003" not in seen, "filtered-out DIN must never be fetched"


# ── 6. 4-hour cache: one build within window; rebuild after TTL; reset clears ──
def test_universe_cache_4h_window_and_reset(monkeypatch):
    builds = {"n": 0}

    def _fake_load(cache_dir=U.UNIVERSE_CACHE_DIR):
        builds["n"] += 1
        return [_dpd("10000001", "MOLX 100 MG", "B", "C INC", "100 MG", "Marketed", "Tablet", 1)]

    async def _fake_gsur():
        return []

    monkeypatch.setattr(U, "load_dpd_universe_records", _fake_load)
    monkeypatch.setattr(U, "_load_gsur_records", _fake_gsur)
    # Download is a separate step from parse (get_universe calls _download_extract,
    # then load_dpd_universe_records); stub the network step so this stays offline.
    monkeypatch.setattr(U, "_download_extract", lambda: None)
    U._CACHE["bundle"] = None

    # Option 3 then Option 4 within the window → ONE build.
    asyncio.run(U.get_universe())
    asyncio.run(U.get_universe())
    assert builds["n"] == 1, "fresh cache must be reused (no double pull)"

    # Force TTL expiry → next request rebuilds.
    U._CACHE["bundle"].built_at -= (U.UNIVERSE_TTL + 1)
    asyncio.run(U.get_universe())
    assert builds["n"] == 2, "stale cache must rebuild after the 4 h window"

    # Reset invalidates → next request rebuilds again.
    U.reset_universe_cache()
    assert U._CACHE["bundle"] is None
    asyncio.run(U.get_universe())
    assert builds["n"] == 3, "reset must force a fresh build"


# ── 7. Match-confidence column + low-confidence KPI (house-brand audit) ────────
def test_match_confidence_and_low_kpi():
    sheet1 = pd.DataFrame([
        {"din": "30000001", "brand_name": "PROGESTERONE"},     # exact-brand
        {"din": "30000002", "brand_name": "TEVA-AMLODIPINE"},  # fuzzy, strong
        {"din": "30000003", "brand_name": "JAMP-METFORMIN"},   # fuzzy, weak → low
        {"din": "30000004", "brand_name": "PRO DOC THING"},    # no IQVIA group
    ])
    recon = pd.DataFrame([
        {"din": "30000001", "status": "matched", "top_score": 100.0,
         "notes": "exact-brand match; score=100"},
        {"din": "30000002", "status": "matched", "top_score": 92.0, "notes": "score=92"},
        {"din": "30000003", "status": "matched", "top_score": 70.0, "notes": "score=70"},
        {"din": "30000004", "status": "din_no_iqvia_match", "top_score": None,
         "notes": "No IQVIA group matched this DIN"},
    ])
    df, low = U.attach_match_confidence(sheet1, recon)
    conf = {r["din"]: r["iqvia_match_confidence"] for _, r in df.iterrows()}
    assert conf["30000001"] == "exact"
    assert conf["30000002"] == "high"
    assert conf["30000003"] == "low"     # JAMP house-brand fuzzy match flagged
    assert conf["30000004"] == "none"
    assert low == 1, "exactly one low-confidence (fuzzy) match counted"


def test_match_confidence_blank_without_iqvia():
    sheet1 = pd.DataFrame([{"din": "40000001", "brand_name": "X"}])
    df, low = U.attach_match_confidence(sheet1, pd.DataFrame())
    assert list(df["iqvia_match_confidence"]) == [""]
    assert low == 0
