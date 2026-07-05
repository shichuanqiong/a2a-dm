"""Tool handlers — wrap the ``a2a_dm`` SDK into Hermes tool shape.

Every handler:
  * Takes ``args: dict`` (the LLM's arguments).
  * Returns ``json.dumps(...)`` on success AND on error.
  * Never raises — exceptions caught and returned as ``{"error": ...}``.

The shared ``AgentClient`` is lazy-initialised from env on first call
(``AGORADIGEST_TOKEN`` + ``AGORADIGEST_BOT_ID``). That lets Hermes
import the plugin without valid credentials — the wizard populates
them, then subsequent tool calls work.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Optional

from a2a_dm import AgentClient


# ── Shared client (lazy, thread-safe) ─────────────────────────────

_client_lock = threading.Lock()
_client: Optional[AgentClient] = None


def _get_client() -> Optional[AgentClient]:
    """Return a cached ``AgentClient`` or None if env is missing.

    Handlers return an error JSON rather than raising when creds
    aren't set yet — Hermes shouldn't crash just because the plugin
    hasn't been configured.
    """
    global _client
    with _client_lock:
        if _client is not None:
            return _client
        token = os.environ.get("AGORADIGEST_TOKEN")
        bot_id = os.environ.get("AGORADIGEST_BOT_ID")
        if not token:
            return None
        _client = AgentClient(token=token, bot_id=bot_id or None)
        return _client


def _err(msg: str) -> str:
    """Consistent error envelope."""
    return json.dumps({"error": msg})


def _no_client() -> str:
    return _err(
        "a2a-dm plugin not configured. Set AGORADIGEST_TOKEN "
        "(and optionally AGORADIGEST_BOT_ID) in ~/.hermes/.env and "
        "restart the gateway."
    )


# ── 1:1 DM tools ──────────────────────────────────────────────────


def send_dm(args: dict, **kwargs: Any) -> str:
    client = _get_client()
    if not client:
        return _no_client()
    target = (args.get("target") or "").strip()
    text = args.get("text") or ""
    if not target or not text:
        return _err("Need both target and text.")
    try:
        task = client.dm.send(target=target, text=text)
        return json.dumps({
            "task_id": task.id,
            "target": target,
            "state": task.state,
            "target_online": task.target_online,
        })
    except Exception as e:  # noqa: BLE001 — surface any SDK error
        return _err(f"send_dm failed: {e}")


def reply(args: dict, **kwargs: Any) -> str:
    client = _get_client()
    if not client:
        return _no_client()
    task_id = (args.get("task_id") or "").strip()
    text = args.get("text") or ""
    if not task_id or not text:
        return _err("Need both task_id and text.")
    try:
        result = client.dm.reply(task_id, text)
        return json.dumps({
            "task_id": task_id,
            "state": getattr(result, "state", "completed"),
            "replied_at": getattr(result, "replied_at", None),
        })
    except Exception as e:  # noqa: BLE001
        return _err(f"reply failed: {e}")


def get_inbox(args: dict, **kwargs: Any) -> str:
    client = _get_client()
    if not client:
        return _no_client()
    state = args.get("state") or "submitted"
    limit = int(args.get("limit") or 20)
    limit = max(1, min(50, limit))
    try:
        view = client.dm.inbox(state=state, limit=limit)
        tasks = []
        for t in view.tasks:
            tasks.append({
                "task_id": t.id,
                "state": t.state,
                "sender_bot_id": t.sender_bot_id,
                "group_id": t.group_id,
                "is_group_message": t.is_group_message,
                "text": (t.message.text if t.message else "") or "",
                "created_at": t.created_at,
            })
        return json.dumps({"tasks": tasks, "count": len(tasks)})
    except Exception as e:  # noqa: BLE001
        return _err(f"get_inbox failed: {e}")


def get_conversation(args: dict, **kwargs: Any) -> str:
    client = _get_client()
    if not client:
        return _no_client()
    peer = (args.get("peer_bot_id") or "").strip()
    limit = int(args.get("limit") or 20)
    if not peer:
        return _err("Need peer_bot_id.")
    try:
        view = client.conversations.get(peer, limit=limit)
        turns = []
        for m in view.messages:
            turns.append({
                "role": m.role,
                "text": m.text,
                "created_at": m.created_at,
            })
        return json.dumps({
            "peer_bot_id": peer,
            "turns": turns,
            "count": len(turns),
        })
    except Exception as e:  # noqa: BLE001
        return _err(f"get_conversation failed: {e}")


# ── Friends tools ─────────────────────────────────────────────────


def list_friends(args: dict, **kwargs: Any) -> str:
    client = _get_client()
    if not client:
        return _no_client()
    try:
        friends = client.friends.list()
        return json.dumps({
            "friends": [
                {
                    "bot_id": f.bot_id,
                    "display_name": f.display_name,
                    "memory": f.memory,
                }
                for f in friends
            ],
            "count": len(friends),
        })
    except Exception as e:  # noqa: BLE001
        return _err(f"list_friends failed: {e}")


def add_friend(args: dict, **kwargs: Any) -> str:
    client = _get_client()
    if not client:
        return _no_client()
    peer = (args.get("peer_bot_id") or "").strip()
    note = args.get("note") or ""
    if not peer:
        return _err("Need peer_bot_id.")
    try:
        memory = {"note": note} if note else {}
        friend = client.friends.add(peer, memory=memory)
        return json.dumps({
            "bot_id": friend.bot_id,
            "display_name": friend.display_name,
        })
    except Exception as e:  # noqa: BLE001
        return _err(f"add_friend failed: {e}")


# ── Group chat tools ──────────────────────────────────────────────


def send_group(args: dict, **kwargs: Any) -> str:
    """Post to a group — same code path as send_dm with a group target
    because the backend fan-out is transparent."""
    client = _get_client()
    if not client:
        return _no_client()
    group_id = (args.get("group_id") or "").strip()
    text = args.get("text") or ""
    if not group_id or not text:
        return _err("Need both group_id and text.")
    if not group_id.startswith("group_"):
        return _err("group_id must start with 'group_'.")
    try:
        task = client.dm.send(target=group_id, text=text)
        return json.dumps({
            "task_id": task.id,
            "group_id": group_id,
            "state": task.state,
        })
    except Exception as e:  # noqa: BLE001
        return _err(f"send_group failed: {e}")


def create_group(args: dict, **kwargs: Any) -> str:
    client = _get_client()
    if not client:
        return _no_client()
    name = (args.get("name") or "").strip()
    description = args.get("description")
    initial_members = args.get("initial_members") or []
    if not name:
        return _err("Need group name.")
    try:
        group = client.groups.create(
            name=name,
            description=description,
            initial_members=initial_members,
        )
        return json.dumps({
            "group_id": group.group_id,
            "name": group.name,
            "member_count": group.member_count,
            "admins": group.admins,
        })
    except Exception as e:  # noqa: BLE001
        return _err(f"create_group failed: {e}")


def list_groups(args: dict, **kwargs: Any) -> str:
    client = _get_client()
    if not client:
        return _no_client()
    try:
        groups = client.groups.list()
        return json.dumps({
            "groups": [
                {
                    "group_id": g.group_id,
                    "name": g.name,
                    "member_count": g.member_count,
                    "is_creator": (
                        g.creator_bot_id == (client.bot_id or "")
                    ),
                }
                for g in groups
            ],
            "count": len(groups),
        })
    except Exception as e:  # noqa: BLE001
        return _err(f"list_groups failed: {e}")


def invite_to_group(args: dict, **kwargs: Any) -> str:
    client = _get_client()
    if not client:
        return _no_client()
    group_id = (args.get("group_id") or "").strip()
    bot_id = (args.get("bot_id") or "").strip()
    if not group_id or not bot_id:
        return _err("Need both group_id and bot_id.")
    try:
        invite = client.groups.invite(group_id, bot_id)
        return json.dumps({
            "invite_id": invite.invite_id,
            "group_id": invite.group_id,
            "to_bot_id": invite.to_bot_id,
            "status": invite.status,
        })
    except Exception as e:  # noqa: BLE001
        return _err(f"invite_to_group failed: {e}")


def accept_invite(args: dict, **kwargs: Any) -> str:
    client = _get_client()
    if not client:
        return _no_client()
    invite_id = (args.get("invite_id") or "").strip()
    if not invite_id:
        return _err("Need invite_id.")
    try:
        membership = client.groups.accept(invite_id)
        return json.dumps({
            "group_id": membership.group_id,
            "role": membership.role,
            "joined_at": membership.joined_at,
        })
    except Exception as e:  # noqa: BLE001
        return _err(f"accept_invite failed: {e}")


def leave_group(args: dict, **kwargs: Any) -> str:
    client = _get_client()
    if not client:
        return _no_client()
    group_id = (args.get("group_id") or "").strip()
    if not group_id:
        return _err("Need group_id.")
    try:
        result = client.groups.leave(group_id)
        return json.dumps(result)
    except Exception as e:  # noqa: BLE001
        return _err(f"leave_group failed: {e}")


# ── Dispatch table used by __init__.register(ctx) ─────────────────

HANDLERS = {
    "a2a_send_dm":         send_dm,
    "a2a_reply":           reply,
    "a2a_get_inbox":       get_inbox,
    "a2a_get_conversation": get_conversation,
    "a2a_list_friends":    list_friends,
    "a2a_add_friend":      add_friend,
    "a2a_send_group":      send_group,
    "a2a_create_group":    create_group,
    "a2a_list_groups":     list_groups,
    "a2a_invite_to_group": invite_to_group,
    "a2a_accept_invite":   accept_invite,
    "a2a_leave_group":     leave_group,
}
