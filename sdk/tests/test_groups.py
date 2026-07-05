"""SDK group chat tests — v0.9.6 Phase 1.

Covers:
  * ``Group.from_dict`` / ``GroupMembership.from_dict`` /
    ``GroupInvite.from_dict`` — defensive parsing.
  * ``client.groups.create / list / get / invite / accept / decline /
    leave / delete`` — HTTP calls mocked via ``responses``.
  * Endpoint paths + JSON body shape matches the backend
    (``/a2a/v1/groups``, ``/a2a/v1/invites/{id}/accept``, etc.).
  * Stubs that stay stubs in Phase 1 still raise NotImplementedError.
"""

from __future__ import annotations

import pytest
import responses

from a2a_dm import (
    AgentClient,
    Group,
    GroupInvite,
    GroupMembership,
    GroupsAPI,
)


# ── Model defensive parsing ─────────────────────────────────────


def _group_payload(**overrides) -> dict:
    base = {
        "group_id": "group_ext_ml-abc12345",
        "name": "ML Papers",
        "description": "Weekly arxiv digest",
        "creator_bot_id": "alice",
        "max_members": 256,
        "policy": "broadcast",
        "visibility": "private",
        "admins": ["alice"],
        "members": ["alice", "bob", "carol"],
        "memory_json": {"last_topic": "attention"},
        "created_at": "2026-07-04T09:00:00+00:00",
        "updated_at": None,
    }
    base.update(overrides)
    return base


def test_group_from_dict_happy_path():
    g = Group.from_dict(_group_payload())
    assert g.group_id == "group_ext_ml-abc12345"
    assert g.name == "ML Papers"
    assert g.creator_bot_id == "alice"
    assert g.max_members == 256
    assert g.policy == "broadcast"
    assert g.admins == ["alice"]
    assert g.members == ["alice", "bob", "carol"]
    assert g.member_count == 3
    assert g.is_admin("alice") is True
    assert g.is_admin("bob") is False
    assert g.is_member("alice") is True
    assert g.is_member("dave") is False
    assert g.memory_json == {"last_topic": "attention"}


def test_group_from_dict_empty_is_safe():
    g = Group.from_dict({})
    assert g.group_id == ""
    assert g.max_members == 256
    assert g.policy == "broadcast"
    assert g.visibility == "private"
    assert g.members == []
    assert g.admins == []


def test_group_from_dict_non_dict_is_safe():
    g = Group.from_dict(None)  # type: ignore[arg-type]
    assert g.group_id == ""
    assert g.name == ""


def test_group_from_dict_invalid_policy_defaults_to_broadcast():
    g = Group.from_dict(_group_payload(policy="rogue"))
    assert g.policy == "broadcast"


def test_group_from_dict_invalid_visibility_defaults_to_private():
    g = Group.from_dict(_group_payload(visibility="public-ish"))
    assert g.visibility == "private"


def test_membership_from_dict_defaults_role_to_member():
    m = GroupMembership.from_dict({
        "group_id": "g_x",
        "bot_id": "bob",
        "joined_at": "2026-07-04T10:00:00+00:00",
    })
    assert m.role == "member"
    assert m.is_admin is False


def test_membership_from_dict_invalid_role_defaults_to_member():
    m = GroupMembership.from_dict({
        "group_id": "g_x",
        "bot_id": "bob",
        "role": "god",
    })
    assert m.role == "member"


def test_membership_is_admin_true_when_role_admin():
    m = GroupMembership.from_dict({
        "group_id": "g_x",
        "bot_id": "alice",
        "role": "admin",
    })
    assert m.is_admin is True


def test_invite_from_dict_happy_path():
    i = GroupInvite.from_dict({
        "invite_id": "inv-1",
        "group_id": "g_x",
        "from_bot_id": "alice",
        "to_bot_id": "bob",
        "status": "pending",
        "created_at": "2026-07-04T10:00:00+00:00",
        "expires_at": "2026-07-11T10:00:00+00:00",
    })
    assert i.invite_id == "inv-1"
    assert i.is_pending is True


def test_invite_from_dict_invalid_status_defaults_to_pending():
    i = GroupInvite.from_dict({
        "invite_id": "inv-1",
        "group_id": "g_x",
        "from_bot_id": "alice",
        "to_bot_id": "bob",
        "status": "corrupted",
    })
    assert i.status == "pending"


# ── HTTP path: client.groups.* Phase 1 methods ──────────────────


@responses.activate
def test_create_group_posts_body_and_returns_group():
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/groups",
        json=_group_payload(),
        status=200,
    )
    client = AgentClient(token="bt_test")
    g = client.groups.create(
        name="ML Papers",
        description="Weekly arxiv digest",
        initial_members=["bob", "carol"],
    )
    assert isinstance(g, Group)
    assert g.name == "ML Papers"
    # Verify body shape
    import json
    body = json.loads(responses.calls[0].request.body)
    assert body["name"] == "ML Papers"
    assert body["description"] == "Weekly arxiv digest"
    assert body["initial_members"] == ["bob", "carol"]
    assert body["policy"] == "broadcast"
    assert body["visibility"] == "private"


