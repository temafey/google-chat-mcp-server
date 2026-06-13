"""Tests for the per-channel 'Card' rendering layer in scripts/notify.py.

Covers the pure formatting/helper surface added in the Card rework:
``relative_time``, ``human_due``, ``chat_room_link`` (room-level), and
``render_card`` across the three markup dialects (plain / gchat / tg_html),
including the tg_html injection-escaping SECURITY guarantee.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# scripts/ on the path so ``import notify`` resolves, mirroring the sibling
# notify test modules.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notify  # noqa: E402

NOW = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)


def _item(iid="n0", **over) -> dict:
    base = {
        "id": iid,
        "space_name": "spaces/AAQAugHrEgY",
        "message_name": f"spaces/AAQAugHrEgY/messages/{iid}.{iid}",
        "sender_id": "users/sender",
        "sender_name": "Mariia Ivanova",
        "text": "hello there",
        "trigger": "user_mention",
        "space_type": "GROUP_CHAT",
        "space_display": "Mobile internal",
        "priority": "high",
        "created_time": "2026-06-04T10:00:00Z",  # 2h before NOW
        "context_summary": "API audit: enumerate group-chat system messages",
        "my_promise": None,
        "promise_due": None,
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# relative_time — coarse buckets, injected ``now``.
# --------------------------------------------------------------------------- #
def test_relative_time_buckets():
    assert notify.relative_time("2026-06-04T11:59:40Z", NOW) == "just now"
    assert notify.relative_time("2026-06-04T11:30:00Z", NOW) == "30m ago"
    assert notify.relative_time("2026-06-04T10:00:00Z", NOW) == "2h ago"
    assert notify.relative_time("2026-06-01T12:00:00Z", NOW) == "3d ago"


def test_relative_time_future_collapses_to_just_now():
    future = (NOW + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    assert notify.relative_time(future, NOW) == "just now"


def test_relative_time_empty_or_garbage_returns_blank():
    assert notify.relative_time(None, NOW) == ""
    assert notify.relative_time("", NOW) == ""
    assert notify.relative_time("not-a-date", NOW) == ""
    assert notify.relative_time("2026-06-04T10:00:00Z", None) == ""


def test_relative_time_handles_fractional_seconds():
    # store created_time carries microseconds — must parse cleanly.
    assert notify.relative_time("2026-06-04T09:30:00.999999Z", NOW) == "2h ago"


# --------------------------------------------------------------------------- #
# human_due — deterministic '%d %b %H:%M'.
# --------------------------------------------------------------------------- #
def test_human_due_formats():
    assert notify.human_due("2026-06-04T18:00:00Z") == "04 Jun 18:00"


def test_human_due_missing():
    assert notify.human_due(None) == "(no due date)"
    assert notify.human_due("") == "(no due date)"


# --------------------------------------------------------------------------- #
# chat_room_link — ROOM-LEVEL string construction (no message segment).
# --------------------------------------------------------------------------- #
def test_room_link_is_room_level():
    # A hand-built URL resolves to the SPACE (room); the message segment is NOT
    # constructable from the API resource name, so it is omitted entirely.
    item = _item()
    url = notify.chat_room_link(item)
    assert url == "https://chat.google.com/room/AAQAugHrEgY"
    assert "/messages/" not in url
    assert "n0.n0" not in url  # message id never enters the URL


def test_room_link_ignores_message_name():
    # Even with a fully-populated message_name, output stays room-level.
    item = {
        "space_name": "spaces/AAQAugHrEgY",
        "message_name": "spaces/AAQAugHrEgY/messages/L1iw1LfMeSg.L1iw1LfMeSg",
    }
    assert notify.chat_room_link(item) == "https://chat.google.com/room/AAQAugHrEgY"


def test_room_link_omitted_when_no_space():
    assert notify.chat_room_link({"space_name": None, "message_name": None}) is None
    assert notify.chat_room_link({}) is None


# --------------------------------------------------------------------------- #
# Priority icon map — aligned to the AI taxonomy: high | normal | low | unset.
# --------------------------------------------------------------------------- #
def test_priority_icon_map():
    # Real taxonomy values: high → 🔥, normal → 🟡, low/unset → 🟢.
    assert notify._priority_icon(_item(priority="high")) == "🔥"
    assert notify._priority_icon(_item(priority="normal")) == "🟡"
    assert notify._priority_icon(_item(priority="low")) == "🟢"
    # Neutral defaults — no crash; low and "no priority yet" share the calm green.
    assert notify._priority_icon(_item(priority="unset")) == "🟢"
    assert notify._priority_icon(_item(priority=None)) == "🟢"
    assert notify._priority_icon(_item(priority="")) == "🟢"


def test_priority_icon_case_insensitive():
    assert notify._priority_icon(_item(priority="HIGH")) == "🔥"
    assert notify._priority_icon(_item(priority="Normal")) == "🟡"
    assert notify._priority_icon(_item(priority="LOW")) == "🟢"


def test_priority_icon_dead_keys_removed():
    # "urgent" and "medium" were pre-taxonomy dead keys — now fall through to 🟢.
    assert notify._priority_icon(_item(priority="urgent")) == "🟢"
    assert notify._priority_icon(_item(priority="medium")) == "🟢"


# --------------------------------------------------------------------------- #
# render_card — header variants.
# --------------------------------------------------------------------------- #
def test_header_both_clauses():
    card = notify.render_card(
        [_item()], [_item("o0", my_promise="x", promise_due="2026-06-04T08:00:00Z")],
        now=NOW, mode="plain",
    )
    lines = card.splitlines()
    assert lines[0] == "📥 Chat triage"
    assert lines[1] == "🆕 1 new · ⏰ 1 overdue"
    assert lines[2] == "──────────"


def test_header_zero_new():
    card = notify.render_card(
        [], [_item("o0", my_promise="x", promise_due="2026-06-04T08:00:00Z")],
        now=NOW, mode="plain",
    )
    assert card.splitlines()[1] == "⏰ 1 overdue"


def test_header_zero_overdue():
    card = notify.render_card([_item()], [], now=NOW, mode="plain")
    assert card.splitlines()[1] == "🆕 1 new"


# --------------------------------------------------------------------------- #
# render_card — NEW block content per mode.
# --------------------------------------------------------------------------- #
def test_plain_new_block_layout():
    card = notify.render_card([_item()], [], now=NOW, mode="plain")
    assert "🔥 Mariia Ivanova" in card  # high → 🔥, no bold markup
    # Source line: group icon + space name (no role → no role prefix).
    assert "👥 Mobile internal" in card
    assert "🕒 04.06 10:00" in card  # absolute send time, never stale, own line
    assert "💬 API audit: enumerate group-chat system messages" in card  # summary line
    # The original message appears under a labelled cut (distinct from the summary).
    assert "   Original message" in card
    assert "   hello there" in card
    assert "🔗 Open in Chat: https://chat.google.com/room/AAQAugHrEgY" in card


def test_gchat_new_block_uses_chat_markup():
    card = notify.render_card([_item()], [], now=NOW, mode="gchat")
    assert "🔥 *Mariia Ivanova*" in card  # *bold*
    assert "<https://chat.google.com/room/AAQAugHrEgY|🔗 Open in Chat>" in card
    # Exactly ONE link line, pointing at the room URL.
    assert card.count("🔗 Open in Chat") == 1


def test_tg_html_new_block_uses_html_markup():
    card = notify.render_card([_item()], [], now=NOW, mode="tg_html")
    assert "🔥 <b>Mariia Ivanova</b>" in card
    assert (
        '<a href="https://chat.google.com/room/AAQAugHrEgY">🔗 Open in Chat</a>'
        in card
    )
    # Exactly ONE link line, pointing at the room URL.
    assert card.count("🔗 Open in Chat") == 1


# --------------------------------------------------------------------------- #
# Telegram expandable blockquotes — the FULL summary AND the original message
# each get their own collapsed-by-default quote; the link stays visible after.
# --------------------------------------------------------------------------- #
def test_tg_html_summary_and_original_message_each_in_expandable_blockquote():
    card = notify.render_card([_item()], [], now=NOW, mode="tg_html")
    # Two distinct quotes: the FULL summary on the 💬 line, the original under the cut.
    assert card.count("<blockquote expandable>") == 2
    assert card.count("</blockquote>") == 2
    # The summary is shown IN FULL inside its own expandable quote.
    assert (
        "💬 <blockquote expandable>API audit: enumerate group-chat system messages"
        "</blockquote>" in card
    )
    # The original message lives in its own quote under the labelled cut.
    assert "<blockquote expandable>hello there</blockquote>" in card
    # The summary quote precedes the original-message quote.
    assert card.index("API audit") < card.index("hello there")


def test_tg_html_link_is_outside_the_blockquote():
    # The link must remain visible while the quote is collapsed → it appears
    # AFTER the closing </blockquote>, never inside the quote.
    card = notify.render_card([_item()], [], now=NOW, mode="tg_html")
    close = card.rindex("</blockquote>")  # the LAST quote (original-message cut)
    link = card.index('<a href="https://chat.google.com/')
    assert link > close  # link comes after the final quote closes
    # And the link is not nested inside an open quote.
    assert "<blockquote" not in card[close + len("</blockquote>"):]


def test_tg_html_message_cap_higher_than_plain():
    # 'Z' appears nowhere else (sender / location / room URL / summary), so counting
    # it isolates the original-message body length under the cut. A short summary keeps
    # the message_block alive; priority 'normal' selects the default profile (cap 280).
    long = _item(priority="normal", context_summary="short", text="Z" * 400)
    tg = notify.render_card([long], [], now=NOW, mode="tg_html")
    plain = notify.render_card([long], [], now=NOW, mode="plain")
    # Telegram expands MORE of the original message than the plain ~140 snippet.
    assert tg.count("Z") > plain.count("Z")
    assert tg.count("Z") <= 280  # but still bounded by the default-profile tg cap


def test_tg_html_no_message_block_when_no_distinct_summary():
    # No context_summary → the text IS the (full, expandable) summary line, so there
    # is exactly ONE quote and NO separate original-message cut (no duplication).
    blank = _item(context_summary="", text="just a short dm")
    card = notify.render_card([blank], [], now=NOW, mode="tg_html")
    assert card.count("<blockquote expandable>") == 1
    assert "💬 <blockquote expandable>just a short dm</blockquote>" in card
    assert "Original message" not in card  # no separate cut


def test_deautolink_breaks_domains_tg_html_only():
    """Intra-word dots in fetched text get a zero-width WORD JOINER in tg_html so
    Telegram won't auto-linkify "foo.py"/"bar.io"; plain/gchat stay untouched."""
    WJ = "⁠"
    item = _item(context_summary="see foo.py now", text="full body bar.io here")
    # plain / gchat: no joiner spliced.
    for mode in ("plain", "gchat"):
        card = notify.render_card([item], [], now=NOW, mode=mode)
        assert WJ not in card
        assert "foo.py" in card
    # tg_html: the dot inside the domain token carries the joiner, so the raw
    # "foo.py" substring no longer appears but the visible chars are unchanged.
    tg = notify.render_card([item], [], now=NOW, mode="tg_html")
    assert f"foo.{WJ}py" in tg
    assert "foo.py" not in tg              # raw domain substring is now broken
    assert f"bar.{WJ}io" in tg
    # Sentence-ending dots (space after) are NOT touched.
    plain_text_item = _item(context_summary="done. ok", text="x")
    tg2 = notify.render_card([plain_text_item], [], now=NOW, mode="tg_html")
    assert WJ not in tg2


