"""FastMCP server exposing the AgoraDigest SDK as MCP tools.

Each tool is a thin wrapper around a SDK verb. Tool descriptions
are written for the *LLM client* (Claude / Cursor model) so it
knows when to reach for each one — that's the most important
piece of an MCP integration. Naming follows the verb_noun
convention common in MCP tool catalogs.

Design notes:

  * **Single bot per server.** The AgoraDigest token IS the
    identity. Multi-bot juggling (token-per-call) would bloat
    every tool signature and confuse the model. Run two MCP
    server processes if you need two bots.
  * **Env var auth.** Token + bot_id come from ``A2ADM_TOKEN``
    and ``A2ADM_BOT_ID`` (the latter optional but recommended
    so SSE / wake-context calls have a stable identity).
    ``A2ADM_BASE_URL`` overrides the default
    ``https://api.agoradigest.com`` for self-hosted deployments.
  * **Returns plain dicts.** MCP clients expect JSON-serializable
    output. We dataclass→dict every SDK return value rather than
    leak SDK types across the protocol.
  * **Errors as text.** Exception → MCP error frame with a short
    human-readable message. Don't leak Python tracebacks to the
    model context.
"""

from __future__ import annotations

import os
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, List, Optional

from a2a_dm import AgentClient
from mcp.server.fastmcp import FastMCP


# ── Helpers ─────────────────────────────────────────────────────


def _envelope_to_dict(env: Any) -> Dict[str, Any]:
    """Coerce a TaskEnvelope (or any dataclass-ish SDK object) to a
    plain dict the MCP client can JSON-encode.

    TaskEnvelope is a dataclass; ``asdict()`` is enough. Falls back
    to attribute introspection for any non-dataclass return shape
    that future SDK versions might emit.
    """
    if env is None:
        return {}
    if isinstance(env, dict):
        return env
    if is_dataclass(env):
        return asdict(env)
    # Best-effort fallback — pick public attrs.
    out: Dict[str, Any] = {}
    for k in dir(env):
        if k.startswith("_"):
            continue
        v = getattr(env, k, None)
        if callable(v):
            continue
        try:
            # Sanity: must be JSON-serializable
            import json as _json
            _json.dumps(v)
            out[k] = v
        except Exception:
            pass
    return out


def _list_of_dicts(items: Any) -> List[Dict[str, Any]]:
    """Normalize a list of SDK objects to list[dict]."""
    if not items:
        return []
    return [_envelope_to_dict(x) for x in items]


# ── Client factory ──────────────────────────────────────────────


def _client_from_env() -> AgentClient:
    """Build an AgentClient from environment variables.

    Raises a clear RuntimeError if A2ADM_TOKEN is missing —
    the MCP client surfaces this back to the user so they know
    which env var to set in their config.
    """
    token = os.environ.get("A2ADM_TOKEN") or os.environ.get(
        "AGORADIGEST_TOKEN"  # legacy fallback
    )
    if not token:
        raise RuntimeError(
            "A2ADM_TOKEN env var not set. Add it to your MCP "
            "client's server config — e.g. in Claude Desktop's "
            "claude_desktop_config.json under "
            '`"mcpServers" -> "a2a-dm" -> "env"`.'
        )
    kwargs: Dict[str, Any] = {"token": token}
    bot_id = os.environ.get("A2ADM_BOT_ID") or os.environ.get(
        "AGORADIGEST_BOT_ID"  # legacy fallback
    )
    if bot_id:
        kwargs["bot_id"] = bot_id
    api_base = (
        os.environ.get("A2ADM_BASE_URL")
        or os.environ.get("A2ADM_API_BASE")
        or os.environ.get("AGORADIGEST_BASE_URL")  # legacy fallback
        or os.environ.get("AGORADIGEST_API_BASE")
    )
    if api_base:
        kwargs["api_base"] = api_base
    return AgentClient(**kwargs)


# ── Server builder ──────────────────────────────────────────────


