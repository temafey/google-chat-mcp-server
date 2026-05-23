"""Unit tests for the write-side MCP tools.

These tests never touch the network or a real token. The googleapiclient
discovery `build()` function is patched to return a MagicMock so the
`spaces().messages().create().execute()` chain can be stubbed.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

import google_chat


@pytest.fixture(autouse=True)
def _fake_credentials():
    """Bypass real OAuth — pretend get_credentials() always succeeds."""
    with patch.object(google_chat, "get_credentials", return_value=MagicMock(name="creds")):
        yield


@pytest.fixture
def fake_service():
    """Patch build() and return the mock service plus the leaf execute() mock."""
    service = MagicMock(name="chat_service")
    messages_create = service.spaces.return_value.messages.return_value.create
    messages_create.return_value.execute.return_value = {
        "name": "spaces/AAAA/messages/MMMM",
        "thread": {"name": "spaces/AAAA/threads/TTTT"},
    }
    media_upload = service.media.return_value.upload
    media_upload.return_value.execute.return_value = {
        "attachmentDataRef": {"attachmentUploadRef": {"attachmentName": "uploads/abc"}}
    }
    with patch.object(google_chat, "build", return_value=service):
        yield service


def _make_http_error(status: int, message: str = "boom") -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "ERR"
    body = json.dumps({"error": {"code": status, "message": message}}).encode("utf-8")
    return HttpError(resp=resp, content=body)


# --- send_message ----------------------------------------------------------


async def test_send_message_rejects_bad_space():
    with pytest.raises(ValueError):
        await google_chat.send_message("not-a-space", "hi")


async def test_send_message_rejects_thread_not_under_space():
    with pytest.raises(ValueError):
        await google_chat.send_message(
            "spaces/AAAA",
            "hi",
            thread_name="spaces/BBBB/threads/T",
        )


async def test_send_message_rejects_oversized_text(fake_service):
    big = "x" * (google_chat.MAX_MESSAGE_TEXT_BYTES + 1)
    with pytest.raises(ValueError):
        await google_chat.send_message("spaces/AAAA", big)
    fake_service.spaces.return_value.messages.return_value.create.assert_not_called()


async def test_send_message_happy_path(fake_service):
    result = await google_chat.send_message("spaces/AAAA", "hello")
    assert result == {
        "name": "spaces/AAAA/messages/MMMM",
        "thread": {"name": "spaces/AAAA/threads/TTTT"},
    }
    create = fake_service.spaces.return_value.messages.return_value.create
    create.assert_called_once()
    kwargs = create.call_args.kwargs
    assert kwargs["parent"] == "spaces/AAAA"
    assert kwargs["body"]["text"] == "hello"
    assert "thread" not in kwargs["body"]
    assert "messageReplyOption" not in kwargs


async def test_send_message_thread_reply_uses_or_fail(fake_service):
    await google_chat.send_message(
        "spaces/AAAA",
        "hi",
        thread_name="spaces/AAAA/threads/TTTT",
    )
    kwargs = fake_service.spaces.return_value.messages.return_value.create.call_args.kwargs
    assert kwargs["body"]["thread"] == {"name": "spaces/AAAA/threads/TTTT"}
    assert kwargs["messageReplyOption"] == "REPLY_MESSAGE_OR_FAIL"


async def test_send_message_maps_http_error_to_dict(fake_service):
    fake_service.spaces.return_value.messages.return_value.create.return_value.execute.side_effect = (
        _make_http_error(403, "forbidden")
    )
    result = await google_chat.send_message("spaces/AAAA", "hi")
    assert result == {"error": "forbidden", "status": 403}


# --- upload_attachment -----------------------------------------------------


async def test_upload_attachment_rejects_missing_file(tmp_path):
    google_chat.set_upload_dir(str(tmp_path))
    with pytest.raises(FileNotFoundError):
        await google_chat.upload_attachment(
            "spaces/AAAA", str(tmp_path / "nope.png")
        )


async def test_upload_attachment_rejects_path_outside_upload_dir(tmp_path):
    google_chat.set_upload_dir(str(tmp_path))
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x")
    try:
        with pytest.raises(ValueError):
            await google_chat.upload_attachment("spaces/AAAA", str(outside))
    finally:
        outside.unlink(missing_ok=True)


async def test_upload_attachment_happy_path(tmp_path, fake_service):
    google_chat.set_upload_dir(str(tmp_path))
    f = tmp_path / "report.txt"
    f.write_text("hello")

    result = await google_chat.upload_attachment(
        "spaces/AAAA", str(f), text="caption"
    )
    assert result == {
        "name": "spaces/AAAA/messages/MMMM",
        "thread": {"name": "spaces/AAAA/threads/TTTT"},
    }

    upload = fake_service.media.return_value.upload
    upload.assert_called_once()
    upload_kwargs = upload.call_args.kwargs
    assert upload_kwargs["parent"] == "spaces/AAAA"
    assert upload_kwargs["body"] == {"filename": "report.txt"}

    create = fake_service.spaces.return_value.messages.return_value.create
    create_kwargs = create.call_args.kwargs
    assert create_kwargs["parent"] == "spaces/AAAA"
    assert create_kwargs["body"]["text"] == "caption"
    # The whole upload response dict must be passed through unwrapped.
    assert create_kwargs["body"]["attachment"] == [
        {"attachmentDataRef": {"attachmentUploadRef": {"attachmentName": "uploads/abc"}}}
    ]


async def test_upload_attachment_maps_upload_http_error(tmp_path, fake_service):
    google_chat.set_upload_dir(str(tmp_path))
    f = tmp_path / "report.txt"
    f.write_text("hi")
    fake_service.media.return_value.upload.return_value.execute.side_effect = (
        _make_http_error(404, "space not found")
    )
    result = await google_chat.upload_attachment("spaces/AAAA", str(f))
    assert result == {"error": "space not found", "status": 404}
    # messages.create must not be called if media.upload failed.
    fake_service.spaces.return_value.messages.return_value.create.assert_not_called()


async def test_upload_attachment_thread_reply_uses_or_fail(tmp_path, fake_service):
    google_chat.set_upload_dir(str(tmp_path))
    f = tmp_path / "a.txt"
    f.write_text("hi")
    await google_chat.upload_attachment(
        "spaces/AAAA",
        str(f),
        thread_name="spaces/AAAA/threads/TTTT",
    )
    create_kwargs = fake_service.spaces.return_value.messages.return_value.create.call_args.kwargs
    assert create_kwargs["body"]["thread"] == {"name": "spaces/AAAA/threads/TTTT"}
    assert create_kwargs["messageReplyOption"] == "REPLY_MESSAGE_OR_FAIL"