def test_tg_html_empty_everything_omits_blockquote():
    blank = _item(context_summary="", text="")
    card = notify.render_card([blank], [], now=NOW, mode="tg_html")
    assert "<blockquote" not in card  # nothing to collapse → no empty quote


def test_tg_html_blockquote_injection_cannot_break_out():
    # SECURITY: an original message trying to close our quote and inject markup is
    # escaped. The message goes under the cut, so the payload lives in ``text`` while a
    # distinct summary keeps the blockquote alive.
    evil = _item(
        context_summary="legit summary",
        text="</blockquote><script>alert(1)</script><blockquote>x",
    )
    card = notify.render_card([evil], [], now=NOW, mode="tg_html")
    # Two of OUR OWN quotes (full summary + original-message cut); the injected
    # tags inside the message body are escaped, not counted as ours.
    assert card.count("<blockquote expandable>") == 2
    assert card.count("</blockquote>") == 2
    assert "&lt;/blockquote&gt;" in card
    assert "&lt;blockquote&gt;" in card
    assert "<script>" not in card
    assert "&lt;script&gt;" in card


def test_gchat_and_plain_summary_not_in_blockquote():
    # The blockquote is a Telegram-only treatment.
    g = notify.render_card([_item()], [], now=NOW, mode="gchat")
    p = notify.render_card([_item()], [], now=NOW, mode="plain")
    assert "blockquote" not in g
    assert "blockquote" not in p


