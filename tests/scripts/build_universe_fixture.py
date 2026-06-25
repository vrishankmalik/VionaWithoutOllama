"""Derive the Full-universe test fixtures from the REAL live data — never hand-typed.

Produces two trimmed-but-real fixtures so the universe regression suite
(`tests/test_universe_coverage.py`) can run fully OFFLINE in the default
`make test` run while still asserting against genuine Health Canada / IQVIA rows:

  tests/fixtures/universe/extract/{drug,ingred,form,route,status,comp}.txt
      A few hundred real `allfiles.zip` rows, filtered to a curated DIN cohort
      (the IQVIA anchor DINs, the GLUCOPHAGE 02099233 column-verify anchor, a
      grandfathered DIN with no IQVIA presence, and a multi-ingredient combo).
      Whole rows are copied verbatim from the live extract, so every column
      position the universe parser relies on is inherently correct.

  tests/fixtures/universe/iqvia_slice.xlsx
      The real IQVIA.xlsx province/channel rows for exactly the molecules in the
      cohort (PROGESTERONE / METFORMIN / AMLODIPINE), so `collapse_iqvia` is
      genuinely exercised and the latest-MAT values equal the production output.

Run (needs the pandas-WMI shim on this host — see the project memory):

    PYTHONPATH=<pyshim> python -m tests.scripts.build_universe_fixture

It downloads allfiles.zip live (cached under the system temp dir) and reads the
real IQVIA.xlsx from $IQVIA_REAL_NEW or the known Desktop location.  The printed
MANIFEST lists the derived anchor values that the test file pins.
"""
from __future__ import annotations

import csv
import io
import os
import sys
import zipfile
from pathlib import Path

import httpx

_ALLFILES_URL = (
    "https://www.canada.ca/content/dam/hc-sc/documents/services/"
    "drug-product-database/allfiles.zip"
)
_EXTRACT_FILES = ("drug.txt", "ingred.txt", "form.txt", "route.txt", "status.txt", "comp.txt")

# Column positions mirrored from app/enrichment/universe.py.
_DRUG_COL_CODE, _DRUG_COL_DIN = 0, 3

# Curated cohort of REAL DINs (verified present in the live extract).  Each is
# here for a documented reason the suite exercises.
_COHORT_DINS = [
    "02099233",  # GLUCOPHAGE  — metformin, GLUCOPHAGE column-verify anchor (marketed)
    "00015741",  # TAPAZOLE    — grandfathered DIN, no IQVIA presence (confidence 'none')
    "02516187",  # PROGESTERONE / SANIS    — IQVIA exact anchor (218591 / 21215081)
    "02493578",  # AURO-PROGESTERONE       — IQVIA exact anchor (233159 / 13005865)
    "02314908",  # PRO-METFORMIN / PRO DOC — generic-label aggregation case
    "02380196",  # JAMP METFORMIN          — fuzzy brand vs IQVIA 'JAMP-METFORMIN'
    "02284065",  # PMS-AMLODIPINE / PHARMASCIENCE — house-brand audit case
    "02522519",  # PRZ-AMLODIPINE / PHARMARIS     — cross-company guard
    "75873",     # placeholder; replaced below with a real CADUET combo DIN
]

# Molecules whose real IQVIA rows we keep in the slice.
_COHORT_MOLECULES = {"PROGESTERONE", "METFORMIN", "AMLODIPINE"}

_REPO = Path(__file__).resolve().parents[2]
_OUT = _REPO / "tests" / "fixtures" / "universe"


def _scratch() -> Path:
    base = os.getenv("UNIVERSE_FIXTURE_SCRATCH") or os.path.join(
        os.getenv("TEMP", "/tmp"), "universe_fixture_build"
    )
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _download_extract(scratch: Path) -> Path:
    ex = scratch / "extract_full"
    if (ex / "drug.txt").exists():
        return ex
    ex.mkdir(parents=True, exist_ok=True)
    print("Downloading live allfiles.zip...")
    r = httpx.get(_ALLFILES_URL, follow_redirects=True, timeout=180.0)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        for f in _EXTRACT_FILES:
            (ex / f).write_bytes(zf.read(f))
    print(f"  extracted {len(_EXTRACT_FILES)} files to {ex}")
    return ex


def _rows(path: Path) -> list[list[str]]:
    with open(path, encoding="latin-1", newline="") as fh:
        return list(csv.reader(fh))


def _write_rows(path: Path, rows: list[list[str]]) -> None:
    with open(path, "w", encoding="latin-1", newline="") as fh:
        csv.writer(fh).writerows(rows)


