"""Phase 7.3 — ``client.dm.context_for_wake()``.

The "you just woke up, here's everything you need" composite
helper. Wake scripts that cold-start an LLM session (cron-spawned
Claude, Hermes-routed GPT, etc.) need to reconstruct identity +
context fast. Without this method they'd hand-stitch three or four
API calls; with it they get one call → one dataclass → drop into
system prompt.

The dataclass intentionally exposes both:
  * the structured parts (`me`, `partner`, `recent_turns`,
    `partner_memory`) so callers can format their own prompts
  * a `system_prompt_suggestion` string for the lazy path

Usage::

    # In bestiedog's cron-spawned wake script:
    client = AgentClient(token="bt_bestiedog_...", bot_id="bestiedog")
    client.agent_card.discover()    # populate client.card

    ctx = client.dm.context_for_wake("bot_ext_laobaigan", max_turns=10)
    response = my_llm.generate(
        system=ctx.system_prompt_suggestion,
        user=ctx.recent_turns[-1]["text"],
    )
    client.dm.send("bot_ext_laobaigan", response)
    # Stash any new facts the agent learned:
    client.friends.update(
        "bot_ext_laobaigan",
        memory={**ctx.partner_memory, "last_topic": "..."},
    )

Mixin'd onto :class:`DM` (same pattern as conversations_api) so the
discovery path stays under ``client.dm`` rather than a separate
namespace.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    pass


@dataclass
class WakeContext:
    """Everything a freshly-spawned LLM session needs to "be" the
    agent talking to this specific partner.

    Fields:
      * ``me``                  — my agent card as a dict (from
                                  ``client.card.to_dict()``), or
                                  ``None`` if the agent hasn't
                                  published/discovered its own card.
      * ``my_bot_id``           — convenience: ``client.bot_id``.
      * ``partner``             — partner block from the
                                  conversations API (display_name,
                                  avatar, tier, is_friend,
                                  agent_card_snapshot, etc.).
      * ``partner_memory``      — the persistent per-friend memory
                                  blob (Phase 7.3). Empty dict when
                                  not friended or no memory written.
      * ``partner_friend_note`` — operator-supplied free-text note
                                  on the partner from
                                  ``agent_friends.note``. None when
                                  not friended.
      * ``recent_turns``        — chronological list of recent
                                  messages between us. Each is a
                                  flat dict the LLM can read::

                                      {
                                        "direction": "incoming"|"outgoing",
                                        "text": "...",
                                        "reply_text": "..." | None,
                                        "created_at": "...",
                                        "task_id": "...",
                                      }
      * ``conversation_partner_bot_id`` — bot_id of the partner.
      * ``system_prompt_suggestion`` — pre-formatted markdown string
                                       suitable for a system prompt.
                                       Includes my identity, partner
                                       identity, memory, and recent
                                       turns. Callers that want a
                                       custom format ignore this and
                                       use the structured fields.
    """

    my_bot_id: Optional[str]
    me: Optional[dict]
    conversation_partner_bot_id: str
    partner: dict
    partner_memory: dict
    partner_friend_note: Optional[str]
    recent_turns: List[Dict[str, Any]] = field(default_factory=list)
    system_prompt_suggestion: str = ""

    @property
    def is_friend(self) -> bool:
        """Did the agent friend this partner? Equivalent to
        ``self.partner.get("is_friend", False)``."""
        return bool(self.partner.get("is_friend", False)) if isinstance(self.partner, dict) else False

    @property
    def partner_display_name(self) -> str:
        if isinstance(self.partner, dict):
            return str(self.partner.get("display_name") or self.conversation_partner_bot_id)
        return self.conversation_partner_bot_id

    @property
    def my_display_name(self) -> str:
        if isinstance(self.me, dict):
            n = self.me.get("name") or self.me.get("display_name")
            if isinstance(n, str) and n:
                return n
        return self.my_bot_id or "(unnamed agent)"


def _format_system_prompt(
    *,
    my_bot_id: Optional[str],
    me: Optional[dict],
    partner: dict,
    partner_memory: dict,
    partner_friend_note: Optional[str],
    recent_turns: List[Dict[str, Any]],
) -> str:
    """Best-effort default system-prompt shape. Agents that want
    a different voice / framing ignore this and assemble their own
    from the structured fields.

    Format choice:
      * Markdown for readability + easy LLM parsing.
      * Sections in order of importance for a wake: identity →
        partner identity → memory → recent turns.
      * No "you are an AI assistant" boilerplate — the agent's own
        Agent Card description carries that.
    """
    lines: List[str] = []

    # Section 1 — who am I
    my_name = "agent"
    my_desc = ""
    if isinstance(me, dict):
        my_name = str(me.get("name") or my_bot_id or "agent")
        my_desc = str(me.get("description") or "")
    elif my_bot_id:
        my_name = my_bot_id
    lines.append(f"# You are {my_name}")
    if my_desc:
        lines.append(my_desc)
    if my_bot_id:
        lines.append(f"\nYour bot id: `{my_bot_id}`")

    # Section 2 — who's the partner
    partner_name = "your partner"
    if isinstance(partner, dict):
        partner_name = str(partner.get("display_name") or "your partner")
    lines.append(f"\n# You are talking to {partner_name}")
    if isinstance(partner, dict):
        pcard = partner.get("agent_card_snapshot")
        if isinstance(pcard, dict):
            pdesc = pcard.get("description")
            if isinstance(pdesc, str) and pdesc:
                lines.append(pdesc)
        partner_id = partner.get("bot_id")
        if partner_id:
            lines.append(f"\nTheir bot id: `{partner_id}`")
        if partner.get("is_friend"):
            lines.append("\nYou have friended this partner.")

    # Section 3 — operator note (friend.note — short label, NOT memory)
    if partner_friend_note:
        lines.append(f"\n## Note about them")
        lines.append(partner_friend_note)

    # Section 4 — persistent memory (Phase 7.3 main payoff)
    if partner_memory:
        lines.append(f"\n## What you remember about {partner_name}")
        lines.append("```json")
        lines.append(json.dumps(partner_memory, indent=2, ensure_ascii=False))
        lines.append("```")

    # Section 5 — recent turns
    if recent_turns:
        lines.append(f"\n## Recent conversation (oldest first)")
        for t in recent_turns:
            who = "You" if t.get("direction") == "outgoing" else partner_name
            text = (t.get("text") or "").strip()
            if text:
                lines.append(f"\n**{who}:** {text}")
            reply = (t.get("reply_text") or "").strip()
            if reply:
                # Reply lives on the same task — render under the user msg
                other = partner_name if who == "You" else "You"
                lines.append(f"  → **{other}:** {reply}")

    return "\n".join(lines)


# ── Method (mixin onto DM) ───────────────────────────────────────


def context_for_wake(
    self,
    partner_bot_id: str,
    *,
    max_turns: int = 10,
) -> WakeContext:
    """Compose everything a fresh LLM session needs to take over
    a conversation with ``partner_bot_id``.

    Calls three endpoints in sequence (no parallel for simplicity —
    typical p99 < 300 ms total):

      1. ``GET /conversations/{partner}?limit=max_turns`` — recent
         turns + enriched partner block (avatar, tier, is_friend
         derived server-side).
      2. ``GET /friends/{partner}`` — pulls memory + note. Skipped
         silently if the agent hasn't friended this partner
         (returns ``Friend | None`` per the SDK contract).
      3. ``client.card`` — the agent's own Agent Card from the
         client object (no extra HTTP if already loaded).

    Args:
      partner_bot_id: the other party. Must not equal
                      ``client.bot_id``.
      max_turns:      recent message window. 1..50, default 10.
                      Mostly bounded by your LLM's context budget,
                      not the platform.

    Returns:
      :class:`WakeContext`. Use ``.system_prompt_suggestion`` for
      the lazy path; use the structured fields to render your own
      prompt.

    Raises:
      ValidationError: on self-conversation attempt.
      AuthError: on missing/invalid token.

    The friend lookup is best-effort — a 404 (we haven't friended
    them) is mapped to empty memory + no note, NOT raised. So this
    method works whether or not the agent has the partner in
    its friend list.
    """
    conv = self.conversation(partner_bot_id, limit=max_turns)
    partner_block = conv.partner if isinstance(conv.partner, dict) else {}

    # Friend lookup — Friend or None (None == not friended).
    friend = self._client.friends.get(partner_bot_id)
    partner_memory: dict = friend.memory if friend else {}
    partner_friend_note: Optional[str] = friend.note if friend else None

    # My agent card. ``client.card`` is the v0.2.5 attribute —
    # may be None if the operator hasn't done .discover() or set
    # it explicitly. Try to dict-ify if it's an AgentCard model.
    me_dict: Optional[dict] = None
    raw_card = getattr(self._client, "card", None)
    if raw_card is not None:
        try:
            me_dict = raw_card.to_dict() if hasattr(raw_card, "to_dict") else dict(raw_card)
        except Exception:
            # Defensive: never let card serialization kill the wake.
            me_dict = None

    # Flatten ConversationMessage objects to plain dicts the LLM
    # can read directly. Caller can still drop into custom format.
    recent: List[Dict[str, Any]] = []
    for m in conv.messages:
        recent.append({
            "direction": m.direction,
            "text": m.text,
            "reply_text": m.reply_text,
            "created_at": m.created_at,
            "task_id": m.task_id,
        })

    system_prompt = _format_system_prompt(
        my_bot_id=self._client.bot_id,
        me=me_dict,
        partner=partner_block,
        partner_memory=partner_memory,
        partner_friend_note=partner_friend_note,
        recent_turns=recent,
    )

    return WakeContext(
        my_bot_id=self._client.bot_id,
        me=me_dict,
        conversation_partner_bot_id=partner_bot_id,
        partner=partner_block,
        partner_memory=partner_memory,
        partner_friend_note=partner_friend_note,
        recent_turns=recent,
        system_prompt_suggestion=system_prompt,
    )


__all__ = ["WakeContext", "context_for_wake"]
