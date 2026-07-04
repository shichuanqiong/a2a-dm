"""SDK friends API tests (Phase 6.2).

Coverage:
  * Friend.from_dict round-trip + display_name fallback chain
  * client.friends.list / search / get / has / add / update / remove
    happy paths via mocked HTTP
  * add() idempotency hint (created bool returned by server)
  * update() with refresh_card=True flag plumbing
  * remove() idempotent True/False mapping
  * get() returns None on 404 (so `client.friends.has()` works)
  * Error mapping passes through (404 → NotFoundError, 409 → ConflictError)
"""

from __future__ import annotations

import pytest
import responses

from a2a_dm import AgentClient, Friend
from a2a_dm.exceptions import ConflictError, NotFoundError, ValidationError


# ── Friend dataclass ────────────────────────────────────────────


def _sample_friend_payload(**overrides) -> dict:
    base = {
        "owner_bot_id": "bot_ext_laobaigan",
        "friend_bot_id": "bestiedog",
        "label": "bestie",
        "note": "dev partner",
        "groups": ["dev-team"],
        "tags": ["python", "docker"],
        "agent_card_snapshot": {"name": "bestiedog the agent"},
        "added_at": "2026-05-30T12:00:00+00:00",
        "client_origin_at": "2026-05-25T08:30:00+00:00",
        "last_contact_at": None,
        "discoverable": False,
        "notify": False,
    }
    base.update(overrides)
    return base


def test_friend_from_dict_round_trips_all_fields():
    payload = _sample_friend_payload()
    f = Friend.from_dict(payload)
    assert f.friend_bot_id == "bestiedog"
    assert f.owner_bot_id == "bot_ext_laobaigan"
    assert f.label == "bestie"
    assert f.note == "dev partner"
    assert f.groups == ["dev-team"]
    assert f.tags == ["python", "docker"]
    assert f.agent_card_snapshot == {"name": "bestiedog the agent"}
    assert f.added_at == "2026-05-30T12:00:00+00:00"
    assert f.client_origin_at == "2026-05-25T08:30:00+00:00"
    assert f.last_contact_at is None
    assert f.discoverable is False
    assert f.notify is False


def test_friend_from_dict_handles_missing_arrays():
    """Server might send tags=None on legacy rows; client must
    not crash."""
    f = Friend.from_dict({"friend_bot_id": "x"})
    assert f.tags == []
    assert f.groups == []


def test_friend_from_dict_handles_non_dict():
    """Robustness against unexpected payloads (e.g. server returns
    string error in 200 body)."""
    f = Friend.from_dict("garbage")
    assert f.friend_bot_id == ""


def test_friend_display_name_prefers_label():
    f = Friend.from_dict(_sample_friend_payload(label="bestie"))
    assert f.display_name == "bestie"


def test_friend_display_name_falls_back_to_card_name():
    """No label → use agent_card_snapshot.name. Lets ``client.friends
    .add("x")`` (no label) still produce a sensible UI label."""
    f = Friend.from_dict(
        _sample_friend_payload(label=None, agent_card_snapshot={"name": "card-name"})
    )
    assert f.display_name == "card-name"


def test_friend_display_name_final_fallback_to_bot_id():
    """Worst case: no label, no card → bot_id is the only thing left."""
    f = Friend.from_dict(
        _sample_friend_payload(
            label=None, agent_card_snapshot=None, friend_bot_id="raw_id"
        )
    )
    assert f.display_name == "raw_id"


# ── client.friends.list ─────────────────────────────────────────


@responses.activate
def test_friends_list_parses_rows():
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends?limit=200",
        json={
            "count": 2,
            "friends": [
                _sample_friend_payload(friend_bot_id="bestiedog"),
                _sample_friend_payload(friend_bot_id="laobaigan", label=None),
            ],
        },
        status=200,
        match_querystring=False,
    )
    client = AgentClient(token="bt_test", bot_id="bot_ext_laobaigan")
    rows = client.friends.list()
    assert len(rows) == 2
    assert rows[0].friend_bot_id == "bestiedog"
    assert isinstance(rows[0], Friend)


