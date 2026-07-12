"""Test-suite wide fixtures for google-chat-mcp-server tests.

Critical safety net: every test is redirected away from the production triage
store (``~/.claude-orchestrator/gchat-triage/store.json``) so that pytest runs
can never overwrite live data.

The fixture is autouse + function-scoped, so it applies automatically to the
entire suite with zero per-test wiring.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure scripts/ is importable (mirrors what individual test files do).
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT))

import store as _store_module  # noqa: E402
import google_chat as _google_chat  # noqa: E402


@pytest.fixture(autouse=True, scope="function")
def _reset_google_chat_service_cache():
    """Drop google_chat's shared Resource cache around every test.

    Mandatory, not hygiene. The cache is module-level and process-wide, so a
    Resource built by an earlier test would survive into the next one — and
    since a cache hit never calls ``build``, every
    ``patch.object(google_chat, "build", ...)`` in the suite would silently
    become a no-op, asserting against a stale mock. Reset on BOTH sides so no
    test ordering can matter.
    """
    _google_chat.reset_service_cache()
    yield
    _google_chat.reset_service_cache()


@pytest.fixture(autouse=True, scope="function")
def _isolate_triage_store(monkeypatch, tmp_path):
    """Redirect all triage-store I/O away from the production path.

    Belt-and-suspenders approach:
      (a) Set ``GCHAT_TRIAGE_STORE`` env var → ``_resolve_path(None)`` in
          store.py returns the tmp path instead of DEFAULT_STORE_PATH.
      (b) Monkeypatch ``store.DEFAULT_STORE_PATH`` → any code that reads the
          constant directly (rather than calling ``_resolve_path``) also gets
          the tmp path.

    Both are restored automatically by pytest's monkeypatch teardown.
    """
    tmp_store = tmp_path / "store.json"
    monkeypatch.setenv("GCHAT_TRIAGE_STORE", str(tmp_store))
    monkeypatch.setattr(_store_module, "DEFAULT_STORE_PATH", tmp_store)
