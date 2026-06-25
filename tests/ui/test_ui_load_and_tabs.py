"""Real-browser load, accessibility-sanity, and top-level tab navigation."""
from __future__ import annotations

import pytest

from tests.ui.conftest import open_app

pytestmark = pytest.mark.ui


def test_home_loads_without_console_errors(page, live_server):
    open_app(page, live_server)
    assert "Drug Intelligence Platform" in page.title()
    # The brand logo is a same-origin static asset; it must actually load.
    resp = page.request.get(live_server + "/static/zydus_logo.png")
    assert resp.ok, f"logo fetch failed: {resp.status}"
    assert page.console_errors == [], f"console errors on load: {page.console_errors}"


def test_primary_controls_are_labeled(page, live_server):
    open_app(page, live_server)
    # Ingredient textarea has an associated <label for=query>.
    assert page.locator("label[for='query']").count() == 1
    assert page.locator("label[for='field']").count() == 1
    # Action buttons carry accessible text.
    assert "Download Excel" in page.locator("#exportBtn").inner_text()
    assert "Download Filtered Excel" in page.locator("#filterBtn").inner_text()


def test_tab_switching_shows_one_pane_at_a_time(page, live_server):
    open_app(page, live_server)
    search = page.locator("#paneSearch")
    universe = page.locator("#paneUniverse")
    iqvia = page.locator("#paneIqvia")

    # Default: Search pane visible, others hidden.
    assert search.is_visible()
    assert not universe.is_visible()
    assert not iqvia.is_visible()

    page.click("#mainTabUniverse")
    assert universe.is_visible()
    assert not search.is_visible()
    assert not iqvia.is_visible()

    page.click("#mainTabIqvia")
    assert iqvia.is_visible()
    assert not search.is_visible()
    assert not universe.is_visible()

    # Back to Search — no leakage.
    page.click("#mainTabSearch")
    assert search.is_visible()
    assert not universe.is_visible()
    assert not iqvia.is_visible()


def test_active_tab_styling_follows_selection(page, live_server):
    open_app(page, live_server)
    page.click("#mainTabUniverse")
    assert "active" in (page.locator("#mainTabUniverse").get_attribute("class") or "")
    assert "active" not in (page.locator("#mainTabSearch").get_attribute("class") or "")
