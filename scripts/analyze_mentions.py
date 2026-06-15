#!/usr/bin/env python3
"""Headless AI analysis stage for the Chat Triage Assistant (Wave B, task B1).

Runnable as ``uv run python scripts/analyze_mentions.py``.  Reads existing
store items and classifies them via the configured LLM adapter.  Makes ZERO
Google Chat network calls — it operates only on stored text.

Pipeline:
  1. GATE        — load config; if ``analyze.enabled`` is False → log + exit 0
                   immediately, store untouched.  Also skip when
                   ``config.is_active`` is False (global kill-switch / mute).
  2. RUN-LOCK    — non-blocking ``flock`` on ``analyze.lock``; a second
                   concurrent run logs "another run in progress" and exits 0.
  3. ADAPTER     — iterate ``analyze.adapters.order``; pick the first whose
                   ``available()`` probe returns True.  Fully injectable via the
                   ``adapter=`` kwarg so tests can pass a fake.
  4. SELECT      — items where ``status == "new"`` AND ``priority == "unset"``
                   AND ``analyzed_at is None``.  Capped at
                   ``analyze.max_items_per_run`` (overridable via ``--limit``).
                   This is the idempotency predicate: once ``analyzed_at`` is
                   set the item is never re-selected.
  5. CLASSIFY    — per item: build ctx → ``build_classify_prompt`` →
                   ``adapter.run`` → validate schema → decide STORE / DEFER /
                   TRANSIENT.
  6. PERSIST     — ``store.save`` once at the end (atomic); no-op on --dry-run.

Wave-C handoff contract
-----------------------
Tier-1 (this script) classifies every selected item.  When context_sufficient
is False **or** confidence < min_confidence_to_store, this script records that
Tier-1 ran (``analyzed_at``, ``analyzed_by``, ``msg_type``) but leaves
``priority == "unset"`` and ``context_summary / thread_status == None``.

Wave-C (thread escalation, task C1) MUST use this predicate to find items that
need a thread summary::

    analyzed_at is not None
    AND priority == "unset"
    AND thread_status is None

This ensures re-runs of B1 do NOT re-call the LLM (``analyzed_at`` is
already set), while Wave-C can identify exactly which items need escalation.

TRANSIENT failures (adapter error / schema invalid) leave ``analyzed_at``
unset so the item is retried on the next run.

Security: message text is untrusted.  ``build_classify_prompt`` sanitizes it
before embedding into the prompt.  This script never posts to Chat.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ``scripts/`` is not an installed package; make sibling modules importable.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT))

import config  # noqa: E402  (scripts/config.py)
import store   # noqa: E402  (scripts/store.py)
from analysis_adapters import (  # noqa: E402
    AnalysisAdapter,
    AnalysisRequest,
    AnalysisResult,
    ClaudeAdapter,
    CodexAdapter,
    GeminiAdapter,
)
from analysis_prompts import (  # noqa: E402
    build_classify_prompt,
    build_reply_suggestions_prompt,
    build_summarize_prompt,
)


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
#: Lock + log filenames (under the triage base dir).
RUN_LOCK_NAME = "analyze.lock"
LOGS_DIRNAME = "logs"

#: The 8 message-type values the classify prompt may return.
_VALID_MSG_TYPES = frozenset({
    "direct_request",
    "question",
    "decision_needed",
    "status_update",
    "fyi",
    "social",
    "continuation",
    "unclear",
})

#: Valid priority values.
_VALID_PRIORITIES = frozenset({"high", "normal", "low"})

#: Valid thread_status values returned by SUMMARIZE.
_VALID_THREAD_STATUSES = frozenset({
    "awaiting_me",
    "awaiting_others",
    "resolved",
    "fyi",
})

#: Known deictic/elliptical markers (case-insensitive).
_DEICTIC_WORDS = re.compile(
    r"\b(this|that|it|these|those)\b",
    re.IGNORECASE,
)
_DEICTIC_PHRASES = [
    "this one",
    "any update",
    "any updates",
    "^",
]


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def _now_dt(now=None) -> datetime:
    """Aware-UTC datetime for ``now`` (None → wall clock)."""
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        dt = now
    else:
        text = str(now).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_z(dt: datetime) -> str:
    """RFC3339 ``...Z`` second-precision string."""
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# Locking (POSIX flock) — mirror of collect_mentions pattern
# --------------------------------------------------------------------------- #
def _acquire_lock(path: Path, *, blocking: bool):
    """Acquire an exclusive flock on *path*.

    Returns the open file object holding the lock, or None when blocking is
    False and the lock is already held.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        fcntl.flock(fh.fileno(), flags)
    except OSError:
        fh.close()
        return None
    return fh


def _release_lock(fh) -> None:
    if fh is None:
        return
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


# --------------------------------------------------------------------------- #
# Logging (structured, secret-free) — mirror of collect_mentions pattern
# --------------------------------------------------------------------------- #
def _make_logger(base_dir: Path, now_dt: datetime):
    """Return ``log(event, **fields)`` appending JSON lines under ``logs/``.

    Best-effort: a logging failure must not abort an analysis run.
    Never logs token values, secrets, or message ``text``.
    """
    logs_dir = base_dir / LOGS_DIRNAME
    log_path = logs_dir / f"analyze-{now_dt.strftime('%Y-%m-%d')}.log"

    def log(event: str, **fields) -> None:
        record = {"ts": _iso_z(_now_dt()), "event": event}
        record.update(fields)
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    return log


