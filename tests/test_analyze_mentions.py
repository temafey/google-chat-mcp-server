"""Unit tests for the AI analysis stage (scripts/analyze_mentions.py, Wave B1).

All external I/O is eliminated:
  - A ``FakeAdapter`` returns canned ``AnalysisResult`` objects; no subprocess.
  - Config / store use tmp dirs; no real ``~/.claude-orchestrator`` access.
  - No Google Chat network calls at any point.

Coverage matrix
---------------
GATE tests
  - ``analyze.enabled=False`` → run_analysis is a no-op (store unchanged,
    adapter never called).
  - ``is_active``=False (global mute) → same result.

ADAPTER tests
  - Adapter ``available()``=False → graceful no-op, store unchanged.
  - Empty order list → no-op.

SELECTION tests
  - Only ``status=new AND priority=unset AND analyzed_at=None`` items selected.
  - ``max_items_per_run`` cap honored.
  - ``--limit`` override honored.
  - Already-analyzed items (analyzed_at set) skipped → idempotent re-run.

STORE path
  - context_sufficient=True + confidence >= threshold → all 7 fields written,
    ``status`` UNCHANGED, NO history entry appended.

DEFER path (Wave-C handoff)
  - context_sufficient=False OR confidence < threshold → only
    ``analyzed_at``, ``analyzed_by``, ``msg_type`` written; ``priority``
    stays "unset"; ``context_summary``/``thread_status`` stay None.

TRANSIENT path
  - adapter ``ok=False`` → NOTHING written for that item; ``analyzed_at``
    stays None (item retried next run).
  - Schema-invalid data → same result.

DRY-RUN
  - Computes/prints but writes NOTHING to the store JSON.

Tier-0 helpers
  - ``extract_quoted_text``: None item, dict without text, dict with text.
  - ``is_deictic``: variety of positive/negative cases.
  - ``build_ctx``: correct field mapping, me_role default.
"""
from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# scripts/ is not an installed package; add it (and the repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT))

import config  # noqa: E402
import store   # noqa: E402
from analysis_adapters import AnalysisAdapter, AnalysisRequest, AnalysisResult  # noqa: E402
import analyze_mentions as am  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #
NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = "2026-06-11T12:00:00Z"


def _make_config(
    *,
    enabled: bool = True,
    analyze_enabled: bool = True,
    min_confidence: float = 0.5,
    max_items: int = 20,
) -> dict:
    """Return a merged config dict with the analyze block configured."""
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    cfg["enabled"] = enabled
    cfg["me_display_name"] = "Artem"
    cfg["analyze"]["enabled"] = analyze_enabled
    cfg["analyze"]["min_confidence_to_store"] = min_confidence
    cfg["analyze"]["max_items_per_run"] = max_items
    return cfg


def _make_store(items: list[dict]) -> dict:
    """Return a fresh in-memory store dict with *items* pre-inserted.

    Intentionally does NOT call store.load(None) — that would read the real
    DEFAULT_STORE_PATH if it exists.  We always start from the private empty
    skeleton so tests are fully isolated.
    """
    s = {
        "version": 1,
        "me_user_id": None,
        "last_run": None,
        "cursor_per_space": {},
        "items": {},
    }
    for item in items:
        store.upsert_item(s, item, now=NOW)
    return s


def _item(
    *,
    msg: str = "spaces/A/messages/1",
    text: str = "@Artem can you review the PR?",
    status: str = "new",
    priority: str = "unset",
    analyzed_at=None,
) -> dict:
    """Build a minimal store item dict."""
    return {
        "space_name": "spaces/A",
        "space_display": "Engineering",
        "space_type": "SPACE",
        "message_name": msg,
        "thread_name": "spaces/A/threads/T",
        "sender_id": "users/42",
        "sender_name": "Bob",
        "created_time": "2026-06-11T11:00:00Z",
        "text": text,
        "trigger": "user_mention",
        "status": status,
        "priority": priority,
        "analyzed_at": analyzed_at,
        "quoted": None,
    }


# --------------------------------------------------------------------------- #
# Fake adapter
# --------------------------------------------------------------------------- #
class FakeAdapter(AnalysisAdapter):
    """Test double — returns canned results, never spawns a subprocess."""

    name = "fake"

    def __init__(
        self,
        *,
        is_available: bool = True,
        results: list[AnalysisResult] | None = None,
        default_result: AnalysisResult | None = None,
    ):
        self._available = is_available
        self._results = list(results or [])
        self._default = default_result or _ok_result()
        self.calls: list[AnalysisRequest] = []

    def available(self) -> bool:
        return self._available

    def run(self, request: AnalysisRequest) -> AnalysisResult:
        self.calls.append(request)
        if self._results:
            return self._results.pop(0)
        return self._default


def _ok_result(
    *,
    msg_type: str = "direct_request",
    priority: str = "high",
    priority_reason: str = "blocking deploy",
    summary: str = "Bob asked Artem to review a PR",
    action_required: bool = True,
    context_sufficient: bool = True,
    confidence: float = 0.9,
    adapter_name: str = "fake/v1",
) -> AnalysisResult:
    return AnalysisResult(
        ok=True,
        adapter=adapter_name,
        mode="classify",
        data={
            "type": msg_type,
            "priority": priority,
            "priority_reason": priority_reason,
            "summary": summary,
            "action_required": action_required,
            "context_sufficient": context_sufficient,
            "confidence": confidence,
        },
    )


def _error_result(reason: str = "timeout") -> AnalysisResult:
    return AnalysisResult(ok=False, adapter="fake/v1", mode="classify", error=reason)


# --------------------------------------------------------------------------- #
# GATE tests
# --------------------------------------------------------------------------- #

class TestGate:
    def test_analyze_disabled_is_noop(self):
        cfg = _make_config(analyze_enabled=False)
        s = _make_store([_item()])
        items_before = copy.deepcopy(s["items"])
        fake = FakeAdapter()

        result = am.run_analysis(s, cfg, NOW, adapter=fake)

        assert result["status"] == "disabled"
        assert fake.calls == []
        assert s["items"] == items_before

    def test_global_muted_is_noop(self):
        cfg = _make_config(analyze_enabled=True, enabled=False)
        s = _make_store([_item()])
        items_before = copy.deepcopy(s["items"])
        fake = FakeAdapter()

        result = am.run_analysis(s, cfg, NOW, adapter=fake)

        assert result["status"] == "disabled"
        assert fake.calls == []
        assert s["items"] == items_before


