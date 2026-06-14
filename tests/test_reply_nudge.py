"""Tests for the Tier-3 reply-nudge subsystem.

Three layers, no live network and no real LLM anywhere:
  - **analyze side** (``analyze_mentions``): ``select_nudge_items`` threshold
    predicate, ``detect_my_reply`` (injected ``fetch_fn``), ``_validate_suggestions``,
    ``generate_reply_suggestions`` (FakeAdapter), and the ``run_reply_nudge`` pass.
  - **templates**: ``render_nudge`` + ``_suggestions_block`` — the collapsible +
    copyable drafts block (Variant A: ``<code>`` inside ``<blockquote expandable>``),
    and the escaping of UNTRUSTED draft text.
  - **notify side**: ``nudge_candidates`` selection, ``run_nudge`` dispatch +
    ``reply_nudged_at`` stamping, gating (feature off / quiet hours / kill switch),
    and ``TelegramSender.send_nudge`` chunking — the transport is patched so no
    test touches the wire.

The autouse conftest fixture isolates the triage store, so passing
``store_path=None`` here is still safe (it resolves to a tmp file).
"""
from __future__ import annotations

import copy
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT))

import config  # noqa: E402
import notify  # noqa: E402
import templates  # noqa: E402
import analyze_mentions as am  # noqa: E402
from analysis_adapters import AnalysisAdapter, AnalysisRequest, AnalysisResult  # noqa: E402

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
ME = "users/117216798078927891621"


# --------------------------------------------------------------------------- #
# Builders.
# --------------------------------------------------------------------------- #
def _item(iid="i0", **over) -> dict:
    base = {
        "id": iid,
        "space_name": "spaces/X",
        "space_display": "Engineering",
        "space_type": "SPACE",
        "message_name": f"spaces/X/messages/{iid}",
        "thread_name": "spaces/X/threads/T",
        "sender_id": "users/42",
        "sender_name": "Bob",
        "created_time": "2026-06-13T11:00:00Z",
        "text": "@Artem can you review the PR?",
        "trigger": "user_mention",
        "priority": "high",
        "status": "new",
        "context_summary": "Bob asked Artem to review a PR",
        "last_notified": None,
        "response_posted": None,
        "reply_suggestions": None,
        "reply_nudged_at": None,
    }
    base.update(over)
    return base


def _store(items: list[dict]) -> dict:
    return {
        "version": 1, "me_user_id": None, "last_run": None,
        "cursor_per_space": {}, "items": {it["id"]: it for it in items},
    }


def _cfg(*, nudge_enabled=True, **nudge_over) -> dict:
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    cfg["me_user_id"] = ME
    nudge = cfg["analyze"]["reply_nudge"]
    nudge["enabled"] = nudge_enabled
    nudge.update(nudge_over)
    return cfg


def _minutes_ago(n: int) -> str:
    return notify._iso(NOW - timedelta(minutes=n))


# --------------------------------------------------------------------------- #
# Fake adapter.
# --------------------------------------------------------------------------- #
class FakeAdapter(AnalysisAdapter):
    name = "fake"

    def __init__(self, result: AnalysisResult):
        self._result = result
        self.calls: list[AnalysisRequest] = []

    def available(self) -> bool:
        return True

    def run(self, request: AnalysisRequest) -> AnalysisResult:
        self.calls.append(request)
        return self._result


def _suggest_ok(*suggestions) -> AnalysisResult:
    return AnalysisResult(ok=True, adapter="fake/v1", mode="classify",
                          data={"suggestions": list(suggestions)})


