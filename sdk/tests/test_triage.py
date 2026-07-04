"""SDK Phase 7.4 — daemon.triage tests.

Coverage:
  * TriagePolicy / TriageDecision dataclass shapes
  * TurnCounter.check / .bump / .reset against mocked friends API
  * Counter handles missing friend, missing key, corrupt value
  * _partner_bot_id_from_task pulls sender / recipient correctly
  * _BaseDaemon triage_gate: passes when under cap, blocks at cap
  * _BaseDaemon triage_bump: increments after handler success only
  * @on_cap_exceeded decorator fires with correct args when capped
  * stats.cap_exceeded_count bumps on each blocked dispatch
  * Backward compat: no triage_policy → behaves exactly like pre-7.4
  * count_on_replies=False disables reply-side gating + bumping
  * Triage check failure (transport error) fails open (handler runs)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import responses

from a2a_dm import AgentClient, Friend, TaskEnvelope
from a2a_dm.daemon import (
    SSEDaemon,
    TriageDecision,
    TriagePolicy,
    TurnCounter,
)
from a2a_dm.daemon._base import _BaseDaemon
from a2a_dm.daemon.triage import (
    TURN_COUNT_KEY,
    _partner_bot_id_from_task,
)
from a2a_dm.exceptions import TransportError


# ── Test helpers ────────────────────────────────────────────────


def _friend_payload(memory=None, **overrides) -> dict:
    base = {
        "owner_bot_id": "me",
        "friend_bot_id": "bestiedog",
        "label": "bestie",
        "note": None,
        "groups": [],
        "tags": [],
        "agent_card_snapshot": None,
        "added_at": "2026-05-30T12:00:00+00:00",
        "client_origin_at": None,
        "last_contact_at": None,
        "discoverable": False,
        "notify": False,
        "memory": memory if memory is not None else {},
    }
    base.update(overrides)
    return base


def _envelope(*, task_id="msg_1", sender="bestiedog", recipient="me") -> TaskEnvelope:
    """Minimal envelope with sender_bot_id + recipient_bot_id attrs."""
    env = TaskEnvelope(
        id=task_id,
        state="submitted",
        message={"text": "hi", "sender_bot_id": sender, "recipient_bot_id": recipient},
    )
    # Some envelope shapes expose these as attrs; we set them explicitly
    # so _partner_bot_id_from_task can pick them up via getattr.
    env.sender_bot_id = sender  # type: ignore[attr-defined]
    env.recipient_bot_id = recipient  # type: ignore[attr-defined]
    return env


def _make_concrete_daemon(client, **kwargs) -> _BaseDaemon:
    """_BaseDaemon._run_loop is abstract — subclass for tests."""
    class _TestDaemon(_BaseDaemon):
        def _run_loop(self):
            pass
    return _TestDaemon(client, **kwargs)


# ── TriagePolicy / TriageDecision ───────────────────────────────


def test_triage_policy_defaults():
    p = TriagePolicy()
    assert p.max_turns_per_partner == 10
    assert p.count_on_replies is True
    assert p.track_partner_for_replies is True


def test_triage_policy_custom():
    p = TriagePolicy(max_turns_per_partner=5, count_on_replies=False)
    assert p.max_turns_per_partner == 5
    assert p.count_on_replies is False


def test_triage_decision_fields():
    d = TriageDecision(
        should_respond=False,
        turn_count=10,
        cap=10,
        reason="cap_exceeded",
        partner_bot_id="bestiedog",
    )
    assert d.should_respond is False
    assert d.partner_bot_id == "bestiedog"


# ── TurnCounter — memory read/write ─────────────────────────────


@responses.activate
def test_turn_counter_check_under_cap_returns_ok():
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={TURN_COUNT_KEY: 3})},
        status=200,
    )
    client = AgentClient(token="bt_test", bot_id="me")
    tc = TurnCounter(client, TriagePolicy(max_turns_per_partner=10))
    decision = tc.check("bestiedog")
    assert decision.should_respond is True
    assert decision.turn_count == 3
    assert decision.cap == 10
    assert decision.reason == "ok"
    assert decision.partner_bot_id == "bestiedog"


@responses.activate
def test_turn_counter_check_at_cap_blocks():
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={TURN_COUNT_KEY: 10})},
        status=200,
    )
    client = AgentClient(token="bt_test", bot_id="me")
    tc = TurnCounter(client, TriagePolicy(max_turns_per_partner=10))
    decision = tc.check("bestiedog")
    assert decision.should_respond is False
    assert decision.turn_count == 10
    assert decision.reason == "cap_exceeded"


@responses.activate
def test_turn_counter_check_over_cap_still_blocks():
    """Counter can drift past cap (manual edits, races). Still blocks."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={TURN_COUNT_KEY: 47})},
        status=200,
    )
    client = AgentClient(token="bt_test", bot_id="me")
    tc = TurnCounter(client, TriagePolicy(max_turns_per_partner=10))
    decision = tc.check("bestiedog")
    assert decision.should_respond is False
    assert decision.turn_count == 47


