"""a2a-dm Hermes plugin — real-time agent DMs for Hermes Agent.

Wires the plugin into Hermes at load time. Reads by Hermes when
either:

  * The plugin is symlinked / copied into ``~/.hermes/plugins/a2a-dm/``
  * OR the package is ``pip install``-ed (Hermes discovers via the
    ``[project.entry-points."hermes_agent.plugins"]`` group set in
    ``pyproject.toml``).

Hermes calls :func:`register` exactly once. We register:

  * 12 tools (send / reply / inbox / conversation / friends /
    groups / invite / accept / leave).
  * ``pre_llm_call`` hook that injects any pending inbound DMs into
    the current turn so the LLM sees them alongside the user
    message.
  * ``/a2adm`` slash command that dumps runtime status.
  * On-start side effect: bring up the SSE wake runtime.
"""

from __future__ import annotations

import logging
from typing import Any

from a2a_dm_hermes import schemas, tools
from a2a_dm_hermes.runtime import WakeRuntime, format_wake_context

__version__ = "0.1.1"

logger = logging.getLogger(__name__)


# ── pre_llm_call hook — the wake injection ────────────────────────


def _wake_injection(**kwargs: Any):
    """Runs once per agent turn. Drains any pending inbound DMs and
    injects them as context for this turn.

    Returns ``{"context": "..."}`` if there's anything to inject, or
    ``None`` if the queue is empty (observer-only). This return shape
    is what Hermes's ``pre_llm_call`` hook uses for context injection.
    """
    try:
        runtime = WakeRuntime.get()
        entries = runtime.drain()
        if not entries:
            return None
        block = format_wake_context(entries)
        if not block:
            return None
        return {"context": block}
    except Exception:  # noqa: BLE001 — hook must never crash the turn
        logger.exception("a2a-dm: wake injection failed; skipping.")
        return None


# ── /a2adm slash command ──────────────────────────────────────────


def _slash_a2adm(raw_args: str) -> str:
    """In-session ``/a2adm`` diagnostic.

    Usage:
      /a2adm            — status summary
      /a2adm status     — same
      /a2adm inbox      — quick inbox peek (up to 5)
    """
    from a2a_dm_hermes.tools import _get_client, get_inbox
    import json

    arg = (raw_args or "").strip().lower()
    runtime = WakeRuntime.get()
    client = _get_client()

    if arg in ("", "status"):
        from a2a_dm_hermes.runtime import _tg_configured
        tg_token, tg_chat = _tg_configured()
        tg_status = "on" if tg_token and tg_chat else "off"
        return (
            f"a2a-dm v{__version__}\n"
            f"  bot_id:          {client.bot_id if client else '(unset)'}\n"
            f"  wake queue:      {runtime.pending_count()} pending\n"
            f"  sse leader:      {runtime._leader_fd is not None}\n"
            f"  configured:      {client is not None}\n"
            f"  tg proactive:    {tg_status}\n"
        )

    if arg == "inbox":
        raw = get_inbox({"limit": 5, "state": "submitted"})
        try:
            data = json.loads(raw)
        except Exception:
            return raw
        if data.get("error"):
            return f"error: {data['error']}"
        if not data.get("tasks"):
            return "inbox: empty"
        lines = [f"inbox: {data['count']} pending"]
        for t in data["tasks"]:
            tag = "GROUP" if t.get("is_group_message") else "DM"
            lines.append(
                f"  [{tag}] from {t['sender_bot_id']}: {t['text'][:80]}"
            )
        return "\n".join(lines)

    return "Usage: /a2adm [status|inbox]"


# ── Hermes-facing entry point ─────────────────────────────────────


def register(ctx) -> None:
    """Called once by Hermes at plugin-load time."""
    # 1. Wire the 12 tools.
    for tool_name, schema in schemas.ALL_SCHEMAS:
        handler = tools.HANDLERS.get(tool_name)
        if not handler:
            logger.error(
                "a2a-dm: schema %s has no handler — skipping.", tool_name
            )
            continue
        try:
            ctx.register_tool(
                name=tool_name,
                toolset="a2a-dm",
                schema=schema,
                handler=handler,
            )
        except Exception:  # noqa: BLE001
            logger.exception("a2a-dm: register_tool(%s) failed", tool_name)

    # 2. Wake-injection hook.
    try:
        ctx.register_hook("pre_llm_call", _wake_injection)
    except Exception:  # noqa: BLE001
        logger.exception("a2a-dm: register_hook(pre_llm_call) failed")

    # 3. Slash command.
    try:
        ctx.register_command(
            "a2adm",
            handler=_slash_a2adm,
            description="a2a-dm plugin status and inbox peek.",
        )
    except Exception:  # noqa: BLE001
        # Not fatal — some Hermes versions may not expose this API.
        logger.debug("a2a-dm: register_command(a2adm) unavailable")

    # 4. Bring up the SSE runtime. Errors here are logged, not raised
    #    — a bad SSE start should not prevent tools from working.
    try:
        WakeRuntime.get().start()
    except Exception:  # noqa: BLE001
        logger.exception("a2a-dm: WakeRuntime.start() failed")

    logger.info(
        "a2a-dm plugin v%s registered (%d tools, 1 hook, 1 command).",
        __version__, len(schemas.ALL_SCHEMAS),
    )
