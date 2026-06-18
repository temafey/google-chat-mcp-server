"""Tests for scripts/notify.py — the deterministic notification dispatcher.

No live network: senders are fakes and ``google_chat.send_message`` is patched.
Time is injected via the ``now`` parameter so quiet-hours / escalation logic is
exercised against a fixed clock.
"""
from __future__ import annotations

import copy
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

# scripts/ on the path so ``import notify`` / ``config`` / ``store`` resolve,
# mirroring the layout the other tests in this suite use.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import notify  # noqa: E402
import store  # noqa: E402

KIEV = ZoneInfo("Europe/Kiev")


# --------------------------------------------------------------------------- #
# Builders.
# --------------------------------------------------------------------------- #
def _item(iid: str, **over) -> dict:
    base = {
        "id": iid,
        "space_name": "spaces/X",
        "message_name": f"spaces/X/messages/{iid}",
        "sender_id": "users/sender",
        "sender_name": "Sender",
        "text": "hello there",
        "trigger": "broadcast",
        "status": "new",
        "priority": "unset",
        "last_notified": None,
        "promise_escalated_at": None,
        "my_promise": None,
        "promise_due": None,
        "history": [],
    }
    base.update(over)
    return base


def _store(*items) -> dict:
    return {"version": 1, "items": {it["id"]: it for it in items}}


def _cfg(**over) -> dict:
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    cfg.update(over)
    return cfg


class FakeSender:
    def __init__(self, name="fake", ok=True, raises=False):
        self.name = name
        self._ok = ok
        self._raises = raises
        self.calls = []

    def enabled(self, cfg):
        return True

    def send(self, new_items, esc_items, now):
        self.calls.append((list(new_items), list(esc_items), now))
        if self._raises:
            raise RuntimeError("boom")
        return self._ok


# A wall-clock time that is NOT inside the default 22:00–08:00 quiet window.
ACTIVE_NOW = datetime(2026, 6, 4, 12, 0, tzinfo=KIEV)


# --------------------------------------------------------------------------- #
# Baseline notifiability.
# --------------------------------------------------------------------------- #
def test_direct_dm_notified_plain_not(tmp_path):
    dm = _item("dm", trigger="direct_dm")
    plain = _item("plain", trigger="broadcast", text="just a normal note")
    st = _store(dm, plain)
    sender = FakeSender()

    res = notify.run_once(
        st, _cfg(), now=ACTIVE_NOW, senders=[sender],
        store_path=tmp_path / "s.json",
    )

    assert "dm" in res["new_notified"]
    assert "plain" not in res["new_notified"]
    assert st["items"]["dm"]["last_notified"] is not None
    assert st["items"]["plain"]["last_notified"] is None
    assert sender.calls  # dispatched


def test_urgency_keyword_and_vip_notified(tmp_path):
    urgent = _item("urg", trigger="broadcast", text="this is URGENT, please look")
    vip = _item("vip", trigger="broadcast", sender_id="users/boss", text="hi")
    st = _store(urgent, vip)
    cfg = _cfg(vip_senders=["users/boss"])

    res = notify.run_once(
        st, cfg, now=ACTIVE_NOW, senders=[FakeSender()],
        store_path=tmp_path / "s.json",
    )

    assert set(res["new_notified"]) == {"urg", "vip"}


def test_already_notified_not_renotified(tmp_path):
    seen = _item("seen", trigger="direct_dm", last_notified="2026-06-01T10:00:00Z")
    st = _store(seen)

    res = notify.run_once(
        st, _cfg(), now=ACTIVE_NOW, senders=[FakeSender()],
        store_path=tmp_path / "s.json",
    )

    assert res["new_notified"] == []
    # unchanged
    assert st["items"]["seen"]["last_notified"] == "2026-06-01T10:00:00Z"


