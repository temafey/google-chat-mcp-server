#!/usr/bin/env python3
"""Headless collector for the Chat Triage Assistant (T1.3) — the engine of Gate G1.

Runnable as ``uv run python scripts/collect_mentions.py``. It detects Google
Chat messages addressed to me (DMs + @mentions) since the last run and upserts
them into the atomic JSON ledger. It is **strictly read-only** against Chat:
NO posting, NO notifications, NO writes back to any space — store + log only.

Pipeline (numbered to match the task spec):
  1. RUN-LOCK   — non-blocking ``flock`` on ``collect.lock``; a second concurrent
                  run logs "another run in progress" and exits 0 (no overlap).
  2. KILL SWITCH (R5) — ``config.is_active`` False (``enabled=false`` or a future
                  ``mute_until``) → log "disabled/muted, skipping" and exit 0.
  3. IDENTITY   — if ``config.me_user_id`` is null, call ``google_chat.whoami()``
                  to resolve+persist ``users/<id>`` and seed the store skeleton.
  4. WINDOW     — ``[since, now]`` where ``since`` is ``store.last_run`` or, on the
                  first run, ``now - LOOKBACK`` (default 24h). ``since`` is handed
                  to ``mentions_core`` for the R3 dormant-space skip.
  5. TOKEN RACE (R2) — the credential load/refresh runs under a blocking ``flock``
                  on ``<token>.lock`` so it can't race the MCP server's refresh.
  6. BACKOFF (R7, coarse) — the ``mentions_core`` fetch is retried with bounded
                  exponential backoff on transient errors (429/5xx/network).
  7. UPSERT     — each detected item → ``store.upsert_item`` (status defaults to
                  ``new``; the store owns new-vs-update + R6 stale handling).
  8. STATE      — ``last_run`` and ``cursor_per_space`` are advanced; the store is
                  saved atomically (temp + ``os.replace``, handled by ``store``).
  9. LOG        — a structured JSON line is appended under ``logs/``. No secrets,
                  no token values, no message text are ever logged.
 10. DIGEST     — a compact human digest is printed to STDOUT (the G1 demo view).

Security: message text is **untrusted data**. This collector never interprets
instructions found inside fetched text — it only classifies metadata and stores
it (prompt-injection guard). ``token.json`` is touched solely via
``google_chat.get_credentials`` under the R2 lock.

NOTE (fine-grained backoff is a documented follow-up): per the task constraints
this module does NOT edit ``google_chat.py`` to add per-space backoff. Retry is
coarse — the whole ``list_messages_for_me`` call is retried as a unit. A future
task may push fine-grained per-space backoff into the API layer.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ``scripts/`` is not an installed package; make sibling modules importable and
# put the repo root on the path so ``google_chat`` / ``mentions_core`` resolve
# regardless of the caller's cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT))

import config  # noqa: E402  (scripts/config.py)
import store  # noqa: E402  (scripts/store.py)
import google_chat as gchat  # noqa: E402
import mentions_core  # noqa: E402

try:  # transient-error detection for R7 backoff; optional at import time.
    from googleapiclient.errors import HttpError
except ImportError:  # pragma: no cover - googleapiclient is a runtime dep.
    HttpError = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
#: First-run lookback when there is no cursor yet.
DEFAULT_LOOKBACK_HOURS = 24
#: Coarse R7 retry policy: 4 attempts, 1s -> 2s -> 4s -> 8s between them.
RETRY_ATTEMPTS = 4
RETRY_BASE_SECONDS = 1.0
#: Max number of new items rendered in the stdout digest list.
DIGEST_MAX_ITEMS = 10
#: Lock + log filenames (under the triage base dir).
RUN_LOCK_NAME = "collect.lock"
LOGS_DIRNAME = "logs"


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def _now_dt(now=None) -> datetime:
    """Aware-UTC ``datetime`` for ``now`` (None → wall clock)."""
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        dt = now
    else:
        text = str(now).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_z(dt: datetime) -> str:
    """RFC3339 ``...Z`` second-precision string (matches store.last_run shape)."""
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# Locking (POSIX flock)
# --------------------------------------------------------------------------- #
def _acquire_lock(path: Path, *, blocking: bool):
    """Acquire an exclusive ``flock`` on ``path``.

    Returns the open file object holding the lock, or ``None`` when
    ``blocking`` is False and the lock is already held by another process.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        fcntl.flock(fh.fileno(), flags)
    except OSError:
        fh.close()
        return None
    return fh


