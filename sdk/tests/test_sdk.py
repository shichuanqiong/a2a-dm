"""Phase 1 SDK tests.

Uses `responses` library to mock HTTP — no live API needed. Each
test pins one piece of the contract:

  * Models parse the actual response shapes the API returns
  * Exception subclasses get raised for the right HTTP statuses
  * Structured-error hints get surfaced on the exception
  * Auth/token resolution works (constructor arg + env var)
  * Defensive behaviors (UUID-only inputs, idempotent ack, etc.)

The fixtures use response shapes captured from real prod calls
during the 2026-05-25 round-trip testing, so a future server-side
schema change AND a SDK regression both surface as test failures.
"""

from __future__ import annotations

import json
import os

import pytest
import responses

from a2a_dm import (
    AgentClient,
    AgoraDigestError,
    AuthError,
    ConflictError,
    InboxView,
    Message,
    NotFoundError,
    PermissionError,
    RateLimitError,
    TaskEnvelope,
    ValidationError,
)


# ── Model: Message ────────────────────────────────────────────────────


def test_message_text_concatenates_text_parts():
    """Message.text walks `parts`, joins text-kind ones with double-
    newline. Non-text parts are skipped silently."""
    msg = Message.from_dict({
        "role": "user",
        "parts": [
            {"kind": "text", "text": "First."},
            {"kind": "file", "url": "https://x/y"},  # skipped
            {"kind": "text", "text": "Second."},
            {"kind": "text", "text": "   "},  # whitespace-only, skipped
        ],
    })
    assert msg.text == "First.\n\nSecond."


def test_message_text_empty_when_no_text_parts():
    msg = Message.from_dict({"role": "user", "parts": [{"kind": "file"}]})
    assert msg.text == ""


def test_message_from_dict_defensive_on_garbage():
    """Server returning a malformed message shouldn't crash the SDK."""
    msg = Message.from_dict("not a dict")  # type: ignore[arg-type]
    assert msg.role == "user"
    assert msg.parts == []


# ── Model: TaskEnvelope ──────────────────────────────────────────────


def test_task_envelope_parses_inbox_shape():
    """Inbox response item — the shape we'd see in dm.inbox()."""
    raw = {
        "id": "fea55518-2eb0-402c-9a76-4c883d9728d5",
        "contextId": "abc-question-id",
        "status": {"state": "submitted"},
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": "Hello"}],
        },
        "x-agoradigest": {
            "agent_task_id": "task_abc123",
            "sender_bot_id": "bot_ext_baolongbro",
            "target_bot_id": "bestiedog",
            "tags": ["engineering", "_a2a_dm"],
        },
    }
    t = TaskEnvelope.from_dict(raw)
    assert t.id == "fea55518-2eb0-402c-9a76-4c883d9728d5"
    assert t.context_id == "abc-question-id"
    assert t.state == "submitted"
    assert t.message is not None
    assert t.message.text == "Hello"
    assert t.agent_task_id == "task_abc123"
    assert t.sender_bot_id == "bot_ext_baolongbro"
    assert t.target_bot_id == "bestiedog"
    assert t.tags == ["engineering", "_a2a_dm"]
    assert t.is_terminal is False


def test_task_envelope_state_from_top_level_field():
    """Some endpoints return `state` at the top level instead of
    `status.state`. The model accepts both shapes — we saw this
    inconsistency during the 2026-05-25 testing."""
    raw = {"id": "abc", "state": "working"}
    t = TaskEnvelope.from_dict(raw)
    assert t.state == "working"


def test_task_envelope_reply_text_when_completed():
    """After submit, the envelope's artifacts carry the reply text.
    `.reply_text` is the convenience accessor."""
    raw = {
        "id": "abc",
        "status": {"state": "completed"},
        "artifacts": [
            {"kind": "text", "text": "First chunk."},
            {"kind": "text", "text": "Second chunk."},
            {"kind": "image", "url": "..."},  # skipped
        ],
    }
    t = TaskEnvelope.from_dict(raw)
    assert t.is_completed
    assert t.reply_text == "First chunk.\n\nSecond chunk."


def test_task_envelope_terminal_states():
    for s in ("completed", "failed", "canceled"):
        t = TaskEnvelope.from_dict({"id": "x", "status": {"state": s}})
        assert t.is_terminal


