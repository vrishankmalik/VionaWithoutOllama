"""IQVIA Canada metrics: parse, collapse, and match to DINs.

Parsing
-------
Reads the 'data' sheet from an IQVIA Excel export.  Metric columns are
detected by pattern (``Dollars|Units|Ext Units MAT MM/YYYY``) so the file
can roll forward every refresh without code changes.

Collapsing
----------
Each product appears multiple times — once per channel (Drugstore/Hospital),
once per province (up to 9), and sometimes once per container type (SYRINGE
vs VIAL).  All of those rows represent the same DIN, so they are summed.
Group key: (Combined Molecule, Product, Manufacturer, Strength).

Matching (cite-or-blank)
------------------------
For each collapsed IQVIA group the algorithm finds candidate DINs in Sheet 1:

  1. Prefilter by strength: normalize both sides to a frozenset of
     ``NUMBERunit`` tokens; require exact set equality.  ``150MG/ML`` drops
     the ``/ML`` denominator before comparison; ``;``-separated DPD strengths
     and ``/``-separated IQVIA combos are split the same way.

  2. Score by brand + company similarity (0–100 each), weighted 50/50 after
     stripping corporate suffixes and trailing strength / form words.

  3. Accept only when:
        • exactly ONE candidate exceeds CONFIDENT_THRESHOLD (65), OR
        • the top candidate exceeds CONFIDENT_THRESHOLD AND the gap to the
          second candidate exceeds TIE_MARGIN (8).
     Any other outcome → blank for all involved DINs + reconciliation entry.

  4. Each IQVIA group is assigned to at most one DIN.  If two DINs both score
     above MIN_CANDIDATE (55) the group is marked ambiguous and no DIN
     receives data.

Reconciliation
--------------
Returns a DataFrame listing every IQVIA group with:
  - ``status``: matched / ambiguous / low_score / no_din_match
  - the matched DIN (or blank)
  - the top-2 candidate scores (for human review)
"""
from __future__ import annotations

import io
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Optional

import pandas as pd

# ── Matching thresholds ───────────────────────────────────────────────────────

# Combined score (0–100) required to accept a match.
CONFIDENT_THRESHOLD = 65

# Minimum brand+company score for a DIN to be considered a candidate at all.
MIN_CANDIDATE = 55

# If the gap between top and second candidate is less than this, flag as a tie.
# Set at 15 so that cases like PROVERA 5MG (gap ≈ 11) are surfaced for review
# rather than silently assigned to the slightly-higher-scoring DIN.
TIE_MARGIN = 15

# ── Regex helpers ─────────────────────────────────────────────────────────────

# Detects IQVIA metric column names: "Dollars MAT 12/2025", "Units MAT 12/2024", etc.
_METRIC_COL_RE = re.compile(
    r"^(Dollars|Units|Ext\s+Units)\s+MAT\s+\d{2}/\d{4}$",
    re.IGNORECASE,
)

# Concentration denominator to strip: /ML, /G, /L (but NOT /MG which is a combo separator)
_CONC_DENOM_RE = re.compile(r"\s*/\s*(ml|g|l)\s*$", re.IGNORECASE)

# Internal spaces between a number and its unit: "100 MG" → "100MG"
_NUM_SPACE_UNIT_RE = re.compile(r"(\d)\s+([A-Za-z%])")

# Tokens that carry no company identity — stripped before fuzzy comparison.
# Longer alternatives precede shorter ones so the regex engine matches the
# longest word first (e.g. "corporation" before "corp", "incorporated" before "inc").
# After unicode normalisation + punctuation stripping, French abbreviations
# resolve to plain ASCII: "S.E.C." → "sec", "Ltée." → "ltee".
_CORP_STRIP_RE = re.compile(
    r"\b("
    r"incorporated|inc|limited|limitee|ltee|ltd\b|llc|llp|ulc|corporation|corp|co\b|"
    r"sa\b|ag\b|gmbh|plc|sencrl|senc|sec\b|"
    r"pharmaceuticals|pharmaceutical|pharma|therapeutics|"
    r"laboratories|laboratory|labs\b|lab\b|healthcare|health|canada|"
    r"a division of|division|serono|consumer"
    r")[.,]*",
    re.IGNORECASE,
)

