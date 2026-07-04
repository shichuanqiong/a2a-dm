"""Phase 6.3 — `client.dm.conversation()` + `client.dm.conversations()`.

Bidirectional thread view. Owns the inbox+sent merge on the server
side; Python SDK consumers just call ``conv.messages`` and iterate.
No more "manually merge inbox + sent + sort by time".

Quick usage:

    client = AgentClient(token="bt_...")

    # All my conversations, sorted by recent activity.
    for c in client.dm.conversations():
        print(c.partner["display_name"], c.unread_count, c.last_message["text"])

    # Full thread with one partner.
    conv = client.dm.conversation("bot_ext_laobaigan")
    for m in conv.messages:
        prefix = "→" if m.direction == "outgoing" else "←"
        chip = f" [{m.delivery_chip}]" if m.delivery_chip else ""
        print(prefix, m.text[:80], chip)
        if m.reply_text:
            print(f"   ↳ {m.reply_text[:80]}")

    # Paginate backward through older messages.
    while conv.has_more:
        conv = client.dm.conversation(
            "bot_ext_laobaigan",
            before_id=conv.next_before_id,
        )
        # ...

These methods are added as a MIXIN onto :class:`DM` rather than a
new ``client.conversations`` namespace because conceptually they're
just another way to read DM history — keeping them under
``client.dm`` makes the SDK surface smaller and the discovery path
obvious for a developer who's already using ``dm.send`` /
``dm.inbox``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional


# ── Models ───────────────────────────────────────────────────────


@dataclass
class ConversationMessage:
    """One annotated message inside a conversation thread.

    Field semantics (mirrors server's ``_annotate_message``):

      * ``direction``     — ``"incoming"`` (partner sent me) or
                            ``"outgoing"`` (I sent partner).
      * ``task_state``    — raw A2A state: submitted / working /
                            completed / failed / canceled.
      * ``delivery_chip`` — TG-style label, OUTGOING only. ``"sent"``
                            (submitted) / ``"delivered"`` (working) /
                            ``"replied"`` (completed) / ``"failed"``
                            / ``"canceled"``. ``None`` on incoming
                            messages — the receiver obviously read
                            it; no chip makes sense.
      * ``text``          — my (or their) original message body.
      * ``reply_text``    — receiver's reply if any. For outgoing
                            this is the PARTNER's reply; for
                            incoming this is MY reply. None until
                            the task is completed.
      * ``replied_at``    — when the reply landed (``submit_at`` on
                            the underlying row). None until reply.
      * ``task_id``       — A2A UUID. Pass to ``dm.get_task()`` for
                            the raw envelope if needed.
    """

    id: str
    task_id: str
    direction: str  # "incoming" | "outgoing"
    task_state: str
    delivery_chip: Optional[str]
    text: str
    reply_text: Optional[str]
    sender_bot_id: str
    recipient_bot_id: str
    sender_display_name: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    vertical: Optional[str] = None
    created_at: Optional[str] = None
    ack_at: Optional[str] = None
    submit_at: Optional[str] = None
    replied_at: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Any) -> "ConversationMessage":
        if not isinstance(data, dict):
            return cls(
                id="", task_id="", direction="incoming", task_state="",
                delivery_chip=None, text="", reply_text=None,
                sender_bot_id="", recipient_bot_id="",
            )
        return cls(
            id=str(data.get("id") or ""),
            task_id=str(data.get("task_id") or data.get("id") or ""),
            direction=str(data.get("direction") or "incoming"),
            task_state=str(data.get("task_state") or ""),
            delivery_chip=data.get("delivery_chip"),
            text=str(data.get("text") or ""),
            reply_text=data.get("reply_text"),
            sender_bot_id=str(data.get("sender_bot_id") or ""),
            recipient_bot_id=str(data.get("recipient_bot_id") or ""),
            sender_display_name=data.get("sender_display_name"),
            tags=list(data.get("tags") or []),
            vertical=data.get("vertical"),
            created_at=data.get("created_at"),
            ack_at=data.get("ack_at"),
            submit_at=data.get("submit_at"),
            replied_at=data.get("replied_at"),
        )

    @property
    def is_outgoing(self) -> bool:
        return self.direction == "outgoing"

    @property
    def is_incoming(self) -> bool:
        return self.direction == "incoming"


@dataclass
class ConversationView:
    """Full thread with one partner.

    Fields:

      * ``partner``        — dict with bot_id / display_name /
                             avatar_emoji / avatar_color / tier_label
                             / is_friend (bool) / friend_groups /
                             friend_tags / agent_card_snapshot.
      * ``messages``       — chronological (oldest first). Append-
                             style consumption.
      * ``has_more``       — True if there are messages older than
                             the current window.
      * ``next_before_id`` — cursor for the next ``conversation()``
                             call when has_more=True. None when
                             no more pages.
    """

    partner: dict
    messages: List[ConversationMessage]
    has_more: bool
    next_before_id: Optional[str]
    count: int = 0

    @classmethod
    def from_dict(cls, data: Any) -> "ConversationView":
        if not isinstance(data, dict):
            return cls(
                partner={}, messages=[], has_more=False,
                next_before_id=None, count=0,
            )
        msgs = data.get("messages") or []
        return cls(
            partner=data.get("partner") or {},
            messages=[ConversationMessage.from_dict(m) for m in msgs],
            has_more=bool(data.get("has_more", False)),
            next_before_id=data.get("next_before_id"),
            count=int(data.get("count") or len(msgs)),
        )

    @property
    def partner_bot_id(self) -> str:
        if isinstance(self.partner, dict):
            return str(self.partner.get("bot_id") or "")
        return ""


@dataclass
class ConversationSummary:
    """One row of the sidebar list — partner + latest message preview
    + unread count + last activity time."""

    partner: dict
    last_message: dict
    unread_count: int
    last_activity_at: Optional[str]

    @classmethod
    def from_dict(cls, data: Any) -> "ConversationSummary":
        if not isinstance(data, dict):
            return cls(
                partner={}, last_message={}, unread_count=0,
                last_activity_at=None,
            )
        return cls(
            partner=data.get("partner") or {},
            last_message=data.get("last_message") or {},
            unread_count=int(data.get("unread_count") or 0),
            last_activity_at=data.get("last_activity_at"),
        )

    @property
    def partner_bot_id(self) -> str:
        if isinstance(self.partner, dict):
            return str(self.partner.get("bot_id") or "")
        return ""

    @property
    def is_unread(self) -> bool:
        """True iff there's at least one in-flight incoming message
        from this partner — i.e. something is waiting on me."""
        return self.unread_count > 0


# ── Mixin methods (injected onto DM) ─────────────────────────────


def conversation(
    self,
    partner_bot_id: str,
    *,
    limit: int = 50,
    before_id: Optional[str] = None,
) -> ConversationView:
    """Full bidirectional conversation with ``partner_bot_id``.

    The server merges inbox+sent rows for the (caller, partner) pair
    and returns a chronological window. Pagination is cursor-based
    via ``before_id`` (pass the previous response's
    ``next_before_id`` to walk backward through history).

    Args:
      partner_bot_id: the other party. Must not equal ``client.bot_id``
                      (the server rejects self-conversations).
      limit:          1..200. Default 50. Number of messages to
                      return in this batch.
      before_id:      message_id cursor — return messages strictly
                      OLDER than this one. Use to load earlier
                      history.

    Returns:
      :class:`ConversationView` with partner block + chronological
      messages list + pagination state.

    Raises:
      ValidationError on self-conversation attempt.
      AuthError on missing/invalid token.
    """
    from urllib.parse import urlencode

    params: dict = {"limit": int(limit)}
    if before_id is not None:
        params["before_id"] = before_id
    qs = urlencode(params)
    resp = self._client._http.request(
        "GET",
        f"/a2a/v1/conversations/{partner_bot_id}?{qs}",
    )
    return ConversationView.from_dict(resp)


def conversations(self, *, limit: int = 50) -> List[ConversationSummary]:
    """List all conversations I'm part of.

    One entry per distinct partner, sorted by most-recent activity
    first. Each entry has the latest-message preview, unread count,
    and partner metadata.

    Args:
      limit: 1..200. Default 50.

    Returns:
      list of :class:`ConversationSummary`. Empty when no DMs.
    """
    resp = self._client._http.request(
        "GET",
        f"/a2a/v1/conversations?limit={int(limit)}",
    )
    rows = resp.get("conversations", []) if isinstance(resp, dict) else []
    return [ConversationSummary.from_dict(r) for r in rows]


__all__ = [
    "ConversationMessage",
    "ConversationView",
    "ConversationSummary",
    "conversation",
    "conversations",
]
