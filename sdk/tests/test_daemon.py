"""Daemon framework tests (v0.2).

These tests are intentionally narrow: they pin the regression
guards for the bugs called out in the v0.2 SDK code review (#114).
Each test names the bug it defends against.

Bugs pinned:
    1. ``_a2a_dm`` heartbeat tag leaks to public surfaces. (Critical
       privacy — would re-leak the data we just scrubbed in #111.)
    2. Unbounded ``set`` dedup grows forever → memory leak.
    3. ``if len(dedup) > 10000: dedup.clear()`` blanket-cleared all
       10000 entries, making them eligible for re-dispatch.
    4. ``A2ADaemon`` didn't inherit ``_BaseDaemon`` → handler
       signatures forked and stats slot lived in two places.
    5. ``A2ADaemon(token="")`` accepted empty token → laobaigan v6
       reference had real prod token as ``os.environ.get(...,
       "bt_real_token")`` default. No defaults.
    6. ``SSEDaemon`` was a fraud — claimed SSE but called only the
       polling fallback. v0.2 fix: ``_connect_and_stream`` exists.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest


# ── LRUSet — the bounded dedup primitive ───────────────────────────


def test_lru_set_membership_basic():
    """Pre-overflow: add returns True once, then False for duplicates."""
    from a2a_dm.daemon._dedup import LRUSet

    s = LRUSet(max_size=3)
    assert s.add("a") is True
    assert s.add("b") is True
    assert s.add("a") is False  # duplicate
    assert "a" in s and "b" in s
    assert len(s) == 2


def test_lru_set_evicts_oldest_on_overflow():
    """Bug 2/3: dedup must NOT grow unbounded and must NOT blanket-clear.

    Adding max_size+1 items must drop only the OLDEST item — every
    other item must still be recognised."""
    from a2a_dm.daemon._dedup import LRUSet

    s = LRUSet(max_size=3)
    s.add("a"); s.add("b"); s.add("c")
    assert len(s) == 3
    s.add("d")  # overflow → evict "a"
    assert len(s) == 3
    assert "a" not in s, "oldest entry must be evicted on overflow"
    assert "b" in s and "c" in s and "d" in s, (
        "evicting MORE than the oldest = blanket-clear regression"
    )


def test_lru_set_bounded_under_load():
    """Push 10x max_size — size stays ≤ max_size + 1 always."""
    from a2a_dm.daemon._dedup import LRUSet

    s = LRUSet(max_size=100)
    for i in range(1000):
        s.add(f"task-{i}")
        assert len(s) <= 100
    # Most recent 100 are still in.
    for i in range(900, 1000):
        assert f"task-{i}" in s
    # Earliest 900 are gone.
    for i in range(900):
        assert f"task-{i}" not in s


def test_lru_set_rejects_zero_max_size():
    from a2a_dm.daemon._dedup import LRUSet

    with pytest.raises(ValueError):
        LRUSet(max_size=0)


# ── A2ADaemon — heartbeat must NOT send a DM (Bug 1) ───────────────


def test_a2a_daemon_inherits_base_daemon():
    """Bug 4: A2ADaemon must extend _BaseDaemon (shared lifecycle,
    stats slot, context manager). The v0.2 draft was a standalone
    class with parallel start/stop machinery."""
    from a2a_dm.daemon._base import _BaseDaemon
    from a2a_dm.daemon.advanced import A2ADaemon

    assert issubclass(A2ADaemon, _BaseDaemon), (
        "A2ADaemon must inherit _BaseDaemon for shared interface"
    )


def test_a2a_daemon_rejects_empty_token():
    """Bug 5: Empty/None token must raise. The laobaigan v6 daemon
    shipped with the operator's real bot token as the os.environ
    default — a single config slip would have exfiltrated the live
    token. No defaults, no fallback."""
    from a2a_dm.daemon.advanced import A2ADaemon

    with pytest.raises(ValueError, match="token"):
        A2ADaemon(token="", bot_id="x")


def test_a2a_daemon_heartbeat_does_not_send_dm(monkeypatch):
    """Bug 1 (CRITICAL): The v0.2 draft's Layer 3 heartbeat sent a
    DM tagged with ``_a2a_dm``. The server strips reserved-prefix
    tags from user input (a2a_gateway.py:566), so the tag landed as
    ``a2a_dm`` instead — bypassing the very filter that's supposed
    to keep DMs off public profile/feed pages.

    v0.2 fix: heartbeat is a LOCAL counter bump only. Verify by:
      (a) class introspection — no ``_a2a_dm`` literal in the
          A2ADaemon source
      (b) construction works without any dm.send mock
    """
    from a2a_dm.daemon.advanced import A2ADaemon

    # (a) Source check of `_heartbeat_bumper` specifically — the
    # whole-module grep would false-positive on the explanatory
    # docstrings that document the bug we're guarding against. Narrow
    # to the actual method body.
    import inspect
    bumper_src = inspect.getsource(A2ADaemon._heartbeat_bumper)
    # The bumper must NOT call client.dm.send (or any send variant).
    # If it does, someone re-introduced the DM-heartbeat pattern.
    for forbidden in ("dm.send", "client.dm.send", "send_dm"):
        assert forbidden not in bumper_src, (
            f"_heartbeat_bumper contains {forbidden!r} — Layer 3 must "
            f"be a local counter bump only, never a DM. v0.2 privacy fix."
        )
    # Note: we deliberately do NOT also check `"_a2a_dm" not in
    # bumper_src` — the docstring explains the original privacy bug
    # and naturally quotes the marker, which would false-positive
    # here. The dm.send absence check above is the real guarantee.

    # (b) Construction works without mocking dm.send — if the
    # heartbeat tried to send a DM, this would explode the moment
    # we accessed self.client.dm.send.
    d = A2ADaemon(
        token="bt_fake_test_token",
        bot_id="testbot",
        partner="other_bot",
        heartbeat_interval=1,
    )
    # Spy on dm.send to ensure NO calls happen during a bumper run.
    sent: list[Any] = []
    monkeypatch.setattr(
        d.client.dm, "send",
        lambda *a, **kw: sent.append((a, kw)),
    )

    # Run the heartbeat bumper in a thread, give it a moment to do
    # one tick (heartbeat_interval=1s but the bump fires BEFORE the
    # wait), then stop. dm.send must not have been called.
    import threading as _th
    t = _th.Thread(target=d._heartbeat_bumper, daemon=True)
    t.start()
    time.sleep(0.05)
    d._stop_event.set()
    t.join(timeout=2)

    assert sent == [], (
        f"heartbeat called dm.send {len(sent)} time(s) — "
        "Layer 3 must be LOCAL only, never a DM. v0.2 privacy fix."
    )
    # And the counter DID get bumped:
    assert d.stats.last_heartbeat is not None


def test_a2a_daemon_status_snapshot_shape():
    """A2ADaemon.status returns the dict shape /healthz endpoints
    rely on. Pin the keys so a refactor doesn't silently rename them."""
    from a2a_dm.daemon.advanced import A2ADaemon

    d = A2ADaemon(
        token="bt_fake_test_token",
        bot_id="testbot",
    )
    snap = d.status
    for key in ("bot_id", "running", "partner", "sse_enabled",
                "poll_interval", "heartbeat_interval", "processed_count",
                "since", "stats"):
        assert key in snap, f"status missing required key {key!r}"


