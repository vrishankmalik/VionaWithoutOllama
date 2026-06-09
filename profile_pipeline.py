"""Profile the full export pipeline end-to-end.

Usage:
    cd c:\\Users\\vrish\\Desktop\\Viona-Pharma-Canada-Database-Search-main
    python profile_pipeline.py --query metformin --field ingredient

Instruments every key operation and prints a ranked breakdown.
No data is changed; this is read-only instrumentation.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections import defaultdict
from typing import Any, Optional

# ── Timing registry ───────────────────────────────────────────────────────────
_timings: dict[str, list[float]] = defaultdict(list)
_call_counts: dict[str, int] = defaultdict(int)
_in_flight: dict[str, int] = defaultdict(int)
_peak_in_flight: dict[str, int] = defaultdict(int)


def _record(name: str, elapsed: float) -> None:
    _timings[name].append(elapsed)
    _call_counts[name] += 1


class _Timer:
    def __init__(self, name: str):
        self.name = name
        self.t0: float = 0.0

    def __enter__(self):
        _in_flight[self.name] += 1
        _peak_in_flight[self.name] = max(_peak_in_flight[self.name], _in_flight[self.name])
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *_):
        _record(self.name, time.perf_counter() - self.t0)
        _in_flight[self.name] -= 1


class _AsyncTimer:
    def __init__(self, name: str):
        self.name = name
        self.t0: float = 0.0

    async def __aenter__(self):
        _in_flight[self.name] += 1
        _peak_in_flight[self.name] = max(_peak_in_flight[self.name], _in_flight[self.name])
        self.t0 = time.perf_counter()
        return self

    async def __aexit__(self, *_):
        _record(self.name, time.perf_counter() - self.t0)
        _in_flight[self.name] -= 1


# ── Patch httpx to count/time every request ───────────────────────────────────
import httpx

_orig_send = httpx.AsyncClient.send


async def _patched_send(self, request, **kwargs):
    label = f"httpx:{request.url.host}:{request.method}"
    async with _AsyncTimer(label):
        return await _orig_send(self, request, **kwargs)


httpx.AsyncClient.send = _patched_send  # type: ignore[method-assign]

# ── Patch cache_get/cache_set to measure SQLite overhead ─────────────────────
import app.cache as _cache_mod

_orig_cache_get = _cache_mod.cache_get
_orig_cache_set = _cache_mod.cache_set


def _patched_cache_get(source: str, query: str) -> Optional[Any]:
    with _Timer("cache_get"):
        result = _orig_cache_get(source, query)
    label = f"cache_get:{source}:{'HIT' if result is not None else 'MISS'}"
    _call_counts[label] += 1
    return result


def _patched_cache_set(source: str, query: str, data: Any, ttl: int = _cache_mod.CACHE_TTL) -> None:
    with _Timer("cache_set"):
        _orig_cache_set(source, query, data, ttl)


_cache_mod.cache_get = _patched_cache_get
_cache_mod.cache_set = _patched_cache_set

# Re-export to already-imported modules
import app.enrichment.patents as _patents_mod
import app.enrichment.labeling as _labeling_mod
_patents_mod.cache_get = _patched_cache_get
_patents_mod.cache_set = _patched_cache_set
_labeling_mod.cache_get = _patched_cache_get
_labeling_mod.cache_set = _patched_cache_set

# ── Patch _is_ollama_available to count calls ─────────────────────────────────
_orig_ollama_check = _labeling_mod._is_ollama_available


async def _patched_ollama_check() -> bool:
    async with _AsyncTimer("ollama_available_check"):
        result = await _orig_ollama_check()
    _call_counts[f"ollama_available_check:{'available' if result else 'unavailable'}"] += 1
    return result


_labeling_mod._is_ollama_available = _patched_ollama_check

# ── Patch key pipeline functions ──────────────────────────────────────────────
_orig_fetch_cpd = _patents_mod._fetch_cpd_dates


async def _patched_fetch_cpd(patent_number: str):
    async with _AsyncTimer("cpd_fetch"):
        return await _orig_fetch_cpd(patent_number)


_patents_mod._fetch_cpd_dates = _patched_fetch_cpd

_orig_pr_detail = _patents_mod._fetch_pr_detail_dates


async def _patched_pr_detail(patent_number: str, session_id: str):
    async with _AsyncTimer("pr_detail_fallback"):
        return await _orig_pr_detail(patent_number, session_id)


_patents_mod._fetch_pr_detail_dates = _patched_pr_detail

_orig_load_zip = _patents_mod.load_patent_zip_both


async def _patched_load_zip():
    async with _AsyncTimer("patent_zip_download_or_parse"):
        return await _orig_load_zip()


_patents_mod.load_patent_zip_both = _patched_load_zip

_orig_fetch_stage2 = _labeling_mod.fetch_stage2_data


async def _patched_fetch_stage2(drug_code: int):
    async with _AsyncTimer("stage2_fetch"):
        return await _orig_fetch_stage2(drug_code)


_labeling_mod.fetch_stage2_data = _patched_fetch_stage2

_orig_download_pdf = _labeling_mod._download_pdf


async def _patched_download_pdf(url: str):
    async with _AsyncTimer("pdf_download"):
        return await _orig_download_pdf(url)


_labeling_mod._download_pdf = _patched_download_pdf

_orig_extract_text = _labeling_mod._extract_text_async


async def _patched_extract_text(pdf_bytes, cache_key, enable_ocr=True):
    async with _AsyncTimer("pdf_text_extraction"):
        return await _orig_extract_text(pdf_bytes, cache_key, enable_ocr)


_labeling_mod._extract_text_async = _patched_extract_text

_orig_parse_fields = _labeling_mod.parse_labeling_fields_async


async def _patched_parse_fields(pages, din_strength, enable_llm=True, **kwargs):
    async with _AsyncTimer("parse_labeling_fields"):
        return await _orig_parse_fields(pages, din_strength, enable_llm, **kwargs)


_labeling_mod.parse_labeling_fields_async = _patched_parse_fields

_orig_query_ollama = _labeling_mod._query_ollama
_ollama_text_hashes: dict[str, set] = defaultdict(set)  # field_group -> set of unique text hashes


async def _patched_query_ollama(section_text, page_num, field_group):
    import hashlib
    _ollama_text_hashes[field_group].add(hashlib.sha256(section_text[:5000].encode()).hexdigest())
    async with _AsyncTimer(f"ollama_query:{field_group}"):
        return await _orig_query_ollama(section_text, page_num, field_group)


_labeling_mod._query_ollama = _patched_query_ollama

# Patch enrich_labeling_batch_fast to time it holistically
_orig_batch_fast = _labeling_mod.enrich_labeling_batch_fast


async def _patched_batch_fast(din_map, enable_ocr=None, enable_llm=True, concurrency=8, on_progress=None):
    async with _AsyncTimer("labeling_batch_fast_total"):
        return await _orig_batch_fast(din_map, enable_ocr, enable_llm, concurrency, on_progress)


_labeling_mod.enrich_labeling_batch_fast = _patched_batch_fast

# Patch enrich_patents holistically
_orig_enrich_patents = _patents_mod.enrich_patents


async def _patched_enrich_patents(dins, on_progress=None):
    async with _AsyncTimer("patents_total"):
        return await _orig_enrich_patents(dins, on_progress)


_patents_mod.enrich_patents = _patched_enrich_patents

# Also patch the data_protection fetch
from app.enrichment import data_protection as _dp_mod

_orig_dp_fetch = _dp_mod.fetch_data_protection_table


async def _patched_dp_fetch():
    async with _AsyncTimer("data_protection_fetch"):
        return await _orig_dp_fetch()


_dp_mod.fetch_data_protection_table = _patched_dp_fetch

# ── CRITICAL: also patch export_job.py local bindings ────────────────────────
# export_job.py uses `from ... import X` which captures function objects at
# import time, so patching the source module is insufficient.  We must also
# update the references stored in export_job's own module dict.
import app.export_job as _export_job_mod

_export_job_mod.enrich_labeling_batch_fast = _patched_batch_fast
_export_job_mod.enrich_patents = _patched_enrich_patents
_export_job_mod.fetch_data_protection_table = _patched_dp_fetch

# Also patch labeling functions called inside labeling.py via module globals
# (these DO work with module-attribute patching since they're in the same module,
#  but we set them explicitly for clarity)
_labeling_mod._query_ollama = _patched_query_ollama
_labeling_mod._is_ollama_available = _patched_ollama_check
_labeling_mod._download_pdf = _patched_download_pdf
_labeling_mod._extract_text_async = _patched_extract_text

# ── Profiling runner ──────────────────────────────────────────────────────────

def _print_report(total_elapsed: float) -> None:
    print("\n" + "="*80)
    print("PIPELINE PROFILE REPORT")
    print("="*80)
    print(f"Total wall-clock: {total_elapsed:.1f}s\n")

    # Aggregate by function name
    rows = []
    for name, times in sorted(_timings.items()):
        total_s = sum(times)
        count = len(times)
        avg_ms = (total_s / count * 1000) if count else 0
        max_ms = max(times) * 1000 if times else 0
        peak = _peak_in_flight.get(name, 1)
        pct = total_s / total_elapsed * 100 if total_elapsed > 0 else 0
        rows.append((total_s, name, count, avg_ms, max_ms, peak, pct))

    rows.sort(reverse=True)

    print(f"{'Function':<45} {'Total(s)':>9} {'%Wall':>7} {'Calls':>7} {'Avg(ms)':>9} {'Max(ms)':>9} {'PeakConc':>9}")
    print("-"*102)
    for total_s, name, count, avg_ms, max_ms, peak, pct in rows:
        print(f"{name:<45} {total_s:>9.2f} {pct:>7.1f}% {count:>7d} {avg_ms:>9.1f} {max_ms:>9.1f} {peak:>9d}")

    print("\n-- HTTP request breakdown by host --")
    http_rows = [(sum(v), k, len(v)) for k, v in _timings.items() if k.startswith("httpx:")]
    http_rows.sort(reverse=True)
    for total_s, name, count in http_rows[:20]:
        avg_ms = total_s / count * 1000
        pct = total_s / total_elapsed * 100
        print(f"  {name:<55} {total_s:>8.2f}s {pct:>6.1f}%  n={count}  avg={avg_ms:.0f}ms")

    print("\n-- Cache hit/miss --")
    for k in sorted(_call_counts):
        if "cache_get:" in k:
            print(f"  {k:<60} {_call_counts[k]:>6}")

    print("\n-- Ollama --")
    for k in sorted(_call_counts):
        if "ollama" in k.lower():
            print(f"  {k:<60} {_call_counts[k]:>6}")
    print("  Unique Ollama input texts (dedup potential):")
    for group, hashes in sorted(_ollama_text_hashes.items()):
        calls = len(_timings.get(f"ollama_query:{group}", []))
        print(f"    {group:<20}  total_calls={calls}  unique_texts={len(hashes)}  "
              f"redundant={max(0, calls - len(hashes))}")

    print("\n-- TOP 10 TIME SINKS --")
    for total_s, name, count, avg_ms, max_ms, peak, pct in rows[:10]:
        print(f"  {pct:>5.1f}%  {total_s:>7.1f}s  n={count:>4}  avg={avg_ms:>7.0f}ms  {name}")

    print("\n-- Concurrency check (were operations actually parallel?) --")
    for name in ("stage2_fetch", "pdf_download", "pdf_text_extraction", "cpd_fetch",
                 "pr_detail_fallback", "parse_labeling_fields", "ollama_query:excipients",
                 "ollama_query:appearance", "ollama_query:ph",
                 "labeling_batch_fast_total", "patents_total"):
        if name in _timings:
            peak = _peak_in_flight.get(name, "?")
            total = sum(_timings[name])
            count = len(_timings[name])
            print(f"  {name:<45}  peak_concurrent={peak}  total_serial={total:.1f}s  n={count}")
    print("="*80)


async def _run(query: str, field: str) -> None:
    """Run the full export pipeline and print a timing report."""
    # Import here so patches above have already been applied
    from app.export_job import run_export_job
    from app.jobs import create_job

    job_id = f"profile-{int(time.time())}"
    job = create_job(job_id, query, field)

    print(f"\nProfiling export pipeline: query={query!r} field={field}")
    print("All stages will run (patents, labeling, data-protection, workbook).\n")

    t0 = time.perf_counter()
    await run_export_job(job, allow_partial=True, enable_ocr=False, enable_llm=True)
    total = time.perf_counter() - t0

    print(f"\nJob status: {job.status}")
    if job.error:
        print(f"Job error: {job.error}")

    _print_report(total)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="alpelisib")
    parser.add_argument("--field", default="ingredient")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    asyncio.run(_run(args.query, args.field))
