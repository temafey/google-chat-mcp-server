"""Unit tests for the headless mention collector (scripts/collect_mentions.py, T1.3).

Everything external is mocked — ``mentions_core.list_messages_for_me``,
``google_chat.get_credentials`` / ``whoami``, the config and the store are all
backed by a tmp dir. No network, no real ``token.json``, no real
``~/.claude-orchestrator``. These tests pin the Gate-G1 acceptance criteria:

  (a) kill switch (enabled=false / future mute_until) short-circuits — no fetch,
      no store write;
  (b) run-lock — a second concurrent invocation exits without double-processing;
  (c) dedup — two runs over the SAME mocked items leave each item in the store
      exactly once; the second run yields 0 new;
  (d) cursor — last_run advances after a run;
  (e) R7 — a transient error is retried, then succeeds;
  (f) new items are upserted with status == "new".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# scripts/ is not a package on the import path; add it (and the repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT))

import collect_mentions  # noqa: E402
import config  # noqa: E402
import store  # noqa: E402


NOW = "2026-06-04T13:00:00Z"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _item(msg="spaces/AAA/messages/BBB", text="@Artem can you check the deploy?"):
    return {
        "space_name": "spaces/AAA",
        "space_display": "Backend errors",
        "space_type": "SPACE",
        "message_name": msg,
        "thread_name": "spaces/AAA/threads/CCC",
        "sender_id": "users/789",
        "sender_name": "Vasya",
        "created_time": "2026-06-04T12:00:00Z",
        "text": text,
        "trigger": "user_mention",
    }


@pytest.fixture
def paths(tmp_path):
    """Tmp config/store/base/token paths + a pre-written active config."""
    base = tmp_path / "gchat-triage"
    base.mkdir()
    cfg_path = base / "config.json"
    store_path = base / "store.json"
    token_path = tmp_path / "token.json"
    # Active config with identity already set (so whoami() is not called).
    cfg = dict(config.DEFAULT_CONFIG)
    cfg["me_user_id"] = "users/ME"
    cfg["me_display_name"] = "Artem"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    return {
        "base": base,
        "config_path": cfg_path,
        "store_path": store_path,
        "token_path": token_path,
    }


@pytest.fixture
def fake_creds():
    """Patch credential load so the R2 token-lock path has something valid."""
    with patch.object(collect_mentions.gchat, "get_credentials", return_value=object()):
        yield


def _patch_fetch(items_or_sideeffect):
    """Patch mentions_core.list_messages_for_me with an async stub.

    ``items_or_sideeffect`` is either a list (returned every call) or a list of
    per-call results where an entry may be an Exception (raised that call).
    """
    calls = {"n": 0}

    async def _fetch(*args, **kwargs):
        calls["n"] += 1
        if isinstance(items_or_sideeffect, list) and items_or_sideeffect and (
            isinstance(items_or_sideeffect[0], (list, BaseException))
            or (isinstance(items_or_sideeffect[0], type) and issubclass(items_or_sideeffect[0], BaseException))
        ):
            result = items_or_sideeffect[min(calls["n"] - 1, len(items_or_sideeffect) - 1)]
            if isinstance(result, BaseException):
                raise result
            return result
        return items_or_sideeffect

    return patch.object(collect_mentions.mentions_core, "list_messages_for_me", _fetch), calls


# --------------------------------------------------------------------------- #
# (a) Kill switch
# --------------------------------------------------------------------------- #
def test_kill_switch_enabled_false_short_circuits(paths, fake_creds):
    cfg = json.loads(paths["config_path"].read_text())
    cfg["enabled"] = False
    paths["config_path"].write_text(json.dumps(cfg))

    patcher, calls = _patch_fetch([_item()])
    with patcher:
        result = collect_mentions.collect(
            config_path=paths["config_path"],
            store_path=paths["store_path"],
            base_dir=paths["base"],
            token_path=paths["token_path"],
            now=NOW,
        )

    assert result["status"] == "disabled"
    assert calls["n"] == 0  # no fetch
    assert not paths["store_path"].exists()  # no store write


def test_kill_switch_future_mute_short_circuits(paths, fake_creds):
    cfg = json.loads(paths["config_path"].read_text())
    cfg["mute_until"] = "2026-06-04T18:00:00Z"  # after NOW
    paths["config_path"].write_text(json.dumps(cfg))

    patcher, calls = _patch_fetch([_item()])
    with patcher:
        result = collect_mentions.collect(
            config_path=paths["config_path"],
            store_path=paths["store_path"],
            base_dir=paths["base"],
            token_path=paths["token_path"],
            now=NOW,
        )

    assert result["status"] == "disabled"
    assert calls["n"] == 0
    assert not paths["store_path"].exists()


# --------------------------------------------------------------------------- #
# (b) Run-lock — second concurrent invocation does not double-process
# --------------------------------------------------------------------------- #
def test_run_lock_second_invocation_exits(paths, fake_creds, capsys):
    # Hold the run lock as if a first collector were mid-run.
    held = collect_mentions._acquire_lock(
        paths["base"] / collect_mentions.RUN_LOCK_NAME, blocking=False
    )
    assert held is not None

    patcher, calls = _patch_fetch([_item()])
    argv = [
        "--config-path", str(paths["config_path"]),
        "--store-path", str(paths["store_path"]),
        "--base-dir", str(paths["base"]),
        "--token-path", str(paths["token_path"]),
    ]
    try:
        with patcher:
            rc = collect_mentions.main(argv)
    finally:
        collect_mentions._release_lock(held)

    assert rc == 0
    assert calls["n"] == 0  # never fetched → no double-processing
    assert not paths["store_path"].exists()
    assert "another run in progress" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# (c) Dedup + (f) new status + (d) cursor advances
# --------------------------------------------------------------------------- #
def test_dedup_two_runs_no_dups_and_status_new(paths, fake_creds):
    items = [_item(), _item(msg="spaces/AAA/messages/DDD", text="second @Artem ping")]
    patcher, _ = _patch_fetch(items)

    with patcher:
        first = collect_mentions.collect(
            config_path=paths["config_path"],
            store_path=paths["store_path"],
            base_dir=paths["base"],
            token_path=paths["token_path"],
            now=NOW,
        )

    assert first["new"] == 2
    assert first["updated"] == 0

    saved = store.load(paths["store_path"])
    assert len(saved["items"]) == 2
    # (f) every freshly-collected item is status == "new".
    assert all(it["status"] == "new" for it in saved["items"].values())
    # (d) cursor advanced.
    assert saved["last_run"] == NOW
    assert saved["cursor_per_space"]["spaces/AAA"] == NOW

    # Second run over the SAME items, later clock.
    later = "2026-06-04T14:00:00Z"
    with patcher:
        second = collect_mentions.collect(
            config_path=paths["config_path"],
            store_path=paths["store_path"],
            base_dir=paths["base"],
            token_path=paths["token_path"],
            now=later,
        )

    assert second["new"] == 0  # (c) zero dups
    assert second["updated"] == 2
    saved2 = store.load(paths["store_path"])
    assert len(saved2["items"]) == 2  # still exactly two
    assert saved2["last_run"] == later  # (d) advanced again


# --------------------------------------------------------------------------- #
# (e) Transient-error retry then success
# --------------------------------------------------------------------------- #
def test_transient_error_retries_then_succeeds(paths, fake_creds):
    # First call raises a retriable network error, second returns items.
    side_effect = [ConnectionError("temporary blip"), [_item()]]
    patcher, calls = _patch_fetch(side_effect)

    slept = []
    with patcher:
        result = collect_mentions.collect(
            config_path=paths["config_path"],
            store_path=paths["store_path"],
            base_dir=paths["base"],
            token_path=paths["token_path"],
            now=NOW,
            sleep=slept.append,  # don't actually sleep
        )

    assert calls["n"] == 2  # one failure + one success
    assert result["retries"] == 1
    assert slept == [collect_mentions.RETRY_BASE_SECONDS]  # backed off once (1s)
    assert result["new"] == 1


def test_transient_error_exhausts_and_raises(paths, fake_creds):
    err = TimeoutError("still down")
    side_effect = [err, err, err, err]  # all RETRY_ATTEMPTS fail
    patcher, calls = _patch_fetch(side_effect)

    with patcher:
        with pytest.raises(TimeoutError):
            collect_mentions.collect(
                config_path=paths["config_path"],
                store_path=paths["store_path"],
                base_dir=paths["base"],
                token_path=paths["token_path"],
                now=NOW,
                sleep=lambda _d: None,
            )
    assert calls["n"] == collect_mentions.RETRY_ATTEMPTS


def test_non_retriable_error_raises_immediately(paths, fake_creds):
    side_effect = [ValueError("bad input"), [_item()]]
    patcher, calls = _patch_fetch(side_effect)

    with patcher:
        with pytest.raises(ValueError):
            collect_mentions.collect(
                config_path=paths["config_path"],
                store_path=paths["store_path"],
                base_dir=paths["base"],
                token_path=paths["token_path"],
                now=NOW,
                sleep=lambda _d: None,
            )
    assert calls["n"] == 1  # no retry on a non-transient error


# --------------------------------------------------------------------------- #
# Identity bootstrap — whoami() called when me_user_id is null
# --------------------------------------------------------------------------- #
def test_identity_resolved_via_whoami_when_missing(paths, fake_creds):
    cfg = json.loads(paths["config_path"].read_text())
    cfg["me_user_id"] = None
    cfg["me_display_name"] = None
    paths["config_path"].write_text(json.dumps(cfg))

    async def _whoami():
        return {"me_user_id": "users/RESOLVED", "me_display_name": "Artem"}

    patcher, _ = _patch_fetch([_item()])
    with patcher, patch.object(collect_mentions.gchat, "whoami", _whoami):
        collect_mentions.collect(
            config_path=paths["config_path"],
            store_path=paths["store_path"],
            base_dir=paths["base"],
            token_path=paths["token_path"],
            now=NOW,
        )

    saved = store.load(paths["store_path"])
    assert saved["me_user_id"] == "users/RESOLVED"
    persisted = json.loads(paths["config_path"].read_text())
    assert persisted["me_user_id"] == "users/RESOLVED"


# --------------------------------------------------------------------------- #
# Logging — a run log is written, contains no message text
# --------------------------------------------------------------------------- #
def test_run_log_written_without_text(paths, fake_creds):
    patcher, _ = _patch_fetch([_item(text="SECRET-do-not-log this @Artem")])
    with patcher:
        collect_mentions.collect(
            config_path=paths["config_path"],
            store_path=paths["store_path"],
            base_dir=paths["base"],
            token_path=paths["token_path"],
            now=NOW,
        )

    log_files = list((paths["base"] / "logs").glob("collect-*.log"))
    assert log_files, "expected a run log file"
    contents = log_files[0].read_text()
    assert '"event": "run"' in contents
    assert "SECRET-do-not-log" not in contents  # text never logged


# --------------------------------------------------------------------------- #
# Digest format
# --------------------------------------------------------------------------- #
def test_digest_format():
    result = {
        "new": 1,
        "open_total": 3,
        "new_items": [_item(text="@Artem can you check the deploy?")],
    }
    digest = collect_mentions.format_digest(result)
    assert digest.splitlines()[0] == "Chat triage — 1 new / 3 open total"
    assert "Vasya" in digest
    assert "Backend errors" in digest
    assert "user_mention" in digest
