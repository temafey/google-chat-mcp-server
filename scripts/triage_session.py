"""scripts/triage_session.py — deterministic triage LIFECYCLE driver for the
Chat Triage Assistant.

ZERO Claude/LLM calls, ZERO network, ZERO config/google_chat imports. This is a
pure state module: it reads the ledger, orders the work queue, and drives items
through the Stage-5 state machine by delegating EVERY mutation to
``scripts/store.py`` (the single source of truth for valid statuses, history
entries, and field writes). It never invents a status, never adds an item field,
and never posts anything — ``record_response`` records a reply that was already
posted upstream; it does not send it.

Design notes
------------
* **Injectable clock** — every public function accepts a ``now`` (``None`` →
  wall clock UTC, a ``datetime``, or an ISO string) so tests run against a fixed
  clock, mirroring ``store.py`` / ``notify.py``.
* **Persistence** — every mutator calls ``store.save(store, path)`` after the
  transition, so a successful call leaves the ledger durable on disk.
* **Error propagation** — an unknown ``item_id`` surfaces ``store.py``'s
  ``KeyError`` verbatim (we never swallow it); an invalid priority raises
  ``ValueError`` before any state is touched.
* **The store module is aliased ``_store``** so the public ``store`` parameter
  (a plain ledger dict) can keep the exact name the API spec mandates without
  shadowing the module.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

# ``scripts/`` is not an installed package; make the sibling ``store`` module
# importable regardless of cwd. No repo-root entry is needed — this module is
# pure state and imports neither ``google_chat`` nor ``config``.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import store as _store  # noqa: E402  (scripts/store.py — aliased; see module docstring)


# --------------------------------------------------------------------------- #
# Time helpers (consistent with store.py / notify.py: ``...Z``, second precision).
# --------------------------------------------------------------------------- #
def _iso(now=None) -> str:
    """ISO-8601 UTC string (``...Z``, second precision).

    ``now`` may be ``None`` (wall clock), a ``datetime``, or an already-formatted
    ISO string (returned untouched) — matches ``store._now_iso``.
    """
    if now is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(now, str):
        return now
    else:
        dt = now
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (
        dt.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse(value) -> datetime:
    """Parse an ISO-8601 string (or pass a ``datetime`` through) to aware UTC."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _resolve_now(now=None) -> datetime:
    """Return an aware UTC ``now`` (default: wall clock UTC)."""
    if now is None:
        return datetime.now(timezone.utc)
    return _parse(now)


# Far-past sentinel so items with no ``created_time`` sort oldest-first.
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Queue ordering.
# --------------------------------------------------------------------------- #
def _tier(item: dict, vip: set, keywords: list) -> int:
    """Attention tier (lower = more urgent): direct_dm < vip < urgency-keyword <
    user_mention < broadcast < everything else."""
    if item.get("trigger") == "direct_dm":
        return 0
    if item.get("sender_id") in vip:
        return 1
    text = (item.get("text") or "").lower()
    if any(kw in text for kw in keywords):
        return 2
    if item.get("trigger") == "user_mention":
        return 3
    if item.get("trigger") == "broadcast":
        return 4
    return 5


def _sort_key(item: dict, now_dt: datetime, vip: set, keywords: list):
    due = item.get("promise_due")
    overdue = bool(due) and _parse(due) < now_dt
    group = 0 if overdue else 1  # overdue promises first
    created = item.get("created_time")
    created_dt = _parse(created) if created else _EPOCH
    return (group, _tier(item, vip, keywords), created_dt)


