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
#   $icon $sender $location $abstime $reltime $summary $message  — atomic
#     $abstime — absolute local send time '04 Jun 10:00' (NEVER goes stale; default)
#     $reltime — relative age '2m ago' at SEND time (kept for back-compat; goes stale)
#     $summary — when the profile sets summary_expandable:True this is the FULL
#                summary (no cap), wrapped in an expandable blockquote on Telegram;
#                otherwise an inline snippet capped to tg_summary_cap.
#   $source $source_icon  — person/group split: $source is the space name (or the
#     localized "Direct message"); $source_icon is 👤 (person/DM) / 👥 (group).
#     $location is a back-compat alias for $source (bare text, pre-split templates).
#   $role $role_icon $role_part  — the mentioning person's role (from config
#     user_profiles). $role_icon is mapped via role_icons keyword rules; $role_part
#     is the ready-to-inline "<icon> <role> · " prefix, or "" when the role is unknown.
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
            "nudge_title": "⏰ Awaiting your reply",
            "nudge_clause": "💬 $count waiting",
            "suggested_replies": "✍️ Suggested replies",
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
            "nudge_title": "⏰ Ждут вашего ответа",
            "nudge_clause": ["💬 $count ждёт", "💬 $count ждут", "💬 $count ждут"],
            "suggested_replies": "✍️ Варианты ответа",
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
            "nudge_title": "⏰ Очікують на відповідь",
            "nudge_clause": ["💬 $count очікує", "💬 $count очікують", "💬 $count очікують"],
            "suggested_replies": "✍️ Варіанти відповіді",
        },
    },
    "profiles": {
        # The user-chosen "Labeled" layout: icon-labelled meta line, a visible 💬 summary,
        # the full original message under an expandable cut, then the link.
        "default": {
            "new_cap": 10,
            # Governs the ORIGINAL-MESSAGE cut length only (the summary is shown in
            # full via summary_expandable). The high/pinned variants route to
            # `detailed` (cap 700) to expand more of the original message.
            "tg_summary_cap": 280,
            "divider": "──────────",
            "counts_sep": " · ",
            "header": "$title\n$counts\n$divider",
            # Line 1: priority icon FIRST, then sender. Line 2: role icon + role text ·
            # source (👤 person / 👥 group). Line 3: send time. Line 4: the full AI
            # summary (collapsed-by-default expandable blockquote on Telegram).
            "new_block": "$icon $sender\n$role_part$source_icon $source\n🕒 $abstime\n💬 $summary$message_block$link_block",
            "overdue_block": "⏰ $sender\n   $promise_label \"$promise\"\n   $due_label $due$link_block",
            # Show the original message under the cut (only when a distinct summary exists).
            "show_message": True,
            # The summary itself is shown in FULL (no truncation) inside an expandable
            # blockquote on Telegram — fully present, visually collapsed when long.
            "summary_expandable": True,
        },
        # Lowest-noise: one meta line, summary only — no expandable message, no link.
        "compact": {
            "new_cap": 15,
            "tg_summary_cap": 0,
            "divider": "──────────",
            "counts_sep": " · ",
            "header": "$title · $counts",
            "new_block": "$icon $sender · $source_icon $source · $abstime\n💬 $summary",
            "overdue_block": "⏰ $sender · $due_label $due · $promise",
            "show_message": False,
            "summary_expandable": False,
        },
        # Richest: same labelled layout as default, but Telegram expands far more of the
        # original message (700 vs 280 chars).
        "detailed": {
            "new_cap": 10,
            "tg_summary_cap": 700,
            "divider": "──────────",
            "counts_sep": " · ",
            "header": "$title\n$counts\n$divider",
            "new_block": "$icon $sender\n$role_part$source_icon $source\n🕒 $abstime\n💬 $summary$message_block$link_block",
            "overdue_block": "⏰ $sender\n   $promise_label \"$promise\"\n   $due_label $due$link_block",
            "show_message": True,
            "summary_expandable": True,
        },
    },
    # Tier-3 reply-nudge block (a SEPARATE digest from the new/overdue one): for items
    # already notified that I haven't answered yet. Layout mirrors the default profile —
    # priority icon + sender, role · source, send time, the full (collapsible) summary —
    # then a SEPARATE collapsible+copyable "suggested replies" block. Placeholders:
    #   $icon $sender $role_part $source_icon $source $abstime $reltime $summary
    #   $suggestions_block (leading-newline label + collapsible copyable drafts, or "")
    #   $link_block (leading-newline "Open in Chat", or "")
    # Header placeholders: $title $counts $divider.
    "nudge": {
        "new_cap": 10,
        "tg_summary_cap": 280,
        "divider": "──────────",
        "counts_sep": " · ",
        "header": "$title\n$counts\n$divider",
        "block": "$icon $sender\n$role_part$source_icon $source\n🕒 $abstime\n💬 $summary$suggestions_block$link_block",
        # FULL summary inside a collapsed-by-default expandable blockquote on Telegram.
        "summary_expandable": True,
    },
    # Per-item profile overrides. First entry whose every ``when`` field matches the item
    # wins. Supported fields: pinned, priority, space_name (≈ project), space_type,
    # trigger (≈ type). The pinned rule is FIRST so a pinned item always renders with the
    # louder ``detailed`` profile, regardless of its priority.
    "variants": [
        {"when": {"pinned": [True]}, "profile": "detailed"},
        {"when": {"priority": ["high"]}, "profile": "detailed"},
    ],
    # Source-line icons: a DM is a 1:1 message from a PERSON; anything else is a
    # GROUP/space where a person @mentioned me. Edit freely.
    "source_icons": {"person": "👤", "group": "👥"},
    # Role → icon. The sender's directory role (config ``user_profiles[<id>].role``,
    # directory cache as fallback) is matched case-insensitively against each rule's
    # keywords IN ORDER; the FIRST rule with any matching keyword wins. ``default`` is
    # used when a role is present but matches nothing. No role at all → no role icon /
    # text is shown (the source icon still appears). Reorder/extend rules to taste —
    # order matters (e.g. "Tech Lead" must precede generic "Developer").
    "role_icons": {
        "default": "👤",
        "rules": [
            {"icon": "👑", "match": ["chief", "ceo", "cto", "coo", "founder"]},
            {"icon": "🎖", "match": ["head of", "director", "vice president", " vp"]},
            {"icon": "🤝", "match": ["technical account", "account manager", "tam",
                                      "relationship", "customer success",
                                      "integration manager", "support"]},
            {"icon": "💰", "match": ["finance", "accountant", "payroll"]},
            {"icon": "🧑‍💼", "match": ["hr ", " hr", "talent", "recruit", "people ops"]},
            {"icon": "💼", "match": ["sales"]},
            {"icon": "🎨", "match": ["designer", "ux", "creative", "graphic",
                                      "motion", "video", "brand", "illustrat"]},
            {"icon": "📣", "match": ["marketing", "content", "editor", "writer",
                                      "ppc", "seo", "copywriter", "social media"]},
            {"icon": "🧭", "match": ["engineering manager", "eng manager",
                                      "team lead", "people manager"]},
            {"icon": "🧪", "match": ["qa", "aqa", "quality assurance", " test"]},
            {"icon": "🛠", "match": ["tech lead", "staff", "principal", "architect",
                                      "chapter lead", "domain expert"]},
            {"icon": "⚙️", "match": ["devops", "sre", "site reliability", "mlops",
                                      "platform", "infrastructure"]},
            {"icon": "🤖", "match": ["data scien", "machine learning", "ml engineer",
                                      "genai", "data engineer", "data operations",
                                      "data analyst"]},
            {"icon": "📦", "match": ["product owner", "product manager",
                                      "technical product", "business analyst", "scrum"]},
            {"icon": "💻", "match": ["developer", "engineer", "back-end", "front-end",
                                      "full-stack", "backend", "frontend", "fullstack",
                                      "android", "ios", "mobile", "programmer", "sde"]},
            {"icon": "🗂", "match": ["operations", "ops manager", "event", "travel",
                                      "office", "administr", "insights"]},
        ],
    },
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
        blocks.append(_render_new_block(it, prof, loc, now=now, mode=mode, link_fn=link_fn, notify=notify, tcfg=tcfg))
    if capped and n > new_cap:
        rest = n - new_cap
        blocks.append(Template(notify._pick_form(loc["more_new"], rest, rule)).safe_substitute(count=rest))
    for it in esc_items:
        prof = _profile(tcfg, _variant_profile_name(it, tcfg))
        blocks.append(_render_overdue_block(it, prof, loc, now=now, mode=mode, link_fn=link_fn, notify=notify))

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