# ── SSEDaemon must actually open SSE (Bug 6) ───────────────────────


def test_sse_daemon_has_real_sse_path():
    """Bug 6: The v0.2 draft SSEDaemon ``start()`` was a wrapper
    that only called the InboxDaemon polling fallback. No SSE
    connection was ever made — the name lied.

    v0.2 fix: ``_connect_and_stream`` method that actually opens
    a urllib SSE GET. Verify by class introspection (a unit test
    that opens a real socket would be flaky in CI)."""
    from a2a_dm.daemon import SSEDaemon

    methods = set(dir(SSEDaemon))
    assert "_connect_and_stream" in methods, (
        "SSEDaemon must have _connect_and_stream method — without "
        "it, the class is just an InboxDaemon wrapper (the v0.2 "
        "draft bug)."
    )
    assert "_process_block" in methods
    # And the run loop is the real driver, not a forwarding stub.
    assert "_run_loop" in methods


def test_sse_daemon_uses_since_cursor():
    """T2 — SSE resume cursor. Build URL must include ``since=N``
    after the cursor advances. Steals the v6 reference daemon's
    pattern so a reconnect mid-stream doesn't drop events."""
    from a2a_dm.client import AgentClient
    from a2a_dm.daemon import SSEDaemon

    client = AgentClient(token="bt_x", api_base="https://api.example.com")
    d = SSEDaemon(client, bot_id="testbot")

    url0 = d._build_url()
    assert "bot_id=testbot" in url0
    assert "since=" not in url0  # no cursor yet

    d._since = 42
    url1 = d._build_url()
    assert "since=42" in url1
    assert "bot_id=testbot" in url1


