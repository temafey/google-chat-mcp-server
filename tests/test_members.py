"""Unit tests for list_space_members and find_users_by_name."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

import google_chat


@pytest.fixture(autouse=True)
def _fake_credentials():
    with patch.object(google_chat, "get_credentials", return_value=MagicMock(name="creds")):
        yield


@pytest.fixture(autouse=True)
def _reset_caches():
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

# Two humans in space A, one of whom is also in B; plus a bot in A;
# plus a group-membership row (no `member` field, only `groupMember`) we should
# skip.
MEMBERS_A = [
    {"name": "spaces/AAA/members/m1", "state": "JOINED", "role": "ROLE_MEMBER",
     "member": {"name": "users/ARTEM", "displayName": "Artem Onyshchenko", "type": "HUMAN"}},
    {"name": "spaces/AAA/members/m2", "state": "JOINED", "role": "ROLE_MEMBER",
     "member": {"name": "users/VASYA", "displayName": "Vasya Pupkin", "type": "HUMAN"}},
    {"name": "spaces/AAA/members/m3", "state": "JOINED", "role": "ROLE_MEMBER",
     "member": {"name": "users/BOT1", "displayName": "Helper Bot", "type": "BOT"}},
    {"name": "spaces/AAA/members/m4", "state": "JOINED", "role": "ROLE_MEMBER",
     "groupMember": {"name": "groups/ENG"}},
]
MEMBERS_B = [
    {"name": "spaces/BBB/members/m5", "state": "JOINED", "role": "ROLE_MEMBER",
     "member": {"name": "users/ARTEM", "displayName": "Artem Onyshchenko", "type": "HUMAN"}},
    {"name": "spaces/BBB/members/m6", "state": "JOINED", "role": "ROLE_MEMBER",
     "member": {"name": "users/JOHN", "displayName": "John Doe", "type": "HUMAN"}},
]


# --- list_space_members ----------------------------------------------------


async def test_list_space_members_happy_path():
    with patch.object(google_chat, "_list_space_members_sync", return_value=MEMBERS_A):
        result = await google_chat.list_space_members("spaces/AAA")

    assert result["space_name"] == "spaces/AAA"
    # 3 individual members (Artem, Vasya, Bot1) — group row is skipped.
    assert result["member_count"] == 3
    ids = {m["user_id"] for m in result["members"]}
    assert ids == {"users/ARTEM", "users/VASYA", "users/BOT1"}
    # Side effect — display names are cached so search results show real names.
    assert google_chat._user_display_name_cache["users/ARTEM"] == "Artem Onyshchenko"
    assert google_chat._user_display_name_cache["users/BOT1"] == "Helper Bot"


async def test_list_space_members_rejects_bad_space_name():
    with pytest.raises(ValueError):
        await google_chat.list_space_members("not-a-space")


async def test_list_space_members_returns_error_dict_on_403():
    with patch.object(google_chat, "_list_space_members_sync",
                      side_effect=_make_http_error(403, "scope missing")):
        result = await google_chat.list_space_members("spaces/AAA")

    assert result == {
        "space_name": "spaces/AAA",
        "error": "scope missing",
        "status": 403,
        "members": [],
    }


# --- find_users_by_name ----------------------------------------------------


async def test_find_users_by_name_dedups_across_spaces():
    side = lambda creds, name: {"spaces/AAA": MEMBERS_A, "spaces/BBB": MEMBERS_B}[name]
    with patch.object(google_chat, "list_chat_spaces", return_value=[SPACE_A, SPACE_B]), \
         patch.object(google_chat, "_list_space_members_sync", side_effect=side):
        result = await google_chat.find_users_by_name("artem")

    assert result["query"] == "artem"
    assert result["spaces_scanned"] == 2
    assert result["spaces_failed"] == 0
    assert result["match_count"] == 1
    match = result["matches"][0]
    assert match["user_id"] == "users/ARTEM"
    assert match["display_name"] == "Artem Onyshchenko"
    # Artem is in both spaces — both reported.
    assert {sp["name"] for sp in match["spaces"]} == {"spaces/AAA", "spaces/BBB"}


async def test_find_users_by_name_substring_is_case_insensitive():
    with patch.object(google_chat, "list_chat_spaces", return_value=[SPACE_A]), \
         patch.object(google_chat, "_list_space_members_sync", return_value=MEMBERS_A):
        result = await google_chat.find_users_by_name("PUPKIN")
    assert result["match_count"] == 1
    assert result["matches"][0]["user_id"] == "users/VASYA"


async def test_find_users_by_name_collects_per_space_errors():
    def side(creds, name):
        if name == "spaces/AAA":
            raise _make_http_error(403, "scope missing")
        return MEMBERS_B

    with patch.object(google_chat, "list_chat_spaces", return_value=[SPACE_A, SPACE_B]), \
         patch.object(google_chat, "_list_space_members_sync", side_effect=side):
        result = await google_chat.find_users_by_name("artem")

    assert result["spaces_failed"] == 1
    assert result["errors"][0] == {
        "space_name": "spaces/AAA", "error": "scope missing", "status": 403
    }
    # Still found Artem in space B.
    assert result["match_count"] == 1


async def test_find_users_by_name_validates_query():
    with pytest.raises(ValueError):
        await google_chat.find_users_by_name("   ")


async def test_find_users_by_name_with_explicit_space_skips_listing():
    list_spaces = MagicMock()
    with patch.object(google_chat, "list_chat_spaces", side_effect=list_spaces), \
         patch.object(google_chat, "_list_space_members_sync", return_value=MEMBERS_A):
        result = await google_chat.find_users_by_name(
            "vasya", space_names=["spaces/AAA"]
        )

    list_spaces.assert_not_called()
    assert result["spaces_scanned"] == 1
    assert result["match_count"] == 1


# --- get_user_display_name (cache-only behavior) ---------------------------


async def test_get_user_display_name_uses_cache_then_inline_then_id():
    google_chat._user_display_name_cache["users/CACHED"] = "Cached Name"
    # 1) cache hit
    assert google_chat.get_user_display_name({"name": "users/CACHED"}) == "Cached Name"
    # 2) inline displayName
    assert google_chat.get_user_display_name(
        {"name": "users/X", "displayName": "Inline Name"}
    ) == "Inline Name"
    # 3) BOT fallback (synthesized)
    name = google_chat.get_user_display_name({"name": "users/BOT123", "type": "BOT"})
    assert name.startswith("Bot (")
    # 4) Human with no info → returns user_id
    assert google_chat.get_user_display_name({"name": "users/UNK"}) == "users/UNK"