def triage_queue(store, *, now=None, vip_senders=(), urgency_keywords=()) -> list:
    """Return the OPEN items needing attention, ordered (see :func:`_sort_key`).

    Includes every item whose status is in ``OPEN_STATUSES`` EXCEPT a ``snoozed``
    item whose ``snooze_until`` is set and still in the future (snooze not yet
    elapsed). A snoozed item with ``snooze_until <= now`` — or with no
    ``snooze_until`` at all — IS included. ``closed`` / ``ignored`` are never
    open, so they are always excluded. Returns the actual item dicts (not copies)
    so callers can mutate them in place.
    """
    now_dt = _resolve_now(now)
    vip = set(vip_senders or ())
    keywords = [kw.lower() for kw in (urgency_keywords or ()) if kw]

    selected = []
    for item in _store.open_items(store):
        if item.get("status") == "snoozed":
            until = item.get("snooze_until")
            if until and _parse(until) > now_dt:
                continue  # still sleeping — hide until it wakes
        selected.append(item)

    selected.sort(key=lambda it: _sort_key(it, now_dt, vip, keywords))
    return selected


# --------------------------------------------------------------------------- #
# Lifecycle transitions. Each delegates to store.set_status (status + history),
# then persists and returns the item dict.
# --------------------------------------------------------------------------- #
def _persisted(store, item_id: str, path) -> dict:
    """Save the ledger and return the (now-mutated) item dict."""
    _store.save(store, path)
    return store["items"][item_id]


_VALID_PRIORITIES = ("high", "normal", "low")


def set_triage(
    store,
    item_id: str,
    *,
    priority: str,
    priority_reason=None,
    context_summary=None,
    context_confidence=None,
    now=None,
    path=None,
) -> dict:
    """Transition an item to ``triaged`` with a human/LLM-decided priority.

    ``priority`` must be one of ``high`` / ``normal`` / ``low`` (else
    ``ValueError``). Only the non-``None`` optional fields are written — passing
    ``context_summary=None`` leaves any existing value untouched.
    """
    if priority not in _VALID_PRIORITIES:
        raise ValueError(
            f"invalid priority {priority!r}; expected one of {', '.join(_VALID_PRIORITIES)}"
        )
    fields = {"priority": priority}
    if priority_reason is not None:
        fields["priority_reason"] = priority_reason
    if context_summary is not None:
        fields["context_summary"] = context_summary
    if context_confidence is not None:
        fields["context_confidence"] = context_confidence
    _store.set_status(store, item_id, "triaged", now=now, **fields)
    return _persisted(store, item_id, path)


def snooze(store, item_id: str, *, until, now=None, path=None) -> dict:
    """Transition an item to ``snoozed`` until ``until`` (ISO str or datetime)."""
    _store.set_status(store, item_id, "snoozed", now=now, snooze_until=_iso(until))
    return _persisted(store, item_id, path)


def record_promise(store, item_id: str, *, my_promise, promise_due, now=None, path=None) -> dict:
    """Transition to ``awaiting_me`` (I owe a response) with a promise + due time.

    The notifier escalates overdue promises while the item stays open.
    """
    _store.set_status(
        store,
        item_id,
        "awaiting_me",
        now=now,
        my_promise=my_promise,
        promise_due=_iso(promise_due),
    )
    return _persisted(store, item_id, path)


def record_response(store, item_id: str, *, response_text, response_posted, now=None, path=None) -> dict:
    """Transition to ``answered`` AFTER a reply was posted upstream.

    Records the reply text, the posted message resource name, and the answer
    timestamp. It RECORDS — it does NOT post anything.
    """
    _store.set_status(
        store,
        item_id,
        "answered",
        now=now,
        response_text=response_text,
        response_posted=response_posted,
        answered_at=_iso(now),
    )
    return _persisted(store, item_id, path)


def close_item(store, item_id: str, *, now=None, path=None) -> dict:
    """Transition an item to the terminal ``closed`` status."""
    _store.set_status(store, item_id, "closed", now=now)
    return _persisted(store, item_id, path)


def ignore_item(store, item_id: str, *, now=None, path=None) -> dict:
    """Transition an item to the terminal ``ignored`` status."""
    _store.set_status(store, item_id, "ignored", now=now)
    return _persisted(store, item_id, path)
