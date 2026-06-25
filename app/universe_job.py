"""Background job runners for the Full-universe tab (options 3 & 4).

This is a NEW orchestration path that lives ALONGSIDE app/export_job.py — it does
not import, reorder, or modify the single/multi-ingredient export pipeline.  It
reuses jobs.py (JobState/emit) for progress + result plumbing and the existing
SSE / result / export-data endpoints serve these jobs unchanged.

  Option 3 (run_universe_full_job): build/reuse the no-PDF universe and emit the
    full workbook (disclaimer + data tabs).  No PM PDFs are ever fetched.

  Option 4 (run_universe_filter_enrich_job): build/reuse the universe → apply the
    six criteria ONCE (build_filtered_workbook layer) → enrich ONLY the survivor
    DINs via enrich_labeling_batch_fast (read-only reuse of labeling.py) → emit the
    enriched filtered workbook.  No human-review step; filtered-out DINs are never
    PM-fetched.
"""
from __future__ import annotations

import logging
import os
import tempfile
import time
from typing import Optional

import pandas as pd

from app.enrichment.labeling import enrich_labeling_batch_fast
from app.enrichment.screen import (
    build_filtered_workbook,
    compute_products,
    filter_products,
    parse_criteria,
    parse_dosage_forms,
    parse_no_file_date,
    requires_iqvia,
)
from app.enrichment.universe import (
    build_universe_response,
    build_universe_sheet1,
    build_universe_sheet2,
    build_universe_workbook,
    get_universe,
    patch_labeling_for_dins,
)
from app.enrichment.workbook import _is_excluded_din
from app.jobs import JobState, emit

logger = logging.getLogger(__name__)

_LABEL_SEM_SIZE = int(os.getenv("LABELING_SEMAPHORE", "8"))


def _resolve_iqvia(job: JobState) -> Optional[pd.DataFrame]:
    """Resolve the collapsed IQVIA frame from the server-side store (or persisted)."""
    from app.main import _IQVIA_STORE, _IQVIA_PERSIST_KEY  # type: ignore[attr-defined]
    if job.iqvia_token:
        df = _IQVIA_STORE.get(job.iqvia_token)
        if df is not None:
            return df
    return _IQVIA_STORE.get(_IQVIA_PERSIST_KEY)


def _snapshot(job: JobState, sheet1_df: pd.DataFrame, sheet2_df: pd.DataFrame) -> None:
    job.sheet1_columns = list(sheet1_df.columns)
    job.sheet1_records = sheet1_df.where(pd.notna(sheet1_df), None).to_dict("records")
    job.sheet2_columns = list(sheet2_df.columns)
    job.sheet2_records = sheet2_df.where(pd.notna(sheet2_df), None).to_dict("records")


def _write_tmp(xlsx_bytes: bytes, prefix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".xlsx", prefix=prefix)
    with os.fdopen(fd, "wb") as fh:
        fh.write(xlsx_bytes)
    return path


# ── Option 3: full no-PDF universe ────────────────────────────────────────────
async def run_universe_full_job(job: JobState) -> None:
    t0 = time.time()

    def elapsed() -> float:
        return round(time.time() - t0, 1)

    try:
        await emit(job, {
            "stage": "Universe", "done": 0, "total": 1, "pct": 0.05,
            "elapsed_s": elapsed(), "eta_s": None,
            "log": "Building full DPD universe (no PDF); reusing the cached pull if fresh (≤4 h)…",
        })
        bundle = await get_universe()
        await emit(job, {
            "stage": "Universe", "done": 1, "total": 1, "pct": 0.45,
            "elapsed_s": elapsed(), "eta_s": None,
            "log": f"Universe loaded: {len(bundle.dpd_records)} DPD products, "
                   f"{len(bundle.gsur_records)} generic submissions.",
        })

        import asyncio
        response = build_universe_response(bundle)
        iqvia_df = _resolve_iqvia(job)
        sheet1_df, recon_df, low_count = await asyncio.to_thread(
            build_universe_sheet1, response, iqvia_df, job.debug_iqvia_rows, bundle.dp_table
        )
        sheet2_df = build_universe_sheet2(bundle)

        await emit(job, {
            "stage": "Workbook", "done": 0, "total": 1, "pct": 0.85,
            "elapsed_s": elapsed(), "eta_s": None,
            "log": f"Assembling workbook: {len(sheet1_df)} rows; "
                   f"{low_count} low-confidence IQVIA match(es) flagged.",
        })
        xlsx = await asyncio.to_thread(
            build_universe_workbook, sheet1_df, sheet2_df, recon_df, low_count
        )
        job.result_path = _write_tmp(xlsx, "cdn_universe_")
        # The downloaded FILE carries every row; the in-page dashboard preview is
        # capped so a ~13.5k-row universe does not freeze the browser.
        _DASH_PREVIEW = 2000
        _snapshot(job, sheet1_df.head(_DASH_PREVIEW), sheet2_df)
        if len(sheet1_df) > _DASH_PREVIEW:
            await emit(job, {
                "stage": "Workbook", "done": 1, "total": 1, "pct": 0.97,
                "elapsed_s": elapsed(), "eta_s": 0,
                "log": f"Dashboard preview limited to first {_DASH_PREVIEW} of "
                       f"{len(sheet1_df)} rows; the downloaded file has them all.",
            })
        if recon_df is not None and not recon_df.empty:
            job.recon_columns = list(recon_df.columns)
            job.recon_records = recon_df.where(pd.notna(recon_df), None).to_dict("records")

        job.status = "complete"
        await emit(job, {
            "status": "complete",
            "download_url": f"/export/result/{job.job_id}",
            "elapsed_s": elapsed(),
            "log": f"Full universe ready: {len(xlsx):,} bytes (PDF data omitted).",
        })
    except Exception as exc:
        logger.exception("Universe full job %s failed", job.job_id)
        job.status = "error"
        job.error = str(exc)
        await emit(job, {"status": "error", "message": str(exc), "elapsed_s": elapsed()})


