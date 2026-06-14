#!/usr/bin/env python3
"""Sync team/role from PeopleForce (HRIS) into triage ``config.json`` user_profiles.

The Google Workspace domain directory is near-empty on org structure (team
1/133, role 2/133 — admins never populated department/title), but PeopleForce
holds authoritative ``department`` (-> team) and ``position`` (-> role) for every
active employee. Email is the join key: it is 100%% populated both in the
directory cache (``name_cache.json`` profiles) and in PeopleForce, so we match
PeopleForce ``email`` -> directory ``email`` -> Chat ``users/<id>`` and write the
resolved team/role into config ``user_profiles`` (the override map already read
by ``google_chat._load_config_overrides`` -> ``list_directory_authors``).

This script is OFFLINE infrastructure: it is the ONLY place the PeopleForce API
key is read, and it runs out-of-band (cron / manual). The stdio MCP server never
calls PeopleForce — it just keeps reading the config the script writes. That
keeps the API key out of the MCP process and stdout clean.

Credentials: ``PEOPLEFORCE_API_KEY`` in ``secrets.env`` (chmod 600, never
committed), parsed via ``config.load_secrets``. A "Company" API key is required
(Settings -> API keys -> Generate). Never logged.

Usage::

    uv run python scripts/peopleforce_sync.py            # dry-run: fetch + match + report, no write
    uv run python scripts/peopleforce_sync.py --apply    # merge matched team/role into config user_profiles
    uv run python scripts/peopleforce_sync.py --apply --include-name   # also override display_name from PeopleForce
    uv run python scripts/peopleforce_sync.py --status all             # include non-active employees

Idempotent: re-running with the same PeopleForce data and the same directory
cache produces the same config. Per-key deep merge — an --apply never wipes
unrelated user_profiles sub-keys (e.g. a hand-set alias) for the same user.
Degrades safely: a missing key, network error, or empty match writes nothing.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

import httpx

# ``scripts/`` is not an installed package — make sibling modules + the repo
# root importable regardless of cwd (mirrors backfill_sender_names.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT))

import config  # noqa: E402  (scripts/config.py)

_API_BASE = "https://app.peopleforce.io/api/public/v3"
_EMPLOYEES_PATH = "/employees"
_PAGE_LIMIT = 200  # hard stop on pagination to avoid an unbounded loop
_HTTP_TIMEOUT = 30.0


def _api_key(secrets_path: Optional[Path] = None) -> str:
    """Return the PeopleForce Company API key from secrets.env (or env override).

    Raises ``SystemExit`` with actionable guidance when absent — never prints the
    key itself.
    """
    env = os.environ.get("PEOPLEFORCE_API_KEY")
    if env:
        return env.strip()
    secrets = config.load_secrets(secrets_path)
    key = secrets.get("PEOPLEFORCE_API_KEY", "").strip()
    if not key:
        sys.exit(
            "PEOPLEFORCE_API_KEY is not set. Add it to "
            f"{config.SECRETS_PATH} as:\n"
            "    PEOPLEFORCE_API_KEY=<your Company API key>\n"
            "Generate one in PeopleForce: Settings -> API keys -> Generate "
            "(choose the 'Company' type)."
        )
    return key


def fetch_employees(api_key: str, status: str = "active") -> List[Dict]:
    """Fetch all employees across paginated ``GET /employees``.

    Honors the 300 req/min rate limit reactively: on HTTP 429 it waits the
    ``Retry-After`` seconds and retries the same page. Returns the raw employee
    dicts (caller projects the fields it needs).
    """
    headers = {"X-API-KEY": api_key, "Accept": "application/json"}
    out: List[Dict] = []
    page = 1
    with httpx.Client(timeout=_HTTP_TIMEOUT, headers=headers) as client:
        while page <= _PAGE_LIMIT:
            resp = client.get(
                f"{_API_BASE}{_EMPLOYEES_PATH}",
                params={"status": status, "page": page},
            )
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "5") or 5)
                _sleep(retry_after)
                continue
            if resp.status_code == 401:
                sys.exit("PeopleForce returned 401 Bad Credentials — check PEOPLEFORCE_API_KEY.")
            resp.raise_for_status()
            payload = resp.json()
            data = payload.get("data") or []
            out.extend(data)
            meta = payload.get("metadata") or {}
            total_pages = int(meta.get("pages") or 1)
            if page >= total_pages or not data:
                break
            page += 1
    return out


def _sleep(seconds: float) -> None:
    """Indirection so tests can monkeypatch the rate-limit backoff."""
    import time

    time.sleep(max(0.0, seconds))


def _project(emp: Dict) -> Dict[str, Optional[str]]:
    """Project a raw employee into {email, team, role, name} (None when absent)."""
    dept = emp.get("department") or {}
    pos = emp.get("position") or {}
    email = (emp.get("email") or "").strip().lower() or None
    return {
        "email": email,
        "team": (dept.get("name") or None),
        "role": (pos.get("name") or None),
        "name": (emp.get("full_name") or None),
    }


def _directory_email_to_uid(cache_path: Optional[Path] = None) -> Dict[str, str]:
    """Build ``email(lowercased) -> users/<id>`` from the directory name cache.

    Reads the ``profiles`` map persisted by ``google_chat.warm_directory_cache``.
    Returns ``{}`` (and the caller reports it) when the cache is missing or has no
    profiles — never raises.

    CRITICAL — key form: the cache profiles are keyed by the BARE numeric id (the
    People ``metadata.sources[].id``), but ``list_directory_authors`` rekeys each
    row to ``users/<id>`` and looks up overrides as ``profile_map.get("users/<id>")``.
    Config ``user_profiles`` MUST therefore be keyed ``users/<id>`` — so we
    normalize to that form here. Emitting the bare id would write overrides that
    are silently never matched.
    """
    import json

    path = cache_path or (config.BASE_DIR / "name_cache.json")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    mapping: Dict[str, str] = {}
    for uid, prof in (data.get("profiles") or {}).items():
        email = (prof.get("email") or "").strip().lower()
        if email:
            mapping[email] = uid if uid.startswith("users/") else f"users/{uid}"
    return mapping


def build_overrides(
    employees: List[Dict],
    email_to_uid: Dict[str, str],
    include_name: bool = False,
) -> Dict[str, Dict[str, str]]:
    """Match employees to ``users/<id>`` by email; build the user_profiles overrides.

    Only non-empty fields are written — an employee with no department contributes
    no ``team`` key, so the directory value (if any) survives.
    """
    overrides: Dict[str, Dict[str, str]] = {}
    for emp in employees:
        proj = _project(emp)
        email = proj["email"]
        if not email:
            continue
        uid = email_to_uid.get(email)
        if not uid:
            continue
        fields: Dict[str, str] = {}
        if proj["team"]:
            fields["team"] = proj["team"]
        if proj["role"]:
            fields["role"] = proj["role"]
        if include_name and proj["name"]:
            fields["name"] = proj["name"]
        if fields:
            overrides[uid] = fields
    return overrides


def _merge_into_config(cfg: dict, overrides: Dict[str, Dict[str, str]]) -> int:
    """Per-key deep-merge ``overrides`` into ``cfg['user_profiles']``; return change count.

    Returns the number of (user_id, field) pairs that were newly set or changed.
    """
    profiles = cfg.setdefault("user_profiles", {})
    changed = 0
    for uid, fields in overrides.items():
        cur = profiles.setdefault(uid, {})
        for k, v in fields.items():
            if cur.get(k) != v:
                cur[k] = v
                changed += 1
    return changed


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write matched team/role into config user_profiles (default: dry-run)")
    parser.add_argument("--include-name", action="store_true", help="also override display_name from PeopleForce full_name")
    parser.add_argument("--status", default="active", choices=["active", "all", "employed", "terminated", "hired", "probation"], help="employee status filter (default: active)")
    parser.add_argument("--config-path", default=None, help="override config.json path")
    parser.add_argument("--cache-path", default=None, help="override name_cache.json path")
    args = parser.parse_args(argv)

    api_key = _api_key()
    print(f"Fetching PeopleForce employees (status={args.status}) ...", file=sys.stderr)
    employees = fetch_employees(api_key, status=args.status)
    print(f"  fetched {len(employees)} employees", file=sys.stderr)

    cache_path = Path(args.cache_path) if args.cache_path else None
    email_to_uid = _directory_email_to_uid(cache_path)
    if not email_to_uid:
        print(
            "WARNING: directory cache has no email->id profiles. Warm it first "
            "(run the collector or list_chat_authors once), then re-run.",
            file=sys.stderr,
        )

    overrides = build_overrides(employees, email_to_uid, include_name=args.include_name)

    # Stats.
    pf_emails = {(_project(e)["email"]) for e in employees if _project(e)["email"]}
    matched = len(overrides)
    unmatched_pf = len(pf_emails - set(email_to_uid))
    dir_unmatched = len(set(email_to_uid) - pf_emails)
    with_team = sum(1 for f in overrides.values() if f.get("team"))
    with_role = sum(1 for f in overrides.values() if f.get("role"))

    print(
        f"\nMatched {matched} directory users by email "
        f"(team set: {with_team}, role set: {with_role}).",
        file=sys.stderr,
    )
    print(f"  PeopleForce employees with no directory match: {unmatched_pf}", file=sys.stderr)
    print(f"  Directory users with no PeopleForce match:     {dir_unmatched}", file=sys.stderr)

    cfg = config.load_config(args.config_path)
    if not args.apply:
        changed = _merge_into_config({**cfg, "user_profiles": dict(cfg.get("user_profiles", {}))}, overrides)
        print(f"\nDRY-RUN: {changed} field(s) would change. Re-run with --apply to write.", file=sys.stderr)
        return 0

    changed = _merge_into_config(cfg, overrides)
    config.save_config(cfg, args.config_path)
    print(f"\nAPPLIED: {changed} field(s) written to {config.CONFIG_PATH if args.config_path is None else args.config_path}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
