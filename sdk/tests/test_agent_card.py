"""AgentCard model tests (v0.2.5).

These tests pin the contract between the SDK model and the JSON
shape served by AgoraDigest at ``/bots/{bot_id}/agent_card.json``
(see ``apps/api/src/routes/a2a.py::bot_agent_card`` for the server-
side template).

Coverage:
  * Sub-models — AgentCapability / AgentEndpoint / AgentAuthentication
    round-trip independently
  * AgentCard parses the real platform shape (spec-compliant)
  * AgentCard parses partial / missing-field inputs without crashing
  * to_dict() emits spec-compliant JSON (capabilities is a dict of
    booleans, x-agoradigest extension block carries the rest)
  * Roundtrip: from_dict(to_dict(card)) ≡ card (semantically)
"""

from __future__ import annotations

import json

import pytest

from a2a_dm.agent_card import (
    AgentAuthentication,
    AgentCapability,
    AgentCard,
    AgentEndpoint,
)


# ── Sub-models ───────────────────────────────────────────────────


def test_capability_roundtrip():
    cap = AgentCapability(
        name="a2a-dm",
        enabled=True,
        description="Agent-to-agent DM",
        tags=["a2a", "dm"],
    )
    out = cap.to_dict()
    back = AgentCapability.from_dict(out)
    assert back.name == "a2a-dm"
    assert back.enabled is True
    assert back.description == "Agent-to-agent DM"
    assert back.tags == ["a2a", "dm"]


def test_capability_defensive_on_non_dict():
    """Server may send a bare string for a capability list; SDK must
    not crash. Cap becomes name-only, enabled=True by default."""
    cap = AgentCapability.from_dict("a2a-dm")
    assert cap.name == "a2a-dm"


def test_endpoint_roundtrip():
    ep = AgentEndpoint(
        kind="dm",
        url="https://api.agoradigest.com/a2a/v1/messages",
        description="DM gateway",
        auth_required=True,
    )
    back = AgentEndpoint.from_dict(ep.to_dict())
    assert back.kind == "dm"
    assert back.url.endswith("/messages")
    assert back.auth_required is True


def test_authentication_roundtrip():
    auth = AgentAuthentication(
        schemes=["bearer"],
        bearer_format="bt_xxx",
        future_scheme="oauth2",
    )
    out = auth.to_dict()
    assert out["schemes"] == ["bearer"]
    assert out["bearerFormat"] == "bt_xxx"
    assert out["x-agoradigest-future-scheme"] == "oauth2"
    back = AgentAuthentication.from_dict(out)
    assert back.schemes == ["bearer"]
    assert back.bearer_format == "bt_xxx"
    assert back.future_scheme == "oauth2"


# ── AgentCard — parse the real platform shape ───────────────────


def test_card_parses_platform_shape():
    """The fixture below mirrors what
    ``GET /bots/bestiedog/agent_card.json`` actually returns
    (see apps/api/src/routes/a2a.py)."""
    platform = {
        "name": "bestiedog",
        "description": "AgoraDigest agent — top-rated in engineering",
        "url": "https://api.agoradigest.com/a2a/v1/bots/bestiedog/message:send",
        "version": "1.0.0",
        "documentationUrl": "https://agoradigest.com/about",
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [{"id": "code-review", "name": "Code review"}],
        "authentication": {
            "schemes": [],
            "x-agoradigest-future-scheme": "bearer",
        },
        "x-agoradigest": {
            "bot_id": "bestiedog",
            "owner": "tyler",
            "is_verified": True,
            "vertical": "engineering",
            "tags": ["engineering", "code-review"],
            "avatar_url": "https://agoradigest.com/avatars/bestiedog.png",
            "profile_url": "https://agoradigest.com/bots/bestiedog",
        },
    }
    card = AgentCard.from_dict(platform)
    assert card.name == "bestiedog"
    assert card.bot_id == "bestiedog"
    assert card.vertical == "engineering"
    assert card.tags == ["engineering", "code-review"]
    assert card.agent_version == "1.0.0"
    assert card.documentation_url == "https://agoradigest.com/about"
    assert card.owner == "tyler"
    assert card.avatar_url.endswith("bestiedog.png")
    # The 3 spec capability flags landed as AgentCapability entries.
    assert "streaming" in card.capability_names
    assert "stateTransitionHistory" in card.capability_names
    assert "pushNotifications" not in card.capability_names  # False
    # Top-level url surfaced as a "dm" endpoint.
    dm = card.endpoint_by_kind("dm")
    assert dm is not None
    assert dm.url.endswith("/message:send")
    # Skills preserved as-is.
    assert card.skills == [{"id": "code-review", "name": "Code review"}]
    # Authentication parsed.
    assert card.authentication is not None
    assert card.authentication.future_scheme == "bearer"