# --------------------------------------------------------------------------- #
# select_nudge_items — threshold predicate.
# --------------------------------------------------------------------------- #
class TestSelectNudgeItems:
    def test_high_eligible_after_threshold(self):
        s = _store([_item(priority="high", last_notified=_minutes_ago(25))])
        got = am.select_nudge_items(s, _cfg(), NOW)
        assert [it["id"] for it in got] == ["i0"]

    def test_high_not_eligible_before_threshold(self):
        s = _store([_item(priority="high", last_notified=_minutes_ago(15))])
        assert am.select_nudge_items(s, _cfg(), NOW) == []

    def test_normal_uses_its_own_threshold(self):
        # 25m: past high(20) but before normal(30) → not yet.
        s = _store([_item(priority="normal", last_notified=_minutes_ago(25))])
        assert am.select_nudge_items(s, _cfg(), NOW) == []
        s2 = _store([_item(priority="normal", last_notified=_minutes_ago(40))])
        assert [it["id"] for it in am.select_nudge_items(s2, _cfg(), NOW)] == ["i0"]

    def test_low_never_nudged(self):
        s = _store([_item(priority="low", last_notified=_minutes_ago(9999))])
        assert am.select_nudge_items(s, _cfg(), NOW) == []

    def test_skips_never_notified(self):
        s = _store([_item(last_notified=None)])
        assert am.select_nudge_items(s, _cfg(), NOW) == []

    def test_skips_already_replied(self):
        s = _store([_item(last_notified=_minutes_ago(60), response_posted=True)])
        assert am.select_nudge_items(s, _cfg(), NOW) == []

    def test_skips_already_nudged(self):
        s = _store([_item(last_notified=_minutes_ago(60),
                          reply_nudged_at=_minutes_ago(5))])
        assert am.select_nudge_items(s, _cfg(), NOW) == []

    def test_skips_closed_status(self):
        s = _store([_item(status="closed", last_notified=_minutes_ago(60))])
        assert am.select_nudge_items(s, _cfg(), NOW) == []

    def test_high_sorted_before_normal(self):
        s = _store([
            _item(iid="n", priority="normal", last_notified=_minutes_ago(60)),
            _item(iid="h", priority="high", last_notified=_minutes_ago(60)),
        ])
        assert [it["id"] for it in am.select_nudge_items(s, _cfg(), NOW)] == ["h", "n"]

    def test_check_limit_caps(self):
        items = [_item(iid=f"i{n}", last_notified=_minutes_ago(60)) for n in range(5)]
        s = _store(items)
        got = am.select_nudge_items(s, _cfg(check_limit=2), NOW)
        assert len(got) == 2


# --------------------------------------------------------------------------- #
# detect_my_reply — READ-ONLY, injected fetch.
# --------------------------------------------------------------------------- #
class TestDetectMyReply:
    def test_true_when_my_newer_message_exists(self):
        item = _item(created_time="2026-06-13T11:00:00Z")
        msgs = [{"sender": {"name": ME}, "createTime": "2026-06-13T11:30:00Z"}]
        assert am.detect_my_reply(item, ME, creds=None,
                                  fetch_fn=lambda *a, **k: msgs) is True

    def test_false_when_only_others_reply(self):
        item = _item()
        msgs = [{"sender": {"name": "users/42"}, "createTime": "2026-06-13T11:30:00Z"}]
        assert am.detect_my_reply(item, ME, creds=None,
                                  fetch_fn=lambda *a, **k: msgs) is False

    def test_older_message_of_mine_does_not_count(self):
        item = _item(created_time="2026-06-13T11:00:00Z")
        msgs = [{"sender": {"name": ME}, "createTime": "2026-06-13T10:00:00Z"}]
        assert am.detect_my_reply(item, ME, creds=None,
                                  fetch_fn=lambda *a, **k: msgs) is False

    def test_none_on_fetch_error(self):
        def boom(*a, **k):
            raise RuntimeError("api down")
        assert am.detect_my_reply(_item(), ME, creds=None, fetch_fn=boom) is None

    def test_none_without_me_id(self):
        assert am.detect_my_reply(_item(), "", creds=None,
                                  fetch_fn=lambda *a, **k: []) is None

    def test_bare_id_matches_prefixed(self):
        item = _item()
        msgs = [{"sender": {"name": "users/117216798078927891621"},
                 "createTime": "2026-06-13T11:30:00Z"}]
        # me_id passed bare — still matches the prefixed sender name.
        assert am.detect_my_reply(item, "117216798078927891621", creds=None,
                                  fetch_fn=lambda *a, **k: msgs) is True


