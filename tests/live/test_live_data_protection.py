"""Part 1 — Live: Register of Innovative Drugs (data protection) on real data.

Covers, against the LIVE register + LIVE DPD universe:
  * the register parses a non-trivial number of active rows;
  * the no-PDF universe attaches a non-blank dp date to a non-zero count of DINs;
  * named innovators carry their exact recorded dates
        clesrovimab / Merck            → 2032-01-30
        alpelisib (DIN 02497069) / Novartis → 2026-03-11
  * cross-tab parity AT SCALE — the per-product Search dp value and the
    full-universe dp value are identical for the same DIN;
  * the fuzzy-match invariants hold on the live join: no fabricated dates, distinct
    (ingredient, manufacturer) identities never exceed the register row count, and
    the attach fan-out is bounded.
"""
from __future__ import annotations

import asyncio

import pytest

from app.enrichment.data_protection import (
    _match_data_protection_deterministic as match_det,
)
from app.enrichment.universe import build_universe_response, build_universe_sheet1
from app.enrichment.workbook import build_sheet1
from tests.live.conftest import present

pytestmark = pytest.mark.integration

_ALPELISIB_DIN = "02497069"


@pytest.fixture(scope="session")
def live_universe_dp_sheet(live_universe):
    """Universe Sheet 1 with dp populated (no IQVIA needed) — built once."""
    df, _recon, _low = build_universe_sheet1(
        build_universe_response(live_universe), None, dp_table=live_universe.dp_table
    )
    return df


# ── Register sanity + named anchors ───────────────────────────────────────────

def test_register_has_active_rows(live_dp_table):
    assert len(live_dp_table) > 100, f"register parsed only {len(live_dp_table)} rows"


def test_clesrovimab_anchor_live(live_dp_table):
    cols = match_det("clesrovimab", "Merck Canada Inc.", live_dp_table)
    assert cols, "clesrovimab/Merck must match a live Register row"
    assert cols["dp_6yr_no_file_date"] == "2032-01-30", cols
    assert cols["data_protection_ends"] == "2034-07-30", cols
    assert cols["pediatric_extension"] == "Yes", cols


def test_alpelisib_anchor_live(live_dp_table):
    cols = match_det("alpelisib", "Novartis Pharmaceuticals Canada Inc.", live_dp_table)
    assert cols, "alpelisib/Novartis must match a live Register row"
    assert cols["dp_6yr_no_file_date"] == "2026-03-11", cols
    assert cols["data_protection_ends"] == "2028-03-11", cols
    assert cols["pediatric_extension"] == "No", cols  # Register prints N/A → "No"


# ── dp populated across the live universe (KPI alive) ─────────────────────────

def test_universe_dp_nonblank_count_positive(live_universe_dp_sheet):
    df = live_universe_dp_sheet
    assert "dp_6yr_no_file_date" in df.columns, df.columns.tolist()
    nonblank = int(df["dp_6yr_no_file_date"].apply(present).sum())
    # Observed ~434 of 13,550 (3.2%) on the live universe 2026-06-25.  A floor well
    # below that catches a dead join; the bound test below catches over-matching.
    assert nonblank > 50, f"only {nonblank} DINs carry a data-protection date — join may be dead"


def test_universe_alpelisib_din_carries_real_date(live_universe_dp_sheet):
    """The hand-verified alpelisib DIN resolves to its Register date on the universe."""
    df = live_universe_dp_sheet
    row = df[df["din"].astype(str) == _ALPELISIB_DIN]
    if row.empty:
        pytest.skip(f"alpelisib DIN {_ALPELISIB_DIN} not in this catalogue snapshot")
    assert str(row["dp_6yr_no_file_date"].iloc[0]).strip() == "2026-03-11", row["dp_6yr_no_file_date"].iloc[0]


# ── Cross-tab parity at scale: Search dp == Universe dp, per DIN ──────────────

def test_search_vs_universe_dp_parity(live_dp_table, live_universe_dp_sheet):
    """For every DIN the per-product Search path returns, its dp date matches the
    full-universe dp date for that same DIN — the two tabs never disagree."""
    if not _reachable_dpd():
        pytest.skip("DPD API unreachable — parity test skipped")
    from app.main import search

    uni = live_universe_dp_sheet
    uni_dp = {
        str(r["din"]).strip(): (str(r["dp_6yr_no_file_date"]).strip() if present(r["dp_6yr_no_file_date"]) else "")
        for _, r in uni.iterrows()
    }

    mismatches = []
    checked = 0
    for ingredient in ("alpelisib", "apremilast", "abrocitinib"):
        resp = asyncio.run(search(q=ingredient, field="ingredient"))
        s1 = build_sheet1(resp, dp_table=live_dp_table, ingredient_name=ingredient)
        for _, row in s1.iterrows():
            din = str(row["din"]).strip()
            if din not in uni_dp:
                continue  # DIN not in the universe snapshot (timing churn) — skip
            v = row.get("dp_6yr_no_file_date")
            search_val = str(v).strip() if present(v) else ""
            if search_val != uni_dp[din]:
                mismatches.append((din, search_val, uni_dp[din]))
            checked += 1
    assert checked > 0, "no overlapping DINs found to compare — parity unverifiable"
    assert not mismatches, f"Search vs Universe dp disagreements (din, search, universe): {mismatches[:8]}"


# ── Fuzzy-match invariants on the live join ───────────────────────────────────

def test_no_fabricated_dates_on_live_universe(live_dp_table, live_universe_dp_sheet):
    """Every dp date attached on the universe exists verbatim in the Register."""
    register_dates = {str(r["no_file_date"]).strip() for r in live_dp_table}
    attached = {
        str(v).strip()
        for v in live_universe_dp_sheet["dp_6yr_no_file_date"].tolist()
        if present(v)
    }
    fabricated = attached - register_dates
    assert not fabricated, f"dp dates not present in the Register (fabricated): {sorted(fabricated)[:8]}"


def test_distinct_identities_within_register_bound(live_dp_table, live_universe_dp_sheet):
    """The matcher can never emit more distinct identities than the Register holds.

    Every Register row defines one (no_file_date, data_protection_ends, pediatric)
    output triple; a match can only REPRODUCE an existing triple, never invent one.
    So the count of distinct attached triples must be ≤ the register row count.
    """
    df = live_universe_dp_sheet
    triples = set()
    for _, r in df.iterrows():
        if not present(r.get("dp_6yr_no_file_date")):
            continue
        triples.add((
            str(r.get("dp_6yr_no_file_date")).strip(),
            str(r.get("data_protection_ends") or "").strip(),
            str(r.get("pediatric_extension") or "").strip(),
        ))
    assert len(triples) <= len(live_dp_table), (
        f"{len(triples)} distinct attached identities exceed {len(live_dp_table)} register rows"
    )


def test_dp_attach_fanout_is_bounded(live_universe_dp_sheet):
    """Bounded fan-out: only a small minority of the catalogue is innovative-drug
    protected, so a runaway over-match (substring collisions attaching everywhere)
    is caught.  Observed ~3.2% on the live universe 2026-06-25."""
    df = live_universe_dp_sheet
    matched = int(df["dp_6yr_no_file_date"].apply(present).sum())
    frac = matched / max(len(df), 1)
    assert frac < 0.25, f"dp attached to {frac:.1%} of the universe — likely over-matching"


def _reachable_dpd() -> bool:
    from tests.live.conftest import reachable, _DPD_PROBE
    return reachable(_DPD_PROBE)