# ── Option 4: filter then enrich survivors ────────────────────────────────────
async def run_universe_filter_enrich_job(job: JobState, enable_ocr: bool) -> None:
    import asyncio

    t0 = time.time()

    def elapsed() -> float:
        return round(time.time() - t0, 1)

    try:
        criteria = parse_criteria(job.filter_criteria)
        dosage_bases = parse_dosage_forms(job.filter_criteria)
        date_filter = parse_no_file_date(job.filter_criteria)
        if not criteria and not dosage_bases and date_filter is None:
            raise RuntimeError("No filter criteria provided. Tick at least one criterion.")

        await emit(job, {
            "stage": "Universe", "done": 0, "total": 1, "pct": 0.05,
            "elapsed_s": elapsed(), "eta_s": None,
            "log": "Building full DPD universe (no PDF); reusing the cached pull if fresh (≤4 h)…",
        })
        bundle = await get_universe()
        response = build_universe_response(bundle)
        iqvia_df = _resolve_iqvia(job)

        if requires_iqvia(criteria) and iqvia_df is None:
            raise RuntimeError(
                "Value / Quantity criteria need an IQVIA file. Upload one first."
            )

        sheet1_df, recon_df, low_count = await asyncio.to_thread(
            build_universe_sheet1, response, iqvia_df, job.debug_iqvia_rows, bundle.dp_table
        )
        sheet2_df = build_universe_sheet2(bundle)

        await emit(job, {
            "stage": "Filter", "done": 0, "total": 1, "pct": 0.40,
            "elapsed_s": elapsed(), "eta_s": None,
            "log": f"Universe ready ({len(sheet1_df)} rows). Applying filter criteria…",
        })

        # All criteria (six numeric + dosage form + no-file date), once across the
        # full universe → survivor product DINs.
        products_df, _warnings = compute_products(sheet1_df, sheet2_df)
        qualifying = filter_products(products_df, criteria, dosage_bases, date_filter)
        survivor_dins: set[str] = set()
        if not qualifying.empty:
            for dins in qualifying["_dins"]:
                for d in dins:
                    survivor_dins.add(str(d).strip())

        # Build the labeling work-list for survivors only.
        din_map: dict[str, tuple[int, Optional[str]]] = {}
        if not sheet1_df.empty and survivor_dins:
            for _, row in sheet1_df.iterrows():
                din = str(row.get("din", "") or "").strip()
                if din not in survivor_dins or din in din_map or _is_excluded_din(din):
                    continue
                dc = row.get("_drug_code")
                if dc is None or (isinstance(dc, float) and pd.isna(dc)):
                    continue
                try:
                    din_map[din] = (int(dc), row.get("strength"))
                except (TypeError, ValueError):
                    continue

        label_total = len(din_map)
        await emit(job, {
            "stage": "Enrich", "done": 0, "total": max(label_total, 1), "pct": 0.45,
            "elapsed_s": elapsed(), "eta_s": None,
            "log": f"{len(qualifying)} product(s) passed; enriching PM PDFs for "
                   f"{label_total} survivor DIN(s) only (concurrency={_LABEL_SEM_SIZE}).",
        })

        if din_map:
            t_label = time.time()

            async def _on_label_progress(done: int, _total: int, din: str) -> None:
                frac = done / max(label_total, 1)
                await emit(job, {
                    "stage": "Enrich", "done": done, "total": label_total,
                    "pct": round(0.45 + 0.40 * frac, 3),
                    "elapsed_s": elapsed(),
                    "eta_s": round((time.time() - t_label) / done * (label_total - done), 1)
                    if done else None,
                    "log": f"DIN {din} labeling complete",
                })

            await enrich_labeling_batch_fast(
                din_map, enable_ocr=enable_ocr,
                concurrency=_LABEL_SEM_SIZE, on_progress=_on_label_progress,
            )

        # Patch survivor labeling into the universe Sheet 1, then build the filtered
        # (Summary + Detail) workbook — Detail = the enriched survivor rows.
        patched = await asyncio.to_thread(patch_labeling_for_dins, sheet1_df, survivor_dins)
        await emit(job, {
            "stage": "Workbook", "done": 0, "total": 1, "pct": 0.90,
            "elapsed_s": elapsed(), "eta_s": None,
            "log": "Assembling enriched filtered workbook (Summary + Detail)…",
        })
        xlsx, summary_out, detail_out, _w = await asyncio.to_thread(
            build_filtered_workbook, patched, sheet2_df, criteria, dosage_bases, date_filter
        )
        job.result_path = _write_tmp(xlsx, "cdn_universe_filtered_")
        job.summary_columns = list(summary_out.columns)
        job.summary_records = summary_out.where(pd.notna(summary_out), None).to_dict("records")
        _snapshot(job, detail_out, sheet2_df)

        job.status = "complete"
        await emit(job, {
            "status": "complete",
            "download_url": f"/export/result/{job.job_id}",
            "elapsed_s": elapsed(),
            "log": f"Enriched filtered workbook ready: {len(summary_out)} qualifying "
                   f"product(s), {len(survivor_dins)} survivor DIN(s), {len(xlsx):,} bytes.",
        })
    except Exception as exc:
        logger.exception("Universe filter+enrich job %s failed", job.job_id)
        job.status = "error"
        job.error = str(exc)
        await emit(job, {"status": "error", "message": str(exc), "elapsed_s": elapsed()})