# --------------------------------------------------------------------------- #
# Adapter router
# --------------------------------------------------------------------------- #
def _build_adapter_registry() -> dict:
    """Return the built-in adapter registry (one instance per known name).

    All known adapters are registered so an operator can opt into fallback via
    ``analyze.adapters.order`` (e.g. ``["claude", "codex", "gemini"]``); each
    adapter's ``available()`` gates whether its CLI is actually present. The
    default order is ``["claude"]``, so codex/gemini stay dormant until enabled.
    """
    return {
        "claude": ClaudeAdapter(),
        "codex": CodexAdapter(),
        "gemini": GeminiAdapter(),
    }


def _select_adapter(cfg: dict, registry: Optional[dict] = None) -> Optional[AnalysisAdapter]:
    """Iterate ``analyze.adapters.order`` and return the first available adapter.

    Returns None if no adapter in the order list is available.
    """
    reg = registry if registry is not None else _build_adapter_registry()
    order = cfg.get("analyze", {}).get("adapters", {}).get("order", ["claude"])
    for name in order:
        adapter = reg.get(name)
        if adapter is not None and adapter.available():
            return adapter
    return None


# --------------------------------------------------------------------------- #
# Tier-0 helpers (pure, tested)
# --------------------------------------------------------------------------- #
def extract_quoted_text(item: dict) -> Optional[str]:
    """Extract the quoted parent message text from an item dict.

    ``item["quoted"]`` is either None or a dict like::

        {"name": "...", "type": "...", "text": "...", "sender": "..."}

    Returns the ``text`` field string, or None when not present / not a string.
    """
    quoted = item.get("quoted")
    if not quoted or not isinstance(quoted, dict):
        return None
    text = quoted.get("text")
    return str(text) if text else None


def is_deictic(text: str) -> bool:
    """Return True when *text* contains deictic/elliptical markers.

    Markers detected:
    - Very short messages (<=6 non-whitespace chars).
    - Leading ``and``, ``also``, or ``+`` (continuation markers).
    - The caret shorthand ``^``.
    - Phrases like "this one", "any update(s)".
    - Bare deictic pronouns: this, that, it, these, those.

    This is a SIGNAL used for logging and future Wave-C routing.  In B1 it
    never blocks the LLM call.
    """
    if not text:
        return False
    stripped = text.strip()
    # Very short message.
    if len(stripped.replace(" ", "")) <= 6:
        return True
    lower = stripped.lower()
    # Leading continuation markers.
    if re.match(r"^(and|also)\b", lower):
        return True
    # Leading + with optional whitespace (e.g. "+ what about logs?")
    if re.match(r"^\+\s", stripped):
        return True
    # Caret shorthand.
    if stripped.startswith("^"):
        return True
    # Known deictic phrases.
    for phrase in _DEICTIC_PHRASES:
        if phrase in lower:
            return True
    # Bare deictic pronoun (word-boundary).
    if _DEICTIC_WORDS.search(stripped):
        return True
    return False


def build_ctx(item: dict, cfg: dict) -> dict:
    """Assemble the classify context dict from a store item and merged config.

    Config keys used:
      ``me_display_name`` — name of the person being triaged for.
      ``me_role``         — role/title (optional; defaults to "engineering lead"
                            when absent so the prompt is always meaningful).

    All untrusted fields (text, sender_name, quoted) are passed raw;
    ``build_classify_prompt`` sanitizes them.
    """
    me_name = cfg.get("me_display_name") or ""
    me_role = cfg.get("me_role") or "engineering lead"
    return {
        "me_name": me_name,
        "me_role": me_role,
        "space_display": item.get("space_display") or "",
        "space_type": item.get("space_type") or "",
        "sender_name": item.get("sender_name") or "",
        "trigger": item.get("trigger") or "",
        "text": item.get("text") or "",
        "quoted": extract_quoted_text(item),
        "summary_language": (cfg.get("analyze") or {}).get("summary_language") or "",
    }


# --------------------------------------------------------------------------- #
# Schema validation
# --------------------------------------------------------------------------- #
def _validate_classify_result(data: dict) -> Optional[str]:
    """Return None when data is schema-valid, else a short error description.

    Required keys: type, priority, priority_reason, summary,
                   action_required, context_sufficient, confidence.
    """
    if not isinstance(data, dict):
        return "data is not a dict"
    required = {
        "type", "priority", "priority_reason", "summary",
        "action_required", "context_sufficient", "confidence",
    }
    missing = required - data.keys()
    if missing:
        return f"missing keys: {sorted(missing)}"
    if data["type"] not in _VALID_MSG_TYPES:
        return f"invalid type {data['type']!r}"
    if data["priority"] not in _VALID_PRIORITIES:
        return f"invalid priority {data['priority']!r}"
    try:
        conf = float(data["confidence"])
    except (TypeError, ValueError):
        return "confidence is not numeric"
    if not (0.0 <= conf <= 1.0):
        return f"confidence {conf!r} out of [0, 1]"
    return None


# --------------------------------------------------------------------------- #
# Selection predicate (Tier-1 work-list)
# --------------------------------------------------------------------------- #
def select_items(store_obj: dict, cfg: dict, limit: Optional[int] = None) -> list:
    """Return items eligible for Tier-1 analysis.

    Predicate: ``status == "new"`` AND ``priority == "unset"`` AND
    ``analyzed_at is None``.

    Capped at ``analyze.max_items_per_run`` (or *limit* if provided).
    """
    cap = limit if limit is not None else cfg.get("analyze", {}).get("max_items_per_run", 20)
    result = []
    for item in store_obj.get("items", {}).values():
        if len(result) >= cap:
            break
        if (
            item.get("status") == "new"
            and item.get("priority") == "unset"
            and item.get("analyzed_at") is None
        ):
            result.append(item)
    return result


