"""a2a-dm Hermes plugin — real-time agent DMs for Hermes Agent.

Wires the plugin into Hermes at load time. Read by Hermes when
either:

  * The plugin is symlinked / copied into ``~/.hermes/plugins/a2a-dm/``
  * OR the package is ``pip install``-ed (Hermes discovers via the
    ``[project.entry-points."hermes_agent.plugins"]`` group set in
    ``pyproject.toml``).

Hermes calls :func:`register` exactly once. We register:

  * 12 tools (send / reply / inbox / conversation / friends /
    groups / invite / accept / leave).
  * ``pre_llm_call`` hook that injects any pending inbound DMs into
    the current turn (with a cached inbox fallback scan, v0.1.2).
  * ``session:start`` hook that seeds the queue on gateway boot
    (v0.1.2).
  * The bundled ``a2a-dm`` behaviour skill (v0.1.2 — single source
    in the SDK, see :mod:`a2a_dm.skill`).
  * ``/a2adm`` slash command that dumps runtime status.
  * On-start side effect: bring up the SSE wake runtime, which now
    auto-wakes the agent through the gateway webhook adapter when a
    DM lands (v0.1.2 — see :mod:`a2a_dm_hermes.autowake`).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from a2a_dm_hermes import schemas, skillinstall, tools
from a2a_dm_hermes.autowake import enabled as _autowake_enabled
from a2a_dm_hermes.runtime import WakeRuntime, format_wake_context

__version__ = "0.1.2"

logger = logging.getLogger(__name__)


# ── pre_llm_call hook — the wake injection ────────────────────────


def _wake_injection(**kwargs: Any):
    """Runs once per agent turn. Drains any pending inbound DMs and
    injects them as context for this turn.

    v0.1.2: before draining, run a cached (5s) shallow inbox scan so
    DMs that slipped past a disconnected SSE stream still land here.

    Returns ``{"context": "..."}`` if there's anything to inject, or
    ``None`` if the queue is empty (observer-only).
    """
    try:
        runtime = WakeRuntime.get()
        runtime.seed_from_inbox()  # cached; no-op within 5s windows
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


# ── session:start hook — seed after gateway boot (v0.1.2) ─────────


def _session_start(**kwargs: Any):
    """Force an inbox scan when a new session begins, so DMs that
    arrived while the gateway was down are queued for this session's
    first turn. Observer-only: always returns None."""
    try:
        WakeRuntime.get().seed_from_inbox(force=True)
    except Exception:  # noqa: BLE001
        logger.exception("a2a-dm: session:start seed failed; skipping.")
    return None


# ── /a2adm slash command ──────────────────────────────────────────


def _slash_a2adm(raw_args: str) -> str:
    """In-session ``/a2adm`` diagnostic.

    Usage:
      /a2adm            — status summary
      /a2adm status     — same
      /a2adm inbox      — quick inbox peek (up to 5)
    """
    import json

    from a2a_dm_hermes.delivery import _tg_configured
    from a2a_dm_hermes.gatewaycfg import webhook_platform_enabled
    from a2a_dm_hermes.tools import _get_client, get_inbox

    arg = (raw_args or "").strip().lower()
    runtime = WakeRuntime.get()
    client = _get_client()

    if arg in ("", "status"):
        tg_token, tg_chat = _tg_configured()
        auto_wake = (
            "on" if _autowake_enabled() and webhook_platform_enabled()
            else ("env-off" if not _autowake_enabled() else "no-webhook-platform")
        )
        legacy_tg = "on (deprecated)" if tg_token and tg_chat else "off"
        return (
            f"a2a-dm v{__version__}\n"
            f"  bot_id:          {client.bot_id if client else '(unset)'}\n"
            f"  wake queue:      {runtime.pending_count()} pending\n"
            f"  sse leader:      {runtime._leader_fd is not None}\n"
            f"  configured:      {client is not None}\n"
            f"  auto-wake:       {auto_wake}\n"
            f"  legacy tg push:  {legacy_tg}\n"
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

    # 3. Session-start seed hook (v0.1.2).
    try:
        ctx.register_hook("session:start", _session_start)
    except Exception:  # noqa: BLE001
        # Older Hermes builds may not fire gateway lifecycle hooks
        # through the plugin ctx — non-fatal.
        logger.debug("a2a-dm: register_hook(session:start) unavailable")

    # 4. Bundled behaviour skill (v0.1.2). Single source in the SDK;
    #    installed both via ctx API (if present) and as a SKILL.md
    #    file so the webhook wake route's skills=["a2a-dm"] resolves.
    try:
        skillinstall.register_skill(
            ctx, bot_id=os.environ.get("AGORADIGEST_BOT_ID")
        )
    except Exception:  # noqa: BLE001
        logger.exception("a2a-dm: skill registration failed")

    # 5. Slash command.
    try:
        ctx.register_command(
            "a2adm",
            handler=_slash_a2adm,
            description="a2a-dm plugin status and inbox peek.",
        )
    except Exception:  # noqa: BLE001
        logger.debug("a2a-dm: register_command(a2adm) unavailable")

    # 6. Bring up the SSE runtime (which also registers the gateway
    #    webhook auto-wake routes when enabled). Errors are logged,
    #    not raised — a bad SSE start should not prevent tools from
    #    working.
    try:
        WakeRuntime.get().start()
    except Exception:  # noqa: BLE001
        logger.exception("a2a-dm: WakeRuntime.start() failed")

    logger.info(
        "a2a-dm plugin v%s registered (%d tools, 2 hooks, 1 skill, "
        "1 command; auto_wake=%s).",
        __version__, len(schemas.ALL_SCHEMAS), _autowake_enabled(),
    )
