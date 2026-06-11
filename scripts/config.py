"""Config + secrets loading layer for the Google Chat triage assistant (T1.4).

This module owns the canonical ``config.json`` schema and the ``secrets.env``
loader. Runtime files live OUTSIDE the git repo, under
``~/.claude-orchestrator/gchat-triage/`` (gitignored by location). The schema is
also documented in-tree via ``config.example.json`` / ``secrets.env.example``.

Design notes:
- ``load_config`` MERGES missing keys from ``DEFAULT_CONFIG`` so a partial file
  written by T0.1 (which sets only ``me_user_id`` / ``me_display_name``) is
  upgraded, never clobbered. T0.1 and T1.4 therefore coexist on one file.
- ``secrets.env`` is created chmod 600 with commented placeholders only; its
  values are NEVER logged.
- Pure stdlib. No third-party imports.
"""
from __future__ import annotations

import copy
import os
from datetime import datetime, timezone
from pathlib import Path

try:  # ``import json`` kept local-friendly; stdlib only.
    import json
except ImportError:  # pragma: no cover - json is always present in stdlib.
    raise

# ``templates`` is a sibling module under scripts/; it imports stdlib only at module
# level (its ``render`` does a function-local ``import notify``), so importing it here is
# cycle-free. We embed its DEFAULT_TEMPLATES into the canonical config schema below.
from templates import DEFAULT_TEMPLATES  # noqa: E402

# --------------------------------------------------------------------------- #
# Paths (runtime files live outside the repo, under ~/.claude-orchestrator).
# --------------------------------------------------------------------------- #
BASE_DIR = Path.home() / ".claude-orchestrator" / "gchat-triage"
CONFIG_PATH = BASE_DIR / "config.json"
SECRETS_PATH = BASE_DIR / "secrets.env"

# --------------------------------------------------------------------------- #
# Canonical config schema. T0.1 only writes me_user_id / me_display_name into
# this; every other key is owned here. Keep in sync with config.example.json.
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG: dict = {
    "version": 1,
    "me_user_id": None,
    "me_display_name": None,
    "enabled": True,
    "mute_until": None,
    "poll_cadence_minutes": 10,
    "quiet_hours": {"start": "22:00", "end": "08:00", "tz": "Europe/Kyiv"},
    "vip_senders": [],
    "urgency_keywords": ["urgent", "blocker", "prod", "asap", "deadline", "eod"],
    "channels": {
        "gc_inbox": {"enabled": False, "space_name": None},
        "telegram": {"enabled": False},
        "windows_toast": {"enabled": False},
    },
    "spaces_allowlist": None,
    "spaces_blocklist": [],
    # Manual sender-name overrides: {"users/<id>": "Display Name"}. Highest
    # priority in google_chat.get_user_display_name — wins over directory
    # lookups. Empty by default; the collector/backfill install these.
    "user_aliases": {},
    # Manual team/role/location (and optional email) overrides for the author
    # roster: {"users/<id>": {"team": "Mobile", "role": "Backend Lead",
    # "location": "Ukraine", "email": "..."}}. Surfaced by list_chat_authors and
    # WINS over the domain directory, which is typically sparse on
    # department/title (and has no location field). Any omitted sub-key falls
    # back to the directory value. Empty by default.
    "user_profiles": {},
    # Digest rendering templates (profiles + locale labels + per-item variant rules).
    # Owned by scripts/templates.py; switch ``active_profile`` / ``locale`` here, or edit
    # ``profiles`` / ``locales`` / ``variants`` to customise. Deep-merged on load, so a
    # custom value wins and new default keys are filled in on upgrade.
    "templates": copy.deepcopy(DEFAULT_TEMPLATES),
    # AI-analysis stage (opt-in). Disabled by default; enable via config.json
    # "analyze": {"enabled": true}.  Deep-merged on load so partial overrides keep
    # all other defaults (including nested adapters.order / adapters.claude.model).
    "analyze": {
        "enabled": False,
        "run_in_cron": False,
        "adapters": {
            "order": ["claude"],
            "claude": {"model": "claude-haiku-4-5-20251001"},
        },
        "escalate_to_thread": True,
        "thread_max_messages": 30,
        "max_items_per_run": 20,
        "min_confidence_to_store": 0.5,
        "timeout_seconds": 60,
    },
}

# Commented placeholders only — NO real values ever land here.
_SECRETS_TEMPLATE = (
    "# Secrets for the Google Chat triage assistant.\n"
    "# This file is chmod 600 and lives outside the git repo. NEVER commit it.\n"
    "# Uncomment and fill in the values below to enable Telegram notifications.\n"
    "# TELEGRAM_BOT_TOKEN=\n"
    "# TELEGRAM_CHAT_ID=\n"
    "# PeopleForce HRIS sync (scripts/peopleforce_sync.py). 'Company' API key:\n"
    "# Settings -> API keys -> Generate. Fills team/role into config user_profiles.\n"
    "# PEOPLEFORCE_API_KEY=\n"
)


# --------------------------------------------------------------------------- #
# Internal helpers.
# --------------------------------------------------------------------------- #
def _deep_merge(default: dict, override: dict) -> dict:
    """Return ``default`` recursively overlaid with ``override``.

    Keys present in ``override`` win. Keys present only in ``default`` are
    filled in (this is the upgrade path). Nested dicts merge recursively;
    everything else is taken verbatim from ``override`` when present. Keys that
    exist only in ``override`` (forward-compat / unknown) are preserved.
    """
    merged = copy.deepcopy(default)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


