"""Part 2 — Railway memory headroom (risk R7): peak RSS of a full build, LOCAL.

Railway containers have a hard memory ceiling (512 MB on the smaller plans).
Building the whole DPD universe, collapsing a 140k-row IQVIA extract, matching it,
and assembling openpyxl workbooks is the app's memory high-water mark.  This test
measures the peak RSS of that worst-case build on THIS machine and flags it against
a configurable ceiling, so you size the Railway plan with real numbers rather than
discovering an OOM kill in production.

Marked integration (needs the live allfiles.zip).  Prints the measured peak even
when it passes, so the number lands in the launch report.  Tunable:
    MEM_CEILING_MB   ceiling to assert against (default 512, the small-plan limit)
"""
from __future__ import annotations

import asyncio
import gc
import os
import threading
import time

import pytest

pytestmark = pytest.mark.integration

_CEILING_MB = float(os.environ.get("MEM_CEILING_MB", "512"))


class _PeakSampler(threading.Thread):
    """Poll the process RSS in the background and remember the peak (MB)."""

    def __init__(self, proc, interval=0.05):
        super().__init__(daemon=True)
        self._proc = proc
        self._interval = interval
        self._stop = threading.Event()
        self.peak_mb = 0.0

    def run(self):
        while not self._stop.is_set():
            try:
                rss = self._proc.memory_info().rss / (1024 * 1024)
                self.peak_mb = max(self.peak_mb, rss)
            except Exception:
                break
            time.sleep(self._interval)

    def stop(self):
        self._stop.set()
        self.join(timeout=2)


def _find_iqvia():
    from tests.live.conftest import find_iqvia
    return find_iqvia()


def test_full_build_peak_memory_under_ceiling(capsys):
    psutil = pytest.importorskip("psutil", reason="psutil needed to measure RSS")
    from tests.live.conftest import reachable
    from app.enrichment import universe as U

    if not reachable(U.DPD_ALLFILES_URL, head=True, timeout=30.0):
        pytest.skip("allfiles.zip unreachable — memory test skipped")

    from app.enrichment.universe import (
        build_universe_response, build_universe_sheet1,
        build_universe_sheet2, build_universe_workbook,
    )

    proc = psutil.Process(os.getpid())
    gc.collect()
    baseline_mb = proc.memory_info().rss / (1024 * 1024)

    sampler = _PeakSampler(proc)
    sampler.start()
    try:
        U.reset_universe_cache()
        bundle = asyncio.run(U.get_universe(force_refresh=True))
        response = build_universe_response(bundle)

        iqvia_df = None
        iq_path = _find_iqvia()
        if iq_path is not None:
            from app.enrichment.iqvia import parse_iqvia, collapse_iqvia
            iqvia_df = collapse_iqvia(parse_iqvia(iq_path.read_bytes()))

        s1, recon, low = build_universe_sheet1(response, iqvia_df, dp_table=bundle.dp_table)
        s2 = build_universe_sheet2(bundle)
        xlsx = build_universe_workbook(s1, s2, recon, low)
        assert xlsx[:2] == b"PK"
    finally:
        sampler.stop()

    peak = sampler.peak_mb
    used = peak - baseline_mb
    with capsys.disabled():
        print(
            f"\n[memory] full universe build: peak RSS={peak:.0f} MB "
            f"(baseline={baseline_mb:.0f} MB, build delta={used:.0f} MB, "
            f"iqvia={'yes' if iq_path else 'no'}); ceiling={_CEILING_MB:.0f} MB"
        )
    assert peak < _CEILING_MB, (
        f"peak RSS {peak:.0f} MB exceeds the {_CEILING_MB:.0f} MB ceiling — "
        f"raise the Railway plan or set MEM_CEILING_MB to the chosen plan's limit"
    )