def test_dm_location_label():
    dm = _item(trigger="direct_dm", space_type="DIRECT_MESSAGE", space_display=None)
    card = notify.render_card([dm], [], now=NOW, mode="plain")
    # A DM is a person source: 👤 person icon + the localized "Direct message".
    assert "👤 Direct message" in card


def test_summary_falls_back_to_text_full_no_truncation():
    long = _item(context_summary="", text="A" * 300)
    card = notify.render_card([long], [], now=NOW, mode="plain")
    # No distinct summary → the text fills the 💬 line. The default profile shows the
    # summary in FULL (no truncation); plain has no native collapse, so all 300 chars
    # appear with NO ellipsis.
    line = [ln for ln in card.splitlines() if "A" in ln][0]
    assert line.startswith("💬 ")
    body = line[len("💬 "):]
    assert not body.endswith("…")
    assert body == "A" * 300


def test_link_omitted_when_no_permalink():
    no_link = _item(space_name=None, message_name=None)
    card = notify.render_card([no_link], [], now=NOW, mode="plain")
    assert "🔗" not in card


# --------------------------------------------------------------------------- #
# render_card — OVERDUE block.
# --------------------------------------------------------------------------- #
def test_overdue_block_layout():
    owe = _item(
        "o0", sender_name="Andrii Skakunenko",
        my_promise="review the PR", promise_due="2026-06-04T18:00:00Z",
    )
    card = notify.render_card([], [owe], now=NOW, mode="plain")
    assert "⏰ Andrii Skakunenko" in card
    assert '   promise "review the PR"' in card
    assert "   due 04 Jun 18:00" in card


