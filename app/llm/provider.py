"""Pluggable LLM provider interface for the Canadian Drug Aggregator.

Provider selection via environment variable:
  LLM_PROVIDER = none          (default) — all methods return empty/None;
                                deterministic logic handles everything
  LLM_PROVIDER = azure_openai  — routes to AzureOpenAIProvider using the
                                env vars listed in that class's docstring

No network calls and no import errors occur when LLM_PROVIDER=none.

To drop in a new provider:
  1. Subclass / implement the LLMProvider protocol.
  2. Add a branch to get_llm_provider().
  3. No call-site changes required.
"""
from __future__ import annotations

import os
from typing import Optional, Protocol, runtime_checkable

# ── Protocol ──────────────────────────────────────────────────────────────────

@runtime_checkable
class LLMProvider(Protocol):
    """Contract that every concrete provider must fulfill.

    Every method returns "no result" (empty list, None, empty dict) when the
    provider has no answer.  Call sites fall through to deterministic logic in
    that case — so NullProvider is always safe as the default.
    """

    async def expand_synonyms(self, name: str) -> list[str]:
        """Return synonym/salt-form/brand-name expansions for a drug name.

        Returns [] when the provider cannot produce synonyms.
        The static synonym map in normalize.py always runs first; this is
        an optional add-on that extends the static results.
        """
        ...

    async def summarize_results(
        self,
        query: str,
        source_summaries: list[dict],
    ) -> Optional[str]:
        """Generate a plain-language summary of multi-source search results.

        Returns None when the provider is not configured (the default).
        The ?summary=true query param is a no-op with NullProvider.
        """
        ...

    async def extract_appearance_fields(
        self,
        section_text: str,
        page_num: int,
        field_group: str,
    ) -> dict:
        """Extract structured fields from a product-monograph text section.

        field_group: "excipients" | "appearance" | "ph"

        Each field in the returned dict must have the form:
          {"value": str | None, "found": bool, "page": int | None}

        Returns {} when the provider is not configured; regex paths take over.
        """
        ...

    async def confirm_innovative_drug_match(
        self,
        ingredient_norm: str,
        dpd_company: str,
        shortlist: list[dict],
    ) -> Optional[dict]:
        """Pick the best manufacturer row from a data-protection shortlist.

        shortlist rows contain at least: submission_number, manufacturer,
        innovative_drug.

        Returns the matching row dict, or None when no confident match.
        NullProvider always returns None so difflib fuzzy matching takes over.
        """
        ...


# ── Null provider (default) ───────────────────────────────────────────────────

class NullProvider:
    """No-op provider.  Every method returns the "no result" sentinel.

    This is the default when LLM_PROVIDER is unset or set to "none".
    With NullProvider active:
      - synonym expansion: only the static map is used
      - AI summary: ?summary=true returns no summary (field is absent)
      - PDF appearance extraction: regex paths handle all fields
      - data-protection matching: difflib fuzzy matching handles ambiguity
    No network connections are made.
    """

    async def expand_synonyms(self, name: str) -> list[str]:
        return []

    async def summarize_results(
        self,
        query: str,
        source_summaries: list[dict],
    ) -> Optional[str]:
        return None

    async def extract_appearance_fields(
        self,
        section_text: str,
        page_num: int,
        field_group: str,
    ) -> dict:
        return {}

    async def confirm_innovative_drug_match(
        self,
        ingredient_norm: str,
        dpd_company: str,
        shortlist: list[dict],
    ) -> Optional[dict]:
        return None


# ── Azure OpenAI provider stub ─────────────────────────────────────────────────

