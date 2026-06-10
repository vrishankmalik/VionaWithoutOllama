"""In-memory job store for async export jobs (per-process; not shared across workers)."""
from __future__ import annotations

import asyncio
import dataclasses
import time
from typing import Optional


@dataclasses.dataclass
class JobState:
    job_id: str
    query: str   # primary query (first in list, or the single query)
    field: str
    queries: list[str] = dataclasses.field(default_factory=list)  # full list for multi-product
    status: str = "running"  # running | complete | error
    events: list[dict] = dataclasses.field(default_factory=list)
    # Signalled whenever a new event is appended
    _notify: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    result_path: Optional[str] = None
    error: Optional[str] = None
    created_at: float = dataclasses.field(default_factory=time.time)
    # Snapshot of the final Sheet 1 and Sheet 2 DataFrames (identical to what
    # was written to the XLSX).  These are the single source of truth for the
    # dashboard view — the dashboard must read from here, never re-scrape.
    sheet1_columns: list[str] = dataclasses.field(default_factory=list)
    sheet1_records: list[dict] = dataclasses.field(default_factory=list)
    sheet2_columns: list[str] = dataclasses.field(default_factory=list)
    sheet2_records: list[dict] = dataclasses.field(default_factory=list)


_jobs: dict[str, JobState] = {}


def create_job(
    job_id: str,
    query: str,
    field: str,
    queries: Optional[list[str]] = None,
) -> JobState:
    effective_queries = queries or [query]
    job = JobState(
        job_id=job_id,
        query=query,
        field=field,
        queries=effective_queries,
    )
    _jobs[job_id] = job
    return job


def get_job(job_id: str) -> Optional[JobState]:
    return _jobs.get(job_id)


async def emit(job: JobState, event: dict) -> None:
    """Append event to job log and wake any waiting SSE consumers."""
    job.events.append(event)
    job._notify.set()
    await asyncio.sleep(0)  # yield so SSE generator can drain immediately
