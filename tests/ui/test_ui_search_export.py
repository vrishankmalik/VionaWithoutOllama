"""Search-tab export journeys: single + multi ingredient, SSE progress, dashboard,
filtered export, and the empty-result state — all driven through the real browser."""
from __future__ import annotations

import pytest

from tests.ui._helpers import assert_valid_xlsx
from tests.ui.conftest import open_app

pytestmark = pytest.mark.ui


def _run_full_export(page, live_server, query: str):
    open_app(page, live_server)
    page.fill("#query", query)
    with page.expect_download(timeout=90_000) as dl:
        page.click("#exportBtn")
    return dl.value


def test_single_ingredient_export_downloads_and_opens(page, live_server):
    download = _run_full_export(page, live_server, "metformin")
    name = assert_valid_xlsx(download)
    assert "metformin" in name.lower()
    # SSE drove the panel to a completed state, exactly once.
    page.wait_for_selector("#exportStageLabel:has-text('Done')", timeout=90_000)
    assert page.locator("#progressFill").evaluate("el => el.style.width") == "100%"


def test_dashboard_renders_after_export(page, live_server):
    _run_full_export(page, live_server, "metformin")
    page.wait_for_selector("#dashboardPanel", state="visible", timeout=90_000)
    # KPI cards populate and the canary confirms the no-rescrape contract.
    page.wait_for_selector(".kpi-card", timeout=90_000)
    assert page.locator(".kpi-card").count() == 4
    assert "No new outbound requests" in page.locator("#canaryBox").inner_text()
    # Dashboard sub-tabs switch.
    page.click("#dashTab2")
    assert page.locator("#dashPane2").is_visible()
    assert not page.locator("#dashPane1").is_visible()


def test_query_count_hint_updates(page, live_server):
    open_app(page, live_server)
    page.fill("#query", "alpelisib\napremilast")
    assert "2 ingredients" in page.locator("#queryHint").inner_text()


def test_multi_ingredient_export(page, live_server):
    open_app(page, live_server)
    page.fill("#query", "metformin\naspirin")
    with page.expect_download(timeout=120_000) as dl:
        page.click("#exportBtn")
    name = assert_valid_xlsx(dl.value)
    assert "2_products" in name


def test_empty_ingredient_shows_alert(page, live_server):
    open_app(page, live_server)
    messages = []
    page.on("dialog", lambda d: (messages.append(d.message), d.accept()))
    page.click("#exportBtn")  # no query typed
    page.wait_for_timeout(500)
    assert any("at least one ingredient" in m for m in messages)


def test_empty_result_shows_clear_no_data_state(page, live_server):
    # "aspirin" has no DPD/NOC fixtures -> every source returns no_results.
    _run_full_export(page, live_server, "aspirin")
    page.wait_for_selector("#dashboardPanel", state="visible", timeout=90_000)
    page.wait_for_selector(".kpi-card", timeout=90_000)
    # Total DINs KPI is the first card; an empty universe reads as 0.
    first_kpi = page.locator(".kpi-value").first.inner_text()
    assert first_kpi == "0", f"expected 0 DINs, got {first_kpi}"


def test_filtered_export_with_numeric_criterion(page, live_server):
    open_app(page, live_server)
    page.fill("#query", "metformin")
    # Open the Go/No-Go panel and set a non-IQVIA criterion.
    page.click("#filterBox summary")
    page.check("#crit_competitors_on")
    page.select_option("#crit_competitors_op", "above")
    page.fill("#crit_competitors_val", "-1")  # keep everything (>-1)
    with page.expect_download(timeout=120_000) as dl:
        page.click("#filterBtn")
    name = assert_valid_xlsx(dl.value)
    assert name.startswith("filtered_")


def test_reset_cache_confirms_and_reports(page, live_server):
    open_app(page, live_server)
    messages = []
    page.on("dialog", lambda d: (messages.append(d.message), d.accept()))
    page.click("text=Reset cache")
    page.wait_for_timeout(1000)
    # First dialog is the confirm; the second is the result alert.
    assert any("Clear all cached data" in m for m in messages)
    assert any("Cache cleared" in m for m in messages)


def test_filtered_export_without_criteria_alerts(page, live_server):
    open_app(page, live_server)
    page.fill("#query", "metformin")
    messages = []
    page.on("dialog", lambda d: (messages.append(d.message), d.accept()))
    page.click("#filterBtn")  # no criteria ticked
    page.wait_for_timeout(500)
    assert any("at least one filter criterion" in m for m in messages)