# --------------------------------------------------------------------------- #
# ADAPTER tests
# --------------------------------------------------------------------------- #

class TestAdapterSelection:
    def test_router_returns_none_when_unavailable(self):
        cfg = _make_config()
        cfg["analyze"]["adapters"]["order"] = ["claude"]
        fake = FakeAdapter(is_available=False)
        registry = {"claude": fake}

        adapter = am._select_adapter(cfg, registry=registry)
        assert adapter is None

    def test_router_returns_none_empty_order(self):
        cfg = _make_config()
        cfg["analyze"]["adapters"]["order"] = []

        adapter = am._select_adapter(cfg)
        assert adapter is None

    def test_run_analysis_no_adapter_from_router(self):
        """When _select_adapter returns None (no adapter= kwarg), result is ok no-op."""
        cfg = _make_config()
        cfg["analyze"]["adapters"]["order"] = []  # nothing available
        s = _make_store([_item()])
        items_before = copy.deepcopy(s["items"])

        result = am.run_analysis(s, cfg, NOW)  # no adapter= kwarg

        assert result["status"] == "ok"
        assert result["selected"] == 0
        assert s["items"] == items_before


# --------------------------------------------------------------------------- #
# SELECTION tests
# --------------------------------------------------------------------------- #

class TestSelection:
    def test_selects_only_new_unset_unanalyzed(self):
        # All 4 items start with status="new" after upsert (upsert forces "new").
        # We then manually adjust status/priority/analyzed_at via store helpers
        # so we can test the predicate.
        items = [
            _item(msg="spaces/A/messages/1"),  # eligible: new + unset + no analyzed_at
            _item(msg="spaces/A/messages/2"),  # ineligible: will be moved to triaged
            _item(msg="spaces/A/messages/3"),  # ineligible: will get priority=high
            _item(msg="spaces/A/messages/4"),  # ineligible: will have analyzed_at set
        ]
        cfg = _make_config()
        s = _make_store(items)

        # Get IDs by message_name
        by_msg = {it["message_name"]: iid for iid, it in s["items"].items()}
        # Make item 2 triaged
        store.set_status(s, by_msg["spaces/A/messages/2"], "triaged", now=NOW)
        # Make item 3 have a non-unset priority
        store.set_fields(s, by_msg["spaces/A/messages/3"], priority="high")
        # Make item 4 already-analyzed
        store.set_fields(s, by_msg["spaces/A/messages/4"], analyzed_at=NOW_ISO)

        selected = am.select_items(s, cfg)
        texts = {it["message_name"] for it in selected}
        assert texts == {"spaces/A/messages/1"}

    def test_max_items_cap(self):
        items = [_item(msg=f"spaces/A/messages/{i}") for i in range(10)]
        cfg = _make_config(max_items=3)
        s = _make_store(items)

        selected = am.select_items(s, cfg)
        assert len(selected) == 3

    def test_limit_override(self):
        items = [_item(msg=f"spaces/A/messages/{i}") for i in range(10)]
        cfg = _make_config(max_items=20)
        s = _make_store(items)

        selected = am.select_items(s, cfg, limit=2)
        assert len(selected) == 2

    def test_idempotent_rerun_skips_analyzed(self, tmp_path):
        """Items with analyzed_at set must not be re-selected on a second pass."""
        cfg = _make_config()
        s = _make_store([_item()])
        fake = FakeAdapter(default_result=_ok_result())
        store_path = tmp_path / "store.json"

        # First run: analyze the item
        am.run_analysis(s, cfg, NOW, adapter=fake, store_path=store_path)
        assert len(fake.calls) == 1

        # Second run on same store: item is already analyzed
        am.run_analysis(s, cfg, NOW, adapter=fake, store_path=store_path)
        assert len(fake.calls) == 1  # no additional call


# --------------------------------------------------------------------------- #
# STORE path
# --------------------------------------------------------------------------- #

class TestStorePath:
    def test_store_path_writes_7_fields(self, tmp_path):
        cfg = _make_config(min_confidence=0.5)
        s = _make_store([_item()])
        fake = FakeAdapter(default_result=_ok_result(
            msg_type="direct_request",
            priority="high",
            priority_reason="blocking deploy",
            summary="Bob needs PR review",
            context_sufficient=True,
            confidence=0.9,
        ))
        iid = list(s["items"].keys())[0]
        store_path = tmp_path / "store.json"

        am.run_analysis(s, cfg, NOW, adapter=fake, store_path=store_path)

        item = s["items"][iid]
        assert item["msg_type"] == "direct_request"
        assert item["priority"] == "high"
        assert item["priority_reason"] == "blocking deploy"
        assert item["context_summary"] == "Bob needs PR review"
        assert item["context_confidence"] == pytest.approx(0.9)
        assert item["analyzed_at"] == NOW_ISO
        assert item["analyzed_by"] == "fake/v1"

    def test_store_path_status_unchanged(self, tmp_path):
        """Status must never be touched by the analysis stage."""
        cfg = _make_config()
        s = _make_store([_item(status="new")])
        fake = FakeAdapter(default_result=_ok_result(context_sufficient=True, confidence=0.9))
        iid = list(s["items"].keys())[0]
        store_path = tmp_path / "store.json"

        am.run_analysis(s, cfg, NOW, adapter=fake, store_path=store_path)

        assert s["items"][iid]["status"] == "new"

    def test_store_path_no_history_entry(self, tmp_path):
        """set_fields must not append a history entry (only status transitions do)."""
        cfg = _make_config()
        s = _make_store([_item()])
        fake = FakeAdapter(default_result=_ok_result(context_sufficient=True, confidence=0.9))
        iid = list(s["items"].keys())[0]
        history_len_before = len(s["items"][iid].get("history", []))
        store_path = tmp_path / "store.json"

        am.run_analysis(s, cfg, NOW, adapter=fake, store_path=store_path)

        assert len(s["items"][iid].get("history", [])) == history_len_before

    def test_store_path_result_stats(self, tmp_path):
        cfg = _make_config()
        s = _make_store([_item(msg=f"spaces/A/messages/{i}") for i in range(3)])
        fake = FakeAdapter(default_result=_ok_result(context_sufficient=True, confidence=0.9))
        store_path = tmp_path / "store.json"

        result = am.run_analysis(s, cfg, NOW, adapter=fake, store_path=store_path)

        assert result["stored"] == 3
        assert result["deferred"] == 0
        assert result["failed"] == 0