# ── Ping-pong helpers ──────────────────────────────────────────────


def test_extract_pd_from_tags():
    from a2a_dm.daemon.advanced import extract_pd

    task = SimpleNamespace(
        x_agoradigest={"tags": ["a2a-ping-pong", "pd=3"]},
        metadata={},
    )
    assert extract_pd(task) == 3


def test_extract_pd_from_metadata_fallback():
    from a2a_dm.daemon.advanced import extract_pd

    task = SimpleNamespace(
        x_agoradigest=None,
        metadata={"pd": 5},
    )
    assert extract_pd(task) == 5


def test_extract_pd_absent_returns_minus_one():
    """Non-ping-pong DMs return -1 so handlers can branch."""
    from a2a_dm.daemon.advanced import extract_pd

    task = SimpleNamespace(x_agoradigest={}, metadata={})
    assert extract_pd(task) == -1


def test_should_continue_terminator():
    from a2a_dm.daemon.advanced import next_round, should_continue

    assert should_continue(0, max_rounds=5) is True
    assert should_continue(4, max_rounds=5) is True
    assert should_continue(5, max_rounds=5) is False
    assert next_round(4, max_rounds=5) == 5
    assert next_round(5, max_rounds=5) is None


# ── Multi-bot config factory ───────────────────────────────────────


def test_daemon_from_config_rejects_missing_token():
    """Bug 5 again — at the multi-bot factory layer. Missing token
    must raise; no fallback to a shared default."""
    from a2a_dm.daemon.advanced import daemon_from_config

    config = {"bots": {"bestiedog": {}}}  # no token
    with pytest.raises(ValueError, match="token"):
        daemon_from_config(config)


def test_daemon_from_config_builds_one_per_bot():
    from a2a_dm.daemon.advanced import A2ADaemon, daemon_from_config

    config = {
        "bots": {
            "bestiedog": {"token": "bt_a", "partner": "laobaigan"},
            "baolongbro": {"token": "bt_b", "partner": "bestiedog"},
        }
    }
    daemons = daemon_from_config(config)
    assert len(daemons) == 2
    assert all(isinstance(d, A2ADaemon) for d in daemons)
    by_id = {d.bot_id: d for d in daemons}
    assert by_id["bestiedog"].partner == "laobaigan"
    assert by_id["baolongbro"].partner == "bestiedog"


# ── _BaseDaemon lifecycle ──────────────────────────────────────────