# Strip trailing strength/dose/form tokens from a brand name before comparison.
# Requires a leading digit so "PROVERA 5MG TABLETS" → "PROVERA".
_BRAND_TRAILING_RE = re.compile(
    r"\s+\d[\d.\s]*(%|mg|mcg|ug|g\b|ml|iu|miu|units?|cap|capsule|tablet|tab|pak\b|pack).*$",
    re.IGNORECASE,
)
# Strip bare dosage-form words at the end of a brand name when no digit precedes them.
# Handles cases like "APO-ABACAVIR-LAMIVUDINE TABLETS" where the DPD brand name
# includes the form but IQVIA omits it — without this, the trailing word inflates
# the dissimilarity and can cause a false near-tie.
_BRAND_TRAILING_FORM_RE = re.compile(
    r"\s+(?:tablets?|capsules?|caps?|injections?|solution|suspension|cream|ointment|gel|patch|spray|drops?|syrup|elixir|lotion)\s*$",
    re.IGNORECASE,
)

# IQVIA sometimes omits the unit on all but the last component of a combination
# (e.g. "160/12.5MG" meaning "160MG/12.5MG"). These regexes detect that case.
_BARE_NUM_RE = re.compile(r'^\d+(?:\.\d+)?$')      # token with no unit at all
_UNIT_TAIL_RE = re.compile(r'(MG|MCG|UG|ML|IU|MIU|%)$', re.IGNORECASE)  # unit suffix

# ── Normalization helpers ─────────────────────────────────────────────────────

def _norm_strength(s: object) -> frozenset[str]:
    """Return a frozenset of normalised 'NUMBER+UNIT' tokens.

    Examples
    --------
    '100 MG'          -> frozenset({'100MG'})
    '1 MG; 100 MG'    -> frozenset({'1MG', '100MG'})   # DPD semicolon format
    '100MG/1MG'       -> frozenset({'100MG', '1MG'})   # IQVIA combo slash
    '150MG/ML'        -> frozenset({'150MG'})           # concentration → drop /ML
    '0.6GM'           -> frozenset({'600MG'})           # GM → MG unit conversion
    '0.6GM/300MG'     -> frozenset({'600MG', '300MG'}) # IQVIA combo with GM unit
    '160/12.5MG'      -> frozenset({'160MG', '12.5MG'}) # bare-number: unit inferred
    '10MG/G'          -> frozenset({'1%'})              # MG/G → % (10 MG/G = 1%)
    '50M'             -> frozenset({'50MG'})            # IQVIA field-width truncation
    '8 %'             -> frozenset({'8%'})
    ''                -> frozenset()
    """
    if s is None:
        return frozenset()
    raw = str(s).strip()
    if not raw or raw.lower() in ("none", "nan", "not applicable", "n/a"):
        return frozenset()
    # MG/G → % BEFORE splitting (/G would otherwise be stripped as a denominator).
    # 10 MG/G = 1% by definition (milligrams per gram).
    raw = re.sub(
        r'(\d+(?:\.\d+)?)\s*MG/G',
        lambda m: f"{float(m.group(1)) / 10:g}%",
        raw,
        flags=re.IGNORECASE,
    )
    # Drop per-volume denominator (/ML, /G, /L) — signals concentration, not combo.
    raw = _CONC_DENOM_RE.sub("", raw)
    # Split on / or ; to get individual components.
    parts = re.split(r"[/;]", raw)
    result: set[str] = set()
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # Collapse "100 MG" → "100MG"
        norm = _NUM_SPACE_UNIT_RE.sub(r"\1\2", p)
        norm = norm.upper().strip()
        if not norm:
            continue
        # Unit conversions — longest suffixes first to avoid partial matches.
        converted = False
        for suffix, factor in [("KG", 1_000_000.0), ("GM", 1_000.0), ("MCG", 0.001), ("UG", 0.001)]:
            if norm.endswith(suffix):
                val_str = norm[: -len(suffix)]
                try:
                    norm = f"{float(val_str) * factor:g}MG"
                    converted = True
                    break
                except ValueError:
                    pass
        if not converted:
            # "G" alone (e.g. "0.5G" → "500MG") — checked last so "MG"/"MCG" don't match.
            m_g = re.match(r'^(\d+(?:\.\d+)?)G$', norm)
            if m_g:
                try:
                    norm = f"{float(m_g.group(1)) * 1000:g}MG"
                    converted = True
                except ValueError:
                    pass
        if not converted:
            # IQVIA field-width truncation: "50M" → "50MG".
            m_trunc = re.match(r'^(\d+(?:\.\d+)?)M$', norm)
            if m_trunc:
                try:
                    norm = f"{float(m_trunc.group(1)):g}MG"
                except ValueError:
                    pass
        if norm:
            result.add(norm)

    # Bare-number inference: IQVIA omits the unit on all but the last component
    # when all components share the same unit, e.g. "160/12.5MG" means "160MG/12.5MG".
    # Find any bare-number tokens (digits only, no unit) and apply the unit from
    # the last non-bare token in the set.  If every token is a bare number (no
    # unit context exists) we leave them unchanged rather than guessing.
    bare = {t for t in result if _BARE_NUM_RE.match(t)}
    if bare:
        inferred_unit: Optional[str] = None
        for t in (result - bare):
            m = _UNIT_TAIL_RE.search(t)
            if m:
                inferred_unit = m.group(1).upper()
        if inferred_unit:
            result -= bare
            result |= {t + inferred_unit for t in bare}

    return frozenset(result)


