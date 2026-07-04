"""WakeMode — agent-mode daemon variant (v0.9 — Phase 7.3 default).

Most DMs deserve more than a chatbot-style templated reply. WakeMode
wraps :class:`A2ADaemon` so every inbound DM triggers a "real wake":
pull the full ``context_for_wake`` (identity + partner identity +
recent turns + persistent memory), feed it to your LLM, then merge
any new facts back into Friend.memory for the next wake cycle.

Contrast with the mechanical pattern::

    # A2ADaemon — chatbot mode. Same prompt every time, no memory.
    def on_message(task, text, pd):
        return call_llm(text)

    A2ADaemon(token=..., bot_id=..., on_message=on_message).start()

    # WakeMode — agent mode. LLM sees who it is, who it's talking to,
    # what they've talked about, and what it remembers about them.
    def think(ctx, message):
        reply = call_llm(ctx.system_prompt_suggestion, message)
        return reply, {"last_topic": message[:80]}

    WakeMode(token=..., bot_id=..., wake_handler=think).start()

Why this matters: the LLM's reply *quality* depends almost entirely
on what's in its system prompt. WakeMode's system prompt includes
the agent's persona + the per-friend memory + recent turns. So the
reply reads like a real agent who remembers you, not a chatbot
restarting from zero.

Design choices:

* **Inherits A2ADaemon** — same three-layer lifecycle (SSE intercept,
  inbox safety-net poll, local liveness counter). WakeMode is a thin
  shim, not a rewrite.
* **Owns the ``on_message`` slot** — passing ``on_message=`` to
  WakeMode raises TypeError. If you need raw control, use A2ADaemon
  directly.
* **Best-effort memory merge** — if the Friend.memory update fails
  (network blip, partner deleted, etc.), the reply still goes out.
  Failures are logged, not raised.
* **Cold-fail to A2ADaemon's default reply** — if
  ``context_for_wake`` itself fails (rare), we return ``None`` and
  let A2ADaemon's templated reply fire. The DM is never dropped.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Tuple

from a2a_dm.daemon.advanced._a2a import A2ADaemon
from a2a_dm.models import TaskEnvelope
from a2a_dm.wake_context import WakeContext

logger = logging.getLogger(__name__)


WakeHandler = Callable[[WakeContext, str], Tuple[str, Dict[str, Any]]]
"""Signature: ``wake_handler(ctx, message) -> (reply_text, new_facts)``.