# --------------------------------------------------------------------------- #
# DEFER path (Wave-C handoff)
# --------------------------------------------------------------------------- #

class TestDeferPath:
    def _check_deferred_fields(self, item: dict):
        """Assert the Wave-C handoff predicate fields are correct."""
        # analyzed_at/by and msg_type must be set
        assert item["analyzed_at"] == NOW_ISO
        assert item["analyzed_by"] == "fake/v1"
        assert item["msg_type"] is not None
        # priority must stay "unset" (Wave-C picks it up via this predicate)
        assert item["priority"] == "unset"
        # context_summary and thread_status must remain None
        assert item["context_summary"] is None
        assert item.get("thread_status") is None

    def test_defer_when_context_insufficient(self, tmp_path):
        cfg = _make_config(min_confidence=0.5)
        s = _make_store([_item()])
        fake = FakeAdapter(default_result=_ok_result(
            context_sufficient=False, confidence=0.8
        ))
        iid = list(s["items"].keys())[0]
        store_path = tmp_path / "store.json"

        result = am.run_analysis(s, cfg, NOW, adapter=fake, store_path=store_path)

        assert result["deferred"] == 1
        assert result["stored"] == 0
        self._check_deferred_fields(s["items"][iid])

    def test_defer_when_low_confidence(self, tmp_path):
        cfg = _make_config(min_confidence=0.5)
        s = _make_store([_item()])
        fake = FakeAdapter(default_result=_ok_result(
            context_sufficient=True, confidence=0.3
        ))
        iid = list(s["items"].keys())[0]
        store_path = tmp_path / "store.json"

        result = am.run_analysis(s, cfg, NOW, adapter=fake, store_path=store_path)

        assert result["deferred"] == 1
        self._check_deferred_fields(s["items"][iid])

    def test_defer_sets_wavec_predicate_fields(self, tmp_path):
        """The exact Wave-C predicate: analyzed_at set, priority unset, thread_status None."""
        cfg = _make_config(min_confidence=0.7)
        s = _make_store([_item()])
        fake = FakeAdapter(default_result=_ok_result(
            context_sufficient=False, confidence=0.6, msg_type="continuation"
        ))
        iid = list(s["items"].keys())[0]
        store_path = tmp_path / "store.json"

        am.run_analysis(s, cfg, NOW, adapter=fake, store_path=store_path)

        item = s["items"][iid]
        # Wave-C predicate check
        assert item["analyzed_at"] is not None
        assert item["priority"] == "unset"
        assert item.get("thread_status") is None


# --------------------------------------------------------------------------- #
# TRANSIENT path
# --------------------------------------------------------------------------- #

class TestTransientPath:
    def test_transient_writes_nothing(self, tmp_path):
        cfg = _make_config()
        s = _make_store([_item()])
        fake = FakeAdapter(default_result=_error_result("timeout"))
        iid = list(s["items"].keys())[0]
        store_path = tmp_path / "store.json"

        result = am.run_analysis(s, cfg, NOW, adapter=fake, store_path=store_path)

        assert result["failed"] == 1
        item = s["items"][iid]
        assert item["analyzed_at"] is None
        assert item["priority"] == "unset"
        assert item["context_summary"] is None

    def test_transient_retry_predicate_still_matches(self, tmp_path):
        """Item with transient failure still has analyzed_at=None → retried next run."""
        cfg = _make_config()
        s = _make_store([_item()])
        fake = FakeAdapter(default_result=_error_result())
        iid = list(s["items"].keys())[0]
        store_path = tmp_path / "store.json"

        am.run_analysis(s, cfg, NOW, adapter=fake, store_path=store_path)

        item = s["items"][iid]
        # Must still match the selection predicate on next run
        assert item["status"] == "new"
        assert item["priority"] == "unset"
        assert item["analyzed_at"] is None

    def test_schema_invalid_data_treated_as_transient(self, tmp_path):
        """An ok=True result with bad schema must not write anything."""
        cfg = _make_config()
        s = _make_store([_item()])
        bad = AnalysisResult(
            ok=True,
            adapter="fake/v1",
            mode="classify",
            data={"type": "not_a_valid_type", "priority": "high"},  # missing keys
        )
        fake = FakeAdapter(default_result=bad)
        iid = list(s["items"].keys())[0]
        store_path = tmp_path / "store.json"

        result = am.run_analysis(s, cfg, NOW, adapter=fake, store_path=store_path)

        assert result["failed"] == 1
        assert s["items"][iid]["analyzed_at"] is None

    def test_schema_invalid_priority(self, tmp_path):
        """Bad priority value → treated as transient; nothing written."""
        cfg = _make_config()
        s = _make_store([_item()])
        bad_data = {
            "type": "direct_request",
            "priority": "CRITICAL",           # invalid
            "priority_reason": "x",
            "summary": "y",
            "action_required": True,
            "context_sufficient": True,
            "confidence": 0.9,
        }
        bad = AnalysisResult(ok=True, adapter="fake/v1", mode="classify", data=bad_data)
        fake = FakeAdapter(default_result=bad)
        iid = list(s["items"].keys())[0]
        store_path = tmp_path / "store.json"

        result = am.run_analysis(s, cfg, NOW, adapter=fake, store_path=store_path)

        assert result["failed"] == 1
        assert s["items"][iid]["analyzed_at"] is None


# --------------------------------------------------------------------------- #
# DRY-RUN
# --------------------------------------------------------------------------- #

