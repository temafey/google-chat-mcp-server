"""scripts/templates.py — config-driven template engine for the triage digest.

The digest used to be hard-coded in ``notify.render_card``. This module makes the
layout DATA: a set of named *profiles* (full template strings) plus a *locale* table
(static labels), both living under ``config.json["templates"]`` so a user can edit /
switch them in settings without touching code.

Selection happens on two axes:
- **profile** (``active_profile``) — the user's preferred look, switchable in settings;
- **variants** — per-item overrides keyed on the item's ``priority`` / ``space_name``
  (≈ project) / ``space_type`` / ``trigger`` (≈ message type), so e.g. an urgent item can
  render with a louder profile inside the same digest.

SECURITY: templates are TRUSTED config (user-authored — they may legitimately contain
literal ``<b>`` / ``<blockquote>`` / ``*bold*``). The *values* substituted into them are
UNTRUSTED Chat text and are escaped per channel via ``notify._esc`` BEFORE substitution.
Substitution uses :meth:`string.Template.safe_substitute` — a plain name→value lookup, so
(unlike ``str.format``) there is no attribute access / format-string injection, missing
placeholders are left literal instead of crashing, and substituted values are never
re-scanned (an injected ``$icon`` inside message text stays literal).

This file imports stdlib only at module level. ``render`` does a function-local
``import notify`` so ``notify`` (which imports this module) and this module do not form an
import cycle, and so the single security/escaping layer (``notify._esc`` et al.) is reused
verbatim rather than duplicated.
"""
from __future__ import annotations

import copy
from string import Template

