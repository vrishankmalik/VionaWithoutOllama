"""Patched dev-server launcher for the Playwright UI suite (Prompt B).

Runs the REAL FastAPI app — real routes, embedded templates, and embedded JS, all
unmodified — but stubs the network seams so UI tests are fast, deterministic, and
never reach live government sites:

  * DPD / NOC / GSUR / Patent Register  -> respx, routed to the SAME recorded
    fixtures the offline unit suite uses (tests/conftest.py side-effects).  Real
    app parsing/grouping/workbook code runs on real fixture bytes.
  * Patent.zip / PM-PDF / data-protection enrichment -> no-op stubs.  These UI
    tests assert that the workbook + dashboard RENDER, not enrichment-extraction
    accuracy (real external calls are Prompt C's responsibility), and skipping
    them keeps every export a few seconds instead of minutes.
  * Full DPD universe -> built once from tests/fixtures/universe/extract; no
    allfiles.zip download is ever attempted.

Nothing under app/ is edited; every stub is a process-start monkeypatch, so the
production code paths the browser exercises are the shipping ones.

Environment (set by the parent conftest BEFORE this process starts):
  CACHE_DIR   throwaway temp dir (isolates the IQVIA persist pickle + SQLite caches)
  ENABLE_OCR  "0"
  UI_PORT     dedicated port (never 8000, which Prompts A/C may bind)
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import respx
import uvicorn

# Importing these now (after the parent set CACHE_DIR) pins config to the temp dir.
import app.enrichment.universe as universe
import app.export_job as export_job
import app.universe_job as universe_job


def _load_offline_conftest():
    """Load tests/conftest.py by path to reuse its recorded-fixture HTTP routers."""
    path = REPO_ROOT / "tests" / "conftest.py"
    spec = importlib.util.spec_from_file_location("ui_offline_conftest", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _install_http_stubs() -> "respx.Router":
    cf = _load_offline_conftest()
    router = respx.mock(assert_all_called=False)
    router.get(re.compile(r"https://health-products\.canada\.ca/api/drug/.*")).mock(
        side_effect=cf._dpd_side_effect
    )
    router.get(
        re.compile(r"https://health-products\.canada\.ca/api/notice-of-compliance/.*")
    ).mock(side_effect=cf._noc_api_side_effect)
    router.get(re.compile(r"https://www\.canada\.ca/.*generic-submissions.*")).mock(
        side_effect=cf._gsur_side_effect
    )
    router.get(re.compile(r"https://pr-rdb\.hc-sc\.gc\.ca/.*")).mock(
        side_effect=cf._pr_get_side_effect
    )
    router.post(re.compile(r"https://pr-rdb\.hc-sc\.gc\.ca/.*")).mock(
        side_effect=cf._pr_post_side_effect
    )
    router.start()  # left running for the process lifetime
    return router


def _stub_enrichment() -> None:
    """No-op the slow external enrichment fetches (PDF/Patent.zip/data-protection)."""

    async def _no_patents(dins, on_progress=None, *a, **k):
        return {}

    async def _no_labeling(*a, **k):
        return {}

    async def _no_dp(*a, **k):
        return []

    export_job.enrich_patents = _no_patents
    export_job.enrich_labeling_batch_fast = _no_labeling
    export_job.fetch_data_protection_table = _no_dp
    universe_job.enrich_labeling_batch_fast = _no_labeling


def _stub_universe() -> None:
    """Serve the full universe from the local fixture extract (no allfiles.zip pull)."""
    extract = REPO_ROOT / "tests" / "fixtures" / "universe" / "extract"
    records = universe.load_dpd_universe_records(extract)
    bundle = universe.UniverseBundle(records, [], dp_table=[])
    universe._CACHE["bundle"] = bundle  # so /api/universe/status reports a fresh cache

    async def _fake_get_universe(force_refresh: bool = False):
        return bundle

    universe.get_universe = _fake_get_universe
    universe_job.get_universe = _fake_get_universe


def main() -> None:
    _install_http_stubs()
    _stub_enrichment()
    _stub_universe()
    from app.main import app  # imported last; all stubs are already in place

    port = int(os.environ.get("UI_PORT", "8753"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio")


if __name__ == "__main__":
    main()