# --------------------------------------------------------------------------- #
# _validate_suggestions / generate_reply_suggestions.
# --------------------------------------------------------------------------- #
class TestValidateSuggestions:
    def test_strips_and_caps(self):
        data = {"suggestions": ["  a ", "b", "", "   ", "c", "d"]}
        assert am._validate_suggestions(data, max_n=3) == ["a", "b", "c"]

    def test_none_when_empty(self):
        assert am._validate_suggestions({"suggestions": ["", "  "]}, max_n=3) is None

    def test_none_when_not_list(self):
        assert am._validate_suggestions({"suggestions": "nope"}, max_n=3) is None

    def test_none_when_not_dict(self):
        assert am._validate_suggestions(None, max_n=3) is None


class TestGenerateReplySuggestions:
    def test_returns_cleaned_list(self):
        adapter = FakeAdapter(_suggest_ok("Sure, on it.", "Give me an hour."))
        out = am.generate_reply_suggestions(_item(), _cfg(), adapter)
        assert out == ["Sure, on it.", "Give me an hour."]
        # The prompt embeds the (sanitized) message text.
        assert "review the PR" in adapter.calls[0].prompt

    def test_none_on_adapter_error(self):
        adapter = FakeAdapter(AnalysisResult(ok=False, adapter="fake/v1",
                                             mode="classify", error="timeout"))
        assert am.generate_reply_suggestions(_item(), _cfg(), adapter) is None


# --------------------------------------------------------------------------- #
# run_reply_nudge — the pass.
# --------------------------------------------------------------------------- #
class TestRunReplyNudge:
    def test_disabled_is_noop(self):
        s = _store([_item(last_notified=_minutes_ago(60))])
        before = copy.deepcopy(s["items"])
        adapter = FakeAdapter(_suggest_ok("x", "y"))
        stats = am.run_reply_nudge(s, _cfg(nudge_enabled=False), NOW,
                                   adapter=adapter, me_id=ME)
        assert stats == {"checked": 0, "replied": 0, "suggested": 0, "failed": 0}
        assert s["items"] == before
        assert adapter.calls == []

    def test_replied_marks_answered_no_suggestions(self):
        s = _store([_item(last_notified=_minutes_ago(60))])
        adapter = FakeAdapter(_suggest_ok("x", "y"))
        msgs = [{"sender": {"name": ME}, "createTime": "2026-06-13T11:30:00Z"}]
        stats = am.run_reply_nudge(s, _cfg(), NOW, adapter=adapter, me_id=ME,
                                   reply_fetch_fn=lambda *a, **k: msgs)
        assert stats["replied"] == 1 and stats["suggested"] == 0
        it = s["items"]["i0"]
        assert it["response_posted"] is True
        assert it["answered_at"] is not None
        assert it["reply_suggestions"] is None
        assert adapter.calls == []  # never asked for drafts

    def test_unanswered_stores_suggestions(self):
        s = _store([_item(last_notified=_minutes_ago(60))])
        adapter = FakeAdapter(_suggest_ok("Sure.", "In an hour."))
        stats = am.run_reply_nudge(s, _cfg(), NOW, adapter=adapter, me_id=ME,
                                   reply_fetch_fn=lambda *a, **k: [])
        assert stats["suggested"] == 1 and stats["replied"] == 0
        it = s["items"]["i0"]
        assert it["reply_suggestions"] == ["Sure.", "In an hour."]
        assert it["reply_checked_at"] is not None
        assert it["status"] == "new"  # lifecycle untouched

    def test_dry_run_writes_nothing(self):
        s = _store([_item(last_notified=_minutes_ago(60))])
        before = copy.deepcopy(s["items"])
        adapter = FakeAdapter(_suggest_ok("a", "b"))
        am.run_reply_nudge(s, _cfg(), NOW, adapter=adapter, me_id=ME,
                           reply_fetch_fn=lambda *a, **k: [], dry_run=True)
        assert s["items"] == before


