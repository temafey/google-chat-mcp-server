"""Unit tests for mentions_core.list_messages_for_me.

The Google Chat API layer is fully mocked — ``get_credentials``,
``_resolve_me_sync``, ``list_chat_spaces`` and the raw ``_list_messages_sync``
fetch are patched, so no network calls happen. These tests pin the detection
rule (R4), the dormant-space skip (R3), and — crucially — that detection reads
mention ``annotations`` from the *full-field* payload, NOT the stripped shape
``list_space_messages`` would produce under SAVE_TOKEN_MODE (R8).
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import google_chat as gchat
import mentions_core

ME = "users/ME"
OTHER = "users/OTHER"

# --- spaces --------------------------------------------------------------
SPACE_TEAM = {
    "name": "spaces/TEAM",
    "spaceType": "SPACE",
    "displayName": "Backend errors",
    "lastActiveTime": "2026-06-04T12:00:00Z",
}
SPACE_DM = {
    "name": "spaces/DM",
    "spaceType": "DIRECT_MESSAGE",
    "displayName": None,
    "lastActiveTime": "2026-06-04T12:00:00Z",
}
SPACE_DORMANT = {
    "name": "spaces/OLD",
    "spaceType": "SPACE",
    "displayName": "Stale room",
    "lastActiveTime": "2026-05-01T00:00:00Z",
}

START = dt.datetime(2026, 6, 4, 0, 0, tzinfo=dt.timezone.utc)
END = dt.datetime(2026, 6, 5, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(autouse=True)
def _reset_display_name_cache():
    gchat._user_display_name_cache.clear()
    yield
    gchat._user_display_name_cache.clear()


def _mention_ann(user_name: str, utype: str = "HUMAN", mtype: str = "MENTION") -> dict:
    return {
        "type": "USER_MENTION",
        "startIndex": 0,
        "length": 6,
        "userMention": {
            "user": {"name": user_name, "type": utype, "displayName": user_name},
            "type": mtype,
        },
    }


def _broadcast_ann() -> dict:
    # Assumed shape for an @all/@here room-wide mention (see module docstring —
    # not confirmed against a live message).
    return {
        "type": "USER_MENTION",
        "startIndex": 0,
        "length": 4,
        "userMention": {"user": {"name": "users/all", "type": "ALL"}, "type": "MENTION"},
    }


def _msg(mid: str, space: str, text: str, *, sender: str = OTHER,
         annotations=None, thread: str = "T1", created: str = "2026-06-04T11:00:00Z") -> dict:
    """A FULL-FIELD message as returned by the raw messages.list payload."""
    return {
        "name": f"{space}/messages/{mid}",
        "createTime": created,
        "text": text,
        "sender": {"name": sender, "type": "HUMAN", "displayName": sender},
        "thread": {"name": f"{space}/threads/{thread}"},
        "annotations": annotations or [],
    }


@contextmanager
def _patched(spaces, messages_by_space, *, me=ME, recorder=None):
    def fetch(creds, name, start_iso, end_iso):
        if recorder is not None:
            recorder.append(name)
        return messages_by_space.get(name, [])

    with patch.object(gchat, "get_credentials", return_value=MagicMock(name="creds")), \
         patch.object(gchat, "_resolve_me_sync", return_value=me), \
         patch.object(gchat, "list_chat_spaces", return_value=list(spaces)), \
         patch.object(gchat, "_list_messages_sync", side_effect=fetch):
        yield


async def test_direct_mention_in_space_detected():
    msgs = {"spaces/TEAM": [_msg("M1", "spaces/TEAM", "@Artem look here",
                                 annotations=[_mention_ann(ME)])]}
    with _patched([SPACE_TEAM], msgs):
        items = await mentions_core.list_messages_for_me(START, END)

    assert len(items) == 1
    assert items[0]["trigger"] == "user_mention"
    assert items[0]["message_name"] == "spaces/TEAM/messages/M1"
    assert items[0]["space_type"] == "SPACE"
    # Full normalized shape (store.json item field names).
    assert set(items[0]) == {
        "space_name", "space_display", "space_type", "message_name",
        "thread_name", "sender_id", "sender_name", "created_time", "text",
        "quoted", "trigger",
    }


async def test_broadcast_all_detected():
    msgs = {"spaces/TEAM": [_msg("M2", "spaces/TEAM", "@all standup now",
                                 annotations=[_broadcast_ann()])]}
    with _patched([SPACE_TEAM], msgs):
        items = await mentions_core.list_messages_for_me(START, END)

    assert len(items) == 1
    assert items[0]["trigger"] == "broadcast"


async def test_someone_elses_mention_not_detected():
    msgs = {"spaces/TEAM": [_msg("M3", "spaces/TEAM", "@Bob can you check",
                                 annotations=[_mention_ann(OTHER)])]}
    with _patched([SPACE_TEAM], msgs):
        items = await mentions_core.list_messages_for_me(START, END)

    assert items == []


async def test_add_membership_annotation_is_not_a_mention():
    """A userMention with type ADD is a membership event (I was ADDED to the
    space), NOT an @mention of me — it must not surface as a triage item."""
    msgs = {"spaces/TEAM": [_msg("M3a", "spaces/TEAM", "added you to the room",
                                 annotations=[_mention_ann(ME, mtype="ADD")])]}
    with _patched([SPACE_TEAM], msgs):
        items = await mentions_core.list_messages_for_me(START, END)

    assert items == []


async def test_real_mention_alongside_add_still_detected():
    """An ADD annotation does not mask a genuine @mention in the same message."""
    msgs = {"spaces/TEAM": [_msg("M3b", "spaces/TEAM", "welcome @Artem",
                                 annotations=[_mention_ann(OTHER, mtype="ADD"),
                                              _mention_ann(ME, mtype="MENTION")])]}
    with _patched([SPACE_TEAM], msgs):
        items = await mentions_core.list_messages_for_me(START, END)

    assert len(items) == 1
    assert items[0]["trigger"] == "user_mention"


async def test_unspecified_mention_type_treated_as_mention():
    """A missing/empty userMention.type is treated permissively as a mention
    (forward-compatible — only ADD is excluded)."""
    msgs = {"spaces/TEAM": [_msg("M3c", "spaces/TEAM", "@Artem ping",
                                 annotations=[_mention_ann(ME, mtype="")])]}
    with _patched([SPACE_TEAM], msgs):
        items = await mentions_core.list_messages_for_me(START, END)

    assert len(items) == 1
    assert items[0]["trigger"] == "user_mention"


async def test_direct_message_detected():
    msgs = {"spaces/DM": [_msg("M4", "spaces/DM", "hey, free for a call?")]}
    with _patched([SPACE_DM], msgs):
        items = await mentions_core.list_messages_for_me(START, END)

    assert len(items) == 1
    assert items[0]["trigger"] == "direct_dm"
    assert items[0]["space_type"] == "DIRECT_MESSAGE"


async def test_unmentioned_busy_room_message_not_detected():
    msgs = {"spaces/TEAM": [
        _msg("M5", "spaces/TEAM", "deploy is green"),
        _msg("M6", "spaces/TEAM", "thanks team"),
    ]}
    with _patched([SPACE_TEAM], msgs):
        items = await mentions_core.list_messages_for_me(START, END)

    assert items == []


async def test_dormant_space_skipped_and_not_fetched():
    fetched: list[str] = []
    msgs = {
        "spaces/TEAM": [_msg("M1", "spaces/TEAM", "@me", annotations=[_mention_ann(ME)])],
        "spaces/OLD": [_msg("M9", "spaces/OLD", "@me", annotations=[_mention_ann(ME)])],
    }
    since = dt.datetime(2026, 6, 1, 0, 0, tzinfo=dt.timezone.utc)
    with _patched([SPACE_TEAM, SPACE_DORMANT], msgs, recorder=fetched):
        items = await mentions_core.list_messages_for_me(START, END, since=since)

    # The dormant space was never fetched...
    assert "spaces/OLD" not in fetched
    assert "spaces/TEAM" in fetched
    # ...so its (would-be) mention never surfaces.
    assert [it["space_name"] for it in items] == ["spaces/TEAM"]


async def test_detection_requires_full_field_annotations():
    """R8 crux: the same logical message is detected when annotations are present
    (raw full-field payload) and dropped when stripped to the SAVE_TOKEN_MODE
    shape ``{sender, createTime, text, thread}`` — proving detection depends on
    the raw fetch path, not ``list_space_messages``."""
    full = _msg("M1", "spaces/TEAM", "@Artem ping", annotations=[_mention_ann(ME)])
    stripped = {
        "sender": full["sender"],
        "createTime": full["createTime"],
        "text": full["text"],
        "thread": full["thread"],
    }

    with _patched([SPACE_TEAM], {"spaces/TEAM": [full]}):
        full_items = await mentions_core.list_messages_for_me(START, END)
    with _patched([SPACE_TEAM], {"spaces/TEAM": [stripped]}):
        stripped_items = await mentions_core.list_messages_for_me(START, END)

    assert len(full_items) == 1 and full_items[0]["trigger"] == "user_mention"
    assert stripped_items == []  # no annotations → invisible to detection


async def test_self_authored_dm_message_dropped():
    """T1.5: a DM message I sent myself is never a triage item."""
    msgs = {"spaces/DM": [_msg("M10", "spaces/DM", "note to self", sender=ME)]}
    with _patched([SPACE_DM], msgs):
        items = await mentions_core.list_messages_for_me(START, END)

    assert items == []


async def test_other_party_dm_message_kept():
    """The other participant's DM message still surfaces as direct_dm."""
    msgs = {"spaces/DM": [_msg("M11", "spaces/DM", "ping?", sender=OTHER)]}
    with _patched([SPACE_DM], msgs):
        items = await mentions_core.list_messages_for_me(START, END)

    assert len(items) == 1
    assert items[0]["trigger"] == "direct_dm"
    assert items[0]["sender_id"] == OTHER