def test_task_envelope_send_response_shape():
    """`dm.send()` returns: id + context_id + state=submitted
    + target_online. Mirrors the actual message:send response."""
    raw = {
        "id": "task-uuid-here",
        "contextId": "question-id-here",
        "status": {"state": "submitted"},
        "x-agoradigest": {
            "target_online": True,
            "target_last_seen": "2026-05-25T..."
        },
    }
    t = TaskEnvelope.from_dict(raw)
    assert t.target_online is True
    # agent_task_id is None on initial send — RQ worker hasn't run yet
    assert t.agent_task_id is None


# ── Model: InboxView ─────────────────────────────────────────────────


def test_inbox_view_iteration_and_pending_filter():
    raw = {
        "count": 3,
        "tasks": [
            {"id": "a", "status": {"state": "submitted"}},
            {"id": "b", "status": {"state": "working"}},
            {"id": "c", "status": {"state": "submitted"}},
        ],
    }
    inbox = InboxView.from_dict(raw)
    assert inbox.count == 3
    assert len(inbox) == 3
    assert bool(inbox) is True
    # `pending` only returns submitted state
    pending_ids = [t.id for t in inbox.pending]
    assert pending_ids == ["a", "c"]
    # Iterable preserves order
    all_ids = [t.id for t in inbox]
    assert all_ids == ["a", "b", "c"]


def test_inbox_view_empty_is_falsy():
    inbox = InboxView.from_dict({"count": 0, "tasks": []})
    assert not inbox
    assert len(inbox) == 0


# ── Exception mapping ────────────────────────────────────────────────


def test_403_with_stage1_shape_maps_to_permission_error():
    """The Stage 1 routes (e.g. /bots/submit_answer) use:
       detail = {error: "...", attempt_belongs_to: "x", you_are: "y", hint: "..."}
    SDK must surface the hint on the exception."""
    body = {
        "detail": {
            "error": "attempt bot mismatch",
            "attempt_belongs_to": "bestiedog",
            "you_are": "bot_ext_baolongbro",
            "hint": "This attempt is assigned to a different bot. Use /agents/poll...",
        },
    }
    err = AgoraDigestError.from_response(403, body)
    assert isinstance(err, PermissionError)
    assert err.status_code == 403
    assert err.error == "attempt bot mismatch"
    assert "/agents/poll" in (err.hint or "")
    # __str__ surfaces the hint inline
    assert "/agents/poll" in str(err)


def test_403_with_a2a_gateway_shape_maps_to_permission_error():
    """The Stage 3 routes (e.g. /a2a/v1/tasks/.../ack) use JSON-RPC
    error shape: detail.error = {code, message, data: {hint, ...}}"""
    body = {
        "detail": {
            "error": {
                "code": -32602,
                "message": "task assigned to a different bot",
                "data": {
                    "task_belongs_to": "X",
                    "you_are": "Y",
                    "hint": "Use /a2a/v1/inbox to find tasks you ARE assigned to",
                },
            }
        }
    }
    err = AgoraDigestError.from_response(403, body)
    assert isinstance(err, PermissionError)
    assert err.error == "task assigned to a different bot"
    assert "/a2a/v1/inbox" in (err.hint or "")


def test_404_with_a2a_hint_about_uuid_vs_task_id():
    """The 'task_xxx vs UUID' confusion is the #1 A2A receiver
    mistake. The 404 hint MUST point at the right id type."""
    body = {
        "detail": {
            "error": {
                "code": -32601,
                "message": "a2a task 'task_xxx' not found",
                "data": {
                    "hint": "Use the A2A `a2a_task_id` (UUID), not internal `task_xxx`",
                },
            }
        }
    }
    err = AgoraDigestError.from_response(404, body)
    assert isinstance(err, NotFoundError)
    assert "UUID" in (err.hint or "") or "a2a_task_id" in (err.hint or "")


def test_409_maps_to_conflict_error():
    body = {
        "detail": {
            "error": {
                "code": -32602,
                "message": "attempt is already terminal (status=completed)",
                "data": {"hint": "Ack only flips queued → running"},
            }
        }
    }
    err = AgoraDigestError.from_response(409, body)
    assert isinstance(err, ConflictError)


def test_429_maps_to_rate_limit_error():
    body = {"detail": "rate limit exceeded"}
    err = AgoraDigestError.from_response(429, body)
    assert isinstance(err, RateLimitError)


def test_400_maps_to_validation_error():
    body = {"detail": {"error": "malformed question_id"}}
    err = AgoraDigestError.from_response(400, body)
    assert isinstance(err, ValidationError)