def _norm_company(s: object) -> str:
    """Strip corporate/legal suffixes and collapse whitespace.

    Processing order:
      1. Unicode NFKD → ASCII so accented variants match plain equivalents
         ("Limitée" → "Limitee", "ltée" → "ltee").
      2. Strip punctuation that appears in French legal abbreviations
         ("S.E.C." → "sec", "Smith & Nephew" → "Smith Nephew").
      3. Apply _CORP_STRIP_RE to remove generic company-type words.
      4. Collapse whitespace.
    """
    if s is None:
        return ""
    t = str(s)
    # Step 1: flatten accented chars to ASCII equivalents
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode("ascii")
    t = t.lower()
    # Step 2: strip punctuation used in abbreviations and separators
    # "." and "," are components of abbreviations (s.e.c.); "/" and "&" are
    # separators ("Smith & Nephew", "Limitée / S.E.C.").
    t = re.sub(r"[.,/&]", "", t)
    # Step 3: remove generic corporate-type tokens
    t = _CORP_STRIP_RE.sub(" ", t)
    # Step 4: normalise whitespace
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _norm_brand(s: object) -> str:
    """Strip trailing strength / form tokens and lowercase."""
    if s is None:
        return ""
    t = str(s).strip()
    t = _BRAND_TRAILING_RE.sub("", t)       # "PROVERA 5MG TABLETS" → "PROVERA"
    t = _BRAND_TRAILING_FORM_RE.sub("", t)  # "APO-DRUG TABLETS" → "APO-DRUG"
    return t.lower().strip()


def _sim(a: str, b: str) -> float:
    """SequenceMatcher ratio scaled to 0–100."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio() * 100.0


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_iqvia(file_bytes: bytes) -> pd.DataFrame:
    """Read the 'data' sheet from an IQVIA Excel export.

    Returns the raw DataFrame (one row per channel × province × pack).
    Header row is assumed to be row 1 (0-indexed row 0); data from row 2.

    Metric cells containing '-' or blank are converted to 0.

    A hidden ``_excel_row`` column is added recording the 1-based Excel row
    number for each data row (header=row 1, first data row=row 2).  This is
    used by collapse_iqvia() to produce provenance strings for the debug column.
    """
    df = pd.read_excel(
        io.BytesIO(file_bytes),
        sheet_name="data",
        header=0,
        dtype=str,
    )
    df.columns = [str(c).strip() for c in df.columns]

    # Excel row 1 = header; data rows start at Excel row 2 (pandas index 0).
    df["_excel_row"] = range(2, len(df) + 2)

    metric_cols = [c for c in df.columns if _METRIC_COL_RE.match(c)]
    for col in metric_cols:
        df[col] = pd.to_numeric(
            df[col].str.strip().replace({"-": "0", "": "0"}),
            errors="coerce",
        ).fillna(0).astype(int)

    return df


def detect_metric_columns(df: pd.DataFrame) -> list[str]:
    """Return metric column names in the order they appear."""
    return [c for c in df.columns if _METRIC_COL_RE.match(c)]


# ── Collapsing ────────────────────────────────────────────────────────────────

_GROUP_KEY = ["Combined Molecule", "Product", "Manufacturer", "Strength"]


def collapse_iqvia(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse channel × province × pack rows to one row per product group.

    Groups by (Combined Molecule, Product, Manufacturer, Strength).
    All metric columns are summed; non-metric columns (Channel, Province,
    Pack, Product Form, Form 3, Corporation) are dropped.

    When ``_excel_row`` is present (added by parse_iqvia), an additional
    ``_source_excel_rows`` column is emitted containing the sorted, comma-
    separated Excel row numbers for every raw row summed into each group.
    This column is the provenance source for the debug audit column.
    """
    metric_cols = detect_metric_columns(df)
    present_keys = [k for k in _GROUP_KEY if k in df.columns]
    grouped = (
        df[present_keys + metric_cols]
        .groupby(present_keys, as_index=False)[metric_cols]
        .sum()
    )
    if "_excel_row" in df.columns:
        prov = (
            df.groupby(present_keys)["_excel_row"]
            .apply(lambda s: ", ".join(str(r) for r in sorted(s)))
            .reset_index()
            .rename(columns={"_excel_row": "_source_excel_rows"})
        )
        grouped = grouped.merge(prov, on=present_keys, how="left")
    return grouped


