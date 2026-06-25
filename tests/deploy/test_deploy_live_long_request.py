"""Part 2 — Railway LIVE: long-request / proxy-timeout survival (risk R6).

Railway terminates a request that produces no bytes for too long.  The export /
universe jobs run for minutes, streaming progress over SSE with a 15-second
``: keepalive`` heartbeat.  These tests confirm, against the deployed URL, that:

  * the SSE stream stays open and keeps emitting past a typical proxy idle window;
  * a full-universe job (the heaviest no-PDF run) reaches ``complete`` end to end
    and the finished workbook downloads;
  * (opt-in, RUN_HEAVY=1) a worst-case option-4 PDF-enrichment run also completes
    without being cut by the proxy.

Dormant until BASE_URL is set.  Tunables:
    DEPLOY_JOB_TIMEOUT   seconds to wait for the full-universe job  (default 360)
    DEPLOY_HEAVY_TIMEOUT seconds to wait for the option-4 job       (default 1800)
    RUN_HEAVY=1          enable the multi-minute PDF-enrichment case
"""
from __future__ import annotations

import json
import os
import time

import httpx
import pytest

pytestmark = pytest.mark.integration

_JOB_TIMEOUT = float(os.environ.get("DEPLOY_JOB_TIMEOUT", "360"))
_HEAVY_TIMEOUT = float(os.environ.get("DEPLOY_HEAVY_TIMEOUT", "1800"))


def _drain_sse(base_url: str, job_id: str, deadline_s: float) -> dict:
    """Follow an export SSE stream to its terminal event.

    Returns the terminal event ({status: complete|error, …}).  Asserts the stream
    actually delivers traffic (events or keepalives) — a proxy that silently cut
    the connection would surface as zero traffic / a read timeout here.
    """
    url = f"{base_url}/export/stream/{job_id}"
    last_progress = None
    saw_traffic = False
    start = time.time()
    # read timeout < keepalive interval would false-fail; the app sends ': keepalive'
    # every 15s, so a 45s read timeout tolerates one missed heartbeat.
    with httpx.Client(timeout=httpx.Timeout(connect=30.0, read=45.0, write=30.0, pool=30.0)) as c:
        with c.stream("GET", url) as resp:
            assert resp.status_code == 200, resp.status_code
            for raw in resp.iter_lines():
                saw_traffic = True
                if time.time() - start > deadline_s:
                    raise AssertionError(
                        f"job {job_id} did not finish within {deadline_s}s "
                        f"(last progress: {last_progress})"
                    )
                line = raw.strip()
                if not line:
                    continue
                if line.startswith(":"):
                    continue  # keepalive comment — connection is alive
                if line.startswith("data:"):
                    evt = json.loads(line[len("data:"):].strip())
                    if "pct" in evt:
                        last_progress = evt
                    if evt.get("status") in ("complete", "error"):
                        assert saw_traffic
                        return evt
    raise AssertionError(f"SSE stream for {job_id} closed before a terminal event")


def test_full_universe_job_streams_to_completion_and_downloads(client, base_url):
    """The heaviest no-PDF run completes over SSE and the workbook downloads."""
    client.post("/api/reset-all-caches", timeout=60.0)
    start = client.post("/universe/start", json={"mode": "full"}, timeout=60.0)
    assert start.status_code == 200, start.text[:300]
    job_id = start.json()["job_id"]

    terminal = _drain_sse(base_url, job_id, _JOB_TIMEOUT)
    assert terminal.get("status") == "complete", terminal
    assert terminal.get("download_url") == f"/export/result/{job_id}", terminal

    dl = client.get(terminal["download_url"], timeout=120.0)
    assert dl.status_code == 200, dl.status_code
    assert dl.content[:2] == b"PK", "downloaded workbook is not a valid .xlsx (zip) file"
    assert len(dl.content) > 50_000, f"workbook suspiciously small: {len(dl.content)} bytes"


def test_sse_stream_survives_idle_keepalive_window(client, base_url):
    """Open the stream and confirm it keeps delivering (events or keepalives) for at
    least 40s — longer than a typical 30s proxy idle cut — without dropping."""
    start = client.post("/universe/start", json={"mode": "full"}, timeout=60.0)
    job_id = start.json()["job_id"]
    url = f"{base_url}/export/stream/{job_id}"

    deadline = time.time() + 40
    chunks = 0
    with httpx.Client(timeout=httpx.Timeout(connect=30.0, read=45.0, write=30.0, pool=30.0)) as c:
        with c.stream("GET", url) as resp:
            assert resp.status_code == 200
            for _ in resp.iter_lines():
                chunks += 1
                if time.time() > deadline:
                    break
    assert chunks > 0, "SSE stream delivered no bytes — proxy may be buffering/cutting it"


@pytest.mark.skipif(os.environ.get("RUN_HEAVY") != "1", reason="set RUN_HEAVY=1 to run the multi-minute PDF case")
def test_worst_case_option4_pdf_enrichment_completes(client, base_url):
    """Worst-case: option-4 filter→enrich fetches + parses real PM PDFs for the
    survivor set.  Confirms a genuinely long job is not cut by the Railway proxy.

    The filter is a broad dosage-form selection to force a meaningful PDF fan-out;
    tune the survivor size via the criteria if you want a larger/smaller run.
    """
    client.post("/api/reset-all-caches", timeout=60.0)
    bases = client.get("/api/dosage-forms", timeout=240.0).json()["base_forms"]
    pick = [b for b in ("TABLET", "CAPSULE") if b in bases] or bases[:1]
    criteria = [{"metric": "dosage_form", "value": pick}]
    start = client.post(
        "/universe/start",
        json={"mode": "filter_enrich", "filter_criteria": criteria, "enable_ocr": False},
        timeout=60.0,
    )
    assert start.status_code == 200, start.text[:300]
    job_id = start.json()["job_id"]

    terminal = _drain_sse(base_url, job_id, _HEAVY_TIMEOUT)
    assert terminal.get("status") == "complete", terminal
    dl = client.get(f"/export/result/{job_id}", timeout=180.0)
    assert dl.status_code == 200 and dl.content[:2] == b"PK"
