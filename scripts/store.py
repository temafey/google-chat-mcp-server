"""Atomic JSON ledger for the Chat Triage Assistant (T1.2).

Single source of truth for triage items lives at
``~/.claude-orchestrator/gchat-triage/store.json``. This module is the
deterministic state layer — no network, no reasoning, pure stdlib.

Design notes
------------
* **Atomic writes** — :func:`save` writes a sibling temp file in the *same*
  directory then ``os.replace``s it over the target. ``os.replace`` is atomic
  on POSIX, so a crash mid-write can never leave a truncated ``store.json``;
  the previous file stays intact until the rename succeeds.
* **Item id** — ``sha1(space_name + message_name)`` so the same source message
  always maps to the same ledger entry (idempotent collection / dedupe).
* **R6 (edited / deleted messages)** — :func:`upsert_item` refreshes ``text``
  but never touches ``status``/``history``; :func:`mark_stale` flags an item
  whose source message 404s on re-fetch without deleting it.
* **Testability** — every timestamping function accepts an injectable ``now``
  so tests stay deterministic; nothing calls ``datetime.now`` implicitly in a
  path a test would exercise with a fixed clock.

Security: this layer handles no secrets and deliberately does no logging of
item ``text``.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# --- store location -------------------------------------------------------

#: Default ledger path; overridable per-call (tests) or via env var.
DEFAULT_STORE_PATH = Path.home() / ".claude-orchestrator" / "gchat-triage" / "store.json"

STORE_VERSION = 1

#: Stage-5 state machine — the only statuses :func:`set_status` will accept.
VALID_STATUSES = (
    "new",
    "triaged",
    "awaiting_me",
    "snoozed",
    "answered",
    "closed",
    "ignored",
)

#: An item is "open" while in one of these. ``answered`` is the
#: "answered-but-not-closed" state from the spec — once an item is explicitly
#: closed its status becomes ``closed`` (which is *not* open). ``ignored`` is
#: likewise terminal and not open.
OPEN_STATUSES = frozenset({"new", "triaged", "awaiting_me", "snoozed", "answered"})


# --- time helpers ---------------------------------------------------------

def _now_iso(now=None) -> str:
    """Return an ISO-8601 UTC string (``...Z``, second precision).

    ``now`` may be ``None`` (use the wall clock), a ``datetime``, or an
    already-formatted ISO string (returned untouched).
    """
    if now is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(now, str):
        return now
    else:
        dt = now
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value) -> datetime:
    """Parse an ISO-8601 string (or pass a ``datetime`` through) to an aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --- skeletons ------------------------------------------------------------

def _empty_store() -> dict:
    """A fresh ledger skeleton (used when no file exists yet)."""
    return {
        "version": STORE_VERSION,
        "me_user_id": None,
        "last_run": None,
        "cursor_per_space": {},
        "items": {},
    }


def _new_item_skeleton() -> dict:
    """All spec item fields with their default/empty values."""
    return {
        "id": None,
        "space_name": None,
        "space_display": None,
        "space_type": None,
        "message_name": None,
        "thread_name": None,
        "sender_id": None,
        "sender_name": None,
        "created_time": None,
        "text": None,
        "trigger": None,
        "detected_at": None,
        "priority": "unset",
        "priority_reason": None,
        "context_summary": None,
        "context_confidence": None,
        "status": "new",
        "snooze_until": None,
        "my_promise": None,
        "promise_due": None,
        "response_posted": None,
        "response_text": None,
        "answered_at": None,
        "last_notified": None,
        # Pin ("starred"): an orthogonal user flag — NOT a status. A pinned open
        # item rides along on every digest until unpinned (see notify.run_once).
        # Toggled via store.set_fields (verbatim, no status churn); absent on
        # pre-existing on-disk items, where .get("pinned") is falsy.
        "pinned": False,
        "pinned_at": None,
        "stale": False,
        "history": [],
    }


# --- persistence ----------------------------------------------------------