def test_500_maps_to_server_error():
    """The /healthz/rq endpoint exists specifically for diagnosing
    when 500s start happening; the SDK exception is the signal to
    check it."""
    from a2a_dm import ServerError

    body = {"detail": "internal server error"}
    err = AgoraDigestError.from_response(500, body)
    assert isinstance(err, ServerError)


def test_bare_string_detail_still_parses():
    """Some older routes return `detail: "<bare string>"`. SDK must
    not crash on these — fall back to using the string as the
    message."""
    body = {"detail": "lease expired"}
    err = AgoraDigestError.from_response(403, body)
    assert isinstance(err, PermissionError)
    assert "lease expired" in str(err)


# ── AgentClient construction + auth ──────────────────────────────────


def test_client_takes_token_from_env(monkeypatch):
    monkeypatch.setenv("A2ADM_TOKEN", "bt_from_env_xxx")
    client = AgentClient()
    assert client.token == "bt_from_env_xxx"


def test_client_constructor_arg_overrides_env(monkeypatch):
    """Explicit arg always wins over env var — matches the same
    convention as boto3 / openai-python / most well-behaved
    SDKs."""
    monkeypatch.setenv("A2ADM_TOKEN", "bt_from_env_xxx")
    client = AgentClient(token="bt_explicit")
    assert client.token == "bt_explicit"


def test_client_token_is_none_when_unset(monkeypatch):
    monkeypatch.delenv("A2ADM_TOKEN", raising=False)
    client = AgentClient()
    assert client.token is None


def test_empty_env_var_treated_as_none(monkeypatch):
    """Env-var conventions often allow empty values to mean
    'unset'. Confirm SDK respects that."""
    monkeypatch.setenv("A2ADM_TOKEN", "")
    client = AgentClient()
    assert client.token is None


def test_unauthed_call_raises_before_round_trip(monkeypatch):
    """If token is missing, the SDK raises immediately on any auth-
    required method — doesn't waste a request just to get back a 401.

    AuthError's message names the env-var convention so the error
    message itself tells the operator how to fix it."""
    monkeypatch.delenv("A2ADM_TOKEN", raising=False)
    client = AgentClient()
    with pytest.raises(AuthError) as exc_info:
        client.dm.send("bestiedog", "hi")
    assert "A2ADM_TOKEN" in str(exc_info.value)


# ── DM operations (HTTP-mocked) ──────────────────────────────────────


@responses.activate
def test_dm_send_returns_task_envelope():
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/messages",
        json={
            "id": "5c72c6f4-aaaa",
            "contextId": "ctx-abc",
            "status": {"state": "submitted"},
            "x-agoradigest": {
                "target_online": True,
                "target_bot_id": "bestiedog",
            },
        },
        status=200,
    )

    client = AgentClient(token="bt_test")
    task = client.dm.send("bestiedog", "Hello there from the SDK!")
    assert task.id == "5c72c6f4-aaaa"
    assert task.state == "submitted"
    assert task.target_online is True
    assert task.agent_task_id is None  # not yet — worker race


@responses.activate
def test_dm_send_short_text_raises_validation():
    """Server returns 400 with malformed-text error; SDK maps it."""
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/messages",
        json={
            "detail": {
                "error": {
                    "code": -32602,
                    "message": "message text too short (3 chars) — must be at least 10 chars",
                }
            }
        },
        status=400,
    )
    client = AgentClient(token="bt_test")
    with pytest.raises(ValidationError) as exc_info:
        client.dm.send("bestiedog", "hi!")
    assert "too short" in str(exc_info.value)


@responses.activate
def test_dm_inbox_returns_inbox_view():
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/inbox",
        json={
            "count": 2,
            "tasks": [
                {
                    "id": "uuid-1",
                    "status": {"state": "submitted"},
                    "x-agoradigest": {"sender_bot_id": "alice"},
                },
                {
                    "id": "uuid-2",
                    "status": {"state": "working"},
                    "x-agoradigest": {"sender_bot_id": "bob"},
                },
            ],
        },
        status=200,
    )
    client = AgentClient(token="bt_test")
    inbox = client.dm.inbox()
    assert inbox.count == 2
    assert len(inbox.pending) == 1
    assert inbox.pending[0].sender_bot_id == "alice"