def _find_caduet_combo(drug_rows: list[list[str]], ingred_by_code: dict[str, list[list[str]]]) -> str | None:
    """Pick one real multi-ingredient AMLODIPINE+ATORVASTATIN (CADUET) DIN."""
    for r in drug_rows:
        if len(r) <= _DRUG_COL_DIN:
            continue
        code = r[_DRUG_COL_CODE]
        names = {ir[2].upper() for ir in ingred_by_code.get(code, []) if len(ir) > 2}
        if any("AMLODIPINE" in n for n in names) and any("ATORVASTATIN" in n for n in names):
            return r[_DRUG_COL_DIN]
    return None


def build_extract_fixture(full_ex: Path) -> list[str]:
    drug_rows = _rows(full_ex / "drug.txt")
    ingred_rows = _rows(full_ex / "ingred.txt")
    ingred_by_code: dict[str, list[list[str]]] = {}
    for ir in ingred_rows:
        if ir:
            ingred_by_code.setdefault(ir[0], []).append(ir)

    cohort = list(_COHORT_DINS)
    combo = _find_caduet_combo(drug_rows, ingred_by_code)
    cohort = [d for d in cohort if d != "75873"]
    if combo:
        cohort.append(combo)

    din_to_code = {r[_DRUG_COL_DIN]: r[_DRUG_COL_CODE] for r in drug_rows if len(r) > _DRUG_COL_DIN}
    keep_codes: set[str] = set()
    missing: list[str] = []
    for din in cohort:
        code = din_to_code.get(din)
        if code is None:
            missing.append(din)
        else:
            keep_codes.add(code)
    if missing:
        print(f"  WARNING: cohort DINs not in extract (skipped): {missing}")

    out_ex = _OUT / "extract"
    out_ex.mkdir(parents=True, exist_ok=True)
    file_to_codecol = {
        "drug.txt": 0, "ingred.txt": 0, "form.txt": 0,
        "route.txt": 0, "status.txt": 0, "comp.txt": 0,
    }
    for fname, ccol in file_to_codecol.items():
        rows = _rows(full_ex / fname)
        kept = [r for r in rows if len(r) > ccol and r[ccol] in keep_codes]
        _write_rows(out_ex / fname, kept)
        print(f"  {fname}: kept {len(kept)} rows for {len(keep_codes)} codes")

    return sorted(din_to_code[d] is not None and d for d in cohort if d in din_to_code)


def build_iqvia_slice() -> Path:
    import pandas as pd
    from app.enrichment.iqvia import parse_iqvia

    src = os.getenv("IQVIA_REAL_NEW")
    if not src or not Path(src).is_file():
        for cand in (
            Path.home() / "OneDrive - Viona Pharmaceuticals USA INC" / "Desktop" / "IQVIA.xlsx",
            Path.home() / "Desktop" / "IQVIA.xlsx",
            Path.home() / "Downloads" / "IQVIA.xlsx",
        ):
            if cand.is_file():
                src = str(cand)
                break
    if not src:
        raise SystemExit("Real IQVIA.xlsx not found — set IQVIA_REAL_NEW.")

    print(f"Reading real IQVIA from {src}...")
    raw = parse_iqvia(open(src, "rb").read())
    mol = raw["Combined Molecule"].astype(str).str.upper()
    mask = pd.Series(False, index=raw.index)
    for m in _COHORT_MOLECULES:
        mask |= mol.str.contains(m, na=False)
    sliced = raw[mask].copy()
    out = _OUT / "iqvia_slice.xlsx"
    sliced.to_excel(out, index=False)
    print(f"  wrote {len(sliced)} raw IQVIA rows -> {out}")
    return out


def print_manifest(iqvia_path: Path) -> None:
    """Re-derive the anchor values the test pins, straight from the fixtures."""
    from app.enrichment.iqvia import parse_iqvia, collapse_iqvia
    from app.enrichment.universe import load_dpd_universe_records, build_universe_response
    from app.enrichment.universe import build_universe_sheet1, UniverseBundle

    recs = load_dpd_universe_records(_OUT / "extract")
    bundle = UniverseBundle(recs, [])
    resp = build_universe_response(bundle)
    iq = collapse_iqvia(parse_iqvia(open(iqvia_path, "rb").read()))
    df, recon, low = build_universe_sheet1(resp, iq)

    print("\n===== UNIVERSE FIXTURE MANIFEST =====")
    print(f"universe DPD records: {len(recs)}")
    print(f"low-confidence count: {low}")
    cols = [c for c in ("din", "brand_name", "company", "status",
                        "Dollars MAT 12/2025", "Units MAT 12/2025",
                        "iqvia_match_confidence") if c in df.columns]
    for _, r in df.sort_values("din").iterrows():
        print(" | ".join(f"{c}={r[c]!r}" for c in cols))
    print("===== END MANIFEST =====")


def main() -> int:
    scratch = _scratch()
    full_ex = _download_extract(scratch)
    build_extract_fixture(full_ex)
    iqvia_path = build_iqvia_slice()
    print_manifest(iqvia_path)
    print(f"\nFixtures written under {_OUT}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(_REPO))
    raise SystemExit(main())
