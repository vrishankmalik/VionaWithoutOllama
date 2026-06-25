"""Part 2 — Railway deployment-readiness: STATIC checks (offline, no BASE_URL).

These run in the default offline suite and guard the repo-level invariants that
must hold for a clean Railway deploy.  Each maps to a checklist item in
tests/deploy/RAILWAY_CHECKLIST.md.

They intentionally do NOT need a deployed URL or the network, so a regression
(e.g. someone hardcodes a path or drops the $PORT start command) is caught on
every commit, long before launch.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_PROCFILE = REPO_ROOT / "Procfile"


# ── $PORT binding + start command (risk R1) ───────────────────────────────────

def test_procfile_exists_and_binds_platform_port():
    """Railway runs the Procfile ``web:`` process; it MUST bind $PORT and 0.0.0.0.

    run_dev.py hardcodes :8000 (a local-only launcher) and cannot be the deploy
    entrypoint — the platform injects a dynamic $PORT that the app must honour.
    """
    assert _PROCFILE.exists(), "Procfile missing — Railway has no start command"
    text = _PROCFILE.read_text(encoding="utf-8")
    web = next((ln for ln in text.splitlines() if ln.strip().startswith("web:")), "")
    assert web, f"Procfile has no 'web:' process: {text!r}"
    assert "$PORT" in web, f"start command must bind the platform $PORT: {web!r}"
    assert "0.0.0.0" in web, f"start command must bind 0.0.0.0 (not localhost): {web!r}"
    assert "app.main:app" in web, f"start command must serve app.main:app: {web!r}"


def test_start_command_is_single_worker():
    """In-memory job + IQVIA-token state (jobs.py, _IQVIA_STORE) requires ONE worker.

    Multiple uvicorn workers would each hold a separate job store, so a job started
    on worker A could not be polled/downloaded from worker B.  Guard against a
    silent ``--workers N>1`` creeping into the start command.
    """
    web = next(
        (ln for ln in _PROCFILE.read_text(encoding="utf-8").splitlines() if ln.strip().startswith("web:")),
        "",
    )
    m = re.search(r"--workers\s+(\d+)", web)
    workers = int(m.group(1)) if m else 1
    assert workers == 1, f"app holds per-process state — start command must use 1 worker, got {workers}"


# ── Ephemeral / non-shared filesystem (risk R2 — highest priority) ────────────

def test_cache_dir_is_env_configurable():
    """CACHE_DIR (and everything derived from it) must come from the environment so
    Railway can point it at a writable container path."""
    import importlib

    target = os.path.join(tempfile.gettempdir(), "zydus_cfg_probe")
    prev = os.environ.get("CACHE_DIR")
    os.environ["CACHE_DIR"] = target
    try:
        import app.config as config
        importlib.reload(config)
        assert config.CACHE_DIR == target, config.CACHE_DIR
    finally:
        if prev is None:
            os.environ.pop("CACHE_DIR", None)
        else:
            os.environ["CACHE_DIR"] = prev
        import app.config as config
        importlib.reload(config)


def test_all_persisted_paths_live_under_cache_dir():
    """Every long-lived on-disk artifact is rooted at CACHE_DIR — nothing is written
    to a hardcoded absolute path that a wiped/non-shared FS would not provide."""
    from app.config import CACHE_DIR
    from app.enrichment.store import _DB_PATH
    from app.enrichment.universe import UNIVERSE_CACHE_DIR
    import app.main as main

    cache = os.path.abspath(CACHE_DIR)
    for p in (_DB_PATH, str(UNIVERSE_CACHE_DIR), main._IQVIA_PERSIST_PATH):
        assert os.path.abspath(p).startswith(cache), f"{p} is not under CACHE_DIR {cache}"


def test_reset_universe_cache_tolerates_missing_dir():
    """Wiped-FS tolerance: clearing the universe cache when the extract dir does not
    exist must not raise (Railway wipes /tmp on restart)."""
    from app.enrichment import universe as U
    import shutil

    if U.UNIVERSE_CACHE_DIR.exists():
        shutil.rmtree(U.UNIVERSE_CACHE_DIR, ignore_errors=True)
    U._CACHE["bundle"] = None
    # Must return cleanly (0 = nothing cached) rather than throwing on a missing dir.
    assert U.reset_universe_cache() == 0


def test_universe_tmp_workbook_goes_to_tempdir():
    """Result workbooks are written via tempfile, not a repo/abs path — so they
    survive within the request and vanish cleanly on restart."""
    from app.universe_job import _write_tmp

    path = _write_tmp(b"PK\x03\x04dummy", "deploy_probe_")
    try:
        assert os.path.abspath(path).startswith(os.path.abspath(tempfile.gettempdir()))
        assert Path(path).read_bytes().startswith(b"PK")
    finally:
        os.unlink(path)


def test_enrichment_store_recreates_on_fresh_filesystem(tmp_path):
    """A fresh container (empty CACHE_DIR, no enrichment.db) must self-heal: opening
    the store creates the schema and reads return cleanly, no crash."""
    import app.enrichment.store as store

    db = tmp_path / "fresh" / "enrichment.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store.reset_for_testing(str(db))
    try:
        # No DB file yet → the first access must create it and return "no row".
        assert store.get_labeling_for_din("00000000") is None
        assert db.exists(), "store did not create its DB file on first use"
    finally:
        store.reset_for_testing(os.path.join(os.environ.get("CACHE_DIR", tempfile.gettempdir()), "enrichment.db"))


# ── Config / no localhost assumptions (risk R5) ───────────────────────────────

def test_cors_origins_are_env_driven():
    """CORS must be configurable for the public domain, not hardcoded."""
    import importlib

    prev = os.environ.get("CORS_ALLOWED_ORIGINS")
    os.environ["CORS_ALLOWED_ORIGINS"] = "https://app.powerbi.com,https://x.fabric.microsoft.com"
    try:
        import app.config as config
        importlib.reload(config)
        assert config.CORS_ALLOWED_ORIGINS == [
            "https://app.powerbi.com", "https://x.fabric.microsoft.com"
        ], config.CORS_ALLOWED_ORIGINS
    finally:
        if prev is None:
            os.environ.pop("CORS_ALLOWED_ORIGINS", None)
        else:
            os.environ["CORS_ALLOWED_ORIGINS"] = prev
        import app.config as config
        importlib.reload(config)


def test_app_code_has_no_runtime_localhost_dependency():
    """No app/ module hardcodes a localhost/127.0.0.1 SELF-call URL — the app must
    never assume it is reachable at localhost (Railway serves it on a public host).

    Documentation strings (the Power BI examples) are allowed; an actual
    ``httpx``/``requests`` call to localhost is not.
    """
    offenders = []
    for py in (REPO_ROOT / "app").rglob("*.py"):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("'"):
                continue
            if re.search(r"(get|post|stream|request|Client)\([^)]*(localhost|127\.0\.0\.1)", line):
                offenders.append(f"{py.name}:{i}: {stripped}")
    assert not offenders, "runtime localhost self-calls found:\n" + "\n".join(offenders)