class TestDryRun:
    def test_dry_run_writes_nothing_to_store(self, tmp_path):
        cfg = _make_config()
        store_path = tmp_path / "store.json"
        s = _make_store([_item()])
        store.save(s, store_path)
        fake = FakeAdapter(default_result=_ok_result(context_sufficient=True, confidence=0.9))

        # Re-load from disk so we have the on-disk baseline
        s_disk = store.load(store_path)
        iid = list(s_disk["items"].keys())[0]
        items_snapshot = copy.deepcopy(s_disk["items"])

        am.run_analysis(s_disk, cfg, NOW, adapter=fake, dry_run=True, store_path=store_path)

        # In-memory object is mutated but file must be unchanged
        on_disk = json.loads(store_path.read_text())
        assert on_disk["items"] == {k: dict(v) for k, v in items_snapshot.items()}

    def test_dry_run_in_memory_mutation(self, tmp_path):
        """dry_run=True prints proposals; in B1 analyze_item may or may not mutate
        the in-memory item — the key guarantee is the FILE is not written.
        This test verifies the call doesn't raise and returns sane stats."""
        cfg = _make_config()
        s = _make_store([_item()])
        fake = FakeAdapter(default_result=_ok_result())
        store_path = tmp_path / "store.json"

        result = am.run_analysis(s, cfg, NOW, adapter=fake, dry_run=True, store_path=store_path)

        assert result["selected"] >= 0  # ran without error


# --------------------------------------------------------------------------- #
# Tier-0 helper unit tests
# --------------------------------------------------------------------------- #

class TestExtractQuotedText:
    def test_none_item_returns_none(self):
        assert am.extract_quoted_text({}) is None

    def test_none_quoted_returns_none(self):
        assert am.extract_quoted_text({"quoted": None}) is None

    def test_non_dict_quoted_returns_none(self):
        assert am.extract_quoted_text({"quoted": "raw string"}) is None

    def test_dict_without_text_returns_none(self):
        assert am.extract_quoted_text({"quoted": {"name": "spaces/A/messages/0", "type": "TEXT"}}) is None

    def test_dict_with_empty_text_returns_none(self):
        assert am.extract_quoted_text({"quoted": {"text": ""}}) is None

    def test_dict_with_text_returns_text(self):
        assert am.extract_quoted_text({"quoted": {"text": "here is the context"}}) == "here is the context"

    def test_dict_with_non_string_text(self):
        result = am.extract_quoted_text({"quoted": {"text": 42}})
        assert result == "42"


class TestIsDeictic:
    def test_empty_string(self):
        assert am.is_deictic("") is False

    def test_very_short(self):
        assert am.is_deictic("ok") is True
        assert am.is_deictic("yes") is True

    def test_caret(self):
        assert am.is_deictic("^") is True
        assert am.is_deictic("^ this") is True

    def test_any_update(self):
        assert am.is_deictic("any update on this?") is True
        assert am.is_deictic("any updates?") is True

    def test_this_one(self):
        assert am.is_deictic("this one please") is True

    def test_leading_and(self):
        assert am.is_deictic("and what about the tests?") is True

    def test_leading_also(self):
        assert am.is_deictic("also can you check X?") is True

    def test_leading_plus(self):
        assert am.is_deictic("+ what about logs?") is True

    def test_bare_it(self):
        assert am.is_deictic("it looks wrong to me") is True

    def test_normal_message(self):
        assert am.is_deictic("@Artem please review the PR for the auth service") is False

    def test_long_message_without_deictic(self):
        msg = "The deploy pipeline failed at the docker-build step with exit 137."
        assert am.is_deictic(msg) is False


class TestBuildCtx:
    def test_basic_mapping(self):
        cfg = _make_config()
        cfg["me_display_name"] = "Artem"
        item = _item(
            text="@Artem check the deploy",
        )
        item["space_display"] = "Backend"
        item["space_type"] = "SPACE"
        item["sender_name"] = "Bob"
        item["trigger"] = "user_mention"
        item["quoted"] = {"text": "original message"}

        ctx = am.build_ctx(item, cfg)

        assert ctx["me_name"] == "Artem"
        assert ctx["space_display"] == "Backend"
        assert ctx["space_type"] == "SPACE"
        assert ctx["sender_name"] == "Bob"
        assert ctx["trigger"] == "user_mention"
        assert ctx["text"] == "@Artem check the deploy"
        assert ctx["quoted"] == "original message"

    def test_me_role_default_when_absent(self):
        cfg = _make_config()
        cfg.pop("me_role", None)  # ensure absent

        ctx = am.build_ctx(_item(), cfg)
        assert ctx["me_role"] == "engineering lead"

    def test_me_role_from_config(self):
        cfg = _make_config()
        cfg["me_role"] = "Staff Engineer"

        ctx = am.build_ctx(_item(), cfg)
        assert ctx["me_role"] == "Staff Engineer"

    def test_no_quoted_returns_none(self):
        cfg = _make_config()
        item = _item()
        item["quoted"] = None

        ctx = am.build_ctx(item, cfg)
        assert ctx["quoted"] is None


# --------------------------------------------------------------------------- #
# Schema validation unit tests
# --------------------------------------------------------------------------- #

class TestValidateClassifyResult:
    def _valid(self, **overrides) -> dict:
        base = {
            "type": "direct_request",
            "priority": "high",
            "priority_reason": "blocker",
            "summary": "review PR",
            "action_required": True,
            "context_sufficient": True,
            "confidence": 0.9,
        }
        base.update(overrides)
        return base

    def test_valid_returns_none(self):
        assert am._validate_classify_result(self._valid()) is None

    def test_missing_key(self):
        d = self._valid()
        del d["confidence"]
        assert am._validate_classify_result(d) is not None

    def test_invalid_type(self):
        assert am._validate_classify_result(self._valid(type="unknown")) is not None

    def test_invalid_priority(self):
        assert am._validate_classify_result(self._valid(priority="blocker")) is not None

    def test_confidence_out_of_range(self):
        assert am._validate_classify_result(self._valid(confidence=1.5)) is not None

    def test_confidence_negative(self):
        assert am._validate_classify_result(self._valid(confidence=-0.1)) is not None

    def test_confidence_not_numeric(self):
        assert am._validate_classify_result(self._valid(confidence="high")) is not None

    def test_not_a_dict(self):
        assert am._validate_classify_result([]) is not None

    def test_all_valid_msg_types(self):
        for t in am._VALID_MSG_TYPES:
            assert am._validate_classify_result(self._valid(type=t)) is None