def _full(text) -> str:
    """Flatten newlines + collapse whitespace, NO truncation (the collapsible
    summary shows the complete text; the expandable blockquote handles length)."""
    text = (text or "").replace("\r", " ").replace("\n", " ").strip()
    return " ".join(text.split())


def _role_icon(role: str, tcfg) -> str:
    """Map a free-text job role to an icon via the config-driven keyword rules.

    First matching rule wins (rule order is significant — see DEFAULT_TEMPLATES
    ['role_icons']). Matching is on the RAW (un-escaped) lower-cased role so the
    keywords compare against real text, not HTML entities. Empty/unknown → default."""
    ri = (tcfg.get("role_icons") if isinstance(tcfg, dict) else None) or DEFAULT_TEMPLATES["role_icons"]
    default = ri.get("default", "👤")
    low = (role or "").lower().strip()
    if not low:
        return default
    for rule in ri.get("rules", []):
        for kw in rule.get("match", []):
            if kw and kw.lower() in low:
                return rule.get("icon", default)
    return default


def _source_parts(item, loc, tcfg, *, mode, notify):
    """Compute (source_icon, source_text) — person vs group.

    A DM is a single person; anything else is a group/space where a person
    mentioned me. The icons come from the config-driven ``source_icons`` table."""
    esc = notify._esc
    sicons = (tcfg.get("source_icons") if isinstance(tcfg, dict) else None) or DEFAULT_TEMPLATES["source_icons"]
    if notify._is_dm(item):
        return sicons.get("person", "👤"), esc(loc["direct_message"], mode)
    source = item.get("space_display") or item.get("space_name") or "Chat"
    return sicons.get("group", "👥"), esc(source, mode)