@responses.activate
def test_friends_list_empty():
    """Empty server response → empty list (not an error)."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends?limit=200",
        json={"count": 0, "friends": []},
        status=200,
        match_querystring=False,
    )
    client = AgentClient(token="bt_test")
    assert client.friends.list() == []


@responses.activate
def test_friends_list_custom_limit():
    """`limit=50` flows through to the URL query."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends?limit=50",
        json={"count": 0, "friends": []},
        status=200,
        match_querystring=False,
    )
    client = AgentClient(token="bt_test")
    client.friends.list(limit=50)
    # responses lets us assert which URL was actually hit.
    assert "limit=50" in responses.calls[0].request.url


# ── client.friends.search ───────────────────────────────────────


@responses.activate
def test_friends_search_passes_query_param():
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/search",
        json={
            "count": 1,
            "friends": [_sample_friend_payload(tags=["docker", "railway"])],
            "query": "railway",
        },
        status=200,
        match_querystring=False,
    )
    client = AgentClient(token="bt_test")
    rows = client.friends.search("railway")
    assert len(rows) == 1
    assert "railway" in rows[0].tags
    # The query was URL-encoded into the request
    assert "q=railway" in responses.calls[0].request.url


@responses.activate
def test_friends_search_url_encodes_special_chars():
    """Search terms with spaces / unicode must be properly encoded."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/search",
        json={"count": 0, "friends": [], "query": "hong kong"},
        status=200,
        match_querystring=False,
    )
    client = AgentClient(token="bt_test")
    client.friends.search("hong kong")
    # urlencode replaces space with +
    assert "q=hong+kong" in responses.calls[0].request.url


# ── client.friends.get + has ────────────────────────────────────


@responses.activate
def test_friends_get_returns_friend():
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _sample_friend_payload()},
        status=200,
    )
    client = AgentClient(token="bt_test")
    f = client.friends.get("bestiedog")
    assert f is not None
    assert f.friend_bot_id == "bestiedog"


@responses.activate
def test_friends_get_returns_none_on_404():
    """Mapping 404 to None instead of raising lets `has()` be a
    one-liner and "do I have X friended?" common case stays clean."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/stranger",
        json={"detail": {"error": "friend_not_found"}},
        status=404,
    )
    client = AgentClient(token="bt_test")
    assert client.friends.get("stranger") is None


@responses.activate
def test_friends_has_uses_get_under_the_hood():
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _sample_friend_payload()},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/stranger",
        json={"detail": {"error": "friend_not_found"}},
        status=404,
    )
    client = AgentClient(token="bt_test")
    assert client.friends.has("bestiedog") is True
    assert client.friends.has("stranger") is False


# ── client.friends.add ──────────────────────────────────────────


@responses.activate
def test_friends_add_minimal():
    """Just a bot_id — everything else optional."""
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/friends",
        json={"friend": _sample_friend_payload(), "created": True},
        status=200,
    )
    client = AgentClient(token="bt_test")
    f = client.friends.add("bestiedog")
    assert f.friend_bot_id == "bestiedog"
    # Body sent to the server should contain just the one field.
    import json as _json
    body = _json.loads(responses.calls[0].request.body)
    assert body == {"friend_bot_id": "bestiedog"}


@responses.activate
def test_friends_add_with_full_payload():
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/friends",
        json={"friend": _sample_friend_payload(), "created": True},
        status=200,
    )
    client = AgentClient(token="bt_test")
    f = client.friends.add(
        "bestiedog",
        label="bestie",
        note="dev partner",
        groups=["dev-team"],
        tags=["python"],
        agent_card_snapshot={"name": "bestiedog"},
        client_origin_at="2026-05-25T08:30:00Z",
        discoverable=True,
        notify=True,
    )
    import json as _json
    body = _json.loads(responses.calls[0].request.body)
    assert body["friend_bot_id"] == "bestiedog"
    assert body["label"] == "bestie"
    assert body["groups"] == ["dev-team"]
    assert body["discoverable"] is True
    assert body["notify"] is True
    assert body["client_origin_at"] == "2026-05-25T08:30:00Z"


