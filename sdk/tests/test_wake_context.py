"""SDK Phase 7.3 — Friend.memory + WakeContext + context_for_wake().

Coverage:
  * Friend.memory round-trip (default empty, populated dict, null/garbage)
  * client.friends.update(memory=...) plumbs body field correctly
  * WakeContext dataclass properties (is_friend, partner_display_name,
    my_display_name) on various partner shapes
  * _format_system_prompt section ordering + content
  * client.dm.context_for_wake() composes three calls
    (conversation + friends.get + client.card) and stitches results
  * Friend-not-found path → empty memory + None note (no raise)
  * No agent card → my_display_name falls back to bot_id
  * max_turns flows through to conversation() limit
"""

from __future__ import annotations

import json

import pytest
import responses

from a2a_dm import AgentClient, AgentCard, Friend, WakeContext
from a2a_dm.wake_context import _format_system_prompt


# ── Friend.memory roundtrip ─────────────────────────────────────


def _friend_payload(**overrides) -> dict:
    base = {
        "owner_bot_id": "bot_ext_laobaigan",
        "friend_bot_id": "bestiedog",
        "label": "bestie",
        "note": "dev partner",
        "groups": ["dev-team"],
        "tags": ["python"],
        "agent_card_snapshot": {"name": "bestiedog"},
        "added_at": "2026-05-30T12:00:00+00:00",
        "client_origin_at": "2026-05-25T08:30:00+00:00",
        "last_contact_at": None,
        "discoverable": False,
        "notify": False,
        "memory": {},
    }
    base.update(overrides)
    return base


def test_friend_memory_defaults_to_empty_dict():
    f = Friend.from_dict(_friend_payload(memory=None))
    assert f.memory == {}


def test_friend_memory_round_trips_populated_dict():
    payload = _friend_payload(
        memory={"last_topic": "deployment", "fav_color": "blue"}
    )
    f = Friend.from_dict(payload)
    assert f.memory == {"last_topic": "deployment", "fav_color": "blue"}


def test_friend_memory_missing_key_defaults_to_empty():
    """Older server might not even include the key (pre-Phase 7.3 row)."""
    payload = _friend_payload()
    del payload["memory"]
    f = Friend.from_dict(payload)
    assert f.memory == {}


def test_friend_memory_garbage_value_coerced_to_empty():
    """Defensive: server bug returns "memory": "oops" → don't crash callers."""
    f = Friend.from_dict(_friend_payload(memory="not a dict"))
    assert f.memory == {}


def test_friend_memory_default_when_payload_is_garbage():
    f = Friend.from_dict("garbage")
    assert f.memory == {}


# ── client.friends.update(memory=...) plumbing ──────────────────


@responses.activate
def test_friends_update_memory_field_only():
    """memory=... shows up alone in body when nothing else passed."""
    responses.add(
        responses.PATCH,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={"k": "v"})},
        status=200,
    )
    client = AgentClient(token="bt_test")
    client.friends.update("bestiedog", memory={"k": "v"})
    body = json.loads(responses.calls[0].request.body)
    assert body == {"memory": {"k": "v"}}


@responses.activate
def test_friends_update_memory_with_label():
    """Mixing memory= with other fields — both keys in body."""
    responses.add(
        responses.PATCH,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(label="bd", memory={"x": 1})},
        status=200,
    )
    client = AgentClient(token="bt_test")
    client.friends.update("bestiedog", label="bd", memory={"x": 1})
    body = json.loads(responses.calls[0].request.body)
    assert body == {"label": "bd", "memory": {"x": 1}}


@responses.activate
def test_friends_update_empty_memory_dict_sent():
    """Empty dict is meaningful (clear-all) and must reach the wire."""
    responses.add(
        responses.PATCH,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={})},
        status=200,
    )
    client = AgentClient(token="bt_test")
    client.friends.update("bestiedog", memory={})
    body = json.loads(responses.calls[0].request.body)
    assert body == {"memory": {}}


@responses.activate
def test_friends_update_returns_friend_with_memory():
    """Roundtrip: send memory, server echoes it back, dataclass exposes it."""
    responses.add(
        responses.PATCH,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={"saved": True})},
        status=200,
    )
    client = AgentClient(token="bt_test")
    f = client.friends.update("bestiedog", memory={"saved": True})
    assert f.memory == {"saved": True}


# ── WakeContext dataclass + helpers ─────────────────────────────


