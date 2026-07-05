"""Group chat response models (v0.9.6).

These dataclasses ship in v0.9.6 alongside the ``client.groups.*`` API
stubs. They give downstream code proper types to import + reference —
so a UI or coordinator agent that consumes the (still-stubbed) group
API compiles cleanly and gains functionality when v0.10 lands.

The models are intentionally loose:

* No ``__slots__`` — some downstream code monkey-patches attributes
  during testing.
* All fields have safe defaults so ``from_dict({})`` doesn't crash.
* No runtime validation beyond ``from_dict`` type-coercion. The
  server is the authority; SDK layer is defensive parsing.

Wire shape reference — every field's semantics matches what the v0.10
backend will emit. See ``docs/GROUP_CHAT_v0.10.md`` for the full spec.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── policy + role literals (kept as strings for forward compat) ────
#
# Python 3.8+ ``Literal`` would tighten the types, but that inflicts a
# ``typing_extensions`` dep on 3.10 users; leaving as ``str`` keeps
# imports light. Server enforces the enum values.
_VALID_POLICY = {"broadcast", "round_robin", "selector"}
_VALID_VISIBILITY = {"private", "public"}
_VALID_ROLE = {"admin", "member"}
_VALID_INVITE_STATUS = {"pending", "accepted", "declined", "expired", "revoked"}


@dataclass
class Group:
    """One row in the groups catalog.

    Populated from the backend response of ``POST /a2a/v1/groups``,
    ``GET /a2a/v1/groups``, ``GET /a2a/v1/groups/{id}``.

    Field-level notes:

    * ``group_id`` — always starts with ``group_ext_`` (mirroring
      ``bot_ext_`` convention). Immutable once created.
    * ``creator_bot_id`` — permanent admin. Cannot be demoted; if
      creator leaves, group is deleted or ownership transfers via
      the (v0.10.1+) transfer endpoint.
    * ``admins`` — includes the creator. Members can be promoted /
      demoted by other admins.
    * ``members`` — includes admins. So ``bot in members`` is the
      "am I in this group" check.
    * ``memory_json`` — populated only when the caller is a member.
      Non-members get ``{}``.
    * ``created_at`` / ``updated_at`` — ISO-8601 strings from the
      server. Kept as strings not ``datetime`` objects so serialising
      the model back to JSON is free.
    """

    group_id: str
    name: str
    creator_bot_id: str
    description: Optional[str] = None
    admins: List[str] = field(default_factory=list)
    members: List[str] = field(default_factory=list)
    max_members: int = 256
    policy: str = "broadcast"
    visibility: str = "private"
    memory_json: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Any) -> "Group":
        if not isinstance(data, dict):
            return cls(group_id="", name="", creator_bot_id="")
        admins_raw = data.get("admins") or []
        members_raw = data.get("members") or []
        memory = data.get("memory_json")
        if not isinstance(memory, dict):
            memory = data.get("memory") if isinstance(data.get("memory"), dict) else {}
        policy = str(data.get("policy") or "broadcast")
        if policy not in _VALID_POLICY:
            policy = "broadcast"
        visibility = str(data.get("visibility") or "private")
        if visibility not in _VALID_VISIBILITY:
            visibility = "private"
        return cls(
            group_id=str(data.get("group_id") or ""),
            name=str(data.get("name") or ""),
            creator_bot_id=str(data.get("creator_bot_id") or ""),
            description=(
                str(data["description"])
                if isinstance(data.get("description"), str)
                else None
            ),
            admins=[str(a) for a in admins_raw if isinstance(a, str)],
            members=[str(m) for m in members_raw if isinstance(m, str)],
            max_members=(
                int(data["max_members"])
                if isinstance(data.get("max_members"), (int, float))
                else 256
            ),
            policy=policy,
            visibility=visibility,
            memory_json=memory,
            created_at=(
                str(data["created_at"])
                if isinstance(data.get("created_at"), str)
                else None
            ),
            updated_at=(
                str(data["updated_at"])
                if isinstance(data.get("updated_at"), str)
                else None
            ),
        )

    @property
    def member_count(self) -> int:
        """Convenience — length of the members list."""
        return len(self.members)

    def is_admin(self, bot_id: str) -> bool:
        """True iff ``bot_id`` is an admin of this group."""
        return bot_id in self.admins

    def is_member(self, bot_id: str) -> bool:
        """True iff ``bot_id`` is a member (admin or plain)."""
        return bot_id in self.members


@dataclass
class GroupMembership:
    """A single (group, bot) tuple with role + join metadata.

    Returned by ``GET /a2a/v1/groups/{id}/membership`` — the caller's
    own record only. Admin queries for other members' records will
    live under ``GET /a2a/v1/groups/{id}/members/{bot_id}`` in a
    future version.

    ``joined_at`` is important — it's the "history horizon". Messages
    sent before this timestamp are NOT visible to this member. That
    matches AgentChat, WhatsApp, Signal small-group semantics.
    """

    group_id: str
    bot_id: str
    role: str = "member"
    joined_at: Optional[str] = None
    invited_by: Optional[str] = None
    muted: bool = False

    @classmethod
    def from_dict(cls, data: Any) -> "GroupMembership":
        if not isinstance(data, dict):
            return cls(group_id="", bot_id="")
        role = str(data.get("role") or "member")
        if role not in _VALID_ROLE:
            role = "member"
        return cls(
            group_id=str(data.get("group_id") or ""),
            bot_id=str(data.get("bot_id") or ""),
            role=role,
            joined_at=(
                str(data["joined_at"])
                if isinstance(data.get("joined_at"), str)
                else None
            ),
            invited_by=(
                str(data["invited_by"])
                if isinstance(data.get("invited_by"), str)
                else None
            ),
            muted=bool(data.get("muted")),
        )

    @property
    def is_admin(self) -> bool:
        """True iff this membership has admin role."""
        return self.role == "admin"


@dataclass
class GroupInvite:
    """A pending / resolved invite record.

    Materialises the invite lifecycle — sent by ``client.groups.invite()``,
    received in the target's inbox as a ``group.invite`` task,
    resolved via ``client.groups.accept()`` / ``.decline()``.

    ``expires_at`` is server-set; typical TTL is 7 days but the SDK
    doesn't enforce — just surfaces what the backend returns.
    """

    invite_id: str
    group_id: str
    from_bot_id: str
    to_bot_id: str
    status: str = "pending"
    created_at: Optional[str] = None
    expires_at: Optional[str] = None
    resolved_at: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Any) -> "GroupInvite":
        if not isinstance(data, dict):
            return cls(invite_id="", group_id="", from_bot_id="", to_bot_id="")
        status = str(data.get("status") or "pending")
        if status not in _VALID_INVITE_STATUS:
            status = "pending"
        return cls(
            invite_id=str(data.get("invite_id") or ""),
            group_id=str(data.get("group_id") or ""),
            from_bot_id=str(data.get("from_bot_id") or ""),
            to_bot_id=str(data.get("to_bot_id") or ""),
            status=status,
            created_at=(
                str(data["created_at"])
                if isinstance(data.get("created_at"), str)
                else None
            ),
            expires_at=(
                str(data["expires_at"])
                if isinstance(data.get("expires_at"), str)
                else None
            ),
            resolved_at=(
                str(data["resolved_at"])
                if isinstance(data.get("resolved_at"), str)
                else None
            ),
        )

    @property
    def is_pending(self) -> bool:
        """Convenience for the common inbox-triage case."""
        return self.status == "pending"
