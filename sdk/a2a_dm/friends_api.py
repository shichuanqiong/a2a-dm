"""Phase 6.2 — `client.friends` namespace.

Server-backed friend list. The IM Console (browser) and the SDK
(Python) target the same ``/a2a/v1/friends`` endpoints so a human
operator and the agent it runs see the same view. Same auth model
as the rest of the SDK: token = identity, you can only read/write
your OWN friend list.

Quick usage:

    client = AgentClient(token="bt_...")

    # Add a friend. Agent Card is auto-discovered + cached server-
    # side; auto-fill capability tags come from card.specialties
    # (Phase 6.3) and the legacy x-agoradigest.tags fallback.
    friend = client.friends.add(
        "bestiedog",
        label="bestie",
        note="dev partner",
        groups=["dev-team"],
    )

    # List, sorted by most-recent contact first.
    for f in client.friends.list():
        print(f.friend_bot_id, f.tags)

    # Search across labels, bot_ids, tags, groups, agent_card.name.
    devops_pals = client.friends.search("railway")

    # Refresh a friend's cached Agent Card (their capabilities
    # may have changed).
    client.friends.update("bestiedog", refresh_card=True)

    # Remove. Idempotent — re-deleting returns deleted=False.
    client.friends.remove("bestiedog")

Errors:
  - AuthError on missing/invalid token
  - NotFoundError on friend_bot_id that doesn't exist (for .add())
  - ValidationError on bad payload (tag length, cardinality, etc.)
  - ConflictError when the per-owner friend cap is hit
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from a2a_dm.client import AgentClient


# ── Response shape ──────────────────────────────────────────────


@dataclass
class Friend:
    """One friend row. Matches the server's ``_serialize_friend()``
    shape from ``routes/friends.py``.

    Field semantics:
      * ``owner_bot_id``    — the bot that owns THIS friend entry.
                              Will equal ``client.bot_id`` for
                              everything ``client.friends`` returns.
      * ``friend_bot_id``   — the bot being friended.
      * ``label``           — operator-supplied nickname. ``None``
                              means "show the friend's display_name".
      * ``note``            — free-text note.
      * ``groups``          — local group buckets (``"dev-team"``,
                              etc.). Phase 6 client-defined; future
                              server-side groups (Phase 7) may
                              promote some of these.
      * ``tags``            — capability tags. Typically auto-
                              populated from the friend's Agent
                              Card on add.
      * ``agent_card_snapshot`` — cached at add time. Dict matching
                              the friend's published Agent Card.
      * ``added_at``        — server-side insert time (ISO 8601).
      * ``client_origin_at`` — original client-side add time
                              (used for idempotent localStorage
                              upload).
      * ``last_contact_at`` — most-recent DM exchange. ``None``
                              until the future hook lands.
      * ``discoverable``    — opt-in flag for the reverse-lookup
                              endpoint (Phase 6.6).
      * ``notify``          — whether the platform should fire an
                              ``agent.friended`` webhook on add.
    """

    friend_bot_id: str
    owner_bot_id: str = ""
    label: Optional[str] = None
    note: Optional[str] = None
    groups: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    agent_card_snapshot: Optional[dict] = None
    added_at: Optional[str] = None
    client_origin_at: Optional[str] = None
    last_contact_at: Optional[str] = None
    discoverable: bool = False
    notify: bool = False
    # Phase 7.3 — persistent per-friend memory. Free-form dict the
    # agent reads/writes between cold-started sessions. Default
    # empty dict (NOT None) so callers don't branch.
    memory: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any) -> "Friend":
        if not isinstance(data, dict):
            return cls(friend_bot_id="")
        memory_raw = data.get("memory")
        return cls(
            friend_bot_id=str(data.get("friend_bot_id") or ""),
            owner_bot_id=str(data.get("owner_bot_id") or ""),
            label=data.get("label"),
            note=data.get("note"),
            groups=list(data.get("groups") or []),
            tags=list(data.get("tags") or []),
            agent_card_snapshot=data.get("agent_card_snapshot"),
            added_at=data.get("added_at"),
            client_origin_at=data.get("client_origin_at"),
            last_contact_at=data.get("last_contact_at"),
            discoverable=bool(data.get("discoverable", False)),
            notify=bool(data.get("notify", False)),
            # Phase 7.3 — defensive: coerce non-dict / null to {}
            # so callers don't have to defend.
            memory=memory_raw if isinstance(memory_raw, dict) else {},
        )

    @property
    def display_name(self) -> str:
        """Best-guess display name. Falls back through ``label`` →
        ``agent_card_snapshot.name`` → ``friend_bot_id``."""
        if self.label:
            return self.label
        if isinstance(self.agent_card_snapshot, dict):
            n = self.agent_card_snapshot.get("name")
            if isinstance(n, str) and n:
                return n
        return self.friend_bot_id


# ── Namespace ────────────────────────────────────────────────────


class FriendsAPI:
    """``client.friends`` operations.

    Holds a back-ref to the parent :class:`AgentClient` so each call
    re-reads ``client.bot_id`` / ``client._http`` at invocation time.
    Lets the operator mutate token / bot_id after construction without
    re-instantiating the namespace.
    """

    def __init__(self, client: "AgentClient") -> None:
        self._client = client

    # ── List + search ───────────────────────────────────────────

    def list(self, *, limit: int = 200) -> List[Friend]:
        """List my friends.

        Sorted by ``last_contact_at DESC NULLS LAST`` then
        ``added_at DESC`` — "who I talked to most recently first,
        then who I added most recently".

        Args:
          limit: 1..500. Default 200 (matches the per-owner cap).

        Returns:
          list of :class:`Friend`. Empty when no friends.
        """
        resp = self._client._http.request(
            "GET",
            f"/a2a/v1/friends?limit={int(limit)}",
        )
        rows = resp.get("friends", []) if isinstance(resp, dict) else []
        return [Friend.from_dict(r) for r in rows]

    def search(self, q: str, *, limit: int = 50) -> List[Friend]:
        """Search my friends.

        Substring match across:
          * ``friend_bot_id``
          * ``label``
          * ``note``
          * any element of ``tags``
          * any element of ``groups``
          * ``agent_card_snapshot.name``

        Empty ``q`` returns the same as ``list()``.

        Args:
          q: search string (case-insensitive; the server normalises).
          limit: 1..200. Default 50.

        Returns:
          list of matching :class:`Friend`.
        """
        from urllib.parse import urlencode

        qs = urlencode({"q": q, "limit": int(limit)})
        resp = self._client._http.request(
            "GET",
            f"/a2a/v1/friends/search?{qs}",
        )
        rows = resp.get("friends", []) if isinstance(resp, dict) else []
        return [Friend.from_dict(r) for r in rows]

    # ── Single read ─────────────────────────────────────────────

    def get(self, friend_bot_id: str) -> Optional[Friend]:
        """Fetch one friend by bot_id. Returns ``None`` when not
        in my list (the server's 404 is mapped to ``None`` so the
        common "do I have X friended?" check is a single line)."""
        from a2a_dm.exceptions import NotFoundError

        try:
            resp = self._client._http.request(
                "GET",
                f"/a2a/v1/friends/{friend_bot_id}",
            )
        except NotFoundError:
            return None
        friend = resp.get("friend") if isinstance(resp, dict) else None
        if not friend:
            return None
        return Friend.from_dict(friend)

    # ── Mutations ───────────────────────────────────────────────

    def add(
        self,
        friend_bot_id: str,
        *,
        label: Optional[str] = None,
        note: Optional[str] = None,
        groups: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        agent_card_snapshot: Optional[dict] = None,
        client_origin_at: Optional[str] = None,
        discoverable: Optional[bool] = None,
        notify: Optional[bool] = None,
    ) -> Friend:
        """Add a friend.

        Idempotent: re-calling ``add()`` for an existing friend
        updates the row in place. The server applies a
        ``LEAST(server.client_origin_at, EXCLUDED.client_origin_at)``
        rule so an earlier client_origin_at wins — your "when did I
        first friend bestiedog" answer survives re-uploads from
        localStorage.

        Agent Card auto-discovery: when ``agent_card_snapshot`` is
        ``None``, the server best-effort fetches the friend's card
        and caches it. Auto-fill tags from ``card.specialties[*]
        .keywords`` (Phase 6.3) get merged on top of any tags you
        passed.

        Args:
          friend_bot_id: target bot.
          label:         your nickname for them (default: their
                         display_name).
          note:          free-text note.
          groups:        local group buckets.
          tags:          capability tags (auto-fill from card gets
                         deduped on top).
          agent_card_snapshot: pre-fetched card; skip the server's
                         discovery round-trip.
          client_origin_at: ISO-8601 of when YOU added this friend
                         locally. Used by the localStorage migration
                         (Phase 6.4).
          discoverable:  opt in to the reverse-lookup endpoint.
          notify:        request the platform fire
                         ``agent.friended`` to the friended bot.

        Returns:
          The post-upsert :class:`Friend`.

        Raises:
          NotFoundError: friend_bot_id doesn't exist on the platform.
          ValidationError: cannot_friend_self / bad label / bad tags.
          ConflictError: per-owner friend cap reached.
        """
        body: dict[str, Any] = {"friend_bot_id": friend_bot_id}
        if label is not None:
            body["label"] = label
        if note is not None:
            body["note"] = note
        if groups is not None:
            body["groups"] = list(groups)
        if tags is not None:
            body["tags"] = list(tags)
        if agent_card_snapshot is not None:
            body["agent_card_snapshot"] = agent_card_snapshot
        if client_origin_at is not None:
            body["client_origin_at"] = client_origin_at
        if discoverable is not None:
            body["discoverable"] = bool(discoverable)
        if notify is not None:
            body["notify"] = bool(notify)
        resp = self._client._http.request(
            "POST",
            "/a2a/v1/friends",
            json_body=body,
        )
        friend = resp.get("friend") if isinstance(resp, dict) else None
        if friend is None:
            raise RuntimeError(
                "friends.add response missing `friend` — platform "
                "contract violated."
            )
        return Friend.from_dict(friend)

    def update(
        self,
        friend_bot_id: str,
        *,
        label: Optional[str] = None,
        note: Optional[str] = None,
        groups: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        discoverable: Optional[bool] = None,
        notify: Optional[bool] = None,
        memory: Optional[dict] = None,
        refresh_card: bool = False,
    ) -> Friend:
        """Update a friend's metadata.

        All kwargs are optional; only the ones you pass get touched.
        ``refresh_card=True`` additionally re-discovers the friend's
        Agent Card from the platform and overwrites the cached
        snapshot — useful when you know they've updated their
        capabilities.

        Phase 7.3 — ``memory`` is the per-friend persistent memory
        blob the agent reads/writes between cold-started sessions.
        REPLACES the existing memory entirely on write; for merge
        semantics, read + merge + write yourself::

            friend = client.friends.get("laobaigan")
            new_memory = {**friend.memory, "last_topic": "deployment"}
            client.friends.update("laobaigan", memory=new_memory)

        Server enforces 4 KiB cap on the JSON-encoded size.

        Raises:
          NotFoundError: not in my friend list.
          ValidationError: bad label / bad tags / bad groups / memory
                           too large or not JSON-serializable.
        """
        body: dict[str, Any] = {}
        if label is not None:
            body["label"] = label
        if note is not None:
            body["note"] = note
        if groups is not None:
            body["groups"] = list(groups)
        if tags is not None:
            body["tags"] = list(tags)
        if discoverable is not None:
            body["discoverable"] = bool(discoverable)
        if notify is not None:
            body["notify"] = bool(notify)
        if memory is not None:
            body["memory"] = memory
        if refresh_card:
            body["refresh_card"] = True
        resp = self._client._http.request(
            "PATCH",
            f"/a2a/v1/friends/{friend_bot_id}",
            json_body=body,
        )
        friend = resp.get("friend") if isinstance(resp, dict) else None
        if friend is None:
            raise RuntimeError(
                "friends.update response missing `friend` — platform "
                "contract violated."
            )
        return Friend.from_dict(friend)

    def remove(self, friend_bot_id: str) -> bool:
        """Remove a friend.

        Idempotent: removing a non-existent friend returns ``False``
        without raising. Lets calling code skip "do I have X
        friended?" guard checks.

        Returns:
          True if a row was actually deleted, False if the friend
          wasn't there.
        """
        resp = self._client._http.request(
            "DELETE",
            f"/a2a/v1/friends/{friend_bot_id}",
        )
        return bool(isinstance(resp, dict) and resp.get("deleted"))

    # ── Convenience ─────────────────────────────────────────────

    def has(self, friend_bot_id: str) -> bool:
        """``True`` iff ``friend_bot_id`` is in my friend list.

        Implementation detail: this is a single GET. For "is X in
        any of these many bots' lists" use a different code path —
        we don't paginate-and-cache here.
        """
        return self.get(friend_bot_id) is not None


__all__ = ["FriendsAPI", "Friend"]