def _release_lock(fh) -> None:
    if fh is None:
        return
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


# NOTE (R2 token-refresh race): the advisory token lock used to live HERE, as an
# external wrapper around ``gchat.get_credentials()``. It has moved INSIDE
# ``google_chat.get_credentials`` (on ``<token>.lock``) so EVERY caller — this
# collector and the MCP server alike — is serialized automatically. Re-adding an
# external token lock here would make this process take ``<token>.lock`` twice
# via two different fds, which with ``flock`` self-deadlocks. Don't.


# --------------------------------------------------------------------------- #
# Backoff (R7)
# --------------------------------------------------------------------------- #
def _is_retriable(exc: BaseException) -> bool:
    """True for transient errors worth retrying: HTTP 429/5xx or network blips."""
    if HttpError is not None and isinstance(exc, HttpError):
        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(getattr(exc, "resp", None), "status", None)
        try:
            status = int(status)
        except (TypeError, ValueError):
            return False
        return status == 429 or 500 <= status < 600
    # ConnectionError / TimeoutError (incl. socket.timeout) subclass OSError.
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


def _fetch_with_backoff(make_coro, *, sleep=time.sleep, log=None):
    """Run an async fetch with bounded exponential backoff on transient errors.

    ``make_coro`` is a zero-arg factory returning a fresh coroutine per attempt
    (a coroutine cannot be awaited twice). Returns ``(result, retries)``.
    Non-retriable errors and the final attempt re-raise.
    """
    retries = 0
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return asyncio.run(make_coro()), retries
        except BaseException as exc:  # noqa: BLE001 - re-raised below if fatal
            last_attempt = attempt == RETRY_ATTEMPTS - 1
            if last_attempt or not _is_retriable(exc):
                raise
            delay = RETRY_BASE_SECONDS * (2 ** attempt)
            retries += 1
            if log is not None:
                log(
                    "transient-error",
                    attempt=attempt + 1,
                    delay_s=delay,
                    error=type(exc).__name__,
                )
            sleep(delay)
    # Unreachable: the loop either returns or raises.
    raise RuntimeError("backoff loop exited without result")  # pragma: no cover


# --------------------------------------------------------------------------- #
# Logging (structured, secret-free)
# --------------------------------------------------------------------------- #
def _make_logger(base_dir: Path, now_dt: datetime):
    """Return ``log(event, **fields)`` appending JSON lines under ``logs/``.

    Never logs token values, secrets, or message ``text`` — only counts and
    metadata. Best-effort: a logging failure must not abort a collection run.
    """
    logs_dir = base_dir / LOGS_DIRNAME
    log_path = logs_dir / f"collect-{now_dt.strftime('%Y-%m-%d')}.log"

    def log(event: str, **fields) -> None:
        record = {"ts": _iso_z(_now_dt()), "event": event}
        record.update(fields)
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            # Logging is observability, not correctness — swallow IO errors.
            pass

    return log


# --------------------------------------------------------------------------- #
# Identity (R: me_user_id bootstrap)
# --------------------------------------------------------------------------- #
def _ensure_identity(cfg: dict, store_obj: dict, config_path, log) -> dict:
    """Ensure ``me_user_id`` is known; resolve via ``whoami()`` if missing.

    ``whoami()`` itself persists the two identity keys into the triage config
    atomically; we reload and additionally persist via ``config.save_config`` so
    the rest of the schema is preserved, then seed the store skeleton.
    """
    if cfg.get("me_user_id"):
        store_obj["me_user_id"] = cfg["me_user_id"]
        return cfg

    log("identity-resolve")
    ident = asyncio.run(gchat.whoami())
    me_user_id = ident.get("me_user_id")
    me_display_name = ident.get("me_display_name")

    # Reload (whoami wrote the two keys) and persist the merged config.
    cfg = config.load_config(config_path)
    cfg["me_user_id"] = me_user_id
    cfg["me_display_name"] = me_display_name
    config.save_config(cfg, config_path)

    store_obj["me_user_id"] = me_user_id
    log("identity-resolved", me_user_id=me_user_id)
    return cfg


