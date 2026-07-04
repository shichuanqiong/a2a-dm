"""Base daemon class and shared types.

Provides the :class:`_BaseDaemon` foundation that all v0.2 daemon
implementations (InboxDaemon, SSEDaemon, A2ADaemon, WebhookDaemon,
AsyncWebhookDaemon) build on.

Design goals:
    * One handler signature across the whole framework:
      ``handler(task: TaskEnvelope, daemon: _BaseDaemon) -> None``.
      A2ADaemon's "return-string-to-reply" sugar lives in its own
      adapter — see :mod:`a2a_dm.daemon.advanced._a2a`.
    * Lifecycle is start/stop in a background thread + context manager.
      No "blocks forever in main thread" footgun.
    * Stats are a single ``DaemonStats`` dataclass usable as the body
      of a ``/healthz`` response.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Optional

from a2a_dm.client import AgentClient
from a2a_dm.daemon.triage import (
    CapExceededHandler,
    TriageDecision,
    TriagePolicy,
    TurnCounter,
    _log_capped,
    _partner_bot_id_from_task,
)
from a2a_dm.exceptions import TransportError
from a2a_dm.models import TaskEnvelope

logger = logging.getLogger(__name__)


# Single canonical handler signature. Both InboxDaemon and SSEDaemon
# use this; A2ADaemon takes a separate 3-arg `on_message` callback in
# its constructor and wraps it into this signature internally.
MessageHandler = Callable[[TaskEnvelope, "_BaseDaemon"], None]
"""Signature: ``handler(task, daemon) -> None``

Called when a NEW INBOUND DM lands — the receiver-side hook.
"""

# Phase 7.1 — separate hook for "my outgoing DM just got replied to".
# Same signature, different trigger event. SDK invokes this only
# when a `a2a.message.replied` SSE arrives (the route fires this
# from agent_messages.submit_message → original sender's stream).
# Lets agents react to peer replies without polling sent tasks.
ReplyHandler = Callable[[TaskEnvelope, "_BaseDaemon"], None]
"""Signature: ``handler(task, daemon) -> None``

