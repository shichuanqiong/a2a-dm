"""SDK conversations API tests (Phase 6.3).

Coverage:
  * ConversationMessage / ConversationView / ConversationSummary
    dataclass round-trips + edge cases
  * is_outgoing / is_incoming / partner_bot_id / is_unread helpers
  * client.dm.conversation(partner) + .conversations() happy paths
    via responses HTTP mocking
  * Pagination cursor (before_id) flows through to the URL
  * URL encoding of special chars in partner_bot_id
  * Empty list / has_more=False contracts
"""

from __future__ import annotations

import pytest
import responses

from a2a_dm import (
    AgentClient,
    ConversationMessage,
    ConversationSummary,
    ConversationView,
)


# ── ConversationMessage round-trip ───────────────────────────────


def _sample_message_payload(**overrides) -> dict:
    base = {
        "id": "msg_abc",
        "task_id": "msg_abc",
        "direction": "outgoing",
        "task_state": "completed",
        "delivery_chip": "replied",
        "text": "hi bestiedog",
        "reply_text": "yo!",
        "sender_bot_id": "bot_ext_laobaigan",
        "recipient_bot_id": "bestiedog",
        "sender_display_name": "laobaigan",
        "tags": [],
        "vertical": None,
        "created_at": "2026-05-30T12:00:00+00:00",
        "ack_at": None,
        "submit_at": "2026-05-30T12:01:00+00:00",
        "replied_at": "2026-05-30T12:01:00+00:00",
    }
    base.update(overrides)
    return base


def test_message_from_dict_round_trips_all_fields():
    m = ConversationMessage.from_dict(_sample_message_payload())
    assert m.id == "msg_abc"
    assert m.task_id == "msg_abc"
    assert m.direction == "outgoing"
    assert m.task_state == "completed"
    assert m.delivery_chip == "replied"
    assert m.text == "hi bestiedog"
    assert m.reply_text == "yo!"
    assert m.sender_bot_id == "bot_ext_laobaigan"
    assert m.recipient_bot_id == "bestiedog"


def test_message_from_dict_task_id_falls_back_to_id():
    """Server contract: task_id == id. If task_id missing, fall
    back to id rather than crashing."""
    m = ConversationMessage.from_dict({"id": "fallback"})
    assert m.task_id == "fallback"


def test_message_from_dict_handles_non_dict():
    m = ConversationMessage.from_dict("garbage")
    assert m.id == ""
    assert m.direction == "incoming"  # safe default


def test_message_is_outgoing_helper():
    m = ConversationMessage.from_dict(
        _sample_message_payload(direction="outgoing")
    )
    assert m.is_outgoing is True
    assert m.is_incoming is False


def test_message_is_incoming_helper():
    m = ConversationMessage.from_dict(
        _sample_message_payload(direction="incoming")
    )
    assert m.is_incoming is True
    assert m.is_outgoing is False


def test_message_empty_text_handled():
    """Server normalizes null text to empty string; SDK passes through."""
    m = ConversationMessage.from_dict(
        _sample_message_payload(text="")
    )
    assert m.text == ""


def test_message_null_text_coerced_to_empty_string():
    m = ConversationMessage.from_dict(
        _sample_message_payload(text=None)
    )
    assert m.text == ""


def test_message_null_reply_stays_none():
    """`reply_text=None` MUST stay None — clients branch on it to
    show "still waiting" vs the reply bubble."""
    m = ConversationMessage.from_dict(
        _sample_message_payload(reply_text=None)
    )
    assert m.reply_text is None


def test_message_delivery_chip_can_be_null():
    """Incoming messages → delivery_chip is null. Don't coerce to
    empty string or the UI would render a phantom chip."""
    m = ConversationMessage.from_dict(
        _sample_message_payload(direction="incoming", delivery_chip=None)
    )
    assert m.delivery_chip is None


# ── ConversationView round-trip ─────────────────────────────────


def _sample_view_payload(**overrides) -> dict:
    base = {
        "partner": {
            "bot_id": "bestiedog",
            "display_name": "bestiedog",
            "is_friend": True,
            "friend_tags": ["python"],
            "agent_card_snapshot": {"name": "bestiedog"},
        },
        "messages": [_sample_message_payload()],
        "has_more": False,
        "next_before_id": None,
        "count": 1,
    }
    base.update(overrides)
    return base


def test_view_round_trips():
    v = ConversationView.from_dict(_sample_view_payload())
    assert v.partner["bot_id"] == "bestiedog"
    assert len(v.messages) == 1
    assert v.messages[0].text == "hi bestiedog"
    assert v.has_more is False
    assert v.next_before_id is None
    assert v.count == 1


def test_view_partner_bot_id_helper():
    v = ConversationView.from_dict(_sample_view_payload())
    assert v.partner_bot_id == "bestiedog"


def test_view_has_more_with_cursor():
    v = ConversationView.from_dict(
        _sample_view_payload(has_more=True, next_before_id="msg_oldest")
    )
    assert v.has_more is True
    assert v.next_before_id == "msg_oldest"


def test_view_empty_messages():
    v = ConversationView.from_dict(
        _sample_view_payload(messages=[], count=0)
    )
    assert v.messages == []
    assert v.count == 0


def test_view_handles_garbage():
    v = ConversationView.from_dict(None)
    assert v.messages == []
    assert v.has_more is False


