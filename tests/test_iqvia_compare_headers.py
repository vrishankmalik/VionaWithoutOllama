"""Offline guard for the /api/iqvia/compare response headers that the in-page UI
reads to render its older->newer reorder notice (mirrors the Excel Summary banner).

These run in the normal offline suite (no Playwright), so the header contract the
browser UI depends on stays covered even where the UI suite is skipped.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

_DIFF = Path(__file__).parent / "fixtures" / "iqvia" / "diff"
_OLD = _DIFF / "old_extract.csv"      # earlier latest-MAT period
_NEW = _DIFF / "new_extract.xlsx"     # later latest-MAT period


@pytest.fixture()
def client():
    return TestClient(app)


def _post(client, old_path: Path, new_path: Path):
    with open(old_path, "rb") as fo, open(new_path, "rb") as fn:
        return client.post(
            "/api/iqvia/compare",
            files={
                "old_file": (old_path.name, fo.read(), "application/octet-stream"),
                "new_file": (new_path.name, fn.read(), "application/octet-stream"),
            },
        )


def test_correct_order_reports_not_reordered(client):
    r = _post(client, _OLD, _NEW)
    assert r.status_code == 200
    assert r.headers["X-IQVIA-Reordered"] == "false"
    assert r.headers["X-IQVIA-Old-Period"] != "NA"
    assert r.headers["X-IQVIA-New-Period"] != "NA"
    # The periods must be ordered older -> newer.
    assert r.headers["X-IQVIA-Old-Period"] < r.headers["X-IQVIA-New-Period"]
    assert "X-IQVIA-Reordered" in r.headers["Access-Control-Expose-Headers"]


def test_reversed_slots_set_reordered_flag(client):
    # Newer file in the OLD slot, older file in the NEW slot.
    r = _post(client, _NEW, _OLD)
    assert r.status_code == 200
    assert r.headers["X-IQVIA-Reordered"] == "true"
    # Even reversed, the resolved periods stay older -> newer.
    assert r.headers["X-IQVIA-Old-Period"] < r.headers["X-IQVIA-New-Period"]


def test_same_period_emits_warning_header(client):
    r = _post(client, _OLD, _OLD)
    assert r.status_code == 200
    assert r.headers["X-IQVIA-Reordered"] == "false"
    assert "same" in r.headers.get("X-IQVIA-Warning", "").lower()