def _wake_context(**overrides) -> WakeContext:
    base = dict(
        my_bot_id="bot_ext_laobaigan",
        me={"name": "Laobaigan", "description": "an old goat"},
        conversation_partner_bot_id="bestiedog",
        partner={
            "bot_id": "bestiedog",
            "display_name": "Bestie Dog",
            "is_friend": True,
        },
        partner_memory={},
        partner_friend_note=None,
        recent_turns=[],
        system_prompt_suggestion="",
    )
    base.update(overrides)
    return WakeContext(**base)


def test_wake_context_is_friend_true():
    ctx = _wake_context()
    assert ctx.is_friend is True


def test_wake_context_is_friend_false():
    ctx = _wake_context(partner={"bot_id": "x", "is_friend": False})
    assert ctx.is_friend is False


def test_wake_context_is_friend_missing_key_returns_false():
    ctx = _wake_context(partner={"bot_id": "x"})  # no is_friend key
    assert ctx.is_friend is False


def test_wake_context_partner_display_name_uses_partner_field():
    ctx = _wake_context()
    assert ctx.partner_display_name == "Bestie Dog"


def test_wake_context_partner_display_name_falls_back_to_bot_id():
    ctx = _wake_context(partner={"bot_id": "bestiedog"})  # no display_name
    assert ctx.partner_display_name == "bestiedog"


def test_wake_context_my_display_name_from_me_name():
    ctx = _wake_context()
    assert ctx.my_display_name == "Laobaigan"


def test_wake_context_my_display_name_falls_back_to_bot_id():
    ctx = _wake_context(me=None)
    assert ctx.my_display_name == "bot_ext_laobaigan"


def test_wake_context_my_display_name_final_fallback():
    ctx = _wake_context(me=None, my_bot_id=None)
    assert ctx.my_display_name == "(unnamed agent)"


# ── _format_system_prompt section ordering ──────────────────────


def test_format_system_prompt_includes_my_identity_heading():
    out = _format_system_prompt(
        my_bot_id="bot_ext_laobaigan",
        me={"name": "Laobaigan", "description": "the elder"},
        partner={"bot_id": "x"},
        partner_memory={},
        partner_friend_note=None,
        recent_turns=[],
    )
    assert "# You are Laobaigan" in out
    assert "the elder" in out
    assert "`bot_ext_laobaigan`" in out


def test_format_system_prompt_includes_partner_section():
    out = _format_system_prompt(
        my_bot_id="me",
        me=None,
        partner={
            "bot_id": "bestiedog",
            "display_name": "Bestie",
            "agent_card_snapshot": {"description": "a friendly dog"},
            "is_friend": True,
        },
        partner_memory={},
        partner_friend_note=None,
        recent_turns=[],
    )
    assert "# You are talking to Bestie" in out
    assert "a friendly dog" in out
    assert "`bestiedog`" in out
    assert "You have friended this partner" in out


def test_format_system_prompt_memory_block_rendered_as_json_fence():
    out = _format_system_prompt(
        my_bot_id="me",
        me=None,
        partner={"display_name": "Bestie"},
        partner_memory={"last_topic": "deployment"},
        partner_friend_note=None,
        recent_turns=[],
    )
    assert "## What you remember about Bestie" in out
    assert "```json" in out
    assert '"last_topic": "deployment"' in out


def test_format_system_prompt_friend_note_block():
    out = _format_system_prompt(
        my_bot_id="me",
        me=None,
        partner={"display_name": "Bestie"},
        partner_memory={},
        partner_friend_note="met at PyCon 2025",
        recent_turns=[],
    )
    assert "## Note about them" in out
    assert "met at PyCon 2025" in out


def test_format_system_prompt_recent_turns_with_outgoing_label_is_you():
    out = _format_system_prompt(
        my_bot_id="me",
        me={"name": "Me"},
        partner={"display_name": "Bestie"},
        partner_memory={},
        partner_friend_note=None,
        recent_turns=[
            {
                "direction": "outgoing",
                "text": "hello",
                "reply_text": "hi back",
                "created_at": "2026-05-30T12:00:00+00:00",
                "task_id": "msg_1",
            }
        ],
    )
    assert "**You:** hello" in out
    assert "**Bestie:** hi back" in out


def test_format_system_prompt_incoming_turn_uses_partner_name():
    out = _format_system_prompt(
        my_bot_id="me",
        me={"name": "Me"},
        partner={"display_name": "Bestie"},
        partner_memory={},
        partner_friend_note=None,
        recent_turns=[
            {
                "direction": "incoming",
                "text": "how are you?",
                "reply_text": None,
                "created_at": "2026-05-30T12:00:00+00:00",
                "task_id": "msg_2",
            }
        ],
    )
    assert "**Bestie:** how are you?" in out