# --------------------------------------------------------------------------- #
# Per-item analysis (Tier-1)
# --------------------------------------------------------------------------- #
def analyze_item(
    item: dict,
    cfg: dict,
    adapter: AnalysisAdapter,
    now: datetime,
    *,
    store_obj: dict,
    log=None,
    dry_run: bool = False,
) -> str:
    """Classify a single item.  Returns one of "stored", "deferred", "failed".

    Mutates *item* directly (caller holds the store object in memory; caller
    calls ``store.save`` once at the end).  In ``dry_run`` mode prints proposed
    mutations instead of applying them.

    Decision logic (the B1 ↔ C1 composition contract):
      TRANSIENT   — ``res.ok`` is False OR schema invalid: write nothing;
                    ``analyzed_at`` stays None → item retried next run.
      STORE       — ``res.ok`` AND valid AND ``context_sufficient`` AND
                    ``confidence >= min_confidence_to_store``: write all 7
                    analysis fields.
      DEFER       — ``res.ok`` AND valid AND (not ``context_sufficient`` OR
                    ``confidence < min_confidence_to_store``): write only
                    ``analyzed_at``, ``analyzed_by``, ``msg_type`` — leave
                    ``priority == "unset"``, ``context_summary/thread_status``
                    as None so Wave-C escalation picks it up.
    """
    def _log(event, **kw):
        if log:
            log(event, item_id=item.get("id"), **kw)

    analyze_cfg = cfg.get("analyze", {})
    min_conf = analyze_cfg.get("min_confidence_to_store", 0.5)
    model = analyze_cfg.get("adapters", {}).get("claude", {}).get(
        "model", "claude-haiku-4-5-20251001"
    )
    timeout = analyze_cfg.get("timeout_seconds", 60)

    ctx = build_ctx(item, cfg)
    deictic = is_deictic(item.get("text") or "")
    if deictic:
        _log("deictic-signal", text_snippet=(item.get("text") or "")[:80])

    prompt = build_classify_prompt(ctx)
    req = AnalysisRequest(
        mode="classify",
        prompt=prompt,
        model=model,
        timeout_seconds=timeout,
    )

    res: AnalysisResult = adapter.run(req)

    # --- TRANSIENT path ---------------------------------------------------- #
    if not res.ok:
        _log("transient-error", adapter=res.adapter, error=res.error)
        return "failed"

    schema_error = _validate_classify_result(res.data or {})
    if schema_error:
        _log("transient-schema-error", adapter=res.adapter, error=schema_error)
        return "failed"

    data = res.data
    now_iso = _iso_z(now)
    msg_type = data["type"]
    context_sufficient = bool(data.get("context_sufficient"))
    confidence = float(data["confidence"])

    if context_sufficient and confidence >= min_conf:
        # --- STORE path ----------------------------------------------------- #
        fields = {
            "msg_type": msg_type,
            "priority": data["priority"],
            "priority_reason": data.get("priority_reason"),
            "context_summary": data.get("summary"),
            "context_confidence": confidence,
            "analyzed_at": now_iso,
            "analyzed_by": res.adapter,
        }
        if dry_run:
            print(f"[dry-run] STORE item={item.get('id')} fields={json.dumps(fields)}")
        else:
            store.set_fields(store_obj, item["id"], **fields)
        _log(
            "classify-stored",
            adapter=res.adapter,
            msg_type=msg_type,
            priority=data["priority"],
            confidence=confidence,
        )
        return "stored"
    else:
        # --- DEFER path (Wave-C handoff) ------------------------------------- #
        # Only minimal fields set — priority stays "unset", context_summary and
        # thread_status stay None.  Wave-C selects via:
        #   analyzed_at is not None AND priority == "unset" AND thread_status is None
        fields = {
            "msg_type": msg_type,
            "analyzed_at": now_iso,
            "analyzed_by": res.adapter,
        }
        if dry_run:
            print(
                f"[dry-run] DEFER item={item.get('id')} "
                f"context_sufficient={context_sufficient} confidence={confidence:.3f} "
                f"fields={json.dumps(fields)}"
            )
        else:
            store.set_fields(store_obj, item["id"], **fields)
        _log(
            "classify-deferred",
            adapter=res.adapter,
            msg_type=msg_type,
            confidence=confidence,
            context_sufficient=context_sufficient,
        )
        return "deferred"


# --------------------------------------------------------------------------- #
# Tier-2: thread escalation helpers
# --------------------------------------------------------------------------- #

def select_handoff_items(store_obj: dict, cfg: dict, limit: Optional[int] = None) -> list:
    """Return items eligible for Tier-2 thread escalation.

    Predicate (Wave-C selection contract):
      - ``analyzed_at`` is not None  (Tier-1 ran)
      - ``priority == "unset"``       (DEFER'd, not STORE'd)
      - ``thread_status is None``     (Tier-2 not yet run)
      - ``thread_name`` is present    (thread known)
      - ``message_name`` is present   (needed to mark TARGET row)

    Capped at ``analyze.max_items_per_run`` (or *limit* if provided).
    """
    cap = limit if limit is not None else cfg.get("analyze", {}).get("max_items_per_run", 20)
    result = []
    for item in store_obj.get("items", {}).values():
        if len(result) >= cap:
            break
        if (
            item.get("analyzed_at") is not None
            and item.get("priority") == "unset"
            and item.get("thread_status") is None
            and item.get("thread_name")
            and item.get("message_name")
        ):
            result.append(item)
    return result


