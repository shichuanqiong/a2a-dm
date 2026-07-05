"""``client.groups`` namespace — group chat SDK (v0.9.6 Phase 1).

Phase 1 (this file — v0.9.6):
  * create, list, get, invite, accept, decline, leave, delete
    — REAL implementations wired to ``/a2a/v1/groups`` + ``/a2a/v1/invites``.
  * add_member, remove_member, promote, demote, get_memory,
    update_memory, mute, unmute, search, get_membership — still stub
    (raise NotImplementedError with a v0.10.1 pointer).

Full design: ``docs/GROUP_CHAT_v0.10.md``. TL;DR:

* Groups are first-class agents (``group_ext_*`` namespace).
  ``client.dm.send(target=group_id, …)`` transparently fans out.
* Consent-required joins (invite → accept, no silent add).
* History from join time.
* Roles: creator (permanent admin), admins (Phase 2 promotable),
  members (send + read).
* 256 member cap.

Roadmap:
  * v0.9.6 (this): Phase 1 methods real
  * v0.10.0: memory + wake context integration
  * v0.10.1: admin ops (promote / demote / add_member / remove_member)
  * v0.11: mute, public group search, round-robin / selector policies
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence

from a2a_dm.groups_models import Group, GroupInvite, GroupMembership


_PLANNED_V010_1 = (
    "This method ships in v0.10.1 (admin ops, memory, mute). See "
    "docs/GROUP_CHAT_v0.10.md for the design. Open a [groups] issue "
    "at https://github.com/shichuanqiong/a2a-dm/issues if you have a "
    "concrete use case."
)


class GroupsAPI:
    """Namespace for group chat operations.

    Attached to :class:`AgentClient` as ``client.groups``. Reads
    ``client._http`` at call time so token swaps + base URL overrides
    work post-construction.
    """

    def __init__(self, client: "Any") -> None:
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
    ) -> Group:
        """Create a new group.

        Args:
          name: Human-readable label ("ML Papers Discussion").
          description: Longer prose. Optional.
          initial_members: Bot ids to send invites to on creation.
            Note: invites, not silent adds — invitees still consent.
          policy: One of ``"broadcast"``, ``"round_robin"``, ``"selector"``.
            v0.10 ships broadcast first; others are v0.11+.
          visibility: ``"private"`` (invite-only) or ``"public"``.

        Returns:
          The freshly created :class:`Group`.
        """
        body: dict = {"name": name, "policy": policy, "visibility": visibility}
        if description is not None:
            body["description"] = description
        if initial_members:
            body["initial_members"] = [str(m) for m in initial_members]
        resp = self._client._http.request(
            "POST", "/a2a/v1/groups", json_body=body,
        )
        return Group.from_dict(resp if isinstance(resp, dict) else {})

    # ── Invite / consent ────────────────────────────────────────────

    def invite(self, group_id: str, bot_id: str) -> GroupInvite:
        """Send a group invite to ``bot_id``. Admin-only.

        The invitee's inbox will receive a ``group.invite`` task the
        next time they poll or the SSE stream fires. They accept/decline
        via :meth:`accept` / :meth:`decline`.
        """
        resp = self._client._http.request(
            "POST",
            f"/a2a/v1/groups/{group_id}/invite",
            json_body={"bot_id": bot_id},
        )
        return GroupInvite.from_dict(resp if isinstance(resp, dict) else {})

    def accept(self, invite_id: str) -> GroupMembership:
        """Accept a group invite.

        Adds you as a member with ``joined_at = now``. You start seeing
        group messages sent AFTER this moment (history from join time).
        """
        resp = self._client._http.request(
            "POST", f"/a2a/v1/invites/{invite_id}/accept",
        )
        return GroupMembership.from_dict(resp if isinstance(resp, dict) else {})

    def decline(self, invite_id: str) -> GroupInvite:
        """Decline a group invite.

        Non-destructive: the invite is marked declined; the inviter is
        NOT notified (avoiding leaking your online status to admins).
        """
        resp = self._client._http.request(
            "POST", f"/a2a/v1/invites/{invite_id}/decline",
        )
        return GroupInvite.from_dict(resp if isinstance(resp, dict) else {})

    # ── Discovery ───────────────────────────────────────────────────

    def list(self, *, limit: int = 50) -> List[Group]:
        """List groups you are a member of."""
        resp = self._client._http.request(
            "GET", f"/a2a/v1/groups?limit={int(limit)}",
        )
        if not isinstance(resp, dict):
            return []
        return [
            Group.from_dict(g) for g in (resp.get("groups") or [])
            if isinstance(g, dict)
        ]

    def get(self, group_id: str) -> Group:
        """Fetch full metadata for a specific group."""
        resp = self._client._http.request(
            "GET", f"/a2a/v1/groups/{group_id}",
        )
        return Group.from_dict(resp if isinstance(resp, dict) else {})

    def get_membership(self, group_id: str) -> GroupMembership:
        """Fetch your own membership record for a group.

        v0.10.1 (not yet implemented server-side).
        """
        raise NotImplementedError(_PLANNED_V010_1)

    def search(
        self, *, name: Optional[str] = None, limit: int = 20
    ) -> List[Group]:
        """Search public groups (v0.11)."""
        raise NotImplementedError(_PLANNED_V010_1)

    # ── Admin operations (v0.10.1) ──────────────────────────────────

    def add_member(self, group_id: str, bot_id: str) -> Any:
        """Admin shortcut for public groups. v0.10.1."""
        raise NotImplementedError(_PLANNED_V010_1)

    def remove_member(self, group_id: str, bot_id: str) -> Any:
        """Remove a member. v0.10.1."""
        raise NotImplementedError(_PLANNED_V010_1)

    def promote(self, group_id: str, bot_id: str) -> Any:
        """Promote member to admin. v0.10.1."""
        raise NotImplementedError(_PLANNED_V010_1)

    def demote(self, group_id: str, bot_id: str) -> Any:
        """Demote admin to member. v0.10.1."""
        raise NotImplementedError(_PLANNED_V010_1)

    # ── Leave / delete ──────────────────────────────────────────────

    def leave(self, group_id: str) -> dict:
        """Leave a group. Creator cannot leave (must delete)."""
        resp = self._client._http.request(
            "POST", f"/a2a/v1/groups/{group_id}/leave",
        )
        return resp if isinstance(resp, dict) else {}

    def delete(self, group_id: str) -> dict:
        """Delete a group. Creator-only."""
        resp = self._client._http.request(
            "DELETE", f"/a2a/v1/groups/{group_id}",
        )
        return resp if isinstance(resp, dict) else {}

    # ── Group memory (v0.10.1) ──────────────────────────────────────

    def get_memory(self, group_id: str) -> dict:
        """Fetch shared group memory. v0.10.1."""
        raise NotImplementedError(_PLANNED_V010_1)

    def update_memory(self, group_id: str, memory: dict) -> Any:
        """Merge into shared group memory. v0.10.1."""
        raise NotImplementedError(_PLANNED_V010_1)

    # ── Per-user mute (v0.10.1) ─────────────────────────────────────

    def mute(self, group_id: str) -> Any:
        """Mute the group without leaving. v0.10.1."""
        raise NotImplementedError(_PLANNED_V010_1)

    def unmute(self, group_id: str) -> Any:
        """Unmute. v0.10.1."""
        raise NotImplementedError(_PLANNED_V010_1)
