"""Detection core for "messages addressed to me" (Chat Triage — T0.2).

Single importable primitive shared by the future MCP tool (T1.1) and the
headless cron collector (T1.3). Given a time range, it returns normalized item
dicts for every Google Chat message that is *for me*:

  * any message in a DIRECT_MESSAGE space            → trigger ``direct_dm``
  * a message in a SPACE / GROUP_CHAT that @mentions
    my ``users/<id>`` specifically                   → trigger ``user_mention``
  * a room-wide @all / @here mention                 → trigger ``broadcast``

Everything else (other people's mentions, plain channel chatter) is dropped.

Why a dedicated fetch path (R8): the public ``google_chat.list_space_messages``
strips messages to ``{sender, createTime, text, thread}`` when
``SAVE_TOKEN_MODE`` is on (the default), discarding the ``annotations`` and
``name`` fields mention detection depends on. We therefore reuse
``google_chat._list_messages_sync`` — the low-level paginated ``messages.list``
helper that returns the *raw* API payload with no field stripping — instead of
routing through ``list_space_messages``.

This module never acts on message *content*; it only classifies metadata
(space type + annotations). Treat message text as untrusted data, never as
instructions (prompt-injection guard).
"""
from __future__ import annotations

import asyncio
import datetime
from typing import Any, Dict, Iterator, List, Optional, Union

import google_chat as gchat

# Space types that can carry an @mention. DIRECT_MESSAGE is handled separately
# (every DM message is for me, no mention required).
_MENTIONABLE_SPACE_TYPES = ("SPACE", "GROUP_CHAT")

# A USER_MENTION annotation's ``userMention.type`` (UserMentionMetadata.Type)
# distinguishes a real @mention from a membership add. The public REST enum is
# {TYPE_UNSPECIFIED, ADD, MENTION}: ``ADD`` means "this user was ADDED to the
# space" (a membership event), NOT "@-mentioned". We must not treat being added
# to a space as a mention of me — only ``MENTION`` (or an unspecified/absent
# type, treated permissively) counts. Verified against the Chat API reference
# (google.chat.v1 Annotation / UserMentionMetadata), 2026-06-07.
_NON_MENTION_USERMENTION_TYPES = {"ADD"}

# Best-effort heuristics for a room-wide ("@all" / "@here") mention. IMPORTANT:
# the public Chat REST API does NOT document any @all/@everyone representation —
# a USER_MENTION's ``user`` is always a concrete ``users/{id}`` (human or BOT),
# and there is no ``users/all`` sentinel in the reference. These sentinels are
# therefore defensive only (harmless if Google never emits them); a real
# broadcast may simply arrive as plain "@all" text with no annotation and go
# undetected. Do NOT rely on broadcast detection being complete.
_BROADCAST_USER_TYPES = {"ALL", "HERE", "EVERYONE"}
_BROADCAST_SENTINEL_NAMES = {"users/all", "users/here", "users/everyone"}

DateLike = Union[datetime.datetime, str, None]