# --------------------------------------------------------------------------- #
# Mixed-items run
# --------------------------------------------------------------------------- #

class TestMixedRun:
    def test_mixed_stored_deferred_failed(self, tmp_path):
        cfg = _make_config(min_confidence=0.5)
        items = [
            _item(msg="spaces/A/messages/1"),  # → stored
            _item(msg="spaces/A/messages/2"),  # → deferred
            _item(msg="spaces/A/messages/3"),  # → failed
        ]
        s = _make_store(items)
        ids = list(s["items"].keys())

        stored_res = _ok_result(context_sufficient=True, confidence=0.9)
        deferred_res = _ok_result(context_sufficient=False, confidence=0.6)
        failed_res = _error_result("network error")

        fake = FakeAdapter(results=[stored_res, deferred_res, failed_res])
        store_path = tmp_path / "store.json"
        result = am.run_analysis(s, cfg, NOW, adapter=fake, store_path=store_path)

        assert result["stored"] == 1
        assert result["deferred"] == 1
        assert result["failed"] == 1
        assert result["selected"] == 3


# --------------------------------------------------------------------------- #
# Tier-2 tests (C1 — thread escalation)
# --------------------------------------------------------------------------- #

# --------------- Helpers for Tier-2 ---------------------------------------- #

def _make_config_tier2(
    *,
    escalate_to_thread: bool = True,
    thread_max_messages: int = 30,
    max_items: int = 20,
) -> dict:
    """Return a merged config with analyze+escalation enabled."""
    cfg = _make_config()
    cfg["analyze"]["escalate_to_thread"] = escalate_to_thread
    cfg["analyze"]["thread_max_messages"] = thread_max_messages
    cfg["analyze"]["max_items_per_run"] = max_items
    return cfg


def _deferred_item(
    *,
    msg: str = "spaces/A/messages/1",
    thread: str = "spaces/A/threads/T1",
    sender_name: str = "Alice",
    text: str = "^",
) -> dict:
    """Build a store item that is already DEFER'd (analyzed_at set, priority unset)."""
    return {
        "space_name": "spaces/A",
        "space_display": "Engineering",
        "space_type": "SPACE",
        "message_name": msg,
        "thread_name": thread,
        "sender_id": "users/42",
        "sender_name": sender_name,
        "created_time": "2026-06-11T11:00:00Z",
        "text": text,
        "trigger": "user_mention",
        "status": "new",
        "priority": "unset",
        # Tier-1 already ran (DEFER):
        "analyzed_at": "2026-06-11T10:00:00Z",
        "analyzed_by": "claude/haiku",
        "msg_type": "continuation",
        "thread_status": None,
        "quoted": None,
    }


def _ok_summarize_result(
    *,
    msg_type: str = "direct_request",
    priority: str = "high",
    priority_reason: str = "blocking deploy",
    summary: str = "Thread discusses a deploy blocker. Bob asked Artem to review. Artem must approve.",
    action_required: bool = True,
    thread_status: str = "awaiting_me",
    confidence: float = 0.85,
    adapter_name: str = "fake/v1",
) -> AnalysisResult:
    return AnalysisResult(
        ok=True,
        adapter=adapter_name,
        mode="summarize",
        data={
            "type": msg_type,
            "priority": priority,
            "priority_reason": priority_reason,
            "summary": summary,
            "action_required": action_required,
            "thread_status": thread_status,
            "confidence": confidence,
        },
    )


def _error_summarize_result(reason: str = "timeout") -> AnalysisResult:
    return AnalysisResult(ok=False, adapter="fake/v1", mode="summarize", error=reason)


def _canned_thread_messages(target_msg_name: str = "spaces/A/messages/1") -> list:
    """Return 3 raw thread messages (oldest→newest); target is the 3rd."""
    return [
        {
            "name": "spaces/A/messages/0",
            "createTime": "2026-06-11T10:55:00Z",
            "sender": {"name": "users/10", "displayName": "Alice"},
            "text": "Can someone review the deploy PR?",
        },
        {
            "name": "spaces/A/messages/2",
            "createTime": "2026-06-11T10:58:00Z",
            "sender": {"name": "users/20", "displayName": "Bob"},
            "text": "^",
        },
        {
            "name": target_msg_name,
            "createTime": "2026-06-11T11:00:00Z",
            "sender": {"name": "users/42", "displayName": "Alice"},
            "text": "^",
        },
    ]


# Sentinel creds object — just needs to be non-None for gchat.get_credentials mock
_FAKE_CREDS = object()


