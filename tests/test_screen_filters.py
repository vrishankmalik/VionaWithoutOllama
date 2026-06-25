"""Offline unit suite for the TWO new filter criteria in the shared screen layer.

Covers (per the locked spec):
  * Dosage-form base→raw map: the real 89 distinct DPD forms collapse to 55 bases,
    every raw maps to exactly one base, no value dropped/misfiled, the
    inconsistent-modifier clusters land where intended.
  * Dosage-form matching: one canonical selects EVERY raw variant under it (and
    nothing under a different base); multi-select unions; no selection ⇒ no
    constraint; multi-form "A; B" cells match on any split.
  * Six-year no-file date: MM/DD/YYYY future-only validation; stored-value parsing
    (YYYY-MM-DD / N/A / footnote / blank); operator boundaries; and the blank rules
    (less ⇒ include blanks, greater/greater_or_equal/equal ⇒ exclude blanks).
  * Additivity: with neither new field set, filter_products == apply_criteria
    (existing six untouched); AND-composition with the six; empty-result safety.

Real data: the 89 raw dosage forms come from a committed fixture sliced verbatim
from the live DPD catalogue (tests/scripts/build_dosage_forms_fixture.py). Date
"today" is pinned so future/past assertions are deterministic.
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.enrichment import screen as S
from app.enrichment.screen import (
    NoFileDateFilter,
    apply_criteria,
    apply_dosage_form_filter,
    apply_no_file_date_filter,
    base_dosage_form,
    build_dosage_form_map,
    compute_products,
    filter_products,
    parse_criteria,
    parse_dosage_forms,
    parse_no_file_date,
    parse_stored_no_file_date,
)

_TODAY = date(2026, 6, 24)
_FORMS_CSV = Path(__file__).parent / "fixtures" / "universe" / "dosage_forms_distinct.csv"

# Expected base-form count for the committed real fixture (89 raw → 55 base).
# Regenerate the fixture (build_dosage_forms_fixture.py) if the DPD catalogue
# changes; this number is an assertion against THIS committed slice, not live data.
_EXPECT_RAW = 89
_EXPECT_BASES = 55


def _real_raw_forms() -> list[str]:
    with open(_FORMS_CSV, encoding="utf-8", newline="") as fh:
        return [row["raw_value"] for row in csv.DictReader(fh)]


# ════════════════════════════════════════════════════════════════════════════
# B. Dosage-form base→raw map (validate the DATA first)
# ════════════════════════════════════════════════════════════════════════════

def test_base_collapse_anchor_examples():
    assert base_dosage_form("TABLET (EXTENDED-RELEASE)") == "TABLET"
    assert base_dosage_form("CAPSULE (DELAYED RELEASE)") == "CAPSULE"
    assert base_dosage_form("POWDER FOR SUSPENSION, SUSTAINED-RELEASE") == "POWDER FOR SUSPENSION"
    assert base_dosage_form("SPRAY, METERED DOSE") == "SPRAY"
    # Route-qualified forms keep their leading words (no '(' or ',') — distinct base.
    assert base_dosage_form("VAGINAL TABLET") == "VAGINAL TABLET"
    # A "weird" singleton collapses to itself, never dropped.
    assert base_dosage_form("DRUG PREMIX") == "DRUG PREMIX"


def test_all_89_real_raw_collapse_to_55_bases_no_loss():
    raws = _real_raw_forms()
    assert len(raws) == _EXPECT_RAW, f"fixture drift: {len(raws)} raw forms (regenerate)"
    m = build_dosage_form_map(raws)
    assert len(m) == _EXPECT_BASES, f"expected {_EXPECT_BASES} base forms, got {len(m)}"

    # Every raw value appears under EXACTLY ONE base, and none is dropped.
    placements = {raw: [b for b, variants in m.items() if raw in variants] for raw in raws}
    multi = {raw: bs for raw, bs in placements.items() if len(bs) != 1}
    assert not multi, f"raw values misfiled into !=1 base: {multi}"
    flat = sorted(v for variants in m.values() for v in variants)
    assert flat == sorted(raws), "a raw value was dropped or duplicated by the map"

    # Each base is itself the collapse of every raw filed under it (no cross-filing).
    for base, variants in m.items():
        for raw in variants:
            assert base_dosage_form(raw) == base, f"{raw!r} misfiled under {base!r}"


def test_inconsistent_modifier_variants_land_under_their_base():
    raws = _real_raw_forms()
    m = build_dosage_form_map(raws)
    # The hyphen/space-inconsistent release modifiers must not fragment the base.
    assert "TABLET (EXTENDED-RELEASE)" in m["TABLET"]
    assert "CAPSULE (EXTENDED RELEASE)" in m["CAPSULE"]
    assert "TABLET (DELAYED-RELEASE)" in m["TABLET"]
    assert "CAPSULE (DELAYED RELEASE)" in m["CAPSULE"]
    # TABLET base aggregates many real sub-forms (plain + several parenthetical ones).
    assert len(m["TABLET"]) >= 10, sorted(m["TABLET"])
    assert "TABLET" in m["TABLET"]


def test_map_handles_multiform_cells_and_is_record_sourced():
    # build_dosage_form_map consumes per-record dosage_form cells (the universe
    # source), including ';'-joined multi-form cells — NOT filtered sheet output.
    m = build_dosage_form_map(["TABLET", "CAPSULE; CAPSULE (DELAYED RELEASE)", "  ", None])
    assert m["TABLET"] == ["TABLET"]
    assert m["CAPSULE"] == ["CAPSULE", "CAPSULE (DELAYED RELEASE)"]


# ════════════════════════════════════════════════════════════════════════════
# B. Dosage-form matching (canonical → all raw variants)
# ════════════════════════════════════════════════════════════════════════════

def _dosage_products() -> pd.DataFrame:
    """One product per distinct raw form so the filter's reach is unambiguous."""
    rows = [
        {"din": "1", "ingredient": "DRUGA", "dosage_form": "TABLET",
         "company": "C1", "status": "marketed"},
        {"din": "2", "ingredient": "DRUGB", "dosage_form": "TABLET (EXTENDED-RELEASE)",
         "company": "C2", "status": "marketed"},
        {"din": "3", "ingredient": "DRUGC", "dosage_form": "TABLET (CHEWABLE)",
         "company": "C3", "status": "marketed"},
        {"din": "4", "ingredient": "DRUGD", "dosage_form": "CAPSULE",
         "company": "C4", "status": "marketed"},
        {"din": "5", "ingredient": "DRUGE", "dosage_form": "CAPSULE (DELAYED RELEASE)",
         "company": "C5", "status": "marketed"},
        # multi-form cell: matches if ANY split maps to the selected base.
        {"din": "6", "ingredient": "DRUGF", "dosage_form": "KIT; TABLET",
         "company": "C6", "status": "marketed"},
    ]
    products, _ = compute_products(pd.DataFrame(rows), pd.DataFrame())
    return products