# Sentinel: ``_diff_against_default`` returns this when a value is identical to
# its default (the caller then drops the key entirely).
_MISSING = object()


def _diff_against_default(value, default):
    """Return the minimal subtree of ``value`` that differs from ``default``.

    Used to keep code-owned defaults OUT of the persisted ``config.json`` so that
    future edits to those defaults always reach runtime instead of being shadowed
    by a frozen copy in the file. Returns ``_MISSING`` when ``value`` equals
    ``default`` (caller drops the key). For dicts, recurses key-by-key; keys
    absent from ``default`` are kept verbatim (user / forward-compat additions).
    Non-dicts and lists are compared whole — a customized list is kept entire.
    """
    if isinstance(value, dict) and isinstance(default, dict):
        out: dict = {}
        for key, val in value.items():
            if key in default:
                diff = _diff_against_default(val, default[key])
                if diff is not _MISSING:
                    out[key] = diff
            else:
                out[key] = copy.deepcopy(val)
        return out if out else _MISSING
    return _MISSING if value == default else copy.deepcopy(value)


def _persistable(merged: dict) -> dict:
    """``merged`` with the code-owned ``templates`` section reduced to its diff.

    The ``templates`` block (profiles / locales / variants / active_profile /
    locale) is owned by ``scripts/templates.py`` via ``DEFAULT_TEMPLATES``.
    Persisting the full block freezes those defaults and shadows future edits —
    the exact footgun this avoids. We persist ONLY the parts a user actually
    customized; everything else is refilled from ``DEFAULT_CONFIG`` on load. All
    non-template keys (user state: me_user_id, channels, user_profiles, …) are
    kept verbatim.
    """
    to_save = copy.deepcopy(merged)
    if "templates" in to_save and "templates" in DEFAULT_CONFIG:
        diff = _diff_against_default(to_save["templates"], DEFAULT_CONFIG["templates"])
        if diff is _MISSING:
            to_save.pop("templates")
        else:
            to_save["templates"] = diff
    return to_save


def _atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write ``text`` to ``path`` via a temp file + os.replace (atomic)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def _to_aware_utc(value) -> datetime:
    """Coerce a datetime or ISO-8601 string to an aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ensure_secrets_file(secrets_path: Path) -> None:
    """Create ``secrets.env`` (chmod 600, commented placeholders) if absent.

    Never overwrites an existing file (would clobber user-entered secrets).
    """
    if secrets_path.exists():
        return
    _atomic_write_text(secrets_path, _SECRETS_TEMPLATE, mode=0o600)


# --------------------------------------------------------------------------- #
# Public API.
# --------------------------------------------------------------------------- #
def save_config(cfg: dict, config_path: Path | str | None = None) -> None:
    """Persist ``cfg`` to ``config_path`` atomically (temp file + replace)."""
    path = Path(config_path) if config_path is not None else CONFIG_PATH
    text = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"
    _atomic_write_text(path, text)


def load_config(config_path: Path | str | None = None) -> dict:
    """Load config.json, creating/upgrading it as needed.

    - Missing file  -> create it from ``DEFAULT_CONFIG`` and return that.
    - Partial file  -> merge missing keys from ``DEFAULT_CONFIG`` (preserving
      values already written, e.g. me_user_id/me_display_name from T0.1) and
      persist the upgraded file.

    The code-owned ``templates`` section is persisted as a DIFF only (see
    :func:`_persistable`): the full defaults live in ``templates.py``, so the
    returned config always reflects current defaults while the file carries only
    the user's customizations. The returned dict is always fully merged.

    The sibling ``secrets.env`` is also ensured (chmod 600) on every load.
    """
    path = Path(config_path) if config_path is not None else CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_secrets_file(path.parent / SECRETS_PATH.name)

    if not path.exists():
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        # Persist the diff (empty templates here) so a fresh file never freezes
        # code-owned template defaults; runtime still gets the full schema.
        save_config(_persistable(cfg), path)
        return cfg

    existing = json.loads(path.read_text(encoding="utf-8"))
    merged = _deep_merge(DEFAULT_CONFIG, existing)
    # Persist only the diff of the code-owned ``templates`` section (see
    # _persistable). The full defaults stay in templates.py, so editing a default
    # there always reaches runtime instead of being frozen in this file. Runtime
    # still gets the fully-merged config returned below.
    to_save = _persistable(merged)
    if to_save != existing:
        save_config(to_save, path)
    return merged


def load_secrets(secrets_path: Path | str | None = None) -> dict:
    """Parse ``KEY=VALUE`` lines from secrets.env.

    Blank lines and ``#`` comments are skipped (so the commented placeholders in
    a fresh file yield ``{}``). A missing file is tolerated and returns ``{}``.
    Values are returned verbatim; callers MUST NOT log them.
    """
    path = Path(secrets_path) if secrets_path is not None else SECRETS_PATH
    if not path.exists():
        return {}
    result: dict = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        result[key] = value.strip()
    return result


def is_active(cfg: dict, now: datetime | None = None) -> bool:
    """R5 kill switch: is the triage assistant currently active?

    Returns ``False`` when ``enabled`` is False OR ``mute_until`` is set and in
    the future. Used to short-circuit collect + notify.
    """
    if not cfg.get("enabled", True):
        return False
    mute_until = cfg.get("mute_until")
    if mute_until:
        now_utc = _to_aware_utc(now) if now is not None else datetime.now(timezone.utc)
        if _to_aware_utc(mute_until) > now_utc:
            return False
    return True