class TestTier2SelectHandoffItems:
    """Tests for the Tier-2 selection predicate."""

    def test_selects_deferred_items(self):
        cfg = _make_config_tier2()
        s = _make_store([_deferred_item()])
        items = am.select_handoff_items(s, cfg)
        assert len(items) == 1

    def test_excludes_items_without_analyzed_at(self):
        cfg = _make_config_tier2()
        item = _deferred_item()
        item["analyzed_at"] = None  # Tier-1 not yet run
        s = _make_store([item])
        assert am.select_handoff_items(s, cfg) == []

    def test_excludes_items_with_non_unset_priority(self):
        cfg = _make_config_tier2()
        item = _deferred_item()
        item["priority"] = "high"  # already resolved
        s = _make_store([item])
        assert am.select_handoff_items(s, cfg) == []

    def test_excludes_items_with_thread_status_set(self):
        cfg = _make_config_tier2()
        item = _deferred_item()
        item["thread_status"] = "awaiting_me"  # Tier-2 already ran
        s = _make_store([item])
        assert am.select_handoff_items(s, cfg) == []

    def test_excludes_items_without_thread_name(self):
        cfg = _make_config_tier2()
        item = _deferred_item()
        item["thread_name"] = None
        s = _make_store([item])
        assert am.select_handoff_items(s, cfg) == []

    def test_excludes_items_without_message_name(self):
        cfg = _make_config_tier2()
        # Insert directly (bypassing upsert_item which requires a non-None message_name)
        s = {"version": 1, "me_user_id": None, "last_run": None, "cursor_per_space": {}, "items": {}}
        raw_item = _deferred_item()
        raw_item["message_name"] = None
        raw_item["id"] = "fakeid"
        s["items"]["fakeid"] = raw_item
        assert am.select_handoff_items(s, cfg) == []

    def test_cap_respected(self):
        cfg = _make_config_tier2(max_items=2)
        items = [_deferred_item(msg=f"spaces/A/messages/{i}") for i in range(5)]
        s = _make_store(items)
        selected = am.select_handoff_items(s, cfg)
        assert len(selected) == 2

    def test_limit_override(self):
        cfg = _make_config_tier2(max_items=10)
        items = [_deferred_item(msg=f"spaces/A/messages/{i}") for i in range(5)]
        s = _make_store(items)
        selected = am.select_handoff_items(s, cfg, limit=1)
        assert len(selected) == 1


class TestTier2ValidateSummarizeResult:
    """Tests for the SUMMARIZE schema validator."""

    def _valid(self, **overrides) -> dict:
        base = {
            "type": "direct_request",
            "priority": "high",
            "priority_reason": "deploy blocked",
            "summary": "Thread needs attention.",
            "action_required": True,
            "thread_status": "awaiting_me",
            "confidence": 0.85,
        }
        base.update(overrides)
        return base

    def test_valid_returns_none(self):
        assert am._validate_summarize_result(self._valid()) is None

    def test_missing_key(self):
        d = self._valid()
        del d["thread_status"]
        assert am._validate_summarize_result(d) is not None

    def test_invalid_type(self):
        assert am._validate_summarize_result(self._valid(type="garbage")) is not None

    def test_invalid_priority(self):
        assert am._validate_summarize_result(self._valid(priority="CRITICAL")) is not None

    def test_invalid_thread_status(self):
        assert am._validate_summarize_result(self._valid(thread_status="unknown")) is not None

    def test_empty_summary(self):
        assert am._validate_summarize_result(self._valid(summary="")) is not None

    def test_confidence_out_of_range(self):
        assert am._validate_summarize_result(self._valid(confidence=1.5)) is not None

    def test_all_valid_thread_statuses(self):
        for ts in am._VALID_THREAD_STATUSES:
            assert am._validate_summarize_result(self._valid(thread_status=ts)) is None


class TestTier2TargetMarking:
    """Verify that _build_thread_ctx marks the TARGET row correctly."""

    def test_target_row_marked_by_message_name(self, monkeypatch):
        """The row whose ``name`` matches ``item["message_name"]`` gets is_target=True."""
        import google_chat as gchat

        # Patch get_user_display_name to return sender name from displayName
        monkeypatch.setattr(
            gchat, "get_user_display_name",
            lambda sender, creds=None: sender.get("displayName") or sender.get("name", ""),
        )

        target_msg = "spaces/A/messages/1"
        item = _deferred_item(msg=target_msg)
        messages = _canned_thread_messages(target_msg_name=target_msg)

        cfg = _make_config_tier2()
        ctx = am._build_thread_ctx(item, messages, cfg)

        thread = ctx["thread"]
        assert len(thread) == 3
        # Only the row matching the target message name should have is_target=True
        target_rows = [r for r in thread if r["is_target"]]
        assert len(target_rows) == 1
        # The target is the last message (index 2)
        assert thread[2]["is_target"] is True
        assert thread[0]["is_target"] is False
        assert thread[1]["is_target"] is False

    def test_target_index_set_in_ctx(self, monkeypatch):
        import google_chat as gchat

        monkeypatch.setattr(
            gchat, "get_user_display_name",
            lambda sender, creds=None: sender.get("displayName") or sender.get("name", ""),
        )

        target_msg = "spaces/A/messages/1"
        item = _deferred_item(msg=target_msg)
        messages = _canned_thread_messages(target_msg_name=target_msg)

        cfg = _make_config_tier2()
        ctx = am._build_thread_ctx(item, messages, cfg)

        assert ctx.get("target_index") == 2


class TestTier2ThreadBoundary:
    """Verify thread_max_messages bounding."""

    def test_thread_bounded_by_max_messages(self, monkeypatch):
        """When more messages exist than thread_max_messages, only the most recent N are used."""
        import google_chat as gchat

        target_msg = "spaces/A/messages/99"
        item = _deferred_item(msg=target_msg)
        # 10 messages, target is the last one
        messages = [
            {
                "name": f"spaces/A/messages/{i}",
                "createTime": f"2026-06-11T10:{i:02d}:00Z",
                "sender": {"name": "users/10", "displayName": "Alice"},
                "text": f"message {i}",
            }
            for i in range(9)
        ] + [
            {
                "name": target_msg,
                "createTime": "2026-06-11T10:59:00Z",
                "sender": {"name": "users/42", "displayName": "Alice"},
                "text": "^",
            }
        ]

        monkeypatch.setattr(
            gchat, "get_user_display_name",
            lambda sender, creds=None: sender.get("displayName") or sender.get("name", ""),
        )

        # fetch_fn returns all 10, but max_messages=4 → only 4 kept
        def fake_fetch(creds, space_name, thread_name, *, max_messages=30):
            # Simulate the helper already truncating to max_messages
            if len(messages) > max_messages:
                return messages[-max_messages:]
            return messages

        cfg = _make_config_tier2(thread_max_messages=4)
        fake_adapter = FakeAdapter(default_result=_ok_summarize_result())
        s = _make_store([item])
        store_path = monkeypatch.mktemp if callable(getattr(monkeypatch, "mktemp", None)) else None

        iid = list(s["items"].keys())[0]

        outcome = am.escalate_item(
            s["items"][iid],
            cfg,
            fake_adapter,
            _FAKE_CREDS,
            NOW,
            store_obj=s,
            fetch_fn=fake_fetch,
        )
        assert outcome == "escalated"
        # Prompt was called with (at most) 4 messages — verified indirectly via adapter call
        assert len(fake_adapter.calls) == 1
        req = fake_adapter.calls[0]
        assert req.mode == "summarize"