# --------------------------------------------------------------------------- #
# Pin piggyback.
# --------------------------------------------------------------------------- #
def test_pinned_item_rides_along_but_not_marked_notified(tmp_path):
    # A DM fires the digest (baseline); a plain pinned broadcast is NOT a baseline
    # candidate on its own, but rides along because the digest is already firing.
    trigger_item = _item("dm", trigger="direct_dm")
    pinned = _item("pin", trigger="broadcast", text="plain note", pinned=True)
    st = _store(trigger_item, pinned)
    sender = FakeSender()

    res = notify.run_once(
        st, _cfg(), now=ACTIVE_NOW, senders=[sender], store_path=tmp_path / "s.json",
    )

    dispatched_ids = {it["id"] for it in sender.calls[0][0]}
    assert dispatched_ids == {"dm", "pin"}            # pin rode along
    assert res["pinned_ridealong"] == ["pin"]
    # The pin is NEVER marked notified — it reappears every cycle until unpinned.
    assert "pin" not in res["new_notified"]
    assert st["items"]["pin"]["last_notified"] is None
    assert st["items"]["dm"]["last_notified"] is not None


def test_pinned_item_alone_never_forces_send(tmp_path):
    # Nothing else is firing — a lone pin must NOT trigger a digest.
    pinned = _item("pin", trigger="broadcast", text="plain note", pinned=True)
    st = _store(pinned)
    sender = FakeSender()

    res = notify.run_once(
        st, _cfg(), now=ACTIVE_NOW, senders=[sender], store_path=tmp_path / "s.json",
    )

    assert sender.calls == []                          # nothing dispatched
    assert res["pinned_ridealong"] == []
    assert st["items"]["pin"]["last_notified"] is None


def test_pinned_candidate_not_duplicated(tmp_path):
    # An item that is BOTH a baseline NEW candidate AND pinned appears once, and is
    # marked notified normally (it is not a ride-along).
    pinned_dm = _item("dm", trigger="direct_dm", pinned=True)
    st = _store(pinned_dm)
    sender = FakeSender()

    res = notify.run_once(
        st, _cfg(), now=ACTIVE_NOW, senders=[sender], store_path=tmp_path / "s.json",
    )

    dispatched_ids = [it["id"] for it in sender.calls[0][0]]
    assert dispatched_ids.count("dm") == 1             # not duplicated
    assert res["pinned_ridealong"] == []               # already a NEW candidate
    assert "dm" in res["new_notified"]
    assert st["items"]["dm"]["last_notified"] is not None


def test_pinned_closed_item_is_inert(tmp_path):
    # A pin on a terminal (closed) item never rides along — pins follow open items.
    trigger_item = _item("dm", trigger="direct_dm")
    pinned_closed = _item("pinc", trigger="broadcast", text="x", pinned=True, status="closed")
    st = _store(trigger_item, pinned_closed)
    sender = FakeSender()

    res = notify.run_once(
        st, _cfg(), now=ACTIVE_NOW, senders=[sender], store_path=tmp_path / "s.json",
    )

    dispatched_ids = {it["id"] for it in sender.calls[0][0]}
    assert "pinc" not in dispatched_ids
    assert res["pinned_ridealong"] == []


def test_pinned_item_suppressed_in_quiet_hours(tmp_path):
    # During quiet hours only the escalation safety net speaks; pins behave like
    # NEW items and are suppressed (they do NOT ride along on an escalation).
    now_quiet = datetime(2026, 6, 4, 23, 0, tzinfo=KIEV)  # inside default 22:00–08:00
    overdue = _item(
        "ovd", status="awaiting_me", my_promise="send report",
        promise_due="2026-06-04T10:00:00Z",  # < now → overdue
    )
    pinned = _item("pin", trigger="broadcast", text="plain note", pinned=True)
    st = _store(overdue, pinned)
    sender = FakeSender()

    res = notify.run_once(
        st, _cfg(), now=now_quiet, senders=[sender], store_path=tmp_path / "s.json",
    )

    assert "ovd" in res["escalated"]                   # safety net still fires
    dispatched_ids = {it["id"] for it in sender.calls[0][0]}
    assert "pin" not in dispatched_ids                 # pin suppressed in quiet hours
    assert res["pinned_ridealong"] == []
    assert st["items"]["pin"]["last_notified"] is None


