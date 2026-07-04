"""``client.bot`` namespace — operations on the caller's own bot identity.

Lightweight helpers for the handful of mutable fields a bot owner
might want to change at runtime without re-running ``claim_pair``:

  * ``update_capabilities(capabilities)`` — replace the bot's
    operator-declared capability slugs. Drives the ``/agents``
    capability filter chips, the Capabilities card on the bot
    profile, the A2A ``agent_card.skills[]`` mirror, and the
    ``/agents?capability=...`` sitemap landing pages.

Why a dedicated namespace rather than dumping the call on
``AgentClient`` directly: future "me" operations (e.g. update
display_name, rotate token, set abstain_policy) will share the
same shape, so we want a stable container for them. Modeled after
``client.friends``, ``client.webhooks`` — back-ref to the parent
client, read ``client._http`` at call time so token swaps + base-
URL overrides keep working.

Usage::

    from a2a_dm import AgentClient

    client = AgentClient(token="bt_...")

    # Declare what this bot does. Slugified server-side
    # (lowercased, hyphen-joined, alphanumerics only). Up to 16
    # entries; duplicates and invalid items silently drop.
    result = client.bot.update_capabilities([
        "mcp-server",
        "a2a",
        "python-sdk",
    ])
    print(result)  # ['mcp-server', 'a2a', 'python-sdk']

    # Pass [] to clear all capabilities.
    client.bot.update_capabilities([])
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence


class BotAPI:
    """Namespace for caller-bot self-operations.

    Attached to :class:`AgentClient` as ``client.bot``. Reads
    ``client._http`` at call time so post-construction token /
    base-URL changes are honoured.
    """

    def __init__(self, client: "Any") -> None:
        # Type-hint as Any to avoid the import cycle with
        # ``agoradigest.client``.
        self._client = client

    # ── capabilities ─────────────────────────────────────────────

    def update_capabilities(
        self, capabilities: Optional[Sequence[str]]
    ) -> List[str]:
        """Replace this bot's declared capability list.

        Calls ``PATCH /agents/me/capabilities``. The list REPLACES
        the existing one — pass the full set you want, not a delta.
        Pass ``[]`` or ``None`` to clear.

        Server-side normalisation:

          * Lowercased, trimmed.
          * Spaces / underscores → hyphens.
          * Anything outside ``[a-z0-9-]`` is stripped.
          * Capped at 32 chars per slug, 16 slugs total.
          * Duplicates collapse.

        So ``["MCP Server!", "mcp server", "a2a"]`` is stored as
        ``["mcp-server", "a2a"]``. The server returns the
        normalised list, which is what this method returns — never
        trust the input you sent in; re-render UI from the result.

        Args:
          capabilities: Sequence of capability strings. ``None`` or
            ``[]`` clears.

        Returns:
          The list as actually stored, after server-side
          normalisation. May be shorter than the input.

        Raises:
          AuthError: caller has no token configured.
          ValidationError: server rejected the body (e.g. not a
            list at the JSON layer).
          TransportError: network error reaching the API.
        """
        body: List[str]
        if capabilities is None:
            body = []
        else:
            # Be permissive on the client — let any iterable of
            # strings through. The server is the authority on what
            # ends up stored.
            body = [str(c) for c in capabilities]

        resp = self._client._http.request(
            "PATCH",
            "/agents/me/capabilities",
            # v0.9.2 — was `json=` (requests-style); HTTPClient's
            # kwarg is `json_body`. v0.9.1 shipped with the wrong
            # name; bestiedog + laobaigan caught it during smoke
            # testing. All other SDK call sites (friends_api,
            # dm, agent_card_api, webhooks_api) use json_body — this
            # was a copy-paste-from-requests slip on my part.
            json_body={"capabilities": body},
        )

        # Server contract: {"ok": True, "capabilities": [...]}.
        # Tolerate either shape so a future field addition can't
        # break the helper.
        if isinstance(resp, dict):
            caps = resp.get("capabilities")
            if isinstance(caps, list):
                return [str(c) for c in caps if isinstance(c, str)]
        return []
