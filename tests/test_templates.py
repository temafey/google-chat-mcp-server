"""Tests for the config-driven template engine in scripts/templates.py.

Covers the layers the engine adds on top of the (already-tested) escaping primitives in
notify.py: locale resolution, profile resolution, per-item variant selection, the
``string.Template`` safe-substitution contract (no format-string injection, no recursive
substitution, missing placeholders left literal), profile-driven caps / message blocks,
the localized link label, and the config embedding + deep-merge upgrade path.
"""
from __future__ import annotations

import copy
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import notify  # noqa: E402
import templates  # noqa: E402

NOW = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)


def _item(iid="n0", **over) -> dict:
    base = {
        "id": iid,
        "space_name": "spaces/AAQAugHrEgY",
        "message_name": f"spaces/AAQAugHrEgY/messages/{iid}.{iid}",
        "sender_id": "users/sender",
        "sender_name": "Mariia Ivanova",
        "text": "the full original message body",
        "trigger": "user_mention",
        "space_type": "GROUP_CHAT",
        "space_display": "Mobile internal",
        "priority": "normal",  # default profile (no variant) unless overridden
        "created_time": "2026-06-04T10:00:00Z",
        "context_summary": "short human summary",
        "my_promise": None,
        "promise_due": None,
    }
    base.update(over)
    return base


def _tcfg(**over) -> dict:
    t = copy.deepcopy(templates.DEFAULT_TEMPLATES)
    t.update(over)
    return t


# --------------------------------------------------------------------------- #
# Locale resolution.
# --------------------------------------------------------------------------- #
def test_locale_ru_labels_render():
    card = notify.render_card([_item()], [], now=NOW, mode="plain", templates_cfg=_tcfg(locale="ru"))
    assert "📥 Триаж чатов" in card
    assert "🆕 1 новое" in card  # 'one' plural form
    assert "Исходное сообщение" in card  # localized "Original message"
    assert "🔗 Открыть в чате:" in card  # localized link label


def test_locale_uk_labels_render():
    card = notify.render_card([_item()], [], now=NOW, mode="plain", templates_cfg=_tcfg(locale="uk"))
    assert "📥 Тріаж чатів" in card
    assert "🆕 1 нове" in card  # 'one' plural form
    assert "Оригінальне повідомлення" in card  # localized "Original message"
    assert "🔗 Відкрити в чаті:" in card  # localized link label


def test_three_locales_shipped():
    assert set(templates.DEFAULT_TEMPLATES["locales"]) >= {"en", "ru", "uk"}


def test_locale_partial_custom_fills_from_en():
    # A custom 'en' locale that overrides ONLY the title — every other label falls back
    # to the built-in default, never a bare $placeholder.
    t = _tcfg()
    t["locales"]["en"] = {"header_title": "📨 Inbox"}
    card = notify.render_card([_item()], [], now=NOW, mode="plain", templates_cfg=t)
    assert "📨 Inbox" in card
    assert "$" not in card  # no unresolved placeholder leaked
    assert "🆕 1 new" in card  # filled from default en


def test_locale_unknown_falls_back_to_en():
    card = notify.render_card([_item()], [], now=NOW, mode="plain", templates_cfg=_tcfg(locale="xx"))
    assert "📥 Chat triage" in card  # default en title


# --------------------------------------------------------------------------- #
# Profile resolution.
# --------------------------------------------------------------------------- #
def test_compact_profile_is_single_line_no_message_no_link():
    card = notify.render_card(
        [_item()], [], now=NOW, mode="plain", templates_cfg=_tcfg(active_profile="compact")
    )
    # Absolute send time (never goes stale), not a relative age, in the meta line.
    # normal → 🟡; group space → 👥 before the source name.
    assert "🟡 Mariia Ivanova · 👥 Mobile internal · 04.06 10:00" in card
    assert "💬 short human summary" in card
    assert "Original message" not in card  # compact hides the cut
    assert "🔗" not in card  # compact hides the link