# --------------------------------------------------------------------------- #
# Escalation.
# --------------------------------------------------------------------------- #
def test_overdue_escalates_then_not_again(tmp_path):
    due = "2026-06-04T08:00:00Z"  # before ACTIVE_NOW (12:00 Kiev == 09:00Z)
    item = _item(
        "owe",
        status="awaiting_me",
        last_notified="2026-06-01T10:00:00Z",  # already notified — still escalates
        my_promise="send the report",
        promise_due=due,
    )
    st = _store(item)
    spath = tmp_path / "s.json"

    res1 = notify.run_once(st, _cfg(), now=ACTIVE_NOW, senders=[FakeSender()], store_path=spath)
    assert res1["escalated"] == ["owe"]
    assert st["items"]["owe"]["promise_escalated_at"] is not None

    # Second run — promise_escalated_at is set → no re-escalation.
    res2 = notify.run_once(st, _cfg(), now=ACTIVE_NOW, senders=[FakeSender()], store_path=spath)
    assert res2["escalated"] == []


# --------------------------------------------------------------------------- #
# Kill switch.
# --------------------------------------------------------------------------- #
def test_inactive_sends_nothing(tmp_path):
    dm = _item("dm", trigger="direct_dm")
    st = _store(dm)
    sender = FakeSender()

    res = notify.run_once(
        st, _cfg(enabled=False), now=ACTIVE_NOW, senders=[sender],
        store_path=tmp_path / "s.json",
    )

    assert res["muted"] is True
    assert sender.calls == []
    assert st["items"]["dm"]["last_notified"] is None


# --------------------------------------------------------------------------- #
# Quiet hours (default window 22:00–08:00, spans midnight).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "hour, minute, inside",
    [(23, 30, True), (3, 0, True), (12, 0, False)],
)
def test_quiet_hours_window(hour, minute, inside):
    now = datetime(2026, 6, 4, hour, minute, tzinfo=KIEV)
    assert notify.in_quiet_hours(now, _cfg()) is inside


def test_quiet_suppresses_new_but_escalation_fires(tmp_path):
    now_quiet = datetime(2026, 6, 4, 23, 30, tzinfo=KIEV)
    dm = _item("dm", trigger="direct_dm")
    owe = _item(
        "owe",
        status="awaiting_me",
        my_promise="reply",
        promise_due="2026-06-04T08:00:00Z",
    )
    st = _store(dm, owe)
    sender = FakeSender()

    res = notify.run_once(
        st, _cfg(), now=now_quiet, senders=[sender], store_path=tmp_path / "s.json",
    )

    # NEW baseline suppressed and NOT marked notified.
    assert res["new_notified"] == []
    assert "dm" in res["suppressed_quiet"]
    assert st["items"]["dm"]["last_notified"] is None
    # Overdue escalation still fires.
    assert res["escalated"] == ["owe"]
    assert st["items"]["owe"]["promise_escalated_at"] is not None


# --------------------------------------------------------------------------- #
# Sender registry.
# --------------------------------------------------------------------------- #
def test_gcinbox_disabled_when_no_space_name():
    senders = notify.build_senders(_cfg())  # gc_inbox.enabled defaults False
    names = [s.name for s in senders]
    assert names == ["console"]


def test_gcinbox_enabled_with_space_name():
    cfg = _cfg()
    cfg["channels"]["gc_inbox"] = {"enabled": True, "space_name": "spaces/INBOX"}
    names = [s.name for s in notify.build_senders(cfg)]
    assert "console" in names and "gc_inbox" in names