# --------------------------------------------------------------------------- #
# templates.render_nudge + _suggestions_block.
# --------------------------------------------------------------------------- #
class TestRenderNudge:
    def _items(self):
        return [_item(reply_suggestions=["Sure, reviewing now.", "Give me an hour."])]

    def test_tg_html_suggestions_collapsible_and_copyable(self):
        out = templates.render_nudge(self._items(), now=NOW, mode="tg_html")
        # Variant A: <code> lines nested inside an expandable blockquote.
        assert "<blockquote expandable>" in out
        assert "<code>Sure, reviewing now.</code>" in out
        assert "<code>Give me an hour.</code>" in out

    def test_plain_suggestions_numbered(self):
        out = templates.render_nudge(self._items(), now=NOW, mode="plain")
        assert "1. Sure, reviewing now." in out
        assert "2. Give me an hour." in out

    def test_no_suggestions_block_when_empty(self):
        out = templates.render_nudge([_item(reply_suggestions=None)],
                                     now=NOW, mode="tg_html")
        assert "<code>" not in out
        # Header + sender still render.
        assert "Bob" in out

    def test_untrusted_draft_is_escaped(self):
        items = [_item(reply_suggestions=["</code></blockquote><script>x</script>"])]
        out = templates.render_nudge(items, now=NOW, mode="tg_html")
        # The injected close tags must be inert entities, not live markup.
        assert "<script>" not in out
        assert "&lt;script&gt;" in out
        assert "&lt;/code&gt;" in out

    def test_header_counts_localized(self):
        out = templates.render_nudge(self._items(), now=NOW, mode="plain")
        assert "Awaiting your reply" in out
        assert "1 waiting" in out


# --------------------------------------------------------------------------- #
# notify.nudge_candidates.
# --------------------------------------------------------------------------- #
class TestNudgeCandidates:
    def test_picks_items_with_suggestions_not_yet_nudged(self):
        s = _store([
            _item(iid="ready", reply_suggestions=["a", "b"]),
            _item(iid="no_sugg", reply_suggestions=None),
            _item(iid="done", reply_suggestions=["a"], reply_nudged_at=_minutes_ago(5)),
            _item(iid="answered", reply_suggestions=["a"], response_posted=True),
        ])
        got = notify.nudge_candidates(s, _cfg())
        assert [it["id"] for it in got] == ["ready"]

    def test_sorted_high_first(self):
        s = _store([
            _item(iid="n", priority="normal", reply_suggestions=["a"]),
            _item(iid="h", priority="high", reply_suggestions=["a"]),
        ])
        assert [it["id"] for it in notify.nudge_candidates(s, _cfg())] == ["h", "n"]


# --------------------------------------------------------------------------- #
# notify.run_nudge — dispatch + stamping + gating.
# --------------------------------------------------------------------------- #
class _RecordingSender(notify.Sender):
    name = "rec"

    def __init__(self, ok=True):
        self._ok = ok
        self.nudged: list = []

    def enabled(self, cfg):
        return True

    def send(self, new_items, esc_items, now):
        return True

    def send_nudge(self, items, now):
        self.nudged.append(list(items))
        return self._ok


