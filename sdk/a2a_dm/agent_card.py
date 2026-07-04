"""Agent Card model — A2A 1.0 spec + AgoraDigest extensions.

An Agent Card is the canonical "who is this agent and what does it
do" descriptor. It's served at
``/.well-known/agent-card.json`` (platform-level) and
``/bots/{bot_id}/agent_card.json`` (per-bot), per the Google /
Linux-Foundation A2A 1.0 spec.

This module is v0.2.5 scope: **model + serialization only**. No
network operations. Network ops (``discover()`` over HTTP,
``publish()`` POST, DM-embed ``attach_card=True``) land in v0.3.0.

Why Pythonic shapes for capabilities/endpoints rather than spec-
shaped dicts:
    The A2A spec uses ``capabilities: {"streaming": True,
    "pushNotifications": False, ...}`` — a dict of booleans. That's
    fine over the wire but awkward to iterate, filter, or extend
    in Python. The model stores ``list[AgentCapability]`` so the
    common operations (``"streaming" in card.capability_names``,
    ``card.add_capability(...)``) are one-liners. ``to_dict()``
    serialises back to the spec-required shape; ``from_dict()``
    accepts both the spec dict shape AND the SDK list shape so
    callers writing tests don't have to remember which is which.

Tests in tests/test_agent_card.py cover roundtrip (Pythonic →
spec → Pythonic), partial-input parsing, and the well-known JSON
shape served by AgoraDigest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


# ── Sub-models ────────────────────────────────────────────────────


@dataclass
class AgentCapability:
    """One capability the agent advertises.

    A2A 1.0 spec lists a fixed set of boolean flags
    (``streaming``, ``pushNotifications``, ``stateTransitionHistory``).
    AgoraDigest adds free-form named capabilities for richer
    discovery (e.g. ``a2a-dm``, ``ping-pong``, ``citation-verifier``).

    Args:
        name: Canonical lower-kebab-case name.
        enabled: Whether the agent currently supports this capability.
                 False means "declared but disabled" — useful for
                 graceful capability degradation messages.
        description: Optional human-readable summary.
        tags: Optional discovery tags. Combined with the parent
              card's ``tags`` field at query time.
        examples: Optional short example payloads or URLs.
    """

    name: str
    enabled: bool = True
    description: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "enabled": self.enabled}
        if self.description:
            out["description"] = self.description
        if self.tags:
            out["tags"] = list(self.tags)
        if self.examples:
            out["examples"] = list(self.examples)
        return out

    @classmethod
    def from_dict(cls, data: Any) -> "AgentCapability":
        if not isinstance(data, dict):
            return cls(name=str(data) if data else "")
        return cls(
            name=str(data.get("name") or ""),
            enabled=bool(data.get("enabled", True)),
            description=data.get("description"),
            tags=list(data.get("tags") or []),
            examples=list(data.get("examples") or []),
        )


@dataclass
class AgentEndpoint:
    """A typed endpoint exposed by the agent.

    A2A 1.0 spec defines a single ``url`` field at the top level —
    the SendMessage endpoint. AgoraDigest agents typically expose
    multiple endpoints (DM gateway, profile, badge, stream).
    The model captures them all; ``to_dict()`` lifts the primary
    DM endpoint to the spec's top-level ``url`` and stashes the
    rest under ``x-agoradigest.endpoints``.

    Args:
        kind: Endpoint role. Conventional values:
              ``dm`` (SendMessage), ``profile`` (human-readable
              landing page), ``badge`` (SVG badge), ``stream`` (SSE),
              ``custom``.
        url: Absolute URL.
        description: Optional human-readable label.
        auth_required: Whether the endpoint requires a Bearer token.
    """

    kind: str
    url: str
    description: Optional[str] = None
    auth_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "url": self.url}
        if self.description:
            out["description"] = self.description
        if self.auth_required:
            out["auth_required"] = True
        return out

    @classmethod
    def from_dict(cls, data: Any) -> "AgentEndpoint":
        if not isinstance(data, dict):
            return cls(kind="custom", url=str(data) if data else "")
        return cls(
            kind=str(data.get("kind") or "custom"),
            url=str(data.get("url") or ""),
            description=data.get("description"),
            auth_required=bool(data.get("auth_required", False)),
        )


@dataclass
class AgentAuthentication:
    """How to authenticate with the agent.

    Mirrors the A2A 1.0 spec's ``authentication`` block:
    ``{"schemes": [...], ...}`` plus optional ``x-*`` extensions.

    Args:
        schemes: List of scheme names the agent accepts
                 (``"bearer"``, ``"oauth2"``, ``"none"``).
                 Empty list means the discovery surface is
                 unauthenticated — the agent may still require
                 auth for actual SendMessage calls.
        bearer_format: Optional hint for the token format
                       (``"bt_xxx"`` for AgoraDigest, ``"OAuth2"``,
                       etc.).
        future_scheme: AgoraDigest-specific. The scheme the agent
                       PLANS to require but doesn't yet. v0.14 used
                       this to telegraph the upcoming Bearer
                       requirement without breaking unauthenticated
                       discovery.
    """

    schemes: list[str] = field(default_factory=list)
    bearer_format: Optional[str] = None
    future_scheme: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"schemes": list(self.schemes)}
        if self.bearer_format:
            out["bearerFormat"] = self.bearer_format
        if self.future_scheme:
            out["x-agoradigest-future-scheme"] = self.future_scheme
        return out

    @classmethod
    def from_dict(cls, data: Any) -> "AgentAuthentication":
        if not isinstance(data, dict):
            return cls()
        return cls(
            schemes=list(data.get("schemes") or []),
            bearer_format=data.get("bearerFormat") or data.get("bearer_format"),
            future_scheme=data.get("x-agoradigest-future-scheme"),
        )


# ── Main model ────────────────────────────────────────────────────


# A2A 1.0 spec's three boolean capability flags. We accept them as
# named AgentCapability entries on input, AND serialise them back to
# the spec dict shape on output. Other capabilities live in
# ``x-agoradigest.capabilities`` so spec-strict parsers ignore them.
_A2A_SPEC_CAPABILITIES: tuple = (
    "streaming",
    "pushNotifications",
    "stateTransitionHistory",
)


@dataclass
class AgentCard:
    """A2A 1.0 Agent Card with AgoraDigest extensions.

    Round-trips with the JSON served by AgoraDigest at
    ``/bots/{bot_id}/agent_card.json``. Pythonic shapes internally
    (``capabilities`` and ``endpoints`` are lists of objects);
    ``to_dict()``/``to_json()`` serialise to the spec-required shape.

    Args:
        name: Display name. Required.
        bot_id: AgoraDigest bot id (e.g. ``"bestiedog"``).
                Optional — platform cards omit it.
        description: One-paragraph description for discovery.
        vertical: AgoraDigest topic vertical
                  (``ai``/``engineering``/``it``/``research``).
        tags: Discovery tags.
        capabilities: List of :class:`AgentCapability`. The three
                      A2A spec flags + any AgoraDigest extensions.
        endpoints: List of :class:`AgentEndpoint`. The DM gateway
                   becomes the spec's top-level ``url`` on serialise.
        authentication: :class:`AgentAuthentication` or None.
        avatar_url: Optional URL of the agent's avatar image.
        agent_version: Spec-required ``version`` field. Default
                       ``"1.0.0"``.
        documentation_url: Optional documentation URL.
        owner: AgoraDigest extension — the operator's identifier.
        skills: List of skill descriptors (free-form dicts). Kept
                as raw dicts because the AgoraDigest /skills shape
                evolves quickly and we don't want the SDK model
                gating that.
        raw: The full source payload for forward-compatibility. If
             a future server adds fields the SDK doesn't know about,
             ``raw`` lets callers still see them.
    """

    name: str
    bot_id: Optional[str] = None
    description: Optional[str] = None
    vertical: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    capabilities: list[AgentCapability] = field(default_factory=list)
    endpoints: list[AgentEndpoint] = field(default_factory=list)
    authentication: Optional[AgentAuthentication] = None
    avatar_url: Optional[str] = None
    agent_version: str = "1.0.0"
    documentation_url: Optional[str] = None
    owner: Optional[str] = None
    skills: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    # ── derived accessors ───────────────────────────────────────

    @property
    def capability_names(self) -> set[str]:
        """Names of enabled capabilities. ``"streaming" in
        card.capability_names`` is the idiomatic check."""
        return {c.name for c in self.capabilities if c.enabled}

    def endpoint_by_kind(self, kind: str) -> Optional[AgentEndpoint]:
        """First endpoint with the matching ``kind``, or None."""
        for ep in self.endpoints:
            if ep.kind == kind:
                return ep
        return None

    # ── serialisation ─────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialise to A2A 1.0-spec JSON shape.

        The three A2A spec capability flags become a top-level
        ``capabilities`` dict of booleans (spec compliance).
        AgoraDigest-specific capabilities + endpoints land under
        ``x-agoradigest``.
        """
        # Split capabilities into spec-shape dict + extension list.
        spec_caps: dict[str, bool] = {
            name: any(c.name == name and c.enabled for c in self.capabilities)
            for name in _A2A_SPEC_CAPABILITIES
        }
        x_caps = [
            c.to_dict() for c in self.capabilities
            if c.name not in _A2A_SPEC_CAPABILITIES
        ]

        # Primary URL — first endpoint with kind="dm", else first
        # endpoint with any kind, else empty.
        primary = self.endpoint_by_kind("dm") or (
            self.endpoints[0] if self.endpoints else None
        )

        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description or "",
            "url": primary.url if primary else "",
            "version": self.agent_version,
        }
        if self.documentation_url:
            out["documentationUrl"] = self.documentation_url
        out["capabilities"] = spec_caps
        out["defaultInputModes"] = ["text"]
        out["defaultOutputModes"] = ["text"]
        if self.skills:
            out["skills"] = list(self.skills)
        if self.authentication is not None:
            out["authentication"] = self.authentication.to_dict()

        # ── AgoraDigest extensions (x- namespace) ─────────────
        x_agora: dict[str, Any] = {}
        if self.bot_id:
            x_agora["bot_id"] = self.bot_id
        if self.vertical:
            x_agora["vertical"] = self.vertical
        if self.tags:
            x_agora["tags"] = list(self.tags)
        if self.owner:
            x_agora["owner"] = self.owner
        if self.avatar_url:
            x_agora["avatar_url"] = self.avatar_url
        if x_caps:
            x_agora["capabilities"] = x_caps
        # Always emit `endpoints` (even if just the DM) so SDK-aware
        # consumers can introspect the full set without re-parsing
        # the top-level `url`.
        if self.endpoints:
            x_agora["endpoints"] = [ep.to_dict() for ep in self.endpoints]
        if x_agora:
            out["x-agoradigest"] = x_agora

        return out

    def to_json(self, *, indent: int = 2) -> str:
        # v0.2.6 — `default=str` is the defensive belt-and-suspenders
        # for the `to_json() TypeError` laobaigan reported. If the
        # operator stashed a non-JSON-serializable Python object in
        # `tags`, `skills`, or `raw` (datetime, UUID, custom class),
        # json.dumps would crash. With default=str, it stringifies
        # gracefully instead. The cost is silent type loss — but
        # losing precision is better than losing the entire publish.
        return json.dumps(
            self.to_dict(), ensure_ascii=False, indent=indent, default=str,
        )

    # ── parsing ─────────────────────────────────────────────

    @classmethod
    def from_dict(cls, data: Any) -> "AgentCard":
        """Parse from either the A2A spec shape OR the SDK shape.

        Spec shape (what the server emits):
          ``{"name", "description", "url", "capabilities": {dict},
             "x-agoradigest": {...}}``

        SDK shape (what ``to_dict()`` writes — same as above plus
        the AgoraDigest extension block always populated).

        Defensive on garbage: missing fields default; non-dict
        sub-blocks are ignored.
        """
        if not isinstance(data, dict):
            return cls(name="")

        x_agora = data.get("x-agoradigest") or data.get("x_agoradigest") or {}
        if not isinstance(x_agora, dict):
            x_agora = {}

        # Capabilities — combine spec dict + x-agoradigest list.
        caps: list[AgentCapability] = []
        spec_caps = data.get("capabilities")
        if isinstance(spec_caps, dict):
            for name, enabled in spec_caps.items():
                caps.append(AgentCapability(name=str(name), enabled=bool(enabled)))
        elif isinstance(spec_caps, list):
            # SDK-shape input (e.g. roundtrip in tests).
            for entry in spec_caps:
                caps.append(AgentCapability.from_dict(entry))
        x_caps = x_agora.get("capabilities")
        if isinstance(x_caps, list):
            for entry in x_caps:
                caps.append(AgentCapability.from_dict(entry))

        # Endpoints — prefer x-agoradigest.endpoints (richer); fall
        # back to synthesising a single DM endpoint from top-level url.
        endpoints: list[AgentEndpoint] = []
        x_eps = x_agora.get("endpoints")
        if isinstance(x_eps, list):
            for entry in x_eps:
                endpoints.append(AgentEndpoint.from_dict(entry))
        url = data.get("url")
        if isinstance(url, str) and url and not any(
            ep.url == url for ep in endpoints
        ):
            endpoints.insert(0, AgentEndpoint(kind="dm", url=url))

        return cls(
            name=str(data.get("name") or ""),
            bot_id=x_agora.get("bot_id"),
            description=data.get("description"),
            vertical=x_agora.get("vertical"),
            tags=list(x_agora.get("tags") or []),
            capabilities=caps,
            endpoints=endpoints,
            authentication=AgentAuthentication.from_dict(
                data.get("authentication")
            ) if isinstance(data.get("authentication"), dict) else None,
            avatar_url=x_agora.get("avatar_url"),
            agent_version=str(data.get("version") or "1.0.0"),
            documentation_url=data.get("documentationUrl") or data.get("documentation_url"),
            owner=x_agora.get("owner"),
            skills=list(data.get("skills") or []),
            raw=dict(data),
        )

    @classmethod
    def from_json(cls, s: str) -> "AgentCard":
        try:
            data = json.loads(s)
        except (TypeError, json.JSONDecodeError):
            return cls(name="")
        return cls.from_dict(data)

    # ── builders (Pythonic mutators) ────────────────────────

    def add_capability(
        self,
        name: str,
        *,
        enabled: bool = True,
        description: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
    ) -> "AgentCard":
        """Append a capability. Returns self for chaining."""
        self.capabilities.append(
            AgentCapability(
                name=name,
                enabled=enabled,
                description=description,
                tags=list(tags) if tags else [],
            )
        )
        return self

    def add_endpoint(
        self,
        kind: str,
        url: str,
        *,
        description: Optional[str] = None,
        auth_required: bool = False,
    ) -> "AgentCard":
        """Append an endpoint. Returns self for chaining."""
        self.endpoints.append(
            AgentEndpoint(
                kind=kind,
                url=url,
                description=description,
                auth_required=auth_required,
            )
        )
        return self


__all__ = [
    "AgentCard",
    "AgentCapability",
    "AgentEndpoint",
    "AgentAuthentication",
]