async def test_self_authored_space_message_dropped_even_with_self_mention():
    """T1.5: a SPACE message I sent is dropped even if it @mentions me."""
    msgs = {"spaces/TEAM": [_msg("M12", "spaces/TEAM", "@Artem reminder", sender=ME,
                                 annotations=[_mention_ann(ME)])]}
    with _patched([SPACE_TEAM], msgs):
        items = await mentions_core.list_messages_for_me(START, END)

    assert items == []


async def test_direct_mention_beats_broadcast():
    msgs = {"spaces/TEAM": [_msg("M7", "spaces/TEAM", "@all and @Artem",
                                 annotations=[_broadcast_ann(), _mention_ann(ME)])]}
    with _patched([SPACE_TEAM], msgs):
        items = await mentions_core.list_messages_for_me(START, END)

    assert len(items) == 1
    assert items[0]["trigger"] == "user_mention"


async def test_include_dms_false_skips_dm_space():
    fetched: list[str] = []
    msgs = {
        "spaces/DM": [_msg("M4", "spaces/DM", "hi")],
        "spaces/TEAM": [_msg("M1", "spaces/TEAM", "@me", annotations=[_mention_ann(ME)])],
    }
    with _patched([SPACE_DM, SPACE_TEAM], msgs, recorder=fetched):
        items = await mentions_core.list_messages_for_me(START, END, include_dms=False)

    assert "spaces/DM" not in fetched
    assert [it["trigger"] for it in items] == ["user_mention"]


async def test_space_names_filters_scan_set():
    fetched: list[str] = []
    msgs = {
        "spaces/TEAM": [_msg("M1", "spaces/TEAM", "@me", annotations=[_mention_ann(ME)])],
        "spaces/DM": [_msg("M4", "spaces/DM", "hi")],
    }
    with _patched([SPACE_TEAM, SPACE_DM], msgs, recorder=fetched):
        items = await mentions_core.list_messages_for_me(
            START, END, space_names=["spaces/TEAM"])

    assert fetched == ["spaces/TEAM"]
    assert len(items) == 1


async def test_items_sorted_newest_first():
    msgs = {"spaces/TEAM": [
        _msg("OLD", "spaces/TEAM", "@me old", annotations=[_mention_ann(ME)],
             created="2026-06-04T08:00:00Z"),
        _msg("NEW", "spaces/TEAM", "@me new", annotations=[_mention_ann(ME)],
             created="2026-06-04T18:00:00Z"),
    ]}
    with _patched([SPACE_TEAM], msgs):
        items = await mentions_core.list_messages_for_me(START, END)

    assert [it["message_name"] for it in items] == [
        "spaces/TEAM/messages/NEW", "spaces/TEAM/messages/OLD",
    ]