def test_gcinbox_requires_both_enabled_and_space():
    cfg = _cfg()
    cfg["channels"]["gc_inbox"] = {"enabled": True, "space_name": None}
    assert [s.name for s in notify.build_senders(cfg)] == ["console"]


# --------------------------------------------------------------------------- #
# Sender isolation.
# --------------------------------------------------------------------------- #
def test_failing_sender_does_not_block_success(tmp_path):
    dm = _item("dm", trigger="direct_dm")
    st = _store(dm)
    failing = FakeSender(name="bad", raises=True)
    good = FakeSender(name="good", ok=True)

    res = notify.run_once(
        st, _cfg(), now=ACTIVE_NOW, senders=[failing, good],
        store_path=tmp_path / "s.json",
    )

    assert res["senders_succeeded"] == ["good"]
    assert res["new_notified"] == ["dm"]
    assert st["items"]["dm"]["last_notified"] is not None


def test_all_senders_fail_no_mark(tmp_path):
    dm = _item("dm", trigger="direct_dm")
    st = _store(dm)
    failing = FakeSender(name="bad", ok=False)

    res = notify.run_once(
        st, _cfg(), now=ACTIVE_NOW, senders=[failing],
        store_path=tmp_path / "s.json",
    )

    assert res["senders_succeeded"] == []
    assert res["new_notified"] == []
    assert st["items"]["dm"]["last_notified"] is None


# --------------------------------------------------------------------------- #
# GCInboxSender against a mocked google_chat.send_message.
# --------------------------------------------------------------------------- #
def test_gcinbox_send_success(monkeypatch):
    calls = {}

    def fake_send(space_name, text, thread_name=None):
        calls["args"] = (space_name, text)
        return {"name": "spaces/INBOX/messages/1"}

    monkeypatch.setattr(notify.google_chat, "send_message", fake_send)
    sender = notify.GCInboxSender(
        {"channels": {"gc_inbox": {"enabled": True, "space_name": "spaces/INBOX"}}}
    )
    item = _item("dm", trigger="direct_dm", sender_name="Mariia")
    assert sender.send([item], [], ACTIVE_NOW) is True
    space_name, text = calls["args"]
    assert space_name == "spaces/INBOX"
    # GC Inbox renders the 'gchat' dialect — header + *bold* sender.
    assert text.startswith("📥 Chat triage")
    assert "*Mariia*" in text


def test_gcinbox_send_failure_returns_false(monkeypatch):
    def fake_send(space_name, text, thread_name=None):
        return {"error": "403 forbidden", "status": 403}

    monkeypatch.setattr(notify.google_chat, "send_message", fake_send)
    sender = notify.GCInboxSender(
        {"channels": {"gc_inbox": {"enabled": True, "space_name": "spaces/INBOX"}}}
    )
    assert sender.send([_item("dm", trigger="direct_dm")], [], ACTIVE_NOW) is False


def test_gcinbox_send_awaits_coroutine(monkeypatch):
    async def fake_send(space_name, text, thread_name=None):
        return {"name": "spaces/INBOX/messages/1"}

    monkeypatch.setattr(notify.google_chat, "send_message", fake_send)
    sender = notify.GCInboxSender(
        {"channels": {"gc_inbox": {"enabled": True, "space_name": "spaces/INBOX"}}}
    )
    assert sender.send([_item("dm", trigger="direct_dm")], [], ACTIVE_NOW) is True


# --------------------------------------------------------------------------- #
# Dry-run.
# --------------------------------------------------------------------------- #
def test_dry_run_sends_and_persists_nothing(tmp_path):
    dm = _item("dm", trigger="direct_dm")
    st = _store(dm)
    sender = FakeSender()
    spath = tmp_path / "s.json"

    res = notify.run_once(
        st, _cfg(), now=ACTIVE_NOW, dry_run=True, senders=[sender], store_path=spath,
    )

    assert res["dry_run"] is True
    assert sender.calls == []  # nothing sent
    assert st["items"]["dm"]["last_notified"] is None  # nothing persisted
    assert not spath.exists()