@responses.activate
def test_turn_counter_check_missing_friend_returns_zero():
    """Not friended → fresh budget. Don't block strangers."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/stranger",
        json={"detail": {"error": "friend_not_found"}},
        status=404,
    )
    client = AgentClient(token="bt_test", bot_id="me")
    tc = TurnCounter(client, TriagePolicy())
    decision = tc.check("stranger")
    assert decision.should_respond is True
    assert decision.turn_count == 0


@responses.activate
def test_turn_counter_check_missing_key_returns_zero():
    """Friend exists but no _turn_count key (Phase 7.3 default {})."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={"unrelated": "data"})},
        status=200,
    )
    client = AgentClient(token="bt_test", bot_id="me")
    tc = TurnCounter(client, TriagePolicy())
    decision = tc.check("bestiedog")
    assert decision.should_respond is True
    assert decision.turn_count == 0


@responses.activate
def test_turn_counter_check_corrupt_value_resets():
    """Memory key got clobbered with a string → coerce to 0, fresh start."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={TURN_COUNT_KEY: "oops"})},
        status=200,
    )
    client = AgentClient(token="bt_test", bot_id="me")
    tc = TurnCounter(client, TriagePolicy())
    decision = tc.check("bestiedog")
    assert decision.turn_count == 0


@responses.activate
def test_turn_counter_check_negative_value_clamped():
    """Defensive: negative count → 0 (no "free turns from going negative")."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={TURN_COUNT_KEY: -5})},
        status=200,
    )
    client = AgentClient(token="bt_test", bot_id="me")
    tc = TurnCounter(client, TriagePolicy())
    decision = tc.check("bestiedog")
    assert decision.turn_count == 0


@responses.activate
def test_turn_counter_bump_increments():
    """Bump reads current count, writes count+1, preserves other memory keys."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(
            memory={TURN_COUNT_KEY: 4, "fav": "blue"}
        )},
        status=200,
    )
    responses.add(
        responses.PATCH,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(
            memory={TURN_COUNT_KEY: 5, "fav": "blue"}
        )},
        status=200,
    )
    client = AgentClient(token="bt_test", bot_id="me")
    tc = TurnCounter(client, TriagePolicy())
    new = tc.bump("bestiedog")
    assert new == 5
    import json
    body = json.loads(responses.calls[1].request.body)
    # Critical: bump preserves other memory keys
    assert body["memory"][TURN_COUNT_KEY] == 5
    assert body["memory"]["fav"] == "blue"


@responses.activate
def test_turn_counter_bump_starts_at_one_when_missing():
    """No prior _turn_count key → first bump writes 1."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={})},
        status=200,
    )
    responses.add(
        responses.PATCH,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={TURN_COUNT_KEY: 1})},
        status=200,
    )
    client = AgentClient(token="bt_test", bot_id="me")
    tc = TurnCounter(client, TriagePolicy())
    assert tc.bump("bestiedog") == 1


