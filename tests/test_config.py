"""Unit tests for the triage config + secrets loading layer (T1.4).

All tests are tmp-dir backed: no real ``~/.claude-orchestrator`` files are
touched. ``scripts/`` is added to ``sys.path`` so ``import config`` resolves the
module under test (scripts/ is not a package, matching repo convention).
"""
from __future__ import annotations

import copy
import json
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import config  # noqa: E402  (path tweak must precede import)


# --------------------------------------------------------------------------- #
# load_config — creation, equality with DEFAULT_CONFIG, partial-file upgrade.
# --------------------------------------------------------------------------- #
def test_load_config_no_file_creates_default(tmp_path):
    cfg_path = tmp_path / "config.json"
    assert not cfg_path.exists()

    cfg = config.load_config(cfg_path)

    assert cfg_path.exists(), "config.json should be created on first load"
    assert cfg == config.DEFAULT_CONFIG
    # And the persisted bytes round-trip to the same dict.
    assert json.loads(cfg_path.read_text()) == config.DEFAULT_CONFIG


def test_load_config_partial_file_is_upgraded_not_clobbered(tmp_path):
    """Proves T0.1 <-> T1.4 coexistence: a file with only the two identity
    fields gets the full schema merged in, preserving those two values."""
    cfg_path = tmp_path / "config.json"
    partial = {"me_user_id": "users/1", "me_display_name": "X"}
    cfg_path.write_text(json.dumps(partial))

    cfg = config.load_config(cfg_path)

    # Preserved.
    assert cfg["me_user_id"] == "users/1"
    assert cfg["me_display_name"] == "X"
    # Filled in from DEFAULT_CONFIG.
    for key, value in config.DEFAULT_CONFIG.items():
        if key in partial:
            continue
        assert cfg[key] == value
    # The merged result is the full schema (same key set as DEFAULT_CONFIG).
    assert set(cfg) == set(config.DEFAULT_CONFIG)
    # The upgraded file was persisted (re-load yields the same merged dict).
    assert json.loads(cfg_path.read_text()) == cfg


def test_load_config_nested_partial_merges_recursively(tmp_path):
    """A partial nested channel dict keeps its set value and gains the rest."""
    cfg_path = tmp_path / "config.json"
    partial = {"channels": {"telegram": {"enabled": True}}}
    cfg_path.write_text(json.dumps(partial))

    cfg = config.load_config(cfg_path)

    assert cfg["channels"]["telegram"] == {"enabled": True}
    # Siblings filled in from default.
    assert cfg["channels"]["gc_inbox"] == {"enabled": False, "space_name": None}
    assert cfg["channels"]["windows_toast"] == {"enabled": False}


def test_load_config_preserves_unknown_forward_compat_keys(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"future_flag": 42}))

    cfg = config.load_config(cfg_path)

    assert cfg["future_flag"] == 42
    assert cfg["version"] == 1  # default still present


# --------------------------------------------------------------------------- #
# quiet_hours.tz — must be a valid IANA zone constructible by zoneinfo.
# Guards against the prod crash on hosts whose only tz DB is the declared
# ``tzdata`` wheel (slim Docker, fresh checkout), and against the deprecated
# "Europe/Kiev" alias resurfacing.
# --------------------------------------------------------------------------- #
def test_default_quiet_hours_tz_constructs():
    tz = config.DEFAULT_CONFIG["quiet_hours"]["tz"]
    assert tz == "Europe/Kyiv"  # canonical name, not the deprecated "Europe/Kiev"
    assert ZoneInfo(tz)  # raises ZoneInfoNotFoundError if the tz DB is missing


# --------------------------------------------------------------------------- #
# save_config — atomic round-trip.
# --------------------------------------------------------------------------- #
def test_save_config_round_trip(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    cfg["me_user_id"] = "users/999"

    config.save_config(cfg, cfg_path)

    assert json.loads(cfg_path.read_text()) == cfg
    # No leftover temp files in the directory.
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


# --------------------------------------------------------------------------- #
# load_secrets — KEY=VALUE parsing, missing file, comment skipping.
# --------------------------------------------------------------------------- #
def test_load_secrets_missing_file_returns_empty(tmp_path):
    assert config.load_secrets(tmp_path / "secrets.env") == {}


def test_load_secrets_parses_key_value(tmp_path):
    secrets_path = tmp_path / "secrets.env"
    secrets_path.write_text(
        "# a comment\n"
        "\n"
        "TELEGRAM_BOT_TOKEN=abc123\n"
        "TELEGRAM_CHAT_ID = 4567 \n"  # surrounding whitespace trimmed
        "# TELEGRAM_DISABLED=should_be_ignored\n"
    )

    secrets = config.load_secrets(secrets_path)

    assert secrets == {"TELEGRAM_BOT_TOKEN": "abc123", "TELEGRAM_CHAT_ID": "4567"}


def test_load_secrets_fresh_placeholder_file_is_empty(tmp_path):
    """A freshly created secrets.env has only commented placeholders -> {}."""
    cfg_path = tmp_path / "config.json"
    config.load_config(cfg_path)  # also creates secrets.env alongside
    secrets_path = tmp_path / "secrets.env"

    assert secrets_path.exists()
    assert config.load_secrets(secrets_path) == {}


# --------------------------------------------------------------------------- #
# secrets.env creation — mode 600.
# --------------------------------------------------------------------------- #
def test_secrets_env_created_mode_600(tmp_path):
    cfg_path = tmp_path / "config.json"
    config.load_config(cfg_path)
    secrets_path = tmp_path / "secrets.env"

    assert secrets_path.exists()
    perms = stat.S_IMODE(secrets_path.stat().st_mode)
    assert perms == 0o600, f"expected 0o600, got {oct(perms)}"
    # Placeholders are commented (documented in-file).
    body = secrets_path.read_text()
    assert "# TELEGRAM_BOT_TOKEN=" in body
    assert "# TELEGRAM_CHAT_ID=" in body


def test_load_config_does_not_clobber_existing_secrets(tmp_path):
    """An existing secrets.env with real values survives a load_config call."""
    cfg_path = tmp_path / "config.json"
    secrets_path = tmp_path / "secrets.env"
    secrets_path.write_text("TELEGRAM_BOT_TOKEN=real_value\n")

    config.load_config(cfg_path)

    assert config.load_secrets(secrets_path) == {"TELEGRAM_BOT_TOKEN": "real_value"}


# --------------------------------------------------------------------------- #
# is_active — R5 kill switch.
# --------------------------------------------------------------------------- #
def test_is_active_enabled_true_no_mute():
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    assert config.is_active(cfg) is True


def test_is_active_enabled_false():
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    cfg["enabled"] = False
    assert config.is_active(cfg) is False


def test_is_active_mute_until_in_future_is_false():
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    cfg["mute_until"] = (now + timedelta(hours=1)).isoformat()
    assert config.is_active(cfg, now=now) is False


def test_is_active_mute_until_in_past_is_true():
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    cfg["mute_until"] = (now - timedelta(hours=1)).isoformat()
    assert config.is_active(cfg, now=now) is True


def test_is_active_mute_until_z_suffix_parsed():
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    cfg["mute_until"] = "2026-06-04T13:00:00Z"  # 1h in the future
    assert config.is_active(cfg, now=now) is False


def test_is_active_default_now_uses_wall_clock():
    """With no explicit now, a far-future mute still mutes."""
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    cfg["mute_until"] = "2099-01-01T00:00:00Z"
    assert config.is_active(cfg) is False