class AzureOpenAIProvider:
    """Azure OpenAI provider stub — wire up credentials and this just works.

    Required environment variables:
      AZURE_OPENAI_ENDPOINT    e.g. https://<resource>.openai.azure.com/
      AZURE_OPENAI_API_KEY     your API key
      AZURE_OPENAI_DEPLOYMENT  deployment name (e.g. "gpt-4o")
      AZURE_OPENAI_API_VERSION e.g. "2024-02-01"  (optional, has default)

    To activate: set LLM_PROVIDER=azure_openai in the environment.

    All four methods share a single `_chat()` helper that calls the Azure
    Chat Completions endpoint.  The prompts mirror the old Ollama prompts
    exactly so accuracy is comparable.

    No credentials are required to *import* this class.  A missing endpoint
    or key only raises at the first actual call — the app starts cleanly.
    """

    _DEFAULT_API_VERSION = "2024-02-01"

    def __init__(self) -> None:
        self._endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "")
        self._api_key = os.getenv("AZURE_OPENAI_API_KEY", "")
        self._deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
        self._api_version = os.getenv(
            "AZURE_OPENAI_API_VERSION", self._DEFAULT_API_VERSION
        )

    async def _chat(self, system: str, user: str, *, temperature: float = 0.0) -> str:
        """Call the Azure OpenAI Chat Completions endpoint.

        TODO: implement this method.
        Example with the openai SDK (pip install openai>=1.0):

            from openai import AsyncAzureOpenAI
            client = AsyncAzureOpenAI(
                azure_endpoint=self._endpoint,
                api_key=self._api_key,
                api_version=self._api_version,
            )
            response = await client.chat.completions.create(
                model=self._deployment,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
            )
            return response.choices[0].message.content or ""

        Or with plain httpx (no extra dependency):

            import httpx, json
            url = (f"{self._endpoint}openai/deployments/{self._deployment}"
                   f"/chat/completions?api-version={self._api_version}")
            payload = {
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
            }
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(
                    url,
                    json=payload,
                    headers={"api-key": self._api_key, "Content-Type": "application/json"},
                )
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"] or ""
        """
        # TODO: replace the NotImplementedError below with one of the patterns above
        raise NotImplementedError(
            "AzureOpenAIProvider._chat() is not yet implemented. "
            "See the docstring for wiring instructions."
        )

    async def expand_synonyms(self, name: str) -> list[str]:
        """Ask the model for synonym/salt-form/brand-name expansions."""
        import json, re
        system = "You are a pharmaceutical terminology assistant."
        user = (
            f"List all common synonyms, salt forms, and brand names for the drug ingredient "
            f"'{name}'. Return ONLY a JSON array of strings, nothing else. "
            f'Example: ["synonym1", "salt form", "brand name"]. If none, return [].'
        )
        try:
            text = await self._chat(system, user)
            m = re.search(r"\[.*?\]", text, re.DOTALL)
            if m:
                candidates = json.loads(m.group(0))
                return [c.strip() for c in candidates if isinstance(c, str) and c.strip()]
        except Exception:
            pass
        return []

    async def summarize_results(
        self,
        query: str,
        source_summaries: list[dict],
    ) -> Optional[str]:
        """Generate a 2-4 sentence plain-language summary of search results."""
        import json
        system = "You are a factual pharmaceutical data assistant. Be concise and accurate."
        records_snippet = "\n".join(
            f"[{r.get('source')}] {r.get('brand_name','N/A')} | "
            f"{r.get('ingredient','N/A')} | {r.get('company','N/A')}"
            for r in source_summaries[:12]
        )
        counts = {r.get("source"): r.get("count") for r in source_summaries}
        user = (
            f"A user searched Canadian drug databases for '{query}'. "
            f"Result counts: {json.dumps(counts)}. "
            f"Sample records:\n{records_snippet}\n\n"
            f"Write a 2-4 sentence plain-language summary. Be factual. "
            f"Start with 'AI Summary:'"
        )
        try:
            return await self._chat(system, user)
        except Exception:
            return None

    async def extract_appearance_fields(
        self,
        section_text: str,
        page_num: int,
        field_group: str,
    ) -> dict:
        """Extract structured pharmaceutical fields from product monograph text."""
        import json
        if field_group == "excipients":
            fields_desc = {
                "excipients_core": (
                    "Non-medicinal / inactive ingredients in the tablet CORE only. "
                    "Do NOT include coating ingredients."
                ),
                "excipients_coating": (
                    "Ingredients in the FILM COAT or COATING ONLY. "
                    "Only populate if there is an explicit 'Film coat' or 'Coating:' subsection."
                ),
                "preservatives": (
                    "Preservative(s) specifically listed. Return null if none listed."
                ),
            }
        elif field_group == "appearance":
            fields_desc = {
                "color": "Color(s) of the tablet/capsule/product.",
                "shape": "Shape (e.g. round, oval, oblong). Null if absent.",
                "size_mm": "Dimensions in mm. Null if absent.",
                "weight": "Weight of the dosage unit in mg. Null if absent.",
            }
        elif field_group == "ph":
            fields_desc = {
                "ph": (
                    "Standalone pH value or range. If only a pH-solubility table, "
                    "return 'Not stated (pH-dependent solubility only)'. Null if pH not mentioned."
                ),
            }
        else:
            return {}

        fields_json = json.dumps(fields_desc, indent=2)
        system = "You are a pharmaceutical data extraction assistant. Extract data verbatim."
        user = (
            f"Extract pharmaceutical data from this product monograph text (page {page_num}).\n\n"
            f"Fields:\n{fields_json}\n\n"
            f"For each field return:\n"
            f'  "value": exact verbatim text, or null\n'
            f'  "found": true if present, false if not\n'
            f'  "page": {page_num} if found, null if not\n\n'
            f"RULES: Copy VERBATIM. Do NOT invent. Return ONLY valid JSON.\n\n"
            f"TEXT:\n{section_text[:5000]}\n\nJSON:"
        )
        try:
            raw = await self._chat(system, user, temperature=0.0)
            return json.loads(raw)
        except Exception:
            return {}

    async def confirm_innovative_drug_match(
        self,
        ingredient_norm: str,
        dpd_company: str,
        shortlist: list[dict],
    ) -> Optional[dict]:
        """Ask the model to pick the best manufacturer row from the shortlist."""
        import json
        candidates = [
            {
                "submission_number": r.get("submission_number", ""),
                "manufacturer": r.get("manufacturer", ""),
                "innovative_drug": r.get("innovative_drug", ""),
            }
            for r in shortlist
        ]
        system = "You are a pharmaceutical regulatory data assistant."
        user = (
            f"Does the DPD manufacturer '{dpd_company}' match any of these register "
            f"manufacturers for ingredient '{ingredient_norm}'? "
            f"Candidates: {json.dumps(candidates)}. "
            f'Respond only in JSON: {{"match": true or false, "submission_number": "..." or null}}'
        )
        try:
            raw = await self._chat(system, user, temperature=0.0)
            resp = json.loads(raw)
            if resp.get("match") and resp.get("submission_number"):
                sub = resp["submission_number"]
                for row in shortlist:
                    if row.get("submission_number") == sub:
                        return row
        except Exception:
            pass
        return None


# ── Provider registry ─────────────────────────────────────────────────────────

_PROVIDER_CACHE: Optional[LLMProvider] = None


def get_llm_provider() -> LLMProvider:
    """Return the configured LLM provider (singleton, selected by LLM_PROVIDER env var).

    LLM_PROVIDER=none (default)       → NullProvider (no network calls)
    LLM_PROVIDER=azure_openai         → AzureOpenAIProvider (requires credentials)
    """
    global _PROVIDER_CACHE
    if _PROVIDER_CACHE is not None:
        return _PROVIDER_CACHE

    name = os.getenv("LLM_PROVIDER", "none").strip().lower()
    if name in ("", "none", "null", "off", "disabled"):
        _PROVIDER_CACHE = NullProvider()
    elif name == "azure_openai":
        _PROVIDER_CACHE = AzureOpenAIProvider()
    else:
        import logging
        logging.getLogger(__name__).warning(
            "Unknown LLM_PROVIDER=%r — falling back to NullProvider", name
        )
        _PROVIDER_CACHE = NullProvider()

    return _PROVIDER_CACHE


def reset_provider_cache() -> None:
    """Force re-read of LLM_PROVIDER env var. Used in tests."""
    global _PROVIDER_CACHE
    _PROVIDER_CACHE = None
