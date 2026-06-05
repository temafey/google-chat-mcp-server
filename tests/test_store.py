"""Unit tests for the atomic JSON ledger (scripts/store.py, T1.2).

Pure stdlib, tmp-dir backed — never touches the real ~/.claude-orchestrator
store. Atomicity is proven by simulating a crash between the temp-write and the
os.replace and asserting the prior file survives intact.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# scripts/ is not a package on the import path; add it explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import store  # noqa: E402


FIXED = datetime(2026, 6, 4, 13, 0, 0, tzinfo=timezone.utc)


def _sample_item(text="@Artem can you check the deploy?"):
    return {
        "space_name": "spaces/AAA",
        "space_display": "Backend errors",
        "space_type": "SPACE",
        "message_name": "spaces/AAA/messages/BBB",
        "thread_name": "spaces/AAA/threads/CCC",
        "sender_id": "users/789",
        "sender_name": "Vasya",
        "created_time": "2026-06-04T12:00:00Z",
        "text": text,
        "trigger": "user_mention",
    }


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "sub" / "store.json"  # 'sub' exercises parent-dir creation


# --- load / skeleton ------------------------------------------------------

def test_load_absent_returns_fresh_skeleton(store_path):
    s = store.load(store_path)
    assert s["version"] == 1
    assert s["items"] == {}
    assert s["cursor_per_space"] == {}
    assert not store_path.exists()  # load must not create the file


# --- upsert ---------------------------------------------------------------

def test_upsert_new_item(store_path):
    s = store.load(store_path)
    iid = store.upsert_item(s, _sample_item(), now=FIXED)

    assert iid in s["items"]
    item = s["items"][iid]
    assert item["id"] == iid
    assert item["status"] == "new"
    assert item["detected_at"] == "2026-06-04T13:00:00Z"
    events = [h["event"] for h in item["history"]]
    assert events == ["detected"]


def test_upsert_same_id_changed_text_refreshes_text_keeps_status(store_path):
    s = store.load(store_path)
    iid = store.upsert_item(s, _sample_item(), now=FIXED)
    # Move it off "new" so we can prove status is preserved.
    store.set_status(s, iid, "triaged", now=FIXED)
    hist_before = len(s["items"][iid]["history"])

    iid2 = store.upsert_item(s, _sample_item(text="edited: check the deploy ASAP"), now=FIXED)

    assert iid2 == iid  # same id for same (space, message)
    item = s["items"][iid]
    assert item["text"] == "edited: check the deploy ASAP"
    assert item["status"] == "triaged"  # preserved, not reset to new
    assert len(item["history"]) > hist_before  # history grew
    assert item["history"][-1]["event"] == "updated"


def test_upsert_unchanged_is_idempotent_no_history_growth(store_path):
    s = store.load(store_path)
    iid = store.upsert_item(s, _sample_item(), now=FIXED)
    hist_before = len(s["items"][iid]["history"])

    store.upsert_item(s, _sample_item(), now=FIXED)  # identical re-collection

    assert len(s["items"][iid]["history"]) == hist_before


def test_item_id_stable():
    a = store.item_id("spaces/AAA", "spaces/AAA/messages/BBB")
    b = store.item_id("spaces/AAA", "spaces/AAA/messages/BBB")
    c = store.item_id("spaces/AAA", "spaces/AAA/messages/CCC")
    assert a == b
    assert a != c


# --- set_status -----------------------------------------------------------

def test_set_status_appends_history_and_extra_fields(store_path):
    s = store.load(store_path)
    iid = store.upsert_item(s, _sample_item(), now=FIXED)

    store.set_status(s, iid, "snoozed", now=FIXED, snooze_until="2026-06-05T09:00:00Z")
    item = s["items"][iid]
    assert item["status"] == "snoozed"
    assert item["snooze_until"] == "2026-06-05T09:00:00Z"
    assert item["history"][-1]["event"] == "status:snoozed"


def test_set_status_rejects_invalid_status(store_path):
    s = store.load(store_path)
    iid = store.upsert_item(s, _sample_item(), now=FIXED)
    with pytest.raises(ValueError):
        store.set_status(s, iid, "bogus", now=FIXED)


def test_set_status_unknown_item_raises(store_path):
    s = store.load(store_path)
    with pytest.raises(KeyError):
        store.set_status(s, "deadbeef", "triaged", now=FIXED)


# --- queries --------------------------------------------------------------

def test_open_items_filters_closed_and_ignored(store_path):
    s = store.load(store_path)

    def add(msg, status):
        it = _sample_item()
        it["message_name"] = f"spaces/AAA/messages/{msg}"
        iid = store.upsert_item(s, it, now=FIXED)
        if status != "new":
            store.set_status(s, iid, status, now=FIXED)
        return iid

    open_new = add("M1", "new")
    open_answered = add("M2", "answered")  # answered-but-not-closed → open
    closed = add("M3", "closed")
    ignored = add("M4", "ignored")

    ids = {it["id"] for it in store.open_items(s)}
    assert open_new in ids
    assert open_answered in ids
    assert closed not in ids
    assert ignored not in ids


def test_overdue_promises(store_path):
    s = store.load(store_path)

    def add(msg, promise_due):
        it = _sample_item()
        it["message_name"] = f"spaces/AAA/messages/{msg}"
        iid = store.upsert_item(s, it, now=FIXED)
        store.set_status(s, iid, "awaiting_me", now=FIXED,
                         my_promise="will analyze", promise_due=promise_due)
        return iid

    overdue = add("P1", "2026-06-04T10:00:00Z")   # before now
    future = add("P2", "2026-06-04T20:00:00Z")     # after now
    nopromise_id = store.upsert_item(
        s, dict(_sample_item(), message_name="spaces/AAA/messages/P3"), now=FIXED)
    store.set_status(s, nopromise_id, "awaiting_me", now=FIXED)

    ids = {it["id"] for it in store.overdue_promises(s, now=FIXED)}
    assert overdue in ids
    assert future not in ids
    assert nopromise_id not in ids


def test_overdue_promises_excludes_closed(store_path):
    s = store.load(store_path)
    iid = store.upsert_item(s, _sample_item(), now=FIXED)
    store.set_status(s, iid, "closed", now=FIXED, promise_due="2026-06-04T10:00:00Z")
    assert store.overdue_promises(s, now=FIXED) == []


# --- mark_stale (R6) ------------------------------------------------------

def test_mark_stale_keeps_item_and_flags(store_path):
    s = store.load(store_path)
    iid = store.upsert_item(s, _sample_item(), now=FIXED)
    store.mark_stale(s, iid, now=FIXED)

    item = s["items"][iid]
    assert item["stale"] is True
    assert iid in s["items"]  # not deleted
    assert item["history"][-1]["event"] == "stale"


def test_upsert_after_stale_clears_flag(store_path):
    s = store.load(store_path)
    iid = store.upsert_item(s, _sample_item(), now=FIXED)
    store.mark_stale(s, iid, now=FIXED)
    store.upsert_item(s, _sample_item(), now=FIXED)  # message re-appeared
    assert s["items"][iid]["stale"] is False


# --- set_fields (notification bookkeeping) --------------------------------

def test_set_fields_sets_field_without_status_or_history_change(store_path):
    s = store.load(store_path)
    iid = store.upsert_item(s, _sample_item(), now=FIXED)
    item = s["items"][iid]
    status_before = item["status"]
    hist_before = len(item["history"])

    store.set_fields(s, iid, now=FIXED, last_notified="2026-06-04T13:30:00Z")

    assert item["last_notified"] == "2026-06-04T13:30:00Z"
    assert item["status"] == status_before          # status unchanged
    assert len(item["history"]) == hist_before        # no history entry added


def test_set_fields_unknown_item_raises(store_path):
    s = store.load(store_path)
    with pytest.raises(KeyError):
        store.set_fields(s, "deadbeef", now=FIXED, last_notified="2026-06-04T13:30:00Z")


# --- persistence roundtrip ------------------------------------------------

def test_save_load_roundtrip_creates_parent_dir(store_path):
    s = store.load(store_path)
    store.upsert_item(s, _sample_item(), now=FIXED)
    store.save(s, store_path)

    assert store_path.exists()
    reloaded = store.load(store_path)
    assert reloaded == s


# --- atomicity ------------------------------------------------------------

def test_save_atomic_crash_leaves_prior_store_intact(store_path, monkeypatch):
    # 1) Persist a known-good store.
    s = store.load(store_path)
    store.upsert_item(s, _sample_item(text="original"), now=FIXED)
    store.save(s, store_path)
    original_bytes = store_path.read_bytes()

    # 2) Simulate a crash between temp-write and rename.
    def boom(src, dst):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(store.os, "replace", boom)

    s2 = store.load(store_path)
    iid = next(iter(s2["items"]))
    s2["items"][iid]["text"] = "corrupting write that never lands"
    with pytest.raises(OSError):
        store.save(s2, store_path)

    # 3) Prior file is byte-for-byte intact, still valid JSON, no leftover temp.
    assert store_path.read_bytes() == original_bytes
    assert json.loads(store_path.read_text())["items"][iid]["text"] == "original"
    leftovers = list(store_path.parent.glob(".store-*.tmp"))
    assert leftovers == []