# --------------------------------------------------------------------------- #
# Card digest — header / NEW-section cap (header keeps TRUE totals).
# --------------------------------------------------------------------------- #
def _new_block_starts(lines):
    """Card NEW blocks open with a priority icon; OVERDUE blocks open with ⏰."""
    icons = ("🔴", "🟡", "🟢", "⚪")
    return [ln for ln in lines if ln.startswith(icons)]


def test_digest_header_two_lines_and_divider():
    new_items = [_item("n0", trigger="direct_dm")]
    esc_items = [_item("o0", my_promise="reply", promise_due="2026-06-04T08:00:00Z")]
    lines = notify.build_digest(new_items, esc_items, now=ACTIVE_NOW).splitlines()
    assert lines[0] == "📥 Chat triage"
    assert lines[1] == "🆕 1 new · ⏰ 1 overdue"
    assert lines[2] == "──────────"


def test_digest_header_drops_new_clause_when_zero():
    esc_items = [_item("o0", my_promise="reply", promise_due="2026-06-04T08:00:00Z")]
    lines = notify.build_digest([], esc_items, now=ACTIVE_NOW).splitlines()
    assert lines[0] == "📥 Chat triage"
    assert lines[1] == "⏰ 1 overdue"  # no 🆕 clause
    assert "new" not in lines[1]


def test_digest_header_drops_overdue_clause_when_zero():
    new_items = [_item("n0", trigger="direct_dm")]
    lines = notify.build_digest(new_items, [], now=ACTIVE_NOW).splitlines()
    assert lines[1] == "🆕 1 new"  # no ⏰ clause
    assert "overdue" not in lines[1]


def test_digest_caps_new_blocks_with_overflow():
    new_items = [_item(f"n{i}", trigger="direct_dm", sender_name=f"S{i}") for i in range(25)]
    digest = notify.build_digest(new_items, [], now=ACTIVE_NOW, cap=10)
    lines = digest.splitlines()

    assert lines[1] == "🆕 25 new"  # TRUE total in header
    assert len(_new_block_starts(lines)) == 10
    overflow = [ln for ln in lines if "more new" in ln]
    assert overflow == ["…and 15 more new"]


def test_digest_exact_cap_no_overflow():
    new_items = [_item(f"n{i}", trigger="direct_dm") for i in range(10)]
    lines = notify.build_digest(new_items, [], now=ACTIVE_NOW, cap=10).splitlines()
    assert len(_new_block_starts(lines)) == 10
    assert not any("more new" in ln for ln in lines)


def test_digest_under_cap_shows_all():
    new_items = [_item(f"n{i}", trigger="direct_dm") for i in range(3)]
    lines = notify.build_digest(new_items, [], now=ACTIVE_NOW, cap=10).splitlines()
    assert len(_new_block_starts(lines)) == 3
    assert not any("more new" in ln for ln in lines)


def test_digest_overdue_never_truncated_even_when_new_overflows():
    new_items = [_item(f"n{i}", trigger="direct_dm") for i in range(25)]
    esc_items = [
        _item(f"o{i}", sender_name=f"O{i}", my_promise="reply", promise_due="2026-06-04T08:00:00Z")
        for i in range(12)
    ]
    digest = notify.build_digest(new_items, esc_items, now=ACTIVE_NOW, cap=10)
    lines = digest.splitlines()

    overdue_lines = [ln for ln in lines if ln.startswith("⏰")]
    assert len(overdue_lines) == 12  # all overdue present, never capped
    # NEW blocks still capped at 10 (overflow line excluded by the ⏰ filter).
    new_blocks = [ln for ln in _new_block_starts(lines) if not ln.startswith("⏰")]
    assert len(new_blocks) == 10


