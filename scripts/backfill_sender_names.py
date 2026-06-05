#!/usr/bin/env python3
"""One-shot backfill of real sender names into the triage store (idempotent).

Rewrites ``sender_name`` for every store item still holding a raw ``users/<id>``
by (1) installing config ``user_aliases`` overrides, (2) warming the domain
directory cache once, and (3) per-id ``people.get`` fallback for anyone the
bulk warm-up missed. Everything is READ-ONLY against Google APIs — the only
write is the atomic ``store.save`` (temp file + ``os.replace``).

Usage::

    uv run python scripts/backfill_sender_names.py            # rewrite in place
    uv run python scripts/backfill_sender_names.py --dry-run  # report only
    uv run python scripts/backfill_sender_names.py --store-path /tmp/copy.json

Idempotent: an item whose ``sender_name`` is already a real name is left
untouched; re-running changes nothing. If directory resolution fails, items
keep their raw ids — the script degrades, it never crashes mid-store.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# ``scripts/`` is not an installed package — make sibling modules + the repo
# root importable regardless of cwd (mirrors collect_mentions.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT))

import config  # noqa: E402  (scripts/config.py)
import store  # noqa: E402  (scripts/store.py)
import google_chat as gchat  # noqa: E402


def _default_token_path() -> str:
    env = os.environ.get("GCHAT_TOKEN_PATH")
    if env:
        return env
    return str(_REPO_ROOT / "token.json")


def _is_raw(sender_name) -> bool:
    return isinstance(sender_name, str) and sender_name.startswith("users/")


def backfill(
    *,
    config_path=None,
    store_path=None,
    token_path=None,
    creds=None,
    dry_run: bool = False,
) -> dict:
    """Rewrite raw-id sender_names in the store. Returns a summary dict.

    ``creds`` may be injected (tests); otherwise loaded via
    ``google_chat.get_credentials`` under the same token lock the collector
    uses. Returns ``{total, raw, resolved, unresolved, changes, dry_run}``.
    """
    cfg = config.load_config(config_path)
    gchat.set_user_aliases(cfg.get("user_aliases") or {})

    if creds is None:
        if token_path is not None:
            gchat.set_token_path(str(token_path))
        creds = gchat.get_credentials()

    # Bulk directory warm-up (read-only; swallows its own errors).
    gchat.warm_directory_cache(creds)

    store_obj = store.load(store_path)
    items = store_obj.get("items", {})

    total = len(items)
    raw = 0
    resolved = 0
    changes = []  # (item_id, old, new)

    for iid, item in items.items():
        old = item.get("sender_name")
        if not _is_raw(old):
            continue
        raw += 1
        sender_id = item.get("sender_id") or old
        # Cache-only resolution first (alias override / warmed directory).
        new = gchat.get_user_display_name({"name": sender_id})
        if _is_raw(new):
            # Per-id fallback for anyone the bulk warm-up missed.
            numeric_id = sender_id.split("/", 1)[1] if "/" in sender_id else sender_id
            people_name = gchat.resolve_one_via_people_get(numeric_id, creds)
            if people_name:
                new = people_name
        if new and not _is_raw(new) and new != old:
            resolved += 1
            changes.append((iid, old, new))
            if not dry_run:
                item["sender_name"] = new

    if changes and not dry_run:
        store.save(store_obj, store_path)

    return {
        "total": total,
        "raw": raw,
        "resolved": resolved,
        "unresolved": raw - resolved,
        "changes": changes,
        "dry_run": dry_run,
    }


def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Backfill real sender names into the triage store (read-only APIs).",
    )
    p.add_argument("--config-path", default=None, help="override config.json path")
    p.add_argument("--store-path", default=None, help="override store.json path (e.g. a copy)")
    p.add_argument("--token-path", default=None, help="override token.json path")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing the store",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    token_path = args.token_path or _default_token_path()
    result = backfill(
        config_path=args.config_path,
        store_path=args.store_path,
        token_path=token_path,
        dry_run=args.dry_run,
    )

    mode = "DRY-RUN" if result["dry_run"] else "WROTE"
    print(
        f"backfill [{mode}]: {result['total']} items, {result['raw']} raw, "
        f"{result['resolved']} resolved, {result['unresolved']} still raw"
    )
    for iid, old, new in result["changes"]:
        print(f"  {iid[:8]}  {old} -> {new}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