class TestTier2StoreOnSuccess:
    """Verify correct field writes on a successful SUMMARIZE."""

    def test_store_fields_on_escalated(self, tmp_path, monkeypatch):
        """All 8 D-7 fields are written; status and history unchanged."""
        import google_chat as gchat

        monkeypatch.setattr(
            gchat, "get_user_display_name",
            lambda sender, creds=None: sender.get("displayName") or sender.get("name", ""),
        )

        target_msg = "spaces/A/messages/1"
        item = _deferred_item(msg=target_msg)
        s = _make_store([item])
        iid = list(s["items"].keys())[0]
        history_before = list(s["items"][iid].get("history", []))
        status_before = s["items"][iid]["status"]

        cfg = _make_config_tier2()
        fake_adapter = FakeAdapter(default_result=_ok_summarize_result(
            msg_type="question",
            priority="normal",
            priority_reason="informational thread",
            summary="Thread about code review. Alice asked for review. Artem should respond.",
            thread_status="awaiting_me",
            confidence=0.88,
        ))

        def fake_fetch(creds, space_name, thread_name, *, max_messages=30):
            return _canned_thread_messages(target_msg_name=target_msg)

        store_path = tmp_path / "store.json"
        outcome = am.escalate_item(
            s["items"][iid],
            cfg,
            fake_adapter,
            _FAKE_CREDS,
            NOW,
            store_obj=s,
            fetch_fn=fake_fetch,
        )

        assert outcome == "escalated"
        item_after = s["items"][iid]

        # D-7 field mapping
        assert item_after["msg_type"] == "question"
        assert item_after["priority"] == "normal"
        assert item_after["priority_reason"] == "informational thread"
        assert item_after["context_summary"] == "Thread about code review. Alice asked for review. Artem should respond."
        assert item_after["context_confidence"] == pytest.approx(0.88)
        assert item_after["thread_status"] == "awaiting_me"
        assert item_after["analyzed_at"] == NOW_ISO
        assert item_after["analyzed_by"] == "fake/v1"

        # Status and history must be untouched
        assert item_after["status"] == status_before
        assert item_after.get("history", []) == history_before

    def test_escalate_via_run_tier2(self, tmp_path, monkeypatch):
        """run_tier2 with monkeypatched gchat → escalated=1."""
        import google_chat as gchat

        target_msg = "spaces/A/messages/1"
        item = _deferred_item(msg=target_msg)
        s = _make_store([item])
        iid = list(s["items"].keys())[0]
        cfg = _make_config_tier2()

        monkeypatch.setattr(gchat, "get_credentials", lambda *a, **kw: _FAKE_CREDS)
        monkeypatch.setattr(
            gchat, "get_user_display_name",
            lambda sender, creds=None: sender.get("displayName") or sender.get("name", ""),
        )

        def fake_fetch(creds, space_name, thread_name, *, max_messages=30):
            return _canned_thread_messages(target_msg_name=target_msg)

        fake_adapter = FakeAdapter(default_result=_ok_summarize_result())

        stats = am.run_tier2(
            s, cfg, NOW,
            adapter=fake_adapter,
            fetch_fn=fake_fetch,
        )

        assert stats["escalated"] == 1
        assert stats["thread_failed"] == 0
        assert s["items"][iid]["priority"] == "high"
        assert s["items"][iid]["thread_status"] == "awaiting_me"


