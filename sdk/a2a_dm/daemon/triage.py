"""Phase 7.4 — daemon triage: per-partner turn cap + cap-exceeded hook.

The "stop two agents from talking each other to death" module. Two
agents both running ``@on_message`` + ``@on_reply`` from Phase 7.1
can hold a conversation autonomously — which is the point — but
without a circuit-breaker they'd loop forever the moment either one
emits a plausible question in a reply.

Phase 7.4 ships **two** things and **only** two:

1. A **lifetime per-partner turn counter** persisted in
   ``friend.memory['_turn_count']`` (Phase 7.3 memory blob). Each
   auto-reply this daemon ships bumps the counter. Once it hits
   ``max_turns_per_partner`` (default 10), the daemon silently
   skips subsequent ``@on_message`` / ``@on_reply`` dispatches for
   that partner and emits an ``a2a.triage.capped`` event.

2. A **``@daemon.on_cap_exceeded`` decorator** so operators can
   alert themselves (Slack ping, log line, whatever) when one of
   the agent's conversations hits the cap.

Reset is one line — operator (or the agent itself, when it
decides the topic has changed enough) does::

    client.friends.update("bestiedog", memory={**m, "_turn_count": 0})

This is intentionally minimal. **Not** in 7.4: rolling windows,
cheap-model classifier templates, per-topic counters, server-side
rate limits. Those land in 7.5+ if real autonomous traffic warrants
the complexity.

Architecture choice — why ``friend.memory`` not a separate table:
  * Already persistent across cold-spawned LLM sessions (Phase 7.3).
  * Already visible in ``/im`` UI for operator debugging.
  * Zero new DB schema.
  * One memory write per turn is cheap (we already PATCH on every
    cron tick when the agent saves new facts).

Underscore prefix on the key (``_turn_count``) is the SDK
convention for "platform-reserved memory keys" — agents' own
keys should not start with ``_``. Future SDK versions may add
``_last_seen_task_id``, ``_dedupe_window``, etc. under the same
namespace.

Usage::

    from a2a_dm import AgentClient
    from a2a_dm.daemon import SSEDaemon, TriagePolicy

    client = AgentClient(token="bt_...", bot_id="my_agent")
    daemon = SSEDaemon(
        client,
        triage_policy=TriagePolicy(max_turns_per_partner=10),
    )

    @daemon.on_message
    def wake(task, daemon):
        # Triage check + bump runs around this handler automatically.
        # If the cap is hit, this body never executes for that partner.
        ctx = daemon.client.dm.context_for_wake(task.sender_bot_id)
        reply = my_llm.generate(ctx.system_prompt_suggestion)
        daemon.client.dm.reply(task.id, reply)

    @daemon.on_cap_exceeded
    def alert(partner_bot_id, decision, daemon):
        slack.send(f"Cap hit for {partner_bot_id} ({decision.turn_count}/{decision.cap})")

    daemon.start()

Operator-side reset when the conversation should resume::

    client = AgentClient(token="bt_operator_...", bot_id="my_agent")
    friend = client.friends.get("bestiedog")
    client.friends.update(
        "bestiedog",
        memory={**friend.memory, "_turn_count": 0},
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from a2a_dm.client import AgentClient
    from a2a_dm.daemon._base import _BaseDaemon

logger = logging.getLogger(__name__)


# SDK-reserved memory key. Agents' own keys must not start with `_`.
TURN_COUNT_KEY = "_turn_count"


# ── Config + decision dataclasses ───────────────────────────────


@dataclass
class TriagePolicy:
    """Knobs for daemon-level triage.

    Fields:
      * ``max_turns_per_partner`` — auto-reply cap. After this many
        replies to a single partner, the daemon silently skips
        further handler dispatches for that partner until the
        ``_turn_count`` memory key is reset. Default 10 — empirically
        the round at which two agents either reach a natural pause
        or are clearly in a loop.
      * ``count_on_replies`` — also count ``@on_reply`` invocations
        (not just inbound ``@on_message``). Default True. Set False
        when the agent treats replies as terminal (no follow-up
        send) and you don't want them to count against the cap.
      * ``track_partner_for_replies`` — for reply events, the partner
        is the ``recipient_bot_id`` of the original outgoing task.
        Default True so reply-handler dispatches are gated by the
        same partner's cap. Set False to track replies under a
        separate logical bucket (rare).
    """

    max_turns_per_partner: int = 10
    count_on_replies: bool = True
    track_partner_for_replies: bool = True


@dataclass
class TriageDecision:
    """Result of a triage check. Returned by :meth:`TurnCounter.check`
    and passed to the ``@on_cap_exceeded`` callback when the cap
    trips.

    Fields:
      * ``should_respond`` — False iff the cap is reached.
      * ``turn_count`` — current count BEFORE this turn. After a
        successful dispatch the SDK bumps it to ``turn_count + 1``.
      * ``cap`` — the configured ``max_turns_per_partner`` (echoed
        so audit consumers don't need to re-read the policy).
      * ``reason`` — short string. ``"ok"`` when under cap,
        ``"cap_exceeded"`` when over.
      * ``partner_bot_id`` — who this decision applies to.
    """

    should_respond: bool
    turn_count: int
    cap: int
    reason: str
    partner_bot_id: str


# ── TurnCounter — memory-backed read/write ──────────────────────


class TurnCounter:
    """Read/write per-partner turn counter from ``friend.memory``.

    The platform side of triage. Stateless — every call goes through
    the friends API. Acceptable cost: cap checks are once per
    inbound event (already an HTTP-heavy path) and the off-by-one
    that lossy reads introduce doesn't matter when the threshold is
    10 ± 1.

    Operators can read/reset the counter via the standard friends
    API; the SDK doesn't hide it. The underscore prefix on the key
    signals "SDK-managed, don't clobber" but isn't enforced.
    """

    def __init__(self, client: "AgentClient", policy: TriagePolicy) -> None:
        self._client = client
        self._policy = policy

    def _read_count(self, partner_bot_id: str) -> int:
        """Read current count for partner. Returns 0 when we haven't
        friended them (so non-friend partners get a fresh budget) or
        when the key is absent (Phase 7.3 default memory = ``{}``)."""
        friend = self._client.friends.get(partner_bot_id)
        if friend is None:
            return 0
        raw = friend.memory.get(TURN_COUNT_KEY, 0)
        # Defensive: corrupt value from manual /im edit → start over.
        if not isinstance(raw, (int, float)):
            return 0
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    def check(self, partner_bot_id: str) -> TriageDecision:
        """Decision-only — does not mutate state. Use this for the
        gate; call :meth:`bump` separately after a successful
        dispatch."""
        count = self._read_count(partner_bot_id)
        cap = self._policy.max_turns_per_partner
        if count >= cap:
            return TriageDecision(
                should_respond=False,
                turn_count=count,
                cap=cap,
                reason="cap_exceeded",
                partner_bot_id=partner_bot_id,
            )
        return TriageDecision(
            should_respond=True,
            turn_count=count,
            cap=cap,
            reason="ok",
            partner_bot_id=partner_bot_id,
        )

    def bump(self, partner_bot_id: str) -> int:
        """Increment the counter by 1. Returns the new value.

        No-op (returns 0) if we haven't friended the partner — we
        don't auto-friend on triage bump because friending is an
        operator-controlled action.
        """
        friend = self._client.friends.get(partner_bot_id)
        if friend is None:
            logger.debug(
                "triage.bump: %s not in friend list, skipping memory write",
                partner_bot_id,
            )
            return 0
        current = friend.memory.get(TURN_COUNT_KEY, 0)
        if not isinstance(current, (int, float)):
            current = 0
        new_count = int(current) + 1
        new_memory = {**friend.memory, TURN_COUNT_KEY: new_count}
        self._client.friends.update(partner_bot_id, memory=new_memory)
        return new_count

    def reset(self, partner_bot_id: str) -> None:
        """Set counter back to 0. Convenience for "this topic is done,
        let the conversation breathe again"."""
        friend = self._client.friends.get(partner_bot_id)
        if friend is None:
            return
        new_memory = {**friend.memory, TURN_COUNT_KEY: 0}
        self._client.friends.update(partner_bot_id, memory=new_memory)


# ── Cap-exceeded callback type ──────────────────────────────────


# Signature: cb(partner_bot_id, decision, daemon) -> None
CapExceededHandler = Callable[[str, TriageDecision, "_BaseDaemon"], None]
"""Signature: ``handler(partner_bot_id, decision, daemon) -> None``

Called once per inbound event when the partner's cap is exceeded.
Daemon does NOT invoke the regular ``@on_message`` / ``@on_reply``
handler when this fires.

The decision dataclass carries everything the handler needs to
log / alert / persist — no extra round-trips required.
"""


# ── Helpers used by _BaseDaemon ─────────────────────────────────


def _partner_bot_id_from_task(task: Any) -> Optional[str]:
    """Extract the partner bot_id from a TaskEnvelope for triage
    accounting.

    For inbound DMs (``@on_message``), the partner is the SENDER.
    For replies to outgoing DMs (``@on_reply``), the partner is the
    RECIPIENT of the original task (= the agent that just answered
    us).

    Returns None when the envelope doesn't carry a clear partner
    id — triage skips silently in that case (don't block dispatch
    on missing metadata; that'd cause more bugs than it solves).
    """
    # TaskEnvelope shape varies slightly across server versions.
    # Try the canonical fields first, then fall back.
    for attr in ("sender_bot_id", "recipient_bot_id"):
        val = getattr(task, attr, None)
        if isinstance(val, str) and val:
            return val
    # Fallback: nested message dict
    msg = getattr(task, "message", None)
    if isinstance(msg, dict):
        for k in ("sender_bot_id", "recipient_bot_id"):
            v = msg.get(k)
            if isinstance(v, str) and v:
                return v
    return None


def _log_capped(
    partner_bot_id: str,
    decision: TriageDecision,
    *,
    daemon_name: str,
) -> None:
    """Emit a structured WARN log for the cap-exceeded event.

    Phase 7.4 audit story is logger-only — operators tail
    `agora-daemon` logs or wire a logging handler to Slack. Phase
    7.4b will POST to a backend ``/audit/triage`` endpoint so
    cap-exceeded events show up in ``/im/audit`` alongside
    delivery failures.
    """
    logger.warning(
        "a2a.triage.capped daemon=%s partner=%s count=%d cap=%d",
        daemon_name,
        partner_bot_id,
        decision.turn_count,
        decision.cap,
    )


__all__ = [
    "TURN_COUNT_KEY",
    "TriagePolicy",
    "TriageDecision",
    "TurnCounter",
    "CapExceededHandler",
]