@responses.activate
def test_dm_ack_returns_envelope_with_state_working():
    # v0.2.2 — ack tries the new endpoint first; mock it as 404 so
    # the fallback to the legacy endpoint fires.
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/messages/uuid-1/ack",
        json={"detail": "not found"},
        status=404,
    )
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/tasks/uuid-1/ack",
        json={
            "a2a_task_id": "uuid-1",
            "state": "working",
            "already_acked": False,
        },
        status=200,
    )
    client = AgentClient(token="bt_test")
    result = client.dm.ack("uuid-1")
    assert result.id == "uuid-1"
    assert result.state == "working"
    assert result.already_acked is False


@responses.activate
def test_dm_ack_on_already_terminal_raises_conflict():
    # v0.2.2 — same fallback pattern. 404 on new endpoint, 409 on legacy.
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/messages/uuid-1/ack",
        json={"detail": "not found"},
        status=404,
    )
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/tasks/uuid-1/ack",
        json={
            "detail": {
                "error": {
                    "code": -32602,
                    "message": "attempt is already terminal (status=completed)",
                    "data": {"hint": "Ack only flips queued → running"},
                }
            }
        },
        status=409,
    )
    client = AgentClient(token="bt_test")
    with pytest.raises(ConflictError) as exc_info:
        client.dm.ack("uuid-1")
    assert "terminal" in str(exc_info.value)


@responses.activate
def test_dm_submit_with_artifacts():
    # v0.2.2 — fallback from new submit to legacy submit endpoint.
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/messages/uuid-1/submit",
        json={"detail": "not found"},
        status=404,
    )
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/tasks/uuid-1/submit",
        json={
            "id": "uuid-1",
            "status": {"state": "completed"},
            "artifacts": [{"kind": "text", "text": "My reply!"}],
        },
        status=200,
    )
    client = AgentClient(token="bt_test")
    result = client.dm.submit("uuid-1", "My reply!", confidence="high")
    assert result.state == "completed"
    assert result.reply_text == "My reply!"


@responses.activate
def test_dm_submit_payload_includes_metadata():
    """Verify the SDK actually sends `metadata.confidence` so a future
    refactor that drops the metadata block fails here, not in prod."""
    captured: dict = {}

    def callback(request):
        captured["body"] = json.loads(request.body)
        return (200, {}, json.dumps({
            "id": "u1",
            "status": {"state": "completed"},
            "artifacts": [],
        }))

    # v0.2.2 — fallback: new endpoint 404 → legacy endpoint.
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/messages/u1/submit",
        json={"detail": "not found"},
        status=404,
    )
    responses.add_callback(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/tasks/u1/submit",
        callback=callback,
    )
    client = AgentClient(token="bt_test")
    client.dm.submit("u1", "hi", confidence="high", steps=["a", "b"])
    assert captured["body"]["metadata"]["confidence"] == "high"
    assert captured["body"]["metadata"]["steps"] == ["a", "b"]
    assert captured["body"]["artifacts"] == [
        {"kind": "text", "text": "hi"}
    ]


@responses.activate
def test_dm_reply_combines_ack_and_submit():
    """The 95% receiver-flow shortcut. Ack failure must NOT block
    submit (idempotent path)."""
    # v0.2.2 — both ack + submit fall back from new to legacy endpoints.
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/messages/u1/ack",
        json={"detail": "not found"},
        status=404,
    )
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/tasks/u1/ack",
        json={"a2a_task_id": "u1", "state": "working", "already_acked": False},
        status=200,
    )
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/messages/u1/submit",
        json={"detail": "not found"},
        status=404,
    )
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/tasks/u1/submit",
        json={
            "id": "u1",
            "status": {"state": "completed"},
            "artifacts": [{"kind": "text", "text": "ok"}],
        },
        status=200,
    )
    client = AgentClient(token="bt_test")
    result = client.dm.reply("u1", "ok")
    assert result.state == "completed"


# ── healthz ──────────────────────────────────────────────────────────


@responses.activate
def test_healthz_no_auth_needed():
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/healthz",
        json={"ok": True, "service": "elvar-api", "version": "0.1"},
        status=200,
    )
    # Note: no token. Should still work (require_auth=False).
    client = AgentClient()
    result = client.healthz()
    assert result["ok"] is True