def test_overdue_unspecified_promise():
    owe = _item("o0", my_promise=None, promise_due="2026-06-04T18:00:00Z")
    card = notify.render_card([], [owe], now=NOW, mode="plain")
    assert 'promise "(unspecified)"' in card


# --------------------------------------------------------------------------- #
# render_card — cap behaviour (NEW capped, OVERDUE never).
# --------------------------------------------------------------------------- #
def test_cap_adds_overflow_block():
    new = [_item(f"n{i}") for i in range(25)]
    card = notify.render_card(new, [], now=NOW, mode="plain", cap=10)
    icon_lines = [ln for ln in card.splitlines() if ln.startswith("🔥")]
    assert len(icon_lines) == 10
    assert "…and 15 more new" in card


def test_cap_zero_means_no_cap():
    new = [_item(f"n{i}") for i in range(15)]
    card = notify.render_card(new, [], now=NOW, mode="plain", cap=0)
    icon_lines = [ln for ln in card.splitlines() if ln.startswith("🔥")]
    assert len(icon_lines) == 15
    assert "more new" not in card


# --------------------------------------------------------------------------- #
# SECURITY — tg_html escapes untrusted Chat text; plain/gchat pass through.
# --------------------------------------------------------------------------- #
def test_tg_html_escapes_injected_markup():
    evil = _item(
        sender_name="<b>boss</b>",
        context_summary='<a href="http://evil">pwn</a><script>x</script>',
    )
    card = notify.render_card([evil], [], now=NOW, mode="tg_html")
    # Injected tags are escaped, not live.
    assert "<script>" not in card
    assert "&lt;script&gt;" in card
    assert "&lt;b&gt;boss&lt;/b&gt;" in card  # the sender's literal <b> escaped
    assert "&lt;a href=&quot;http://evil&quot;&gt;" in card
    # Our OWN wrapper bold + permalink anchor remain live markup.
    assert "<b>&lt;b&gt;boss&lt;/b&gt;</b>" in card  # bold wraps escaped name
    assert '<a href="https://chat.google.com/' in card


