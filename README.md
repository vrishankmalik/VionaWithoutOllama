# Canadian Drug Database Aggregator — Fabric Edition

A local/cloud web application that searches four Canadian health-product databases
simultaneously and returns consolidated results viewable in a web UI or downloadable
as XLSX. This copy is **Ollama/llama3-free** and is structured for Microsoft Fabric
or any hosted environment.

## Running (no LLM — default)

```bash
cd /path/to/this/project
pip install -r requirements.txt
python3 -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
# open http://localhost:8000
```

No Ollama, no local model, no GPU needed. All extraction is deterministic by default.

## LLM Provider support

The application has a **pluggable provider slot** (`app/llm/provider.py`). By default,
`NullProvider` is active — every LLM operation is a no-op and the deterministic
(regex / difflib) paths handle everything.

### Env vars

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `none` | `none` \| `azure_openai` |
| `AZURE_OPENAI_ENDPOINT` | — | e.g. `https://<resource>.openai.azure.com/` |
| `AZURE_OPENAI_API_KEY` | — | your API key |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-4o` | deployment name |
| `AZURE_OPENAI_API_VERSION` | `2024-02-01` | API version |

### Enabling Azure OpenAI

1. Implement `AzureOpenAIProvider._chat()` in [app/llm/provider.py](app/llm/provider.py)
   (the docstring shows two complete patterns — openai SDK or plain httpx).
2. Set the four `AZURE_OPENAI_*` env vars.
3. Set `LLM_PROVIDER=azure_openai`.

No other changes are needed — all four call sites (synonym expansion, AI summary,
PDF field extraction, data-protection matching) automatically use the provider.

### What each provider method does

| Method | NullProvider | Configured provider |
|---|---|---|
| `expand_synonyms(name)` | `[]` — static map only | Adds LLM synonyms on top |
| `summarize_results(q, srcs)` | `None` — no summary | Returns "AI Summary: …" |
| `extract_appearance_fields(text, page, group)` | `{}` — regex path active | Returns structured fields |
| `confirm_innovative_drug_match(ing, co, sl)` | `None` — difflib fallback | Picks best manufacturer row |

### Adding a different provider

1. Create a new class with the four async methods (see the `LLMProvider` Protocol).
2. Add a branch in `get_llm_provider()` for a new `LLM_PROVIDER` value.
3. That's it — no call-site changes.

## Accuracy notes

With `LLM_PROVIDER=none` (the default):

- **DPD, NOC, GSUR, Patent Register searches** — fully deterministic, same accuracy
  as the Ollama version (these never used LLM for search).
- **Excipients / preservatives** — regex extraction; accuracy ~90 % on common PM layouts.
- **Appearance fields (colour, shape, size_mm, weight)** — regex extraction;
  colour and shape are well-covered; size_mm and weight are present ~60–70 % of PMs.
- **pH** — regex extraction; coverage ~80 % for liquid/solution PMs.
- **Data-protection matching** — difflib fuzzy at cutoff 0.8; handles most manufacturer
  name variations without LLM. Ambiguous multi-row shortlists return `{}` (blank)
  rather than guessing.

To recover the appearance field accuracy to the Ollama baseline, implement
`AzureOpenAIProvider._chat()` and set `LLM_PROVIDER=azure_openai`.

## Microsoft Fabric notes

- No `localhost` dependencies — set `LLM_PROVIDER=azure_openai` and the four
  `AZURE_OPENAI_*` vars via Fabric environment variables.
- `CACHE_DIR` defaults to `/tmp/canadian_drug_db_cache`; override with a writable
  path in your Fabric workspace.
- `ENABLE_OCR=0` disables pdf2image/pytesseract (not available in all Fabric runtimes).

## Running tests

```bash
# Offline suite (fast, no network, no LLM)
make test

# Integration suite (live government sites)
make test-live
```

All tests pass with `LLM_PROVIDER=none` (unset) and no Ollama installed.
