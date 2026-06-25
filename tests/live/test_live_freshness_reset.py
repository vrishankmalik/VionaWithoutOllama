"""Part 1 — Live: 4-hour universe freshness + genuine reset (real network).

This is the ONLY suite allowed to touch the real reset path, because
/api/reset-all-caches wipes the HTTP cache, the patents + labeling stores, AND the
full-universe extract — running it next to the other live tests (or Prompts A/B)
would poison their state.  Run this file alone.

Asserts the documented freshness contract end to end:
  * a fresh build downloads + parses allfiles.zip once;
  * a second request inside the 4-hour window reuses it (NO re-pull / re-parse);
  * POST /api/reset-all-caches forces the next request to genuinely re-pull.
"""
from __future__ import annotations

import asyncio

import pytest

from app.enrichment import universe as U

pytestmark = pytest.mark.integration


def _allfiles_reachable() -> bool:
    from tests.live.conftest import reachable
    return reachable(U.DPD_ALLFILES_URL, head=True, timeout=30.0)


def test_build_once_reuse_then_reset_repulls(monkeypatch):
    if not _allfiles_reachable():
        pytest.skip("allfiles.zip unreachable — freshness/reset test skipped")

    U.reset_universe_cache()

    parses = {"n": 0}
    real_load = U.load_dpd_universe_records

    def _counting_load(cache_dir=U.UNIVERSE_CACHE_DIR):
        parses["n"] += 1
        return real_load(cache_dir)

    monkeypatch.setattr(U, "load_dpd_universe_records", _counting_load)

    # First build → downloads + parses once.
    asyncio.run(U.get_universe())
    assert parses["n"] == 1, "first build should parse exactly once"

    # Within the 4-hour window → cached, no re-parse.
    asyncio.run(U.get_universe())
    assert parses["n"] == 1, "fresh universe must be reused within the 4h window"

    # The genuine reset endpoint must clear the cache AND on-disk extract.
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        body = client.post("/api/reset-all-caches").json()
    assert body["status"] == "ok", body
    assert "universe_cleared" in body, body
    assert U._CACHE.get("bundle") is None, "reset-all-caches must drop the in-process bundle"

    # Next request must re-pull + re-parse.
    asyncio.run(U.get_universe())
    assert parses["n"] == 2, "reset-all-caches must force a fresh allfiles.zip pull"


def test_reset_endpoint_reports_all_cache_classes():
    """The reset endpoint reports every cache class it clears (contract surface)."""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        body = client.post("/api/reset-all-caches").json()
    for key in ("http_rows_cleared", "patent_rows_cleared", "labeling_rows_cleared", "universe_cleared"):
        assert key in body, f"reset-all-caches missing {key!r}: {body}"
