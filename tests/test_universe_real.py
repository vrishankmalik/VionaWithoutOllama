"""Live integration suite for the Full-universe feature — catches schema drift the
trimmed offline fixtures cannot (the real allfiles.zip layout, the real full
product count, and the real IQVIA-on-universe match at market scale).

Marked @pytest.mark.integration, so it is EXCLUDED from the default offline run
(pytest.ini: addopts = -m "not integration") and only runs via:

    pytest tests/test_universe_real.py -m integration -v

It downloads the live allfiles.zip through the production get_universe() path and
reads the real IQVIA.xlsx (env IQVIA_REAL_NEW or the known Desktop/Downloads
location).  Each test skips — never silently passes — when its live dependency is
unreachable.  Mirrors tests/reconciliation/ and tests/test_iqvia_diff_real.py.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from app.enrichment import universe as U

pytestmark = pytest.mark.integration


# ── Locate the real IQVIA extract (same convention as test_iqvia_diff_real) ────
def _find_iqvia() -> Path | None:
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


def _net_ok() -> bool:
    try:
        import httpx
        with httpx.stream("GET", U.DPD_ALLFILES_URL, follow_redirects=True, timeout=20.0) as r:
            return r.status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="module")
def live_universe():
    if not _net_ok():
        pytest.skip("allfiles.zip unreachable — live universe test skipped")
    U.reset_universe_cache()
    bundle = asyncio.run(U.get_universe(force_refresh=True))
    return bundle


def test_real_universe_product_count_sane(live_universe):
    """The live universe parses the whole DPD catalogue (all status sets)."""
    recs = live_universe.dpd_records
    # The DPD catalogue is ~13.5k products; assert a generous floor so the test is
    # robust to weekly catalogue churn but still catches a parse that lost the bulk.
    assert len(recs) > 10000, f"only {len(recs)} universe records — parse regression?"
    dins = {r.din for r in recs}
    assert len(dins) == len(recs), "every universe record must carry a unique DIN"


def test_real_universe_glucophage_anchor(live_universe):
    """The GLUCOPHAGE 02099233 column-verify anchor parses with correct identity."""
    by_din = {r.din: r for r in live_universe.dpd_records}
    glu = by_din.get("02099233")
    assert glu is not None, "GLUCOPHAGE 02099233 missing from live universe"
    assert "GLUCOPHAGE" in (glu.brand_name or "").upper()
    assert "METFORMIN" in (glu.ingredient or "").upper()
    assert glu.status, "GLUCOPHAGE must carry a current status"


@pytest.fixture(scope="module")
def live_universe_iqvia_sheet(live_universe):
    """Build the universe Sheet 1 once from the live universe + real IQVIA.xlsx.

    Module-scoped so the expensive 142k-row IQVIA collapse runs a single time for both
    the anchor and the low-confidence-KPI assertions.
    """
    iq_path = _find_iqvia()
    if iq_path is None:
        pytest.skip("real IQVIA.xlsx not found — set IQVIA_REAL_NEW")
    from app.enrichment.iqvia import parse_iqvia, collapse_iqvia
    from app.enrichment.universe import build_universe_response, build_universe_sheet1

    iq = collapse_iqvia(parse_iqvia(iq_path.read_bytes()))
    df, _recon, low = build_universe_sheet1(build_universe_response(live_universe), iq)
    return df, low


def test_real_iqvia_on_live_universe_reproduces_anchor(live_universe_iqvia_sheet):
    """Real IQVIA.xlsx matched on the live universe reproduces a hand-verified DIN.

    SANIS PROGESTERONE 100MG (DIN 02516187) = 218,591 units / $21,215,081 — the
    same anchor pinned in tests/test_iqvia.py, now flowing through the universe
    assembly with NO PDF dependency.
    """
    df, _low = live_universe_iqvia_sheet
    row = df[df["din"] == "02516187"]
    assert len(row) == 1, "SANIS PROGESTERONE DIN missing from live universe"
    assert int(row["Units MAT 12/2025"].iloc[0]) == 218591
    assert int(row["Dollars MAT 12/2025"].iloc[0]) == 21215081
    assert row["iqvia_match_confidence"].iloc[0] in ("exact", "high")


def test_real_low_confidence_kpi_is_alive(live_universe_iqvia_sheet):
    """Proof the low-confidence KPI is NOT dead in production.

    The 9-DIN offline fixture has low_count=0 (its house brands all resolve
    exact-brand-first), so only the full-scale match proves the band ever fires.
    Observed 2026-06-24 on the live universe (13,550 DINs) vs live IQVIA (9,672
    groups): none=6910, exact=5522, high=834, low=284.  We lock a churn-tolerant
    band — > 0 catches a dead KPI; a floor near the ~284 baseline catches a silent
    collapse; an upper bound (<15% of rows) catches a matcher regression that would
    flag everything fuzzy.
    """
    df, low = live_universe_iqvia_sheet
    n = len(df)
    dist = df["iqvia_match_confidence"].value_counts(dropna=False).to_dict()
    assert low > 0, f"low-confidence KPI is dead — no fuzzy matches at full scale (dist={dist})"
    assert low >= 50, f"low-confidence count {low} far below the ~284 baseline (dist={dist})"
    assert low < n * 0.15, (
        f"low-confidence KPI exploded: {low}/{n} (>15%) — likely a matcher regression (dist={dist})"
    )


def test_real_one_build_per_window_then_reset_repulls(monkeypatch):
    """Live cache: get_universe builds once within the window; reset forces a re-pull."""
    if not _net_ok():
        pytest.skip("allfiles.zip unreachable")
    U.reset_universe_cache()

    parses = {"n": 0}
    real_load = U.load_dpd_universe_records

    def _counting_load(cache_dir=U.UNIVERSE_CACHE_DIR):
        parses["n"] += 1
        return real_load(cache_dir)

    monkeypatch.setattr(U, "load_dpd_universe_records", _counting_load)

    asyncio.run(U.get_universe())          # first build → downloads + parses
    asyncio.run(U.get_universe())          # within window → cached, no reparse
    assert parses["n"] == 1, "fresh universe must be reused within the 4h window"

    U.reset_universe_cache()               # clears in-process + on-disk extract
    asyncio.run(U.get_universe())          # must re-pull + reparse
    assert parses["n"] == 2, "reset must force a fresh allfiles.zip pull"