def test_detailed_profile_expands_more_on_telegram():
    long = _item(priority="normal", context_summary="sum", text="Z" * 500)
    default = notify.render_card([long], [], now=NOW, mode="tg_html", templates_cfg=_tcfg())
    detailed = notify.render_card(
        [long], [], now=NOW, mode="tg_html", templates_cfg=_tcfg(active_profile="detailed")
    )
    assert default.count("Z") <= 280
    assert detailed.count("Z") > 280  # detailed cap is 700


def test_unknown_active_profile_falls_back_to_default_layout():
    card = notify.render_card(
        [_item()], [], now=NOW, mode="plain", templates_cfg=_tcfg(active_profile="does-not-exist")
    )
    # Default labelled layout still renders (no crash, no bare placeholder).
    assert "👥 Mobile internal" in card
    assert "🕒 04.06 10:00" in card
    assert "$" not in card


# --------------------------------------------------------------------------- #
# Variant selection (priority / project=space / type).
# --------------------------------------------------------------------------- #
def test_variant_priority_match_is_case_insensitive():
    assert templates._variant_matches(_item(priority="HIGH"), {"priority": ["high"]})
    assert templates._variant_matches(_item(priority="high"), {"priority": ["HIGH"]})
    assert not templates._variant_matches(_item(priority="normal"), {"priority": ["high"]})


def test_variant_matches_on_space_and_trigger():
    it = _item(space_name="spaces/PROJ", trigger="direct_dm")
    assert templates._variant_matches(it, {"space_name": ["spaces/PROJ"]})
    assert templates._variant_matches(it, {"trigger": ["direct_dm"]})
    # ALL fields must match (AND semantics).
    assert templates._variant_matches(it, {"space_name": ["spaces/PROJ"], "trigger": ["direct_dm"]})
    assert not templates._variant_matches(it, {"space_name": ["spaces/PROJ"], "trigger": ["user_mention"]})


def test_empty_when_never_matches():
    assert not templates._variant_matches(_item(), {})


def test_default_variant_routes_high_priority_to_detailed():
    # The shipped variant: high → detailed (bigger tg expand).
    long = _item(priority="high", context_summary="sum", text="Z" * 500)
    card = notify.render_card([long], [], now=NOW, mode="tg_html", templates_cfg=_tcfg())
    assert card.count("Z") > 280  # detailed cap applied via the variant


def test_default_variant_no_urgent_key():
    # "urgent" is not in the AI taxonomy — the default variants list must NOT reference it.
    import json
    variants = templates.DEFAULT_TEMPLATES.get("variants", [])
    serialized = json.dumps(variants)
    assert "urgent" not in serialized, (
        "Dead key 'urgent' found in DEFAULT_TEMPLATES variants — remove it."
    )


def test_default_variant_normal_priority_not_detailed():
    # normal priority must NOT trigger the high→detailed variant.
    normal = _item(priority="normal", context_summary="sum", text="Z" * 500)
    card = notify.render_card([normal], [], now=NOW, mode="tg_html", templates_cfg=_tcfg())
    assert card.count("Z") <= 280  # default profile cap 280, not detailed 700


def test_pinned_item_renders_pin_icon_override():
    # A pinned item gets 📌 instead of its priority icon.
    pinned = _item(priority="normal", pinned=True, context_summary="s", text="body")
    card = notify.render_card([pinned], [], now=NOW, mode="plain", templates_cfg=_tcfg())
    assert "📌" in card
    assert "🟡" not in card  # the 'normal' priority icon is overridden


def test_pinned_variant_routes_to_detailed():
    # The shipped pinned → detailed variant: a pinned (even normal-priority) item
    # gets the detailed profile's bigger Telegram expand.
    pinned = _item(priority="normal", pinned=True, context_summary="sum", text="Z" * 500)
    card = notify.render_card([pinned], [], now=NOW, mode="tg_html", templates_cfg=_tcfg())
    assert card.count("Z") > 280  # detailed cap applied via the pinned variant