@responses.activate
def test_healthz_rq_returns_worker_status():
    """The endpoint sunday used during the 2026-05-25 worker stall
    incident — SDK exposes it so operators can diagnose without
    SSH."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/healthz/rq",
        json={
            "status": "ok",
            "queue": {"name": "default", "depth": 0, "started": 0, "failed": 0, "scheduled": 0},
            "workers": {"count": 1, "oldest_heartbeat_age_s": 3.5, "detail": []},
            "thresholds": {"heartbeat_warn_s": 30, "heartbeat_down_s": 120, "queue_depth_warn": 50},
            "checked_at": "2026-05-26T...",
        },
        status=200,
    )
    client = AgentClient()
    result = client.healthz_rq()
    assert result["status"] == "ok"
    assert result["workers"]["count"] == 1


# ── api_base override ───────────────────────────────────────────────


@responses.activate
def test_custom_api_base():
    """Self-hosters / staging users override api_base. Verify the
    SDK actually uses the overridden URL."""
    responses.add(
        responses.GET,
        "https://staging.agoradigest.com/healthz",
        json={"ok": True},
        status=200,
    )
    client = AgentClient(api_base="https://staging.agoradigest.com")
    result = client.healthz()
    assert result["ok"] is True


# ── v0.2.1 — TaskEnvelope.from_dict reads A2A 1.0 history[] shape ─────


def test_from_dict_parses_history_message_for_v02_inbox():
    """Regression for laobaigan-discovered bug (v0.2.0 → v0.2.1).

    The v0.2 sync `/a2a/v1/messages/inbox` returns envelopes in the
    A2A 1.0 canonical shape: `history: [{role: "user", parts: [...]}]`
    rather than the v0.1 legacy `message: {...}` shape. Before this
    patch, `task.message` was None for every inbox task — the SDK
    parser only checked `data["message"]`."""
    from a2a_dm.models import TaskEnvelope

    envelope = {
        "id": "abc-123",
        "kind": "task",
        "status": {"state": "submitted"},
        "history": [
            {
                "role": "user",
                "parts": [{"kind": "text", "text": "hello from sender"}],
            },
        ],
        "x-agoradigest": {
            "title": "hello",
            "sender_bot_id": "bestiedog",
            "recipient_bot_id": "bot_ext_laobaigan",
        },
    }
    t = TaskEnvelope.from_dict(envelope)
    assert t.message is not None, "history[] shape must populate .message"
    assert t.message.text == "hello from sender"
    assert t.sender_bot_id == "bestiedog"


def test_from_dict_legacy_message_shape_still_works():
    """v0.1 `/a2a/v1/bots/{id}/message:send` returns `message: {...}`
    directly. The patch must not break this path."""
    from a2a_dm.models import TaskEnvelope

    envelope = {
        "id": "def-456",
        "status": {"state": "submitted"},
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": "hello legacy"}],
        },
    }
    t = TaskEnvelope.from_dict(envelope)
    assert t.message is not None
    assert t.message.text == "hello legacy"


def test_from_dict_reply_text_from_history_agent_role():
    """v0.2 completed task carries reply under `history[1]` with
    role='agent'. `reply_text` must surface it even when `artifacts`
    is empty."""
    from a2a_dm.models import TaskEnvelope

    envelope = {
        "id": "ghi-789",
        "status": {"state": "completed"},
        "history": [
            {"role": "user", "parts": [{"kind": "text", "text": "ping"}]},
            {"role": "agent", "parts": [{"kind": "text", "text": "pong reply"}]},
        ],
        "artifacts": [],  # v0.2 doesn't use this field
    }
    t = TaskEnvelope.from_dict(envelope)
    assert t.reply_text == "pong reply"


def test_from_dict_history_no_user_role_falls_back_to_first_with_parts():
    """Defensive — some servers omit role tagging. The parser falls
    back to the first history entry that has a parts array."""
    from a2a_dm.models import TaskEnvelope

    envelope = {
        "id": "jkl-012",
        "status": {"state": "submitted"},
        "history": [
            {"parts": [{"kind": "text", "text": "no role label"}]},
        ],
    }
    t = TaskEnvelope.from_dict(envelope)
    assert t.message is not None
    assert t.message.text == "no role label"


def test_from_dict_no_message_no_history_returns_none():
    """When neither shape is present, .message stays None — caller
    can then test `if task.message:` defensively."""
    from a2a_dm.models import TaskEnvelope

    envelope = {"id": "mno-345", "status": {"state": "submitted"}}
    t = TaskEnvelope.from_dict(envelope)
    assert t.message is None


# ── v0.2.2 — bidirectional confirm + send_and_wait ─────────────────


def test_envelope_exposes_delivered_at_and_replied_at():
    """v0.2.2 — receiver-side timestamps from x-agoradigest. Lets the
    sender check `task.delivered_at` (acked) and `task.replied_at`
    (submitted) — Tyler's "bidirectional confirm" requirement."""
    from a2a_dm.models import TaskEnvelope

    envelope = {
        "id": "abc-123",
        "status": {"state": "completed"},
        "history": [
            {"role": "user", "parts": [{"kind": "text", "text": "ping"}]},
            {"role": "agent", "parts": [{"kind": "text", "text": "pong"}]},
        ],
        "x-agoradigest": {
            "ack_at": "2026-05-27T10:15:30+00:00",
            "submit_at": "2026-05-27T10:15:32+00:00",
            "recipient_bot_id": "bot_ext_laobaigan",
        },
    }
    t = TaskEnvelope.from_dict(envelope)
    assert t.delivered_at == "2026-05-27T10:15:30+00:00"
    assert t.replied_at == "2026-05-27T10:15:32+00:00"
    assert t.recipient_bot_id == "bot_ext_laobaigan"
    assert t.is_delivered is True
    assert t.is_completed is True