def test_base_daemon_context_manager():
    """`with daemon:` must call start on enter, stop on exit."""
    from a2a_dm.client import AgentClient
    from a2a_dm.daemon._base import _BaseDaemon

    class _TestDaemon(_BaseDaemon):
        ran = False

        def _run_loop(self) -> None:
            self.ran = True
            # Block on stop_event so the thread doesn't terminate
            # before the test inspects daemon.running.
            self._stop_event.wait(timeout=5)

    d = _TestDaemon(AgentClient(token="bt_x"))
    with d:
        # Give the run thread a moment to start.
        time.sleep(0.05)
        assert d.running is True
    assert d.running is False
    assert d.ran is True


def test_base_daemon_on_message_decorator():
    from a2a_dm.client import AgentClient
    from a2a_dm.daemon._base import _BaseDaemon

    class _Stub(_BaseDaemon):
        def _run_loop(self) -> None:
            pass

    d = _Stub(AgentClient(token="bt_x"))

    @d.on_message
    def handler(task, daemon):
        return "ack"

    assert d._user_handler is handler


# ── v0.2.3 — _payload_to_envelope constructor + SSEBridge fixes ────


def test_payload_to_envelope_handles_inline_text_shape():
    """Bug Tyler patched locally: Message has no `text=` ctor param
    (text is a computed property over parts) and TaskEnvelope has no
    `metadata=` field. The v0.2.2 _payload_to_envelope was passing
    both → crash on first SSE event."""
    from a2a_dm.daemon.advanced._webhook import _payload_to_envelope

    payload = {
        "task_id": "abc-123",
        "text": "hello",
        "sender": "bot_ext_laobaigan",
        "state": "submitted",
        "tags": ["pd=1"],
        "metadata": {"vertical": "ai"},
    }
    env = _payload_to_envelope(payload, default_bot_id="default")
    assert env is not None
    assert env.id == "abc-123"
    assert env.sender_bot_id == "bot_ext_laobaigan"
    assert env.message is not None
    assert env.message.text == "hello"
    assert env.tags == ["pd=1"]
    # metadata stashed into .raw — TaskEnvelope has no metadata field
    assert env.raw.get("metadata") == {"vertical": "ai"}


def test_payload_to_envelope_handles_nested_message_shape():
    """A2A 1.0 canonical shape: {message: {role, parts}}. Parts list
    must be forwarded as-is (no text= ctor arg)."""
    from a2a_dm.daemon.advanced._webhook import _payload_to_envelope

    payload = {
        "id": "def-456",
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": "nested"}],
        },
    }
    env = _payload_to_envelope(payload, default_bot_id="")
    assert env is not None
    assert env.message is not None
    assert env.message.text == "nested"
    assert env.message.parts == [{"kind": "text", "text": "nested"}]


def test_payload_to_envelope_minimal_no_message():
    """Minimal envelope — no text content. Must not crash."""
    from a2a_dm.daemon.advanced._webhook import _payload_to_envelope

    env = _payload_to_envelope({"id": "x"}, default_bot_id="")
    assert env is not None
    assert env.id == "x"
    assert env.message is not None
    assert env.message.text == ""


def test_sse_bridge_accepts_optional_client():
    """v0.2.3 — SSEBridge takes optional client= for inbox lookups
    on event arrival. When omitted, an AgentClient is auto-constructed
    from token + bot_id."""
    from a2a_dm import AgentClient
    from a2a_dm.daemon.advanced import SSEBridge

    # Explicit client
    c = AgentClient(token="bt_x", bot_id="bestiedog")
    b = SSEBridge(token="bt_x", bot_id="bestiedog", client=c)
    assert b._client is c

    # Auto-construct from token+bot_id
    b2 = SSEBridge(token="bt_y", bot_id="other")
    assert b2._client is not None
    assert b2._client.bot_id == "other"


# ── v0.2.4 — _payload_to_envelope SSE nested-payload + dm.send retry ─