def test_plain_mode_does_not_escape():
    evil = _item(sender_name="<b>boss</b>", context_summary="a & b < c")
    card = notify.render_card([evil], [], now=NOW, mode="plain")
    assert "<b>boss</b>" in card  # raw, no entities in plain text
    assert "a & b < c" in card


def test_gchat_defangs_smuggled_link_mention_and_bold():
    # Google Chat renders its own markup. A message body that smuggles a spoofed
    # link, a mention/ping, and bold must render INERT — yet our own permalink
    # and the bold-sender wrapper must STILL render live.
    evil = _item(
        sender_name="Mariia Ivanova",
        context_summary="Click <https://phish.example|🔗 Open in Chat> <users/all> *x*",
    )
    card = notify.render_card([evil], [], now=NOW, mode="gchat")

    # Smuggled structures are neutralized — none of these live substrings survive.
    assert "<https://phish.example|" not in card
    assert "<users/all>" not in card
    # The smuggled bold markers are defanged (no live ``*x*`` in the summary line).
    summary_line = [ln for ln in card.splitlines() if "Click" in ln][0]
    assert "*x*" not in summary_line

    # Our OWN markup is untouched and still live (room-level link, ONE line).
    assert "*Mariia Ivanova*" in card  # bold wrapper added by _bold, after _esc
    assert "<https://chat.google.com/room/AAQAugHrEgY|🔗 Open in Chat>" in card
    # Exactly ONE LIVE link structure: the smuggled one was defanged to ‹…∣…›,
    # so only our own `<https://…|…>` survives as a real Chat link.
    assert card.count("<https://") == 1


def test_gchat_defang_preserves_sender_bold_when_name_has_asterisk():
    # A name containing '*' has ITS asterisks defanged, but the wrapping bold '*'
    # added by _bold stays intact, so the sender still renders bold.
    evil = _item(sender_name="a*b")
    card = notify.render_card([evil], [], now=NOW, mode="gchat")
    assert "*a∗b*" in card  # inner * defanged → ∗ ; outer * wrapper intact


def test_tg_html_escaping_unchanged_after_gchat_hardening():
    # Re-assert tg_html still html-escapes (the gchat branch must not affect it).
    evil = _item(
        sender_name="<b>boss</b>",
        context_summary='<a href="http://evil">pwn</a><script>x</script>',
    )
    card = notify.render_card([evil], [], now=NOW, mode="tg_html")
    assert "<script>" not in card
    assert "&lt;script&gt;" in card
    assert "&lt;b&gt;boss&lt;/b&gt;" in card
    assert "&lt;a href=&quot;http://evil&quot;&gt;" in card
    assert "<b>&lt;b&gt;boss&lt;/b&gt;</b>" in card
    assert '<a href="https://chat.google.com/' in card


# --------------------------------------------------------------------------- #
# new_candidates — priority-based ordering.
# --------------------------------------------------------------------------- #

def _store_with_items(*items) -> dict:
    """Build a minimal store_data dict from a list of item dicts."""
    return {"items": {it["id"]: it for it in items}}


def _new_item(iid: str, priority: str, cfg: dict | None = None) -> dict:
    """Minimal store item that passes _is_new_candidate."""
    return {
        "id": iid,
        "status": "new",
        "last_notified": None,
        "trigger": "user_mention",
        "priority": priority,
        "created_time": f"2026-06-04T10:00:0{iid[-1]}Z",
    }


def _minimal_cfg() -> dict:
    """Minimal config that lets _is_new_candidate accept every 'new' item."""
    return {
        "baseline": {
            "triggers": [],  # empty → accept all triggers via _matches_baseline
        },
    }


def test_new_candidates_high_before_normal_before_low():
    """high items appear first, then normal, then low — regardless of insertion order."""
    # Insert in worst-case order: low, normal, high.
    cfg = _minimal_cfg()
    store = _store_with_items(
        _new_item("a1", "low"),
        _new_item("a2", "normal"),
        _new_item("a3", "high"),
    )
    result = notify.new_candidates(store, cfg)
    priorities = [it["priority"] for it in result]
    # high must come before normal, normal before low.
    assert priorities.index("high") < priorities.index("normal")
    assert priorities.index("normal") < priorities.index("low")


