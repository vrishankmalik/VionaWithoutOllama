"""Acceptance: the labeling batch's bounded async fan-out.

`enrich_labeling_batch_fast` groups DINs by Product-Monograph URL and processes
one group per `asyncio` task, each gated by a `Semaphore(concurrency)` (the
`pdf_sem`).  These tests give every DIN its own unique drug_code → unique pdf_url
(so one group == one DIN == one `_download_pdf` call), then monkeypatch the two
collaborators the batch calls so NO network happens:

  * `fetch_stage2_data` → returns a unique pdf_url per drug_code (forces N groups).
  * `_download_pdf`     → a counting stub that records max-in-flight then returns
    None (the "PM download failed" branch: results[din]=None, progress ticks).

Asserting on `_download_pdf`'s max concurrency proves the `pdf_sem` bound, since
that call lives inside `_process_pdf_group`, which runs while holding `pdf_sem`.

Hermetic, deterministic, fast: no real PDFs, no sleeps beyond `asyncio.sleep(0)`
yields used to force interleaving.
"""
from __future__ import annotations

import asyncio

import pytest

from app.enrichment import labeling as L


def _make_din_map(n: int) -> dict[str, tuple[int, str | None]]:
    """n DINs, each with its own drug_code (so each lands in its own pdf_url group)."""
    return {f"{2000000 + i:08d}": (500000 + i, "50 mg") for i in range(n)}


class _Tracker:
    """Records concurrent in-flight count and which DINs/codes were processed."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0
        self.download_calls: list[str] = []
        self.lock = asyncio.Lock()


@pytest.fixture
def patched_batch(monkeypatch):
    """Wire fetch_stage2_data + _download_pdf to counting stubs; return the tracker."""
    tracker = _Tracker()

    async def fake_fetch_stage2_data(drug_code: int) -> dict:
        # Each drug_code → a unique PM url so groups never collapse.
        return {
            "active_ingredient": None,
            "pack_size": None,
            "pack_style": None,
            "pdf_url": f"https://example.test/pm/{drug_code}.pdf",
            "pdf_date": None,
            "description": None,
            "pdf_lookup_ok": True,
        }

    async def fake_download_pdf(url: str):
        # Enter: bump in-flight, record peak under lock.
        async with tracker.lock:
            tracker.in_flight += 1
            tracker.max_in_flight = max(tracker.max_in_flight, tracker.in_flight)
            tracker.download_calls.append(url)
        # Yield a few times so other ready coroutines get a chance to run — this is
        # what would let an UNBOUNDED implementation spike the in-flight count.
        for _ in range(3):
            await asyncio.sleep(0)
        async with tracker.lock:
            tracker.in_flight -= 1
        # Returning None drives the "download failed" path: results[din]=None and a
        # progress tick — no PDF parsing, no store writes, fully deterministic.
        return None

    monkeypatch.setattr(L, "fetch_stage2_data", fake_fetch_stage2_data)
    monkeypatch.setattr(L, "_download_pdf", fake_download_pdf)
    return tracker


@pytest.mark.parametrize("k", [1, 4, 8])
async def test_fan_out_is_bounded_by_concurrency(patched_batch, k):
    n = 50
    din_map = _make_din_map(n)

    results = await L.enrich_labeling_batch_fast(
        din_map, enable_ocr=False, concurrency=k,
    )

    # Batch completes (no deadlock) and processes every DIN exactly once.
    assert set(results.keys()) == set(din_map.keys())
    assert len(results) == n
    # Each unique pdf_url downloaded exactly once.
    assert len(patched_batch.download_calls) == n
    assert len(set(patched_batch.download_calls)) == n
    # The semaphore bound held: never more than k group-tasks in flight at once.
    assert patched_batch.max_in_flight <= k, (patched_batch.max_in_flight, k)
    # And we actually exercised real parallelism when allowed (k>1 should peak >1).
    if k > 1:
        assert patched_batch.max_in_flight >= 2, patched_batch.max_in_flight


async def test_on_progress_is_monotonic_1_to_n_no_gaps(patched_batch):
    n = 50
    din_map = _make_din_map(n)
    seen_done: list[int] = []
    seen_total: list[int] = []

    async def on_progress(done: int, total: int, din: str):
        seen_done.append(done)
        seen_total.append(total)

    await L.enrich_labeling_batch_fast(
        din_map, enable_ocr=False, concurrency=4, on_progress=on_progress,
    )

    # Total is constant and correct.
    assert set(seen_total) == {n}
    # done fires exactly N times, covering 1..N with no gaps or duplicates.
    assert len(seen_done) == n
    assert sorted(seen_done) == list(range(1, n + 1))


async def test_sync_on_progress_callback_supported(patched_batch):
    """on_progress may be a plain (non-coroutine) callable — it must still fire N times."""
    n = 12
    din_map = _make_din_map(n)
    ticks: list[int] = []

    def on_progress(done: int, total: int, din: str):
        ticks.append(done)

    await L.enrich_labeling_batch_fast(
        din_map, enable_ocr=False, concurrency=3, on_progress=on_progress,
    )
    assert sorted(ticks) == list(range(1, n + 1))


async def test_empty_din_map_returns_empty_without_work(patched_batch):
    out = await L.enrich_labeling_batch_fast({}, enable_ocr=False, concurrency=8)
    assert out == {}
    assert patched_batch.download_calls == []