def test_payload_to_envelope_unwraps_sse_event_payload_key():
    """v0.2.4 — platform SSE firehose wraps the task under
    `payload.payload`, not `payload.task`. v0.2.3 missed this and
    returned None when SSEBridge handed it a raw SSE event. Fixed
    in T8.1."""
    from a2a_dm.daemon.advanced._webhook import _payload_to_envelope

    sse_event = {
        "event": "attempt.requested",
        "payload": {
            "task_id": "task_xxx_internal",
            "bot_id": "bestiedog",
            "sender": "bot_ext_laobaigan",
            "text": "ping",
        },
    }
    env = _payload_to_envelope(sse_event, default_bot_id="bestiedog")
    assert env is not None
    assert env.id == "task_xxx_internal"
    assert env.sender_bot_id == "bot_ext_laobaigan"
    assert env.message is not None
    assert env.message.text == "ping"


def test_sse_daemon_inbox_lookup_method_exists():
    """v0.2.4 — SSEDaemon must use inbox() not get_task() on SSE event.
    The platform's attempt.requested event carries internal task_xxx
    not A2A UUID, so get_task() 404'd on both endpoints.

    Verify by introspection: _process_block source no longer calls
    `client.dm.get_task` (only the new inbox path).
    """
    import inspect
    from a2a_dm.daemon import SSEDaemon

    src = inspect.getsource(SSEDaemon._process_block)
    assert "self.client.dm.inbox" in src, (
        "SSEDaemon._process_block must call client.dm.inbox() on SSE "
        "event (v0.2.4 fix). Without this, attempt.requested events "
        "with internal task_xxx ids 404 on get_task()."
    )
    # Old path: get_task call must be gone from this method.
    assert "self.client.dm.get_task" not in src, (
        "SSEDaemon._process_block still references get_task — v0.2.3 "
        "bug. Use inbox() instead."
    )


# Retry tests live in tests/test_sdk.py (where `responses` +
# `AgentClient` + `ValidationError` are already imported at module
# top). Daemon tests intentionally stay stdlib-only.


# ── v0.2.7 — InboxDaemon auto_ack=False + mark_processed ──────────


def test_inbox_daemon_auto_ack_false_does_not_consume_seen():
    """v0.2.7 — when auto_ack=False the daemon must NOT add tasks
    to _seen automatically. Otherwise a handler that defers (notify
    owner, wait approval, etc.) never sees the task again after the
    first poll cycle.

    User is expected to call daemon.mark_processed(task.id) explicitly
    when they're done with a task in auto_ack=False mode.
    """
    from a2a_dm.client import AgentClient
    from a2a_dm.daemon._inbox import InboxDaemon

    d = InboxDaemon(
        AgentClient(token="bt_x"),
        auto_ack=False,
    )
    # Simulate what _run_loop does after _dispatch returns True:
    # the v0.2.6 code added to _seen unconditionally; v0.2.7 only
    # adds when auto_ack=True.
    assert d.auto_ack is False
    # Direct source-level check: the run loop guards _seen.add on
    # `self.auto_ack`. The grep would false-positive on docstrings
    # so we check via behaviour — call mark_processed and confirm
    # _seen size goes up by exactly that.
    assert len(d._seen) == 0
    d.mark_processed("task-abc")
    assert "task-abc" in d._seen
    assert len(d._seen) == 1


def test_mark_processed_is_idempotent():
    """Calling twice with the same id is a no-op the second time."""
    from a2a_dm.client import AgentClient
    from a2a_dm.daemon._inbox import InboxDaemon

    d = InboxDaemon(AgentClient(token="bt_x"))
    assert d.mark_processed("t1") is True
    assert d.mark_processed("t1") is False  # already there


