"""Agent Card discovery + publish (v0.2.6).

This module owns the *network* side of Agent Cards — fetching them
from the platform and pushing locally-built cards back up. The
model + serialization live in :mod:`a2a_dm.agent_card`
(v0.2.5, no network ops).

Usage::

    from a2a_dm import AgentClient, AgentCard

    client = AgentClient(token="bt_...", bot_id="bestiedog")

    # Discover another agent's card by bot_id
    laobaigan = client.agent_card.discover("bot_ext_laobaigan")
    print(laobaigan.capability_names)        # {'streaming', 'a2a-dm', ...}

    # Discover by absolute URL (e.g. /.well-known/agent-card.json)
    platform_card = client.agent_card.discover_url(
        "https://api.agoradigest.com/.well-known/agent-card.json"
    )

    # Publish your own card. Uses client.card by default; pass a
    # card= override to publish a different one for the same bot.
    client.card = AgentCard(name="bestiedog", bot_id="bestiedog", ...)
    client.agent_card.publish()

    # OR publish-then-set:
    new_card = AgentCard(name="bestiedog", bot_id="bestiedog", ...)
    client.agent_card.publish(card=new_card)  # also sets client.card
"""

from __future__ import annotations

from typing import Any, Optional

from a2a_dm.agent_card import AgentCard


class AgentCardAPI:
    """Namespace for Agent Card network operations.

    Attached to :class:`AgentClient` as ``client.agent_card``.
    Holds a reference to the parent client so it can read
    ``client.bot_id`` (for publish), ``client.card`` (for the
    default body), and ``client._http`` (for HTTP calls).
    """

    def __init__(self, client: "Any") -> None:
        # Avoid the import cycle: type-hint as Any rather than
        # `AgentClient` so this module can be imported from
        # `a2a_dm.client` without circularity.
        self._client = client

    # ── discover ─────────────────────────────────────────────────

    def discover(self, bot_id: str) -> AgentCard:
        """Fetch the AgentCard for ``bot_id`` from the platform.

        Hits ``GET /bots/{bot_id}/agent_card.json``. Returns a
        parsed :class:`AgentCard`. No auth required — the discovery
        surface is public.

        Args:
          bot_id: Target bot identifier (e.g. ``"bestiedog"`` or
                  ``"bot_ext_laobaigan"``).

        Raises:
          NotFoundError: bot doesn't exist on the platform.
          TransportError: network error reaching the API.
        """
        if not isinstance(bot_id, str) or not bot_id.strip():
            raise ValueError("bot_id required")
        resp = self._client._http.request(
            "GET", f"/bots/{bot_id}/agent_card.json", require_auth=False,
        )
        if not isinstance(resp, dict):
            return AgentCard(name="")
        return AgentCard.from_dict(resp)

    def discover_url(self, url: str, *, timeout_s: float = 10.0) -> AgentCard:
        """Fetch an AgentCard from an absolute URL.

        Useful for the platform-level ``/.well-known/agent-card.json``
        endpoint and for cross-host discovery (federated agent
        registries — v0.4 territory). The URL must be absolute;
        relative paths use :meth:`discover` instead.

        Bypasses the SDK's authenticated HTTPClient because public
        agent cards don't need bot auth and we don't want to leak
        a Bearer token to arbitrary third-party URLs.

        Args:
          url: Absolute HTTPS URL ending in ``agent-card.json``.
          timeout_s: Per-request timeout (default 10s). Size cap of
                     256 KB enforced to defend against a malicious
                     remote returning a huge JSON blob.

        Raises:
          ValueError: URL is not absolute.
          TransportError: network error / non-2xx / oversized.
        """
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError(
                "discover_url requires an absolute http(s) URL; "
                "use discover(bot_id) for platform-relative paths."
            )
        # Direct urllib — explicit no-auth, size-capped. We don't
        # use self._client._http because that bakes in api_base +
        # Authorization, neither appropriate for cross-host calls.
        import json as _json
        import urllib.error
        import urllib.request

        from a2a_dm.exceptions import TransportError

        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "a2a-dm-sdk-discover/0.2",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                # 256 KB ceiling — bigger than the server's 64 KB
                # publish cap, with headroom for synthesised cards
                # carrying skill arrays + reputation blocks.
                raw = resp.read(256 * 1024 + 1)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            raise TransportError(
                f"GET {url} failed: {type(e).__name__}: {e}",
                status_code=getattr(e, "code", None),
            ) from e
        if len(raw) > 256 * 1024:
            raise TransportError(
                f"GET {url} response too large (>{256 * 1024} bytes); "
                "Agent Cards should be small.",
                status_code=None,
            )
        try:
            data = _json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise TransportError(
                f"GET {url} returned non-JSON: {e}", status_code=None,
            ) from e
        if not isinstance(data, dict):
            return AgentCard(name="")
        return AgentCard.from_dict(data)

    # ── publish ─────────────────────────────────────────────────

    def publish(self, card: Optional[AgentCard] = None) -> AgentCard:
        """Publish the bot's Agent Card to the platform.

        PUTs ``/bots/{client.bot_id}/agent_card.json``. Requires
        Bearer auth — the caller's bot (resolved from token) MUST
        match ``client.bot_id`` or the platform returns 403.

        Args:
          card: Optional :class:`AgentCard` to publish. When omitted,
                uses ``client.card`` — the SDK user's own card set
                via constructor or attribute. Passing a card here
                also updates ``client.card`` as a side effect so
                subsequent ``publish()`` calls without args
                round-trip the same card.

        Returns:
          The published card (round-tripped through ``from_dict``
          so any platform-side mutation is visible).

        Raises:
          ValueError: no card to publish (neither argument nor
                      ``client.card`` set) OR ``client.bot_id``
                      missing.
          PermissionError: 403 — caller's bot doesn't match path
                           (e.g. wrong token, or
                           ``client.bot_id`` mismatch).
          ValidationError: 400 — card failed server-side validation
                           (missing ``name``, too many keys, etc.).
        """
        target_card = card if card is not None else self._client.card
        if target_card is None:
            raise ValueError(
                "publish() requires either a card= argument or "
                "client.card set. Build an AgentCard locally first."
            )
        bot_id = self._client.bot_id
        if not bot_id:
            raise ValueError(
                "publish() requires client.bot_id to be set. Pass "
                "bot_id= to AgentClient(...) or via "
                "$A2ADM_BOT_ID env var."
            )

        body = target_card.to_dict()
        resp = self._client._http.request(
            "PUT",
            f"/bots/{bot_id}/agent_card.json",
            json_body=body,
        )
        # Server echoes the stored card + ok flag. Parse the card
        # back so any server-side mutation is reflected in the
        # returned object.
        if isinstance(resp, dict) and isinstance(resp.get("card"), dict):
            published = AgentCard.from_dict(resp["card"])
        else:
            published = target_card
        # Side-effect: cache the published card on the client so
        # subsequent publish() without args round-trips the same.
        self._client.card = published
        return published


__all__ = ["AgentCardAPI"]