def _validate_summarize_result(data: dict) -> Optional[str]:
    """Return None when SUMMARIZE data is schema-valid, else a short error string.

    Required keys: type, priority, priority_reason, summary,
                   action_required, thread_status, confidence.
    """
    if not isinstance(data, dict):
        return "data is not a dict"
    required = {
        "type", "priority", "priority_reason", "summary",
        "action_required", "thread_status", "confidence",
    }
    missing = required - data.keys()
    if missing:
        return f"missing keys: {sorted(missing)}"
    if data["type"] not in _VALID_MSG_TYPES:
        return f"invalid type {data['type']!r}"
    if data["priority"] not in _VALID_PRIORITIES:
        return f"invalid priority {data['priority']!r}"
    if data["thread_status"] not in _VALID_THREAD_STATUSES:
        return f"invalid thread_status {data['thread_status']!r}"
    if not data.get("summary"):
        return "summary is empty"
    try:
        conf = float(data["confidence"])
    except (TypeError, ValueError):
        return "confidence is not numeric"
    if not (0.0 <= conf <= 1.0):
        return f"confidence {conf!r} out of [0, 1]"
    return None


def _build_thread_ctx(
    item: dict,
    messages: list,
    cfg: dict,
) -> dict:
    """Build the SUMMARIZE context dict from a store item + raw thread messages.

    Resolves sender display names via the gchat module-level cache (best-effort;
    falls back to sender_id or raw sender.name).  Marks the TARGET row by
    comparing each message's ``name`` field to ``item["message_name"]``.

    Args:
        item:     The store item being escalated.
        messages: Raw message dicts from ``_list_thread_messages_sync``,
                  already in chronological order (oldest first).
        cfg:      Merged config dict.

    Returns:
        ctx dict ready for ``build_summarize_prompt``.
    """
    # Import gchat lazily so the module can still be tested without a real
    # token — tests monkeypatch the fetch helper, not the import.
    import google_chat as gchat  # noqa: PLC0415

    me_name = cfg.get("me_display_name") or ""
    me_role = cfg.get("me_role") or "engineering lead"
    space_display = item.get("space_display") or ""
    space_type = item.get("space_type") or ""
    target_message_name = item.get("message_name")

    thread_rows = []
    target_index: Optional[int] = None

    for idx, msg in enumerate(messages):
        # Timestamp label: use createTime or fall back to index
        created_time = msg.get("createTime") or msg.get("created_time") or ""
        t_label = created_time[:19].replace("T", " ") if created_time else str(idx)

        # Sender resolution: try the gchat cache, fallback to item's sender_name
        sender_obj = msg.get("sender") or {}
        display_name = gchat.get_user_display_name(sender_obj)
        # If cache missed (returned the raw id), try sender_name from the item
        # when this message is the target message.
        if (display_name == sender_obj.get("name", "") and
                msg.get("name") == target_message_name):
            display_name = item.get("sender_name") or display_name

        # Text extraction
        text = msg.get("text") or ""

        is_target = (msg.get("name") == target_message_name)
        if is_target and target_index is None:
            target_index = idx

        thread_rows.append({
            "t": t_label,
            "sender": display_name,
            "text": text,
            "is_target": is_target,
        })

    ctx: dict = {
        "me_name": me_name,
        "me_role": me_role,
        "space_display": space_display,
        "space_type": space_type,
        "thread": thread_rows,
        "summary_language": (cfg.get("analyze") or {}).get("summary_language") or "",
    }
    if target_index is not None:
        ctx["target_index"] = target_index
    return ctx