def test_card_parses_empty_input():
    """Defensive — server may return ``{}`` for a missing bot before
    the route synthesises a real card."""
    card = AgentCard.from_dict({})
    assert card.name == ""
    assert card.bot_id is None
    assert card.capabilities == []
    assert card.endpoints == []


def test_card_parses_non_dict_input():
    """Pure garbage in (string, None, list) → empty card, no crash."""
    assert AgentCard.from_dict(None).name == ""
    assert AgentCard.from_dict("not json").name == ""
    assert AgentCard.from_dict(["a", "list"]).name == ""


def test_card_parses_partial_card_just_name():
    """The minimum legal card per A2A spec is ``{"name": "..."}``."""
    card = AgentCard.from_dict({"name": "minimal"})
    assert card.name == "minimal"
    assert card.agent_version == "1.0.0"  # default
    assert card.capabilities == []


# ── to_dict() serialises to spec shape ─────────────────────────


def test_to_dict_uses_spec_dict_for_capabilities():
    """The 3 A2A spec capabilities (streaming, pushNotifications,
    stateTransitionHistory) must land as a dict of booleans at the
    top level, NOT as a list. Spec-strict A2A parsers reject the
    list shape."""
    card = (
        AgentCard(name="agent")
        .add_capability("streaming", enabled=True)
        .add_capability("pushNotifications", enabled=False)
    )
    out = card.to_dict()
    assert out["capabilities"] == {
        "streaming": True,
        "pushNotifications": False,
        "stateTransitionHistory": False,  # default False if not added
    }


def test_to_dict_lifts_dm_endpoint_to_top_level_url():
    """A2A spec requires a single top-level ``url`` field. The model
    has multiple endpoints; ``to_dict()`` lifts the first one with
    kind="dm" to spec-compliant top-level, and emits ALL of them
    under x-agoradigest.endpoints."""
    card = (
        AgentCard(name="agent")
        .add_endpoint("profile", "https://x.com/profile")
        .add_endpoint("dm", "https://x.com/dm")
        .add_endpoint("badge", "https://x.com/badge.svg")
    )
    out = card.to_dict()
    assert out["url"] == "https://x.com/dm"
    # All three preserved under x-agoradigest.
    assert {
        ep["kind"] for ep in out["x-agoradigest"]["endpoints"]
    } == {"profile", "dm", "badge"}


def test_to_dict_stashes_agoradigest_extensions():
    card = AgentCard(
        name="agent",
        bot_id="bestiedog",
        vertical="engineering",
        tags=["e", "f"],
        owner="tyler",
        avatar_url="https://x.com/a.png",
    )
    out = card.to_dict()
    x = out["x-agoradigest"]
    assert x["bot_id"] == "bestiedog"
    assert x["vertical"] == "engineering"
    assert x["tags"] == ["e", "f"]
    assert x["owner"] == "tyler"
    assert x["avatar_url"] == "https://x.com/a.png"


def test_to_dict_omits_empty_extensions():
    """When AgoraDigest extension fields are all empty, the
    x-agoradigest block is omitted entirely so the JSON stays
    clean for vanilla A2A clients."""
    card = AgentCard(name="agent")
    out = card.to_dict()
    assert "x-agoradigest" not in out
    assert out["name"] == "agent"


# ── Roundtrip ──────────────────────────────────────────────────


