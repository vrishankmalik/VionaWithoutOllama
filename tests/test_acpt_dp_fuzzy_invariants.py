"""Acceptance: data-protection fuzzy-match INVARIANTS at real fixture scale.

Operates over the committed ~300-row Active Data Protection register fixture and
the whole-universe join fixture (matched / near_miss / blank) produced by the
production matcher.  These are PROPERTY/INVARIANT checks layered on top of the
per-row anchors already in tests/test_dp_register_parse.py — they assert the
matcher can never fabricate a date, never invent more identities than the
register holds, never false-attach a near-miss manufacturer, and only ever emits
"Yes"/"No" for the pediatric extension.

Offline; no network.  Anchors are taken verbatim from the live Register page and
pinned in the task spec:
  clesrovimab / Enflonsia / Merck Canada Inc / submission 295182:
      no_file_date 2032-01-30, data_protection_ends 2034-07-30, pediatric "Yes".
  alpelisib   / Piqray    / Novartis ...      / submission 226941:
      no_file_date 2026-03-11, data_protection_ends 2028-03-11, pediatric N/A → "No".
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from app.enrichment.data_protection import (
    _extract_dp_fields,
    _find_active_table,
    _match_data_protection_deterministic,
    _normalize_ingredient_dp as NI,
    _normalize_manufacturer as NM,
    _parse_data_protection_table,
)
from app.enrichment.screen import parse_stored_no_file_date

_FIX = Path(__file__).parent / "fixtures" / "data_protection"
_HTML = _FIX / "register_active_sample.html"
_JOIN = _FIX / "dp_join_products.json"

pytestmark = pytest.mark.skipif(
    not _HTML.exists() or not _JOIN.exists(),
    reason="dp fixtures missing — run python -m tests.scripts.build_dp_register_fixture",
)


def _dp_table() -> list[dict]:
    soup = BeautifulSoup(_HTML.read_text(encoding="utf-8"), "html.parser")
    return _parse_data_protection_table(_find_active_table(soup))


def _join() -> dict:
    return json.loads(_JOIN.read_text(encoding="utf-8"))


# ════════════════════════════════════════════════════════════════════════════
# Named anchors — exact dates, no fabrication, pediatric normalization
# ════════════════════════════════════════════════════════════════════════════

def test_clesrovimab_anchor_exact_dates_and_pediatric_yes():
    """clesrovimab / Enflonsia / Merck — every output field pinned to the Register."""
    dp = _dp_table()
    cols = _match_data_protection_deterministic("clesrovimab", "Merck Canada Inc.", dp)
    assert cols, "clesrovimab/Merck must match a Register row"
    assert cols["dp_6yr_no_file_date"] == "2032-01-30", cols
    assert cols["data_protection_ends"] == "2034-07-30", cols
    assert cols["pediatric_extension"] == "Yes", cols


def test_alpelisib_anchor_exact_dates_and_pediatric_no():
    """alpelisib / Piqray / Novartis — pediatric N/A on the Register → output 'No'."""
    dp = _dp_table()
    cols = _match_data_protection_deterministic(
        "alpelisib", "Novartis Pharmaceuticals Canada Inc.", dp
    )
    assert cols, "alpelisib/Novartis must match a Register row"
    assert cols["dp_6yr_no_file_date"] == "2026-03-11", cols
    assert cols["data_protection_ends"] == "2028-03-11", cols
    # The Register prints N/A in the pediatric column → must normalize to "No".
    assert cols["pediatric_extension"] == "No", cols


# ════════════════════════════════════════════════════════════════════════════
# No fabricated dates — every matched product reproduces its recorded date
# ════════════════════════════════════════════════════════════════════════════

def test_every_matched_product_reproduces_its_exact_recorded_date():
    """For every product the production matcher recorded as matched, the live
    deterministic matcher returns EXACTLY the fixture's expected date — never a
    different (fabricated) one."""
    dp = _dp_table()
    matched = _join()["matched"]
    assert len(matched) > 100, "fixture should carry the full real matched set"
    mismatches = []
    for p in matched:
        cols = _match_data_protection_deterministic(p["ingredient"], p["company"], dp)
        got = cols.get("dp_6yr_no_file_date") or ""
        if got != p["expected_no_file_date"]:
            mismatches.append((p["din"], p["expected_no_file_date"], got))
    assert not mismatches, f"matched products whose date drifted: {mismatches[:8]}"


def test_matched_dates_are_all_drawn_from_the_register():
    """No attached date is absent from the Register column (the definition of
    'not fabricated')."""
    dp = _dp_table()
    register_dates = {r["no_file_date"].strip() for r in dp}
    attached = set()
    for p in _join()["matched"]:
        cols = _match_data_protection_deterministic(p["ingredient"], p["company"], dp)
        d = (cols.get("dp_6yr_no_file_date") or "").strip()
        if d:
            attached.add(d)
    fabricated = attached - register_dates
    assert not fabricated, f"attached dates absent from the Register: {fabricated}"


# ════════════════════════════════════════════════════════════════════════════
# Near-miss manufacturers never false-attach
# ════════════════════════════════════════════════════════════════════════════

def test_near_miss_manufacturer_never_attaches_a_date():
    """A product that shares an ingredient with a Register row but has a DIFFERENT
    manufacturer must return {} (blank) — never inherit the row's date."""
    dp = _dp_table()
    near = _join()["near_miss"]
    assert near, "fixture should carry near-miss cases"
    false_attaches = []
    for p in near:
        cols = _match_data_protection_deterministic(p["ingredient"], p["company"], dp)
        if cols.get("dp_6yr_no_file_date"):
            false_attaches.append((p["din"], p["ingredient"], p["company"], cols))
    assert not false_attaches, f"near-miss false attaches: {false_attaches[:8]}"