# --------------------------------------------------------------------------- #
# DEFAULT_TEMPLATES — pure data (NO imports). Embedded into
# ``config.DEFAULT_CONFIG["templates"]`` so a fresh / partial config.json gets it via
# the existing deep-merge upgrade path. Edit a copy in config.json to customise.
#
# Placeholders available to the NEW block template (values pre-escaped per channel):
#   $icon $sender $location $reltime $summary $message  — atomic
#   $original_message                                   — localized "Original message" label
#   $message_block  — the original-message wrapper (expandable on Telegram), or ""
#   $link_block     — the "Open in Chat" line, or ""    (both carry a leading newline)
# OVERDUE block: $icon $sender $promise $due $promise_label $due_label $link_block.
# Header: $title $counts $divider.
# --------------------------------------------------------------------------- #
DEFAULT_TEMPLATES: dict = {
    "active_profile": "default",
    "locale": "en",
    "locales": {
        "en": {
            "plural": "en",
            "header_title": "📥 Chat triage",
            "new_clause": "🆕 $count new",
            "overdue_clause": "⏰ $count overdue",
            "more_new": "…and $count more new",
            "direct_message": "Direct message",
            "unknown_sender": "(unknown)",
            "open_link": "🔗 Open in Chat",
            "original_message": "Original message",
            "promise_label": "promise",
            "due_label": "due",
            "no_due": "(no due date)",
            "months": ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
            "rel_just_now": "just now",
            "rel_min": "${count}m ago",
            "rel_hour": "${count}h ago",
            "rel_day": "${count}d ago",
        },
        "ru": {
            # Slavic plurals: count-bearing labels are 3-form lists [one, few, many]
            # (e.g. 1 новое · 2 новых · 5 новых), picked by notify._plural_index.
            "plural": "slavic",
            "header_title": "📥 Триаж чатов",
            "new_clause": ["🆕 $count новое", "🆕 $count новых", "🆕 $count новых"],
            "overdue_clause": "⏰ $count просрочено",
            "more_new": ["…и ещё $count новое", "…и ещё $count новых", "…и ещё $count новых"],
            "direct_message": "Личное сообщение",
            "unknown_sender": "(неизвестно)",
            "open_link": "🔗 Открыть в чате",
            "original_message": "Исходное сообщение",
            "promise_label": "обещание",
            "due_label": "до",
            "no_due": "(без срока)",
            "months": ["янв", "фев", "мар", "апр", "май", "июн",
                       "июл", "авг", "сен", "окт", "ноя", "дек"],
            "rel_just_now": "только что",
            "rel_min": ["$count минуту назад", "$count минуты назад", "$count минут назад"],
            "rel_hour": ["$count час назад", "$count часа назад", "$count часов назад"],
            "rel_day": ["$count день назад", "$count дня назад", "$count дней назад"],
        },
        "uk": {
            "plural": "slavic",
            "header_title": "📥 Тріаж чатів",
            "new_clause": ["🆕 $count нове", "🆕 $count нові", "🆕 $count нових"],
            "overdue_clause": "⏰ $count прострочено",
            "more_new": ["…та ще $count нове", "…та ще $count нові", "…та ще $count нових"],
            "direct_message": "Особисте повідомлення",
            "unknown_sender": "(невідомо)",
            "open_link": "🔗 Відкрити в чаті",
            "original_message": "Оригінальне повідомлення",
            "promise_label": "обіцянка",
            "due_label": "до",
            "no_due": "(без терміну)",
            "months": ["січ", "лют", "бер", "кві", "тра", "чер",
                       "лип", "сер", "вер", "жов", "лис", "гру"],
            "rel_just_now": "щойно",
            "rel_min": ["$count хвилину тому", "$count хвилини тому", "$count хвилин тому"],
            "rel_hour": ["$count годину тому", "$count години тому", "$count годин тому"],
            "rel_day": ["$count день тому", "$count дні тому", "$count днів тому"],
        },
    },
    "profiles": {
        # The user-chosen "Labeled" layout: icon-labelled meta line, a visible 💬 summary,
        # the full original message under an expandable cut, then the link.
        "default": {
            "new_cap": 10,
            "tg_summary_cap": 280,
            "divider": "──────────",
            "counts_sep": " · ",
            "header": "$title\n$counts\n$divider",
            "new_block": "$icon $sender\n📍 $location · 🕒 $reltime\n💬 $summary$message_block$link_block",
            "overdue_block": "⏰ $sender\n   $promise_label \"$promise\"\n   $due_label $due$link_block",
            # Show the original message under the cut (only when a distinct summary exists).
            "show_message": True,
        },
        # Lowest-noise: one meta line, summary only — no expandable message, no link.
        "compact": {
            "new_cap": 15,
            "tg_summary_cap": 0,
            "divider": "──────────",
            "counts_sep": " · ",
            "header": "$title · $counts",
            "new_block": "$icon $sender · $location · $reltime\n💬 $summary",
            "overdue_block": "⏰ $sender · $due_label $due · $promise",
            "show_message": False,
        },
        # Richest: same labelled layout as default, but Telegram expands far more of the
        # original message (700 vs 280 chars).
        "detailed": {
            "new_cap": 10,
            "tg_summary_cap": 700,
            "divider": "──────────",
            "counts_sep": " · ",
            "header": "$title\n$counts\n$divider",
            "new_block": "$icon $sender\n📍 $location · 🕒 $reltime\n💬 $summary$message_block$link_block",
            "overdue_block": "⏰ $sender\n   $promise_label \"$promise\"\n   $due_label $due$link_block",
            "show_message": True,
        },
    },
    # Per-item profile overrides. First entry whose every ``when`` field matches the item
    # wins. Supported fields: priority, space_name (≈ project), space_type, trigger (≈ type).
    "variants": [
        {"when": {"priority": ["urgent", "high"]}, "profile": "detailed"},
    ],
}


# --------------------------------------------------------------------------- #
# Resolution helpers.
# --------------------------------------------------------------------------- #
def _cfg(templates_cfg) -> dict:
    """Return a usable templates config (fall back to DEFAULT_TEMPLATES)."""
    if isinstance(templates_cfg, dict) and templates_cfg:
        return templates_cfg
    return DEFAULT_TEMPLATES


def _locale(tcfg: dict) -> dict:
    """The active locale label table.

    DEFAULT 'en' labels are the base, overlaid by the DEFAULT table for the chosen
    locale, then by any user-supplied labels — so a partial / custom locale never
    leaves a bare ``$placeholder``.
    """
    name = tcfg.get("locale") or "en"
    base = copy.deepcopy(DEFAULT_TEMPLATES["locales"]["en"])
    base.update(DEFAULT_TEMPLATES["locales"].get(name, {}))
    user_locales = tcfg.get("locales")
    if isinstance(user_locales, dict) and isinstance(user_locales.get(name), dict):
        base.update(user_locales[name])
    return base


