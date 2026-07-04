"""a2a-dm-mcp server tests.

Coverage:
  * build_server returns a FastMCP instance
  * All expected tools are registered (regression guard against
    accidental drops if someone refactors)
  * Each tool has a non-empty description (LLMs need these to
    decide when to call)
  * Env-var client builder raises a useful error when token missing
  * Env-var client builder picks up token + bot_id + base_url
  * Tool invocation paths call the SDK correctly (mock client)
  * Output coercion handles dataclass, dict, and None cleanly
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest

from a2a_dm_mcp.server import (
    _client_from_env,
    _envelope_to_dict,
    _list_of_dicts,
    build_server,
)


# ── Server / tool registration ──────────────────────────────────


def _list_tools(mcp):
    return asyncio.run(mcp.list_tools())


def test_build_server_returns_fastmcp():
    from mcp.server.fastmcp import FastMCP
    mcp = build_server(client=MagicMock())
    assert isinstance(mcp, FastMCP)


EXPECTED_TOOLS = {
    "send_dm",
    "get_inbox",
    "get_task",
    "reply",
    "ack",
    "list_friends",
    "get_friend",
    "add_friend",
    "update_friend_memory",
    "get_conversation",
    "list_conversations",
    "context_for_wake",
}


def test_all_expected_tools_registered():
    """Regression guard — every documented tool must be present.
    Adding a new tool should bump this set."""
    mcp = build_server(client=MagicMock())
    names = {t.name for t in _list_tools(mcp)}
    assert names == EXPECTED_TOOLS


def test_tools_have_descriptions():
    """MCP clients show these to the LLM to decide when to call.
    Empty descriptions are useless."""
    mcp = build_server(client=MagicMock())
    for t in _list_tools(mcp):
        assert t.description, f"tool {t.name} has no description"
        assert len(t.description) > 50, (
            f"tool {t.name} description too short — "
            "needs enough detail for the LLM to route correctly"
        )


def test_tool_count_is_twelve():
    """Hard pin — changes here mean docs need updating too."""
    mcp = build_server(client=MagicMock())
    assert len(_list_tools(mcp)) == 12


# ── Env var client builder ──────────────────────────────────────


def test_client_from_env_raises_without_token():
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError, match="A2ADM_TOKEN"):
            _client_from_env()


def test_client_from_env_with_just_token():
    with patch.dict(os.environ, {"A2ADM_TOKEN": "bt_xxx"}, clear=True):
        client = _client_from_env()
        assert client._http.token == "bt_xxx"


def test_client_from_env_passes_bot_id():
    env = {
        "A2ADM_TOKEN": "bt_xxx",
        "A2ADM_BOT_ID": "my_bot",
    }
    with patch.dict(os.environ, env, clear=True):
        client = _client_from_env()
        assert client.bot_id == "my_bot"


def test_client_from_env_passes_base_url():
    env = {
        "A2ADM_TOKEN": "bt_xxx",
        "A2ADM_BASE_URL": "https://api.staging.agoradigest.com",
    }
    with patch.dict(os.environ, env, clear=True):
        client = _client_from_env()
        assert "staging" in client._http.api_base


# ── Output coercion helpers ─────────────────────────────────────


def test_envelope_to_dict_handles_none():
    assert _envelope_to_dict(None) == {}


def test_envelope_to_dict_passes_through_dict():
    d = {"id": "x", "state": "completed"}
    assert _envelope_to_dict(d) == d


def test_envelope_to_dict_dataclasses_friend():
    from a2a_dm import Friend
    f = Friend(friend_bot_id="bestie", label="Bestie", memory={"k": "v"})
    out = _envelope_to_dict(f)
    assert out["friend_bot_id"] == "bestie"
    assert out["label"] == "Bestie"
    assert out["memory"] == {"k": "v"}


def test_list_of_dicts_empty():
    assert _list_of_dicts([]) == []
    assert _list_of_dicts(None) == []


def test_list_of_dicts_coerces_each_item():
    from a2a_dm import Friend
    rows = [
        Friend(friend_bot_id="a"),
        Friend(friend_bot_id="b"),
    ]
    out = _list_of_dicts(rows)
    assert len(out) == 2
    assert out[0]["friend_bot_id"] == "a"
    assert out[1]["friend_bot_id"] == "b"


# ── Tool invocation paths via MagicMock client ──────────────────


def _call_tool(mcp, name, args):
    """FastMCP exposes _tool_manager._tools dict — pull the fn out
    and call it directly. This bypasses the JSON-RPC layer but
    exercises the wrapper logic that matters for these tests."""
    tool = mcp._tool_manager._tools[name]
    return tool.fn(**args)


def test_send_dm_calls_sdk():
    from a2a_dm import TaskEnvelope
    client = MagicMock()
    client.dm.send.return_value = TaskEnvelope(
        id="task_123", state="submitted", message={"text": "hi"}
    )
    mcp = build_server(client=client)
    out = _call_tool(mcp, "send_dm", {
        "recipient_bot_id": "bestiedog",
        "text": "hi",
    })
    client.dm.send.assert_called_once_with(
        "bestiedog", "hi", vertical="engineering", tags=None,
    )
    assert out["id"] == "task_123"


def test_get_inbox_returns_count_and_tasks():
    client = MagicMock()
    inbox_view = MagicMock()
    inbox_view.count = 2
    inbox_view.tasks = []
    client.dm.inbox.return_value = inbox_view
    mcp = build_server(client=client)
    out = _call_tool(mcp, "get_inbox", {"limit": 5})
    client.dm.inbox.assert_called_once_with(limit=5, include_acked=True)
    assert out["count"] == 2
    assert out["tasks"] == []


def test_reply_passes_confidence():
    from a2a_dm import TaskEnvelope
    client = MagicMock()
    client.dm.reply.return_value = TaskEnvelope(
        id="x", state="completed", message={}
    )
    mcp = build_server(client=client)
    _call_tool(mcp, "reply", {
        "a2a_task_id": "task_99",
        "text": "got it",
        "confidence": "high",
    })
    client.dm.reply.assert_called_once_with(
        "task_99", "got it", confidence="high",
    )


def test_get_friend_returns_none_when_missing():
    """SDK returns None for missing friends — MCP tool must
    pass that through (don't coerce to {})."""
    client = MagicMock()
    client.friends.get.return_value = None
    mcp = build_server(client=client)
    out = _call_tool(mcp, "get_friend", {"friend_bot_id": "stranger"})
    assert out is None


def test_update_friend_memory_plumbs_dict():
    from a2a_dm import Friend
    client = MagicMock()
    client.friends.update.return_value = Friend(
        friend_bot_id="bestie", memory={"k": "v"}
    )
    mcp = build_server(client=client)
    _call_tool(mcp, "update_friend_memory", {
        "friend_bot_id": "bestie",
        "memory": {"k": "v"},
    })
    client.friends.update.assert_called_once_with(
        "bestie", memory={"k": "v"}
    )


def test_context_for_wake_unpacks_all_fields():
    """Crown jewel tool — must expose every WakeContext field so
    the LLM can ignore system_prompt_suggestion and roll its own."""
    from a2a_dm import WakeContext
    client = MagicMock()
    client.dm.context_for_wake.return_value = WakeContext(
        my_bot_id="me",
        me={"name": "Me"},
        conversation_partner_bot_id="bestie",
        partner={"display_name": "Bestie", "is_friend": True},
        partner_memory={"topic": "deploy"},
        partner_friend_note="trusted",
        recent_turns=[{"direction": "outgoing", "text": "hi"}],
        system_prompt_suggestion="# You are Me\n...",
    )
    mcp = build_server(client=client)
    out = _call_tool(mcp, "context_for_wake", {
        "partner_bot_id": "bestie",
        "max_turns": 5,
    })
    client.dm.context_for_wake.assert_called_once_with(
        "bestie", max_turns=5,
    )
    assert out["my_bot_id"] == "me"
    assert out["partner_memory"] == {"topic": "deploy"}
    assert out["partner_friend_note"] == "trusted"
    assert out["recent_turns"][0]["text"] == "hi"
    assert "# You are Me" in out["system_prompt_suggestion"]
    assert out["is_friend"] is True
    assert out["partner_display_name"] == "Bestie"
    assert out["my_display_name"] == "Me"


def test_list_conversations_wraps_count():
    client = MagicMock()
    client.dm.conversations.return_value = []
    mcp = build_server(client=client)
    out = _call_tool(mcp, "list_conversations", {"limit": 10})
    client.dm.conversations.assert_called_once_with(limit=10)
    assert out == {"count": 0, "conversations": []}


def test_get_conversation_unpacks_view():
    client = MagicMock()
    view = MagicMock()
    view.partner = {"bot_id": "bestie"}
    view.messages = []
    view.has_more = False
    view.next_before_id = None
    view.count = 0
    client.dm.conversation.return_value = view
    mcp = build_server(client=client)
    out = _call_tool(mcp, "get_conversation", {"partner_bot_id": "bestie"})
    assert out["partner"] == {"bot_id": "bestie"}
    assert out["has_more"] is False
    assert out["count"] == 0


def test_late_binding_client_boots_without_env():
    """When build_server(client=None), the client is built lazily —
    server boot must NOT require A2ADM_TOKEN at module-load
    time (otherwise MCP client startup races would crash before the
    user sees the env-var error)."""
    with patch.dict(os.environ, {}, clear=True):
        # No env vars set, no client injected — must NOT raise.
        mcp = build_server(client=None)
        assert mcp is not None
        # And the tools are still registered.
        assert len(_list_tools(mcp)) == 12


def test_late_binding_first_tool_call_raises_with_helpful_msg():
    """When env var is missing AND the first tool actually fires,
    surface the A2ADM_TOKEN error so the user knows to fix
    their MCP config."""
    with patch.dict(os.environ, {}, clear=True):
        mcp = build_server(client=None)
        tool = mcp._tool_manager._tools["get_friend"]
        with pytest.raises(RuntimeError, match="A2ADM_TOKEN"):
            tool.fn(friend_bot_id="anyone")