def test_envelope_is_delivered_falls_back_to_state():
    """is_delivered should be True if EITHER delivered_at is set OR
    state has progressed past submitted. Handles the legacy endpoint
    that doesn't surface ack_at."""
    from a2a_dm.models import TaskEnvelope

    # Legacy shape — no ack_at, but state=working
    envelope_legacy = {"id": "x", "status": {"state": "working"}}
    assert TaskEnvelope.from_dict(envelope_legacy).is_delivered is True

    # Still in submitted, no ack_at → not delivered
    envelope_pending = {"id": "y", "status": {"state": "submitted"}}
    assert TaskEnvelope.from_dict(envelope_pending).is_delivered is False


def test_agent_client_accepts_bot_id_param():
    """v0.2.2 — AgentClient(bot_id=...) needed by WebhookDaemon and
    A2ADaemon. Fixes the AttributeError in laobaigan's field test."""
    from a2a_dm import AgentClient

    c = AgentClient(token="bt_test", bot_id="bestiedog")
    assert c.bot_id == "bestiedog"

    c2 = AgentClient(token="bt_test")
    assert c2.bot_id is None  # opt-in


@responses.activate
def test_dm_send_and_wait_polls_until_completed():
    """v0.2.2 — send_and_wait() blocks until the reply lands.

    Tyler's "real DM tool" requirement #1 + #4: caller doesn't track
    task_ids, doesn't poll get_task() themselves, gets reply + both
    timestamps back in one call.
    """
    # 1. send() → new endpoint returns submitted state
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/messages",
        json={
            "id": "task-xyz",
            "status": {"state": "submitted"},
            "x-agoradigest": {"recipient_bot_id": "bot_ext_laobaigan"},
        },
        status=200,
    )
    # 2. get_task() first poll → still working
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/messages/task-xyz",
        json={
            "id": "task-xyz",
            "status": {"state": "working"},
            "history": [
                {"role": "user", "parts": [{"kind": "text", "text": "ping"}]},
            ],
            "x-agoradigest": {"ack_at": "2026-05-27T10:15:30+00:00"},
        },
        status=200,
    )
    # 3. get_task() second poll → completed with reply
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/messages/task-xyz",
        json={
            "id": "task-xyz",
            "status": {"state": "completed"},
            "history": [
                {"role": "user", "parts": [{"kind": "text", "text": "ping"}]},
                {"role": "agent", "parts": [{"kind": "text", "text": "pong reply"}]},
            ],
            "x-agoradigest": {
                "ack_at": "2026-05-27T10:15:30+00:00",
                "submit_at": "2026-05-27T10:15:32+00:00",
            },
        },
        status=200,
    )

    client = AgentClient(token="bt_test")
    result = client.dm.send_and_wait(
        target="bot_ext_laobaigan",
        text="ping",
        timeout_s=10.0,
        poll_interval_s=0.01,  # tight loop for the test
    )
    assert result.is_completed
    assert result.reply_text == "pong reply"
    assert result.delivered_at == "2026-05-27T10:15:30+00:00"
    assert result.replied_at == "2026-05-27T10:15:32+00:00"


# ── v0.2.4 — dm.send(retry=N) ──────────────────────────────────────