Called when a DM I sent gets replied to. ``task.reply_text`` is
populated; ``task.state`` is ``"completed"``.
"""


@dataclass
class DaemonStats:
    """Runtime statistics for a daemon instance.

    Updated by the daemon on each poll cycle, dispatch, or error.
    Useful for health-check endpoints and monitoring.
    """

    poll_count: int = 0
    messages_processed: int = 0
    last_poll_time: Optional[float] = None
    last_message_time: Optional[float] = None
    errors: int = 0
    running: bool = False
    # v0.2 — replaces "send a DM heartbeat" pattern. Bumps locally
    # every N seconds while the run loop is alive. /healthz endpoints
    # check `time.time() - last_heartbeat < 2 * heartbeat_interval`.
    last_heartbeat: Optional[float] = None
    # Phase 7.4 — triage observability. Bumped each time the daemon
    # silently skips a handler dispatch because the partner's turn
    # cap was reached. Surfaces in `/healthz`-style endpoints so
    # operators notice runaway conversations even without wiring
    # @on_cap_exceeded.
    cap_exceeded_count: int = 0


class _BaseDaemon:
    """Abstract base for all A2A daemon implementations.

    Provides:
      * ``start()`` / ``stop()`` lifecycle in a background thread
      * ``on_message()`` decorator for registering handlers
      * ``__enter__`` / ``__exit__`` context manager support
      * ``_dispatch()`` for safe, ack-aware handler invocation
      * :class:`DaemonStats` tracking

    Subclasses must implement :meth:`_run_loop`.
    """

    def __init__(
        self,
        client: AgentClient,
        *,
        handler: Optional[MessageHandler] = None,
        auto_ack: bool = True,
        name: str = "agora-daemon",
        triage_policy: Optional[TriagePolicy] = None,
    ) -> None:
        self.client = client
        self._user_handler: Optional[MessageHandler] = handler
        # Phase 7.1 — reply hook is registered via @on_reply. None
        # means "drop reply events silently" (backward compat —
        # daemons that only want incoming DMs unchanged).
        self._reply_handler: Optional[ReplyHandler] = None
        # Phase 7.4 — optional cap-exceeded hook. None means "log
        # only, no callback". Daemon still bumps stats.cap_exceeded_count.
        self._cap_exceeded_handler: Optional[CapExceededHandler] = None
        self.auto_ack = auto_ack
        self.name = name
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.stats = DaemonStats()
        # Phase 7.4 — triage policy. None disables the cap entirely
        # (backward compat with pre-7.4 callers). Set to a TriagePolicy
        # to enable per-partner turn caps.
        self.triage_policy: Optional[TriagePolicy] = triage_policy
        self.triage: Optional[TurnCounter] = (
            TurnCounter(client, triage_policy) if triage_policy else None
        )

    # ── lifecycle ─────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self.stats.running = True
        self._thread = threading.Thread(
            target=self._run_wrapper, name=self.name, daemon=True,
        )
        self._thread.start()
        logger.info("%s: background daemon started", self.name)

    def stop(self, timeout_s: float = 10.0) -> None:
        if not self.running:
            return
        logger.info("%s: stopping...", self.name)
        self._stop_event.set()
        self.stats.running = False
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout_s)
        logger.info("%s: stopped", self.name)

    # ── context manager ───────────────────────────────────────────────

    def __enter__(self) -> _BaseDaemon:
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()

    # ── manual dedup control (v0.2.7) ─────────────────────────────────

    def mark_processed(self, task_id: str) -> bool:
        """Tell the daemon this task is done — don't redispatch it.

        v0.2.7 — needed when ``auto_ack=False``. The daemon won't
        automatically add tasks to its dedup set in that mode (so the
        handler can defer or re-fetch); the user signals "I'm done"
        by calling this method.

        Subclasses that maintain a ``_seen`` LRUSet (InboxDaemon,
        SSEDaemon) override this to add the id. The base class is a
        no-op so daemons without dedup don't trip on the call.

        Returns:
            True if the id was newly recorded, False if it was already
            in the dedup set (or the daemon has no dedup).
        """
        seen = getattr(self, "_seen", None)
        if seen is None:
            return False
        try:
            return bool(seen.add(task_id))
        except Exception:
            return False

    # ── handler registration ──────────────────────────────────────────

    def on_message(self, func: MessageHandler) -> MessageHandler:
        """Register a handler for INCOMING DMs (someone messaged me).

        Usable as a decorator::

            daemon = SSEDaemon(client)

            @daemon.on_message
            def handler(task, daemon):
                # task.message.text — what they said
                # task.id — A2A task id; ack/submit by this
                daemon.client.dm.reply(task.id, "got it")

        Triggered by ``a2a.message.sent`` SSE events whose
        ``recipient_bot_id`` is this bot.
        """
        self._user_handler = func
        return func

    def on_reply(self, func: ReplyHandler) -> ReplyHandler:
        """Register a handler for REPLIES TO MY OUTGOING DMs.

        Phase 7.1 — separate hook from :meth:`on_message` because
        the semantics differ: incoming DMs need ack+submit; replies
        are terminal (the conversation half-turn is done from the
        sender's side). Splitting lets the handler avoid the
        "did I send this myself?" check.

        Usable as a decorator::

            @daemon.on_reply
            def handler(task, daemon):
                # task.reply_text — what the receiver said back
                # task.id — original task I sent
                wake_my_llm(context=task.reply_text)

        Triggered by ``a2a.message.replied`` SSE events whose
        ``original_sender_bot_id`` is this bot. Backward compat:
        if no reply handler is registered, the SDK drops the
        event silently (older user code unchanged).
        """
        self._reply_handler = func
        return func

    def on_cap_exceeded(self, func: CapExceededHandler) -> CapExceededHandler:
        """Register a handler that fires when a partner hits the
        per-partner turn cap (Phase 7.4).

        Only meaningful when the daemon was constructed with
        ``triage_policy=TriagePolicy(...)``. With no policy, the
        cap is never reached and this handler never fires.

        Usable as a decorator::

            @daemon.on_cap_exceeded
            def alert(partner_bot_id, decision, daemon):
                # decision.turn_count / .cap / .reason populated
                slack.send(
                    f"Cap hit: {partner_bot_id} "
                    f"({decision.turn_count}/{decision.cap})"
                )

        Daemon does NOT invoke the regular ``@on_message`` /
        ``@on_reply`` handler when this fires — the cap-exceeded
        callback runs instead. To resume the conversation, the
        operator (or the agent itself) resets the partner's
        ``friend.memory['_turn_count']`` to 0.
        """
        self._cap_exceeded_handler = func
        return func

    # ── internal ──────────────────────────────────────────────────────

    def _run_wrapper(self) -> None:
        try:
            self._run_loop()
        except Exception:
            logger.exception("%s: fatal error in run loop", self.name)
            self.stats.errors += 1
        finally:
            self.stats.running = False

    def _run_loop(self) -> None:
        """Override in subclasses with the actual polling/listening logic."""
        raise NotImplementedError

    def _dispatch(self, task: TaskEnvelope) -> bool:
        """Dispatch one inbound task.

        Returns:
            True if the caller should mark the task as seen (i.e. not
            retry on next poll). False signals a recoverable transport
            error — caller should leave the task in the un-seen set so
            it gets retried on the next cycle.
        """
        if self.auto_ack and task.state == "submitted":
            try:
                self.client.dm.ack(task.id)
            except TransportError:
                logger.warning(
                    "%s: ack transport error for %s (will retry on next poll)",
                    self.name, task.id,
                )
                return False  # retry next poll
            except Exception:
                # Non-transport errors (already-acked, 404, etc.) are
                # logged but don't block dispatch — the handler may
                # still want to see the message.
                logger.warning(
                    "%s: ack failed for %s (non-fatal): %s",
                    self.name, task.id, traceback.format_exc(),
                )
        if self._user_handler:
            # Phase 7.4 — triage gate. If the partner is over the
            # cap, skip the user handler entirely and emit the
            # cap-exceeded signal. ack already happened above (we
            # want the platform to know we received it, even when
            # we choose not to respond).
            if not self._triage_gate(task):
                return True
            try:
                self._user_handler(task, self)
                self.stats.messages_processed += 1
                self.stats.last_message_time = time.time()
                # Successful dispatch → bump the partner's turn
                # count so the next inbound message is one step
                # closer to the cap.
                self._triage_bump(task)
            except Exception:
                # Full stack trace to logger; the agents' v0.2 draft
                # only logged the str(e) which made bug-hunting hard.
                logger.exception("handler failed for task %s", task.id)
                self.stats.errors += 1
        return True

    # ── Phase 7.4 — triage internals ──────────────────────────────────

    def _triage_gate(self, task: TaskEnvelope) -> bool:
        """Return True if the handler should run, False if we're
        capped. When capped: bump stats, log, fire optional
        @on_cap_exceeded callback.

        No-op (returns True) when triage_policy is None — backward
        compat with pre-Phase 7.4 daemons.
        """
        if self.triage is None or self.triage_policy is None:
            return True
        partner = _partner_bot_id_from_task(task)
        if not partner:
            # No clear partner id → don't block dispatch. Logging
            # but not gating is the safer failure mode.
            logger.debug(
                "%s: triage skipped, no partner_bot_id on task %s",
                self.name, getattr(task, "id", "<unknown>"),
            )
            return True
        try:
            decision = self.triage.check(partner)
        except Exception:
            # Triage failure (transport, etc.) must not block real
            # work. Log + let the handler run.
            logger.exception(
                "%s: triage.check failed for partner %s — letting "
                "handler run (fail-open)",
                self.name, partner,
            )
            return True
        if decision.should_respond:
            return True
        # Capped — skip handler, emit signal.
        self.stats.cap_exceeded_count += 1
        _log_capped(partner, decision, daemon_name=self.name)
        if self._cap_exceeded_handler:
            try:
                self._cap_exceeded_handler(partner, decision, self)
            except Exception:
                logger.exception(
                    "%s: on_cap_exceeded handler failed for %s",
                    self.name, partner,
                )
        return False

    def _triage_bump(self, task: TaskEnvelope) -> None:
        """Increment the partner's turn counter after a successful
        handler dispatch. No-op when triage_policy is None or the
        task is a reply and the policy says not to count replies.
        """
        if self.triage is None or self.triage_policy is None:
            return
        partner = _partner_bot_id_from_task(task)
        if not partner:
            return
        try:
            self.triage.bump(partner)
        except Exception:
            # Memory write failures shouldn't break the daemon
            # loop. The counter will drift but the cap eventually
            # trips anyway.
            logger.exception(
                "%s: triage.bump failed for partner %s",
                self.name, partner,
            )

    def _dispatch_reply(self, task: TaskEnvelope) -> bool:
        """Dispatch a "my outgoing DM was replied to" task to the
        on_reply handler.

        Phase 7.1 — parallel to ``_dispatch`` but skips the
        ack/auto-ack step (replies are inherently terminal — the
        receiver already submitted). If no reply handler is
        registered, drops silently with True so the caller marks
        the event as seen and doesn't retry.

        Returns:
            True — replies are always considered "handled" (silently
            or via the user hook). False reserved for future
            transport-error semantics if we ever need to re-dispatch.
        """
        if not self._reply_handler:
            return True
        # Phase 7.4 — same triage gate as inbound messages, but
        # respect policy.count_on_replies so daemons that don't
        # follow-up on replies don't drain the budget.
        if (
            self.triage_policy is not None
            and self.triage_policy.count_on_replies
            and not self._triage_gate(task)
        ):
            return True
        try:
            self._reply_handler(task, self)
            # Reuse the same counters — operators looking at
            # /healthz care about "messages handled per minute",
            # not the sent/reply split.
            self.stats.messages_processed += 1
            self.stats.last_message_time = time.time()
            if (
                self.triage_policy is not None
                and self.triage_policy.count_on_replies
            ):
                self._triage_bump(task)
        except Exception:
            logger.exception("reply handler failed for task %s", task.id)
            self.stats.errors += 1
        return True


__all__ = ["MessageHandler", "ReplyHandler", "DaemonStats", "_BaseDaemon"]
