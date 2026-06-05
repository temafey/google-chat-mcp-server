"""Tests for scripts/triage_cli.py — the integration CLI.

No live network: ``google_chat.send_message`` is patched with an ``AsyncMock``
so the ``post`` path never touches the wire. Every test runs against a tmp store
(and, where config matters, a tmp config) so the real
``~/.claude-orchestrator`` ledger is never read or written. Write subcommands are
re-loaded from disk to prove the mutation was persisted, not merely applied in
memory.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# scripts/ + repo root on the path so ``triage_cli`` / ``store`` / ``google_chat``
# resolve, mirroring the other suites in tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import google_chat  # noqa: E402
import store  # noqa: E402
import triage_cli  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures / builders.
# --------------------------------------------------------------------------- #
@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "sub" / "store.json"  # 'sub' exercises parent-dir creation


@pytest.fixture
def config_path(tmp_path):
    return tmp_path / "config.json"


def _seed(
    path,
    msg,
    *,
    trigger="broadcast",
    sender_id="users/x",
    sender_name="Sender",
    space_name="spaces/AAA",
    thread_name="spaces/AAA/threads/T",
    created_time="2026-06-04T12:00:00Z",
    text="hello there",
) -> str:
    """Upsert one skeleton item into the on-disk store; return its full id."""
    store_data = store.load(path)
    iid = store.upsert_item(
        store_data,
        {
            "space_name": space_name,
            "space_type": "SPACE",
            "message_name": f"{space_name}/messages/{msg}",
            "thread_name": thread_name,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "created_time": created_time,
            "text": text,
            "trigger": trigger,
        },
        now=created_time,
    )
    store.save(store_data, path)
    return iid


def _run(store_path, *argv, config_path=None) -> int:
    base = ["--store-path", str(store_path)]
    if config_path is not None:
        base += ["--config-path", str(config_path)]
    return triage_cli.main(base + list(argv))


# --------------------------------------------------------------------------- #
# list.
# --------------------------------------------------------------------------- #
def test_list_json_in_triage_queue_order(store_path, config_path, capsys):
    # tier: direct_dm(0) < user_mention(3) < broadcast(4) — same created_time.
    dm = _seed(store_path, "dm", trigger="direct_dm")
    mention = _seed(store_path, "mention", trigger="user_mention")
    broadcast = _seed(store_path, "bcast", trigger="broadcast")

    rc = _run(store_path, "list", "--json", config_path=config_path)
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == [dm, mention, broadcast]
    # The JSON carries the fields the skill parses.
    assert set(rows[0]) >= {
        "id", "priority", "trigger", "sender_name", "sender_id",
        "space_name", "thread_name", "created_time", "status", "text",
    }


# --------------------------------------------------------------------------- #
# show.
# --------------------------------------------------------------------------- #
def test_show_json_returns_item(store_path, capsys):
    iid = _seed(store_path, "m1", text="full text here")
    rc = _run(store_path, "show", iid, "--json")
    assert rc == 0
    item = json.loads(capsys.readouterr().out)
    assert item["id"] == iid
    assert item["text"] == "full text here"
    assert item["thread_name"] == "spaces/AAA/threads/T"


def test_show_unknown_id_exits_2(store_path, capsys):
    _seed(store_path, "m1")
    rc = _run(store_path, "show", "deadbeefdeadbeef", "--json")
    assert rc == 2
    assert "unknown item id" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# post — the only outward write.
# --------------------------------------------------------------------------- #
def test_post_success_records_answered(store_path, tmp_path, capsys, monkeypatch):
    iid = _seed(store_path, "m1")
    reply = tmp_path / "reply.txt"
    reply.write_text("Thanks, looking into it now.", encoding="utf-8")

    mock = AsyncMock(return_value={"name": "spaces/X/messages/Y"})
    monkeypatch.setattr(google_chat, "send_message", mock)

    rc = _run(store_path, "post", iid, "--text-file", str(reply))
    assert rc == 0

    # Posted threaded, with the file text verbatim.
    mock.assert_awaited_once_with(
        "spaces/AAA", "Thanks, looking into it now.", thread_name="spaces/AAA/threads/T"
    )

    out = json.loads(capsys.readouterr().out)
    assert out == {"id": iid, "posted": "spaces/X/messages/Y", "status": "answered"}

    # Persisted to disk.
    reloaded = store.load(store_path)["items"][iid]
    assert reloaded["status"] == "answered"
    assert reloaded["response_posted"] == "spaces/X/messages/Y"
    assert reloaded["response_text"] == "Thanks, looking into it now."


def test_post_api_error_records_nothing(store_path, tmp_path, capsys, monkeypatch):
    iid = _seed(store_path, "m1")
    reply = tmp_path / "reply.txt"
    reply.write_text("never sent", encoding="utf-8")

    mock = AsyncMock(return_value={"error": "thread not found", "status": 404})
    monkeypatch.setattr(google_chat, "send_message", mock)

    rc = _run(store_path, "post", iid, "--text-file", str(reply))
    assert rc == 1
    assert "send failed" in capsys.readouterr().err

    # NOT answered, nothing recorded.
    reloaded = store.load(store_path)["items"][iid]
    assert reloaded["status"] == "new"
    assert reloaded["response_posted"] is None
    assert reloaded["response_text"] is None


def test_post_reads_multiline_text_verbatim(store_path, tmp_path, monkeypatch):
    iid = _seed(store_path, "m1")
    multiline = "Line one.\nLine two.\n\n  - bullet with spaces\n"
    reply = tmp_path / "reply.txt"
    reply.write_text(multiline, encoding="utf-8")

    mock = AsyncMock(return_value={"name": "spaces/X/messages/Z"})
    monkeypatch.setattr(google_chat, "send_message", mock)

    rc = _run(store_path, "post", iid, "--text-file", str(reply))
    assert rc == 0
    mock.assert_awaited_once_with(
        "spaces/AAA", multiline, thread_name="spaces/AAA/threads/T"
    )
    assert store.load(store_path)["items"][iid]["response_text"] == multiline


def test_post_unknown_id_exits_2(store_path, tmp_path, monkeypatch):
    _seed(store_path, "m1")
    reply = tmp_path / "reply.txt"
    reply.write_text("hi", encoding="utf-8")
    mock = AsyncMock(return_value={"name": "spaces/X/messages/Y"})
    monkeypatch.setattr(google_chat, "send_message", mock)

    rc = _run(store_path, "post", "nope", "--text-file", str(reply))
    assert rc == 2
    mock.assert_not_awaited()


# --------------------------------------------------------------------------- #
# triage / snooze / promise / close / ignore — safe state transitions.
# --------------------------------------------------------------------------- #
def test_triage_sets_priority_and_summary(store_path, tmp_path, capsys):
    iid = _seed(store_path, "m1")
    summary = tmp_path / "summary.txt"
    summary.write_text("They asked about the deploy window.", encoding="utf-8")

    rc = _run(
        store_path, "triage", iid,
        "--priority", "high", "--reason", "VIP blocker",
        "--summary-file", str(summary), "--confidence", "high",
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"id": iid, "status": "triaged", "priority": "high"}

    item = store.load(store_path)["items"][iid]
    assert item["status"] == "triaged"
    assert item["priority"] == "high"
    assert item["priority_reason"] == "VIP blocker"
    assert item["context_summary"] == "They asked about the deploy window."
    assert item["context_confidence"] == "high"


def test_snooze_iso_passthrough(store_path, capsys):
    iid = _seed(store_path, "m1")
    rc = _run(store_path, "snooze", iid, "--until", "2026-06-10T09:00:00Z")
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "snoozed"
    assert out["snooze_until"] == "2026-06-10T09:00:00Z"
    assert store.load(store_path)["items"][iid]["status"] == "snoozed"


def test_promise_records_due(store_path, capsys):
    iid = _seed(store_path, "m1")
    rc = _run(
        store_path, "promise", iid,
        "--text", "send the doc", "--due", "2026-06-09T17:00:00Z",
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "awaiting_me"
    assert out["my_promise"] == "send the doc"
    assert out["promise_due"] == "2026-06-09T17:00:00Z"

    item = store.load(store_path)["items"][iid]
    assert item["status"] == "awaiting_me"
    assert item["my_promise"] == "send the doc"
    assert item["promise_due"] == "2026-06-09T17:00:00Z"


def test_close_and_ignore_are_terminal(store_path, capsys):
    a = _seed(store_path, "a")
    b = _seed(store_path, "b")

    assert _run(store_path, "close", a) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"id": a, "status": "closed"}

    assert _run(store_path, "ignore", b) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"id": b, "status": "ignored"}

    items = store.load(store_path)["items"]
    assert items[a]["status"] == "closed"
    assert items[b]["status"] == "ignored"


# --------------------------------------------------------------------------- #
# _parse_when.
# --------------------------------------------------------------------------- #
def test_parse_when_iso_passthrough():
    assert triage_cli._parse_when("2026-06-10T09:00:00Z") == "2026-06-10T09:00:00Z"


def test_parse_when_relative_hours_and_days():
    now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
    assert triage_cli._parse_when("+4h", now=now) == "2026-06-05T16:00:00Z"
    assert triage_cli._parse_when("+2d", now=now) == "2026-06-07T12:00:00Z"