def test_roundtrip_pythonic_to_spec_to_pythonic():
    """Build a card, serialise, parse back — should be semantically
    equivalent (lists/dicts preserved, capabilities re-bucketed)."""
    original = (
        AgentCard(
            name="bestiedog",
            bot_id="bestiedog",
            description="Engineering arena top performer",
            vertical="engineering",
            tags=["engineering", "code-review"],
            owner="tyler",
            avatar_url="https://agoradigest.com/avatars/bestiedog.png",
            authentication=AgentAuthentication(
                schemes=[], future_scheme="bearer",
            ),
        )
        .add_capability("streaming", enabled=True)
        .add_capability("stateTransitionHistory", enabled=True)
        .add_capability("a2a-dm", enabled=True, description="DM gateway")
        .add_endpoint("dm", "https://api.agoradigest.com/a2a/v1/messages")
        .add_endpoint("profile", "https://agoradigest.com/bots/bestiedog")
    )
    js = original.to_json()
    back = AgentCard.from_json(js)

    assert back.name == original.name
    assert back.bot_id == original.bot_id
    assert back.vertical == original.vertical
    assert sorted(back.tags) == sorted(original.tags)
    assert back.owner == original.owner
    assert back.avatar_url == original.avatar_url
    # Capability set matches (order-independent — the spec dict is
    # unordered).
    assert back.capability_names == original.capability_names
    # Endpoints — at least the DM and profile survived.
    dm = back.endpoint_by_kind("dm")
    assert dm is not None and dm.url.endswith("/messages")
    profile = back.endpoint_by_kind("profile")
    assert profile is not None and profile.url.endswith("/bestiedog")
    # Authentication preserved.
    assert back.authentication is not None
    assert back.authentication.future_scheme == "bearer"


def test_from_json_garbage_input():
    """from_json on malformed text → empty card, no crash."""
    assert AgentCard.from_json("").name == ""
    assert AgentCard.from_json("{not json").name == ""
    assert AgentCard.from_json("null").name == ""


# ── client.card property ──────────────────────────────────────


def test_agent_client_card_default_none():
    """client.card defaults to None — the SDK doesn't fabricate one."""
    from a2a_dm import AgentClient

    c = AgentClient(token="bt_test")
    assert c.card is None


def test_agent_client_card_constructor_arg():
    """Pass card= to the constructor to advertise the user's agent."""
    from a2a_dm import AgentClient

    card = AgentCard(name="bestiedog", bot_id="bestiedog", vertical="engineering")
    c = AgentClient(token="bt_test", card=card)
    assert c.card is card
    assert c.card.bot_id == "bestiedog"


def test_agent_client_card_is_mutable():
    """Late-binding the card after construction is allowed; daemons
    pick up whatever's set when they read client.card."""
    from a2a_dm import AgentClient

    c = AgentClient(token="bt_test")
    c.card = AgentCard(name="late", bot_id="late")
    assert c.card.bot_id == "late"


# ── builder fluency ───────────────────────────────────────────


def test_add_capability_returns_self_for_chaining():
    card = (
        AgentCard(name="x")
        .add_capability("a")
        .add_capability("b")
        .add_endpoint("dm", "u")
    )
    assert card.capability_names == {"a", "b"}
    assert card.endpoint_by_kind("dm").url == "u"


# ── v0.2.6 — discover + publish + defensive to_json ────────────────


import responses


def test_to_json_defensive_on_unserializable_field():
    """v0.2.6 — laobaigan reported to_json() TypeError when something
    non-JSON-serializable landed in skills/etc. The defensive
    `default=str` makes it stringify instead of crash.

    The vulnerable path is ``skills`` (list of free-form dicts
    forwarded verbatim from the constructor) — easy for operators
    to put a ``datetime`` / UUID / custom class in there.
    """
    from datetime import datetime

    card = AgentCard(
        name="x",
        # Stash a datetime inside a skill dict — without default=str,
        # json.dumps would raise TypeError trying to serialise it.
        skills=[{"id": "s1", "created": datetime(2026, 5, 27, 10, 30)}],
    )
    # Should not raise; the datetime becomes its str() form.
    js = card.to_json()
    assert "2026-05-27" in js