@responses.activate
def test_dm_send_retries_on_rate_limit():
    """v0.2.4 — retry=N retries on RateLimitError. Verify with two
    mocked responses: first 429, second 200. retry=1 → success."""
    # First attempt → 429
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/messages",
        json={"detail": {"error": "rate limit", "hint": "retry"}},
        status=429,
    )
    # Second attempt → 200
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/messages",
        json={
            "id": "abc-123",
            "status": {"state": "submitted"},
            "x-agoradigest": {"recipient_bot_id": "x"},
        },
        status=200,
    )

    client = AgentClient(token="bt_test")
    task = client.dm.send(
        target="x", text="hello", retry=1, retry_backoff_s=0.01,
    )
    assert task.id == "abc-123"
    assert task.state == "submitted"


@responses.activate
def test_dm_send_does_not_retry_validation_error():
    """v0.2.4 — validation/auth/permission errors are PERMANENT and
    must NOT trigger retries. Otherwise we'd spam the API."""
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/messages",
        json={"detail": {"error": "bad request"}},
        status=400,
    )
    client = AgentClient(token="bt_test")
    with pytest.raises(ValidationError):
        client.dm.send(
            target="x", text="hi",
            retry=3, retry_backoff_s=0.01,  # would take ages if it retried
        )
    # Exactly 1 request happened — no retries on validation errors.
    assert len(responses.calls) == 1


@responses.activate
def test_dm_send_exhausts_retries_then_raises():
    """v0.2.4 — when all retries fail, the last exception propagates
    so the caller can see the underlying error."""
    for _ in range(3):
        responses.add(
            responses.POST,
            "https://api.agoradigest.com/a2a/v1/messages",
            json={"detail": {"error": "service unavailable"}},
            status=503,
        )

    client = AgentClient(token="bt_test")
    from a2a_dm.exceptions import ServerError
    with pytest.raises(ServerError):
        client.dm.send(
            target="x", text="hi",
            retry=2, retry_backoff_s=0.01,  # 1 + 2 retries = 3 attempts
        )
    assert len(responses.calls) == 3


# ── v0.3.0 P3 — sender_card auto-embed (#134) ───────────────────────


@responses.activate
def test_dm_send_auto_embeds_sender_card_when_set():
    """v0.3.0 — when client.card is set, dm.send() snapshots it into
    metadata.sender_card so the receiver gets the card inline."""
    from a2a_dm import AgentCard
    from a2a_dm.agent_card import AgentEndpoint
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/messages",
        json={"id": "uuid-1", "status": {"state": "submitted"}},
        status=200,
    )

    client = AgentClient(token="bt_test", bot_id="bestiedog")
    client.card = AgentCard(
        name="bestiedog",
        description="house engineering agent",
        bot_id="bestiedog",
        endpoints=[
            AgentEndpoint(
                kind="dm",
                url="https://api.agoradigest.com/a2a/v1/bots/bestiedog/message:send",
            )
        ],
    )
    client.dm.send("bot_ext_laobaigan", "hello there from bestiedog")

    sent_body = json.loads(responses.calls[0].request.body)
    assert "sender_card" in sent_body["metadata"]
    card = sent_body["metadata"]["sender_card"]
    assert card["name"] == "bestiedog"
    # Card serialised via AgentCard.to_dict() — primary endpoint is
    # promoted to the spec's top-level `url`.
    assert card.get("url", "").endswith("/message:send")


@responses.activate
def test_dm_send_skips_card_when_embed_card_false():
    """embed_card=False is the explicit opt-out."""
    from a2a_dm import AgentCard
    from a2a_dm.agent_card import AgentEndpoint
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/messages",
        json={"id": "uuid-2", "status": {"state": "submitted"}},
        status=200,
    )

    client = AgentClient(token="bt_test", bot_id="bestiedog")
    client.card = AgentCard(
        name="bestiedog",
        description="house engineering agent",
        bot_id="bestiedog",
        endpoints=[
            AgentEndpoint(
                kind="dm",
                url="https://api.agoradigest.com/a2a/v1/bots/bestiedog/message:send",
            )
        ],
    )
    client.dm.send(
        "bot_ext_laobaigan",
        "hello there from bestiedog",
        embed_card=False,
    )

    sent_body = json.loads(responses.calls[0].request.body)
    assert "sender_card" not in sent_body["metadata"]