def test_no_register_product_returns_blank_dict():
    dp = _dp_table()
    blanks = _join()["blank"]
    assert blanks, "fixture should carry no-Register generics"
    for p in blanks:
        cols = _match_data_protection_deterministic(p["ingredient"], p["company"], dp)
        # Either {} or no date — both mean "did not attach".
        assert not cols.get("dp_6yr_no_file_date"), (p["din"], cols)


# ════════════════════════════════════════════════════════════════════════════
# Identity bound — matching can never invent more identities than rows exist
# ════════════════════════════════════════════════════════════════════════════

def test_distinct_matched_identities_do_not_exceed_register_rows():
    """The number of DISTINCT (normalized ingredient, normalized manufacturer)
    pairs that successfully match is ≤ the register row count."""
    dp = _dp_table()
    join = _join()
    register_row_count = join["register_row_count"]
    assert register_row_count == len(dp), (register_row_count, len(dp))

    identities = set()
    for bucket in ("matched", "near_miss", "blank"):
        for p in join[bucket]:
            cols = _match_data_protection_deterministic(p["ingredient"], p["company"], dp)
            if cols.get("dp_6yr_no_file_date"):
                identities.add((NI(p["ingredient"] or ""), NM(p["company"] or "")))
    assert len(identities) <= register_row_count, (len(identities), register_row_count)


# ════════════════════════════════════════════════════════════════════════════
# Ambiguity → blank (never guess)
# ════════════════════════════════════════════════════════════════════════════

def test_ambiguous_exact_manufacturer_shortlist_returns_blank():
    """When the ingredient shortlist has MORE THAN ONE exact-manufacturer hit, the
    matcher must return {} rather than guess which row to attach."""
    # Build a synthetic two-row register where both rows share the same normalized
    # ingredient AND the same normalized manufacturer but carry DIFFERENT dates.
    dp = [
        {
            "medicinal_ingredient": "fictomab",
            "submission_number": "900001",
            "innovative_drug": "Brand A",
            "manufacturer": "Acme Pharma Inc.",
            "noc_date": "2020-01-01",
            "no_file_date": "2030-01-01",
            "pediatric_extension": "No",
            "data_protection_ends": "2032-01-01",
        },
        {
            "medicinal_ingredient": "fictomab",
            "submission_number": "900002",
            "innovative_drug": "Brand B",
            "manufacturer": "Acme Pharma Ltd.",  # normalizes to same as row 1
            "noc_date": "2021-01-01",
            "no_file_date": "2031-06-06",
            "pediatric_extension": "Yes",
            "data_protection_ends": "2033-06-06",
        },
    ]
    # Sanity: the two manufacturers really do collapse to the same normalized form.
    assert NM("Acme Pharma Inc.") == NM("Acme Pharma Ltd.")
    cols = _match_data_protection_deterministic("fictomab", "Acme Pharma Inc.", dp)
    assert cols == {}, f"ambiguous (>1 exact hit) must return blank, got {cols}"