def _profile(tcfg: dict, name: str) -> dict:
    """Resolve a named profile, filling missing keys from DEFAULT 'default'.

    Falls back to ``active_profile`` then the built-in ``default`` when ``name`` is
    unknown, so a partial custom profile still renders.
    """
    profiles = tcfg.get("profiles") or {}
    chosen = profiles.get(name)
    if not isinstance(chosen, dict):
        chosen = profiles.get(tcfg.get("active_profile") or "default")
    if not isinstance(chosen, dict):
        chosen = DEFAULT_TEMPLATES["profiles"].get(name) or DEFAULT_TEMPLATES["profiles"]["default"]
    base = copy.deepcopy(DEFAULT_TEMPLATES["profiles"]["default"])
    base.update(chosen)
    return base


def _variant_matches(item: dict, when: dict) -> bool:
    """True iff EVERY field in ``when`` matches the item (value in the allowed list).

    ``priority`` matches case-insensitively; the rest match verbatim. An empty
    ``when`` never matches (so a misconfigured catch-all cannot fire by accident).
    """
    if not when:
        return False
    for field, allowed in when.items():
        allowed_list = allowed if isinstance(allowed, list) else [allowed]
        value = item.get(field)
        if field == "priority":
            value = (value or "").lower()
            allowed_list = [str(a).lower() for a in allowed_list]
        if value not in allowed_list:
            return False
    return True


def _variant_profile_name(item: dict, tcfg: dict) -> str:
    """Profile name for ``item``: first matching variant, else ``active_profile``."""
    active = tcfg.get("active_profile") or "default"
    for variant in tcfg.get("variants") or []:
        if isinstance(variant, dict) and _variant_matches(item, variant.get("when") or {}):
            return variant.get("profile") or active
    return active


# --------------------------------------------------------------------------- #
# Rendering.
# --------------------------------------------------------------------------- #
def render(
    new_items: list,
    esc_items: list,
    *,
    now,
    mode: str,
    templates_cfg=None,
    link_fn=None,
    cap=None,
) -> str:
    """Render the per-channel digest from config-driven templates.

    ``mode`` ∈ {'plain', 'gchat', 'tg_html'}. ``templates_cfg`` is
    ``config["templates"]`` (None → DEFAULT_TEMPLATES). ``link_fn`` defaults to
    ``notify.chat_room_link``. ``cap`` overrides the active profile's ``new_cap``
    (None → profile value; ``<= 0`` → no cap).
    """
    import notify  # function-local: avoids an import cycle, reuses the escape layer

    if link_fn is None:
        link_fn = notify.chat_room_link

    tcfg = _cfg(templates_cfg)
    loc = _locale(tcfg)
    active = _profile(tcfg, tcfg.get("active_profile") or "default")

    n, m = len(new_items), len(esc_items)
    header = _render_header(active, loc, n, m, notify=notify)

    new_cap = active.get("new_cap", notify.DIGEST_NEW_CAP) if cap is None else cap
    if new_cap is None or new_cap <= 0:
        shown, capped = new_items, False
    else:
        shown, capped = new_items[:new_cap], True

    rule = loc.get("plural", "en")

    blocks: list[str] = []
    for it in shown:
        prof = _profile(tcfg, _variant_profile_name(it, tcfg))
        blocks.append(_render_new_block(it, prof, loc, now=now, mode=mode, link_fn=link_fn, notify=notify))
    if capped and n > new_cap:
        rest = n - new_cap
        blocks.append(Template(notify._pick_form(loc["more_new"], rest, rule)).safe_substitute(count=rest))
    for it in esc_items:
        prof = _profile(tcfg, _variant_profile_name(it, tcfg))
        blocks.append(_render_overdue_block(it, prof, loc, mode=mode, link_fn=link_fn, notify=notify))

    if not blocks:
        return header
    return header + "\n" + "\n\n".join(blocks)


