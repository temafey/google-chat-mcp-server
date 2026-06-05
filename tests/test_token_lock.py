"""Unit tests for the in-process advisory token lock (R2 token-refresh race).

The lock now lives INSIDE ``google_chat.get_credentials`` so every caller — the
MCP server and the headless collector alike — is serialized on ``<token>.lock``.
These tests pin the double-checked-locking contract:

  (1) a still-valid token returns on the FAST path, WITHOUT taking the lock;
  (2) an expired token takes the lock, refreshes, saves, and releases;
  (3) under the lock, a token another process already refreshed (reloaded from
      disk) is picked up WITHOUT a second refresh (no double-refresh);
  (4) the lock is ALWAYS released, even when the refresh call raises;
  (5) the lock path matches the collector's ``<token>.lock`` convention and the
      real fcntl lock actually acquires + releases.

Everything is mocked — there is NO live Google OAuth refresh and NO network.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import google_chat as gchat  # noqa: E402


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class FakeCreds:
    """Stand-in for google.oauth2.credentials.Credentials — no network.

    ``refresh`` flips the credential to valid (mirroring a successful OAuth
    refresh) unless ``refresh_error`` is set, in which case it raises.
    """

    def __init__(self, *, valid=True, expired=False, refresh_token="rt"):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.refresh_calls = 0
        self.refresh_error = None

    def refresh(self, request):  # noqa: ARG002 - request unused in the fake
        self.refresh_calls += 1
        if self.refresh_error is not None:
            raise self.refresh_error
        self.valid = True
        self.expired = False

    def to_json(self):
        return "{}"


class LockSpy:
    """Context-manager spy standing in for ``gchat._token_file_lock``.

    Counts entries/exits so a test can assert whether the lock was taken and
    that it was released even on error.
    """

    def __init__(self):
        self.entered = 0
        self.exited = 0

    @contextmanager
    def cm(self, token_path):  # noqa: ARG002 - signature mirrors the real lock
        self.entered += 1
        try:
            yield
        finally:
            self.exited += 1


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def reset_token_info(tmp_path):
    """Isolate the module-level ``token_info`` cache + point at a tmp token."""
    saved = dict(gchat.token_info)
    token_file = tmp_path / "token.json"
    gchat.token_info["credentials"] = None
    gchat.token_info["last_refresh"] = None
    gchat.token_info["token_path"] = str(token_file)
    try:
        yield {"token_file": token_file}
    finally:
        gchat.token_info.clear()
        gchat.token_info.update(saved)


# --------------------------------------------------------------------------- #
# (1) Fast path — valid token skips the lock
# --------------------------------------------------------------------------- #
def test_valid_token_returns_without_taking_lock(reset_token_info):
    creds = FakeCreds(valid=True)
    gchat.token_info["credentials"] = creds

    spy = LockSpy()
    with patch.object(gchat, "_token_file_lock", spy.cm):
        out = gchat.get_credentials()

    assert out is creds
    assert spy.entered == 0  # hot path never contends on the lock


# --------------------------------------------------------------------------- #
# (2) Slow path — expired token locks, refreshes, saves, releases
# --------------------------------------------------------------------------- #
def test_expired_token_locks_refreshes_saves_releases(reset_token_info):
    creds = FakeCreds(valid=False, expired=True)
    gchat.token_info["credentials"] = creds

    spy = LockSpy()
    saved = []
    with patch.object(gchat, "_token_file_lock", spy.cm), patch.object(
        gchat, "save_credentials", side_effect=lambda c, p=None: saved.append(c)
    ):
        out = gchat.get_credentials()

    assert creds.refresh_calls == 1
    assert saved == [creds]
    assert out is creds
    assert spy.entered == 1
    assert spy.exited == 1  # released


# --------------------------------------------------------------------------- #
# (3) Double-checked — another process refreshed token.json → no double refresh
# --------------------------------------------------------------------------- #
def test_recheck_under_lock_picks_up_other_process_refresh(reset_token_info):
    stale = FakeCreds(valid=False, expired=True)
    gchat.token_info["credentials"] = stale
    # Simulate another process having written a fresh token.json while we waited.
    reset_token_info["token_file"].write_text("{}", encoding="utf-8")
    fresh = FakeCreds(valid=True, expired=False)

    spy = LockSpy()
    with patch.object(gchat, "_token_file_lock", spy.cm), patch.object(
        gchat.Credentials, "from_authorized_user_file", return_value=fresh
    ), patch.object(gchat, "save_credentials") as save_mock:
        out = gchat.get_credentials()

    assert out is fresh
    assert stale.refresh_calls == 0  # stale cred never refreshed
    assert fresh.refresh_calls == 0  # reloaded token was already valid
    assert save_mock.call_count == 0  # nothing to write
    assert spy.entered == 1


# --------------------------------------------------------------------------- #
# (4) Lock released even when refresh raises
# --------------------------------------------------------------------------- #
def test_lock_released_when_refresh_raises(reset_token_info):
    creds = FakeCreds(valid=False, expired=True)
    creds.refresh_error = RuntimeError("boom")
    gchat.token_info["credentials"] = creds

    spy = LockSpy()
    with patch.object(gchat, "_token_file_lock", spy.cm):
        out = gchat.get_credentials()

    assert out is None
    assert creds.refresh_calls == 1
    assert spy.entered == 1
    assert spy.exited == 1  # finally released despite the error


# --------------------------------------------------------------------------- #
# (5) Lock path convention + real fcntl acquire/release
# --------------------------------------------------------------------------- #
def test_lock_path_matches_collector_convention():
    # The collector derives Path(str(token_path) + ".lock"); the server must use
    # the identical path so both processes serialize on the SAME file.
    assert str(gchat._token_lock_path("/x/y/token.json")) == "/x/y/token.json.lock"


def test_real_token_file_lock_acquires_and_releases(tmp_path):
    token_path = tmp_path / "token.json"
    lock_path = tmp_path / "token.json.lock"

    with gchat._token_file_lock(str(token_path)):
        assert lock_path.exists()  # lock file created on demand

    # Released: a second acquisition in the same process must not block/deadlock.
    with gchat._token_file_lock(str(token_path)):
        pass