@responses.activate
def test_turn_counter_bump_noop_when_not_friended():
    """Don't auto-friend on bump — operator owns the friend list."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/stranger",
        json={"detail": {"error": "friend_not_found"}},
        status=404,
    )
    client = AgentClient(token="bt_test", bot_id="me")
    tc = TurnCounter(client, TriagePolicy())
    assert tc.bump("stranger") == 0
    # Only the GET happened — no PATCH attempt.
    assert len(responses.calls) == 1


@responses.activate
def test_turn_counter_reset_zeroes_key():
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(
            memory={TURN_COUNT_KEY: 8, "fav": "red"}
        )},
        status=200,
    )
    responses.add(
        responses.PATCH,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={TURN_COUNT_KEY: 0, "fav": "red"})},
        status=200,
    )
    client = AgentClient(token="bt_test", bot_id="me")
    tc = TurnCounter(client, TriagePolicy())
    tc.reset("bestiedog")
    import json
    body = json.loads(responses.calls[1].request.body)
    assert body["memory"][TURN_COUNT_KEY] == 0
    assert body["memory"]["fav"] == "red"


# ── _partner_bot_id_from_task ───────────────────────────────────


def test_partner_id_pulls_sender_attr_first():
    env = _envelope(sender="alice", recipient="bob")
    assert _partner_bot_id_from_task(env) == "alice"


def test_partner_id_falls_back_to_message_dict():
    env = TaskEnvelope(
        id="x", state="submitted",
        message={"sender_bot_id": "alice", "recipient_bot_id": "bob"},
    )
    # No top-level sender_bot_id attr → falls into message dict
    assert _partner_bot_id_from_task(env) == "alice"


def test_partner_id_returns_none_when_missing():
    env = TaskEnvelope(id="x", state="submitted", message={})
    assert _partner_bot_id_from_task(env) is None


# ── _BaseDaemon triage gate / bump ──────────────────────────────


@responses.activate
def test_dispatch_runs_handler_when_under_cap():
    """Triage gate passes → handler invoked → bump runs after success."""
    # check (under cap)
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={TURN_COUNT_KEY: 3})},
        status=200,
    )
    # bump's GET
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={TURN_COUNT_KEY: 3})},
        status=200,
    )
    # bump's PATCH
    responses.add(
        responses.PATCH,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={TURN_COUNT_KEY: 4})},
        status=200,
    )
    client = AgentClient(token="bt_test", bot_id="me")
    daemon = _make_concrete_daemon(
        client,
        triage_policy=TriagePolicy(max_turns_per_partner=10),
        auto_ack=False,
    )
    calls = []

    @daemon.on_message
    def handler(task, d):
        calls.append(task.id)

    env = _envelope(task_id="msg_1", sender="bestiedog", recipient="me")
    env.state = "completed"  # skip ack path
    result = daemon._dispatch(env)
    assert result is True
    assert calls == ["msg_1"]
    assert daemon.stats.messages_processed == 1
    assert daemon.stats.cap_exceeded_count == 0


@responses.activate
def test_dispatch_skips_handler_when_capped():
    """Triage gate blocks → handler NOT invoked → cap_exceeded_count bumps."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={TURN_COUNT_KEY: 10})},
        status=200,
    )
    client = AgentClient(token="bt_test", bot_id="me")
    daemon = _make_concrete_daemon(
        client,
        triage_policy=TriagePolicy(max_turns_per_partner=10),
        auto_ack=False,
    )
    calls = []

    @daemon.on_message
    def handler(task, d):
        calls.append(task.id)

    env = _envelope(task_id="msg_1", sender="bestiedog", recipient="me")
    env.state = "completed"
    result = daemon._dispatch(env)
    assert result is True  # we still mark seen
    assert calls == []  # handler never ran
    assert daemon.stats.messages_processed == 0
    assert daemon.stats.cap_exceeded_count == 1


@responses.activate
def test_on_cap_exceeded_callback_fires_with_decision():
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={TURN_COUNT_KEY: 10})},
        status=200,
    )
    client = AgentClient(token="bt_test", bot_id="me")
    daemon = _make_concrete_daemon(
        client,
        triage_policy=TriagePolicy(max_turns_per_partner=10),
        auto_ack=False,
    )

    @daemon.on_message
    def handler(task, d):
        pass

    received = []

    @daemon.on_cap_exceeded
    def cap_cb(partner_bot_id, decision, d):
        received.append((partner_bot_id, decision))

    env = _envelope(task_id="msg_1", sender="bestiedog", recipient="me")
    env.state = "completed"
    daemon._dispatch(env)

    assert len(received) == 1
    partner, decision = received[0]
    assert partner == "bestiedog"
    assert decision.should_respond is False
    assert decision.turn_count == 10
    assert decision.cap == 10
    assert decision.reason == "cap_exceeded"