# --------------------------------------------------------------------------- #
# Digest
# --------------------------------------------------------------------------- #
def _truncate(text, limit=60) -> str:
    if not text:
        return ""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def format_digest(result: dict) -> str:
    """Build the compact human digest printed to STDOUT (the G1 demo view)."""
    new = result.get("new", 0)
    open_total = result.get("open_total", 0)
    lines = [f"Chat triage — {new} new / {open_total} open total"]
    for item in result.get("new_items", [])[:DIGEST_MAX_ITEMS]:
        sender = item.get("sender_name") or item.get("sender_id") or "unknown"
        space = item.get("space_display") or item.get("space_name") or "?"
        trigger = item.get("trigger") or "?"
        snippet = _truncate(item.get("text"))
        lines.append(f"  • {sender} — {space} [{trigger}]: {snippet}")
    extra = len(result.get("new_items", [])) - DIGEST_MAX_ITEMS
    if extra > 0:
        lines.append(f"  … and {extra} more new")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Core collection
# --------------------------------------------------------------------------- #
def collect(
    *,
    config_path=None,
    store_path=None,
    base_dir=None,
    token_path=None,
    now=None,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    sleep=time.sleep,
) -> dict:
    """Run one collection pass. Assumes the run-lock is already held by caller.

    Returns a result dict: ``{status, new, updated, open_total, new_items,
    since, until, retries, scanned_spaces}``. ``status`` is ``"disabled"`` when
    the kill switch short-circuits, else ``"ok"``.
    """
    base_dir = Path(base_dir) if base_dir is not None else config.BASE_DIR
    now_dt = _now_dt(now)
    log = _make_logger(base_dir, now_dt)

    cfg = config.load_config(config_path)
    if not config.is_active(cfg, now=now_dt):
        log("skip", reason="disabled-or-muted")
        return {
            "status": "disabled",
            "new": 0,
            "updated": 0,
            "open_total": 0,
            "new_items": [],
            "since": None,
            "until": _iso_z(now_dt),
            "retries": 0,
            "scanned_spaces": 0,
        }

    store_obj = store.load(store_path)
    cfg = _ensure_identity(cfg, store_obj, config_path, log)

    # --- Window [since, now] ------------------------------------------------ #
    last_run = store_obj.get("last_run")
    if last_run:
        since_dt = _now_dt(last_run)
    else:
        since_dt = now_dt - timedelta(hours=lookback_hours)
    since_iso = _iso_z(since_dt)
    until_iso = _iso_z(now_dt)
    log("window", since=since_iso, until=until_iso)

    # --- R2: load/refresh credentials --------------------------------------- #
    # The advisory token lock now lives INSIDE gchat.get_credentials() (on
    # ``<token>.lock``), serializing this collector with the MCP server. We only
    # point google_chat at the right token path; get_credentials() takes and
    # releases the lock itself. NO external token lock here (see note above) —
    # double-locking the same file via two fds would self-deadlock under flock.
    if token_path is not None:
        gchat.set_token_path(str(token_path))

    creds = gchat.get_credentials()
    if not creds:
        log("error", reason="no-credentials")
        raise RuntimeError(
            "No valid credentials. Authenticate the MCP server first "
            "(uv run python server.py --auth cli)."
        )

    # --- Name resolution: aliases (highest) + domain-directory warm-up ------ #
    # READ-ONLY. Install manual aliases, then bulk-resolve every in-domain
    # `users/<id>` to a real display name BEFORE items (and their sender_name)
    # are built by mentions_core. Any failure degrades to raw ids — never
    # aborts the run.
    gchat.set_user_aliases(cfg.get("user_aliases") or {})
    try:
        resolved = gchat.warm_directory_cache(creds)
        log("directory-warm", names=resolved)
    except Exception as exc:  # noqa: BLE001 - defensive; warm itself swallows.
        log("directory-warm-error", error=type(exc).__name__)

    # --- R7: fetch detected items with coarse backoff ----------------------- #
    # ``since`` drives the R3 dormant-space skip inside mentions_core. We scan
    # [since, now]; the per-run cursor is store.last_run.
    allowlist = cfg.get("spaces_allowlist")
    space_names = list(allowlist) if allowlist else None

    def make_coro():
        return mentions_core.list_messages_for_me(
            since_iso,
            until_iso,
            space_names=space_names,
            include_dms=True,
            since=since_iso,
        )

    items, retries = _fetch_with_backoff(make_coro, sleep=sleep, log=log)

    # --- R7/store: apply blocklist, then upsert ----------------------------- #
    blocklist = set(cfg.get("spaces_blocklist") or [])
    now_iso = until_iso
    new_count = 0
    updated_count = 0
    new_items = []
    scanned_spaces = set()

    for item in items:
        space_name = item.get("space_name")
        if space_name in blocklist:
            continue
        scanned_spaces.add(space_name)

        # Per-id fallback: if the bulk directory warm-up didn't cover this
        # sender (sender_name is still a raw `users/<id>`), try a single
        # People `people.get`. Read-only; on failure the raw id is kept.
        sender_name = item.get("sender_name")
        if isinstance(sender_name, str) and sender_name.startswith("users/"):
            numeric_id = sender_name.split("/", 1)[1]
            resolved = gchat.resolve_one_via_people_get(numeric_id, creds)
            if resolved:
                item["sender_name"] = resolved

        iid = store.item_id(space_name, item.get("message_name"))
        was_present = iid in store_obj.get("items", {})
        store.upsert_item(store_obj, item, now=now_iso)
        if was_present:
            updated_count += 1
        else:
            new_count += 1
            new_items.append(item)

    # --- State: advance cursor + last_run, save atomically ------------------ #
    # Collector OWNS writing last_run + cursor_per_space (orchestrator decision).
    # NOTE: cursor_per_space is advanced for spaces that yielded detected items
    # this run; the authoritative dormant-skip `since` is the single last_run
    # value. Full per-space cursor coverage (incl. silent spaces) would require
    # mentions_core to return its scanned-space set — a documented follow-up.
    cursor = store_obj.setdefault("cursor_per_space", {})
    for space_name in scanned_spaces:
        cursor[space_name] = now_iso
    store_obj["last_run"] = now_iso
    store.save(store_obj, store_path)

    open_total = len(store.open_items(store_obj))
    log(
        "run",
        status="ok",
        new=new_count,
        updated=updated_count,
        open_total=open_total,
        scanned_spaces=len(scanned_spaces),
        retries=retries,
    )

    return {
        "status": "ok",
        "new": new_count,
        "updated": updated_count,
        "open_total": open_total,
        "new_items": new_items,
        "since": since_iso,
        "until": until_iso,
        "retries": retries,
        "scanned_spaces": len(scanned_spaces),
    }