def test_format_system_prompt_omits_empty_sections():
    """No memory, no note, no turns → those sections don't render."""
    out = _format_system_prompt(
        my_bot_id="me",
        me={"name": "Me"},
        partner={"display_name": "Bestie"},
        partner_memory={},
        partner_friend_note=None,
        recent_turns=[],
    )
    assert "## What you remember about" not in out
    assert "## Note about them" not in out
    assert "## Recent conversation" not in out


def test_format_system_prompt_section_ordering():
    """Identity → partner → note → memory → turns. Stable order matters
    because LLMs weight earlier instructions more."""
    out = _format_system_prompt(
        my_bot_id="me",
        me={"name": "Me", "description": "agent"},
        partner={"display_name": "Bestie"},
        partner_memory={"k": "v"},
        partner_friend_note="trusted",
        recent_turns=[
            {"direction": "incoming", "text": "hi", "reply_text": None,
             "created_at": "", "task_id": "1"},
        ],
    )
    idx_me = out.index("# You are Me")
    idx_partner = out.index("# You are talking to Bestie")
    idx_note = out.index("## Note about them")
    idx_memory = out.index("## What you remember about")
    idx_turns = out.index("## Recent conversation")
    assert idx_me < idx_partner < idx_note < idx_memory < idx_turns


# ── client.dm.context_for_wake() end-to-end ─────────────────────


def _conv_payload(**overrides) -> dict:
    base = {
        "partner": {
            "bot_id": "bestiedog",
            "display_name": "Bestie Dog",
            "is_friend": True,
            "agent_card_snapshot": {"name": "Bestie Dog"},
        },
        "messages": [
            {
                "id": "msg_1",
                "task_id": "msg_1",
                "direction": "outgoing",
                "task_state": "completed",
                "delivery_chip": "replied",
                "text": "hello",
                "reply_text": "yo!",
                "sender_bot_id": "bot_ext_laobaigan",
                "recipient_bot_id": "bestiedog",
                "sender_display_name": "Laobaigan",
                "tags": [],
                "vertical": None,
                "created_at": "2026-05-30T12:00:00+00:00",
                "ack_at": None,
                "submit_at": "2026-05-30T12:01:00+00:00",
                "replied_at": "2026-05-30T12:02:00+00:00",
            }
        ],
        "has_more": False,
        "next_before_id": None,
        "count": 1,
    }
    base.update(overrides)
    return base


@responses.activate
def test_context_for_wake_composes_three_sources():
    """Happy path — conversation + friend + client.card all available."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/conversations/bestiedog",
        json=_conv_payload(),
        status=200,
        match_querystring=False,
    )
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(
            memory={"last_topic": "deployment"},
            note="bestie since 2025",
        )},
        status=200,
    )
    client = AgentClient(token="bt_test", bot_id="bot_ext_laobaigan")
    # Pre-populate client.card so we don't have to mock /.well-known
    client.card = AgentCard(
        bot_id="bot_ext_laobaigan",
        name="Laobaigan",
        description="the elder",
    )

    ctx = client.dm.context_for_wake("bestiedog", max_turns=10)

    assert isinstance(ctx, WakeContext)
    assert ctx.my_bot_id == "bot_ext_laobaigan"
    assert ctx.conversation_partner_bot_id == "bestiedog"
    assert ctx.partner_memory == {"last_topic": "deployment"}
    assert ctx.partner_friend_note == "bestie since 2025"
    assert len(ctx.recent_turns) == 1
    assert ctx.recent_turns[0]["text"] == "hello"
    assert ctx.recent_turns[0]["reply_text"] == "yo!"
    assert ctx.recent_turns[0]["direction"] == "outgoing"
    assert ctx.me is not None
    assert ctx.me["name"] == "Laobaigan"
    assert "# You are Laobaigan" in ctx.system_prompt_suggestion
    assert "# You are talking to Bestie Dog" in ctx.system_prompt_suggestion
    assert "last_topic" in ctx.system_prompt_suggestion
    assert "bestie since 2025" in ctx.system_prompt_suggestion


@responses.activate
def test_context_for_wake_handles_not_friended_partner():
    """404 from /friends/{partner} → empty memory + None note, no raise."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/conversations/stranger",
        json=_conv_payload(
            partner={
                "bot_id": "stranger",
                "display_name": "Stranger",
                "is_friend": False,
            }
        ),
        status=200,
        match_querystring=False,
    )
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/stranger",
        json={"detail": {"error": "friend_not_found"}},
        status=404,
    )
    client = AgentClient(token="bt_test", bot_id="me")
    ctx = client.dm.context_for_wake("stranger")
    assert ctx.partner_memory == {}
    assert ctx.partner_friend_note is None
    assert ctx.is_friend is False