def test_pinned_variant_matches_boolean():
    assert templates._variant_matches(_item(pinned=True), {"pinned": [True]})
    assert not templates._variant_matches(_item(pinned=False), {"pinned": [True]})
    assert not templates._variant_matches(_item(), {"pinned": [True]})  # absent → no match


def test_variant_only_affects_blocks_not_header():
    # active_profile=compact, but a high item is routed to detailed by the variant.
    # The HEADER stays compact (single line); only that item's block is detailed.
    t = _tcfg(active_profile="compact")
    normal = _item("a", priority="normal", text="n")
    high = _item("b", priority="high", context_summary="s", text="the message")
    card = notify.render_card([normal, high], [], now=NOW, mode="plain", templates_cfg=t)
    assert "📥 Chat triage · 🆕 2 new" in card  # compact header
    # normal item → compact (no cut); high item → detailed (cut present).
    assert "Original message" in card


# --------------------------------------------------------------------------- #
# safe_substitute contract.
# --------------------------------------------------------------------------- #
def test_missing_placeholder_left_literal_not_crash():
    t = _tcfg()
    t["profiles"]["default"]["new_block"] = "$icon $sender — $totally_unknown"
    card = notify.render_card([_item()], [], now=NOW, mode="plain", templates_cfg=t)
    assert "$totally_unknown" in card  # left verbatim, no KeyError


def test_value_is_not_recursively_substituted():
    # An item value that LOOKS like a placeholder must NOT be re-expanded.
    evil = _item(context_summary="", text="$location and ${icon}")
    card = notify.render_card([evil], [], now=NOW, mode="plain", templates_cfg=_tcfg())
    assert "$location and ${icon}" in card  # literal, not replaced by location/icon


def test_no_format_string_injection():
    # A classic str.format attack payload is inert under string.Template.
    evil = _item(context_summary="", text="{0.__class__.__mro__}")
    card = notify.render_card([evil], [], now=NOW, mode="plain", templates_cfg=_tcfg())
    assert "{0.__class__.__mro__}" in card  # literal — no attribute walking


# --------------------------------------------------------------------------- #
# Caps + message block sourcing.
# --------------------------------------------------------------------------- #
def test_profile_new_cap_applies_without_explicit_cap():
    items = [_item(f"n{i}", priority="normal") for i in range(14)]
    card = notify.render_card(items, [], now=NOW, mode="plain", templates_cfg=_tcfg())  # default cap 10
    icon_lines = [ln for ln in card.splitlines() if ln.startswith("🟡")]
    assert len(icon_lines) == 10
    assert "…and 4 more new" in card


def test_explicit_cap_overrides_profile_cap():
    items = [_item(f"n{i}", priority="normal") for i in range(14)]
    card = notify.render_card(items, [], now=NOW, mode="plain", templates_cfg=_tcfg(), cap=3)
    icon_lines = [ln for ln in card.splitlines() if ln.startswith("🟡")]
    assert len(icon_lines) == 3
    assert "…and 11 more new" in card


def test_message_block_omitted_when_summary_equals_text():
    # No distinct summary → no duplicated original-message cut.
    it = _item(context_summary="", text="hi")
    card = notify.render_card([it], [], now=NOW, mode="plain", templates_cfg=_tcfg())
    assert "Original message" not in card
    assert "💬 hi" in card


def test_message_block_present_when_summary_distinct():
    it = _item(context_summary="a summary", text="the body")
    card = notify.render_card([it], [], now=NOW, mode="plain", templates_cfg=_tcfg())
    assert "💬 a summary" in card
    assert "   Original message" in card
    assert "   the body" in card


