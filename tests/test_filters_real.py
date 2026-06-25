"""Live integration suite for the two filters on REAL full-scale data — the checks
the trimmed offline fixtures cannot prove: the six-year date is populated and
correct across the WHOLE live universe×Register join, the Search/Universe tabs
agree on the same real DIN, the dropdown serves the real base-form set, and the
fuzzy match does not blow up at market scale.

Marked @pytest.mark.integration → EXCLUDED from the default offline run
(pytest.ini: addopts = -m "not integration"); run with:

    pytest tests/test_filters_real.py -m integration -v

Each test SKIPS (never silently passes) when its live dependency is unreachable.
Mirrors tests/test_universe_real.py. Assertions are derived from live data at run
time (not hard-coded dates) so they stay true as the Register updates.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from difflib import get_close_matches

import pytest
from fastapi.testclient import TestClient

from app.enrichment import universe as U
from app.enrichment.data_protection import (
    _normalize_ingredient_dp as NI,
    _normalize_manufacturer as NM,
)
from app.enrichment.screen import parse_stored_no_file_date
from app.enrichment.workbook import _get_dp_cols

pytestmark = pytest.mark.integration

_FANOUT_CEILING = 60  # churn-tolerant; live max observed ≈ 18 DINs / (date, mfr)


def _net_ok() -> bool:
    try:
        import httpx
        with httpx.stream("GET", U.DPD_ALLFILES_URL, follow_redirects=True, timeout=20.0) as r:
            return r.status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="module")
def live_universe():
    if not _net_ok():
        pytest.skip("allfiles.zip unreachable — live filter tests skipped")
    U.reset_universe_cache()
    bundle = asyncio.run(U.get_universe(force_refresh=True))
    if not bundle.dp_table:
        pytest.skip("Register of Innovative Drugs unreachable — dp_table empty")
    return bundle


@pytest.fixture(scope="module")
def live_join(live_universe):
    """The real DPD-product → six-year-date join, via the production matcher."""
    dp = live_universe.dp_table
    attached = []
    for rec in live_universe.dpd_records:
        cols = _get_dp_cols(rec.ingredient, rec.company, dp)
        d = (cols.get("dp_6yr_no_file_date") or "").strip()
        if d:
            attached.append((rec.din, rec.ingredient, rec.company, d))
    return attached


# ── A0 live: populated, correct, and cross-tab consistent ─────────────────────

def test_live_universe_dp_nonblank_count_positive(live_join):
    assert len(live_join) > 0, "no six-year dates attached on the live universe — fix dead"
    # Every attached value is a genuine parseable date (no junk leaks through).
    assert all(parse_stored_no_file_date(d) is not None for *_x, d in live_join)


def test_live_named_innovator_din_carries_its_real_date(live_universe):
    """alpelisib / PIQRAY (DIN 02497069, Novartis) carries the date the live Register
    prints for it — derived live, not hard-coded, so it survives Register updates."""
    dp = live_universe.dp_table
    by_din = {r.din: r for r in live_universe.dpd_records}
    rec = by_din.get("02497069")
    if rec is None:
        pytest.skip("alpelisib DIN 02497069 not in the current catalogue")
    attached = (_get_dp_cols(rec.ingredient, rec.company, dp).get("dp_6yr_no_file_date") or "").strip()
    # Independent expectation straight from the Register rows for this identity.
    ni, nc = NI(rec.ingredient or ""), NM(rec.company or "")
    expected = [r["no_file_date"].strip() for r in dp
                if (NI(r["medicinal_ingredient"]) in ni or ni in NI(r["medicinal_ingredient"]))
                and (NM(r["manufacturer"]) == nc
                     or get_close_matches(nc, [NM(r["manufacturer"])], n=1, cutoff=0.8))]
    assert attached and attached in expected, (attached, expected[:5])


def test_live_cross_tab_parity_same_din(live_universe, tmp_path):
    """The same real DIN gets the same six-year date through build_sheet1 (Search)
    and build_universe_sheet1 (Universe)."""
    import app.enrichment.store as store_mod
    store_mod.reset_for_testing(str(tmp_path / "enrich.db"))
    from app.enrichment.universe import build_universe_response, build_universe_sheet1
    from app.enrichment.workbook import build_sheet1
    from app.models import DrugRecord, SearchMetadata, SearchResponse, SourceResult, DrugRecord as DR

    dp = live_universe.dp_table
    rec = next((r for r in live_universe.dpd_records if r.din == "02497069"), None)
    if rec is None:
        pytest.skip("alpelisib DIN 02497069 not present")

    noc = DR(source="NOC", din=rec.din, brand_name=rec.brand_name, ingredient=rec.ingredient,
             company=rec.company, source_specific={"noc_date": "2020-01-01", "submission_type": "NDS",
             "submission_class": "New", "reason_for_supplement": None, "therapeutic_class": "X"})
    resp = SearchResponse(metadata=SearchMetadata(query="alpelisib", field="ingredient", timestamp="t"),
                          sources=[SourceResult(source="DPD", status="ok", records=[rec]),
                                   SourceResult(source="NOC", status="ok", records=[noc])])
    search_df = build_sheet1(resp, dp_table=dp)
    uni_df, _r, _l = build_universe_sheet1(
        build_universe_response(U.UniverseBundle([rec], [])), None, dp_table=dp)

    def _date(df):
        row = df[df["din"] == "02497069"]
        return "" if row.empty else str(row["dp_6yr_no_file_date"].iloc[0] or "")

    assert _date(search_df) and _date(search_df) == _date(uni_df), (_date(search_df), _date(uni_df))


# ── Fuzzy-match-at-scale guard on the LIVE join ───────────────────────────────

def test_live_join_invariants_hold_at_market_scale(live_universe, live_join):
    dp = live_universe.dp_table
    register_dates = {r["no_file_date"].strip() for r in dp}

    # No fabrication: every attached date exists in the Register.
    attached_dates = {d for *_x, d in live_join}
    assert attached_dates <= register_dates, attached_dates - register_dates

    # No more distinct (ingredient, manufacturer) identities than Register rows.
    identities = {(NI(i or ""), NM(c or "")) for _din, i, c, _d in live_join}
    assert len(identities) <= len(dp), (len(identities), len(dp))

    # Bounded fan-out per (date, manufacturer) identity.
    fan = Counter((d, NM(c or "")) for _din, _i, c, d in live_join)
    assert max(fan.values()) <= _FANOUT_CEILING, fan.most_common(3)


# ── Dropdown source: the live base-form set ───────────────────────────────────

def test_live_dosage_forms_endpoint_serves_real_bases(live_universe):
    # The endpoint reads the cached universe built above; assert a real, sane set.
    with TestClient(U_app()) as c:
        r = c.get("/api/dosage-forms")
    assert r.status_code == 200
    bases = r.json()["base_forms"]
    assert 45 <= len(bases) <= 70, f"{len(bases)} base forms — expected ~55"
    for must in ("TABLET", "CAPSULE", "SOLUTION"):
        assert must in bases, must
    assert bases == sorted(bases)


def U_app():
    from app.main import app
    return app
