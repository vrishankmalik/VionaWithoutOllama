"""Derive the distinct DPD dosage-form fixture from REAL live data — never hand-typed.

Produces a committed, reproducible fixture so the offline dosage-form filter tests
(`tests/test_screen_filters.py`) assert the base→raw collapse against the genuine
full set of DPD dosage-form strings:

  tests/fixtures/universe/dosage_forms_distinct.csv
      Every distinct raw `form.txt` dosage-form value across the whole live
      catalogue, with the number of drug_codes carrying it. Columns:
      raw_value,n_drug_codes. Saved verbatim — no normalization applied here; the
      collapse is what the test exercises.

Run (needs network + the pandas-WMI shim on this host — see project memory):

    PYTHONPATH=<pyshim> python -m tests.scripts.build_dosage_forms_fixture

It reuses the universe loader's allfiles.zip extract (downloading via the
production get_universe path if not already fresh), then reads form.txt directly.
"""
from __future__ import annotations

import asyncio
import csv
import sys
from collections import defaultdict
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_OUT = _REPO / "tests" / "fixtures" / "universe"
_FORM_COL_CODE, _FORM_COL_NAME = 0, 2  # mirrors app/enrichment/universe.py


def main() -> int:
    from app.enrichment.universe import get_universe, UNIVERSE_CACHE_DIR

    # Ensure the production extract is present/fresh (downloads allfiles.zip if stale).
    asyncio.run(get_universe())
    form_path = UNIVERSE_CACHE_DIR / "form.txt"
    if not form_path.exists():
        raise SystemExit(f"form.txt not found at {form_path} — universe build failed.")

    by_code: dict[str, set[str]] = defaultdict(set)
    with open(form_path, encoding="latin-1", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) > _FORM_COL_NAME and row[_FORM_COL_CODE] and row[_FORM_COL_NAME].strip():
                by_code[row[_FORM_COL_NAME]].add(row[_FORM_COL_CODE])

    rows = sorted(by_code.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    _OUT.mkdir(parents=True, exist_ok=True)
    out = _OUT / "dosage_forms_distinct.csv"
    with open(out, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["raw_value", "n_drug_codes"])
        for raw, codes in rows:
            w.writerow([raw, len(codes)])

    print("===== DOSAGE-FORM FIXTURE MANIFEST =====")
    print(f"distinct raw dosage-form values: {len(rows)}")
    print(f"top 5: {[r[0] for r in rows[:5]]}")
    print(f"written: {out}")
    print("===== END MANIFEST =====")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(_REPO))
    raise SystemExit(main())
