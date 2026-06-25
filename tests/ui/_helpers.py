"""Shared helpers for the Playwright UI suite."""
from __future__ import annotations

import tempfile
from pathlib import Path


def assert_valid_xlsx(download) -> str:
    """Assert a Playwright download is a real, openable .xlsx; return its filename.

    Confirms the user not only triggered a download but received a workbook that
    actually opens (the prompt's "file downloads and opens").  Playwright stores
    the download at an extensionless temp path, so we save it under its real
    .xlsx name before letting openpyxl (which infers the format from the
    extension) open it.
    """
    import openpyxl

    name = download.suggested_filename
    assert name.endswith(".xlsx"), f"unexpected download name: {name}"
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / name
        download.save_as(str(target))
        assert target.stat().st_size > 0, "downloaded file is empty"
        wb = openpyxl.load_workbook(str(target))
        try:
            assert wb.sheetnames, "workbook has no sheets"
        finally:
            wb.close()
    return name