def _render_new_block(item, prof, loc, *, now, mode, link_fn, notify, tcfg=None) -> str:
    esc = notify._esc
    if tcfg is None:
        tcfg = DEFAULT_TEMPLATES
    summary_src = item.get("context_summary") or item.get("text")
    has_summary = bool((item.get("context_summary") or "").strip())

    # A pinned ("starred") item gets the 📌 marker, overriding the priority icon,
    # so it is visually distinct when it rides along on a digest (see
    # notify.pinned_candidates / run_once).
    icon = "📌" if item.get("pinned") else notify._priority_icon(item)
    sender = notify._bold(esc(item.get("sender_name") or loc["unknown_sender"], mode), mode)

    # Source line: person (DM) vs group (space), with role of the mentioning
    # person prefixed when known. $location stays as a back-compat alias for the
    # bare source text (older custom new_block templates may still reference it).
    source_icon, source = _source_parts(item, loc, tcfg, mode=mode, notify=notify)
    role_raw = (item.get("sender_role") or "").strip()
    if role_raw:
        role_icon = _role_icon(role_raw, tcfg)
        role_part = f"{role_icon} {esc(role_raw, mode)} · "
    else:
        role_icon, role_part = "", ""

    created = item.get("created_time")
    reltime = notify.relative_time(created, now, loc)
    abstime = notify.absolute_time(created, now, loc)

    tg_cap = prof.get("tg_summary_cap", 280)

    # Summary rendering: when the profile is collapsible we show the FULL summary
    # (no cap) — in tg_html inside an expandable, collapsed-by-default blockquote;
    # in plain/gchat the full text inline (no native collapse). Otherwise we keep
    # the legacy inline snippet capped to tg_cap.
    expandable = prof.get("summary_expandable", False)
    if expandable:
        full_summary = notify._deautolink(esc(_full(summary_src), mode), mode)
        if mode == "tg_html" and full_summary:
            summary = f"<blockquote expandable>{full_summary}</blockquote>"
        else:
            summary = full_summary
    else:
        summary = notify._deautolink(esc(notify._snippet(summary_src, tg_cap or 140), mode), mode)

    # Original message under the cut: only when the profile allows AND there is a
    # distinct summary (so we never duplicate text==summary).
    message_block = ""
    if prof.get("show_message", True) and has_summary:
        message_block = _message_block(item.get("text"), loc, mode=mode, tg_cap=tg_cap, notify=notify)

    mapping = {
        "icon": icon,
        "sender": sender,
        # $location: legacy alias for the bare source text (pre-person/group split).
        "location": source,
        "source": source,
        "source_icon": source_icon,
        "role": esc(role_raw, mode),
        "role_icon": role_icon,
        "role_part": role_part,
        "reltime": reltime,
        "abstime": abstime,
        # Break domain-like tokens (e.g. "templates.py") in tg_html so Telegram
        # does not auto-linkify them; visually unchanged. No-op elsewhere.
        "summary": summary,
        "message": notify._deautolink(esc(notify._snippet(item.get("text"), tg_cap or 140), mode), mode),
        "message_block": message_block,
        "link_block": _link_block(link_fn(item) if link_fn else None, loc, mode, notify=notify),
        "original_message": esc(loc["original_message"], mode),
    }
    return Template(prof["new_block"]).safe_substitute(mapping)