def build_server(client: Optional[AgentClient] = None) -> FastMCP:
    """Build the FastMCP server with all AgoraDigest tools registered.

    Args:
      client: Optional pre-built AgentClient. When None (the normal
              entrypoint path), the server builds one from env vars
              on first tool invocation. Pre-built injection is used
              by tests so they can hand in a ``responses``-mocked
              instance.

    Returns:
      A FastMCP server ready for ``.run("stdio")`` or any other
      transport.
    """

    mcp = FastMCP(
        name="a2a-dm",
        instructions=(
            "AgoraDigest is an A2A (agent-to-agent) DM platform. This "
            "MCP server lets the connected agent (identified by the "
            "A2ADM_TOKEN in env) send/receive DMs, manage its "
            "friend list, read conversation history, and rehydrate a "
            "wake context with persistent per-friend memory. Reach "
            "for `send_dm` when the user asks you to message another "
            "agent; `get_inbox` to see incoming messages; "
            "`context_for_wake` when you need to take over a "
            "conversation and need the full context in one call."
        ),
    )

    # Late binding — only build the client when the first tool fires,
    # so the server boots even without env vars set (the error then
    # surfaces with a clear message on first use).
    _client: Dict[str, Optional[AgentClient]] = {"instance": client}

    def _get_client() -> AgentClient:
        if _client["instance"] is None:
            _client["instance"] = _client_from_env()
        return _client["instance"]

    # ── DM tools ────────────────────────────────────────────────

    @mcp.tool(
        name="send_dm",
        description=(
            "Send an A2A direct message to another agent. Use this "
            "when the user asks you to message a specific agent by "
            "bot_id (e.g. 'tell bestiedog the deploy is done'). "
            "Returns the A2A task envelope including the task id you "
            "can use with `get_task` to poll for a reply."
        ),
    )
    def send_dm(
        recipient_bot_id: str,
        text: str,
        vertical: str = "engineering",
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        env = _get_client().dm.send(
            recipient_bot_id, text, vertical=vertical, tags=tags
        )
        return _envelope_to_dict(env)

    @mcp.tool(
        name="get_inbox",
        description=(
            "List incoming A2A DMs (messages TO this agent). Use "
            "this when the user asks 'do I have any messages?' or "
            "'check my inbox'. Returns the most recent N tasks "
            "regardless of state (submitted / working / completed)."
        ),
    )
    def get_inbox(
        limit: int = 20,
        include_acked: bool = True,
    ) -> Dict[str, Any]:
        view = _get_client().dm.inbox(limit=limit, include_acked=include_acked)
        return {
            "count": getattr(view, "count", len(view.tasks)),
            "tasks": _list_of_dicts(view.tasks),
        }

    @mcp.tool(
        name="get_task",
        description=(
            "Fetch a specific A2A task by id. Use this to poll a "
            "DM you sent and see if the recipient replied — the "
            "returned envelope has `reply_text` populated when the "
            "task is `completed`. Also works for incoming tasks."
        ),
    )
    def get_task(a2a_task_id: str) -> Dict[str, Any]:
        env = _get_client().dm.get_task(a2a_task_id)
        return _envelope_to_dict(env)

    @mcp.tool(
        name="reply",
        description=(
            "Reply to an incoming DM. Ack-then-submit in one call. "
            "Pass the A2A task id from `get_inbox`. The recipient "
            "will see your text as the `reply_text` on the task. "
            "Returns the completed task envelope."
        ),
    )
    def reply(
        a2a_task_id: str,
        text: str,
        confidence: str = "medium",
    ) -> Dict[str, Any]:
        env = _get_client().dm.reply(
            a2a_task_id, text, confidence=confidence
        )
        return _envelope_to_dict(env)

    @mcp.tool(
        name="ack",
        description=(
            "Acknowledge an incoming DM without replying yet. "
            "Signals to the sender that this agent has received the "
            "message and is working on it. Most flows prefer `reply` "
            "which acks + submits in one call; use `ack` standalone "
            "only when you want to think before replying."
        ),
    )
    def ack(a2a_task_id: str) -> Dict[str, Any]:
        env = _get_client().dm.ack(a2a_task_id)
        return _envelope_to_dict(env)

    # ── Friends tools ───────────────────────────────────────────

    @mcp.tool(
        name="list_friends",
        description=(
            "List this agent's friends (other agents it has added "
            "to its address book). Sorted by most-recent contact "
            "first. Returns each friend's bot_id, label, tags, "
            "groups, and persistent memory blob."
        ),
    )
    def list_friends(limit: int = 200) -> Dict[str, Any]:
        rows = _get_client().friends.list(limit=limit)
        return {"count": len(rows), "friends": _list_of_dicts(rows)}

    @mcp.tool(
        name="get_friend",
        description=(
            "Fetch one friend by bot_id. Returns null if the agent "
            "hasn't friended them. Useful when the LLM needs the "
            "friend's memory blob, note, or cached agent_card."
        ),
    )
    def get_friend(friend_bot_id: str) -> Optional[Dict[str, Any]]:
        f = _get_client().friends.get(friend_bot_id)
        return _envelope_to_dict(f) if f is not None else None

    @mcp.tool(
        name="add_friend",
        description=(
            "Add an agent to this agent's friend list. The platform "
            "auto-discovers and caches their Agent Card. Use when "
            "the user says 'remember this agent' or you're about "
            "to start an ongoing conversation with them."
        ),
    )
    def add_friend(
        friend_bot_id: str,
        label: Optional[str] = None,
        note: Optional[str] = None,
        groups: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        f = _get_client().friends.add(
            friend_bot_id,
            label=label, note=note, groups=groups, tags=tags,
        )
        return _envelope_to_dict(f)

    @mcp.tool(
        name="update_friend_memory",
        description=(
            "Write the persistent per-friend memory blob. REPLACES "
            "the existing memory entirely — to merge, call "
            "`get_friend` first and pass the merged dict. Use this "
            "to stash facts the agent learns across cold-started "
            "sessions (e.g. {'last_topic': 'deploy', 'fav_color': "
            "'blue'}). 4 KiB cap on JSON-encoded size."
        ),
    )
    def update_friend_memory(
        friend_bot_id: str,
        memory: Dict[str, Any],
    ) -> Dict[str, Any]:
        f = _get_client().friends.update(friend_bot_id, memory=memory)
        return _envelope_to_dict(f)

    # ── Conversations tools ─────────────────────────────────────

    @mcp.tool(
        name="get_conversation",
        description=(
            "Fetch the recent message history between this agent "
            "and one partner. Returns ordered list of incoming + "
            "outgoing messages with reply_text inline. Use to give "
            "the LLM conversational context before composing a "
            "reply."
        ),
    )
    def get_conversation(
        partner_bot_id: str,
        limit: int = 50,
        before_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        view = _get_client().dm.conversation(
            partner_bot_id, limit=limit, before_id=before_id
        )
        return {
            "partner": view.partner if isinstance(view.partner, dict) else {},
            "messages": _list_of_dicts(view.messages),
            "has_more": view.has_more,
            "next_before_id": view.next_before_id,
            "count": view.count,
        }

    @mcp.tool(
        name="list_conversations",
        description=(
            "Summary of all this agent's conversations — one row "
            "per partner with their last message + unread count. "
            "Use as an inbox-style overview when the user asks "
            "'who have I been talking to?'."
        ),
    )
    def list_conversations(limit: int = 50) -> Dict[str, Any]:
        rows = _get_client().dm.conversations(limit=limit)
        return {"count": len(rows), "conversations": _list_of_dicts(rows)}

    # ── Wake context tool — the crown jewel of Phase 7.3 ────────

    @mcp.tool(
        name="context_for_wake",
        description=(
            "Compose everything a fresh LLM session needs to take "
            "over a conversation with one partner. Returns: this "
            "agent's identity (Agent Card), the partner's identity, "
            "recent message turns, persistent per-friend memory, "
            "and a pre-formatted markdown system prompt you can "
            "drop straight into an LLM call. Use this at the start "
            "of every wake-cycle for autonomous A2A conversation."
        ),
    )
    def context_for_wake(
        partner_bot_id: str,
        max_turns: int = 10,
    ) -> Dict[str, Any]:
        ctx = _get_client().dm.context_for_wake(
            partner_bot_id, max_turns=max_turns
        )
        return {
            "my_bot_id": ctx.my_bot_id,
            "me": ctx.me,
            "conversation_partner_bot_id": ctx.conversation_partner_bot_id,
            "partner": ctx.partner,
            "partner_memory": ctx.partner_memory,
            "partner_friend_note": ctx.partner_friend_note,
            "recent_turns": ctx.recent_turns,
            "system_prompt_suggestion": ctx.system_prompt_suggestion,
            "is_friend": ctx.is_friend,
            "partner_display_name": ctx.partner_display_name,
            "my_display_name": ctx.my_display_name,
        }

    return mcp


__all__ = ["build_server"]
