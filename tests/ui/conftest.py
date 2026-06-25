"""Fixtures for the real-browser Playwright UI suite (Prompt B).

Parallel-safety contract (this suite runs at the same time as other pre-launch
suites): a dedicated server process on its own port, its cache dir pointed at a
throwaway temp location, and a fresh browser context per test — no shared on-disk
state and no reliance on the default :8000 server.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# Out-of-repo shim that works around `import pandas` hanging in platform._wmi_query
# on this Python 3.14 host; the child server imports pandas, so it needs this too.
_PYSHIM = r"C:\Users\vmalik\AppData\Local\Temp\pyshim"
_PREFERRED_PORT = 8753  # deliberately not 8000


def pytest_configure(config):
    config.addinivalue_line("markers", "ui: real-browser Playwright UI tests")


def _pick_port() -> int:
    """Bind-test the preferred dedicated port; fall back to an OS-assigned one."""
    for candidate in (_PREFERRED_PORT, 0):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", candidate))
            return s.getsockname()[1]
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("Could not allocate a port for the UI test server")


@pytest.fixture(scope="session")
def live_server():
    """Launch the patched dev server in its own process; yield its base URL."""
    pytest.importorskip("playwright", reason="Playwright not installed")

    port = _pick_port()
    cache_dir = tempfile.mkdtemp(prefix="zydus_ui_cache_")
    log_path = Path(cache_dir) / "server.log"

    env = dict(os.environ)
    env["CACHE_DIR"] = cache_dir
    env["ENABLE_OCR"] = "0"
    env["UI_PORT"] = str(port)
    # OpenBLAS/OMP pre-allocate a large per-thread workspace; on this many-core host
    # the default thread count can exhaust memory at numpy/pandas import and crash
    # the child server. One thread is ample for the small fixture datasets.
    env.setdefault("OPENBLAS_NUM_THREADS", "1")
    env.setdefault("OMP_NUM_THREADS", "1")
    env.setdefault("MKL_NUM_THREADS", "1")
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (_PYSHIM, str(REPO_ROOT), env.get("PYTHONPATH", "")) if p
    )

    log_fh = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).parent / "_ui_server.py")],
        env=env,
        stdin=subprocess.DEVNULL,  # avoid duplicating pytest's (captured) stdin handle
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 90
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            log_fh.flush()
            raise RuntimeError(
                "UI server exited before becoming ready:\n"
                + log_path.read_text(encoding="utf-8", errors="replace")
            )
        try:
            with urllib.request.urlopen(base + "/", timeout=2) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.5)

    if not ready:
        proc.terminate()
        raise RuntimeError(
            "UI server did not become ready within 90s:\n"
            + log_path.read_text(encoding="utf-8", errors="replace")
        )

    try:
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_fh.close()
        shutil.rmtree(cache_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser, live_server):
    """Fresh browser context + page per test. Exposes captured console errors.

    External font requests (Google Fonts) are blocked so the suite is offline-safe
    and a failed font fetch can never masquerade as an app console error.
    """
    context = browser.new_context(accept_downloads=True)
    pg = context.new_page()

    errors: list[str] = []
    pg.on(
        "console",
        lambda m: errors.append(m.text) if m.type == "error" else None,
    )
    pg.on("pageerror", lambda exc: errors.append(str(exc)))
    pg.console_errors = errors  # type: ignore[attr-defined]

    # Neutralise external font CDNs — keeps the run hermetic. The Google Fonts
    # stylesheet is fulfilled with empty CSS (so no gstatic glyph fetch follows and
    # no failed-resource console error appears); everything else passes through.
    def _route(route):
        url = route.request.url
        if "fonts.googleapis.com" in url:
            route.fulfill(status=200, content_type="text/css", body="")
        elif "fonts.gstatic.com" in url:
            route.abort()
        else:
            route.continue_()

    pg.route("**/*", _route)

    try:
        yield pg
    finally:
        context.close()


def open_app(page, live_server):
    """Navigate to the main single-page app and wait for the JS init to run."""
    page.goto(live_server + "/", wait_until="domcontentloaded")
    # The criteria forms are built by the inline init script at the bottom of body.
    # The rows live inside a collapsed <details>, so wait for attachment, not
    # visibility.
    page.wait_for_selector("#crit_competitors_on", state="attached")
    return page