@responses.activate
def test_friends_add_404_raises_not_found():
    """Adding a non-existent bot → NotFoundError (no silent map)."""
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/friends",
        json={"detail": {"error": "friend_bot_not_found"}},
        status=404,
    )
    client = AgentClient(token="bt_test")
    with pytest.raises(NotFoundError):
        client.friends.add("ghost_bot")


@responses.activate
def test_friends_add_409_raises_conflict():
    """Hitting the per-owner cap → ConflictError."""
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/friends",
        json={
            "detail": {
                "error": "friend_cap_reached",
                "cap": 200,
            }
        },
        status=409,
    )
    client = AgentClient(token="bt_test")
    with pytest.raises(ConflictError):
        client.friends.add("anyone")


@responses.activate
def test_friends_add_400_raises_validation():
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/friends",
        json={"detail": {"error": "cannot_friend_self"}},
        status=400,
    )
    client = AgentClient(token="bt_test", bot_id="bot_test")
    with pytest.raises(ValidationError):
        client.friends.add("bot_test")


# ── client.friends.update ───────────────────────────────────────


@responses.activate
def test_friends_update_only_sends_supplied_fields():
    """Omitted kwargs must not show up in the body — server's
    PATCH semantics depend on this for "leave field alone"."""
    responses.add(
        responses.PATCH,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _sample_friend_payload(label="renamed")},
        status=200,
    )
    client = AgentClient(token="bt_test")
    client.friends.update("bestiedog", label="renamed")
    import json as _json
    body = _json.loads(responses.calls[0].request.body)
    assert body == {"label": "renamed"}


@responses.activate
def test_friends_update_refresh_card_flag():
    responses.add(
        responses.PATCH,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _sample_friend_payload()},
        status=200,
    )
    client = AgentClient(token="bt_test")
    client.friends.update("bestiedog", refresh_card=True)
    import json as _json
    body = _json.loads(responses.calls[0].request.body)
    assert body == {"refresh_card": True}


@responses.activate
def test_friends_update_empty_call_is_valid():
    """PATCH with no kwargs is a no-op the server accepts (e.g.
    pinging the row to touch updated_at). Body is empty dict."""
    responses.add(
        responses.PATCH,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _sample_friend_payload()},
        status=200,
    )
    client = AgentClient(token="bt_test")
    client.friends.update("bestiedog")
    import json as _json
    body = _json.loads(responses.calls[0].request.body)
    assert body == {}


@responses.activate
def test_friends_update_404_raises():
    responses.add(
        responses.PATCH,
        "https://api.agoradigest.com/a2a/v1/friends/ghost",
        json={"detail": {"error": "friend_not_found"}},
        status=404,
    )
    client = AgentClient(token="bt_test")
    with pytest.raises(NotFoundError):
        client.friends.update("ghost", label="x")


# ── client.friends.remove ───────────────────────────────────────


@responses.activate
def test_friends_remove_returns_true_on_actual_delete():
    responses.add(
        responses.DELETE,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"deleted": True, "friend_bot_id": "bestiedog"},
        status=200,
    )
    client = AgentClient(token="bt_test")
    assert client.friends.remove("bestiedog") is True


@responses.activate
def test_friends_remove_returns_false_on_idempotent_noop():
    """Deleting a friend that wasn't there returns 200 with
    deleted=False — the SDK propagates the bool so callers can
    skip "did I really delete something?" log lines."""
    responses.add(
        responses.DELETE,
        "https://api.agoradigest.com/a2a/v1/friends/ghost",
        json={"deleted": False, "friend_bot_id": "ghost"},
        status=200,
    )
    client = AgentClient(token="bt_test")
    assert client.friends.remove("ghost") is False
