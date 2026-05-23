"""Unit tests for search_chat_messages.

`_list_messages_sync` is patched to return canned results per space, so the
fan-out concurrency, sender filtering, sorting, error aggregation, and limit
clamping can all be exercised without touching the network.
"""
from __future__ import annotations

import json
import datetime as dt
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

import google_chat


@pytest.fixture(autouse=True)
def _fake_credentials():
    with patch.object(google_chat, "get_credentials", return_value=MagicMock(name="creds")):
        yield


@pytest.fixture(autouse=True)
def _reset_display_name_cache():
    google_chat._user_display_name_cache.clear()
    yield
    google_chat._user_display_name_cache.clear()


def _make_http_error(status: int, message: str = "boom") -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "ERR"
    body = json.dumps({"error": {"code": status, "message": message}}).encode("utf-8")
    return HttpError(resp=resp, content=body)


SPACE_A = {"name": "spaces/AAA", "displayName": "Alpha"}
SPACE_B = {"name": "spaces/BBB", "displayName": "Beta"}

# Two messages from "me" and one from someone else, spread across two spaces.
MSG_ME_1 = {
    "name": "spaces/AAA/messages/M1",
    "createTime": "2026-05-20T10:00:00Z",
    "text": "first from me",
    "sender": {"name": "users/ME", "type": "HUMAN"},
    "thread": {"name": "spaces/AAA/threads/T1"},
}
MSG_OTHER = {
    "name": "spaces/AAA/messages/M2",
    "createTime": "2026-05-20T11:00:00Z",
    "text": "from someone else",
    "sender": {"name": "users/OTHER", "type": "HUMAN", "displayName": "Other Person"},
    "thread": {"name": "spaces/AAA/threads/T1"},
}
MSG_ME_2 = {
    "name": "spaces/BBB/messages/M3",
    "createTime": "2026-05-21T09:00:00Z",
    "text": "second from me",
    "sender": {"name": "users/ME", "type": "HUMAN"},
    "thread": {"name": "spaces/BBB/threads/T2"},
}


async def test_search_by_id_across_two_spaces():
    with patch.object(google_chat, "list_chat_spaces", return_value=[SPACE_A, SPACE_B]), \
         patch.object(google_chat, "_list_messages_sync", side_effect=lambda creds, name, s, e: {
             "spaces/AAA": [MSG_ME_1, MSG_OTHER],
             "spaces/BBB": [MSG_ME_2],
         }[name]), \
         patch.object(google_chat, "get_user_display_name", return_value="Me"):
        result = await google_chat.search_chat_messages(
            sender="users/ME",
            start_date=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
            end_date=dt.datetime(2026, 5, 22, tzinfo=dt.timezone.utc),
        )

    assert result["spaces_scanned"] == 2
    assert result["spaces_failed"] == 0
    assert result["errors"] == []
    assert result["total_matches"] == 2
    # Sorted desc by createTime — MSG_ME_2 (21st) before MSG_ME_1 (20th).
    assert [r["message_name"] for r in result["results"]] == [
        "spaces/BBB/messages/M3",
        "spaces/AAA/messages/M1",
    ]
    assert result["results"][0]["space_display_name"] == "Beta"
    assert result["results"][0]["sender_id"] == "users/ME"


async def test_search_rejects_display_name_substring():
    """The substring path was removed — display-name search goes through
    find_users_by_name instead. Make sure a raw name is rejected loudly."""
    with pytest.raises(ValueError, match="find_users_by_name"):
        await google_chat.search_chat_messages(
            sender="Artem",
            start_date=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
        )


async def test_search_collects_per_space_errors_without_aborting():
    def side_effect(creds, name, s, e):
        if name == "spaces/AAA":
            raise _make_http_error(403, "forbidden")
        return [MSG_ME_2]

    with patch.object(google_chat, "list_chat_spaces", return_value=[SPACE_A, SPACE_B]), \
         patch.object(google_chat, "_list_messages_sync", side_effect=side_effect), \
         patch.object(google_chat, "get_user_display_name", return_value="Me"):
        result = await google_chat.search_chat_messages(
            sender="users/ME",
            start_date=dt.datetime(2026, 5, 21, tzinfo=dt.timezone.utc),
        )

    assert result["spaces_failed"] == 1
    assert result["errors"] == [
        {"space_name": "spaces/AAA", "error": "forbidden", "status": 403}
    ]
    assert result["total_matches"] == 1
    assert result["results"][0]["space_name"] == "spaces/BBB"


async def test_search_limit_caps_results():
    msgs = [
        {**MSG_ME_1, "name": f"spaces/AAA/messages/M{i}",
         "createTime": f"2026-05-2{i}T00:00:00Z"}
        for i in range(1, 6)
    ]
    with patch.object(google_chat, "list_chat_spaces", return_value=[SPACE_A]), \
         patch.object(google_chat, "_list_messages_sync", return_value=msgs), \
         patch.object(google_chat, "get_user_display_name", return_value="Me"):
        result = await google_chat.search_chat_messages(
            sender="users/ME",
            start_date=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
            end_date=dt.datetime(2026, 5, 26, tzinfo=dt.timezone.utc),
            limit=2,
        )

    assert result["total_matches"] == 2
    # Limit kept the two most recent.
    assert [r["message_name"] for r in result["results"]] == [
        "spaces/AAA/messages/M5",
        "spaces/AAA/messages/M4",
    ]


async def test_search_with_explicit_space_names_skips_list_chat_spaces():
    list_called = MagicMock()
    with patch.object(google_chat, "list_chat_spaces", side_effect=list_called), \
         patch.object(google_chat, "_list_messages_sync", return_value=[MSG_ME_1]), \
         patch.object(google_chat, "get_user_display_name", return_value="Me"):
        result = await google_chat.search_chat_messages(
            sender="users/ME",
            start_date=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
            space_names=["spaces/AAA"],
        )

    list_called.assert_not_called()
    assert result["spaces_scanned"] == 1
    assert result["total_matches"] == 1


async def test_search_me_alias_resolves_via_userinfo():
    with patch.object(google_chat, "list_chat_spaces", return_value=[SPACE_A]), \
         patch.object(google_chat, "_resolve_me_sync", return_value="users/ME"), \
         patch.object(google_chat, "_list_messages_sync", return_value=[MSG_ME_1, MSG_OTHER]), \
         patch.object(google_chat, "get_user_display_name", return_value="Me"):
        result = await google_chat.search_chat_messages(
            sender="me",
            start_date=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
        )

    assert result["sender"] == "users/ME"
    assert result["total_matches"] == 1
    assert result["results"][0]["sender_id"] == "users/ME"


async def test_search_validates_space_names_format():
    with pytest.raises(ValueError):
        await google_chat.search_chat_messages(
            sender="users/ME",
            start_date=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
            space_names=["not-a-space"],
        )