class TestRunNudge:
    def test_disabled_skips(self):
        s = _store([_item(reply_suggestions=["a"])])
        rec = _RecordingSender()
        res = notify.run_nudge(s, _cfg(nudge_enabled=False), now=NOW,
                               senders=[rec], store_path=None)
        assert res["skipped"] is True
        assert rec.nudged == []

    def test_dispatch_and_stamp(self):
        s = _store([_item(iid="ready", reply_suggestions=["a", "b"])])
        rec = _RecordingSender(ok=True)
        res = notify.run_nudge(s, _cfg(), now=NOW, senders=[rec], store_path=None)
        assert res["nudged"] == ["ready"]
        assert res["senders_succeeded"] == ["rec"]
        # reply_nudged_at stamped on success — one nudge per item.
        assert s["items"]["ready"]["reply_nudged_at"] is not None
        assert len(rec.nudged) == 1 and rec.nudged[0][0]["id"] == "ready"

    def test_no_stamp_when_send_fails(self):
        s = _store([_item(iid="ready", reply_suggestions=["a"])])
        rec = _RecordingSender(ok=False)
        res = notify.run_nudge(s, _cfg(), now=NOW, senders=[rec], store_path=None)
        assert res["nudged"] == []
        assert s["items"]["ready"]["reply_nudged_at"] is None

    def test_quiet_hours_hold(self):
        s = _store([_item(reply_suggestions=["a"])])
        cfg = _cfg()
        cfg["quiet_hours"] = {"start": "00:00", "end": "23:59", "tz": "UTC"}
        rec = _RecordingSender()
        res = notify.run_nudge(s, cfg, now=NOW, senders=[rec], store_path=None)
        assert res["quiet"] is True
        assert rec.nudged == []
        assert s["items"][_item()["id"]]["reply_nudged_at"] is None

    def test_kill_switch_mutes(self):
        s = _store([_item(reply_suggestions=["a"])])
        cfg = _cfg()
        cfg["enabled"] = False
        rec = _RecordingSender()
        res = notify.run_nudge(s, cfg, now=NOW, senders=[rec], store_path=None)
        assert res["muted"] is True
        assert rec.nudged == []

    def test_dry_run_no_stamp(self):
        s = _store([_item(iid="ready", reply_suggestions=["a"])])
        rec = _RecordingSender()
        res = notify.run_nudge(s, _cfg(), now=NOW, senders=[rec],
                               store_path=None, dry_run=True)
        assert res["nudged"] == ["ready"]
        assert rec.nudged == []  # dry-run prints, never dispatches
        assert s["items"]["ready"]["reply_nudged_at"] is None


# --------------------------------------------------------------------------- #
# TelegramSender.send_nudge — chunking + transport (patched).
# --------------------------------------------------------------------------- #
def _tg_sender(monkeypatch):
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    cfg["channels"]["telegram"] = {"enabled": True}
    secrets = {"TELEGRAM_BOT_TOKEN": "T:tok", "TELEGRAM_CHAT_ID": "C"}
    return notify.TelegramSender(cfg, secrets=secrets)


class TestTelegramSendNudge:
    def test_short_nudge_single_message(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notify.TelegramSender, "_http_post",
                            lambda self, url, payload: sent.append(payload) or {"ok": True})
        sender = _tg_sender(monkeypatch)
        items = [_item(reply_suggestions=["Sure.", "In an hour."])]
        assert sender.send_nudge(items, NOW) is True
        assert len(sent) == 1
        assert "<code>Sure.</code>" in sent[0]["text"]

    def test_long_nudge_splits_into_valid_chunks(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notify.TelegramSender, "_http_post",
                            lambda self, url, payload: sent.append(payload) or {"ok": True})
        sender = _tg_sender(monkeypatch)
        big = "x" * 1500
        items = [_item(iid=f"i{n}", reply_suggestions=[big, big]) for n in range(6)]
        assert sender.send_nudge(items, NOW) is True
        assert len(sent) >= 2
        for payload in sent:
            text = payload["text"]
            assert len(text) <= notify.TelegramSender._TEXT_LIMIT
            # Each chunk is self-contained, balanced HTML.
            assert text.count("<blockquote expandable>") == text.count("</blockquote>")

    def test_failed_chunk_reports_failure(self, monkeypatch):
        monkeypatch.setattr(notify.TelegramSender, "_http_post",
                            lambda self, url, payload: {"ok": False, "description": "bad"})
        sender = _tg_sender(monkeypatch)
        items = [_item(reply_suggestions=["a"])]
        assert sender.send_nudge(items, NOW) is False