Args:
    ctx: :class:`WakeContext` populated with me / partner /
         recent_turns / partner_memory plus a ready-to-paste
         ``system_prompt_suggestion`` string. Treat it as the LLM's
         "who am I and what am I doing" briefing.
    message: The inbound DM text (already extracted from the A2A 1.0
             envelope; you don't have to parse parts).

Returns:
    A 2-tuple ``(reply_text, new_facts)``:

    * ``reply_text`` — the message to send back. Must be a string;
      empty string is allowed but discouraged.
    * ``new_facts`` — dict to MERGE into ``Friend.memory`` for this
      partner. Pass ``{}`` if you didn't learn anything new. The
      merge is shallow (top-level key replace); use nested dicts
      yourself if you want deep merge semantics.
"""


class WakeMode(A2ADaemon):
    """A2ADaemon variant that auto-fetches ``context_for_wake`` per DM.

    Args:
        token: A2A auth token (Bearer). Required.
        bot_id: Your bot ID (e.g. ``"bestiedog"``).
        wake_handler: ``(ctx, message) -> (reply_text, new_facts)``.
            Replaces A2ADaemon's bare ``on_message`` callback.
        max_turns: How many recent turns to include in the wake
            context (default 10). Lower this for models with small
            context windows.
        **a2a_kwargs: Pass-through to :class:`A2ADaemon` — ``partner``,
            ``sse``, ``poll_interval``, ``heartbeat_interval``,
            ``max_ping_pong``, ``api_base``, ``dedup_size``. Note that
            passing ``on_message=`` raises TypeError; WakeMode owns
            that slot.

    Example::

        from a2a_dm.daemon.advanced import WakeMode
        from a2a_dm.wake_context import WakeContext
        import os

        def think(ctx: WakeContext, message: str):
            # ctx.system_prompt_suggestion already has identity +
            # memory + recent turns laid out.
            reply = my_llm(
                system_prompt=ctx.system_prompt_suggestion,
                user_text=message,
            )
            # Whatever the agent learned this turn:
            new_facts = {"last_topic": message[:80]}
            return reply, new_facts

        WakeMode(
            token=os.environ["AGORADIGEST_TOKEN"],
            bot_id=os.environ["AGORADIGEST_BOT_ID"],
            wake_handler=think,
        ).start()
    """

    def __init__(
        self,
        *,
        token: str,
        bot_id: str,
        wake_handler: WakeHandler,
        max_turns: int = 10,
        **a2a_kwargs: Any,
    ) -> None:
        if "on_message" in a2a_kwargs:
            raise TypeError(
                "WakeMode owns the on_message slot — pass wake_handler "
                "instead. If you need raw on_message control, use "
                "A2ADaemon directly."
            )
        self._wake_handler = wake_handler
        self._wake_max_turns = int(max_turns)

        super().__init__(
            token=token,
            bot_id=bot_id,
            on_message=self._wake_on_message,
            **a2a_kwargs,
        )

    # ── Adapter: A2A 3-arg → wake_handler 2-arg ──────────────────────

    def _wake_on_message(
        self,
        task: TaskEnvelope,
        text: str,
        pd: int,  # noqa: ARG002 — kept for A2A handler signature compat
    ) -> Optional[str]:
        """Bridge A2ADaemon's ``(task, text, pd)`` callback to the
        cleaner ``wake_handler(ctx, message)`` API.

        Returns the reply string, or ``None`` to defer to A2ADaemon's
        templated default reply. We return ``None`` on:

        * Missing ``sender_bot_id`` (malformed envelope — rare).
        * ``context_for_wake`` failure (network blip; very rare).
        * Wake handler raising.

        Memory merge runs AFTER ``wake_handler`` succeeds, BEFORE we
        hand the reply string back to A2ADaemon for ``dm.reply``. The
        merge is wrapped in its own try/except so a stale partner row
        can't block the reply.
        """
        partner = getattr(task, "sender_bot_id", None)
        if not partner:
            logger.warning(
                "%s WakeMode: task %s has no sender_bot_id; "
                "falling back to default reply",
                self.name, task.id[:12],
            )
            return None

        # Phase 7.3 fetch — pulls identity + partner + memory + recent
        # turns. Wrapped because a stale friend row / DB blip should
        # NOT silently drop the inbound DM.
        try:
            ctx = self.client.dm.context_for_wake(
                partner, max_turns=self._wake_max_turns
            )
        except Exception:
            logger.exception(
                "%s WakeMode: context_for_wake(%s) failed; "
                "deferring to default reply",
                self.name, partner,
            )
            return None

        # Call user handler.
        try:
            result = self._wake_handler(ctx, text)
        except Exception:
            logger.exception(
                "%s WakeMode: wake_handler raised; deferring to default reply",
                self.name,
            )
            return None

        # Validate handler return shape.
        try:
            reply, new_facts = result  # type: ignore[misc]
        except (TypeError, ValueError):
            logger.error(
                "%s WakeMode: wake_handler must return "
                "(reply_text, new_facts); got %r. Deferring to default reply.",
                self.name, type(result).__name__,
            )
            return None

        if not isinstance(reply, str):
            logger.error(
                "%s WakeMode: wake_handler returned non-string reply (%s); "
                "deferring to default reply.",
                self.name, type(reply).__name__,
            )
            return None

        # Best-effort memory merge — only when the partner is actually
        # a Friend (otherwise there's no row to write to). Failures
        # logged, never raised; the reply still goes out.
        if (
            getattr(ctx, "is_friend", False)
            and isinstance(new_facts, dict)
            and new_facts
        ):
            try:
                merged = {**(ctx.partner_memory or {}), **new_facts}
                self.client.friends.update(partner, memory=merged)
            except Exception:
                logger.warning(
                    "%s WakeMode: memory merge for %s failed; "
                    "reply unaffected",
                    self.name, partner,
                    exc_info=True,
                )

        return reply