# --------------------------------------------------------------------------- #
# CLI entrypoint
# --------------------------------------------------------------------------- #
def _default_token_path() -> str:
    env = os.environ.get("GCHAT_TOKEN_PATH")
    if env:
        return env
    return str(_REPO_ROOT / "token.json")


def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Headless Chat-triage mention collector (read-only).",
    )
    p.add_argument("--config-path", default=None, help="override config.json path")
    p.add_argument("--store-path", default=None, help="override store.json path")
    p.add_argument("--base-dir", default=None, help="override triage base dir (locks/logs)")
    p.add_argument("--token-path", default=None, help="override token.json path")
    p.add_argument(
        "--lookback-hours",
        type=int,
        default=DEFAULT_LOOKBACK_HOURS,
        help=f"first-run lookback window in hours (default {DEFAULT_LOOKBACK_HOURS})",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    base_dir = Path(args.base_dir) if args.base_dir else config.BASE_DIR
    token_path = args.token_path or _default_token_path()

    # 1. RUN-LOCK (non-blocking): only one collector at a time.
    run_lock = _acquire_lock(base_dir / RUN_LOCK_NAME, blocking=False)
    if run_lock is None:
        _make_logger(base_dir, _now_dt())("skip", reason="another-run-in-progress")
        print("another run in progress; exiting")
        return 0

    try:
        result = collect(
            config_path=args.config_path,
            store_path=args.store_path,
            base_dir=base_dir,
            token_path=token_path,
            lookback_hours=args.lookback_hours,
        )
    finally:
        _release_lock(run_lock)

    if result["status"] == "disabled":
        print("triage disabled/muted, skipping")
        return 0

    print(format_digest(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