def test_new_candidates_unset_priority_after_low():
    """Items with no/unset priority sort last (same bucket as low)."""
    cfg = _minimal_cfg()
    store = _store_with_items(
        _new_item("b1", "unset"),
        _new_item("b2", "high"),
        _new_item("b3", ""),
    )
    result = notify.new_candidates(store, cfg)
    ids = [it["id"] for it in result]
    # high first, then unset/empty after it.
    assert ids[0] == "b2"
    assert set(ids[1:]) == {"b1", "b3"}


def test_new_candidates_stable_tiebreak_within_priority():
    """Items with equal priority keep their original insertion order."""
    cfg = _minimal_cfg()
    # Insert three 'normal' items; their iteration order is a1, a2, a3.
    store = _store_with_items(
        _new_item("a1", "normal"),
        _new_item("a2", "normal"),
        _new_item("a3", "normal"),
    )
    result = notify.new_candidates(store, cfg)
    assert [it["id"] for it in result] == ["a1", "a2", "a3"]


def test_new_candidates_mixed_stable_tiebreak():
    """Stable sort: within each priority bucket, insertion order is preserved."""
    cfg = _minimal_cfg()
    store = _store_with_items(
        _new_item("h1", "high"),
        _new_item("n1", "normal"),
        _new_item("h2", "high"),
        _new_item("n2", "normal"),
        _new_item("l1", "low"),
    )
    result = notify.new_candidates(store, cfg)
    ids = [it["id"] for it in result]
    # high items in insertion order, then normal in insertion order, then low.
    assert ids == ["h1", "h2", "n1", "n2", "l1"]


# --------------------------------------------------------------------------- #
# SECURITY — context_summary stays escaped in all modes.
# --------------------------------------------------------------------------- #
def test_context_summary_escaped_tg_html():
    """context_summary with HTML injection is always escaped in tg_html mode."""
    evil = _item(context_summary="<script>alert(1)</script>")
    card = notify.render_card([evil], [], now=NOW, mode="tg_html")
    assert "<script>" not in card
    assert "&lt;script&gt;" in card


def test_context_summary_escaped_gchat():
    """context_summary with Chat-markup injection is defanged in gchat mode."""
    evil = _item(context_summary="<https://evil.example|pwn> <users/all>")
    card = notify.render_card([evil], [], now=NOW, mode="gchat")
    assert "<https://evil.example|" not in card
    assert "<users/all>" not in card


def test_context_summary_passthrough_plain():
    """In plain mode, context_summary is not HTML-escaped (no markup expected)."""
    it = _item(context_summary="a & b < c > d")
    card = notify.render_card([it], [], now=NOW, mode="plain")
    assert "a & b < c > d" in card


# --------------------------------------------------------------------------- #
# Sender role resolution (_bare_id / _role_map / _attach_roles).
# --------------------------------------------------------------------------- #
def test_bare_id_strips_users_prefix():
    assert notify._bare_id("users/123") == "123"
    assert notify._bare_id("123") == "123"
    assert notify._bare_id(None) == ""