def escalate_item(
    item: dict,
    cfg: dict,
    adapter: AnalysisAdapter,
    creds,
    now: datetime,
    *,
    store_obj: dict,
    fetch_fn=None,
    log=None,
    dry_run: bool = False,
) -> str:
    """Run Tier-2 SUMMARIZE for a single DEFER'd item.

    Fetches the thread, calls SUMMARIZE, validates schema, and writes results
    via ``store.set_fields``.  Returns one of:
      ``"escalated"``  — SUMMARIZE succeeded; fields written.
      ``"failed"``     — fetch error, adapter error, or schema invalid; item
                         stays DEFER'd so next run retries.

    *fetch_fn* is injectable for tests (monkeypatches the thread fetch);
    defaults to ``google_chat._list_thread_messages_sync``.

    Security:
      - Only calls messages.list (READ-ONLY).
      - Untrusted thread text is sanitized by ``build_summarize_prompt``.
      - Adapter env is scrubbed by the adapter itself (ClaudeAdapter).
      - Never writes to Chat; never changes item status or history.
    """
    def _log(event, **kw):
        if log:
            log(event, item_id=item.get("id"), **kw)

    analyze_cfg = cfg.get("analyze", {})
    thread_max = analyze_cfg.get("thread_max_messages", 30)
    model = analyze_cfg.get("adapters", {}).get("claude", {}).get(
        "model", "claude-haiku-4-5-20251001"
    )
    timeout = analyze_cfg.get("timeout_seconds", 60)

    space_name = item.get("space_name") or ""
    thread_name = item.get("thread_name") or ""

    # --- Fetch thread messages --------------------------------------------- #
    if fetch_fn is None:
        import google_chat as gchat  # noqa: PLC0415
        fetch_fn = gchat._list_thread_messages_sync

    try:
        messages = fetch_fn(creds, space_name, thread_name, max_messages=thread_max)
    except Exception as exc:  # noqa: BLE001
        _log("tier2-fetch-error", error=str(exc))
        return "failed"

    if not messages:
        _log("tier2-empty-thread")
        return "failed"

    # --- Build context + prompt -------------------------------------------- #
    ctx = _build_thread_ctx(item, messages, cfg)
    prompt = build_summarize_prompt(ctx)
    req = AnalysisRequest(
        mode="summarize",
        prompt=prompt,
        model=model,
        timeout_seconds=timeout,
    )

    # --- Call adapter -------------------------------------------------------- #
    res: AnalysisResult = adapter.run(req)

    if not res.ok:
        _log("tier2-adapter-error", adapter=res.adapter, error=res.error)
        return "failed"

    # --- Schema validation -------------------------------------------------- #
    schema_error = _validate_summarize_result(res.data or {})
    if schema_error:
        _log("tier2-schema-error", adapter=res.adapter, error=schema_error)
        return "failed"

    data = res.data
    now_iso = _iso_z(now)

    # --- Store via set_fields (D-7) — NO status change, NO history ----------- #
    fields = {
        "msg_type": data["type"],
        "priority": data["priority"],
        "priority_reason": data.get("priority_reason"),
        "context_summary": data.get("summary"),
        "context_confidence": float(data["confidence"]),
        "thread_status": data["thread_status"],
        "analyzed_at": now_iso,
        "analyzed_by": res.adapter,
    }
    if dry_run:
        print(
            f"[dry-run] TIER2-ESCALATE item={item.get('id')} "
            f"fields={_iso_z(now)}"
        )
    else:
        store.set_fields(store_obj, item["id"], **fields)

    _log(
        "tier2-escalated",
        adapter=res.adapter,
        msg_type=data["type"],
        priority=data["priority"],
        thread_status=data["thread_status"],
        confidence=float(data["confidence"]),
    )
    return "escalated"


def run_tier2(
    store_obj: dict,
    cfg: dict,
    now: datetime,
    *,
    adapter: Optional[AnalysisAdapter] = None,
    fetch_fn=None,
    dry_run: bool = False,
    limit: Optional[int] = None,
    log=None,
) -> dict:
    """Run one Tier-2 (thread escalation) pass.

    Caller must have already confirmed ``escalate_to_thread=True`` and that an
    adapter is available.

    Returns stats dict: ``{escalated, thread_failed}``.

    Lazy credentials: only calls ``gchat.get_credentials()`` if there is at
    least one handoff item to process.  If credentials are unavailable (offline),
    skips Tier-2 gracefully and returns zeros.
    """
    def _log(event, **kw):
        if log:
            log(event, **kw)

    handoff_items = select_handoff_items(store_obj, cfg, limit=limit)
    if not handoff_items:
        return {"escalated": 0, "thread_failed": 0}

    # --- Lazy credentials (only needed if there's actual work) --------------- #
    import google_chat as gchat  # noqa: PLC0415

    creds = gchat.get_credentials()
    if creds is None:
        _log("tier2-skip", reason="no-credentials")
        return {"escalated": 0, "thread_failed": 0}

    # --- Per-item escalation ------------------------------------------------- #
    escalated = 0
    thread_failed = 0

    for item in handoff_items:
        outcome = escalate_item(
            item,
            cfg,
            adapter,
            creds,
            now,
            store_obj=store_obj,
            fetch_fn=fetch_fn,
            log=log,
            dry_run=dry_run,
        )
        if outcome == "escalated":
            escalated += 1
        else:
            thread_failed += 1

    # Persist is handled by run_analysis after this returns (single save).
    _log("tier2-run", escalated=escalated, thread_failed=thread_failed)
    return {"escalated": escalated, "thread_failed": thread_failed}


# --------------------------------------------------------------------------- #
# Tier-3: reply nudge (detect-no-reply → generate ready-to-send drafts)
# --------------------------------------------------------------------------- #
def _nudge_threshold_minutes(priority: str, nudge_cfg: dict) -> Optional[int]:
    """Minutes to wait before nudging this priority, or None if never nudged."""
    thresholds = nudge_cfg.get("thresholds_minutes") or {}
    val = thresholds.get((priority or "").lower())
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def select_nudge_items(store_obj: dict, cfg: dict, now: datetime) -> list:
    """Items eligible for a reply nudge.

    Predicate (all must hold):
      - status is OPEN (still actionable),
      - ``last_notified`` is set (I was already told about it),
      - not ``response_posted`` (no reply detected yet),
      - ``reply_nudged_at`` is None (one nudge per item),
      - the item's priority has a configured threshold (low → never), and
      - ``now - last_notified >= threshold(priority)``.

    Sorted high-priority first, oldest-notified first; capped at ``check_limit``.
    """
    nudge_cfg = (cfg.get("analyze") or {}).get("reply_nudge") or {}
    open_statuses = set(getattr(store, "OPEN_STATUSES", ()))
    out = []
    for item in store_obj.get("items", {}).values():
        if open_statuses and item.get("status") not in open_statuses:
            continue
        if item.get("response_posted"):
            continue
        if item.get("reply_nudged_at"):
            continue
        last_notified = item.get("last_notified")
        if not last_notified:
            continue
        minutes = _nudge_threshold_minutes(item.get("priority"), nudge_cfg)
        if minutes is None:
            continue
        notified_dt = _parse_store_iso(last_notified)
        if notified_dt is None:
            continue
        if (now - notified_dt).total_seconds() < minutes * 60:
            continue
        out.append(item)

    out.sort(key=lambda it: (
        {"high": 0, "normal": 1, "low": 2}.get((it.get("priority") or "").lower(), 3),
        _parse_store_iso(it.get("last_notified")) or now,
    ))
    try:
        cap = int(nudge_cfg.get("check_limit") or 25)
    except (TypeError, ValueError):
        cap = 25
    return out[:cap]