# --------------------------------------------------------------------------- #
# SECURITY parity across modes (the engine must route ALL dynamic text through _esc).
# --------------------------------------------------------------------------- #
def test_injection_escaped_in_tg_html_summary_and_message():
    evil = _item(
        sender_name="<b>boss</b>",
        context_summary="<script>s</script>",
        text="<img src=x onerror=1>",
    )
    card = notify.render_card([evil], [], now=NOW, mode="tg_html", templates_cfg=_tcfg())
    assert "<script>" not in card
    assert "&lt;script&gt;" in card
    assert "<img" not in card
    assert "&lt;img src=x onerror=1&gt;" in card
    assert "<b>&lt;b&gt;boss&lt;/b&gt;</b>" in card  # our bold wraps the escaped name


def test_injection_defanged_in_gchat_message_block():
    evil = _item(
        context_summary="legit",
        text="<https://phish|click> <users/all> *x*",
    )
    card = notify.render_card([evil], [], now=NOW, mode="gchat", templates_cfg=_tcfg())
    assert "<https://phish|" not in card
    assert "<users/all>" not in card
    # Only our own permalink survives as a live <…|…> structure.
    assert card.count("<https://") == 1


def test_plain_mode_passthrough_in_engine():
    it = _item(sender_name="<b>x</b>", context_summary="a & b < c", text="d > e")
    card = notify.render_card([it], [], now=NOW, mode="plain", templates_cfg=_tcfg())
    assert "<b>x</b>" in card
    assert "a & b < c" in card
    assert "d > e" in card


# --------------------------------------------------------------------------- #
# Config embedding + deep-merge upgrade.
# --------------------------------------------------------------------------- #
def test_default_config_embeds_templates():
    assert "templates" in config.DEFAULT_CONFIG
    assert config.DEFAULT_CONFIG["templates"]["active_profile"] == "default"
    assert {"en", "ru", "uk"} <= set(config.DEFAULT_CONFIG["templates"]["locales"])


def test_load_config_upgrades_partial_templates(tmp_path):
    # A config that pins only active_profile must keep that value AND gain the full
    # default profiles/locales via deep-merge.
    cfg_path = tmp_path / "config.json"
    import json
    cfg_path.write_text(json.dumps({"templates": {"active_profile": "compact"}}), encoding="utf-8")
    merged = config.load_config(cfg_path)
    assert merged["templates"]["active_profile"] == "compact"  # user value preserved
    assert "default" in merged["templates"]["profiles"]  # default profiles filled in
    assert merged["templates"]["locales"]["ru"]["header_title"] == "📥 Триаж чатов"


# --------------------------------------------------------------------------- #
# Localized dates + declensions (plurals).
# --------------------------------------------------------------------------- #
RU = templates._locale({"locale": "ru"})
UK = templates._locale({"locale": "uk"})
EN = templates._locale({"locale": "en"})