def test_role_map_config_wins_over_directory(monkeypatch, tmp_path):
    # Directory cache provides a sparse fallback; config user_profiles override it.
    cache = tmp_path / "name_cache.json"
    cache.write_text(
        '{"profiles": {"111": {"role": "Stale Title"}, "222": {"role": "Designer"}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(notify.google_chat, "_DIRECTORY_CACHE_PATH", cache)
    cfg = {"user_profiles": {"users/111": {"role": "Engineering Manager"}}}
    rmap = notify._role_map(cfg)
    assert rmap["111"] == "Engineering Manager"  # config overrides directory
    assert rmap["222"] == "Designer"             # directory-only fallback kept


def test_role_map_missing_cache_is_config_only(monkeypatch, tmp_path):
    missing = tmp_path / "nope.json"
    monkeypatch.setattr(notify.google_chat, "_DIRECTORY_CACHE_PATH", missing)
    cfg = {"user_profiles": {"users/abc": {"role": "QA Engineer"}}}
    assert notify._role_map(cfg) == {"abc": "QA Engineer"}


def test_attach_roles_stamps_copies_without_mutating_store():
    item = {"id": "x", "sender_id": "users/777"}
    rmap = {"777": "Backend Developer"}
    out = notify._attach_roles([item], rmap)
    assert out[0]["sender_role"] == "Backend Developer"
    assert "sender_role" not in item  # original store dict untouched


def test_attach_roles_no_map_returns_input():
    items = [{"id": "x", "sender_id": "users/1"}]
    assert notify._attach_roles(items, {}) is items


def test_attach_roles_preserves_existing_role():
    item = {"id": "x", "sender_id": "users/1", "sender_role": "Pre-set"}
    out = notify._attach_roles([item], {"1": "From Map"})
    assert out[0]["sender_role"] == "Pre-set"


# --------------------------------------------------------------------------- #
# _pack_items / TelegramSender chunking — a digest over the 4096 cap must be
# split into multiple whole-card messages, each valid HTML under the limit
# (regression: full summaries pushed digests past the cap and text[:limit]
# sliced through <blockquote>, yielding Telegram 400 "can't parse entities").
# --------------------------------------------------------------------------- #
def test_pack_items_splits_when_over_limit():
    # render_fn returns a string 30 chars per item; limit 100 → 3 per batch.
    items = list(range(10))
    batches = notify._pack_items(items, lambda b: "x" * (30 * len(b)), 100)
    assert [len(b) for b in batches] == [3, 3, 3, 1]
    assert [x for b in batches for x in b] == items  # order + completeness preserved


def test_pack_items_oversized_single_item_is_its_own_batch():
    batches = notify._pack_items([1, 2], lambda b: "x" * (200 * len(b)), 100)
    assert batches == [[1], [2]]  # each alone exceeds limit → emitted solo


def test_pack_items_empty_returns_empty():
    assert notify._pack_items([], lambda b: "", 100) == []


def _long_item(i):
    return _item(
        iid=f"big{i}",
        sender_name=f"Sender {i}",
        context_summary=("Lorem ipsum dolor sit amet " * 30).strip(),
        text=("original message body " * 20).strip(),
    )


def test_telegram_send_splits_long_digest_into_valid_messages(monkeypatch):
    sender = notify.TelegramSender(
        cfg={"channels": {"telegram": {"enabled": True}}},
        secrets={"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "C"},
    )
    sent: list = []

    def fake_post(url, payload):
        sent.append(payload["text"])
        return {"ok": True}

    monkeypatch.setattr(sender, "_http_post", fake_post)
    items = [_long_item(i) for i in range(12)]
    # Sanity: a single render would blow past the cap (the bug precondition).
    assert len(sender._render(items, [], NOW)) > sender._TEXT_LIMIT

    assert sender.send(items, [], NOW) is True
    assert len(sent) >= 2  # actually chunked
    for text in sent:
        assert len(text) <= sender._TEXT_LIMIT
        # Never sliced mid-tag: blockquote/b/a open and close counts match.
        assert text.count("<blockquote") == text.count("</blockquote>")
        assert text.count("<b>") == text.count("</b>")
        assert text.count("<a ") == text.count("</a>")
    # Every card landed in exactly one message (no loss, no dup).
    assert sum(t.count("🕒") for t in sent) == 12


def test_telegram_send_short_digest_is_single_message(monkeypatch):
    sender = notify.TelegramSender(
        cfg={"channels": {"telegram": {"enabled": True}}},
        secrets={"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "C"},
    )
    sent: list = []
    monkeypatch.setattr(sender, "_http_post", lambda u, p: sent.append(p["text"]) or {"ok": True})
    assert sender.send([_item()], [], NOW) is True
    assert len(sent) == 1


def test_telegram_send_reports_failure_when_a_chunk_fails(monkeypatch):
    sender = notify.TelegramSender(
        cfg={"channels": {"telegram": {"enabled": True}}},
        secrets={"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "C"},
    )
    calls = {"n": 0}

    def flaky_post(url, payload):
        calls["n"] += 1
        return {"ok": True} if calls["n"] == 1 else {"ok": False, "description": "boom"}

    monkeypatch.setattr(sender, "_http_post", flaky_post)
    items = [_long_item(i) for i in range(12)]
    assert sender.send(items, [], NOW) is False  # one chunk failed → overall False