def _parse_store_iso(value) -> Optional[datetime]:
    """Parse an ISO timestamp from the store into a tz-aware UTC datetime."""
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def detect_my_reply(
    item: dict, me_id: str, creds, *, fetch_fn=None, space_fetch_fn=None
) -> Optional[bool]:
    """READ-ONLY: did *me_id* post in this item's thread/space after the message?

    Scope of the search depends on the space's threading model:

    * **DIRECT_MESSAGE / GROUP_CHAT** spaces are FLAT — every message is its own
      thread, so a thread-scoped fetch would return only the original message and
      never see my reply (which lives in a *sibling* thread of the same space).
      We therefore scan the whole space there.
    * **Named SPACE rooms** keep precise thread scoping ("did I answer THIS
      conversation"), so a reply I posted elsewhere in the room is not mistaken
      for an answer to this item.

    Returns:
      True  — a message from me_id newer than the item exists,
      False — none found,
      None  — could not determine (no ids / fetch error) → caller skips safely.

    Never writes to Chat. Bare numeric ids are compared so "users/123" matches.
    """
    if not me_id:
        return None
    space_name = item.get("space_name") or ""
    if not space_name:
        return None
    me_bare = str(me_id).split("/")[-1]
    created = item.get("created_time")

    import google_chat as gchat  # noqa: PLC0415
    fetch_thread = fetch_fn or gchat._list_thread_messages_sync
    fetch_space = space_fetch_fn or gchat._list_messages_sync
    thread_name = item.get("thread_name") or ""
    space_type = (item.get("space_type") or "").upper()
    # Per-message threading in DMs/group chats makes thread scoping useless for
    # reply detection — fall back to a space-wide scan there (see docstring).
    use_thread = bool(thread_name) and space_type not in (
        "DIRECT_MESSAGE",
        "GROUP_CHAT",
    )

    try:
        if use_thread:
            messages = fetch_thread(creds, space_name, thread_name, max_messages=50)
        else:
            messages = fetch_space(creds, space_name, created, None)
    except Exception:  # noqa: BLE001 - read failure → undetermined, never raise
        return None

    created_dt = _parse_store_iso(created)
    for msg in messages or []:
        sender = (msg.get("sender") or {}).get("name") or ""
        if str(sender).split("/")[-1] != me_bare:
            continue
        # In thread mode we still bound by the original message time so an older
        # message of mine in the same thread does not count as a reply.
        mt = _parse_store_iso(msg.get("createTime") or msg.get("created_time"))
        if created_dt is not None and mt is not None and mt <= created_dt:
            continue
        return True
    return False


def _validate_suggestions(data: dict, *, max_n: int) -> Optional[list]:
    """Return a cleaned list of 1..max_n non-empty reply strings, or None."""
    if not isinstance(data, dict):
        return None
    raw = data.get("suggestions")
    if not isinstance(raw, list):
        return None
    out = []
    for s in raw:
        if isinstance(s, str) and s.strip():
            out.append(s.strip())
    return out[:max_n] if out else None


def generate_reply_suggestions(
    item: dict, cfg: dict, adapter: AnalysisAdapter
) -> Optional[list]:
    """Call the LLM adapter to draft reply options. Returns a cleaned list or None.

    UNTRUSTED message text is sanitized inside ``build_reply_suggestions_prompt``;
    the returned drafts are themselves UNTRUSTED and MUST be escaped at render."""
    analyze_cfg = cfg.get("analyze") or {}
    nudge_cfg = analyze_cfg.get("reply_nudge") or {}
    model = (analyze_cfg.get("adapters", {}).get("claude", {})
             .get("model", "claude-haiku-4-5-20251001"))
    timeout = analyze_cfg.get("timeout_seconds", 60)
    max_n = int(nudge_cfg.get("max_suggestions") or 3)

    ctx = {
        "me_name": cfg.get("me_display_name") or "",
        "me_role": cfg.get("me_role") or "engineering lead",
        "sender_name": item.get("sender_name") or "",
        "sender_role": item.get("sender_role") or "",
        "space_display": item.get("space_display") or "",
        "space_type": item.get("space_type") or "",
        "summary": item.get("context_summary") or "",
        "text": item.get("text") or "",
        "min_suggestions": nudge_cfg.get("min_suggestions") or 2,
        "max_suggestions": max_n,
        "reply_language": nudge_cfg.get("reply_language") or "",
    }
    req = AnalysisRequest(
        mode="classify",  # single-shot JSON call; reuses the classify adapter path
        prompt=build_reply_suggestions_prompt(ctx),
        model=model,
        timeout_seconds=timeout,
    )
    res: AnalysisResult = adapter.run(req)
    if not res.ok:
        return None
    return _validate_suggestions(res.data or {}, max_n=max_n)