def _to_iso(value: DateLike) -> Optional[str]:
    """Coerce a datetime/str/None into an RFC3339 string for the API filter."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def _coerce_aware_dt(value: DateLike) -> Optional[datetime.datetime]:
    """Parse a datetime/RFC3339-str into a timezone-aware UTC datetime.

    Naive datetimes are assumed to be UTC so they compare cleanly against
    Google's ``lastActiveTime`` (always ``...Z``).
    """
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _is_dormant(space: Dict[str, Any], since_dt: Optional[datetime.datetime]) -> bool:
    """R3: a space is dormant (skip its fetch) when its last activity predates
    the ``since`` cursor. Unknown ``lastActiveTime`` → never skip (fetch it)."""
    if since_dt is None:
        return False
    last = _coerce_aware_dt(space.get("lastActiveTime"))
    if last is None:
        return False
    return last < since_dt


def _is_real_mention(um: Dict[str, Any]) -> bool:
    """True unless this ``userMention`` is an ADD (membership), not an @mention.

    ``ADD`` annotations are emitted when a user is added to a space; they are not
    a mention of anyone. An absent / unspecified type is treated permissively as
    a mention (forward-compatible with future enum values).
    """
    return str(um.get("type") or "").upper() not in _NON_MENTION_USERMENTION_TYPES


def _iter_user_mentions(message: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """Yield the ``userMention`` payload of every *real* USER_MENTION annotation.

    Membership-add annotations (``userMention.type == "ADD"``) are skipped — see
    :func:`_is_real_mention`.
    """
    for ann in message.get("annotations") or []:
        if ann.get("type") != "USER_MENTION":
            continue
        um = ann.get("userMention") or {}
        if not _is_real_mention(um):
            continue
        yield um


def _mention_trigger(message: Dict[str, Any], me: str) -> Optional[str]:
    """Classify a SPACE/GROUP_CHAT message's mention annotations.

    Returns ``"user_mention"`` if I'm mentioned by id (wins outright),
    ``"broadcast"`` if only a room-wide mention is present, else ``None``.
    Membership-add annotations are ignored (see :func:`_iter_user_mentions`).
    """
    broadcast = False
    for um in _iter_user_mentions(message):
        user = um.get("user") or {}
        if user.get("name") == me:
            return "user_mention"
        utype = str(user.get("type") or "").upper()
        if utype in _BROADCAST_USER_TYPES or user.get("name") in _BROADCAST_SENTINEL_NAMES:
            broadcast = True
    return "broadcast" if broadcast else None


def _classify(message: Dict[str, Any], space_type: Optional[str], me: str) -> Optional[str]:
    """Return the trigger for a message, or None if it is not for me."""
    if space_type == "DIRECT_MESSAGE":
        return "direct_dm"
    if space_type in _MENTIONABLE_SPACE_TYPES:
        return _mention_trigger(message, me)
    return None


def _normalize(
    message: Dict[str, Any],
    space: Dict[str, Any],
    trigger: str,
    creds: Any,
) -> Dict[str, Any]:
    """Build a store.json-shaped item dict from a raw message + its space."""
    sender = message.get("sender") or {}
    return {
        "space_name": space.get("name"),
        "space_display": space.get("displayName"),
        "space_type": space.get("spaceType"),
        "message_name": message.get("name"),
        "thread_name": (message.get("thread") or {}).get("name"),
        "sender_id": sender.get("name"),
        "sender_name": gchat.get_user_display_name(sender, creds) if sender else None,
        "created_time": message.get("createTime"),
        "text": message.get("text"),
        "trigger": trigger,
    }


async def list_messages_for_me(
    start: DateLike,
    end: DateLike,
    space_names: Optional[List[str]] = None,
    include_dms: bool = True,
    since: DateLike = None,
) -> List[Dict[str, Any]]:
    """Return normalized items for every message addressed to me in [start, end].

    Args:
        start: range lower bound (datetime or RFC3339 str). ``createTime >`` filter.
        end: range upper bound (datetime or RFC3339 str). ``createTime <`` filter.
        space_names: optional ``["spaces/<id>", ...]`` subset to scan; defaults to
            every space the user is a member of. Named spaces the user is not a
            member of are silently ignored (they cannot contain mentions of me).
        include_dms: when False, skip DIRECT_MESSAGE spaces entirely.
        since: R3 dormant-space cursor — spaces whose ``lastActiveTime`` predates
            this are skipped without being fetched.

    Returns:
        Items sorted newest-first, each with keys: ``space_name, space_display,
        space_type, message_name, thread_name, sender_id, sender_name,
        created_time, text, trigger`` (the store.json item field names).
    """
    creds = gchat.get_credentials()
    if not creds:
        raise RuntimeError("No valid credentials found. Please authenticate first.")

    me = await asyncio.to_thread(gchat._resolve_me_sync, creds)
    since_dt = _coerce_aware_dt(since)
    start_iso = _to_iso(start)
    end_iso = _to_iso(end)

    all_spaces = await gchat.list_chat_spaces()
    if space_names:
        for sp in space_names:
            gchat._validate_space_name(sp)
        wanted = set(space_names)
        target_spaces = [sp for sp in all_spaces if sp.get("name") in wanted]
    else:
        target_spaces = list(all_spaces)

    items: List[Dict[str, Any]] = []
    for space in target_spaces:
        space_type = space.get("spaceType")
        if space_type == "DIRECT_MESSAGE" and not include_dms:
            continue
        if _is_dormant(space, since_dt):
            continue
        # RAW FETCH (R8): _list_messages_sync returns the unstripped messages.list
        # payload, preserving annotations + name that detection requires.
        messages = await asyncio.to_thread(
            gchat._list_messages_sync, creds, space.get("name"), start_iso, end_iso
        )
        for message in messages:
            trigger = _classify(message, space_type, me)
            if trigger is None:
                continue
            # T1.5: a message I authored is never "for me". Drop self-sent
            # items across ALL triggers (a self-mention or a DM I sent myself
            # still isn't something needing my attention). Reuse the same `me`
            # id resolved for mention matching — one source of truth, and the
            # sender is already in the fetched payload, so no extra API call.
            if (message.get("sender") or {}).get("name") == me:
                continue
            items.append(_normalize(message, space, trigger, creds))

    items.sort(key=lambda it: it.get("created_time") or "", reverse=True)
    return items
