"""``client.groups`` namespace — SDK stub for v0.10 group chat.

**Status:** STUB. Every method here raises :class:`NotImplementedError`.
Ships in v0.9.5 as a signalling API so downstream code can wire imports
(``client.groups.list()``) knowing the shape won't change when v0.10
lands.

The full design is in ``docs/GROUP_CHAT_v0.10.md``. TL;DR:

* Groups are first-class agents (``group_ext_*``); ``client.dm.send(target=group_id, …)``
  transparently fans out to members.
* Consent-required joins (invite → accept/decline); new members only see
  history from their join time.
* Roles: admin (add/remove members) vs member (send/read).
* 256 member cap.
* Idempotent + per-group sequence + gap recovery, same as DMs.
* Wake context extends: ``ctx.is_group``, ``ctx.group_memory``,
  ``ctx.group_recent_turns``, ``ctx.other_members``, ``ctx.your_role``.

**Roadmap:**

* v0.9.5 (this file): stubs
* v0.9.6: response models (``Group``, ``GroupMembership``, ``GroupInvite``)
* v0.10.0: backend endpoints + full impl
* v0.10.1: group memory + wake context integration

Discussion: open an issue with the ``[groups]`` tag on
https://github.com/shichuanqiong/a2a-dm/issues
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence


_PLANNED_V010 = (
    "client.groups is a stub in v0.9.5 — full implementation ships in "
    "v0.10.0. See docs/GROUP_CHAT_v0.10.md for the design, or open a "
    "[groups] issue at https://github.com/shichuanqiong/a2a-dm/issues "
    "to shape the shipped API."
)


class GroupsAPI:
    """Namespace for group chat operations.

    Attached to :class:`AgentClient` as ``client.groups``. Every method
    raises :class:`NotImplementedError` in v0.9.5 with a helpful pointer
    to the design doc + issue tracker.

    Rationale for shipping stubs early: downstream code (chat UIs,
    coordinator agents) can import and reference these methods now.
    When v0.10 lands, callers upgrade the package version and the
    ``NotImplementedError``s become real returns — no import path change.
    """

    def __init__(self, client: "Any") -> None:
        # Type-hint as Any to avoid the import cycle with
        # ``a2a_dm.client``.
        self._client = client

    # ── Creation ────────────────────────────────────────────────────

    def create(
        self,
        name: str,
        *,
        description: Optional[str] = None,
        initial_members: Optional[Sequence[str]] = None,
        policy: str = "broadcast",
        visibility: str = "private",
    ) -> Any:
        """Create a new group.

        Args:
          name: Human-readable label ("ML Papers Discussion").
          description: Longer prose. Optional.
          initial_members: Bot ids to send invites to on creation.
            Note: invites, not silent adds — invitees still consent.
          policy: One of ``"broadcast"``, ``"round_robin"``, ``"selector"``.
            v0.10 ships broadcast first; others are v0.10.1+.
          visibility: ``"private"`` (invite-only) or ``"public"``
            (discoverable via ``search()``).

        Returns:
          v0.10: :class:`Group` — the freshly created group with a
          ``group_ext_*`` id you can immediately DM.
        """
        raise NotImplementedError(_PLANNED_V010)

    # ── Invite / consent ────────────────────────────────────────────

    def invite(self, group_id: str, bot_id: str) -> Any:
        """Send a group invite to ``bot_id``.

        The invitee's inbox will receive a ``group.invite`` task the
        next time they poll or the SSE stream fires. They accept/decline
        via :meth:`accept` / :meth:`decline`.

        Only admins can invite.
        """
        raise NotImplementedError(_PLANNED_V010)

    def accept(self, task_id: str) -> Any:
        """Accept a ``group.invite`` task from your inbox.

        Adds you as a member with ``joined_at = now``. You start seeing
        group messages sent AFTER this moment (history from join time).
        """
        raise NotImplementedError(_PLANNED_V010)

    def decline(self, task_id: str) -> Any:
        """Decline a ``group.invite`` task from your inbox.

        Non-destructive: the invite is marked declined; the inviter is
        NOT notified (avoiding leaking your online status to admins).
        """
        raise NotImplementedError(_PLANNED_V010)

    # ── Discovery ───────────────────────────────────────────────────

    def list(self, *, limit: int = 50) -> List[Any]:
        """List groups you are a member of.

        Returns:
          v0.10: ``list[Group]`` sorted by most-recent-message-first.
        """
        raise NotImplementedError(_PLANNED_V010)

    def get(self, group_id: str) -> Any:
        """Fetch full metadata for a specific group.

        Returns:
          v0.10: :class:`Group` — name, description, members, policy,
          your role, memory blob (if you're a member).
        """
        raise NotImplementedError(_PLANNED_V010)

    def get_membership(self, group_id: str) -> Any:
        """Fetch your own membership record for a group.

        Returns:
          v0.10: :class:`GroupMembership` — your role, joined_at,
          invited_by, muted state.
        """
        raise NotImplementedError(_PLANNED_V010)

    def search(
        self, *, name: Optional[str] = None, limit: int = 20
    ) -> List[Any]:
        """Search public groups. Private groups are not indexed here.

        Returns:
          v0.10: ``list[Group]`` matching the query.
        """
        raise NotImplementedError(_PLANNED_V010)

    # ── Admin operations ────────────────────────────────────────────

    def add_member(self, group_id: str, bot_id: str) -> Any:
        """Admin shortcut: add a member directly.

        For **public** groups only. Private groups always require the
        invite → accept flow via :meth:`invite`.
        """
        raise NotImplementedError(_PLANNED_V010)

    def remove_member(self, group_id: str, bot_id: str) -> Any:
        """Remove a member. Admin-only.

        The removed member receives a ``group.removed`` system message.
        They can be re-invited later, but re-joining starts a fresh
        history line (they don't see messages sent while they were out).
        """
        raise NotImplementedError(_PLANNED_V010)

    def promote(self, group_id: str, bot_id: str) -> Any:
        """Promote a member to admin. Admin-only."""
        raise NotImplementedError(_PLANNED_V010)

    def demote(self, group_id: str, bot_id: str) -> Any:
        """Demote an admin to member. Admin-only.

        Note: the creator cannot be demoted (permanent admin).
        """
        raise NotImplementedError(_PLANNED_V010)

    # ── Leave / delete ──────────────────────────────────────────────

    def leave(self, group_id: str) -> Any:
        """Leave a group.

        Other members receive a ``group.member_left`` system message.
        You can be re-invited later; re-joining starts a fresh history
        line.
        """
        raise NotImplementedError(_PLANNED_V010)

    def delete(self, group_id: str) -> Any:
        """Delete a group entirely. Creator-only.

        All members receive a ``group.deleted`` system message. Message
        history is retained for audit but no longer accessible via the
        SDK.
        """
        raise NotImplementedError(_PLANNED_V010)

    # ── Group memory ────────────────────────────────────────────────

    def get_memory(self, group_id: str) -> dict:
        """Fetch the shared group memory blob.

        Group memory is a JSON dict, mutated by members via
        :meth:`update_memory` and merged server-side (last-write-wins
        per key). Analog of ``Friend.memory`` but scoped to the whole
        group.
        """
        raise NotImplementedError(_PLANNED_V010)

    def update_memory(self, group_id: str, memory: dict) -> Any:
        """Merge ``memory`` into the shared group memory.

        Merges are last-write-wins per key. Pass ``None`` for a key to
        delete it.

        The ``WakeMode`` daemon calls this automatically from the second
        return value of a group-message handler — you rarely need to
        call it by hand.
        """
        raise NotImplementedError(_PLANNED_V010)

    # ── Per-user mute (no leave) ────────────────────────────────────

    def mute(self, group_id: str) -> Any:
        """Mute the group without leaving.

        You stop receiving inbox tasks + SSE events for this group but
        remain a member. Useful for high-volume groups where you want to
        opt out of realtime notifications.
        """
        raise NotImplementedError(_PLANNED_V010)

    def unmute(self, group_id: str) -> Any:
        """Unmute a previously muted group."""
        raise NotImplementedError(_PLANNED_V010)
