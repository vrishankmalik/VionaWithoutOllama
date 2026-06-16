#!/usr/bin/env python3
"""LLM-powered stress test for IQVIA matching.

Ollama llama3 plays two roles:
  Generator — produces batches of real Canadian drug queries, cycling through
              8 strategies so the matching sees diverse edge cases:
              INN names, brand names, salt forms, combination products,
              partial/substring queries, therapeutic classes, typo variants,
              and multi-ingredient combos.

  Judge     — for each (DIN, IQVIA group) pair produced by the algorithmic
              matcher, decides CORRECT / INCORRECT / UNCERTAIN and gives a
              one-line reason. This builds an independent accuracy estimate.

Flow per round:
  1. Generator produces N drug names via Ollama.
  2. Each name is searched against the live API (GET /api/search).
  3. Results are collapsed to a minimal Sheet 1 DataFrame.
  4. match_iqvia_to_sheet1() runs against the collapsed IQVIA file.
  5. Every "matched" pair is sent to Ollama for judging.
  6. Stats are updated and a rolling dashboard is printed.
  7. Everything is appended to a JSONL log.

Usage:
    # Requires: server running at --api (default localhost:8000)
    #           Ollama running at --ollama (default localhost:11434)
    python tests/stress_iqvia.py \\
        --iqvia  /Users/vmalik/Downloads/IQVIA_SAMPLE_progesterone.xlsx \\
        --rounds 0                     # 0 = infinite (Ctrl+C to stop) \\
        --api    http://localhost:8000 \\
        --ollama http://localhost:11434 \\
        --model  llama3 \\
        --log    /tmp/iqvia_stress.jsonl \\
        --batch  10                    # queries per generator call

Exit codes:
    0  — completed requested rounds (or Ctrl+C)
    1  — could not connect to server or Ollama at startup

The JSONL log has one JSON object per line:
    {type: "match"|"ambiguous"|"unmatched_iqvia"|"no_iqvia_in_db",
     query, din, brand, company, strength, iqvia_product, iqvia_manufacturer,
     iqvia_strength, algo_score, judge_verdict, judge_reason,
     strategy, round, ts}
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import pandas as pd

# ── Ensure the project root is importable ────────────────────────────────────
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from app.enrichment.iqvia import collapse_iqvia, detect_metric_columns, match_iqvia_to_sheet1, parse_iqvia

# ── Generation strategies ─────────────────────────────────────────────────────

_STRATEGIES: list[tuple[str, str]] = [
    (
        "inn",
        "List {n} real Canadian drug MEDICINAL INGREDIENT names (INN / generic names "
        "only, no brands). These should be diverse — different therapeutic classes. "
        "Output ONLY one ingredient per line, nothing else.",
    ),
    (
        "brand",
        "List {n} real Canadian prescription BRAND names as they appear in Health "
        "Canada's Drug Product Database. Output ONLY one brand name per line.",
    ),
    (
        "salt_form",
        "List {n} drug ingredient names that have common salt/ester variants "
        "(e.g. 'metformin hydrochloride', 'atorvastatin calcium', "
        "'progesterone micronized'). Output one name per line, include the salt form.",
    ),
    (
        "combo",
        "List {n} combination drug ingredient strings as used in Health Canada's DPD, "
        "where two active ingredients are combined (e.g. 'AMLODIPINE; ATORVASTATIN'). "
        "Use semicolon separator. Output one combination per line.",
    ),
    (
        "partial",
        "List {n} PARTIAL drug ingredient name prefixes (3–8 characters) that would "
        "match multiple drugs in a database (e.g. 'MET', 'PROG', 'ATOR'). "
        "Output one prefix per line.",
    ),
    (
        "therapeutic_class",
        "Pick a random Canadian drug therapeutic class (e.g. antihypertensive, "
        "statin, SSRI, progestin, bisphosphonate) and list {n} drug ingredient "
        "names from that class. Output one name per line.",
    ),
    (
        "typo",
        "List {n} realistic MISSPELLINGS of real drug ingredient names "
        "(the kind a user would type — transpositions, missing letters, "
        "extra letters). Output one misspelled name per line.",
    ),
    (
        "brand_to_inn",
        "List {n} Canadian drug BRAND names that are commonly searched instead of "
        "the INN (e.g. LIPITOR for atorvastatin, CRESTOR for rosuvastatin, "
        "PREMARIN for conjugated estrogens). Output one brand name per line.",
    ),
]


# ── Ollama client ─────────────────────────────────────────────────────────────

async def _ollama_generate(
    client: httpx.AsyncClient,
    ollama_url: str,
    model: str,
    prompt: str,
    system: str = "",
    temperature: float = 0.8,
    timeout: float = 60.0,
) -> str:
    """Call Ollama /api/chat and return the assistant text."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }
    resp = await client.post(
        f"{ollama_url}/api/chat",
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"].strip()


async def generate_queries(
    client: httpx.AsyncClient,
    ollama_url: str,
    model: str,
    strategy: str,
    prompt_template: str,
    batch: int,
) -> list[str]:
    """Ask Ollama to generate a batch of drug queries for the given strategy."""
    prompt = prompt_template.format(n=batch)
    system = (
        "You are a Canadian pharmacy database expert. "
        "Respond with exactly the requested list — one item per line, "
        "no numbering, no extra text, no explanations."
    )
    try:
        text = await _ollama_generate(client, ollama_url, model, prompt, system=system, temperature=0.9)
    except Exception as exc:
        print(f"  [generator] Ollama error ({strategy}): {type(exc).__name__}: {exc}")
        return []

    lines = [ln.strip().strip("-•·*").strip() for ln in text.splitlines()]
    queries = [ln for ln in lines if 2 < len(ln) < 120]
    return queries[:batch]


async def judge_match(
    client: httpx.AsyncClient,
    ollama_url: str,
    model: str,
    din: str,
    brand: str,
    company: str,
    strength: str,
    iqvia_product: str,
    iqvia_manufacturer: str,
    iqvia_strength: str,
    algo_score: Optional[float],
) -> tuple[str, str]:
    """Ask Ollama to judge a (DIN, IQVIA group) match.

    Returns (verdict, reason) where verdict is CORRECT / INCORRECT / UNCERTAIN.
    """
    prompt = (
        f"A drug-matching algorithm linked a Health Canada DIN to an IQVIA sales record.\n\n"
        f"DIN record:\n"
        f"  DIN:      {din}\n"
        f"  Brand:    {brand}\n"
        f"  Company:  {company}\n"
        f"  Strength: {strength}\n\n"
        f"IQVIA record:\n"
        f"  Product:      {iqvia_product}\n"
        f"  Manufacturer: {iqvia_manufacturer}\n"
        f"  Strength:     {iqvia_strength}\n\n"
        f"Algorithm confidence score: {algo_score:.1f}/100\n\n"
        "Is this match CORRECT, INCORRECT, or UNCERTAIN? "
        "Reply with exactly one of those words on the first line, "
        "then one sentence of reasoning."
    )
    system = (
        "You are a Canadian pharmacy expert validating drug database matches. "
        "You know Canadian drug brand names, manufacturers, and DIN conventions. "
        "Be concise and accurate."
    )
    try:
        text = await _ollama_generate(
            client, ollama_url, model, prompt, system=system, temperature=0.1
        )
    except Exception as exc:
        return "UNCERTAIN", f"Ollama error: {exc}"

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    first = lines[0].upper() if lines else ""
    verdict = "UNCERTAIN"
    for v in ("CORRECT", "INCORRECT", "UNCERTAIN"):
        if v in first:
            verdict = v
            break
    reason = lines[1] if len(lines) > 1 else lines[0] if lines else ""
    return verdict, reason


# ── Search API client ─────────────────────────────────────────────────────────

async def search_api(
    client: httpx.AsyncClient,
    api_url: str,
    query: str,
    field: str = "ingredient",
    timeout: float = 30.0,
) -> Optional[dict]:
    """Call /api/search and return the parsed JSON, or None on error."""
    try:
        resp = await client.get(
            f"{api_url}/api/search",
            params={"q": query, "field": field},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return None


def _response_to_sheet1(search_response: dict) -> pd.DataFrame:
    """Build a minimal Sheet 1 DataFrame from a /api/search response.

    Only DPD records with a DIN are used (same as the real workbook pipeline).
    """
    rows = []
    for src in search_response.get("sources", []):
        if src.get("source") != "DPD":
            continue
        for rec in src.get("records", []):
            din = (rec.get("din") or "").strip()
            if not din or din.lower() in ("", "not applicable", "n/a"):
                continue
            rows.append({
                "din": din,
                "ingredient": rec.get("ingredient") or "",
                "brand_name": rec.get("brand_name") or "",
                "company": rec.get("company") or "",
                "strength": rec.get("strength") or "",
                "dosage_form": rec.get("dosage_form") or "",
            })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).drop_duplicates(subset=["din"])


# ── Stats tracker ─────────────────────────────────────────────────────────────

class Stats:
    def __init__(self) -> None:
        self.rounds = 0
        self.queries = 0
        self.api_hits = 0       # queries that returned ≥1 DIN
        self.api_misses = 0     # queries that returned 0 DINs (or timed out)
        self.matches = 0        # algorithmic matches
        self.ambiguous = 0      # ambiguous / low-score by algorithm
        self.no_iqvia = 0       # DINs with no IQVIA group matched
        self.unmatched_iqvia = 0  # IQVIA groups with no DIN in DB response
        self.judge_correct = 0
        self.judge_incorrect = 0
        self.judge_uncertain = 0
        self.by_strategy: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.errors = 0
        self.t_start = time.time()

    def elapsed(self) -> str:
        s = int(time.time() - self.t_start)
        return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"

    def judge_total(self) -> int:
        return self.judge_correct + self.judge_incorrect + self.judge_uncertain

    def precision(self) -> Optional[float]:
        t = self.judge_total()
        if t == 0:
            return None
        return self.judge_correct / t

    def dashboard(self) -> str:
        lines = [
            f"\n{'─'*60}",
            f"  IQVIA Stress Test  |  elapsed {self.elapsed()}  |  round {self.rounds}",
            f"{'─'*60}",
            f"  Queries generated : {self.queries:>6}   API hits   : {self.api_hits:>6}",
            f"  Algorithmic matches: {self.matches:>6}   Ambiguous  : {self.ambiguous:>6}",
            f"  No IQVIA (DIN)     : {self.no_iqvia:>6}   No DIN (IQ): {self.unmatched_iqvia:>6}",
            "",
            f"  Judge verdicts (n={self.judge_total():>4}):  "
            f"CORRECT={self.judge_correct}  INCORRECT={self.judge_incorrect}  UNCERTAIN={self.judge_uncertain}",
        ]
        p = self.precision()
        if p is not None:
            lines.append(f"  Estimated precision: {p:.1%}")
        if self.errors:
            lines.append(f"  Errors: {self.errors}")
        lines.append(f"{'─'*60}")

        # Per-strategy breakdown
        if self.by_strategy:
            lines.append("  By strategy:")
            for strat, counts in sorted(self.by_strategy.items()):
                c = counts.get("correct", 0)
                i_ = counts.get("incorrect", 0)
                u = counts.get("uncertain", 0)
                m = counts.get("matches", 0)
                lines.append(f"    {strat:<22} matches={m:>4}  C={c} I={i_} U={u}")
        lines.append("")
        return "\n".join(lines)


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> None:
    stats = Stats()
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = log_path.open("a", encoding="utf-8")

    def _log(obj: dict) -> None:
        obj["ts"] = datetime.now(timezone.utc).isoformat()
        log_fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        log_fh.flush()

    # Load and collapse IQVIA file
    print(f"Loading IQVIA file: {args.iqvia}")
    with open(args.iqvia, "rb") as fh:
        iqvia_raw = parse_iqvia(fh.read())
    iqvia_collapsed = collapse_iqvia(iqvia_raw)
    metric_cols = detect_metric_columns(iqvia_collapsed)
    print(f"  {len(iqvia_collapsed)} collapsed groups, {len(metric_cols)} metric columns")

    # Molecules in the IQVIA file — used as seed for generation
    iq_molecules = sorted(iqvia_collapsed["Combined Molecule"].dropna().unique()) if "Combined Molecule" in iqvia_collapsed.columns else []
    iq_products = sorted(iqvia_collapsed["Product"].dropna().unique()) if "Product" in iqvia_collapsed.columns else []
    print(f"  Molecules: {', '.join(iq_molecules)}")
    print(f"  Products: {', '.join(iq_products)}")

    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
        # ── Connectivity checks ───────────────────────────────────────────────
        print(f"\nChecking API at {args.api}…")
        try:
            r = await client.get(f"{args.api}/api/search", params={"q": "test", "field": "ingredient"}, timeout=10)
            print(f"  API OK (status {r.status_code})")
        except Exception as exc:
            print(f"  ERROR: Cannot reach API at {args.api}: {exc}")
            print("  Start the server: python3 -m uvicorn app.main:app --reload --port 8000")
            sys.exit(1)

        print(f"Checking Ollama at {args.ollama}…")
        try:
            r = await client.get(f"{args.ollama}/api/tags", timeout=5)
            models = [m["name"] for m in r.json().get("models", [])]
            if args.model not in models and not any(args.model in m for m in models):
                print(f"  WARNING: model {args.model!r} not in {models}")
            else:
                print(f"  Ollama OK (model {args.model})")
        except Exception as exc:
            print(f"  ERROR: Cannot reach Ollama at {args.ollama}: {exc}")
            sys.exit(1)

        print(f"\nLog → {log_path}  |  rounds={'∞' if args.rounds == 0 else args.rounds}  |  batch={args.batch}")
        print("Press Ctrl+C to stop and print final report.\n")

        strategy_cycle = 0

        try:
            while args.rounds == 0 or stats.rounds < args.rounds:
                # Pick next strategy (round-robin)
                strat_name, strat_prompt = _STRATEGIES[strategy_cycle % len(_STRATEGIES)]
                strategy_cycle += 1
                stats.rounds += 1

                # Extra context: seed with IQVIA molecules to ensure relevant queries
                seed_hint = ""
                if iq_molecules and stats.rounds % 3 == 0:
                    seed_hint = (
                        f"\n\nFocus on drugs related to these molecules if possible: "
                        f"{', '.join(iq_molecules[:5])}."
                    )
                augmented_prompt = strat_prompt + seed_hint

                print(f"Round {stats.rounds} | strategy={strat_name}", end="", flush=True)

                # ── Generate queries ──────────────────────────────────────────
                queries = await generate_queries(
                    client, args.ollama, args.model,
                    strat_name, augmented_prompt, args.batch,
                )
                if not queries:
                    print(" → no queries generated, skipping")
                    continue

                stats.queries += len(queries)
                print(f" | {len(queries)} queries", end="", flush=True)

                round_matches = 0

                # ── Search + match ────────────────────────────────────────────
                for q in queries:
                    search_resp = await search_api(client, args.api, q)
                    if search_resp is None:
                        stats.api_misses += 1
                        stats.errors += 1
                        continue

                    sheet1 = _response_to_sheet1(search_resp)
                    if sheet1.empty:
                        stats.api_misses += 1
                        _log({"type": "no_db_results", "query": q, "strategy": strat_name, "round": stats.rounds})
                        continue

                    stats.api_hits += 1

                    # Run IQVIA matching
                    try:
                        enriched, recon = match_iqvia_to_sheet1(sheet1, iqvia_collapsed)
                    except Exception as exc:
                        stats.errors += 1
                        _log({"type": "match_error", "query": q, "error": str(exc), "round": stats.rounds})
                        continue

                    # Accumulate reconciliation stats
                    for _, row in recon.iterrows():
                        status = str(row.get("status", ""))
                        if status == "matched":
                            stats.matches += 1
                            round_matches += 1
                            stats.by_strategy[strat_name]["matches"] = stats.by_strategy[strat_name].get("matches", 0) + 1
                            # Judge this match
                            din = str(row.get("din", ""))
                            iq_product = str(row.get("iqvia_product", ""))
                            iq_mfr = str(row.get("iqvia_manufacturer", ""))
                            iq_strength = str(row.get("iqvia_strength", ""))
                            top_score = row.get("top_score")

                            # Find DIN info from sheet1
                            din_row = sheet1[sheet1["din"] == din]
                            brand = str(din_row["brand_name"].iloc[0]) if not din_row.empty else ""
                            company = str(din_row["company"].iloc[0]) if not din_row.empty else ""
                            strength = str(din_row["strength"].iloc[0]) if not din_row.empty else ""

                            verdict, reason = await judge_match(
                                client, args.ollama, args.model,
                                din, brand, company, strength,
                                iq_product, iq_mfr, iq_strength,
                                float(top_score) if top_score is not None else None,
                            )

                            # Update stats
                            if verdict == "CORRECT":
                                stats.judge_correct += 1
                                stats.by_strategy[strat_name]["correct"] = stats.by_strategy[strat_name].get("correct", 0) + 1
                            elif verdict == "INCORRECT":
                                stats.judge_incorrect += 1
                                stats.by_strategy[strat_name]["incorrect"] = stats.by_strategy[strat_name].get("incorrect", 0) + 1
                                # Print incorrect matches immediately so user sees them
                                print(f"\n  [INCORRECT] q={q!r} DIN={din} brand={brand!r} "
                                      f"→ IQVIA {iq_product!r}/{iq_mfr!r}/{iq_strength!r}")
                                print(f"             reason: {reason}")
                            else:
                                stats.judge_uncertain += 1
                                stats.by_strategy[strat_name]["uncertain"] = stats.by_strategy[strat_name].get("uncertain", 0) + 1

                            _log({
                                "type": "match",
                                "query": q,
                                "din": din,
                                "brand": brand,
                                "company": company,
                                "strength": strength,
                                "iqvia_product": iq_product,
                                "iqvia_manufacturer": iq_mfr,
                                "iqvia_strength": iq_strength,
                                "algo_score": float(top_score) if top_score is not None else None,
                                "judge_verdict": verdict,
                                "judge_reason": reason,
                                "strategy": strat_name,
                                "round": stats.rounds,
                            })

                        elif status == "ambiguous":
                            stats.ambiguous += 1
                            _log({
                                "type": "ambiguous",
                                "query": q,
                                "iqvia_product": str(row.get("iqvia_product", "")),
                                "iqvia_strength": str(row.get("iqvia_strength", "")),
                                "notes": str(row.get("notes", "")),
                                "strategy": strat_name,
                                "round": stats.rounds,
                            })

                        elif status == "no_din_match":
                            stats.unmatched_iqvia += 1
                            _log({
                                "type": "unmatched_iqvia",
                                "query": q,
                                "iqvia_product": str(row.get("iqvia_product", "")),
                                "iqvia_manufacturer": str(row.get("iqvia_manufacturer", "")),
                                "iqvia_strength": str(row.get("iqvia_strength", "")),
                                "strategy": strat_name,
                                "round": stats.rounds,
                            })

                        elif status == "din_no_iqvia_match":
                            stats.no_iqvia += 1

                print(f" | matches={round_matches}", end="")

                # Print dashboard every 5 rounds
                if stats.rounds % 5 == 0:
                    print(stats.dashboard())
                else:
                    print()  # newline

        except KeyboardInterrupt:
            print("\n\n[Interrupted by user]")

    print(stats.dashboard())
    print(f"Full log written to: {log_path}")

    # ── Summary report ────────────────────────────────────────────────────────
    _print_summary(log_path, stats)

    log_fh.close()


def _print_summary(log_path: Path, stats: Stats) -> None:
    """Read the JSONL log and print a brief accuracy summary."""
    if not log_path.exists():
        return
    incorrect: list[dict] = []
    correct: list[dict] = []
    try:
        with log_path.open() as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                    if obj.get("type") == "match":
                        if obj.get("judge_verdict") == "INCORRECT":
                            incorrect.append(obj)
                        elif obj.get("judge_verdict") == "CORRECT":
                            correct.append(obj)
                except json.JSONDecodeError:
                    pass
    except OSError:
        return

    print(f"\n{'═'*60}")
    print(f"  FINAL REPORT  |  {stats.rounds} rounds  |  {stats.queries} queries")
    print(f"{'═'*60}")
    p = stats.precision()
    if p is not None:
        print(f"  Judge-estimated precision: {p:.1%}  "
              f"({stats.judge_correct} correct / {stats.judge_total()} judged)")
    if incorrect:
        print(f"\n  Incorrect matches ({len(incorrect)}):")
        for obj in incorrect[-10:]:  # last 10
            print(f"    DIN {obj['din']:<10} brand={obj['brand']!r:30} "
                  f"→ IQVIA {obj['iqvia_product']!r}/{obj['iqvia_manufacturer']!r}/{obj['iqvia_strength']!r}")
            print(f"      score={obj.get('algo_score','?')}  reason: {obj.get('judge_reason','')}")
    print(f"{'═'*60}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="LLM-powered stress test for IQVIA matching",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--iqvia", required=True,
        help="Path to IQVIA Excel file (.xlsx)",
    )
    parser.add_argument(
        "--rounds", type=int, default=0,
        help="Number of generator rounds (0 = infinite). Each round = one Ollama generation call.",
    )
    parser.add_argument(
        "--batch", type=int, default=10,
        help="Number of drug queries per generator call.",
    )
    parser.add_argument(
        "--api", default="http://localhost:8000",
        help="Base URL of the running Zydus API server.",
    )
    parser.add_argument(
        "--ollama", default="http://localhost:11434",
        help="Base URL of the Ollama server.",
    )
    parser.add_argument(
        "--model", default="llama3",
        help="Ollama model name to use.",
    )
    parser.add_argument(
        "--log", default="/tmp/iqvia_stress.jsonl",
        help="Path to append JSONL results log.",
    )
    args = parser.parse_args()

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
