"""Unit tests for domain-directory sender-name resolution.

Covers the additive resolver in ``google_chat`` and its use by the collector and
the backfill script. Everything external is mocked — the People API ``build`` is
patched, the directory cache + store live in a tmp dir, no network and no real
``token.json`` / ``~/.claude-orchestrator`` are ever touched.

Pinned behaviours:
  * warm_directory_cache success → names land in the in-memory cache + persist;
  * pagination across nextPageToken is followed;
  * 403 / API-disabled → graceful: warning logged, no raise, raw-id fallback;
  * resolve_one_via_people_get → per-id fallback, swallows errors;
  * alias override beats a warmed directory name (highest priority);
  * collect(): a raw ``users/<id>`` becomes a real name via the directory mock;
  * backfill rewrites raw ids in a temp store and is idempotent.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT))

import google_chat  # noqa: E402
import collect_mentions  # noqa: E402
import config  # noqa: E402
import store  # noqa: E402
import backfill_sender_names  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _http_error(status: int) -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "ERR"
    body = json.dumps({"error": {"code": status, "message": "boom"}}).encode("utf-8")
    return HttpError(resp=resp, content=body)


def _person(numeric_id: str, name: str) -> dict:
    return {
        "names": [{"displayName": name}],
        "metadata": {"sources": [{"type": "DOMAIN_PROFILE", "id": numeric_id}]},
    }


def _fake_people_service(*, pages=None, get_map=None, list_error=None, get_error=None):
    """Build a MagicMock standing in for ``build('people', 'v1', ...)``.

    ``pages`` is a list of listDirectoryPeople response dicts returned in order
    (nextPageToken wiring is the caller's responsibility). ``get_map`` maps
    ``resourceName`` -> person dict for people.get.
    """
    service = MagicMock()

    list_calls = {"n": 0}

    def _list(**kwargs):
        call = MagicMock()

        def _execute():
            if list_error is not None:
                raise list_error
            idx = list_calls["n"]
            list_calls["n"] += 1
            return pages[idx] if pages and idx < len(pages) else {"people": []}

        call.execute.side_effect = _execute
        return call

    service.people.return_value.listDirectoryPeople.side_effect = _list

    def _get(resourceName=None, personFields=None):
        call = MagicMock()

        def _execute():
            if get_error is not None:
                raise get_error
            return (get_map or {}).get(resourceName, {"names": []})

        call.execute.side_effect = _execute
        return call

    service.people.return_value.get.side_effect = _get
    service._list_calls = list_calls
    return service


@pytest.fixture(autouse=True)
def _reset_caches(tmp_path, monkeypatch):
    # Isolate the in-memory caches AND the persisted directory-cache path so no
    # test ever reads/writes the real ~/.claude-orchestrator name_cache.json (a
    # fresh real cache would short-circuit warm_directory_cache and starve the
    # mocked People service).
    google_chat._user_display_name_cache.clear()
    google_chat._user_aliases.clear()
    monkeypatch.setattr(
        google_chat, "_DIRECTORY_CACHE_PATH", tmp_path / "isolated_name_cache.json"
    )
    yield
    google_chat._user_display_name_cache.clear()
    google_chat._user_aliases.clear()


# --------------------------------------------------------------------------- #
# warm_directory_cache
# --------------------------------------------------------------------------- #
def test_warm_directory_cache_success(tmp_path):
    cache = tmp_path / "name_cache.json"
    pages = [{"people": [_person("789", "Vasya Pupkin"), _person("111", "John Doe")]}]
    service = _fake_people_service(pages=pages)

    with patch.object(google_chat, "build", return_value=service):
        loaded = google_chat.warm_directory_cache(object(), cache_path=cache, force=True)

    assert loaded == 2
    assert google_chat._user_display_name_cache["users/789"] == "Vasya Pupkin"
    assert google_chat._user_display_name_cache["users/111"] == "John Doe"
    # Persisted with the expected shape.
    persisted = json.loads(cache.read_text())
    assert persisted["names"] == {"789": "Vasya Pupkin", "111": "John Doe"}
    assert "fetched_at" in persisted


def test_warm_directory_cache_paginates(tmp_path):
    cache = tmp_path / "name_cache.json"
    pages = [
        {"people": [_person("1", "Alice")], "nextPageToken": "tok"},
        {"people": [_person("2", "Bob")]},
    ]
    service = _fake_people_service(pages=pages)

    with patch.object(google_chat, "build", return_value=service):
        loaded = google_chat.warm_directory_cache(object(), cache_path=cache, force=True)

    assert loaded == 2
    assert service._list_calls["n"] == 2  # both pages fetched
    assert google_chat._user_display_name_cache["users/2"] == "Bob"


def test_warm_directory_cache_403_graceful(tmp_path):
    cache = tmp_path / "name_cache.json"
    service = _fake_people_service(list_error=_http_error(403))

    with patch.object(google_chat, "build", return_value=service):
        loaded = google_chat.warm_directory_cache(object(), cache_path=cache, force=True)

    assert loaded == 0  # no raise, degrades to nothing
    assert not cache.exists()
    assert google_chat._user_display_name_cache == {}


def test_warm_directory_cache_403_falls_back_to_stale_cache(tmp_path):
    cache = tmp_path / "name_cache.json"
    cache.write_text(json.dumps({"fetched_at": "2000-01-01T00:00:00Z", "names": {"5": "Old Name"}}))
    service = _fake_people_service(list_error=_http_error(403))

    with patch.object(google_chat, "build", return_value=service):
        loaded = google_chat.warm_directory_cache(object(), cache_path=cache, force=True)

    assert loaded == 1
    assert google_chat._user_display_name_cache["users/5"] == "Old Name"


def test_warm_directory_cache_fresh_skips_network(tmp_path):
    cache = tmp_path / "name_cache.json"
    # Freshly stamped — within the default 12h TTL.
    from datetime import datetime, timezone

    fresh = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    cache.write_text(json.dumps({"fetched_at": fresh, "names": {"9": "Cached Person"}}))
    service = _fake_people_service(pages=[{"people": []}])

    with patch.object(google_chat, "build", return_value=service) as build_mock:
        loaded = google_chat.warm_directory_cache(object(), cache_path=cache)

    assert loaded == 1
    build_mock.assert_not_called()  # fresh cache → no API call
    assert google_chat._user_display_name_cache["users/9"] == "Cached Person"


# --------------------------------------------------------------------------- #
# resolve_one_via_people_get
# --------------------------------------------------------------------------- #
def test_resolve_one_via_people_get_success():
    service = _fake_people_service(get_map={"people/789": {"names": [{"displayName": "Vasya"}]}})
    with patch.object(google_chat, "build", return_value=service):
        name = google_chat.resolve_one_via_people_get("789", object())
    assert name == "Vasya"
    assert google_chat._user_display_name_cache["users/789"] == "Vasya"


def test_resolve_one_via_people_get_error_returns_none():
    service = _fake_people_service(get_error=_http_error(404))
    with patch.object(google_chat, "build", return_value=service):
        name = google_chat.resolve_one_via_people_get("789", object())
    assert name is None
    assert "users/789" not in google_chat._user_display_name_cache


def test_resolve_one_via_people_get_no_creds():
    assert google_chat.resolve_one_via_people_get("789", None) is None


# --------------------------------------------------------------------------- #
# Alias override precedence
# --------------------------------------------------------------------------- #
def test_alias_overrides_directory_name():
    google_chat._user_display_name_cache["users/789"] = "Directory Name"
    google_chat.set_user_aliases({"users/789": "Alias Wins"})
    assert google_chat.get_user_display_name({"name": "users/789"}) == "Alias Wins"


def test_set_user_aliases_clears_previous():
    google_chat.set_user_aliases({"users/1": "First"})
    google_chat.set_user_aliases({"users/2": "Second"})
    assert "users/1" not in google_chat._user_aliases
    assert google_chat.get_user_display_name({"name": "users/2"}) == "Second"


# --------------------------------------------------------------------------- #
# Collector integration: raw id -> real name via directory mock
# --------------------------------------------------------------------------- #
def _collect_item(sender_name="users/789"):
    return {
        "space_name": "spaces/AAA",
        "space_display": "Backend",
        "space_type": "SPACE",
        "message_name": "spaces/AAA/messages/BBB",
        "thread_name": "spaces/AAA/threads/CCC",
        "sender_id": "users/789",
        "sender_name": sender_name,
        "created_time": "2026-06-04T12:00:00Z",
        "text": "ping",
        "trigger": "user_mention",
    }


def test_collect_resolves_raw_id_via_per_id_fallback(tmp_path):
    base = tmp_path / "gchat-triage"
    base.mkdir()
    cfg_path = base / "config.json"
    store_path = base / "store.json"
    cache = base / "name_cache.json"
    cfg = dict(config.DEFAULT_CONFIG)
    cfg["me_user_id"] = "users/ME"
    cfg["me_display_name"] = "Artem"
    cfg_path.write_text(json.dumps(cfg))

    # Directory warm-up returns nobody; per-id people.get resolves the sender.
    service = _fake_people_service(
        pages=[{"people": []}],
        get_map={"people/789": {"names": [{"displayName": "Vasya Pupkin"}]}},
    )

    async def _fetch(*args, **kwargs):
        return [_collect_item(sender_name="users/789")]

    with patch.object(google_chat, "_DIRECTORY_CACHE_PATH", cache), \
         patch.object(google_chat, "build", return_value=service), \
         patch.object(collect_mentions.gchat, "get_credentials", return_value=object()), \
         patch.object(collect_mentions.mentions_core, "list_messages_for_me", _fetch):
        result = collect_mentions.collect(
            config_path=cfg_path,
            store_path=store_path,
            base_dir=base,
            now="2026-06-04T13:00:00Z",
        )

    assert result["new"] == 1
    saved = store.load(store_path)
    item = next(iter(saved["items"].values()))
    assert item["sender_name"] == "Vasya Pupkin"


def test_collect_degrades_to_raw_id_when_resolution_fails(tmp_path):
    base = tmp_path / "gchat-triage"
    base.mkdir()
    cfg_path = base / "config.json"
    store_path = base / "store.json"
    cache = base / "name_cache.json"
    cfg = dict(config.DEFAULT_CONFIG)
    cfg["me_user_id"] = "users/ME"
    cfg["me_display_name"] = "Artem"
    cfg_path.write_text(json.dumps(cfg))

    service = _fake_people_service(list_error=_http_error(403), get_error=_http_error(403))

    async def _fetch(*args, **kwargs):
        return [_collect_item(sender_name="users/789")]

    with patch.object(google_chat, "_DIRECTORY_CACHE_PATH", cache), \
         patch.object(google_chat, "build", return_value=service), \
         patch.object(collect_mentions.gchat, "get_credentials", return_value=object()), \
         patch.object(collect_mentions.mentions_core, "list_messages_for_me", _fetch):
        result = collect_mentions.collect(
            config_path=cfg_path,
            store_path=store_path,
            base_dir=base,
            now="2026-06-04T13:00:00Z",
        )

    assert result["new"] == 1  # collector still ran
    saved = store.load(store_path)
    item = next(iter(saved["items"].values()))
    assert item["sender_name"] == "users/789"  # degraded to raw id


# --------------------------------------------------------------------------- #
# Backfill
# --------------------------------------------------------------------------- #
def test_backfill_rewrites_raw_ids(tmp_path):
    base = tmp_path / "gchat-triage"
    base.mkdir()
    cfg_path = base / "config.json"
    store_path = base / "store.json"
    cache = base / "name_cache.json"
    cfg = dict(config.DEFAULT_CONFIG)
    cfg_path.write_text(json.dumps(cfg))

    # Seed a store with one raw item and one already-resolved item.
    st = store._empty_store()
    store.upsert_item(st, _collect_item(sender_name="users/789"), now="2026-06-04T12:00:00Z")
    already = _collect_item(sender_name="Already Named")
    already["message_name"] = "spaces/AAA/messages/CCC"
    already["sender_id"] = "users/111"
    store.upsert_item(st, already, now="2026-06-04T12:00:00Z")
    store.save(st, store_path)

    service = _fake_people_service(
        pages=[{"people": [_person("789", "Vasya Pupkin")]}],
    )

    with patch.object(google_chat, "build", return_value=service):
        result = backfill_sender_names.backfill(
            config_path=cfg_path,
            store_path=store_path,
            creds=object(),
        )

    assert result["resolved"] == 1
    saved = store.load(store_path)
    names = {it["sender_name"] for it in saved["items"].values()}
    assert "Vasya Pupkin" in names
    assert "Already Named" in names  # untouched


def test_backfill_dry_run_does_not_write(tmp_path):
    base = tmp_path / "gchat-triage"
    base.mkdir()
    cfg_path = base / "config.json"
    store_path = base / "store.json"
    cfg = dict(config.DEFAULT_CONFIG)
    cfg_path.write_text(json.dumps(cfg))

    st = store._empty_store()
    store.upsert_item(st, _collect_item(sender_name="users/789"), now="2026-06-04T12:00:00Z")
    store.save(st, store_path)

    service = _fake_people_service(pages=[{"people": [_person("789", "Vasya Pupkin")]}])

    with patch.object(google_chat, "build", return_value=service):
        result = backfill_sender_names.backfill(
            config_path=cfg_path,
            store_path=store_path,
            creds=object(),
            dry_run=True,
        )

    assert result["resolved"] == 1
    saved = store.load(store_path)
    item = next(iter(saved["items"].values()))
    assert item["sender_name"] == "users/789"  # not written in dry-run


def test_backfill_alias_override(tmp_path):
    base = tmp_path / "gchat-triage"
    base.mkdir()
    cfg_path = base / "config.json"
    store_path = base / "store.json"
    cfg = dict(config.DEFAULT_CONFIG)
    cfg["user_aliases"] = {"users/789": "Aliased Vasya"}
    cfg_path.write_text(json.dumps(cfg))

    st = store._empty_store()
    store.upsert_item(st, _collect_item(sender_name="users/789"), now="2026-06-04T12:00:00Z")
    store.save(st, store_path)

    # Directory returns a different name; alias must win.
    service = _fake_people_service(pages=[{"people": [_person("789", "Directory Vasya")]}])

    with patch.object(google_chat, "build", return_value=service):
        backfill_sender_names.backfill(
            config_path=cfg_path,
            store_path=store_path,
            creds=object(),
        )

    saved = store.load(store_path)
    item = next(iter(saved["items"].values()))
    assert item["sender_name"] == "Aliased Vasya"
