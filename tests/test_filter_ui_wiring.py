"""Offline TestClient suite proving the two new filters are present and wired in the
filter UI on BOTH tabs (Search by ingredient(s) AND Full universe), with an
identical option set, plus the /api/dosage-forms endpoint that feeds the dropdown.

The controls are rendered client-side by one shared builder, so we assert the served
page carries: the per-tab containers, the shared builder invoked for BOTH suffixes,
the shared collector, the dropdown fetch, and the lazy-load hook on BOTH filter
panels. This catches a regression that wires a new criterion into only one tab.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.enrichment.universe import UniverseBundle
from app.main import app

_UNI_EXTRACT = Path(__file__).parent / "fixtures" / "universe" / "extract"


@pytest.fixture()
def client():
    return TestClient(app)


def test_home_page_renders():
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]


def test_both_tabs_have_dosage_and_date_controls(client):
    html = client.get("/").text

    # Per-tab containers for the additive criteria — one for Search, one for Universe.
    assert 'id="extraCriteria"' in html
    assert 'id="extraCriteriaU"' in html

    # The shared builder is invoked for BOTH tab suffixes ('s' = Search, 'u' = Universe).
    assert "buildExtraCriteria('s', 'extraCriteria')" in html
    assert "buildExtraCriteria('u', 'extraCriteriaU')" in html

    # Both collectors append the additive filters (dosage + no-file date).
    assert "collectExtraCriteria('s', out)" in html
    assert "collectExtraCriteria('u', out)" in html

    # The dropdown is lazily populated from the cached universe list on BOTH panels.
    assert html.count("populateDosageForms()") >= 2
    assert "fetch('/api/dosage-forms')" in html


def test_filter_controls_use_identical_option_set(client):
    html = client.get("/").text
    # Both new criteria are emitted by the shared builder (single source → identical
    # on both tabs): the dosage multi-select + the four no-file-date operators.
    assert "metric: 'dosage_form'" in html
    assert "metric: 'no_file_date'" in html
    for op in ("'less'", "'greater'", "'greater_or_equal'", "'equal'"):
        assert op in html, op
    # MM/DD/YYYY entry, labelled Month/Day/Year + future-only validation present.
    assert "MM/DD/YYYY" in html
    assert "_checkFutureMdy" in html


def test_dosage_forms_endpoint_returns_base_list(monkeypatch):
    import app.enrichment.universe as U

    recs = U.load_dpd_universe_records(_UNI_EXTRACT)
    bundle = UniverseBundle(recs, [])

    async def _fake_get_universe(force_refresh=False):
        return bundle

    # The endpoint imports get_universe from the universe module at call time.
    monkeypatch.setattr(U, "get_universe", _fake_get_universe)

    with TestClient(app) as c:
        r = c.get("/api/dosage-forms")
    assert r.status_code == 200
    bases = r.json()["base_forms"]
    assert isinstance(bases, list) and bases == sorted(bases)
    assert bases == sorted(bundle.dosage_form_map.keys())
    assert "TABLET" in bases  # the real extract cohort carries tablets