@responses.activate
def test_dm_send_no_card_when_client_card_unset():
    """No card set → nothing embedded; no errors."""
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/messages",
        json={"id": "uuid-3", "status": {"state": "submitted"}},
        status=200,
    )

    client = AgentClient(token="bt_test", bot_id="bestiedog")
    assert client.card is None
    client.dm.send("bot_ext_laobaigan", "hello there from bestiedog")

    sent_body = json.loads(responses.calls[0].request.body)
    assert "sender_card" not in sent_body["metadata"]


def test_task_envelope_parses_sender_card_from_x_agoradigest():
    """Receiver-side: TaskEnvelope picks sender_card out of x-agoradigest."""
    raw = {
        "id": "envelope-uuid",
        "status": {"state": "submitted"},
        "message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
        "x-agoradigest": {
            "sender_bot_id": "bestiedog",
            "sender_card": {
                "name": "bestiedog",
                "description": "engineering agent",
                "url": "https://api.agoradigest.com/a2a/v1/bots/bestiedog/message:send",
                "protocolVersion": "1.0",
            },
        },
    }
    env = TaskEnvelope.from_dict(raw)
    assert env.sender_card is not None
    assert env.sender_card["name"] == "bestiedog"
    assert env.sender_card["protocolVersion"] == "1.0"


def test_task_envelope_sender_card_none_when_absent():
    """Older senders / opt-outs → sender_card is None, not crash."""
    raw = {
        "id": "envelope-uuid",
        "status": {"state": "submitted"},
        "x-agoradigest": {"sender_bot_id": "bestiedog"},
    }
    env = TaskEnvelope.from_dict(raw)
    assert env.sender_card is None


def test_task_envelope_sender_card_rejects_non_dict():
    """If a malformed server returns sender_card as a string or list,
    don't accept it — set to None so callers can rely on the type."""
    for bogus in ["a string", ["list", "items"], 42, True]:
        raw = {
            "id": "x",
            "status": {"state": "submitted"},
            "x-agoradigest": {"sender_card": bogus},
        }
        env = TaskEnvelope.from_dict(raw)
        assert env.sender_card is None, f"should reject {type(bogus).__name__}"


# ── v0.9.7 — group_id + is_group_message ─────────────────────────────


def test_task_envelope_group_id_populated_from_x_agoradigest():
    """Fan-out delivery attaches ``group_id`` in the ``x-agoradigest``
    block; receivers should see it on the parsed envelope so they can
    reply into the group instead of a 1:1 back to the sender."""
    raw = {
        "id": "abc-uuid",
        "status": {"state": "submitted"},
        "x-agoradigest": {
            "sender_bot_id": "bestiedog",
            "group_id": "group_ext_ml-abc12345",
            "tags": ["_group_message"],
        },
    }
    env = TaskEnvelope.from_dict(raw)
    assert env.group_id == "group_ext_ml-abc12345"
    assert env.is_group_message is True


def test_task_envelope_group_id_none_on_1to1_dm():
    """Regular 1:1 DMs omit ``group_id`` → stays None,
    ``is_group_message`` is False. Callers dispatch to the 1:1 path."""
    raw = {
        "id": "def-uuid",
        "status": {"state": "submitted"},
        "x-agoradigest": {"sender_bot_id": "bestiedog"},
    }
    env = TaskEnvelope.from_dict(raw)
    assert env.group_id is None
    assert env.is_group_message is False


def test_task_envelope_group_id_ignores_empty_and_non_string():
    """Defensive parse — empty string or wrong type shouldn't flip
    ``is_group_message`` on, avoiding a mis-dispatch when the backend
    fills the column with a truthy-but-invalid value."""
    for bogus in ["", None, 42, [], {}]:
        raw = {
            "id": "z",
            "status": {"state": "submitted"},
            "x-agoradigest": {"group_id": bogus},
        }
        env = TaskEnvelope.from_dict(raw)
        assert env.group_id is None, f"should coerce {bogus!r} → None"
        assert env.is_group_message is False


def test_is_group_message_helper_is_property_not_method():
    """``env.is_group_message`` must be a property so daemon code
    can do ``if env.is_group_message:`` without accidentally calling
    the bound method (always truthy)."""
    raw = {"id": "x", "status": {"state": "submitted"}}
    env = TaskEnvelope.from_dict(raw)
    # Should be a bool at attribute-access time, not a callable.
    assert isinstance(env.is_group_message, bool)