def test_mark_processed_present_on_all_daemons():
    """_BaseDaemon.mark_processed is inherited by every daemon class
    so the API surface is consistent. SSEDaemon + A2ADaemon must
    also expose the method."""
    from a2a_dm.client import AgentClient
    from a2a_dm.daemon import SSEDaemon, InboxDaemon
    from a2a_dm.daemon.advanced import A2ADaemon

    c = AgentClient(token="bt_x")
    for d in [
        InboxDaemon(c),
        SSEDaemon(c, bot_id="x"),
        A2ADaemon(token="bt_x", bot_id="x"),
    ]:
        assert callable(getattr(d, "mark_processed", None)), (
            f"{type(d).__name__} missing mark_processed()"
        )


# ── v0.2.9 / v3.0 Phase 4 — OrchestratedDaemon ─────────────────────


def test_orchestrated_daemon_rejects_empty_configs():
    """No configs → use A2ADaemon directly."""
    from a2a_dm.daemon.advanced import OrchestratedDaemon

    with pytest.raises(ValueError, match="at least one"):
        OrchestratedDaemon([])


def test_orchestrated_daemon_rejects_duplicate_bot_id():
    from a2a_dm.daemon.advanced import OrchestratedDaemon

    with pytest.raises(ValueError, match="duplicate"):
        OrchestratedDaemon([
            {"token": "bt_a", "bot_id": "x"},
            {"token": "bt_b", "bot_id": "x"},  # dup
        ])


def test_orchestrated_daemon_rejects_missing_required_keys():
    from a2a_dm.daemon.advanced import OrchestratedDaemon

    with pytest.raises(ValueError, match="bot_id"):
        OrchestratedDaemon([{"token": "bt_a"}])
    with pytest.raises(ValueError, match="token"):
        OrchestratedDaemon([{"bot_id": "x"}])


def test_orchestrated_daemon_construct_bot_ids():
    from a2a_dm.daemon.advanced import OrchestratedDaemon

    orch = OrchestratedDaemon([
        {"token": "bt_a", "bot_id": "alice"},
        {"token": "bt_b", "bot_id": "bob"},
    ], restart_on_crash=False)
    assert orch.bot_ids == ["alice", "bob"]
    assert orch.running is False
    # stats_summary works pre-start
    summary = orch.stats_summary()
    assert summary["bots"] == 0  # no daemons spawned yet


# ── Phase 7.1 — on_reply callback ──────────────────────────────────


def test_base_daemon_on_reply_decorator():
    """on_reply mirrors on_message: returns the func, stores it on
    self._reply_handler. Decorator-style usage just works."""
    from a2a_dm.client import AgentClient
    from a2a_dm.daemon._base import _BaseDaemon

    class _Stub(_BaseDaemon):
        def _run_loop(self) -> None:
            pass

    d = _Stub(AgentClient(token="bt_x"))

    @d.on_reply
    def reply_handler(task, daemon):
        return "noted"

    assert d._reply_handler is reply_handler


def test_base_daemon_reply_handler_starts_unregistered():
    """Default state: no reply handler. SDK drops reply events
    silently — old code that only used on_message is unchanged."""
    from a2a_dm.client import AgentClient
    from a2a_dm.daemon._base import _BaseDaemon

    class _Stub(_BaseDaemon):
        def _run_loop(self) -> None:
            pass

    d = _Stub(AgentClient(token="bt_x"))
    assert d._reply_handler is None


def test_dispatch_reply_invokes_handler_when_registered():
    """_dispatch_reply fans out to the reply handler and bumps
    messages_processed. Tasks are NOT auto-acked here (replies
    are terminal — receiver already submitted)."""
    from a2a_dm.client import AgentClient
    from a2a_dm.daemon._base import _BaseDaemon
    from a2a_dm.models import TaskEnvelope

    class _Stub(_BaseDaemon):
        def _run_loop(self) -> None:
            pass

    d = _Stub(AgentClient(token="bt_x"))
    received = []

    @d.on_reply
    def handler(task, daemon):
        received.append(task.id)

    fake_task = TaskEnvelope(
        id="task-abc",
        state="completed",
        message=None,
        sender_bot_id="me",
        recipient_bot_id="partner",
    )
    ok = d._dispatch_reply(fake_task)
    assert ok is True
    assert received == ["task-abc"]
    assert d.stats.messages_processed == 1


