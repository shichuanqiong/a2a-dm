"""SSE-based inbox listener with InboxDaemon polling fallback.

Connects to the platform SSE stream (``GET /agents/stream``) and
dispatches new DM events to the registered handler in real time.
A safety-net :class:`InboxDaemon` polls the inbox at a slower cadence
in case an SSE event is dropped or the stream stalls.

This v0.2 implementation is **not** the same as the v0.2 draft the
agents shipped — that draft's `SSEDaemon` was a pure InboxDaemon
wrapper that never opened an SSE connection. This version actually
streams.

Resume cursor (``since=N``)
    Adopted from the v6 single-file reference daemon. On reconnect
    the SSE GET re-sends the last-seen event id so the platform can
    replay any events missed during the disconnect window. Without
    this, a network blip during deploy → lost events forever.

Quickstart::

    from a2a_dm import AgentClient
    from a2a_dm.daemon import SSEDaemon

    client = AgentClient(token="bt_...")

    def handler(task, daemon):
        daemon.client.dm.reply(task.id, "got it")

    with SSEDaemon(client, bot_id="bestiedog", handler=handler):
        ...
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from a2a_dm.client import AgentClient
from a2a_dm.daemon._base import MessageHandler, _BaseDaemon
from a2a_dm.daemon._dedup import LRUSet
from a2a_dm.daemon._inbox import InboxDaemon
from a2a_dm.exceptions import TransportError

logger = logging.getLogger(__name__)


class SSEDaemon(_BaseDaemon):
    """SSE listener with inbox polling fallback.

    Runs two threads:
      * SSE thread — connects to ``/agents/stream`` and processes
        events as they arrive. On disconnect, exponential backoff
        with the last-seen event id resent so the server can replay.
      * Polling thread — an internal :class:`InboxDaemon` that polls
        the inbox at *fallback_interval_s* as insurance against
        dropped SSE events.

    Both threads share a single :class:`LRUSet` so a message that
    arrives via SSE is suppressed on the next polling cycle (and vice
    versa). Auto-ack and dispatch go through the same
    :meth:`_BaseDaemon._dispatch` so the user handler is called at
    most once per task.

    Args:
        client: Authenticated :class:`AgentClient`.
        bot_id: Bot ID for the SSE subscription path.
        handler: Optional ``handler(task, daemon) -> None``.
        sse_path: SSE endpoint relative to the API base
            (default ``"/agents/stream"``).
        fallback_interval_s: Polling fallback cadence (default 30s —
            higher than InboxDaemon's default because SSE handles the
            real-time path).
        auto_ack: Auto-ack before dispatching (default True).
        dedup_size: LRU dedup capacity (default 10K).
    """

    def __init__(
        self,
        client: AgentClient,
        *,
        bot_id: Optional[str] = None,
        handler: Optional[MessageHandler] = None,
        sse_path: str = "/agents/stream",
        fallback_interval_s: float = 30.0,
        auto_ack: bool = True,
        dedup_size: int = 10_000,
    ) -> None:
        super().__init__(client, handler=handler, auto_ack=auto_ack, name="sse-daemon")
        self.bot_id = bot_id or client.bot_id or ""
        self.sse_path = sse_path
        self.fallback_interval_s = fallback_interval_s
        # Shared dedup between SSE thread and fallback poll thread.
        self._seen: LRUSet = LRUSet(max_size=dedup_size)
        # SSE resume cursor — set from `id:` lines, sent as `?since=N`
        # on reconnect. Last-seen-id semantics from the v6 reference.
        self._since: int = 0
        # Fallback InboxDaemon (composition, not inheritance). We
        # override its dispatch so it shares this daemon's dedup +
        # handler.
        self._fallback = InboxDaemon(
            client,
            handler=handler,
            interval_s=fallback_interval_s,
            auto_ack=auto_ack,
            dedup_size=dedup_size,
        )
        # Re-point fallback dedup + handler at the parent so both
        # threads see the same view of "what's been seen".
        self._fallback._seen = self._seen
        # Reconnect backoff: starts at 1s, caps at 30s
        self._reconnect_delay: float = 1.0
        # v0.2.4 — throttle SSE-triggered inbox fetches to 1/3s under
        # event bursts (collapses round-trip multi-DM bursts).
        self._last_inbox_fetch: float = 0.0

    # Note on stats: ``self.stats`` reflects the SSE thread (inherited
    # from ``_BaseDaemon``). The fallback poll thread's stats are at
    # ``daemon._fallback.stats`` for callers who care about the split.
    # We deliberately don't aggregate — a single counter is more
    # useful than a fused one for /healthz consumers.

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        if self.running:
            return
        # Fire the fallback poll thread first so it's ready to catch
        # any messages that arrive before the SSE connects.
        # Share the user handler with the fallback explicitly — we
        # may have been constructed with handler=None and had one
        # registered later via @on_message.
        self._fallback._user_handler = self._user_handler
        self._fallback.start()
        super().start()

    def stop(self, timeout_s: float = 10.0) -> None:
        # Stop SSE thread first (long-poll), then fallback (short-cycle).
        super().stop(timeout_s=timeout_s)
        self._fallback.stop(timeout_s=timeout_s)

    def on_message(self, func: MessageHandler) -> MessageHandler:
        # Mirror the registration to the fallback so a handler set
        # after construction reaches both code paths.
        super().on_message(func)
        self._fallback.on_message(func)
        return func

    # Phase 7.1 — on_reply doesn't need to be mirrored to the
    # fallback InboxDaemon: that daemon polls /messages/inbox which
    # is by definition INCOMING. Reply events are SSE-only on the
    # outgoing side. So we keep the parent class's straight
    # registration and skip the fallback mirror. (If we ever add
    # an OutboxDaemon polling /messages/sent for state changes,
    # this is where it'd get wired.)
    # Inherited on_reply() from _BaseDaemon is sufficient.

    # ── SSE thread (main run loop) ────────────────────────────────────

    def _run_loop(self) -> None:
        """Open SSE, process events, reconnect with backoff."""
        while not self._stop_event.is_set():
            try:
                self._connect_and_stream()
                # _connect_and_stream returns when the stream ends
                # cleanly; treat that the same as an error for backoff.
            except (urllib.error.HTTPError, urllib.error.URLError) as e:
                logger.warning("%s: SSE HTTP error: %s", self.name, e)
                self.stats.errors += 1
            except socket.timeout:
                logger.warning("%s: SSE timeout", self.name)
                self.stats.errors += 1
            except Exception:
                logger.exception("%s: SSE error", self.name)
                self.stats.errors += 1

            if self._stop_event.is_set():
                break
            # Exponential backoff, capped at 30s. Reset to 1s on next
            # successful connect (handled inside _connect_and_stream).
            logger.info(
                "%s: reconnecting in %.0fs (since=%d)...",
                self.name, self._reconnect_delay, self._since,
            )
            self._stop_event.wait(timeout=self._reconnect_delay)
            self._reconnect_delay = min(30.0, self._reconnect_delay * 2)

    def _connect_and_stream(self) -> None:
        """One SSE connect-and-read cycle. Returns on EOF or stop."""
        url = self._build_url()
        headers = {
            "Authorization": f"Bearer {self.client.token}",
            "Accept": "text/event-stream",
            "User-Agent": f"a2a-dm-sdk-sse-{self.bot_id or 'anon'}",
        }
        req = urllib.request.Request(url, headers=headers)
        # 120s read timeout — server sends keepalive every ~30s.
        with urllib.request.urlopen(req, timeout=120) as resp:
            logger.info("%s: SSE connected (%s)", self.name, resp.status)
            # Reset backoff on successful connect.
            self._reconnect_delay = 1.0
            self.stats.last_heartbeat = time.time()
            buf = ""
            while not self._stop_event.is_set():
                chunk = resp.read(4096)
                if not chunk:
                    logger.warning("%s: SSE stream ended", self.name)
                    return
                buf += chunk.decode("utf-8", errors="replace")
                while "\n\n" in buf:
                    block, buf = buf.split("\n\n", 1)
                    self._process_block(block)
                self.stats.last_heartbeat = time.time()

    def _build_url(self) -> str:
        base = self.client.api_base.rstrip("/")
        params = []
        if self.bot_id:
            params.append(f"bot_id={self.bot_id}")
        if self._since > 0:
            # Server-side replay cursor — see v6 daemon notes.
            params.append(f"since={self._since}")
        qs = ("?" + "&".join(params)) if params else ""
        return f"{base}{self.sse_path}{qs}"

    def _process_block(self, block: str) -> None:
        """Parse one SSE block and dispatch if it's a DM event.

        Each block is a set of newline-separated ``key: value`` lines
        terminated by a blank line. We care about ``id:`` (for the
        resume cursor) and ``data:`` (the JSON payload).
        """
        event_type = ""
        data_payload: Optional[dict[str, Any]] = None
        for line in block.split("\n"):
            if line.startswith("id: "):
                try:
                    self._since = int(line[4:].strip())
                except ValueError:
                    pass
            elif line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: "):
                try:
                    data_payload = json.loads(line[6:])
                except json.JSONDecodeError:
                    logger.debug(
                        "%s: SSE non-JSON data line dropped", self.name,
                    )

        if not data_payload:
            return

        evt_name = str(data_payload.get("event", event_type) or "")
        # We only care about A2A task-bearing events. Other firehose
        # events (digest publishes, dispute opens, etc.) are ignored.
        if not self._is_dm_event(evt_name):
            return
        self.stats.poll_count += 1
        self.stats.last_poll_time = time.time()

        # Phase 7.1 — REPLY path. The platform fires
        # `a2a.message.replied` to the ORIGINAL sender when their
        # receiver submits. Different code path from incoming DMs:
        # don't fetch inbox (replies aren't in inbox — they're on
        # outgoing tasks). Instead fetch the specific task and
        # dispatch via _dispatch_reply.
        if evt_name == "a2a.message.replied":
            self._handle_reply_event(data_payload)
            return

        # v0.2.4 — use SSE event as a wake signal only; fetch the
        # canonical inbox to get tasks with A2A UUIDs.
        #
        # WHY: the platform's `attempt.requested` event carries the
        # INTERNAL `task_xxx` id in payload.task_id, NOT the A2A
        # UUID. v0.2.3 SSEDaemon called `get_task(task_xxx)` and
        # 404'd on both endpoints. The inbox endpoint is the only
        # source-of-truth for A2A UUIDs, and it returns ALL pending
        # tasks at once — usually what the handler wants anyway.
        #
        # Throttle: at most one inbox fetch per 3s under a burst of
        # events (e.g. a multi-DM round). Repeated wake signals
        # collapse to a single inbox sweep that picks up everything.
        now = time.time()
        if hasattr(self, "_last_inbox_fetch") and now - self._last_inbox_fetch < 3.0:
            return
        self._last_inbox_fetch = now

        try:
            inbox = self.client.dm.inbox(include_acked=False)
        except TransportError:
            logger.warning(
                "%s: SSE-triggered inbox fetch transport error; fallback "
                "poll thread will catch any missed tasks",
                self.name,
            )
            self.stats.errors += 1
            return
        except Exception:
            logger.exception("%s: SSE-triggered inbox fetch failed", self.name)
            self.stats.errors += 1
            return

        for envelope in inbox.pending:
            if envelope.id in self._seen:
                continue
            if envelope.state != "submitted":
                self._seen.add(envelope.id)
                continue
            if self._dispatch(envelope):
                self._seen.add(envelope.id)

    def _handle_reply_event(self, data_payload: dict[str, Any]) -> None:
        """Phase 7.1 — fan out an `a2a.message.replied` SSE event
        to the registered on_reply handler.

        The SSE payload only carries ids (task_id +
        original_sender_bot_id + responder_bot_id) — privacy-safe.
        To give the handler the full reply text + state, we fetch
        the task via ``client.dm.get_task(task_id)`` first.

        Dedup share: we add the task id to ``self._seen`` so that
        if the same reply lands again via SSE replay or the
        fallback poll thread, the handler isn't invoked twice.
        """
        # Payload shape (set in routes/agent_messages.py):
        #   {"task_id": str, "original_sender_bot_id": str,
        #    "responder_bot_id": str}
        # The outer SSE envelope wraps it in `payload`.
        payload = data_payload.get("payload") or {}
        task_id = payload.get("task_id")
        if not task_id:
            logger.debug(
                "%s: a2a.message.replied missing task_id, dropping",
                self.name,
            )
            return
        # Dedup before the fetch — SSE replay on reconnect can
        # re-emit the same event.
        if task_id in self._seen:
            return

        try:
            task = self.client.dm.get_task(task_id)
        except TransportError:
            logger.warning(
                "%s: reply-event transport error for %s; will be "
                "retried on reconnect",
                self.name, task_id,
            )
            self.stats.errors += 1
            return
        except Exception:
            logger.exception(
                "%s: reply-event fetch failed for %s", self.name, task_id,
            )
            self.stats.errors += 1
            return

        if self._dispatch_reply(task):
            self._seen.add(task_id)

    @staticmethod
    def _is_dm_event(evt_name: str) -> bool:
        """Heuristic: which SSE events are worth fetching the inbox for.

        The platform firehose emits many event types; we react to any
        that *could* carry an inbox change for the subscribed bot:

          * ``a2a.*`` — canonical A2A namespace (e.g. ``a2a.message.sent``)
          * ``attempt.*`` — platform's "you're asked to respond" signal
            (``attempt.requested`` fires for both public Qs and DMs;
            laobaigan's field test in v0.2.1 caught that the SDK was
            missing this — the filter was previously a2a-only)
          * ``message.*`` — explicit message lifecycle events
          * any name containing ``dm`` or ``task`` — defensive net for
            future event types

        Over-broad on purpose: a spurious inbox fetch is cheap; a
        missed event means the daemon's Layer 1 silently fails.
        """
        if not evt_name:
            return False
        lower = evt_name.lower()
        return (
            lower.startswith("a2a.")
            or lower.startswith("attempt.")
            or lower.startswith("message.")
            or "dm" in lower
            or "task" in lower
        )


__all__ = ["SSEDaemon"]