def test_canonical_selects_every_raw_variant_under_it():
    products = _dosage_products()
    q = apply_dosage_form_filter(products, ["TABLET"])
    forms = set(q["dosage_form"])
    # plain + extended-release + chewable + the multi-form KIT;TABLET — all TABLET.
    assert forms == {"TABLET", "TABLET (EXTENDED-RELEASE)", "TABLET (CHEWABLE)", "KIT; TABLET"}
    # No CAPSULE product leaks in.
    assert not any("CAPSULE" in f for f in forms)


def test_dosage_multiselect_unions_mapped_sets():
    products = _dosage_products()
    q = apply_dosage_form_filter(products, ["TABLET", "CAPSULE"])
    forms = set(q["dosage_form"])
    assert forms == {
        "TABLET", "TABLET (EXTENDED-RELEASE)", "TABLET (CHEWABLE)",
        "CAPSULE", "CAPSULE (DELAYED RELEASE)", "KIT; TABLET",
    }


def test_no_dosage_selection_does_not_constrain():
    products = _dosage_products()
    assert len(apply_dosage_form_filter(products, [])) == len(products)
    assert len(apply_dosage_form_filter(products, None)) == len(products)


def test_dosage_selection_with_no_hits_is_empty_not_crash():
    products = _dosage_products()
    q = apply_dosage_form_filter(products, ["LOTION"])
    assert q.empty


