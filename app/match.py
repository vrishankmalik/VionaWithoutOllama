"""
Optional plain-language summary of cross-source search results.

With NullProvider (default): generate_summary always returns None.
  The ?summary=true query parameter is accepted but produces no summary.

With a configured provider (e.g. LLM_PROVIDER=azure_openai):
  generate_summary returns an "AI Summary: ..." string.
"""
from __future__ import annotations

from typing import Optional

from app.llm.provider import get_llm_provider
from app.models import SourceResult


async def generate_summary(
    query: str,
    sources: list[SourceResult],
) -> Optional[str]:
    """Generate a plain-language summary of results across sources.

    Returns None when no LLM provider is configured (the default).
    The caller should treat None as "no summary available".
    """
    provider = get_llm_provider()

    source_summaries = [
        {
            "source": s.source,
            "count": s.count,
            "status": s.status,
            "error_message": s.error_message,
            "brand_name": s.records[0].brand_name if s.records else None,
            "ingredient": s.records[0].ingredient if s.records else None,
            "company": s.records[0].company if s.records else None,
        }
        for s in sources
    ]

    return await provider.summarize_results(query, source_summaries)
