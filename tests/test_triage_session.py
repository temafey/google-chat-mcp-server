"""Tests for scripts/triage_session.py — the deterministic triage lifecycle.

Pure stdlib, tmp-dir backed — never touches the real ~/.claude-orchestrator
store. Time is injected via a fixed ``now`` so ordering, snooze elapse, and the
timestamps the module generates are all exercised against a frozen clock. Every
transition test also reloads the ledger from disk to prove the mutation was
persisted, not merely applied in memory.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# scripts/ is not a package on the import path; add it explicitly (mirrors the
# other suites in tests/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import store  # noqa: E402
import triage_session  # noqa: E402


# A frozen clock: 13:00Z. Overdue cutoff is "before this", future is "after".
FIXED = datetime(2026, 6, 4, 13, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Builders.
# --------------------------------------------------------------------------- #
@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "sub" / "store.json"  # 'sub' exercises parent-dir creation


def _seed(
    s,
    msg,
    *,
    trigger="broadcast",
    sender_id="users/x",
    sender_name="Sender",
    created_time="2026-06-04T12:00:00Z",
    text="hello there",
    status="new",
    **status_fields,
):
    """Upsert a skeleton item, optionally moving it off ``new`` with extra fields."""
    item = {
        "space_name": "spaces/AAA",
        "space_type": "SPACE",
        "message_name": f"spaces/AAA/messages/{msg}",
        "thread_name": "spaces/AAA/threads/T",
        "sender_id": sender_id,
        "sender_name": sender_name,
        "created_time": created_time,
        "text": text,
        "trigger": trigger,
    }
    iid = store.upsert_item(s, item, now=FIXED)
    if status != "new":
        store.set_status(s, iid, status, now=FIXED, **status_fields)
    elif status_fields:
        store.set_fields(s, iid, **status_fields)
    return iid


def _fresh(store_path, **over):
    """A store persisted to ``store_path`` holding a single seeded item ('M1')."""
    s = store.load(store_path)
    iid = _seed(s, "M1", **over)
    store.save(s, store_path)
    return s, iid


# --------------------------------------------------------------------------- #
# triage_queue — ordering.
# --------------------------------------------------------------------------- #
def test_triage_queue_full_ordering(store_path):
    s = store.load(store_path)
    # group 1 items (no overdue promise), all with identical created_time so the
    # tier alone decides their relative order.
    dm = _seed(s, "DM", trigger="direct_dm")
    vip = _seed(s, "VIP", trigger="broadcast", sender_id="users/boss")
    urg = _seed(s, "URG", trigger="broadcast", text="this is URGENT, please look")
    mention = _seed(s, "MEN", trigger="user_mention")
    bcast = _seed(s, "BC", trigger="broadcast", text="just a normal note")
    other = _seed(s, "OTH", trigger="reaction", text="👍")
    # group 0: an overdue promise jumps the whole queue regardless of its tier.
    overdue = _seed(
        s, "OVD", trigger="broadcast", status="awaiting_me",
        my_promise="send report", promise_due="2026-06-04T10:00:00Z",  # < FIXED
    )

    queue = triage_session.triage_queue(
        s, now=FIXED, vip_senders=["users/boss"], urgency_keywords=["urgent"],
    )
    assert [it["id"] for it in queue] == [overdue, dm, vip, urg, mention, bcast, other]


def test_triage_queue_created_time_tiebreak_oldest_first(store_path):
    s = store.load(store_path)
    newer = _seed(s, "NEW", trigger="direct_dm", created_time="2026-06-04T12:30:00Z")
    older = _seed(s, "OLD", trigger="direct_dm", created_time="2026-06-04T11:00:00Z")

    queue = triage_session.triage_queue(s, now=FIXED)
    assert [it["id"] for it in queue] == [older, newer]  # same tier → oldest first


def test_triage_queue_returns_actual_item_dicts_not_copies(store_path):
    s = store.load(store_path)
    iid = _seed(s, "DM", trigger="direct_dm")
    queue = triage_session.triage_queue(s, now=FIXED)
    assert queue[0] is s["items"][iid]


# --------------------------------------------------------------------------- #
# triage_queue — snooze exclusion + terminal exclusion.
# --------------------------------------------------------------------------- #
def test_triage_queue_snooze_and_terminal_filtering(store_path):
    s = store.load(store_path)
    future = _seed(s, "FUT", status="snoozed", snooze_until="2026-06-04T20:00:00Z")  # > FIXED
    elapsed = _seed(s, "ELA", status="snoozed", snooze_until="2026-06-04T09:00:00Z")  # < FIXED
    none_snz = _seed(s, "NON", status="snoozed")  # snooze_until stays None
    closed = _seed(s, "CLO", status="closed")
    ignored = _seed(s, "IGN", status="ignored")

    ids = {it["id"] for it in triage_session.triage_queue(s, now=FIXED)}
    assert future not in ids          # still sleeping
    assert elapsed in ids             # woke up
    assert none_snz in ids            # no snooze_until set → included
    assert closed not in ids          # terminal
    assert ignored not in ids         # terminal


# --------------------------------------------------------------------------- #
# Transitions — each asserts status, verbatim fields, history entry, persistence.
# --------------------------------------------------------------------------- #
def test_set_triage_sets_priority_context_and_persists(store_path):
    s, iid = _fresh(store_path)

    item = triage_session.set_triage(
        s, iid, priority="high", priority_reason="prod is down",
        context_summary="deploy is failing in prod", context_confidence=0.9,
        now=FIXED, path=store_path,
    )

    assert item["status"] == "triaged"
    assert item["priority"] == "high"
    assert item["priority_reason"] == "prod is down"
    assert item["context_summary"] == "deploy is failing in prod"
    assert item["context_confidence"] == 0.9
    assert item["history"][-1]["event"] == "status:triaged"

    reloaded = store.load(store_path)["items"][iid]
    assert reloaded["status"] == "triaged"
    assert reloaded["priority"] == "high"
    assert reloaded["context_summary"] == "deploy is failing in prod"
    assert reloaded["history"][-1]["event"] == "status:triaged"


def test_set_triage_omits_none_optional_fields(store_path):
    # Item already carries a context_summary; a None on the next set_triage must
    # NOT overwrite it (we omit it from the set_status call entirely).
    s, iid = _fresh(store_path, status="triaged", context_summary="keep me")

    item = triage_session.set_triage(
        s, iid, priority="low", context_summary=None, now=FIXED, path=store_path,
    )

    assert item["priority"] == "low"
    assert item["context_summary"] == "keep me"  # untouched
    assert store.load(store_path)["items"][iid]["context_summary"] == "keep me"


def test_set_triage_invalid_priority_raises_valueerror(store_path):
    s, iid = _fresh(store_path)
    with pytest.raises(ValueError):
        triage_session.set_triage(s, iid, priority="critical", now=FIXED, path=store_path)


def test_snooze_sets_status_and_until_and_persists(store_path):
    s, iid = _fresh(store_path)

    item = triage_session.snooze(
        s, iid, until="2026-06-05T09:00:00Z", now=FIXED, path=store_path,
    )

    assert item["status"] == "snoozed"
    assert item["snooze_until"] == "2026-06-05T09:00:00Z"
    assert item["history"][-1]["event"] == "status:snoozed"

    reloaded = store.load(store_path)["items"][iid]
    assert reloaded["status"] == "snoozed"
    assert reloaded["snooze_until"] == "2026-06-05T09:00:00Z"


def test_snooze_coerces_datetime_until_to_iso(store_path):
    s, iid = _fresh(store_path)
    until = datetime(2026, 6, 5, 9, 0, 0, tzinfo=timezone.utc)
    item = triage_session.snooze(s, iid, until=until, now=FIXED, path=store_path)
    assert item["snooze_until"] == "2026-06-05T09:00:00Z"


def test_record_promise_sets_awaiting_me_and_persists(store_path):
    s, iid = _fresh(store_path)

    item = triage_session.record_promise(
        s, iid, my_promise="I'll analyze and reply", promise_due="2026-06-05T17:00:00Z",
        now=FIXED, path=store_path,
    )

    assert item["status"] == "awaiting_me"
    assert item["my_promise"] == "I'll analyze and reply"
    assert item["promise_due"] == "2026-06-05T17:00:00Z"
    assert item["history"][-1]["event"] == "status:awaiting_me"

    reloaded = store.load(store_path)["items"][iid]
    assert reloaded["status"] == "awaiting_me"
    assert reloaded["my_promise"] == "I'll analyze and reply"
    assert reloaded["promise_due"] == "2026-06-05T17:00:00Z"


def test_record_response_sets_answered_and_persists(store_path):
    s, iid = _fresh(store_path)

    item = triage_session.record_response(
        s, iid, response_text="Done — fixed and deployed.",
        response_posted="spaces/AAA/messages/REPLY1", now=FIXED, path=store_path,
    )

    assert item["status"] == "answered"
    assert item["response_text"] == "Done — fixed and deployed."
    assert item["response_posted"] == "spaces/AAA/messages/REPLY1"
    assert item["answered_at"] == "2026-06-04T13:00:00Z"  # _iso(FIXED)
    assert item["history"][-1]["event"] == "status:answered"

    reloaded = store.load(store_path)["items"][iid]
    assert reloaded["status"] == "answered"
    assert reloaded["response_posted"] == "spaces/AAA/messages/REPLY1"
    assert reloaded["answered_at"] == "2026-06-04T13:00:00Z"


def test_close_item_sets_closed_and_persists(store_path):
    s, iid = _fresh(store_path)
    item = triage_session.close_item(s, iid, now=FIXED, path=store_path)
    assert item["status"] == "closed"
    assert item["history"][-1]["event"] == "status:closed"
    assert store.load(store_path)["items"][iid]["status"] == "closed"


def test_ignore_item_sets_ignored_and_persists(store_path):
    s, iid = _fresh(store_path)
    item = triage_session.ignore_item(s, iid, now=FIXED, path=store_path)
    assert item["status"] == "ignored"
    assert item["history"][-1]["event"] == "status:ignored"
    assert store.load(store_path)["items"][iid]["status"] == "ignored"


# --------------------------------------------------------------------------- #
# Error propagation — unknown id surfaces store.py's KeyError on every mutator.
# --------------------------------------------------------------------------- #
def test_unknown_id_propagates_keyerror_on_every_mutator(store_path):
    s, _ = _fresh(store_path)
    bad = "deadbeef"

    with pytest.raises(KeyError):
        triage_session.set_triage(s, bad, priority="high", now=FIXED, path=store_path)
    with pytest.raises(KeyError):
        triage_session.snooze(s, bad, until="2026-06-05T09:00:00Z", now=FIXED, path=store_path)
    with pytest.raises(KeyError):
        triage_session.record_promise(
            s, bad, my_promise="x", promise_due="2026-06-05T09:00:00Z", now=FIXED, path=store_path)
    with pytest.raises(KeyError):
        triage_session.record_response(
            s, bad, response_text="x", response_posted="spaces/AAA/messages/Z",
            now=FIXED, path=store_path)
    with pytest.raises(KeyError):
        triage_session.close_item(s, bad, now=FIXED, path=store_path)
    with pytest.raises(KeyError):
        triage_session.ignore_item(s, bad, now=FIXED, path=store_path)
