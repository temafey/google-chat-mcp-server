"""Guards for the shared googleapiclient Resource layer in google_chat.

Background: googleapiclient materializes sub-resources lazily, and each
``service.spaces().messages()`` regenerates that resource's methods, schemas and
a 731 KB ``__doc__`` — 20 MB and 78 ms per call. That accessor used to sit inside
the pagination loops, so a 213-space search peaked at 1.5 GB of Python objects /
1.9 GB RSS. These tests pin the two properties that keep it fixed:

  1. the sub-resource is materialized ONCE, not once per page;
  2. every request executes through ``_exec``, which supplies a per-thread HTTP
     transport (the Resource is shared; its connection pool must not be).
"""
from __future__ import annotations

import ast
import pathlib
import sys
import threading
from unittest.mock import MagicMock, patch

import google_auth_httplib2
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import google_chat  # noqa: E402


def _chat_service_mock(pages):
    """MagicMock chat service whose messages.list().execute() walks *pages*."""
    service = MagicMock()
    service.spaces.return_value.messages.return_value.list.return_value.execute.side_effect = pages
    return service


# --------------------------------------------------------------------------
# 1. The regression guard: the 20 MB accessor must not run per page.
# --------------------------------------------------------------------------

def test_sub_resource_materialized_once_not_once_per_page():
    """Two pages must cost two .list() calls but only ONE .messages() walk.

    This is the test that would have caught the original bug: before the fix
    ``service.spaces().messages()`` ran inside the while-loop, so both counts
    tracked the page count.
    """
    service = _chat_service_mock([
        {"messages": [{"name": "m1"}], "nextPageToken": "tok"},
        {"messages": [{"name": "m2"}]},
    ])

    with patch.object(google_chat, "build", return_value=service):
        out = google_chat._list_messages_sync(MagicMock(), "spaces/A", None, None)

    assert [m["name"] for m in out] == ["m1", "m2"]

    listed = service.spaces.return_value.messages.return_value.list
    assert listed.call_count == 2, "both pages should still be fetched"
    assert service.spaces.call_count == 1, "spaces() re-walked per page"
    assert service.spaces.return_value.messages.call_count == 1, (
        "messages() re-materialized per page — this is the 20 MB/page leak"
    )


def test_resource_is_cached_across_calls_and_reset_rebuilds():
    service = MagicMock()
    # Single-page response, reusable for any number of calls.
    service.spaces.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": []
    }

    with patch.object(google_chat, "build", return_value=service) as build_mock:
        google_chat._list_messages_sync(MagicMock(), "spaces/A", None, None)
        google_chat._list_messages_sync(MagicMock(), "spaces/B", None, None)
        assert build_mock.call_count == 1, "the service should be built once per process"

        google_chat.reset_service_cache()
        google_chat._list_messages_sync(MagicMock(), "spaces/C", None, None)
        assert build_mock.call_count == 2, "reset_service_cache() must force a rebuild"


# --------------------------------------------------------------------------
# 2. Every request goes through _exec (the thread-safety invariant).
# --------------------------------------------------------------------------

@pytest.mark.parametrize("module_name", ["google_chat", "google_calendar"])
def test_no_bare_execute_calls(module_name):
    """Static guard: no ``.execute()`` without an explicit ``http=``.

    The cached Resource is credential-free, so a bare .execute() is both
    unauthenticated (hard 401) and racy (shared connection pool across the 24
    to_thread workers). Rather than trust discipline, forbid it in the AST.
    """
    path = pathlib.Path(__file__).resolve().parent.parent / f"{module_name}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
        and not any(kw.arg == "http" for kw in node.keywords)
    ]

    assert offenders == [], (
        f"bare .execute() at {module_name}.py:{offenders} — every request must go "
        "through _exec(request, creds) to get a per-thread transport"
    )


def test_exec_binds_current_creds_and_reuses_the_thread_transport():
    """Creds are bound per call (never stale); the transport is per thread."""
    creds1, creds2 = MagicMock(), MagicMock()
    request = MagicMock()

    google_chat._exec(request, creds1)
    google_chat._exec(request, creds2)

    http1 = request.execute.call_args_list[0].kwargs["http"]
    http2 = request.execute.call_args_list[1].kwargs["http"]

    assert http1.credentials is creds1
    assert http2.credentials is creds2, (
        "second call reused the first call's credentials — token reload would go unseen"
    )
    assert http1.http is http2.http, "the httplib2 connection pool should be reused"


def test_exec_rejects_missing_credentials():
    with pytest.raises(ValueError, match="credentials are required"):
        google_chat._exec(MagicMock(), None)


def test_cached_resource_carries_no_credentials():
    """The shared Resource must be built on an UNAUTHENTICATED transport.

    Note this deliberately does NOT assert ``_http.credentials is None`` — an
    httplib2.Http always exposes its own (empty) httplib2.Credentials store, so
    that assertion would pass vacuously today and fail confusingly tomorrow.
    What matters is that it is not an AuthorizedHttp carrying a user's token.
    """
    res = google_chat._resource("chat", "v1", "spaces", "messages")
    assert not isinstance(res._http, google_auth_httplib2.AuthorizedHttp)


# --------------------------------------------------------------------------
# 3. Concurrency.
# --------------------------------------------------------------------------

def test_concurrent_cold_cache_does_not_deadlock():
    """_resource() recurses into itself on a miss, while holding the lock.

    With a plain threading.Lock that self-deadlocks the first time two spaces are
    scanned in parallel on a cold cache — invisible to single-threaded tests, and
    it wedges the server. join(timeout=...) turns a hang into a FAILURE.
    """
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def work():
        try:
            barrier.wait(timeout=10)  # maximize the odds of a real race
            google_chat._resource("chat", "v1", "spaces", "messages")
        except BaseException as exc:  # noqa: BLE001 - the assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not any(t.is_alive() for t in threads), "deadlock in _resource()"
    assert errors == []


def test_thread_transports_are_not_shared_across_threads():
    """The Resource is shared; the httplib2 connection pool must NOT be."""
    transports: dict[int, object] = {}

    def work():
        transports[threading.get_ident()] = google_chat._thread_http()

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(transports) == 4
    assert len({id(h) for h in transports.values()}) == 4, (
        "worker threads shared one httplib2 transport — not thread-safe"
    )
