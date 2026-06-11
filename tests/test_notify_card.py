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
# Priority icon map.
# --------------------------------------------------------------------------- #
def test_priority_icon_map():
    assert notify._priority_icon(_item(priority="urgent")) == "🔴"
    assert notify._priority_icon(_item(priority="high")) == "🔴"
    assert notify._priority_icon(_item(priority="medium")) == "🟡"
    assert notify._priority_icon(_item(priority="normal")) == "🟢"
    assert notify._priority_icon(_item(priority="unset")) == "⚪"
    assert notify._priority_icon(_item(priority=None)) == "⚪"


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
    assert "🔴 Mariia Ivanova" in card  # high → 🔴, no bold markup
    assert "📍 Mobile internal · 🕒 04.06 10:00" in card  # absolute send time, never stale
    assert "💬 API audit: enumerate group-chat system messages" in card  # summary line
    # The original message appears under a labelled cut (distinct from the summary).
    assert "   Original message" in card
    assert "   hello there" in card
    assert "🔗 Open in Chat: https://chat.google.com/room/AAQAugHrEgY" in card


def test_gchat_new_block_uses_chat_markup():
    card = notify.render_card([_item()], [], now=NOW, mode="gchat")
    assert "🔴 *Mariia Ivanova*" in card  # *bold*
    assert "<https://chat.google.com/room/AAQAugHrEgY|🔗 Open in Chat>" in card
    # Exactly ONE link line, pointing at the room URL.
    assert card.count("🔗 Open in Chat") == 1


def test_tg_html_new_block_uses_html_markup():
    card = notify.render_card([_item()], [], now=NOW, mode="tg_html")
    assert "🔴 <b>Mariia Ivanova</b>" in card
    assert (
        '<a href="https://chat.google.com/room/AAQAugHrEgY">🔗 Open in Chat</a>'
        in card
    )
    # Exactly ONE link line, pointing at the room URL.
    assert card.count("🔗 Open in Chat") == 1


# --------------------------------------------------------------------------- #
# Telegram expandable blockquote — collapse the ORIGINAL MESSAGE, link stays visible.
# The visible 💬 line is the (short) summary; the full message goes under the cut.
# --------------------------------------------------------------------------- #
def test_tg_html_original_message_wrapped_in_expandable_blockquote():
    card = notify.render_card([_item()], [], now=NOW, mode="tg_html")
    assert "<blockquote expandable>" in card
    assert "</blockquote>" in card
    # The ORIGINAL MESSAGE lives inside the quote; the summary stays on the 💬 line.
    qstart = card.index("<blockquote expandable>")
    qend = card.index("</blockquote>")
    assert "hello there" in card[qstart:qend]            # message under the cut
    assert "API audit" not in card[qstart:qend]          # summary NOT in the quote
    assert "💬 API audit: enumerate group-chat system messages" in card
    # Body stays plain inside the quote (no <code>); auto-link guard only splices a
    # zero-width joiner into intra-word dots, of which this text has none.
    assert "<blockquote expandable>hello there</blockquote>" in card


def test_tg_html_link_is_outside_the_blockquote():
    # The link must remain visible while the quote is collapsed → it appears
    # AFTER the closing </blockquote>, never inside the quote.
    card = notify.render_card([_item()], [], now=NOW, mode="tg_html")
    close = card.index("</blockquote>")
    link = card.index('<a href="https://chat.google.com/')
    assert link > close  # link comes after the quote closes
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
    # No context_summary → the text IS the summary line; no separate expandable cut
    # (and so no empty/duplicate blockquote).
    blank = _item(context_summary="", text="just a short dm")
    card = notify.render_card([blank], [], now=NOW, mode="tg_html")
    assert "<blockquote" not in card
    assert "💬 just a short dm" in card


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
    # Only OUR own opening/closing tags exist — the injected ones are escaped.
    assert card.count("<blockquote expandable>") == 1
    assert card.count("</blockquote>") == 1
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
    assert "Direct message ·" in card


def test_summary_falls_back_to_text_and_truncates():
    long = _item(context_summary="", text="A" * 300)
    card = notify.render_card([long], [], now=NOW, mode="plain")
    # No distinct summary → the text fills the 💬 line, truncated to ~140 + ellipsis.
    line = [ln for ln in card.splitlines() if "A" in ln][0]
    assert line.startswith("💬 ")
    body = line[len("💬 "):]
    assert body.endswith("…")
    assert len(body) <= 141


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
    icon_lines = [ln for ln in card.splitlines() if ln.startswith("🔴")]
    assert len(icon_lines) == 10
    assert "…and 15 more new" in card


def test_cap_zero_means_no_cap():
    new = [_item(f"n{i}") for i in range(15)]
    card = notify.render_card(new, [], now=NOW, mode="plain", cap=0)
    icon_lines = [ln for ln in card.splitlines() if ln.startswith("🔴")]
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