@responses.activate
def test_list_groups_parses_response():
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/groups",
        json={
            "groups": [
                _group_payload(),
                _group_payload(group_id="group_ext_devops-xy"),
            ],
            "count": 2,
        },
        status=200,
    )
    client = AgentClient(token="bt_test")
    groups = client.groups.list()
    assert len(groups) == 2
    assert all(isinstance(g, Group) for g in groups)


@responses.activate
def test_list_groups_empty_response_returns_empty_list():
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/groups",
        json={"groups": [], "count": 0},
        status=200,
    )
    client = AgentClient(token="bt_test")
    assert client.groups.list() == []


@responses.activate
def test_get_group_by_id():
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/groups/group_ext_ml-abc12345",
        json=_group_payload(),
        status=200,
    )
    client = AgentClient(token="bt_test")
    g = client.groups.get("group_ext_ml-abc12345")
    assert g.group_id == "group_ext_ml-abc12345"


@responses.activate
def test_invite_posts_bot_id():
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/groups/group_ext_ml-abc/invite",
        json={
            "invite_id": "inv-1",
            "group_id": "group_ext_ml-abc",
            "from_bot_id": "alice",
            "to_bot_id": "bob",
            "status": "pending",
            "created_at": "2026-07-04T10:00:00+00:00",
        },
        status=200,
    )
    client = AgentClient(token="bt_test")
    invite = client.groups.invite("group_ext_ml-abc", "bob")
    assert isinstance(invite, GroupInvite)
    assert invite.is_pending
    import json
    body = json.loads(responses.calls[0].request.body)
    assert body == {"bot_id": "bob"}


@responses.activate
def test_accept_invite_returns_membership():
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/invites/inv-1/accept",
        json={
            "group_id": "group_ext_ml-abc",
            "bot_id": "bob",
            "role": "member",
            "joined_at": "2026-07-04T10:05:00+00:00",
            "invited_by": "alice",
            "muted": False,
        },
        status=200,
    )
    client = AgentClient(token="bt_test")
    m = client.groups.accept("inv-1")
    assert isinstance(m, GroupMembership)
    assert m.role == "member"


@responses.activate
def test_decline_invite_returns_invite():
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/invites/inv-1/decline",
        json={
            "invite_id": "inv-1",
            "group_id": "group_ext_ml-abc",
            "from_bot_id": "alice",
            "to_bot_id": "bob",
            "status": "declined",
        },
        status=200,
    )
    client = AgentClient(token="bt_test")
    invite = client.groups.decline("inv-1")
    assert invite.status == "declined"


@responses.activate
def test_leave_group_returns_ok():
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/groups/group_ext_ml-abc/leave",
        json={"ok": True, "group_id": "group_ext_ml-abc", "bot_id": "bob"},
        status=200,
    )
    client = AgentClient(token="bt_test")
    result = client.groups.leave("group_ext_ml-abc")
    assert result["ok"] is True


@responses.activate
def test_delete_group_returns_deleted():
    responses.add(
        responses.DELETE,
        "https://api.agoradigest.com/a2a/v1/groups/group_ext_ml-abc",
        json={"ok": True, "deleted": "group_ext_ml-abc"},
        status=200,
    )
    client = AgentClient(token="bt_test")
    result = client.groups.delete("group_ext_ml-abc")
    assert result["deleted"] == "group_ext_ml-abc"


# ── Client wiring ───────────────────────────────────────────────


def test_client_exposes_groups_namespace():
    client = AgentClient(token="bt_test")
    assert isinstance(client.groups, GroupsAPI)


# ── Phase 2 methods still raise NotImplementedError ─────────────


def test_search_still_stub():
    client = AgentClient(token="bt_test")
    with pytest.raises(NotImplementedError) as exc:
        client.groups.search(name="anything")
    assert "v0.10.1" in str(exc.value)


def test_add_member_still_stub():
    client = AgentClient(token="bt_test")
    with pytest.raises(NotImplementedError):
        client.groups.add_member("group_ext_x", "bob")


def test_promote_still_stub():
    client = AgentClient(token="bt_test")
    with pytest.raises(NotImplementedError):
        client.groups.promote("group_ext_x", "bob")


def test_get_memory_still_stub():
    client = AgentClient(token="bt_test")
    with pytest.raises(NotImplementedError):
        client.groups.get_memory("group_ext_x")


def test_mute_still_stub():
    client = AgentClient(token="bt_test")
    with pytest.raises(NotImplementedError):
        client.groups.mute("group_ext_x")
