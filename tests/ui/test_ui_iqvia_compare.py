"""IQVIA quarter-over-quarter compare tab: normal compare download, the reversed-
slot reorder notice (new UI surfacing of the server's auto-order decision), the
same-period info note, and a bad-upload error state."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.ui._helpers import assert_valid_xlsx
from tests.ui.conftest import open_app

pytestmark = pytest.mark.ui

_DIFF = Path(__file__).resolve().parents[1] / "fixtures" / "iqvia" / "diff"
_OLD = _DIFF / "old_extract.csv"      # earlier latest-MAT period
_NEW = _DIFF / "new_extract.xlsx"     # later latest-MAT period


def _open_iqvia(page, live_server):
    open_app(page, live_server)
    page.click("#mainTabIqvia")
    page.wait_for_selector("#paneIqvia", state="visible")


def test_compare_button_enables_only_with_both_files(page, live_server):
    _open_iqvia(page, live_server)
    assert page.locator("#cmpGoBtn").is_disabled()
    page.set_input_files("#cmpOldFile", str(_OLD))
    assert page.locator("#cmpGoBtn").is_disabled()
    page.set_input_files("#cmpNewFile", str(_NEW))
    assert not page.locator("#cmpGoBtn").is_disabled()


def test_compare_downloads_change_report(page, live_server):
    _open_iqvia(page, live_server)
    page.set_input_files("#cmpOldFile", str(_OLD))
    page.set_input_files("#cmpNewFile", str(_NEW))
    with page.expect_download(timeout=120_000) as dl:
        page.click("#cmpGoBtn")
    name = assert_valid_xlsx(dl.value)
    assert "iqvia_changes" in name
    page.wait_for_selector("#cmpStatus:has-text('Downloaded')", timeout=10_000)
    # Correct order -> no reorder banner shown.
    assert not page.locator("#cmpNotice").is_visible()


def test_reversed_slots_show_reorder_notice(page, live_server):
    _open_iqvia(page, live_server)
    # Deliberately put the NEWER file in the OLD slot and vice-versa.
    page.set_input_files("#cmpOldFile", str(_NEW))
    page.set_input_files("#cmpNewFile", str(_OLD))
    with page.expect_download(timeout=120_000):
        page.click("#cmpGoBtn")
    page.wait_for_selector("#cmpNotice", state="visible", timeout=10_000)
    text = page.locator("#cmpNotice").inner_text()
    assert "reverse" in text.lower()
    assert "older" in text.lower() and "newer" in text.lower()


def test_same_period_shows_info_note(page, live_server):
    _open_iqvia(page, live_server)
    # Same file in both slots -> identical latest period -> informational tie note.
    page.set_input_files("#cmpOldFile", str(_OLD))
    page.set_input_files("#cmpNewFile", str(_OLD))
    with page.expect_download(timeout=120_000):
        page.click("#cmpGoBtn")
    page.wait_for_selector("#cmpNotice", state="visible", timeout=10_000)
    assert "same" in page.locator("#cmpNotice").inner_text().lower()


def test_bad_upload_shows_error(page, live_server, tmp_path):
    _open_iqvia(page, live_server)
    junk = tmp_path / "junk.csv"
    junk.write_text("not,a,real,iqvia,file\n1,2,3,4,5\n")
    page.set_input_files("#cmpOldFile", str(junk))
    page.set_input_files("#cmpNewFile", str(junk))
    page.click("#cmpGoBtn")
    # A failed compare must surface a visible error, not a blank page.
    page.wait_for_selector("#cmpStatus:has-text('✗')", timeout=30_000)