def _ago(**kw) -> str:
    return (NOW - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_plural_index_slavic_categories():
    # one / few / many → 0 / 1 / 2 under the CLDR-ish slavic rule.
    assert [notify._plural_index(n, "slavic") for n in (1, 21, 31)] == [0, 0, 0]
    assert [notify._plural_index(n, "slavic") for n in (2, 3, 4, 22)] == [1, 1, 1, 1]
    assert [notify._plural_index(n, "slavic") for n in (5, 11, 12, 14, 25)] == [2, 2, 2, 2, 2]


def test_plural_index_english_is_one_other():
    assert notify._plural_index(1, "en") == 0
    assert notify._plural_index(2, "en") == 1


def test_relative_time_english_default_unchanged():
    assert notify.relative_time(_ago(seconds=10), NOW) == "just now"
    assert notify.relative_time(_ago(minutes=30), NOW) == "30m ago"
    assert notify.relative_time(_ago(hours=2), NOW) == "2h ago"
    assert notify.relative_time(_ago(days=3), NOW) == "3d ago"


def test_relative_time_localized_ru_declensions():
    assert notify.relative_time(_ago(seconds=5), NOW, RU) == "только что"
    assert notify.relative_time(_ago(minutes=1), NOW, RU) == "1 минуту назад"
    assert notify.relative_time(_ago(minutes=2), NOW, RU) == "2 минуты назад"
    assert notify.relative_time(_ago(minutes=5), NOW, RU) == "5 минут назад"
    assert notify.relative_time(_ago(hours=2), NOW, RU) == "2 часа назад"
    assert notify.relative_time(_ago(days=5), NOW, RU) == "5 дней назад"


def test_relative_time_localized_uk_declensions():
    assert notify.relative_time(_ago(seconds=5), NOW, UK) == "щойно"
    assert notify.relative_time(_ago(minutes=1), NOW, UK) == "1 хвилину тому"
    assert notify.relative_time(_ago(minutes=3), NOW, UK) == "3 хвилини тому"
    assert notify.relative_time(_ago(minutes=5), NOW, UK) == "5 хвилин тому"


def test_absolute_time_renders_send_time_not_age():
    # Absolute time shows WHEN the message was sent (never stale), in NOW's tz (UTC here).
    # The month is a number, so the output is locale-independent.
    sent = "2026-06-04T10:00:00Z"
    assert notify.absolute_time(sent, NOW) == "04.06 10:00"
    assert notify.absolute_time(sent, NOW, RU) == "04.06 10:00"
    assert notify.absolute_time(sent, NOW, UK) == "04.06 10:00"


def test_absolute_time_uses_now_timezone():
    # Rendered in NOW's tz, so a +03:00 'now' shifts the wall clock by 3h.
    from zoneinfo import ZoneInfo
    sent = "2026-06-04T10:00:00Z"
    kyiv_now = datetime(2026, 6, 4, 15, 0, tzinfo=ZoneInfo("Europe/Kyiv"))  # UTC+3 in June
    assert notify.absolute_time(sent, kyiv_now) == "04.06 13:00"


def test_absolute_time_junk_or_missing_is_empty():
    assert notify.absolute_time(None, NOW) == ""
    assert notify.absolute_time("not-a-date", NOW) == ""


def test_human_due_localized_months():
    due = "2026-06-03T15:00:00Z"
    assert notify.human_due(due, EN) == "03 Jun 15:00"
    assert notify.human_due(due, RU) == "03 июн 15:00"
    assert notify.human_due(due, UK) == "03 чер 15:00"


def test_human_due_renders_in_now_timezone():
    from zoneinfo import ZoneInfo
    due = "2026-06-03T15:00:00Z"
    kyiv_now = datetime(2026, 6, 3, 18, 0, tzinfo=ZoneInfo("Europe/Kyiv"))  # UTC+3
    assert notify.human_due(due, EN, now=kyiv_now) == "03 Jun 18:00"


def test_human_due_localized_no_due():
    assert notify.human_due(None, RU) == "(без срока)"
    assert notify.human_due(None, UK) == "(без терміну)"
    assert notify.human_due(None) == "(no due date)"


def test_header_count_declensions_ru():
    def hdr(count):
        items = [_item(f"n{i}", priority="normal") for i in range(count)]
        return notify.render_card(items, [], now=NOW, mode="plain", templates_cfg=_tcfg(locale="ru"))
    assert "🆕 1 новое" in hdr(1)   # one
    assert "🆕 2 новых" in hdr(2)   # few
    assert "🆕 5 новых" in hdr(5)   # many


def test_header_count_declensions_uk():
    def hdr(count):
        items = [_item(f"n{i}", priority="normal") for i in range(count)]
        return notify.render_card(items, [], now=NOW, mode="plain", templates_cfg=_tcfg(locale="uk"))
    assert "🆕 1 нове" in hdr(1)    # one
    assert "🆕 2 нові" in hdr(2)    # few
    assert "🆕 5 нових" in hdr(5)   # many


def test_more_new_line_is_declined():
    items = [_item(f"n{i}", priority="normal") for i in range(12)]  # default cap 10 → 2 more
    card = notify.render_card(items, [], now=NOW, mode="plain", templates_cfg=_tcfg(locale="uk"))
    assert "…та ще 2 нові" in card  # 'few' form for the remainder


def test_overdue_block_uses_localized_month_and_reltime():
    overdue = _item(
        "o0",
        my_promise="ship the build",
        promise_due="2026-06-03T15:00:00Z",
    )
    card = notify.render_card([], [overdue], now=NOW, mode="plain", templates_cfg=_tcfg(locale="ru"))
    assert "до 03 июн 15:00" in card


# --------------------------------------------------------------------------- #
# Role icons + person/group source split.
# --------------------------------------------------------------------------- #
def test_role_icon_keyword_rules():
    t = templates.DEFAULT_TEMPLATES
    # First-match-wins ordering: QA before generic engineer; tech-lead before dev.
    assert templates._role_icon("Back-End Tech Lead", t) == "🛠"
    assert templates._role_icon("Staff Software Engineer (QA)", t) == "🧪"
    assert templates._role_icon("Engineering Manager", t) == "🧭"
    assert templates._role_icon("Senior Backend Developer", t) == "💻"
    assert templates._role_icon("Chief Executive Officer", t) == "👑"
    assert templates._role_icon("DevOps Engineer", t) == "⚙️"
    assert templates._role_icon("Product Manager", t) == "📦"


def test_role_icon_default_for_unknown_or_empty():
    t = templates.DEFAULT_TEMPLATES
    assert templates._role_icon("", t) == "👤"
    assert templates._role_icon("Supreme Wizard of Nothing", t) == "👤"


def test_role_line_rendered_when_sender_role_present():
    it = _item(priority="high", sender_role="Back-End Tech Lead")
    card = notify.render_card([it], [], now=NOW, mode="plain", templates_cfg=_tcfg())
    # Role icon + role text · group icon + space, all on the source line.
    assert "🛠 Back-End Tech Lead · 👥 Mobile internal" in card


def test_role_line_omitted_when_no_role():
    it = _item(priority="high")  # no sender_role
    card = notify.render_card([it], [], now=NOW, mode="plain", templates_cfg=_tcfg())
    # Bare source line: group icon + space, no role prefix, no dangling separator.
    assert "👥 Mobile internal" in card
    assert "· 👥" not in card


def test_person_source_icon_for_dm():
    dm = _item(priority="high", trigger="direct_dm", space_type="DIRECT_MESSAGE", space_display=None)
    card = notify.render_card([dm], [], now=NOW, mode="plain", templates_cfg=_tcfg())
    assert "👤 Direct message" in card


def test_group_source_icon_for_space():
    it = _item(priority="high", space_type="SPACE", space_display="Mobile Team")
    card = notify.render_card([it], [], now=NOW, mode="plain", templates_cfg=_tcfg())
    assert "👥 Mobile Team" in card


def test_role_text_is_escaped_per_channel():
    # An injected role string must be escaped like any other untrusted value.
    it = _item(priority="high", sender_role="<b>boss</b>")
    card = notify.render_card([it], [], now=NOW, mode="tg_html", templates_cfg=_tcfg())
    assert "&lt;b&gt;boss&lt;/b&gt;" in card
    assert "<b>boss</b>" not in card


def test_full_summary_in_expandable_blockquote_on_telegram():
    long_summary = "S" * 600
    it = _item(priority="high", context_summary=long_summary, text="orig body")
    card = notify.render_card([it], [], now=NOW, mode="tg_html", templates_cfg=_tcfg())
    # The summary is shown IN FULL (no truncation) inside an expandable quote.
    assert f"💬 <blockquote expandable>{long_summary}</blockquote>" in card
    assert card.count("S") == 600  # nothing trimmed


def test_compact_summary_is_inline_not_expandable():
    it = _item(priority="normal", context_summary="a concise summary", text="body")
    card = notify.render_card([it], [], now=NOW, mode="tg_html", templates_cfg=_tcfg(active_profile="compact"))
    assert "blockquote" not in card  # compact keeps the summary inline
    assert "💬 a concise summary" in card
