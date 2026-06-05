"""Tests for the TelegramSender in scripts/notify.py.

No live network: the sole transport method (``TelegramSender._http_post``) is
patched in every test, so nothing here ever touches the wire. The recurring
concern is the SANITIZED failure path — the bot URL embeds the token, so neither
the token nor the ``api.telegram.org/bot<token>`` substring may ever reach
stdout/stderr.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

# scripts/ on the path so ``import notify`` / ``config`` resolve, mirroring the
# layout the sibling notify tests use.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import notify  # noqa: E402

TOKEN = "123456:AAH-secret-bot-token-DO-NOT-LEAK"
CHAT_ID = "987654321"


# --------------------------------------------------------------------------- #
# Builders.
# --------------------------------------------------------------------------- #
def _cfg(*, enabled=True, chat_id=None) -> dict:
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    tg = {"enabled": enabled}
    if chat_id is not None:
        tg["chat_id"] = chat_id
    cfg["channels"]["telegram"] = tg
    return cfg


def _secrets(*, token=TOKEN, chat_id=CHAT_ID) -> dict:
    s = {}
    if token is not None:
        s["TELEGRAM_BOT_TOKEN"] = token
    if chat_id is not None:
        s["TELEGRAM_CHAT_ID"] = chat_id
    return s


# --------------------------------------------------------------------------- #
# enabled() gating matrix.
# --------------------------------------------------------------------------- #
def test_enabled_false_when_channel_off():
    sender = notify.TelegramSender(_cfg(enabled=False), secrets=_secrets())
    assert sender.enabled(_cfg(enabled=False)) is False


def test_enabled_false_when_no_token():
    cfg = _cfg(enabled=True)
    sender = notify.TelegramSender(cfg, secrets=_secrets(token=None))
    assert sender.enabled(cfg) is False


def test_enabled_false_when_no_chat_id():
    cfg = _cfg(enabled=True)
    sender = notify.TelegramSender(cfg, secrets=_secrets(chat_id=None))
    assert sender.enabled(cfg) is False


def test_enabled_true_when_channel_on_token_and_chat_id():
    cfg = _cfg(enabled=True)
    sender = notify.TelegramSender(cfg, secrets=_secrets())
    assert sender.enabled(cfg) is True


def test_chat_id_falls_back_to_config_when_not_in_secrets():
    cfg = _cfg(enabled=True, chat_id="from-config")
    sender = notify.TelegramSender(cfg, secrets=_secrets(chat_id=None))
    assert sender.enabled(cfg) is True
    assert sender._chat_id == "from-config"


def test_secrets_chat_id_wins_over_config():
    cfg = _cfg(enabled=True, chat_id="from-config")
    sender = notify.TelegramSender(cfg, secrets=_secrets(chat_id="from-secrets"))
    assert sender._chat_id == "from-secrets"


# --------------------------------------------------------------------------- #
# send() success — body shape, plain text, no parse_mode.
# --------------------------------------------------------------------------- #
def test_send_success_returns_true_with_correct_body(monkeypatch):
    captured = {}

    def fake_post(self, url, payload):
        captured["url"] = url
        captured["payload"] = payload
        return {"ok": True, "result": {"message_id": 1}}

    monkeypatch.setattr(notify.TelegramSender, "_http_post", fake_post)
    sender = notify.TelegramSender(_cfg(), secrets=_secrets())

    assert sender.send("hello digest", []) is True
    assert captured["payload"]["chat_id"] == CHAT_ID
    assert captured["payload"]["text"] == "hello digest"
    assert captured["payload"]["disable_web_page_preview"] is True
    assert "parse_mode" not in captured["payload"]


# --------------------------------------------------------------------------- #
# send() failure modes — never raises, always False.
# --------------------------------------------------------------------------- #
def test_send_returns_false_on_ok_false(monkeypatch):
    def fake_post(self, url, payload):
        return {"ok": False, "description": "chat not found"}

    monkeypatch.setattr(notify.TelegramSender, "_http_post", fake_post)
    sender = notify.TelegramSender(_cfg(), secrets=_secrets())
    assert sender.send("digest", []) is False


def test_send_returns_false_on_exception(monkeypatch):
    def fake_post(self, url, payload):
        raise TimeoutError("timed out")

    monkeypatch.setattr(notify.TelegramSender, "_http_post", fake_post)
    sender = notify.TelegramSender(_cfg(), secrets=_secrets())
    # Must NOT raise.
    assert sender.send("digest", []) is False


def test_send_returns_false_on_non_dict_body(monkeypatch):
    def fake_post(self, url, payload):
        return "not json"

    monkeypatch.setattr(notify.TelegramSender, "_http_post", fake_post)
    sender = notify.TelegramSender(_cfg(), secrets=_secrets())
    assert sender.send("digest", []) is False


# --------------------------------------------------------------------------- #
# Token-leak guard — the token / bot-URL must NEVER reach stdout or stderr.
# --------------------------------------------------------------------------- #
def test_failure_via_exception_does_not_leak_token(monkeypatch, capsys):
    def fake_post(self, url, payload):
        # An exception whose text embeds the URL+token — the WORST case.
        raise RuntimeError(f"HTTP 401 for {url}")

    monkeypatch.setattr(notify.TelegramSender, "_http_post", fake_post)
    sender = notify.TelegramSender(_cfg(), secrets=_secrets())

    assert sender.send("digest", []) is False
    out = capsys.readouterr()
    blob = out.out + out.err
    assert TOKEN not in blob
    assert f"api.telegram.org/bot{TOKEN}" not in blob
    assert "api.telegram.org/bot" not in blob


def test_failure_via_ok_false_does_not_leak_token(monkeypatch, capsys):
    def fake_post(self, url, payload):
        return {"ok": False, "description": "Unauthorized"}

    monkeypatch.setattr(notify.TelegramSender, "_http_post", fake_post)
    sender = notify.TelegramSender(_cfg(), secrets=_secrets())

    assert sender.send("digest", []) is False
    out = capsys.readouterr()
    blob = out.out + out.err
    assert TOKEN not in blob
    assert "api.telegram.org/bot" not in blob


# --------------------------------------------------------------------------- #
# Truncation — >4000 chars is trimmed in the POST body.
# --------------------------------------------------------------------------- #
def test_long_summary_truncated_to_4000(monkeypatch):
    captured = {}

    def fake_post(self, url, payload):
        captured["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(notify.TelegramSender, "_http_post", fake_post)
    sender = notify.TelegramSender(_cfg(), secrets=_secrets())

    long_summary = "x" * 5000
    assert sender.send(long_summary, []) is True
    assert len(captured["payload"]["text"]) == 4000


# --------------------------------------------------------------------------- #
# build_senders integration — included when enabled + secrets, excluded off.
# --------------------------------------------------------------------------- #
def test_build_senders_includes_telegram_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "load_secrets", lambda *a, **k: _secrets())
    names = [s.name for s in notify.build_senders(_cfg(enabled=True))]
    assert "telegram" in names


def test_build_senders_excludes_telegram_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "load_secrets", lambda *a, **k: _secrets())
    names = [s.name for s in notify.build_senders(_cfg(enabled=False))]
    assert "telegram" not in names


def test_build_senders_excludes_telegram_when_no_secrets(monkeypatch):
    monkeypatch.setattr(config, "load_secrets", lambda *a, **k: {})
    names = [s.name for s in notify.build_senders(_cfg(enabled=True))]
    assert "telegram" not in names