def run_reply_nudge(
    store_obj: dict,
    cfg: dict,
    now: datetime,
    *,
    adapter: Optional[AnalysisAdapter] = None,
    me_id: Optional[str] = None,
    reply_fetch_fn=None,
    dry_run: bool = False,
    limit: Optional[int] = None,
    log=None,
) -> dict:
    """Run one Tier-3 reply-nudge pass.

    For each eligible item: detect whether I already replied (READ-ONLY). If so,
    record it (``response_posted``/``answered_at``) and skip. Otherwise generate
    reply drafts and stash them on the item so the notify stage can send the
    nudge. NEVER writes to Chat; NEVER changes lifecycle status.

    Returns stats: ``{checked, replied, suggested, failed}``.
    """
    def _log(event, **kw):
        if log:
            log(event, **kw)

    analyze_cfg = cfg.get("analyze") or {}
    nudge_cfg = analyze_cfg.get("reply_nudge") or {}
    if not nudge_cfg.get("enabled", False):
        return {"checked": 0, "replied": 0, "suggested": 0, "failed": 0}

    items = select_nudge_items(store_obj, cfg, now)
    if limit is not None:
        items = items[:limit]
    if not items:
        return {"checked": 0, "replied": 0, "suggested": 0, "failed": 0}

    if adapter is None:
        adapter = _select_adapter(cfg)
    if adapter is None:
        _log("nudge-skip", reason="no-adapter-available")
        return {"checked": 0, "replied": 0, "suggested": 0, "failed": 0}

    import google_chat as gchat  # noqa: PLC0415
    creds = gchat.get_credentials()
    if creds is None:
        _log("nudge-skip", reason="no-credentials")
        return {"checked": 0, "replied": 0, "suggested": 0, "failed": 0}

    if not me_id:
        me_id = cfg.get("me_user_id") or ""
        if not me_id:
            try:
                me_id = gchat._resolve_me_sync(creds)
            except Exception:  # noqa: BLE001
                me_id = ""
    if not me_id:
        _log("nudge-skip", reason="no-me-id")
        return {"checked": 0, "replied": 0, "suggested": 0, "failed": 0}

    now_iso = _iso_z(now)
    checked = replied = suggested = failed = 0

    for item in items:
        checked += 1
        replied_now = detect_my_reply(item, me_id, creds, fetch_fn=reply_fetch_fn)
        if replied_now:
            replied += 1
            if dry_run:
                print(f"[dry-run] NUDGE item={item.get('id')} → already replied")
            else:
                store.set_fields(
                    store_obj, item["id"],
                    response_posted=True, answered_at=now_iso, reply_checked_at=now_iso,
                )
            _log("nudge-replied", item_id=item.get("id"))
            continue

        drafts = generate_reply_suggestions(item, cfg, adapter)
        if not drafts:
            failed += 1
            _log("nudge-failed", item_id=item.get("id"))
            continue

        suggested += 1
        if dry_run:
            print(f"[dry-run] NUDGE item={item.get('id')} → {len(drafts)} drafts")
        else:
            store.set_fields(
                store_obj, item["id"],
                reply_suggestions=drafts, reply_checked_at=now_iso,
            )
        _log("nudge-suggested", item_id=item.get("id"), n=len(drafts))

    _log("nudge-run", checked=checked, replied=replied, suggested=suggested, failed=failed)
    return {"checked": checked, "replied": replied, "suggested": suggested, "failed": failed}


# --------------------------------------------------------------------------- #
# Core orchestrator
# --------------------------------------------------------------------------- #
def run_analysis(
    store_obj: dict,
    cfg: dict,
    now: datetime,
    *,
    adapter: Optional[AnalysisAdapter] = None,
    dry_run: bool = False,
    limit: Optional[int] = None,
    log=None,
    store_path=None,
    cron: bool = False,
) -> dict:
    """Run one analysis pass.  Assumes the run-lock is already held by caller.

    Returns stats dict: ``{status, selected, stored, deferred, failed, skipped}``.
    ``status`` is ``"disabled"`` when the gate short-circuits, else ``"ok"``.

    The *adapter* parameter is intentionally injectable so tests can pass a
    fake adapter and avoid any real subprocess/LLM calls.

    When *dry_run* is True the proposed store mutations are printed but the
    store is never written to disk.
    """
    def _log(event, **kw):
        if log:
            log(event, **kw)

    # --- GATE: analyze.enabled -------------------------------------------- #
    analyze_cfg = cfg.get("analyze", {})
    if not analyze_cfg.get("enabled", False):
        _log("skip", reason="analyze-disabled")
        return {"status": "disabled", "selected": 0, "stored": 0, "deferred": 0, "failed": 0, "skipped": 0, "escalated": 0, "thread_failed": 0}

    # --- GATE: global kill-switch ----------------------------------------- #
    if not config.is_active(cfg, now=now):
        _log("skip", reason="global-inactive")
        return {"status": "disabled", "selected": 0, "stored": 0, "deferred": 0, "failed": 0, "skipped": 0, "escalated": 0, "thread_failed": 0}

    # --- ADAPTER SELECTION ------------------------------------------------- #
    if adapter is None:
        adapter = _select_adapter(cfg)
    if adapter is None:
        _log("skip", reason="no-adapter-available")
        return {"status": "ok", "selected": 0, "stored": 0, "deferred": 0, "failed": 0, "skipped": 0, "escalated": 0, "thread_failed": 0}

    # --- SELECTION --------------------------------------------------------- #
    items = select_items(store_obj, cfg, limit=limit)
    _log("selected", count=len(items))

    stored = 0
    deferred = 0
    failed = 0

    for item in items:
        outcome = analyze_item(
            item,
            cfg,
            adapter,
            now,
            store_obj=store_obj,
            log=log,
            dry_run=dry_run,
        )
        if outcome == "stored":
            stored += 1
        elif outcome == "deferred":
            deferred += 1
        else:
            failed += 1

    # --- TIER-2: thread escalation ----------------------------------------- #
    # Run AFTER the Tier-1 loop so items just-DEFER'd this run are also included.
    # Gate: only when escalate_to_thread is enabled and an adapter is available.
    escalated = 0
    thread_failed = 0
    if analyze_cfg.get("escalate_to_thread", True):
        tier2_stats = run_tier2(
            store_obj,
            cfg,
            now,
            adapter=adapter,
            dry_run=dry_run,
            limit=limit,
            log=log,
        )
        escalated = tier2_stats.get("escalated", 0)
        thread_failed = tier2_stats.get("thread_failed", 0)

    # --- TIER-3: reply nudge ---------------------------------------------- #
    # Gate: reply_nudge.enabled (master) AND, in cron context, run_in_cron.
    # Detection is READ-ONLY; drafts are stored for the notify stage to send.
    nudge_stats = {"checked": 0, "replied": 0, "suggested": 0, "failed": 0}
    nudge_cfg = analyze_cfg.get("reply_nudge") or {}
    if nudge_cfg.get("enabled", False) and (not cron or nudge_cfg.get("run_in_cron", False)):
        nudge_stats = run_reply_nudge(
            store_obj,
            cfg,
            now,
            adapter=adapter,
            dry_run=dry_run,
            log=log,
        )

    # --- PERSIST (atomic, once) ------------------------------------------- #
    if not dry_run:
        store.save(store_obj, store_path)

    _log(
        "run",
        status="ok",
        selected=len(items),
        stored=stored,
        deferred=deferred,
        failed=failed,
        escalated=escalated,
        thread_failed=thread_failed,
        nudge_suggested=nudge_stats["suggested"],
        nudge_replied=nudge_stats["replied"],
    )
    return {
        "status": "ok",
        "selected": len(items),
        "stored": stored,
        "deferred": deferred,
        "failed": failed,
        "skipped": 0,
        "escalated": escalated,
        "thread_failed": thread_failed,
        "nudge_checked": nudge_stats["checked"],
        "nudge_replied": nudge_stats["replied"],
        "nudge_suggested": nudge_stats["suggested"],
        "nudge_failed": nudge_stats["failed"],
    }


