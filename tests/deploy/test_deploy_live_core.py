"""Part 2 — Railway LIVE checks: port/health, egress, ephemeral FS, concurrency.

Dormant until BASE_URL is set (see conftest).  Run after deploy:

    $env:BASE_URL = "https://<your-app>.up.railway.app"
    <python> -m pytest tests/deploy/test_deploy_live_core.py -m integration -v
"""
from __future__ import annotations

import concurrent.futures as cf

import pytest

pytestmark = pytest.mark.integration


# ── $PORT binding + health (risk R1) ──────────────────────────────────────────

def test_health_root_responds(client):
    """The platform-injected $PORT is bound and the app serves the SPA at '/'."""
    r = client.get("/")
    assert r.status_code == 200, r.status_code
    assert "text/html" in r.headers.get("content-type", ""), r.headers.get("content-type")


def test_iqvia_compare_page_serves(client):
    r = client.get("/iqvia-compare")
    assert r.status_code == 200, r.status_code


# ── Outbound egress (risk R6): the deployed app can reach Health Canada ────────

def test_dosage_forms_proves_outbound_egress(client):
    """/api/dosage-forms forces the allfiles.zip pull from Railway's network. ~55
    live base forms proves DPD egress (TLS, proxy, rate limits) works in prod."""
    r = client.get("/api/dosage-forms", timeout=240.0)
    assert r.status_code == 200, r.text[:300]
    bases = r.json()["base_forms"]
    assert 40 <= len(bases) <= 80, f"expected ~55 base forms from live DPD, got {len(bases)}"


def test_search_egress_abrocitinib_anchor(client):
    """A live search from the deployed app reproduces the CIBINQO anchor — proves
    DPD + NOC egress, not just allfiles.zip."""
    r = client.get("/api/search", params={"q": "abrocitinib", "field": "ingredient"}, timeout=120.0)
    assert r.status_code == 200, r.text[:300]
    sources = {s["source"]: s for s in r.json()["sources"]}
    dpd = sources.get("DPD", {})
    dins = {rec["din"] for rec in dpd.get("records", []) if rec.get("din")}
    assert dins == {"02528363", "02528371", "02528398"}, sorted(dins)


# ── Ephemeral / non-shared filesystem (risk R2): reset → genuine re-pull ───────

def test_reset_all_caches_then_rebuild(client):
    """reset-all-caches wipes the on-disk caches; the next universe request must
    rebuild cleanly from a wiped FS (the Railway-restart scenario in miniature)."""
    body = client.post("/api/reset-all-caches", timeout=60.0).json()
    assert body["status"] == "ok", body
    for key in ("http_rows_cleared", "patent_rows_cleared", "labeling_rows_cleared", "universe_cleared"):
        assert key in body, body

    # Immediately after reset the universe must not report a stale cached build…
    status = client.get("/api/universe/status").json()
    assert status["cached"] is False, status

    # …and a fresh build succeeds from the wiped filesystem.
    r = client.get("/api/dosage-forms", timeout=240.0)
    assert r.status_code == 200, r.text[:300]
    assert client.get("/api/universe/status").json()["cached"] is True


# ── Cold start + concurrency (risk R7): two builds don't corrupt the cache ─────

def test_two_concurrent_universe_requests_single_coherent_build(client, base_url):
    """Two simultaneous first-requests must both succeed and converge on ONE cached
    build — no crash, no half-written cache, no divergent base-form lists."""
    import httpx

    client.post("/api/reset-all-caches", timeout=60.0)

    def _hit():
        with httpx.Client(base_url=base_url, timeout=240.0, follow_redirects=True) as c:
            r = c.get("/api/dosage-forms")
            r.raise_for_status()
            return r.json()["base_forms"]

    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        a, b = [f.result() for f in [ex.submit(_hit), ex.submit(_hit)]]

    assert a == b, "concurrent universe builds produced divergent base-form lists (cache corruption)"
    assert 40 <= len(a) <= 80, len(a)
    assert client.get("/api/universe/status").json()["cached"] is True
