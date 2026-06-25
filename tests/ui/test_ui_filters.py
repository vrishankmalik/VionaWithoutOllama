"""Go/No-Go filter controls on the Search tab: the dosage-form multi-select, the
six-year no-file-date validation (real UI rejection of a past date), and unlocking
the IQVIA-dependent criteria by uploading a file."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.ui.conftest import open_app

pytestmark = pytest.mark.ui

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_IQVIA_XLSX = _FIXTURES / "iqvia" / "diff" / "new_extract.xlsx"


def _open_search_filter_panel(page, live_server):
    open_app(page, live_server)
    page.click("#filterBox summary")
    # Lazy population fires on the <details> toggle.
    page.wait_for_function(
        "() => { const s = document.getElementById('dform_s');"
        " return s && s.options.length > 1 && !s.options[0].disabled; }",
        timeout=30_000,
    )


def test_dosage_form_dropdown_populates_and_selects(page, live_server):
    _open_search_filter_panel(page, live_server)
    values = page.locator("#dform_s option").evaluate_all(
        "opts => opts.map(o => o.value)"
    )
    assert "TABLET" in values, f"base forms missing TABLET: {values[:10]}"
    page.select_option("#dform_s", "TABLET")
    selected = page.locator("#dform_s").evaluate(
        "el => Array.from(el.selectedOptions).map(o => o.value)"
    )
    assert selected == ["TABLET"]


def test_six_year_date_rejects_past_date(page, live_server):
    _open_search_filter_panel(page, live_server)
    page.select_option("#nofileop_s", "greater")
    page.fill("#nofileval_s", "01/01/2020")  # clearly in the past
    page.fill("#query", "metformin")

    messages = []
    page.on("dialog", lambda d: (messages.append(d.message), d.accept()))
    page.click("#filterBtn")
    page.wait_for_timeout(700)
    assert any("future" in m.lower() for m in messages), messages


def test_six_year_date_rejects_malformed_date(page, live_server):
    _open_search_filter_panel(page, live_server)
    page.select_option("#nofileop_s", "greater")
    page.fill("#nofileval_s", "2099-01-01")  # wrong format (not MM/DD/YYYY)
    page.fill("#query", "metformin")

    messages = []
    page.on("dialog", lambda d: (messages.append(d.message), d.accept()))
    page.click("#filterBtn")
    page.wait_for_timeout(700)
    assert any("MM/DD/YYYY" in m for m in messages), messages


def test_six_year_date_accepts_future_date(page, live_server):
    _open_search_filter_panel(page, live_server)
    page.select_option("#nofileop_s", "greater")
    page.fill("#nofileval_s", "01/01/2099")  # valid future date
    page.fill("#query", "metformin")

    messages = []
    page.on("dialog", lambda d: (messages.append(d.message), d.accept()))
    page.click("#filterBtn")
    # A valid date must NOT raise a validation dialog; the job starts instead.
    page.wait_for_selector("#exportPanel", state="visible", timeout=15_000)
    assert not any(
        ("future" in m.lower() or "MM/DD/YYYY" in m) for m in messages
    ), messages


def test_iqvia_upload_unlocks_value_criteria(page, live_server):
    open_app(page, live_server)
    page.click("#filterBox summary")
    # Value criterion starts disabled (needs IQVIA metrics).
    assert page.locator("#crit_value_on").is_disabled()
    assert page.locator("#iqviaCritNote").is_visible()

    page.set_input_files("#iqviaFile", str(_IQVIA_XLSX))
    page.wait_for_selector("#iqviaStatus:has-text('Loaded')", timeout=60_000)

    assert not page.locator("#crit_value_on").is_disabled()
    assert not page.locator("#iqviaCritNote").is_visible()


def test_bad_iqvia_upload_shows_error(page, live_server, tmp_path):
    open_app(page, live_server)
    page.click("#filterBox summary")
    junk = tmp_path / "broken.xlsx"
    junk.write_bytes(b"this is not a real xlsx file")
    page.set_input_files("#iqviaFile", str(junk))
    # The server rejects it; the status line shows a visible error, not a blank state.
    page.wait_for_selector("#iqviaStatus:has-text('✗')", timeout=60_000)
    assert page.locator("#crit_value_on").is_disabled()  # stays locked on failure
