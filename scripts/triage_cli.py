"""scripts/triage_cli.py — integration CLI for the Chat Triage Assistant.

The thin INTEGRATION layer that the ``chat-triage`` skill drives. It glues the
deterministic state modules (``store`` / ``triage_session`` / ``config``) to the
single outward write path (``google_chat.send_message``) behind an argparse
command surface. Every subcommand is a one-shot: load the ledger, do exactly the
one thing asked, print a one-line JSON (or a human table), exit.

Design notes
------------
* **One outward write.** ``post`` is the ONLY subcommand that touches the
  network. It posts the reply (threaded via the item's ``thread_name``), and
  ONLY on success records the item as ``answered`` — an API error exits non-zero
  and records nothing, so the ledger never shows a false "answered".
* **Reply text comes from a FILE, never argv.** Multi-line and injection-safe:
  the skill writes the approved draft to a temp file and passes its path.
* **The CLI does not decide WHETHER to post.** It posts when invoked. The
  confirmation gate lives in the skill (the human in the loop).
* **No new state.** All mutations delegate to ``triage_session`` /
  ``store`` — this module invents no status and writes no item field directly.
* **Injectable clock for parsing.** ``_parse_when`` accepts a ``now`` so the
  ``+Nh`` / ``+Nd`` relative forms are deterministically testable.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ``scripts/`` is not an installed package. Make sibling modules importable and
# put the repo root on the path so ``google_chat`` resolves regardless of cwd —
# mirrors scripts/notify.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT))

import config  # noqa: E402  (scripts/config.py)
import store  # noqa: E402  (scripts/store.py)
import triage_session  # noqa: E402  (scripts/triage_session.py)
import google_chat  # noqa: E402  (repo-root google_chat.py)


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #
_RELATIVE_WHEN = re.compile(r"^\+(\d+)([hd])$")


def _parse_when(spec: str, now=None) -> str:
    """Resolve a snooze/due SPEC to an ISO-8601 UTC string.

    Two accepted forms:

    * **Relative** ``+<N>h`` / ``+<N>d`` — ``now`` plus N hours / days.
    * **Absolute** — any other string is treated as ISO-8601 and passed through
      verbatim (``triage_session`` normalises it downstream via ``_iso``).

    ``now`` may be ``None`` (wall clock UTC), a ``datetime``, or an ISO string.
    """
    spec = (spec or "").strip()
    match = _RELATIVE_WHEN.match(spec)
    if not match:
        return spec  # absolute ISO — passthrough
    amount = int(match.group(1))
    unit = match.group(2)
    if now is None:
        base = datetime.now(timezone.utc)
    else:
        base = store._parse_iso(now)
    delta = timedelta(hours=amount) if unit == "h" else timedelta(days=amount)
    return store._now_iso(base + delta)


def _resolve_id(store_data: dict, raw: str):
    """Resolve ``raw`` to a full item id (exact match, else unique prefix).

    Returns the full id, or ``None`` when nothing matches or the prefix is
    ambiguous (the caller turns that into an exit-2 error).
    """
    items = store_data.get("items", {})
    if raw in items:
        return raw
    matches = [k for k in items if k.startswith(raw)]
    if len(matches) == 1:
        return matches[0]
    return None


def _snippet(text, limit: int = 80) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _print_json(obj) -> None:
    """Print a single-line JSON result (the contract for write subcommands)."""
    print(json.dumps(obj, ensure_ascii=False))


def _err(message: str) -> None:
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------- #
# Subcommands.
# --------------------------------------------------------------------------- #
def _cmd_list(args) -> int:
    cfg = config.load_config(args.config_path)
    store_data = store.load(args.store_path)
    queue = triage_session.triage_queue(
        store_data,
        now=None,
        vip_senders=cfg.get("vip_senders", []),
        urgency_keywords=cfg.get("urgency_keywords", []),
    )
    if args.json:
        rows = [
            {
                "id": it.get("id"),
                "priority": it.get("priority"),
                "trigger": it.get("trigger"),
                "sender_name": it.get("sender_name"),
                "sender_id": it.get("sender_id"),
                "space_name": it.get("space_name"),
                "thread_name": it.get("thread_name"),
                "created_time": it.get("created_time"),
                "status": it.get("status"),
                "text": it.get("text"),
            }
            for it in queue
        ]
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    if not queue:
        print("(no items to triage)")
        return 0
    for it in queue:
        print(
            f"{(it.get('id') or '')[:12]:12}  "
            f"{(it.get('priority') or '-'):6}  "
            f"{(it.get('trigger') or '-'):12}  "
            f"{(it.get('sender_name') or '-'):20}  "
            f"{it.get('created_time') or '-'}  "
            f"{it.get('status') or '-'}  "
            f"{_snippet(it.get('text'))}"
        )
    return 0


def _cmd_show(args) -> int:
    store_data = store.load(args.store_path)
    item_id = _resolve_id(store_data, args.id)
    if item_id is None:
        _err(f"error: unknown item id {args.id!r}")
        return 2
    item = store_data["items"][item_id]
    if args.json:
        print(json.dumps(item, ensure_ascii=False, indent=2))
        return 0
    for key in (
        "id",
        "status",
        "priority",
        "priority_reason",
        "trigger",
        "sender_name",
        "sender_id",
        "space_name",
        "space_display",
        "thread_name",
        "created_time",
        "context_summary",
        "context_confidence",
        "my_promise",
        "promise_due",
        "snooze_until",
        "response_posted",
        "answered_at",
    ):
        print(f"{key:18}: {item.get(key)}")
    print(f"{'text':18}: {item.get('text')}")
    print(f"{'history':18}: {json.dumps(item.get('history') or [], ensure_ascii=False)}")
    return 0


def _cmd_post(args) -> int:
    store_data = store.load(args.store_path)
    item_id = _resolve_id(store_data, args.id)
    if item_id is None:
        _err(f"error: unknown item id {args.id!r}")
        return 2
    item = store_data["items"][item_id]

    # Reply text always comes from the file — verbatim, multi-line, never argv.
    text = Path(args.text_file).read_text(encoding="utf-8")

    result = asyncio.run(
        google_chat.send_message(
            item["space_name"], text, thread_name=item.get("thread_name")
        )
    )

    if isinstance(result, dict) and result.get("error"):
        # API error — surface it, record NOTHING (no false "answered").
        _err(
            f"error: send failed: {result.get('error')} "
            f"(status {result.get('status')})"
        )
        return 1

    posted_name = result.get("name") if isinstance(result, dict) else None
    triage_session.record_response(
        store_data,
        item_id,
        response_text=text,
        response_posted=posted_name,
        path=args.store_path,
    )
    _print_json({"id": item_id, "posted": posted_name, "status": "answered"})
    return 0


def _cmd_triage(args) -> int:
    store_data = store.load(args.store_path)
    item_id = _resolve_id(store_data, args.id)
    if item_id is None:
        _err(f"error: unknown item id {args.id!r}")
        return 2
    summary = None
    if args.summary_file:
        summary = Path(args.summary_file).read_text(encoding="utf-8")
    item = triage_session.set_triage(
        store_data,
        item_id,
        priority=args.priority,
        priority_reason=args.reason,
        context_summary=summary,
        context_confidence=args.confidence,
        path=args.store_path,
    )
    _print_json(
        {"id": item_id, "status": item.get("status"), "priority": item.get("priority")}
    )
    return 0


def _cmd_snooze(args) -> int:
    store_data = store.load(args.store_path)
    item_id = _resolve_id(store_data, args.id)
    if item_id is None:
        _err(f"error: unknown item id {args.id!r}")
        return 2
    until = _parse_when(args.until)
    item = triage_session.snooze(store_data, item_id, until=until, path=args.store_path)
    _print_json(
        {"id": item_id, "status": item.get("status"), "snooze_until": item.get("snooze_until")}
    )
    return 0


def _cmd_promise(args) -> int:
    store_data = store.load(args.store_path)
    item_id = _resolve_id(store_data, args.id)
    if item_id is None:
        _err(f"error: unknown item id {args.id!r}")
        return 2
    due = _parse_when(args.due)
    item = triage_session.record_promise(
        store_data, item_id, my_promise=args.text, promise_due=due, path=args.store_path
    )
    _print_json(
        {
            "id": item_id,
            "status": item.get("status"),
            "my_promise": item.get("my_promise"),
            "promise_due": item.get("promise_due"),
        }
    )
    return 0


def _cmd_close(args) -> int:
    store_data = store.load(args.store_path)
    item_id = _resolve_id(store_data, args.id)
    if item_id is None:
        _err(f"error: unknown item id {args.id!r}")
        return 2
    item = triage_session.close_item(store_data, item_id, path=args.store_path)
    _print_json({"id": item_id, "status": item.get("status")})
    return 0


def _cmd_ignore(args) -> int:
    store_data = store.load(args.store_path)
    item_id = _resolve_id(store_data, args.id)
    if item_id is None:
        _err(f"error: unknown item id {args.id!r}")
        return 2
    item = triage_session.ignore_item(store_data, item_id, path=args.store_path)
    _print_json({"id": item_id, "status": item.get("status")})
    return 0


# --------------------------------------------------------------------------- #
# Argument parsing.
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="triage_cli.py",
        description="Integration CLI for the Chat Triage Assistant.",
    )
    parser.add_argument(
        "--store-path",
        default=None,
        help="override store.json path (default: the canonical ledger)",
    )
    parser.add_argument(
        "--config-path",
        default=None,
        help="override config.json path (default: the canonical config)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list the triage queue")
    p_list.add_argument("--json", action="store_true", help="emit a JSON array")
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="show one item in full")
    p_show.add_argument("id")
    p_show.add_argument("--json", action="store_true", help="emit the raw item JSON")
    p_show.set_defaults(func=_cmd_show)

    p_post = sub.add_parser("post", help="post a reply (the only outward write)")
    p_post.add_argument("id")
    p_post.add_argument(
        "--text-file",
        dest="text_file",
        required=True,
        help="file holding the reply text (read verbatim — never via argv)",
    )
    p_post.set_defaults(func=_cmd_post)

    p_triage = sub.add_parser("triage", help="set priority + context (no network)")
    p_triage.add_argument("id")
    p_triage.add_argument("--priority", required=True, choices=("high", "normal", "low"))
    p_triage.add_argument("--reason", default=None)
    p_triage.add_argument("--summary-file", dest="summary_file", default=None)
    p_triage.add_argument("--confidence", default=None)
    p_triage.set_defaults(func=_cmd_triage)

    p_snooze = sub.add_parser("snooze", help="snooze until a time (ISO or +Nh/+Nd)")
    p_snooze.add_argument("id")
    p_snooze.add_argument("--until", required=True)
    p_snooze.set_defaults(func=_cmd_snooze)

    p_promise = sub.add_parser("promise", help="record a promise I owe + its due time")
    p_promise.add_argument("id")
    p_promise.add_argument("--text", required=True)
    p_promise.add_argument("--due", required=True)
    p_promise.set_defaults(func=_cmd_promise)

    p_close = sub.add_parser("close", help="close an item (terminal)")
    p_close.add_argument("id")
    p_close.set_defaults(func=_cmd_close)

    p_ignore = sub.add_parser("ignore", help="ignore an item (terminal)")
    p_ignore.add_argument("id")
    p_ignore.set_defaults(func=_cmd_ignore)

    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