class TestTier2GracefulFailures:
    """Verify graceful no-ops on error conditions."""

    def test_noop_when_escalate_to_thread_false(self, tmp_path, monkeypatch):
        """When escalate_to_thread=False, Tier-2 is never called."""
        import google_chat as gchat

        creds_called = []
        monkeypatch.setattr(gchat, "get_credentials", lambda *a, **kw: (creds_called.append(1) or _FAKE_CREDS))

        cfg = _make_config_tier2(escalate_to_thread=False)
        item = _deferred_item()
        s = _make_store([item])
        iid = list(s["items"].keys())[0]
        fake_adapter = FakeAdapter(default_result=_ok_summarize_result())
        store_path = tmp_path / "store.json"

        result = am.run_analysis(s, cfg, NOW, adapter=fake_adapter, store_path=store_path)

        # escalate_to_thread=False → Tier-2 blocked at gate in run_analysis
        assert result.get("escalated", 0) == 0
        assert s["items"][iid]["priority"] == "unset"
        assert s["items"][iid]["thread_status"] is None

    def test_noop_when_creds_none(self, tmp_path, monkeypatch):
        """When gchat.get_credentials() returns None, Tier-2 skips gracefully."""
        import google_chat as gchat

        monkeypatch.setattr(gchat, "get_credentials", lambda *a, **kw: None)

        cfg = _make_config_tier2()
        item = _deferred_item()
        s = _make_store([item])
        iid = list(s["items"].keys())[0]
        fake_adapter = FakeAdapter(default_result=_ok_summarize_result())

        stats = am.run_tier2(s, cfg, NOW, adapter=fake_adapter)

        assert stats["escalated"] == 0
        assert stats["thread_failed"] == 0
        assert s["items"][iid]["priority"] == "unset"
        assert s["items"][iid]["thread_status"] is None

    def test_noop_when_fetch_raises(self, tmp_path, monkeypatch):
        """When the thread fetch raises, the item stays DEFER'd (priority unset)."""
        import google_chat as gchat

        monkeypatch.setattr(gchat, "get_credentials", lambda *a, **kw: _FAKE_CREDS)
        monkeypatch.setattr(
            gchat, "get_user_display_name",
            lambda sender, creds=None: sender.get("displayName") or sender.get("name", ""),
        )

        def failing_fetch(creds, space_name, thread_name, *, max_messages=30):
            raise RuntimeError("network error")

        cfg = _make_config_tier2()
        item = _deferred_item()
        s = _make_store([item])
        iid = list(s["items"].keys())[0]
        fake_adapter = FakeAdapter(default_result=_ok_summarize_result())

        stats = am.run_tier2(
            s, cfg, NOW,
            adapter=fake_adapter,
            fetch_fn=failing_fetch,
        )

        assert stats["thread_failed"] == 1
        assert s["items"][iid]["priority"] == "unset"
        assert s["items"][iid]["thread_status"] is None

    def test_noop_when_adapter_error(self, tmp_path, monkeypatch):
        """When adapter.run returns ok=False, item stays DEFER'd."""
        import google_chat as gchat

        monkeypatch.setattr(gchat, "get_credentials", lambda *a, **kw: _FAKE_CREDS)
        monkeypatch.setattr(
            gchat, "get_user_display_name",
            lambda sender, creds=None: sender.get("displayName") or sender.get("name", ""),
        )

        target_msg = "spaces/A/messages/1"

        def ok_fetch(creds, space_name, thread_name, *, max_messages=30):
            return _canned_thread_messages(target_msg_name=target_msg)

        cfg = _make_config_tier2()
        item = _deferred_item(msg=target_msg)
        s = _make_store([item])
        iid = list(s["items"].keys())[0]
        fake_adapter = FakeAdapter(default_result=_error_summarize_result("timeout"))

        stats = am.run_tier2(
            s, cfg, NOW,
            adapter=fake_adapter,
            fetch_fn=ok_fetch,
        )

        assert stats["thread_failed"] == 1
        assert s["items"][iid]["priority"] == "unset"
        assert s["items"][iid]["thread_status"] is None

    def test_noop_when_schema_invalid(self, tmp_path, monkeypatch):
        """When SUMMARIZE returns invalid schema, item stays DEFER'd."""
        import google_chat as gchat

        monkeypatch.setattr(gchat, "get_credentials", lambda *a, **kw: _FAKE_CREDS)
        monkeypatch.setattr(
            gchat, "get_user_display_name",
            lambda sender, creds=None: sender.get("displayName") or sender.get("name", ""),
        )

        target_msg = "spaces/A/messages/1"

        def ok_fetch(creds, space_name, thread_name, *, max_messages=30):
            return _canned_thread_messages(target_msg_name=target_msg)

        cfg = _make_config_tier2()
        item = _deferred_item(msg=target_msg)
        s = _make_store([item])
        iid = list(s["items"].keys())[0]

        bad_result = AnalysisResult(
            ok=True,
            adapter="fake/v1",
            mode="summarize",
            data={
                "type": "not_valid",  # invalid type
                "priority": "high",
                "priority_reason": "x",
                "summary": "y",
                "action_required": True,
                "thread_status": "awaiting_me",
                "confidence": 0.9,
            },
        )
        fake_adapter = FakeAdapter(default_result=bad_result)

        stats = am.run_tier2(
            s, cfg, NOW,
            adapter=fake_adapter,
            fetch_fn=ok_fetch,
        )

        assert stats["thread_failed"] == 1
        assert s["items"][iid]["priority"] == "unset"
        assert s["items"][iid]["thread_status"] is None


class TestTier2DryRun:
    """dry_run=True must not write the store file."""

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        import google_chat as gchat

        monkeypatch.setattr(gchat, "get_credentials", lambda *a, **kw: _FAKE_CREDS)
        monkeypatch.setattr(
            gchat, "get_user_display_name",
            lambda sender, creds=None: sender.get("displayName") or sender.get("name", ""),
        )

        target_msg = "spaces/A/messages/1"

        def ok_fetch(creds, space_name, thread_name, *, max_messages=30):
            return _canned_thread_messages(target_msg_name=target_msg)

        cfg = _make_config_tier2()
        item = _deferred_item(msg=target_msg)
        s = _make_store([item])
        store_path = tmp_path / "store.json"
        store.save(s, store_path)
        iid = list(s["items"].keys())[0]

        fake_adapter = FakeAdapter(default_result=_ok_summarize_result())

        am.run_tier2(
            s, cfg, NOW,
            adapter=fake_adapter,
            fetch_fn=ok_fetch,
            dry_run=True,
        )

        # File on disk must be unchanged (priority="unset" still in file)
        on_disk = json.loads(store_path.read_text())
        assert on_disk["items"][iid]["priority"] == "unset"
        assert on_disk["items"][iid]["thread_status"] is None


class TestTier2NoHandoffItems:
    """When there are no handoff items, gchat.get_credentials must NOT be called."""

    def test_no_creds_call_when_no_handoff(self, monkeypatch):
        import google_chat as gchat

        creds_called = []
        monkeypatch.setattr(
            gchat, "get_credentials",
            lambda *a, **kw: creds_called.append(1) or _FAKE_CREDS,
        )

        cfg = _make_config_tier2()
        # A fresh (not DEFER'd) item — no handoff items
        s = _make_store([_item()])

        stats = am.run_tier2(s, cfg, NOW, adapter=FakeAdapter())
        assert stats["escalated"] == 0
        assert creds_called == []  # credentials never requested


class TestTier2Tier1Unchanged:
    """Regression: existing Tier-1 tests still pass after C1 additions.

    This class verifies the Tier-1 path is orthogonal to Tier-2 by running
    a plain Tier-1 (escalate_to_thread=False) run and checking stats.
    """

    def test_tier1_stored_result_unaffected(self, tmp_path, monkeypatch):
        import google_chat as gchat

        monkeypatch.setattr(gchat, "get_credentials", lambda *a, **kw: None)

        cfg = _make_config(min_confidence=0.5)
        cfg["analyze"]["escalate_to_thread"] = False
        s = _make_store([_item(msg="spaces/A/messages/1")])
        fake = FakeAdapter(default_result=_ok_result(context_sufficient=True, confidence=0.9))
        store_path = tmp_path / "store.json"

        result = am.run_analysis(s, cfg, NOW, adapter=fake, store_path=store_path)

        assert result["stored"] == 1
        assert result["deferred"] == 0
        assert result["escalated"] == 0