def _resolve_path(path=None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get("GCHAT_TRIAGE_STORE")
    if env:
        return Path(env)
    return DEFAULT_STORE_PATH


def load(path=None) -> dict:
    """Load the ledger, or return a fresh skeleton if the file is absent."""
    target = _resolve_path(path)
    if not target.exists():
        return _empty_store()
    with target.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save(store: dict, path=None) -> None:
    """Atomically persist ``store``.

    Writes a temp file in the same directory, fsyncs it, then ``os.replace``s
    it over the target. If anything fails before the rename the temp file is
    removed and the prior ``store.json`` is left untouched.
    """
    target = _resolve_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".store-", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(store, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(target))
    except BaseException:
        # Crash before the rename: drop the partial temp, leave target intact.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


# --- ids ------------------------------------------------------------------

def item_id(space_name: str, message_name: str) -> str:
    """Stable id for a source message: ``sha1(space_name + message_name)``."""
    return hashlib.sha1((space_name + message_name).encode("utf-8")).hexdigest()


# --- mutations ------------------------------------------------------------

def upsert_item(store: dict, item: dict, now=None) -> str:
    """Insert a new item or refresh an existing one; return its id.

    *New* item → seeded from the skeleton, forced ``status="new"``, history
    gets a ``detected`` entry.

    *Existing* item → ``text`` is refreshed and the ``stale`` flag cleared,
    but ``status`` and prior ``history`` are preserved (R6). A history
    ``updated`` entry is appended **only when something actually changed**, so
    idempotent re-collection of an unchanged message does not bloat history.
    """
    iid = item_id(item["space_name"], item["message_name"])
    ts = _now_iso(now)
    items = store.setdefault("items", {})
    existing = items.get(iid)

    if existing is None:
        record = _new_item_skeleton()
        record.update(item)
        record["id"] = iid
        record["status"] = "new"
        if not record.get("detected_at"):
            record["detected_at"] = ts
        record["stale"] = bool(record.get("stale", False))
        history = list(record.get("history") or [])
        history.append({"ts": ts, "event": "detected"})
        record["history"] = history
        items[iid] = record
        return iid

    # Update path — preserve status + history, refresh text (R6).
    changed = False
    new_text = item.get("text", existing.get("text"))
    if new_text != existing.get("text"):
        existing["text"] = new_text
        changed = True
    if existing.get("stale"):
        existing["stale"] = False
        changed = True
    if changed:
        existing.setdefault("history", []).append({"ts": ts, "event": "updated"})
    return iid


def set_status(store: dict, item_id_: str, status: str, now=None, **fields) -> None:
    """Transition an item to ``status`` (validated) and append a history entry.

    Extra ``**fields`` (e.g. ``snooze_until``, ``my_promise``, ``promise_due``,
    ``response_posted``, ``answered_at``) are written onto the item verbatim.
    """
    if status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status {status!r}; expected one of {', '.join(VALID_STATUSES)}"
        )
    item = store.get("items", {}).get(item_id_)
    if item is None:
        raise KeyError(f"unknown item id {item_id_!r}")
    ts = _now_iso(now)
    item["status"] = status
    for key, value in fields.items():
        item[key] = value
    item.setdefault("history", []).append({"ts": ts, "event": f"status:{status}"})


def mark_stale(store: dict, item_id_: str, now=None) -> None:
    """R6: source message 404'd on re-fetch — flag it stale, keep the item."""
    item = store.get("items", {}).get(item_id_)
    if item is None:
        raise KeyError(f"unknown item id {item_id_!r}")
    ts = _now_iso(now)
    item["stale"] = True
    item.setdefault("history", []).append({"ts": ts, "event": "stale"})


def set_fields(store: dict, item_id_: str, now=None, **fields) -> None:
    """Set arbitrary item fields verbatim. NO status change, NO history entry.

    For notification bookkeeping (``last_notified``, ``promise_escalated_at``).
    ``now`` is accepted for signature symmetry with the other mutators but is
    unused — this writes no history entry. Raises ``KeyError`` if the item id
    is unknown.
    """
    item = store.get("items", {}).get(item_id_)
    if item is None:
        raise KeyError(f"unknown item id {item_id_!r}")
    item.update(fields)


# --- queries --------------------------------------------------------------

def open_items(store: dict) -> list:
    """Items whose status is open (new/triaged/awaiting_me/snoozed/answered)."""
    return [it for it in store.get("items", {}).values() if it.get("status") in OPEN_STATUSES]


def overdue_promises(store: dict, now=None) -> list:
    """Open items with a ``promise_due`` set and earlier than ``now``."""
    cutoff = _parse_iso(now) if now is not None else datetime.now(timezone.utc)
    result = []
    for it in open_items(store):
        due = it.get("promise_due")
        if due and _parse_iso(due) < cutoff:
            result.append(it)
    return result