# --------------------------------------------------------------------------- #
# CLI entrypoint
# --------------------------------------------------------------------------- #
def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Headless Chat-triage AI analysis stage (Tier-1 classify).",
    )
    p.add_argument("--config-path", default=None, help="override config.json path")
    p.add_argument("--store-path", default=None, help="override store.json path")
    p.add_argument("--base-dir", default=None, help="override triage base dir (locks/logs)")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="run Tier-0+Tier-1 (incl. real LLM) but print proposals and write NOTHING",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="override max_items_per_run for this run",
    )
    p.add_argument("--verbose", action="store_true", help="print structured log lines to stdout")
    p.add_argument(
        "--cron",
        action="store_true",
        help=(
            "invoked by triage_cron.sh; self-skip (exit 0) when "
            "analyze.run_in_cron is False in config, so the cron chain "
            "always proceeds to notify.py regardless"
        ),
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    base_dir = Path(args.base_dir) if args.base_dir else config.BASE_DIR
    now_dt = _now_dt()
    log = _make_logger(base_dir, now_dt)

    # --- GATE: eager config check BEFORE acquiring the lock so a disabled
    #     pipeline is completely silent. --------------------------------------- #
    cfg = config.load_config(args.config_path)
    analyze_cfg = cfg.get("analyze", {})

    # --- CRON GATE: when invoked by triage_cron.sh (--cron flag), skip silently
    #     unless analyze.run_in_cron is explicitly True.  This ensures notify.py
    #     always runs on the cron chain even when LLM analysis is not opted in.
    if args.cron and not analyze_cfg.get("run_in_cron", False):
        log("skip", reason="run_in_cron-disabled")
        if args.verbose:
            print("analyze.run_in_cron is False; skipping (cron mode)")
        return 0
    if not analyze_cfg.get("enabled", False):
        log("skip", reason="analyze-disabled")
        if args.verbose:
            print("analyze disabled in config, skipping")
        return 0
    if not config.is_active(cfg, now=now_dt):
        log("skip", reason="global-inactive")
        if args.verbose:
            print("triage globally inactive/muted, skipping")
        return 0

    # --- RUN-LOCK ----------------------------------------------------------- #
    run_lock = _acquire_lock(base_dir / RUN_LOCK_NAME, blocking=False)
    if run_lock is None:
        log("skip", reason="another-run-in-progress")
        if args.verbose:
            print("another analyze run in progress; exiting")
        return 0

    try:
        store_obj = store.load(args.store_path)
        result = run_analysis(
            store_obj,
            cfg,
            now_dt,
            dry_run=args.dry_run,
            limit=args.limit,
            log=log,
            store_path=args.store_path,
            cron=args.cron,
        )
    finally:
        _release_lock(run_lock)

    if result["status"] == "disabled":
        print("analyze disabled/muted, skipping")
        return 0

    print(
        f"analyze — selected={result['selected']} "
        f"stored={result['stored']} "
        f"deferred={result['deferred']} "
        f"failed={result['failed']} "
        f"escalated={result.get('escalated', 0)} "
        f"thread_failed={result.get('thread_failed', 0)} "
        f"nudge_suggested={result.get('nudge_suggested', 0)} "
        f"nudge_replied={result.get('nudge_replied', 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
