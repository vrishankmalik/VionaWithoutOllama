# Real-browser UI suite (Playwright)

End-to-end tests that drive the **real rendered app in a real headless Chromium** —
clicks, typing, selects, file uploads, downloads, SSE progress, dialogs, and error
states. This is the layer the rest of the test suite doesn't cover: every other
test only checks that controls exist in the served HTML; these actually exercise
them in a browser.

## Install (one-time)

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium     # downloads the browser binary
```

Confirmed working headless in this environment (Windows, Python 3.14).

## Run

```bash
# From the repo root. The pandas-import workaround shim must be on PYTHONPATH
# on this host (see the project memory note); on a normal host just run pytest.
python -m pytest tests/ui/ -o addopts="" -q
```

The default `pytest.ini` `addopts` (`-m "not integration"`) is fine to keep; pass
`-o addopts=""` only if you have a local filter that would exclude these. If
Playwright isn't installed the whole suite **skips** (it never hard-fails a machine
without a browser).

## How it stays fast, deterministic, and parallel-safe

`conftest.py` launches a **dedicated server process** (`_ui_server.py`) on its own
port (8753, or an OS-assigned fallback) with its cache dir pointed at a throwaway
temp dir — so it can run at the same time as other suites without sharing the
default `:8000` server or any on-disk state. Each test gets its own browser
context.

`_ui_server.py` runs the **real** app code, templates, and JS unmodified, but stubs
the network seams so tests don't hit live government sites:

- **DPD / NOC / GSUR / Patent Register** → `respx`, routed to the same recorded
  fixtures the offline unit suite uses. Real parsing/grouping/workbook code runs.
- **Patent.zip / PM-PDF / data-protection** enrichment → no-op stubs (these tests
  assert the workbook + dashboard *render*, not extraction accuracy — that's the
  live-data suite's job).
- **Full DPD universe** → built once from `tests/fixtures/universe/extract` (no
  `allfiles.zip` download).

## Coverage

| File | Journeys |
|---|---|
| `test_ui_load_and_tabs.py` | Page load, no console errors, labeled controls, tab switching with no leakage |
| `test_ui_search_export.py` | Single + multi-ingredient export (download opens), SSE progress to 100%, dashboard + KPI cards + sub-tabs, empty-result state, empty-query / no-criteria alerts, filtered export, reset-cache dialogs |
| `test_ui_filters.py` | Dosage-form multi-select populate + select, six-year date rejects past/malformed dates (real UI validation), accepts future date, IQVIA upload unlocks value criteria, bad upload error |
| `test_ui_universe.py` | Option 3 no-PDF download + disclaimer + cache/freshness note, Option 4 filter→enrich download, no-criteria alert |
| `test_ui_iqvia_compare.py` | Compare download, **reversed-slot reorder notice**, same-period info note, button-enable gating, bad-upload error |
