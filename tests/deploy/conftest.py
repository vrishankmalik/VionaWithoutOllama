"""Fixtures for the Part-2 Railway deployment-readiness suite.

Two kinds of test live here:

  * STATIC readiness checks (test_deploy_static_readiness.py) — no network, no
    deployed URL.  They run in the DEFAULT offline suite and guard the things that
    must be true in the repo for a clean Railway deploy ($PORT start command,
    env-configurable cache dir, wiped-FS tolerance, no localhost assumptions).

  * LIVE checks against a deployed instance — gated on the ``BASE_URL`` env var.
    They are ``@pytest.mark.integration`` AND skip when ``BASE_URL`` is unset, so
    they are dormant until you paste in the Railway URL:

        $env:BASE_URL = "https://<your-app>.up.railway.app"
        <python> -m pytest tests/deploy -m integration -v

The live tests assume a SINGLE Railway instance (the app keeps job state and IQVIA
upload tokens in process memory — see RAILWAY_CHECKLIST.md, risk R2).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def pytest_configure(config):
    config.addinivalue_line("markers", "deploy: Railway deployment-readiness tests")


@pytest.fixture(scope="session")
def base_url() -> str:
    """The deployed Railway base URL, or skip the whole live test."""
    url = (os.environ.get("BASE_URL") or "").strip().rstrip("/")
    if not url:
        pytest.skip("BASE_URL not set — live deployment test skipped (set it after deploy)")
    return url


@pytest.fixture
def client(base_url):
    """A plain httpx client pointed at the deployed app."""
    import httpx
    with httpx.Client(base_url=base_url, timeout=60.0, follow_redirects=True) as c:
        yield c