def test_agent_card_api_attached_to_client():
    """v0.2.6 — client.agent_card namespace exists and has the
    discover/publish surface."""
    from a2a_dm import AgentClient

    c = AgentClient(token="bt_test", bot_id="bestiedog")
    assert c.agent_card is not None
    assert callable(c.agent_card.discover)
    assert callable(c.agent_card.publish)
    assert callable(c.agent_card.discover_url)


@responses.activate
def test_agent_card_discover_fetches_and_parses():
    """v0.2.6 — discover(bot_id) hits GET /bots/{id}/agent_card.json
    and returns a parsed AgentCard."""
    import responses as _r
    from a2a_dm import AgentClient

    _r.add(
        _r.GET,
        "https://api.agoradigest.com/bots/laobaigan/agent_card.json",
        json={
            "name": "Laobaigan",
            "description": "Test agent",
            "url": "https://x.com/dm",
            "version": "1.0.0",
            "capabilities": {"streaming": True},
            "x-agoradigest": {
                "bot_id": "laobaigan",
                "vertical": "ai",
            },
        },
        status=200,
    )
    client = AgentClient(token="bt_test", bot_id="bestiedog")
    card = client.agent_card.discover("laobaigan")
    assert card.name == "Laobaigan"
    assert card.bot_id == "laobaigan"
    assert card.vertical == "ai"
    assert "streaming" in card.capability_names


def test_agent_card_discover_rejects_empty_bot_id():
    from a2a_dm import AgentClient

    c = AgentClient(token="bt_test")
    with pytest.raises(ValueError):
        c.agent_card.discover("")


@responses.activate
def test_agent_card_publish_uses_client_card_by_default():
    """v0.2.6 — publish() with no args uses client.card."""
    import responses as _r
    from a2a_dm import AgentClient

    _r.add(
        _r.PUT,
        "https://api.agoradigest.com/bots/bestiedog/agent_card.json",
        json={
            "ok": True,
            "bot_id": "bestiedog",
            "card": {
                "name": "bestiedog",
                "version": "1.0.0",
                "x-agoradigest": {"bot_id": "bestiedog"},
            },
            "source": "operator",
        },
        status=200,
    )
    card = AgentCard(name="bestiedog", bot_id="bestiedog")
    client = AgentClient(token="bt_test", bot_id="bestiedog", card=card)
    published = client.agent_card.publish()
    assert published.name == "bestiedog"
    # publish() stores the returned card on client.card for round-trip.
    assert client.card is published


def test_agent_card_publish_rejects_missing_bot_id():
    from a2a_dm import AgentClient

    c = AgentClient(token="bt_test", card=AgentCard(name="x", bot_id="x"))
    # No bot_id set on client → can't publish (we don't know the path).
    with pytest.raises(ValueError, match="bot_id"):
        c.agent_card.publish()


def test_agent_card_publish_rejects_missing_card():
    from a2a_dm import AgentClient

    c = AgentClient(token="bt_test", bot_id="bestiedog")
    # No card set, no card= passed.
    with pytest.raises(ValueError, match="card"):
        c.agent_card.publish()


def test_agent_card_publish_with_explicit_card_overrides_client_card():
    """publish(card=...) replaces client.card with the published one."""
    import responses as _r
    from a2a_dm import AgentClient

    @_r.activate
    def go():
        _r.add(
            _r.PUT,
            "https://api.agoradigest.com/bots/bestiedog/agent_card.json",
            json={
                "ok": True,
                "bot_id": "bestiedog",
                "card": {
                    "name": "v2",
                    "version": "1.0.0",
                    "x-agoradigest": {"bot_id": "bestiedog"},
                },
                "source": "operator",
            },
            status=200,
        )
        old = AgentCard(name="v1", bot_id="bestiedog")
        new = AgentCard(name="v2", bot_id="bestiedog")
        client = AgentClient(token="bt_test", bot_id="bestiedog", card=old)
        published = client.agent_card.publish(card=new)
        assert published.name == "v2"
        assert client.card.name == "v2"

    go()
