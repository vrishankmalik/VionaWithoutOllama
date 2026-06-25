"""Full-universe tab: Option 3 (no-PDF download + disclaimer + cache note) and
Option 4 (filter -> enrich -> download), driven through the real browser."""
from __future__ import annotations

import pytest

from tests.ui._helpers import assert_valid_xlsx
from tests.ui.conftest import open_app

pytestmark = pytest.mark.ui


def _open_universe(page, live_server):
    open_app(page, live_server)
    page.click("#mainTabUniverse")
    page.wait_for_selector("#paneUniverse", state="visible")


def test_universe_disclaimer_and_cache_note_render(page, live_server):
    _open_universe(page, live_server)
    body = page.locator("#paneUniverse").inner_text()
    assert "no PDF" in body
    assert "does not read the slow Product-Monograph PDFs" in body
    # The cache/freshness note resolves against /api/universe/status.
    page.wait_for_selector("#universeCacheNote:has-text('fresh')", timeout=30_000)


def test_universe_full_download(page, live_server):
    _open_universe(page, live_server)
    with page.expect_download(timeout=90_000) as dl:
        page.click("#universeFullBtn")
    assert_valid_xlsx(dl.value)
    page.wait_for_selector("#exportStageLabel:has-text('Done')", timeout=90_000)


def test_universe_filter_enrich_download(page, live_server):
    _open_universe(page, live_server)
    page.click("#filterBoxU summary")
    page.wait_for_function(
        "() => { const s = document.getElementById('dform_u');"
        " return s && s.options.length > 1 && !s.options[0].disabled; }",
        timeout=30_000,
    )
    page.select_option("#dform_u", "TABLET")
    with page.expect_download(timeout=120_000) as dl:
        page.click("#universeFilterBtn")
    assert_valid_xlsx(dl.value)


def test_universe_filter_without_criteria_alerts(page, live_server):
    _open_universe(page, live_server)
    messages = []
    page.on("dialog", lambda d: (messages.append(d.message), d.accept()))
    page.click("#universeFilterBtn")  # nothing ticked / selected
    page.wait_for_timeout(500)
    assert any("at least one filter criterion" in m for m in messages)