def _render_overdue_block(item, prof, loc, *, now, mode, link_fn, notify) -> str:
    esc = notify._esc
    sender = notify._bold(esc(item.get("sender_name") or loc["unknown_sender"], mode), mode)
    promise = esc(item.get("my_promise") or "(unspecified)", mode)
    due_raw = notify.human_due(item.get("promise_due"), loc, now=now)
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
        body = notify._deautolink(esc(notify._snippet(text, tg_cap or 280), mode), mode)
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


# --------------------------------------------------------------------------- #
# Tier-3 reply-nudge rendering (a SEPARATE digest message from new/overdue).
# --------------------------------------------------------------------------- #
def _nudge_cfg(tcfg: dict) -> dict:
    """Resolve the nudge block config, filling missing keys from DEFAULT 'nudge'."""
    base = copy.deepcopy(DEFAULT_TEMPLATES["nudge"])
    user = tcfg.get("nudge") if isinstance(tcfg, dict) else None
    if isinstance(user, dict):
        base.update(user)
    return base


def render_nudge(
    items: list,
    *,
    now,
    mode: str,
    templates_cfg=None,
    link_fn=None,
) -> str:
    """Render the reply-nudge digest from config-driven templates.

    ``items`` are open items I haven't answered yet (each may carry
    ``reply_suggestions``). ``mode`` ∈ {'plain', 'gchat', 'tg_html'}. Same
    escaping discipline as :func:`render` — every substituted value is UNTRUSTED
    and escaped per channel via ``notify._esc`` before substitution.
    """
    import notify  # function-local: avoids an import cycle, reuses the escape layer

    if link_fn is None:
        link_fn = notify.chat_room_link

    tcfg = _cfg(templates_cfg)
    loc = _locale(tcfg)
    nudge = _nudge_cfg(tcfg)

    header = _render_nudge_header(nudge, loc, len(items), notify=notify)
    blocks = [
        _render_nudge_block(it, nudge, loc, now=now, mode=mode, link_fn=link_fn, notify=notify, tcfg=tcfg)
        for it in items
    ]
    if not blocks:
        return header
    return header + "\n" + "\n\n".join(blocks)