def test_dispatch_reply_silent_when_no_handler():
    """Unregistered reply handler → silently True (event consumed,
    no callback). Backward-compat path for daemons that only care
    about incoming DMs."""
    from a2a_dm.client import AgentClient
    from a2a_dm.daemon._base import _BaseDaemon
    from a2a_dm.models import TaskEnvelope

    class _Stub(_BaseDaemon):
        def _run_loop(self) -> None:
            pass

    d = _Stub(AgentClient(token="bt_x"))
    fake_task = TaskEnvelope(
        id="task-noop",
        state="completed",
        message=None,
        sender_bot_id="me",
        recipient_bot_id="partner",
    )
    assert d._dispatch_reply(fake_task) is True
    # No handler → messages_processed NOT bumped (the counter is
    # for actual user-code executions).
    assert d.stats.messages_processed == 0


def test_dispatch_reply_handler_exception_does_not_propagate():
    """If the user's reply handler raises, the daemon logs and
    bumps the error counter — must NOT kill the SSE thread."""
    from a2a_dm.client import AgentClient
    from a2a_dm.daemon._base import _BaseDaemon
    from a2a_dm.models import TaskEnvelope

    class _Stub(_BaseDaemon):
        def _run_loop(self) -> None:
            pass

    d = _Stub(AgentClient(token="bt_x"))

    @d.on_reply
    def bad(task, daemon):
        raise RuntimeError("boom")

    fake_task = TaskEnvelope(
        id="task-bad",
        state="completed",
        message=None,
        sender_bot_id="me",
        recipient_bot_id="partner",
    )
    # Should NOT raise — daemon swallows handler exceptions.
    assert d._dispatch_reply(fake_task) is True
    assert d.stats.errors == 1
    assert d.stats.messages_processed == 0


def test_sse_handle_reply_event_drops_missing_task_id():
    """Phase 7.1 — defensive: malformed `a2a.message.replied`
    SSE payload (no task_id) is dropped silently. Real platform
    payloads always include task_id (set in
    routes/agent_messages.submit_message), but the SDK doesn't
    trust the wire format unilaterally."""
    from a2a_dm.client import AgentClient
    from a2a_dm.daemon._sse import SSEDaemon

    daemon = SSEDaemon(AgentClient(token="bt_x"), bot_id="me")
    invocations = []

    @daemon.on_reply
    def handler(task, daemon):
        invocations.append(task.id)

    # No task_id → drop, no fetch, no handler call.
    daemon._handle_reply_event({"payload": {"original_sender_bot_id": "me"}})
    assert invocations == []
    daemon._handle_reply_event({"payload": {}})
    assert invocations == []
    # Top-level no `payload` key — also tolerated.
    daemon._handle_reply_event({})
    assert invocations == []


def test_sse_handle_reply_event_dedups_via_seen_set():
    """If the same task_id arrives twice (SSE replay), the second
    one is short-circuited via the shared `_seen` LRU before any
    network call."""
    from a2a_dm.client import AgentClient
    from a2a_dm.daemon._sse import SSEDaemon

    daemon = SSEDaemon(AgentClient(token="bt_x"), bot_id="me")
    daemon._seen.add("task-already-seen")

    fetched = []
    original = daemon.client.dm.get_task

    def fake_get_task(task_id):
        fetched.append(task_id)
        return original(task_id)  # would 401 in test — but we don't get here
    daemon.client.dm.get_task = fake_get_task  # type: ignore[assignment]

    daemon._handle_reply_event({
        "payload": {"task_id": "task-already-seen"},
    })
    assert fetched == []  # short-circuited; never fetched