@responses.activate
def test_context_for_wake_no_card_loaded_me_is_none():
    """Operator hasn't done client.agent_card.discover() — me is None."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/conversations/bestiedog",
        json=_conv_payload(),
        status=200,
        match_querystring=False,
    )
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={})},
        status=200,
    )
    client = AgentClient(token="bt_test", bot_id="bot_ext_laobaigan")
    # Don't set client.card
    ctx = client.dm.context_for_wake("bestiedog")
    assert ctx.me is None
    # System prompt still works — uses bot_id as fallback identity
    assert "bot_ext_laobaigan" in ctx.system_prompt_suggestion


@responses.activate
def test_context_for_wake_max_turns_flows_to_conversation():
    """max_turns kwarg becomes the conversation() limit query param."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/conversations/bestiedog",
        json=_conv_payload(),
        status=200,
        match_querystring=False,
    )
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={})},
        status=200,
    )
    client = AgentClient(token="bt_test", bot_id="me")
    client.dm.context_for_wake("bestiedog", max_turns=25)
    assert "limit=25" in responses.calls[0].request.url


@responses.activate
def test_context_for_wake_recent_turns_preserve_order_and_fields():
    """Flatten ConversationMessage → dict; verify shape."""
    multi_msg_payload = _conv_payload(
        messages=[
            {
                "id": "msg_a", "task_id": "msg_a",
                "direction": "incoming", "task_state": "completed",
                "delivery_chip": None,
                "text": "first", "reply_text": None,
                "sender_bot_id": "bestiedog", "recipient_bot_id": "me",
                "sender_display_name": "Bestie",
                "tags": [], "vertical": None,
                "created_at": "2026-05-30T11:00:00+00:00",
                "ack_at": None, "submit_at": None, "replied_at": None,
            },
            {
                "id": "msg_b", "task_id": "msg_b",
                "direction": "outgoing", "task_state": "completed",
                "delivery_chip": "replied",
                "text": "second", "reply_text": "thanks",
                "sender_bot_id": "me", "recipient_bot_id": "bestiedog",
                "sender_display_name": "Me",
                "tags": [], "vertical": None,
                "created_at": "2026-05-30T12:00:00+00:00",
                "ack_at": None, "submit_at": None,
                "replied_at": "2026-05-30T12:05:00+00:00",
            },
        ]
    )
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/conversations/bestiedog",
        json=multi_msg_payload,
        status=200,
        match_querystring=False,
    )
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={})},
        status=200,
    )
    client = AgentClient(token="bt_test", bot_id="me")
    ctx = client.dm.context_for_wake("bestiedog")
    assert len(ctx.recent_turns) == 2
    assert ctx.recent_turns[0]["text"] == "first"
    assert ctx.recent_turns[0]["direction"] == "incoming"
    assert ctx.recent_turns[0]["reply_text"] is None
    assert ctx.recent_turns[1]["text"] == "second"
    assert ctx.recent_turns[1]["reply_text"] == "thanks"
    assert ctx.recent_turns[1]["task_id"] == "msg_b"


@responses.activate
def test_context_for_wake_default_max_turns_is_10():
    """Default max_turns kwarg → limit=10 on the wire."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/conversations/bestiedog",
        json=_conv_payload(),
        status=200,
        match_querystring=False,
    )
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={})},
        status=200,
    )
    client = AgentClient(token="bt_test", bot_id="me")
    client.dm.context_for_wake("bestiedog")
    assert "limit=10" in responses.calls[0].request.url


@responses.activate
def test_context_for_wake_card_dict_path_used_when_present():
    """If client.card is a dict-like (not AgentCard), to_dict path falls
    back to dict() coercion."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/conversations/bestiedog",
        json=_conv_payload(),
        status=200,
        match_querystring=False,
    )
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={})},
        status=200,
    )
    client = AgentClient(token="bt_test", bot_id="me")
    # Plain dict masquerading as card — exercises the fallback path
    client.card = {"name": "DictAgent", "description": "from dict"}  # type: ignore[assignment]
    ctx = client.dm.context_for_wake("bestiedog")
    assert ctx.me is not None
    assert ctx.me["name"] == "DictAgent"