def test_real_register_has_no_ambiguous_pair_that_silently_attaches():
    """Sweep the real matched set: no product attaches a date when its ingredient
    shortlist holds >1 exact-manufacturer hit (would be an unflagged guess)."""
    dp = _dp_table()
    leaked = []
    for p in _join()["matched"]:
        ing_norm = NI(p["ingredient"] or "")
        if not ing_norm:
            continue
        shortlist = [
            r for r in dp
            if ing_norm in NI(r.get("medicinal_ingredient", ""))
            or NI(r.get("medicinal_ingredient", "")) in ing_norm
        ]
        mfr_norm = NM(p["company"] or "")
        exact = [r for r in shortlist if NM(r.get("manufacturer", "")) == mfr_norm]
        if len(exact) > 1:
            cols = _match_data_protection_deterministic(p["ingredient"], p["company"], dp)
            if cols.get("dp_6yr_no_file_date"):
                leaked.append((p["din"], len(exact), cols["dp_6yr_no_file_date"]))
    assert not leaked, f"ambiguous shortlists that still attached a date: {leaked[:8]}"


# ════════════════════════════════════════════════════════════════════════════
# Pediatric normalization — only "Yes"/"No" ever emitted
# ════════════════════════════════════════════════════════════════════════════

def test_extract_dp_fields_pediatric_only_yes_or_no_over_whole_register():
    dp = _dp_table()
    for r in dp:
        out = _extract_dp_fields(r)
        assert out["pediatric_extension"] in ("Yes", "No"), (
            r.get("medicinal_ingredient"), r.get("pediatric_extension"),
            out["pediatric_extension"],
        )


def test_extract_dp_fields_pediatric_sentinels_collapse_to_no():
    base = {"no_file_date": "2030-01-01", "data_protection_ends": "2032-01-01"}
    for sentinel in ("N/A", "", "-", "n/a", "  ", "unknown", "maybe", "Pending"):
        out = _extract_dp_fields({**base, "pediatric_extension": sentinel})
        assert out["pediatric_extension"] == "No", (sentinel, out["pediatric_extension"])
    for yes in ("Yes", "yes", "Y", "1", "oui", "OUI"):
        out = _extract_dp_fields({**base, "pediatric_extension": yes})
        assert out["pediatric_extension"] == "Yes", (yes, out["pediatric_extension"])


# ════════════════════════════════════════════════════════════════════════════
# DP-column consumer side: stored-date sentinels parse per the documented rules
# ════════════════════════════════════════════════════════════════════════════

def test_parse_stored_no_file_date_sentinels():
    # Blank / unparseable → None.
    for blank in (None, "", "   ", "N/A", "n/a", "-", "TBD", "see footnote", "20300101"):
        assert parse_stored_no_file_date(blank) is None, blank
    # Real ISO date, optionally footnote-annotated → that date.
    assert parse_stored_no_file_date("2032-01-30") == date(2032, 1, 30)
    assert parse_stored_no_file_date("2026-03-11 (note 4)") == date(2026, 3, 11)
    assert parse_stored_no_file_date("  2028-3-1  ") == date(2028, 3, 1)
    # Impossible calendar date embedded → None (ValueError swallowed).
    assert parse_stored_no_file_date("2030-13-40") is None


def test_every_register_stored_date_parses_or_is_blank():
    """No Register cell yields a fabricated/garbage date object — each is either a
    real date or blank (None)."""
    dp = _dp_table()
    for r in dp:
        val = r["no_file_date"]
        parsed = parse_stored_no_file_date(val)
        assert parsed is None or isinstance(parsed, date), (val, parsed)
