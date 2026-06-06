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
from datetime import datetime, timezone
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
    assert "🆕 1 новых" in card
    assert "Исходное сообщение" in card  # localized "Original message"
    assert "🔗 Открыть в чате:" in card  # localized link label


def test_locale_uk_labels_render():
    card = notify.render_card([_item()], [], now=NOW, mode="plain", templates_cfg=_tcfg(locale="uk"))
    assert "📥 Тріаж чатів" in card
    assert "🆕 1 нових" in card
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
    assert "🟢 Mariia Ivanova · Mobile internal · just now" not in card  # 2h not 'just now'
    assert "🟢 Mariia Ivanova · Mobile internal · 2h ago" in card
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
    assert "📍 Mobile internal · 🕒 2h ago" in card
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
    # The shipped example variant: urgent/high → detailed (bigger tg expand).
    long = _item(priority="high", context_summary="sum", text="Z" * 500)
    card = notify.render_card([long], [], now=NOW, mode="tg_html", templates_cfg=_tcfg())
    assert card.count("Z") > 280  # detailed cap applied via the variant


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
    icon_lines = [ln for ln in card.splitlines() if ln.startswith("🟢")]
    assert len(icon_lines) == 10
    assert "…and 4 more new" in card


def test_explicit_cap_overrides_profile_cap():
    items = [_item(f"n{i}", priority="normal") for i in range(14)]
    card = notify.render_card(items, [], now=NOW, mode="plain", templates_cfg=_tcfg(), cap=3)
    icon_lines = [ln for ln in card.splitlines() if ln.startswith("🟢")]
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
