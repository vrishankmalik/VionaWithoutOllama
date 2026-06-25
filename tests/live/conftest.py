"""Shared fixtures + reachability helpers for the Part-1 LIVE pre-launch suite.

This suite hits the REAL Health Canada sources (DPD / NOC / Patent Register /
Register of Innovative Drugs / allfiles.zip) and the REAL IQVIA extract — no
respx, no recorded fixtures.  Every test is ``@pytest.mark.integration`` so it is
EXCLUDED from the default offline run (pytest.ini: addopts = -m "not integration")
and runs only on demand:

    # the import-pandas WMI shim is required on this Py3.14 host (see project memory)
    $env:PYTHONPATH = "C:\\Users\\vmalik\\AppData\\Local\\Temp\\pyshim;<repo-root>"
    <python> -m pytest tests/live -m integration -v -s

Contract (mirrors tests/test_universe_real.py and tests/test_iqvia_diff_real.py):
each test SKIPS — never silently passes — when its live dependency is unreachable.
A skip means "not verified"; a pass means "verified against live data".

The expensive live inputs (full DPD universe, active DP register, abrocitinib
search) are built ONCE per session here and shared across the suite.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

import httpx
import pytest

from app.enrichment import universe as U

# ── Live endpoints we probe for reachability ──────────────────────────────────
_DPD_PROBE = "https://health-products.canada.ca/api/drug/drugproduct/?id=1"
# Import the canonical register URL from the app so the probe can never drift from
# the URL the code actually fetches.
from app.enrichment.data_protection import _REGISTER_URL as _REGISTER_PROBE  # noqa: E402


def reachable(url: str, *, head: bool = False, timeout: float = 20.0) -> bool:
    """True if ``url`` answers 200 over the live network, else False (→ skip)."""
    try:
        method = "HEAD" if head else "GET"
        with httpx.stream(method, url, follow_redirects=True, timeout=timeout, verify=False) as r:
            return r.status_code == 200
    except Exception:
        return False


def present(v) -> bool:
    """Pandas-aware 'cell is populated' test.

    Unmatched cells on a universe DataFrame hold a float ``NaN`` (not ""), and
    ``str(NaN)`` renders the truthy string ``"nan"`` — so naive truthiness counts
    every blank as populated.  This treats NaN / "" / "nan" / "none" as blank.
    """
    import pandas as pd
    if v is None:
        return False
    try:
        if pd.isna(v):
            return False
    except (TypeError, ValueError):
        pass
    return str(v).strip().lower() not in ("", "nan", "none")


def find_iqvia() -> Optional[Path]:
    """Locate the real IQVIA.xlsx (same convention as tests/test_universe_real.py)."""
    env = os.environ.get("IQVIA_REAL_NEW")
    if env and Path(env).is_file():
        return Path(env)
    home = Path.home()
    for cand in (
        home / "OneDrive - Viona Pharmaceuticals USA INC" / "Desktop" / "IQVIA.xlsx",
        home / "Desktop" / "IQVIA.xlsx",
        home / "Downloads" / "IQVIA.xlsx",
    ):
        if cand.is_file():
            return cand
    return None


# ── Session-scoped live inputs (built once) ───────────────────────────────────

@pytest.fixture(scope="session")
def live_search_abrocitinib():
    """Run the REAL all-source search for abrocitinib once.

    abrocitinib is the chosen end-to-end anchor: a single-ingredient, single-brand
    (CIBINQO) product whose DPD/NOC footprint is small and hand-verifiable, so any
    contamination (an unrelated DIN leaking in) is obvious.
    """
    if not reachable(_DPD_PROBE):
        pytest.skip("DPD API unreachable — live search skipped")
    from app.main import search
    return asyncio.run(search(q="abrocitinib", field="ingredient"))


@pytest.fixture(scope="session")
def live_dp_table():
    """Fetch the REAL active Register of Innovative Drugs once."""
    if not reachable(_REGISTER_PROBE):
        pytest.skip("Register of Innovative Drugs unreachable — DP live test skipped")
    from app.enrichment.data_protection import fetch_data_protection_table
    table = asyncio.run(fetch_data_protection_table())
    if not table:
        pytest.skip("Register returned 0 rows (fetch failed) — DP live test skipped")
    return table


@pytest.fixture(scope="session")
def live_universe():
    """Build the REAL full DPD universe once (downloads allfiles.zip)."""
    if not reachable(U.DPD_ALLFILES_URL, head=True, timeout=30.0):
        pytest.skip("allfiles.zip unreachable — live universe skipped")
    U.reset_universe_cache()
    return asyncio.run(U.get_universe(force_refresh=True))


@pytest.fixture(scope="session")
def live_universe_iqvia_sheet(live_universe):
    """Universe Sheet 1 built once from the live universe + the real IQVIA.xlsx.

    Returns (sheet1_df, recon_df, low_count).  Skips if the IQVIA file is absent.
    """
    iq_path = find_iqvia()
    if iq_path is None:
        pytest.skip("real IQVIA.xlsx not found — set IQVIA_REAL_NEW")
    from app.enrichment.iqvia import collapse_iqvia, parse_iqvia
    from app.enrichment.universe import build_universe_response, build_universe_sheet1

    iq = collapse_iqvia(parse_iqvia(iq_path.read_bytes()))
    df, recon, low = build_universe_sheet1(
        build_universe_response(live_universe), iq, dp_table=live_universe.dp_table
    )
    return df, recon, low