def _render_nudge_header(nudge: dict, loc: dict, n: int, *, notify) -> str:
    rule = loc.get("plural", "en")
    counts = Template(notify._pick_form(loc["nudge_clause"], n, rule)).safe_substitute(count=n)
    out = Template(nudge["header"]).safe_substitute(
        title=loc["nudge_title"], counts=counts, divider=nudge.get("divider", "")
    )
    return "\n".join(line for line in out.split("\n") if line.strip())


def _render_nudge_block(item, nudge, loc, *, now, mode, link_fn, notify, tcfg) -> str:
    esc = notify._esc
    summary_src = item.get("context_summary") or item.get("text")

    icon = notify._priority_icon(item)
    sender = notify._bold(esc(item.get("sender_name") or loc["unknown_sender"], mode), mode)

    source_icon, source = _source_parts(item, loc, tcfg, mode=mode, notify=notify)
    role_raw = (item.get("sender_role") or "").strip()
    if role_raw:
        role_part = f"{_role_icon(role_raw, tcfg)} {esc(role_raw, mode)} · "
    else:
        role_part = ""

    created = item.get("created_time")
    abstime = notify.absolute_time(created, now, loc)
    reltime = notify.relative_time(created, now, loc)

    if nudge.get("summary_expandable", True):
        full_summary = notify._deautolink(esc(_full(summary_src), mode), mode)
        if mode == "tg_html" and full_summary:
            summary = f"<blockquote expandable>{full_summary}</blockquote>"
        else:
            summary = full_summary
    else:
        tg_cap = nudge.get("tg_summary_cap", 280)
        summary = notify._deautolink(esc(notify._snippet(summary_src, tg_cap or 140), mode), mode)

    mapping = {
        "icon": icon,
        "sender": sender,
        "location": source,
        "source": source,
        "source_icon": source_icon,
        "role_part": role_part,
        "abstime": abstime,
        "reltime": reltime,
        "summary": summary,
        "suggestions_block": _suggestions_block(item.get("reply_suggestions"), loc, mode=mode, notify=notify),
        "link_block": _link_block(link_fn(item) if link_fn else None, loc, mode, notify=notify),
    }
    return Template(nudge["block"]).safe_substitute(mapping)


def _suggestions_block(suggestions, loc, *, mode, notify) -> str:
    """The collapsible + copyable reply-suggestions wrapper. '' when there are none.

    tg_html: a bold label, then a collapsed-by-default expandable blockquote whose
    every line is a ``<code>`` span — so the whole block COLLAPSES (blockquote) yet
    each suggestion is individually tap-to-copy (code). Verified live: ``<code>``
    nests cleanly inside ``<blockquote expandable>``. NOTE: we do NOT ``_deautolink``
    inside ``<code>`` — Telegram never auto-linkifies code spans, and the zero-width
    joiners would otherwise be copied into the clipboard and corrupt the pasted text.
    Each suggestion is html-escaped, so an injected ``</code>``/``</blockquote>``
    becomes inert entities and cannot break out. plain/gchat: an indented label +
    numbered lines (no native collapse/copy).
    """
    esc = notify._esc
    drafts = [s for s in (suggestions or []) if isinstance(s, str) and s.strip()]
    if not drafts:
        return ""
    label = esc(loc["suggested_replies"], mode)
    if mode == "tg_html":
        lines = "\n".join(f"<code>{esc(_full(s), mode)}</code>" for s in drafts)
        return f"\n<b>{label}</b>\n<blockquote expandable>{lines}</blockquote>"
    indent = notify._INDENT
    lines = "\n".join(f"{indent}{i}. {esc(_full(s), mode)}" for i, s in enumerate(drafts, 1))
    return f"\n{indent}{label}\n{lines}"