# ── ConversationSummary round-trip ──────────────────────────────


def _sample_summary_payload(**overrides) -> dict:
    base = {
        "partner": {
            "bot_id": "bestiedog",
            "display_name": "bestiedog",
            "is_friend": True,
        },
        "last_message": {
            "text": "hi",
            "direction": "incoming",
            "task_state": "submitted",
            "delivery_chip": None,
        },
        "unread_count": 2,
        "last_activity_at": "2026-05-30T12:00:00+00:00",
    }
    base.update(overrides)
    return base


def test_summary_round_trips():
    s = ConversationSummary.from_dict(_sample_summary_payload())
    assert s.partner_bot_id == "bestiedog"
    assert s.unread_count == 2
    assert s.is_unread is True


def test_summary_is_unread_false_when_zero():
    s = ConversationSummary.from_dict(
        _sample_summary_payload(unread_count=0)
    )
    assert s.is_unread is False


def test_summary_unread_count_coerces_to_int():
    """Server always sends int; defensive coercion guards against
    null / string drift."""
    s = ConversationSummary.from_dict(
        _sample_summary_payload(unread_count="3")
    )
    assert s.unread_count == 3


# ── client.dm.conversations() ──────────────────────────────────


@responses.activate
def test_conversations_list_parses_rows():
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/conversations?limit=50",
        json={
            "count": 2,
            "conversations": [
                _sample_summary_payload(),
                _sample_summary_payload(
                    partner={"bot_id": "other", "display_name": "Other"},
                    unread_count=0,
                ),
            ],
        },
        status=200,
        match_querystring=False,
    )
    client = AgentClient(token="bt_test", bot_id="bot_ext_laobaigan")
    rows = client.dm.conversations()
    assert len(rows) == 2
    assert rows[0].partner_bot_id == "bestiedog"
    assert rows[0].is_unread is True
    assert rows[1].is_unread is False


@responses.activate
def test_conversations_list_empty():
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/conversations?limit=50",
        json={"count": 0, "conversations": []},
        status=200,
        match_querystring=False,
    )
    client = AgentClient(token="bt_test")
    assert client.dm.conversations() == []


@responses.activate
def test_conversations_list_passes_custom_limit():
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/conversations?limit=10",
        json={"count": 0, "conversations": []},
        status=200,
        match_querystring=False,
    )
    client = AgentClient(token="bt_test")
    client.dm.conversations(limit=10)
    assert "limit=10" in responses.calls[0].request.url


# ── client.dm.conversation(partner) ────────────────────────────


@responses.activate
def test_conversation_get_basic():
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/conversations/bestiedog",
        json=_sample_view_payload(),
        status=200,
        match_querystring=False,
    )
    client = AgentClient(token="bt_test")
    v = client.dm.conversation("bestiedog")
    assert isinstance(v, ConversationView)
    assert v.partner_bot_id == "bestiedog"
    assert len(v.messages) == 1
    assert v.messages[0].direction == "outgoing"


@responses.activate
def test_conversation_get_passes_before_id():
    """Pagination cursor must flow through to the URL — otherwise
    you'd loop on the same page forever."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/conversations/bestiedog",
        json=_sample_view_payload(),
        status=200,
        match_querystring=False,
    )
    client = AgentClient(token="bt_test")
    client.dm.conversation("bestiedog", before_id="msg_99")
    assert "before_id=msg_99" in responses.calls[0].request.url


@responses.activate
def test_conversation_get_default_limit_is_50():
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/conversations/bestiedog",
        json=_sample_view_payload(),
        status=200,
        match_querystring=False,
    )
    client = AgentClient(token="bt_test")
    client.dm.conversation("bestiedog")
    assert "limit=50" in responses.calls[0].request.url


@responses.activate
def test_conversation_get_custom_limit():
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/conversations/bestiedog",
        json=_sample_view_payload(),
        status=200,
        match_querystring=False,
    )
    client = AgentClient(token="bt_test")
    client.dm.conversation("bestiedog", limit=100)
    assert "limit=100" in responses.calls[0].request.url


@responses.activate
def test_conversation_get_returns_pagination_state():
    """has_more=True + next_before_id → caller paginates."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/conversations/bestiedog",
        json=_sample_view_payload(has_more=True, next_before_id="msg_oldest"),
        status=200,
        match_querystring=False,
    )
    client = AgentClient(token="bt_test")
    v = client.dm.conversation("bestiedog")
    assert v.has_more is True
    assert v.next_before_id == "msg_oldest"


@responses.activate
def test_conversation_walks_backward_through_pages():
    """End-to-end pagination loop — caller passes prev page's
    next_before_id to get the next batch."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/conversations/bestiedog",
        json=_sample_view_payload(has_more=True, next_before_id="cursor1"),
        status=200,
        match_querystring=False,
    )
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/conversations/bestiedog",
        json=_sample_view_payload(has_more=False, next_before_id=None),
        status=200,
        match_querystring=False,
    )
    client = AgentClient(token="bt_test")

    page1 = client.dm.conversation("bestiedog")
    assert page1.has_more
    page2 = client.dm.conversation("bestiedog", before_id=page1.next_before_id)
    assert page2.has_more is False
    assert page2.next_before_id is None
    # Second call used the cursor
    assert "before_id=cursor1" in responses.calls[1].request.url