def test_run_once_marks_all_candidates_notified_regardless_of_display_cap(tmp_path):
    # 25 NEW candidates — more than DIGEST_NEW_CAP. The rendered TEXT caps at 10,
    # but ALL 25 dispatched items must still be marked notified.
    candidates = [_item(f"n{i}", trigger="direct_dm") for i in range(25)]
    st = _store(*candidates)
    sender = FakeSender()

    res = notify.run_once(
        st, _cfg(), now=ACTIVE_NOW, senders=[sender],
        store_path=tmp_path / "s.json",
    )

    assert len(res["new_notified"]) == 25  # all candidates, NOT <= cap
    assert all(st["items"][f"n{i}"]["last_notified"] is not None for i in range(25))
    # The sender received ALL 25 items (it renders its own text); the rendered
    # plain Card still caps the visible blocks at 10 with an overflow line.
    dispatched_new, dispatched_esc, _now = sender.calls[0]
    assert len(dispatched_new) == 25
    rendered = notify.build_digest(dispatched_new, dispatched_esc, now=ACTIVE_NOW)
    assert rendered.splitlines()[1] == "🆕 25 new"
    assert "…and 15 more new" in rendered


class TestDigestOrdering:
    """new_candidates / nudge_candidates: priority bucket first, then grouped by
    space and chronological (oldest→newest) within each space, so a conversation
    reads in send order instead of dict-insertion order."""

    def test_chronological_within_space(self):
        # Same priority, one space, inserted out of order → sorted by created_time.
        st = _store(
            _item("c", trigger="user_mention", priority="normal",
                  created_time="2026-06-15T10:30:00Z"),
            _item("a", trigger="user_mention", priority="normal",
                  created_time="2026-06-15T10:00:00Z"),
            _item("b", trigger="user_mention", priority="normal",
                  created_time="2026-06-15T10:15:00Z"),
        )
        order = [it["id"] for it in notify.new_candidates(st, _cfg())]
        assert order == ["a", "b", "c"]

    def test_grouped_by_space_then_chronological(self):
        # Two spaces interleaved on insertion → each space's run stays contiguous
        # and chronological. Space ordering is by space_name (stable, deterministic).
        st = _store(
            _item("y2", trigger="user_mention", priority="normal",
                  space_name="spaces/Y", created_time="2026-06-15T10:20:00Z"),
            _item("x1", trigger="user_mention", priority="normal",
                  space_name="spaces/X", created_time="2026-06-15T10:00:00Z"),
            _item("y1", trigger="user_mention", priority="normal",
                  space_name="spaces/Y", created_time="2026-06-15T10:05:00Z"),
            _item("x2", trigger="user_mention", priority="normal",
                  space_name="spaces/X", created_time="2026-06-15T10:10:00Z"),
        )
        order = [it["id"] for it in notify.new_candidates(st, _cfg())]
        assert order == ["x1", "x2", "y1", "y2"]

    def test_priority_beats_chronology(self):
        # A later high-priority message still sorts before an earlier normal one.
        st = _store(
            _item("normal_early", trigger="user_mention", priority="normal",
                  created_time="2026-06-15T09:00:00Z"),
            _item("high_late", trigger="user_mention", priority="high",
                  created_time="2026-06-15T11:00:00Z"),
        )
        order = [it["id"] for it in notify.new_candidates(st, _cfg())]
        assert order == ["high_late", "normal_early"]

    def test_nudge_candidates_same_ordering(self):
        st = _store(
            _item("n2", status="new", priority="normal",
                  reply_suggestions=["ok"], reply_nudged_at=None,
                  created_time="2026-06-15T10:20:00Z"),
            _item("n1", status="new", priority="normal",
                  reply_suggestions=["ok"], reply_nudged_at=None,
                  created_time="2026-06-15T10:00:00Z"),
        )
        order = [it["id"] for it in notify.nudge_candidates(st, _cfg())]
        assert order == ["n1", "n2"]