def test_no_triage_policy_means_no_gating():
    """Backward compat: pre-7.4 daemons (no policy) skip the gate entirely."""
    client = MagicMock()
    daemon = _make_concrete_daemon(client, auto_ack=False)
    assert daemon.triage_policy is None
    assert daemon.triage is None
    calls = []

    @daemon.on_message
    def handler(task, d):
        calls.append(task.id)

    env = _envelope(task_id="msg_1", sender="bestiedog", recipient="me")
    env.state = "completed"
    daemon._dispatch(env)
    assert calls == ["msg_1"]
    # No friends.get/update calls — gate fully bypassed.
    client.friends.get.assert_not_called()


@responses.activate
def test_triage_check_failure_fails_open():
    """If the triage HTTP call blows up, let the handler run anyway.
    Better to over-respond than to silently swallow real messages."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        body=ConnectionError("network down"),
    )
    client = AgentClient(token="bt_test", bot_id="me")
    daemon = _make_concrete_daemon(
        client,
        triage_policy=TriagePolicy(max_turns_per_partner=10),
        auto_ack=False,
    )
    calls = []

    @daemon.on_message
    def handler(task, d):
        calls.append(task.id)

    env = _envelope(task_id="msg_1", sender="bestiedog", recipient="me")
    env.state = "completed"
    # Bump will also fail — wrap so the test still completes
    try:
        daemon._dispatch(env)
    except Exception:
        pass
    # Handler ran despite triage failure
    assert calls == ["msg_1"]


def test_missing_partner_id_does_not_block():
    """Envelope with no sender/recipient → triage skips, handler runs."""
    client = MagicMock()
    daemon = _make_concrete_daemon(
        client,
        triage_policy=TriagePolicy(),
        auto_ack=False,
    )
    calls = []

    @daemon.on_message
    def handler(task, d):
        calls.append(task.id)

    env = TaskEnvelope(id="x", state="completed", message={})
    daemon._dispatch(env)
    assert calls == ["x"]
    assert daemon.stats.cap_exceeded_count == 0
    client.friends.get.assert_not_called()


# ── Reply-side triage ───────────────────────────────────────────


@responses.activate
def test_dispatch_reply_gated_when_count_on_replies_true():
    """count_on_replies=True (default) → reply handler also gated."""
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/friends/bestiedog",
        json={"friend": _friend_payload(memory={TURN_COUNT_KEY: 10})},
        status=200,
    )
    client = AgentClient(token="bt_test", bot_id="me")
    daemon = _make_concrete_daemon(
        client,
        triage_policy=TriagePolicy(max_turns_per_partner=10, count_on_replies=True),
    )
    calls = []

    @daemon.on_reply
    def reply_handler(task, d):
        calls.append(task.id)

    env = _envelope(task_id="msg_1", sender="bestiedog", recipient="me")
    env.state = "completed"
    daemon._dispatch_reply(env)
    assert calls == []  # capped
    assert daemon.stats.cap_exceeded_count == 1


def test_dispatch_reply_not_gated_when_count_on_replies_false():
    """count_on_replies=False → reply handler ALWAYS runs, no bump."""
    client = MagicMock()
    daemon = _make_concrete_daemon(
        client,
        triage_policy=TriagePolicy(count_on_replies=False),
    )
    calls = []

    @daemon.on_reply
    def reply_handler(task, d):
        calls.append(task.id)

    env = _envelope(task_id="msg_1", sender="bestiedog", recipient="me")
    daemon._dispatch_reply(env)
    assert calls == ["msg_1"]
    # No friend lookups
    client.friends.get.assert_not_called()
    # No counter bump either
    client.friends.update.assert_not_called()


# ── Stats / observability ───────────────────────────────────────


def test_daemon_stats_includes_cap_exceeded_count():
    """DaemonStats has the new field, defaults to 0."""
    client = MagicMock()
    daemon = _make_concrete_daemon(client)
    assert daemon.stats.cap_exceeded_count == 0


def test_daemon_exports_triage_attrs():
    """Public API: .triage_policy and .triage available on the daemon."""
    client = MagicMock()
    policy = TriagePolicy(max_turns_per_partner=5)
    daemon = _make_concrete_daemon(client, triage_policy=policy)
    assert daemon.triage_policy is policy
    assert isinstance(daemon.triage, TurnCounter)