def test_parse_dosage_forms_from_request_list():
    raw = [
        {"metric": "competitors", "operator": "above", "value": 1},
        {"metric": "dosage_form", "value": ["TABLET", "capsule", "  "]},
    ]
    assert parse_dosage_forms(raw) == ["CAPSULE", "TABLET"]  # upper, de-duped, sorted
    # parse_criteria must SKIP the dosage_form entry (it is not a numeric criterion).
    assert [c.metric for c in parse_criteria(raw)] == ["competitors"]


# ════════════════════════════════════════════════════════════════════════════
# C. Six-year no-file date — parsing + validation
# ════════════════════════════════════════════════════════════════════════════

def test_parse_stored_value_variants():
    assert parse_stored_no_file_date("2030-01-15") == date(2030, 1, 15)
    # footnote-annotated → still extracts the leading ISO date
    assert parse_stored_no_file_date("2027-04-14 Footnote 3") == date(2027, 4, 14)
    # blank / N/A / junk → None (treated as blank)
    for blank in (None, "", "   ", "N/A", "n/a", "-", "see note", "TBD"):
        assert parse_stored_no_file_date(blank) is None, blank


def test_user_date_must_be_mdy_and_future():
    assert S._parse_user_mdy("12/31/2030", today=_TODAY) == date(2030, 12, 31)
    # past + today are rejected (future-only)
    for bad in ("01/01/2020", "06/24/2026"):
        with pytest.raises(ValueError):
            S._parse_user_mdy(bad, today=_TODAY)
    # malformed / ambiguous formats rejected
    for bad in ("2030-01-01", "1/1/2030", "13/01/2030", "01/32/2030", "31/12/2030", "abc"):
        with pytest.raises(ValueError):
            S._parse_user_mdy(bad, today=_TODAY)


def test_mdy_is_month_day_not_day_month():
    # 03/04/2030 is March 4, never April 3 — guards against MM/DD vs DD/MM mix-up.
    assert S._parse_user_mdy("03/04/2030", today=_TODAY) == date(2030, 3, 4)


def test_parse_no_file_date_from_request_list():
    raw = [{"metric": "no_file_date", "operator": "greater", "value": "01/01/2028"}]
    f = parse_no_file_date(raw, today=_TODAY)
    assert f.operator == "greater" and f.threshold == date(2028, 1, 1)
    # value-less entry ⇒ no filter (additive); bad operator ⇒ raise
    assert parse_no_file_date([{"metric": "no_file_date", "operator": "greater", "value": ""}]) is None
    with pytest.raises(ValueError):
        parse_no_file_date([{"metric": "no_file_date", "operator": "between", "value": "01/01/2028"}],
                           today=_TODAY)
    # parse_criteria skips it
    assert parse_criteria(raw) == []


# ════════════════════════════════════════════════════════════════════════════
# C. Six-year no-file date — operators + blank rules (the crux)
# ════════════════════════════════════════════════════════════════════════════

def _date_products() -> pd.DataFrame:
    """Four products: dates 2027 / 2030 and two BLANK (one N/A, one no register)."""
    rows = [
        {"din": "1", "ingredient": "A", "dosage_form": "TABLET", "company": "C1",
         "status": "marketed", "dp_6yr_no_file_date": "2027-01-01"},
        {"din": "2", "ingredient": "B", "dosage_form": "TABLET", "company": "C2",
         "status": "marketed", "dp_6yr_no_file_date": "2030-01-01"},
        {"din": "3", "ingredient": "C", "dosage_form": "TABLET", "company": "C3",
         "status": "marketed", "dp_6yr_no_file_date": "N/A"},     # blank
        {"din": "4", "ingredient": "D", "dosage_form": "TABLET", "company": "C4",
         "status": "marketed", "dp_6yr_no_file_date": ""},        # blank
    ]
    products, _ = compute_products(pd.DataFrame(rows), pd.DataFrame())
    return products


def _ings(df) -> set:
    return set(df["ingredient"])


