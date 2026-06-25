"""Derive the data-protection filter fixtures from REAL live data — never hand-typed.

Produces two committed, reproducible fixtures so the offline filter suite
(`tests/test_dp_register_parse.py`, `tests/test_universe_filters.py`) asserts
against genuine Register-of-Innovative-Drugs rows and the genuine 6-year-no-file
date join, while running fully offline in the default `make test` pass:

  tests/fixtures/data_protection/register_active_sample.html
      The REAL "Active Data Protection Period" table (#a1), saved verbatim from the
      live Register page so the parser is exercised against real markup at full
      scale (~300 rows). No cells are edited.

  tests/fixtures/data_protection/dp_join_products.json
      The REAL DPD-product → 6-year-date join, computed by running the production
      matcher (`_get_dp_cols`) over the whole live universe. Each product carries
      its real DPD identity (din/ingredient/company/dosage_form/status) and the
      date the matcher attached (or null). Three buckets are kept:
        matched   — products that received a real date (the innovator products)
        near_miss — products sharing an ingredient with a Register row but a
                    DIFFERENT manufacturer, which must stay blank (no false attach)
        blank     — products with no Register overlap at all (stay blank)
      Named hand-verifiable anchors are surfaced in the printed MANIFEST.

Run (needs network + the pandas-WMI shim on this host — see project memory):

    PYTHONPATH=<pyshim> python -m tests.scripts.build_dp_register_fixture
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

_REPO = Path(__file__).resolve().parents[2]
_OUT = _REPO / "tests" / "fixtures" / "data_protection"

# A few well-known innovator anchors to surface in the manifest (hand-verifiable
# on the live Register page). Not used to filter — only to print for the maintainer.
_ANCHOR_INGREDIENTS = ("alpelisib", "abemaciclib", "abrocitinib", "upadacitinib")


def _save_register_html() -> int:
    from app.enrichment.data_protection import _REGISTER_URL, _find_active_table
    from app.config import USER_AGENT

    print("Downloading live Register of Innovative Drugs…")
    r = httpx.get(_REGISTER_URL, headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
                  follow_redirects=True, timeout=60.0)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    table = _find_active_table(soup)
    if table is None:
        raise SystemExit("Active table not found on live Register page — layout changed.")
    # Ensure the saved fixture is found by the same id="a1" anchor the parser uses.
    table["id"] = "a1"
    n_rows = len(table.find_all("tr"))
    _OUT.mkdir(parents=True, exist_ok=True)
    html = (
        "<!-- REAL Register 'Active Data Protection Period' table, saved verbatim by\n"
        "     tests/scripts/build_dp_register_fixture.py. Do not hand-edit. -->\n"
        "<html><body>\n" + str(table) + "\n</body></html>\n"
    )
    (_OUT / "register_active_sample.html").write_text(html, encoding="utf-8")
    print(f"  saved register_active_sample.html ({n_rows} <tr> rows)")
    return n_rows


def _build_join() -> dict:
    """Run the production matcher over the whole live universe → the real join."""
    from app.enrichment.universe import get_universe
    from app.enrichment.workbook import _get_dp_cols
    from app.enrichment.data_protection import (
        _normalize_ingredient_dp as NI, _normalize_manufacturer as NM,
    )

    bundle = asyncio.run(get_universe())
    dp = bundle.dp_table or []
    reg_ings = {NI(r.get("medicinal_ingredient", "")) for r in dp}

    matched, near_miss, blank = [], [], []
    for rec in bundle.dpd_records:
        cols = _get_dp_cols(rec.ingredient, rec.company, dp)
        date = (cols.get("dp_6yr_no_file_date") or "").strip()
        item = {
            "din": rec.din,
            "ingredient": rec.ingredient,
            "company": rec.company,
            "dosage_form": rec.dosage_form,
            "status": rec.status,
            "expected_no_file_date": date or None,
        }
        if date:
            matched.append(item)
            continue
        ni = NI(rec.ingredient or "")
        # near-miss: ingredient overlaps a Register ingredient but no date attached
        if ni and any(ni == ri or ni in ri or ri in ni for ri in reg_ings if ri):
            near_miss.append(item)
        else:
            blank.append(item)

    # Keep ALL matched (the innovator universe) + a deterministic, bounded sample of
    # the negative cases (sorted by DIN so the fixture is stable across regenerations).
    near_miss.sort(key=lambda x: x["din"] or "")
    blank.sort(key=lambda x: x["din"] or "")
    return {
        "register_row_count": len(dp),
        "universe_record_count": len(bundle.dpd_records),
        "matched": sorted(matched, key=lambda x: x["din"] or ""),
        "near_miss": near_miss[:40],
        "blank": blank[:80],
    }


def main() -> int:
    n_rows = _save_register_html()
    join = _build_join()
    (_OUT / "dp_join_products.json").write_text(
        json.dumps(join, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print("\n===== DP FILTER FIXTURE MANIFEST =====")
    print(f"register rows (html)      : {n_rows}")
    print(f"register rows (parsed)    : {join['register_row_count']}")
    print(f"universe records          : {join['universe_record_count']}")
    print(f"matched products          : {len(join['matched'])}")
    print(f"near_miss kept            : {len(join['near_miss'])}")
    print(f"blank kept                : {len(join['blank'])}")
    dates = {m["expected_no_file_date"] for m in join["matched"]}
    print(f"distinct attached dates   : {len(dates)}")
    for name in _ANCHOR_INGREDIENTS:
        hits = [m for m in join["matched"] if name in (m["ingredient"] or "").lower()]
        if hits:
            h = hits[0]
            print(f"  anchor {name:14s}: DIN {h['din']} {h['company']!r} "
                  f"date={h['expected_no_file_date']} ({len(hits)} DIN[s])")
    print("===== END MANIFEST =====")
    print(f"\nFixtures written under {_OUT}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(_REPO))
    raise SystemExit(main())