def _render_header(prof: dict, loc: dict, n: int, m: int, *, notify) -> str:
    rule = loc.get("plural", "en")
    clauses = []
    if n:
        clauses.append(Template(notify._pick_form(loc["new_clause"], n, rule)).safe_substitute(count=n))
    if m:
        clauses.append(Template(notify._pick_form(loc["overdue_clause"], m, rule)).safe_substitute(count=m))
    counts = (prof.get("counts_sep") or " · ").join(clauses)
    out = Template(prof["header"]).safe_substitute(
        title=loc["header_title"], counts=counts, divider=prof.get("divider", "")
    )
    # Drop any line that collapsed to whitespace because $counts was blank.
    return "\n".join(line for line in out.split("\n") if line.strip())


def _render_new_block(item, prof, loc, *, now, mode, link_fn, notify) -> str:
    esc = notify._esc
    summary_src = item.get("context_summary") or item.get("text")
    has_summary = bool((item.get("context_summary") or "").strip())

    icon = notify._priority_icon(item)
    sender = notify._bold(esc(item.get("sender_name") or loc["unknown_sender"], mode), mode)
    if notify._is_dm(item):
        location = loc["direct_message"]
    else:
        location = item.get("space_display") or item.get("space_name") or "Chat"
    location = esc(location, mode)
    reltime = notify.relative_time(item.get("created_time"), now, loc)

    tg_cap = prof.get("tg_summary_cap", 280)

    # Original message under the cut: only when the profile allows AND there is a
    # distinct summary (so we never duplicate text==summary).
    message_block = ""
    if prof.get("show_message", True) and has_summary:
        message_block = _message_block(item.get("text"), loc, mode=mode, tg_cap=tg_cap, notify=notify)

    mapping = {
        "icon": icon,
        "sender": sender,
        "location": location,
        "reltime": reltime,
        "summary": esc(notify._snippet(summary_src), mode),
        "message": esc(notify._snippet(item.get("text"), tg_cap or 140), mode),
        "message_block": message_block,
        "link_block": _link_block(link_fn(item) if link_fn else None, loc, mode, notify=notify),
        "original_message": esc(loc["original_message"], mode),
    }
    return Template(prof["new_block"]).safe_substitute(mapping)


def _render_overdue_block(item, prof, loc, *, mode, link_fn, notify) -> str:
    esc = notify._esc
    sender = notify._bold(esc(item.get("sender_name") or loc["unknown_sender"], mode), mode)
    promise = esc(item.get("my_promise") or "(unspecified)", mode)
    due_raw = notify.human_due(item.get("promise_due"), loc)
    mapping = {
        "icon": "⏰",
        "sender": sender,
        "promise": promise,
        "due": esc(due_raw, mode),
        "promise_label": loc["promise_label"],
        "due_label": loc["due_label"],
        "link_block": _link_block(link_fn(item) if link_fn else None, loc, mode, notify=notify),
    }
    return Template(prof["overdue_block"]).safe_substitute(mapping)


def _message_block(text, loc, *, mode, tg_cap, notify) -> str:
    """The original-message wrapper. Empty string when there is no text.

    tg_html: a bold label + an expandable (collapsed-by-default) blockquote. The body is
    html-escaped, so an injected ``</blockquote>`` becomes ``&lt;/blockquote&gt;`` and
    cannot break out. plain/gchat: an indented label + snippet.
    """
    esc = notify._esc
    label = esc(loc["original_message"], mode)
    if mode == "tg_html":
        body = esc(notify._snippet(text, tg_cap or 280), mode)
        if not body:
            return ""
        return f"\n<b>{label}</b>\n<blockquote expandable>{body}</blockquote>"
    body = esc(notify._snippet(text), mode)
    if not body:
        return ""
    indent = notify._INDENT
    return f"\n{indent}{label}\n{indent}{body}"


def _link_block(url, loc, mode, *, notify) -> str:
    """The link line with a leading newline, or '' when there is no url.

    The link LABEL is localized (``loc['open_link']``); the URL/markup is built by
    ``notify._link_line`` (mode-aware, security-reviewed).
    """
    line = notify._link_line(url, mode, label=loc["open_link"])
    if not line:
        return ""
    if mode == "tg_html":
        return f"\n{line}"
    return f"\n{notify._INDENT}{line}"