def _apply(products, op, threshold: date):
    # Construct the filter threshold directly: we are testing operator + blank
    # logic at arbitrary boundaries, not the future-only INPUT validation (which is
    # exercised by test_user_date_must_be_mdy_and_future).
    return apply_no_file_date_filter(products, NoFileDateFilter(op, threshold))


def test_compute_products_blank_rules_and_representative_date():
    products = _date_products()
    by = {r["ingredient"]: r["_no_file_date"] for _, r in products.iterrows()}
    assert by["A"] == date(2027, 1, 1)
    assert by["B"] == date(2030, 1, 1)
    assert by["C"] is None and by["D"] is None  # N/A + empty → blank


def test_date_operators_at_boundaries():
    products = _date_products()
    # greater than 2028 → only 2030 (blanks excluded)
    assert _ings(_apply(products, "greater", date(2028, 1, 1))) == {"B"}
    # greater_or_equal 2030-01-01 → 2030 included at the boundary (blanks excluded)
    assert _ings(_apply(products, "greater_or_equal", date(2030, 1, 1))) == {"B"}
    # equal exact boundary → only the exact date (blanks excluded)
    assert _ings(_apply(products, "equal", date(2027, 1, 1))) == {"A"}
    # less than 2028 → 2027 AND both blanks included
    assert _ings(_apply(products, "less", date(2028, 1, 1))) == {"A", "C", "D"}


def test_blank_rule_counts_numeric():
    products = _date_products()  # exactly 2 blank products
    assert len(_apply(products, "less", date(2099, 1, 1))) == 4      # all incl. 2 blanks
    assert len(_apply(products, "greater", date(2000, 1, 1))) == 2   # only the 2 dated
    assert len(_apply(products, "greater_or_equal", date(2000, 1, 1))) == 2
    assert len(_apply(products, "equal", date(2030, 1, 1))) == 1


def test_no_date_filter_does_not_constrain():
    products = _date_products()
    assert len(apply_no_file_date_filter(products, None)) == len(products)


# ════════════════════════════════════════════════════════════════════════════
# A + D. Additivity and AND-composition with the existing six
# ════════════════════════════════════════════════════════════════════════════

def _mixed_products() -> pd.DataFrame:
    rows = [
        {"din": "1", "ingredient": "A", "dosage_form": "TABLET", "company": "C1",
         "status": "marketed", "dp_6yr_no_file_date": "2030-01-01"},
        {"din": "2", "ingredient": "A", "dosage_form": "TABLET", "company": "C2",
         "status": "marketed", "dp_6yr_no_file_date": "2030-01-01"},
        {"din": "3", "ingredient": "B", "dosage_form": "CAPSULE", "company": "C3",
         "status": "marketed", "dp_6yr_no_file_date": "2020-01-01"},
    ]
    products, _ = compute_products(pd.DataFrame(rows), pd.DataFrame())
    return products


def test_empty_new_fields_identical_to_apply_criteria():
    products = _mixed_products()
    criteria = parse_criteria([{"metric": "competitors", "operator": "above", "value": 0}])
    base = apply_criteria(products, criteria).reset_index(drop=True)
    additive = filter_products(products, criteria, dosage_bases=None, date_filter=None).reset_index(drop=True)
    pd.testing.assert_frame_equal(base, additive)


def test_and_composition_dosage_plus_date_plus_six():
    products = _mixed_products()
    criteria = parse_criteria([{"metric": "competitors", "operator": "above", "value": 1}])
    f = NoFileDateFilter("greater", S._parse_user_mdy("01/01/2028", today=_TODAY))
    # competitors>1 (A only: 2 marketed cos) AND TABLET AND date>2028 → just A.
    q = filter_products(products, criteria, dosage_bases=["TABLET"], date_filter=f)
    assert set(zip(q["ingredient"], q["dosage_form"])) == {("A", "TABLET")}


def test_over_constrained_yields_empty_frame_not_crash():
    products = _mixed_products()
    f = NoFileDateFilter("greater", S._parse_user_mdy("01/01/2099", today=_TODAY))
    q = filter_products(products, [], dosage_bases=["LOTION"], date_filter=f)
    assert q.empty
    assert list(q.columns) == list(products.columns)