# ── Matching ──────────────────────────────────────────────────────────────────

def match_iqvia_to_sheet1(
    sheet1_df: pd.DataFrame,
    iqvia_collapsed: pd.DataFrame,
    debug_iqvia_rows: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Attach IQVIA metric columns to Sheet 1 by matching IQVIA groups to DINs.

    Returns
    -------
    enriched_df
        Sheet 1 DataFrame with metric columns appended.  DINs that could not
        be confidently and unambiguously matched have None in those columns.
    reconciliation_df
        One row per IQVIA group, plus one row per DIN that had no IQVIA match,
        documenting the outcome of every matching decision.
    """
    if sheet1_df.empty or iqvia_collapsed.empty:
        return sheet1_df.copy(), _empty_reconciliation()

    metric_cols = detect_metric_columns(iqvia_collapsed)
    if not metric_cols:
        return sheet1_df.copy(), _empty_reconciliation()

    # ── Build normalised lookup for each Sheet 1 row ──────────────────────────
    # Each row: din, ingredient, brand_name, company, strength
    s1 = sheet1_df.copy()

    _PLACEHOLDER = {"not in dpd", "not applicable", "n/a", "none", ""}

    def _s1_rows() -> list[dict]:
        result = []
        for _, row in s1.iterrows():
            din = str(row.get("din", "") or "").strip()
            if not din:
                continue
            ing = str(row.get("ingredient", "") or "").strip()
            brand = str(row.get("brand_name", "") or "").strip()
            company = str(row.get("company", "") or "").strip()
            strength = str(row.get("strength", "") or "").strip()
            status = str(row.get("status", "") or "").strip().lower()
            # Skip rows that are DPD sentinels ("Not in DPD" etc.)
            if brand.lower() in _PLACEHOLDER or ing.lower() in _PLACEHOLDER:
                continue
            # "Approved" in the Canadian DPD means regulatory approval was granted
            # but the product was NEVER commercially launched (original_market_date is
            # null). These DINs have no sales history and will never appear in IQVIA.
            # Including them as candidates creates false near-ties against the correctly
            # marketed DIN from the same manufacturer (e.g. APO-ABACAVIR-LAMIVUDINE
            # TABLETS DIN 02518287 vs the marketed DIN 02399539), causing the real
            # match to be flagged ambiguous and receive no IQVIA data.
            if status == "approved":
                continue
            result.append({
                "din": din,
                "ingredient": ing,
                "brand_norm": _norm_brand(brand),
                "company_norm": _norm_company(company),
                "strength_set": _norm_strength(strength),
            })
        return result

    s1_rows = _s1_rows()

    # ── For each IQVIA group, score against all Sheet 1 rows ─────────────────
    # din_to_group: DIN → iqvia row index (assigned matches)
    din_to_iqvia: dict[str, int] = {}

    recon_rows: list[dict] = []

    for iq_idx, iq_row in iqvia_collapsed.iterrows():
        molecule = str(iq_row.get("Combined Molecule", "") or "").strip()
        product = str(iq_row.get("Product", "") or "").strip()
        manufacturer = str(iq_row.get("Manufacturer", "") or "").strip()
        strength_raw = str(iq_row.get("Strength", "") or "").strip()

        iq_strength_set = _norm_strength(strength_raw)
        iq_brand_norm = _norm_brand(product)
        iq_company_norm = _norm_company(manufacturer)
        iq_molecule_norm = molecule.upper()

        # Step 1: strength prefilter
        strength_candidates = [
            r for r in s1_rows
            if r["strength_set"] and r["strength_set"] == iq_strength_set
        ]

        # Step 1b: molecule prefilter — require at least one root word of the
        # IQVIA molecule to appear in the DIN's ingredient string, or vice versa.
        # This prevents cross-class false positives (e.g., PMS-AMITRIPTYLINE
        # matching PMS-PROGESTERONE on strength + company alone).
        def _molecule_overlap(iq_mol: str, din_ing: str) -> bool:
            """True if the molecules share at least one significant root word."""
            mol_words = set(re.findall(r"[A-Za-z]{4,}", iq_mol.upper()))
            ing_words = set(re.findall(r"[A-Za-z]{4,}", din_ing.upper()))
            return bool(mol_words & ing_words)

        molecule_candidates = [
            r for r in strength_candidates
            if _molecule_overlap(iq_molecule_norm, r["ingredient"])
            and r["din"] not in din_to_iqvia  # already claimed by an earlier IQVIA group
        ]

        # Step 2: score each candidate
        scored: list[tuple[float, dict]] = []
        for r in molecule_candidates:
            brand_sim = _sim(iq_brand_norm, r["brand_norm"])
            company_sim = _sim(iq_company_norm, r["company_norm"])
            score = brand_sim * 0.5 + company_sim * 0.5
            scored.append((score, r))

        scored.sort(key=lambda t: t[0], reverse=True)

        top_candidates = [(sc, r) for sc, r in scored if sc >= MIN_CANDIDATE]

        # Step 3: decide
        if not top_candidates:
            recon_rows.append(_recon_row(
                iqvia_group=(molecule, product, manufacturer, strength_raw),
                metric_cols=metric_cols,
                iq_row=iq_row,
                status="no_din_match",
                notes=f"No DIN had strength={strength_raw!r} + score≥{MIN_CANDIDATE} (searched {len(molecule_candidates)} candidates; {len(strength_candidates)-len(molecule_candidates)} excluded/claimed)",
                din="",
                top_score=scored[0][0] if scored else 0.0,
                second_score=scored[1][0] if len(scored) > 1 else 0.0,
            ))
            continue

        if len(top_candidates) >= 2:
            top_score, top_r = top_candidates[0]
            sec_score, _ = top_candidates[1]
            gap = top_score - sec_score
            if gap < TIE_MARGIN:
                # Near-tie → ambiguous
                dins_involved = ", ".join(r["din"] for _, r in top_candidates[:4])
                recon_rows.append(_recon_row(
                    iqvia_group=(molecule, product, manufacturer, strength_raw),
                    metric_cols=metric_cols,
                    iq_row=iq_row,
                    status="ambiguous",
                    notes=f"Near-tie: top={top_score:.0f} gap={gap:.0f}<{TIE_MARGIN}; candidates: {dins_involved}",
                    din="",
                    top_score=top_score,
                    second_score=sec_score,
                ))
                continue
            # Gap is large enough but still 2+ candidates above MIN_CANDIDATE.
            # Accept only if the top candidate also clears CONFIDENT_THRESHOLD.
            if top_score < CONFIDENT_THRESHOLD:
                recon_rows.append(_recon_row(
                    iqvia_group=(molecule, product, manufacturer, strength_raw),
                    metric_cols=metric_cols,
                    iq_row=iq_row,
                    status="low_score",
                    notes=f"Top score {top_score:.0f} < {CONFIDENT_THRESHOLD}",
                    din=top_r["din"],
                    top_score=top_score,
                    second_score=sec_score,
                ))
                continue
            # Accept top candidate
            assigned_din = top_r["din"]
        else:
            # Exactly one candidate above MIN_CANDIDATE
            top_score, top_r = top_candidates[0]
            if top_score < CONFIDENT_THRESHOLD:
                recon_rows.append(_recon_row(
                    iqvia_group=(molecule, product, manufacturer, strength_raw),
                    metric_cols=metric_cols,
                    iq_row=iq_row,
                    status="low_score",
                    notes=f"Top score {top_score:.0f} < {CONFIDENT_THRESHOLD}",
                    din=top_r["din"],
                    top_score=top_score,
                    second_score=0.0,
                ))
                continue
            assigned_din = top_r["din"]
            sec_score = 0.0

        # Check: was this DIN already claimed by a different IQVIA group?
        if assigned_din in din_to_iqvia:
            prev_iq_idx = din_to_iqvia[assigned_din]
            recon_rows.append(_recon_row(
                iqvia_group=(molecule, product, manufacturer, strength_raw),
                metric_cols=metric_cols,
                iq_row=iq_row,
                status="ambiguous",
                notes=f"DIN {assigned_din} already claimed by IQVIA group #{prev_iq_idx}; collision",
                din=assigned_din,
                top_score=top_score,
                second_score=sec_score if len(top_candidates) >= 2 else 0.0,
            ))
            continue

        din_to_iqvia[assigned_din] = iq_idx
        recon_rows.append(_recon_row(
            iqvia_group=(molecule, product, manufacturer, strength_raw),
            metric_cols=metric_cols,
            iq_row=iq_row,
            status="matched",
            notes=f"score={top_score:.0f}",
            din=assigned_din,
            top_score=top_score,
            second_score=sec_score if len(top_candidates) >= 2 else 0.0,
        ))

    # ── Append DINs that got no IQVIA group assigned ──────────────────────────
    matched_dins = set(din_to_iqvia.keys())
    for r in s1_rows:
        if r["din"] not in matched_dins:
            recon_rows.append({
                "iqvia_molecule": "",
                "iqvia_product": "",
                "iqvia_manufacturer": "",
                "iqvia_strength": "",
                "din": r["din"],
                "status": "din_no_iqvia_match",
                "top_score": None,
                "second_score": None,
                "notes": "No IQVIA group matched this DIN",
                **{c: None for c in metric_cols},
            })

    # ── Merge metric cols into sheet1 ─────────────────────────────────────────
    # Build a mapping: DIN → metric values + debug info from collapsed IQVIA row
    din_metric_map: dict[str, dict] = {}
    din_debug_rows: dict[str, str] = {}    # DIN → source excel rows string
    din_debug_product: dict[str, str] = {} # DIN → "Product (Manufacturer)" label
    has_provenance = "_source_excel_rows" in iqvia_collapsed.columns
    for din, iq_idx in din_to_iqvia.items():
        iq_row = iqvia_collapsed.loc[iq_idx]
        din_metric_map[din] = {col: int(iq_row[col]) for col in metric_cols}
        if debug_iqvia_rows:
            if has_provenance:
                din_debug_rows[din] = str(iq_row.get("_source_excel_rows") or "")
            product = str(iq_row.get("Product") or "").strip()
            mfr = str(iq_row.get("Manufacturer") or "").strip()
            din_debug_product[din] = f"{product} ({mfr})" if mfr else product

    for col in metric_cols:
        s1[col] = s1["din"].map(
            lambda d, col=col: din_metric_map.get(str(d).strip(), {}).get(col)
        )

    if debug_iqvia_rows:
        s1["IQVIA Source Rows (debug)"] = s1["din"].map(
            lambda d: din_debug_rows.get(str(d).strip())
        )
        s1["IQVIA Matched Product (debug)"] = s1["din"].map(
            lambda d: din_debug_product.get(str(d).strip())
        )

    recon_df = pd.DataFrame(recon_rows) if recon_rows else _empty_reconciliation()
    # Ensure consistent column order
    recon_col_order = [
        "status", "iqvia_molecule", "iqvia_product", "iqvia_manufacturer",
        "iqvia_strength", "din", "top_score", "second_score", "notes",
    ] + metric_cols
    existing = [c for c in recon_col_order if c in recon_df.columns]
    extra = [c for c in recon_df.columns if c not in set(recon_col_order)]
    recon_df = recon_df[existing + extra]

    return s1, recon_df


def _recon_row(
    iqvia_group: tuple[str, str, str, str],
    metric_cols: list[str],
    iq_row: "pd.Series",
    status: str,
    notes: str,
    din: str,
    top_score: float,
    second_score: float,
) -> dict:
    molecule, product, manufacturer, strength = iqvia_group
    row: dict = {
        "iqvia_molecule": molecule,
        "iqvia_product": product,
        "iqvia_manufacturer": manufacturer,
        "iqvia_strength": strength,
        "din": din,
        "status": status,
        "top_score": round(top_score, 1),
        "second_score": round(second_score, 1),
        "notes": notes,
    }
    for c in metric_cols:
        row[c] = int(iq_row[c]) if c in iq_row.index else None
    return row


def _empty_reconciliation() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "status", "iqvia_molecule", "iqvia_product", "iqvia_manufacturer",
        "iqvia_strength", "din", "top_score", "second_score", "notes",
    ])
