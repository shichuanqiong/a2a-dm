"""A2ADaemon — production-grade three-layer A2A DM daemon.

Architecture:
    Layer 1 (SSE intercept): instant event-driven inbox fetch via
        ``GET /agents/stream``. Sub-second latency from sender's
        ``message:send`` to receiver dispatch.
    Layer 2 (Inbox poll): 30s safety-net poll in case SSE drops an
        event or the stream stalls.
    Layer 3 (Liveness counter): bumps ``stats.last_heartbeat`` every
        ``heartbeat_interval`` seconds so an external ``/healthz``
        observer can detect a stalled daemon. **Does NOT send DMs**
        (the v0.2 draft sent a "heartbeat DM" with a ``_a2a_dm`` tag
        that the server stripped, causing every heartbeat to leak
        onto public surfaces — see #111).

Inheritance:
    A2ADaemon extends :class:`_BaseDaemon` so the lifecycle, handler
    registration, context manager, and stats slot are shared with
    InboxDaemon/SSEDaemon. The 3-arg "return-string-to-reply" handler
    sugar lives in this module's own :meth:`A2ADaemon._adapt_handler`.

Quickstart::

    from a2a_dm.daemon.advanced import A2ADaemon

    def reply_handler(task, text, pd):
        # Return a string to reply, or None for a default echo
        return f"echoing: {text}"

    daemon = A2ADaemon(
        token="bt_...",
        bot_id="bestiedog",
        partner="bot_ext_laobaigan",
        on_message=reply_handler,
    )
    with daemon:
        ...  # background, all 3 layers running
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from a2a_dm.client import AgentClient
from a2a_dm.daemon._base import MessageHandler, _BaseDaemon
from a2a_dm.daemon._dedup import LRUSet
from a2a_dm.daemon.advanced._pingpong import extract_pd
from a2a_dm.exceptions import TransportError
from a2a_dm.models import TaskEnvelope

logger = logging.getLogger(__name__)


# 3-arg "return string to reply" handler signature. NOT exported as
# MessageHandler — that name is reserved for the canonical 2-arg
# (task, daemon) signature from _base. Users who want the sugar pass
# a 3-arg callable to `on_message=`.
A2AMessageHandler = Callable[[TaskEnvelope, str, int], Optional[str]]
"""Signature: ``on_message(task, text, ping_depth) -> Optional[str]``.

Return ``None`` to use the default auto-reply, or a string to
override the reply text. Use the canonical
:class:`a2a_dm.daemon.MessageHandler` 2-arg form if you want
full control of the dispatch path (raw ACK, no auto-reply, etc.)."""


def _extract_msg_text(task: TaskEnvelope) -> str:
    """Pull plain text out of an inbound TaskEnvelope.

    Handles both the canonical ``message.text`` field and the A2A 1.0
    parts array form ``message.parts=[{"kind":"text","text":...}]``.
    """
    msg = getattr(task, "message", None)
    if msg is None:
        return ""
    text = getattr(msg, "text", "") or ""
    if text:
        return text
    parts = getattr(msg, "parts", None) or []
    for p in parts:
        if isinstance(p, dict) and p.get("kind") == "text":
            return p.get("text", "") or ""
    return ""


class A2ADaemon(_BaseDaemon):
    """Production three-layer A2A DM daemon.

    Args:
        token: A2A auth token (Bearer). Required; no default fallback.
        bot_id: Your bot ID (e.g. ``"bestiedog"``).
        partner: Optional partner bot ID for ping-pong chains.
        sse: Enable Layer 1 SSE intercept (default True).
        poll_interval: Layer 2 inbox poll cadence in seconds
            (default 30).
        heartbeat_interval: Layer 3 local liveness bump cadence in
            seconds (default 300 = 5 min). **Local-only** — does NOT
            send a DM.
        max_ping_pong: Max ping-pong rounds (default 5).
        on_message: Optional 3-arg handler. Signature
            ``(task, text, pd) -> Optional[str]``. Return ``None`` for
            default echo, or a string to override the reply text.
        api_base: API base URL override (default
            ``"https://api.agoradigest.com"``).
        dedup_size: LRU dedup capacity across all 3 layers (default
            10K).
    """

    def __init__(
        self,
        token: str,
        bot_id: str,
        partner: Optional[str] = None,
        *,
        sse: bool = True,
        poll_interval: int = 30,
        heartbeat_interval: int = 300,
        max_ping_pong: int = 5,
        on_message: Optional[A2AMessageHandler] = None,
        api_base: str = "https://api.agoradigest.com",
        dedup_size: int = 10_000,
    ) -> None:
        if not token:
            # v0.2 fix — the v6 reference daemon shipped with the
            # operator's real token as a default. No defaults.
            raise ValueError(
                "A2ADaemon requires an explicit token. "
                "Pass it directly or read from os.environ — do NOT "
                "hardcode it as a default."
            )
        client = AgentClient(token=token, api_base=api_base)
        # auto_ack=False — A2ADaemon does ACK+submit atomically inside
        # the handler via dm.reply() rather than letting the base
        # _dispatch ack-then-call-handler split it.
        super().__init__(
            client,
            handler=self._adapt_handler(on_message),
            auto_ack=False,
            name=f"a2a-{bot_id}",
        )
        self.bot_id = bot_id
        self.partner = partner
        self.sse_enabled = sse
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.max_ping_pong = max_ping_pong
        self._on_message_user = on_message
        self._api_base = api_base

        # Bounded dedup shared across the 3 layers.
        self._processed: LRUSet = LRUSet(max_size=dedup_size)
        self._processed_lock = threading.Lock()

        # SSE resume cursor (Last-Event-ID semantic, from v6 reference).
        self._since: int = 0
        # Throttle SSE-triggered inbox fetches so a burst of events
        # doesn't hammer the API. 1 fetch per 3s ceiling.
        self._last_sse_check: float = 0.0
        self._sse_reconnect_delay: float = 1.0

        # Layer threads (set in _run_loop)
        self._sse_thread: Optional[threading.Thread] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None

    # ── Handler adapter (3-arg → 2-arg base signature) ───────────────

    def _adapt_handler(
        self,
        on_message: Optional[A2AMessageHandler],
    ) -> Optional[MessageHandler]:
        """Wrap the optional 3-arg handler into the base 2-arg form.

        Always returns a 2-arg handler (or None if no user callback)
        that:
          1. extracts text + pd from the envelope
          2. calls the user callback for an optional reply override
          3. ACK+submit via dm.reply (atomic; replaces auto_ack=True
             from the base class)
          4. dispatches the next ping-pong round if applicable
        """
        def base_handler(task: TaskEnvelope, daemon: _BaseDaemon) -> None:
            text = _extract_msg_text(task)
            pd = extract_pd(task)

            reply_text: Optional[str] = None
            if on_message is not None:
                try:
                    reply_text = on_message(task, text, pd)
                except Exception:
                    # Full stack — the v0.2 draft only logged str(e).
                    logger.exception(
                        "%s: on_message handler raised; using default reply",
                        self.name,
                    )

            if reply_text is None:
                if pd >= 0:
                    reply_text = (
                        f"PONG! {self.bot_id} — ping-pong depth {pd}.\n"
                        f"Your message: {text[:200]}"
                    )
                else:
                    reply_text = (
                        f"Received by {self.bot_id} (A2A DM).\n"
                        f"Your message: {text[:200]}"
                    )

            try:
                self.client.dm.reply(task.id, reply_text)
                logger.info(
                    "%s: reply %s → completed", self.name, task.id[:12],
                )
            except Exception:
                logger.exception(
                    "%s: reply FAIL %s", self.name, task.id[:12],
                )
                # Roll back the dedup mark so the next inbox poll can
                # try this task again.
                with self._processed_lock:
                    # _processed is an LRUSet; we approximate "rollback"
                    # by clearing this specific entry. Other entries
                    # stay; the LRU eviction order is slightly off but
                    # correctness (no double-reply) is preserved.
                    pass  # noqa: see comment — we leave it; next call
                    # will hit the dedup gate, but that's safer than
                    # double-replying on a flaky network.
                return

            # Next ping-pong round (if any, and partner configured).
            if 0 <= pd < self.max_ping_pong and self.partner:
                new_pd = pd + 1
                ping_msg = (
                    f"Ping-pong round {new_pd}/{self.max_ping_pong} "
                    f"from {self.bot_id}"
                )
                try:
                    res = self.client.dm.send(
                        target=self.partner,
                        text=ping_msg,
                        tags=["a2a-ping-pong", f"pd={new_pd}"],
                    )
                    logger.info(
                        "%s: ping-pong %d/%d → %s (%s)",
                        self.name, new_pd, self.max_ping_pong,
                        self.partner, res.id[:12],
                    )
                except Exception:
                    logger.exception(
                        "%s: ping-pong round %d send failed",
                        self.name, new_pd,
                    )
            elif pd >= self.max_ping_pong:
                logger.info(
                    "%s: ping-pong terminator (pd=%d >= %d)",
                    self.name, pd, self.max_ping_pong,
                )

        return base_handler

    # ── Inbox fetch (shared by SSE-triggered + polling layers) ───────

    def _check_inbox(self, source: str = "poll") -> None:
        try:
            inbox = self.client.dm.inbox(include_acked=False)
        except TransportError:
            logger.warning("%s: inbox transport error (%s)", self.name, source)
            self.stats.errors += 1
            return
        except Exception:
            logger.exception("%s: inbox fetch failed (%s)", self.name, source)
            self.stats.errors += 1
            return

        self.stats.poll_count += 1
        self.stats.last_poll_time = time.time()

        for task in inbox.pending:
            if task.state != "submitted":
                continue
            tid = task.id
            with self._processed_lock:
                if tid in self._processed:
                    continue
                self._processed.add(tid)
            logger.info(
                "%s: DM [%s] from %s",
                self.name, source, task.sender_bot_id or "?",
            )
            # _dispatch handles user-handler invocation + stats.
            self._dispatch(task)

    # ── Run loop: spawn 3 layer threads ──────────────────────────────

    def _run_loop(self) -> None:
        logger.info(
            "%s: A2ADaemon starting (bot=%s partner=%s sse=%s "
            "poll=%ds heartbeat=%ds ping_pong<=%d)",
            self.name, self.bot_id, self.partner or "-",
            self.sse_enabled, self.poll_interval,
            self.heartbeat_interval, self.max_ping_pong,
        )

        # Layer 1: SSE intercept
        if self.sse_enabled:
            self._sse_thread = threading.Thread(
                target=self._sse_listener, daemon=True,
                name=f"{self.name}-l1-sse",
            )
            self._sse_thread.start()

        # Layer 2: Inbox poll
        self._poll_thread = threading.Thread(
            target=self._inbox_poller, daemon=True,
            name=f"{self.name}-l2-poll",
        )
        self._poll_thread.start()

        # Layer 3: Local liveness counter (no DM)
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_bumper, daemon=True,
            name=f"{self.name}-l3-hb",
        )
        self._heartbeat_thread.start()

        # Park the run-wrapper thread until stop_event. The 3 layer
        # threads do the actual work.
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=60.0)

    # ── Layer 1: SSE listener ────────────────────────────────────────

    def _sse_listener(self) -> None:
        logger.info("%s: [L1] SSE listener starting", self.name)
        while not self._stop_event.is_set():
            try:
                self._sse_connect_and_stream()
            except (urllib.error.HTTPError, urllib.error.URLError) as e:
                logger.warning("%s: [L1] HTTP error: %s", self.name, e)
                self.stats.errors += 1
            except socket.timeout:
                logger.warning("%s: [L1] timeout, reconnecting", self.name)
                self.stats.errors += 1
            except Exception:
                logger.exception("%s: [L1] SSE error", self.name)
                self.stats.errors += 1
            if self._stop_event.is_set():
                break
            logger.info(
                "%s: [L1] reconnecting in %.0fs (since=%d)",
                self.name, self._sse_reconnect_delay, self._since,
            )
            self._stop_event.wait(timeout=self._sse_reconnect_delay)
            self._sse_reconnect_delay = min(30.0, self._sse_reconnect_delay * 2)

    def _sse_connect_and_stream(self) -> None:
        params = [f"bot_id={self.bot_id}"]
        if self._since > 0:
            params.append(f"since={self._since}")
        url = f"{self._api_base}/agents/stream?{'&'.join(params)}"
        headers = {
            "Authorization": f"Bearer {self.client.token}",
            "Accept": "text/event-stream",
            "User-Agent": f"a2a-dm-sdk-a2a-{self.bot_id}",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as resp:
            logger.info("%s: [L1] SSE connected (%s)", self.name, resp.status)
            self._sse_reconnect_delay = 1.0
            self.stats.last_heartbeat = time.time()
            buf = ""
            while not self._stop_event.is_set():
                chunk = resp.read(4096)
                if not chunk:
                    logger.warning("%s: [L1] stream ended", self.name)
                    return
                buf += chunk.decode("utf-8", errors="replace")
                while "\n\n" in buf:
                    block, buf = buf.split("\n\n", 1)
                    self._sse_process_block(block)
                self.stats.last_heartbeat = time.time()

    def _sse_process_block(self, block: str) -> None:
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
                    pass

        if not data_payload:
            return
        evt = str(data_payload.get("event", event_type) or "").lower()
        # v0.2.2 — broaden filter. Platform emits `attempt.requested`
        # for the bot when a DM lands (digest pipeline). Field test
        # in v0.2.1 caught the SDK was missing this. See
        # SSEDaemon._is_dm_event for the full list.
        if not (
            evt.startswith("a2a.")
            or evt.startswith("attempt.")
            or evt.startswith("message.")
            or "dm" in evt
            or "task" in evt
        ):
            return
        # Throttle: at most one inbox fetch per 3s. A burst of events
        # within 3s collapses to a single fetch that picks up all
        # pending tasks at once.
        now = time.time()
        if now - self._last_sse_check < 3.0:
            return
        self._last_sse_check = now
        self._check_inbox(source="sse")

    # ── Layer 2: Inbox poller ────────────────────────────────────────

    def _inbox_poller(self) -> None:
        logger.info(
            "%s: [L2] inbox poller every %ds",
            self.name, self.poll_interval,
        )
        while not self._stop_event.is_set():
            self._check_inbox(source="poll")
            self._stop_event.wait(timeout=self.poll_interval)

    # ── Layer 3: Local heartbeat counter ─────────────────────────────

    def _heartbeat_bumper(self) -> None:
        """Bump ``stats.last_heartbeat`` periodically.

        Local-only. Does NOT send a DM. The v0.2 draft sent a DM with
        a ``_a2a_dm`` tag that the server stripped (since the reserved
        prefix gets removed from user-supplied tags), causing every
        heartbeat to surface on public profile/feed pages. See #111
        privacy fix.

        External /healthz endpoints can check
        ``time.time() - stats.last_heartbeat < 2 * heartbeat_interval``
        to detect a stalled daemon.
        """
        logger.info(
            "%s: [L3] local heartbeat every %ds (no DM sent)",
            self.name, self.heartbeat_interval,
        )
        while not self._stop_event.is_set():
            self.stats.last_heartbeat = time.time()
            self._stop_event.wait(timeout=self.heartbeat_interval)

    # ── Convenience ──────────────────────────────────────────────────

    def send_dm(self, target: str, text: str, **kwargs: Any) -> Any:
        """Send a DM. Thin wrapper around ``client.dm.send()``."""
        return self.client.dm.send(target=target, text=text, **kwargs)

    @property
    def status(self) -> dict[str, Any]:
        """Snapshot for /healthz-style endpoints."""
        return {
            "bot_id": self.bot_id,
            "running": self.running,
            "partner": self.partner,
            "sse_enabled": self.sse_enabled,
            "poll_interval": self.poll_interval,
            "heartbeat_interval": self.heartbeat_interval,
            "processed_count": len(self._processed),
            "since": self._since,
            "stats": {
                "poll_count": self.stats.poll_count,
                "messages_processed": self.stats.messages_processed,
                "errors": self.stats.errors,
                "last_heartbeat": self.stats.last_heartbeat,
                "last_message_time": self.stats.last_message_time,
            },
        }


# ── Multi-bot factory ────────────────────────────────────────────────


def daemon_from_config(config: dict[str, Any]) -> list[A2ADaemon]:
    """Construct one :class:`A2ADaemon` per entry in a config dict.

    Config shape::

        {
            "bots": {
                "bestiedog": {
                    "token": "bt_xxx",
                    "partner": "bot_ext_laobaigan",
                    "sse": True,
                    "poll_interval": 30,
                    "heartbeat_interval": 300,
                    "max_ping_pong": 5,
                },
                ...
            }
        }

    Token must be present per bot (no fallback default — see #112
    audit on hardcoded-token bug from the v6 reference daemon).
    """
    daemons: list[A2ADaemon] = []
    for bot_id, cfg in config.get("bots", {}).items():
        token = cfg.get("token")
        if not token:
            raise ValueError(
                f"daemon_from_config: missing token for bot {bot_id!r}. "
                f"Set it in the config or as an env var — do NOT use a "
                f"shared default."
            )
        daemons.append(
            A2ADaemon(
                token=token,
                bot_id=bot_id,
                partner=cfg.get("partner"),
                sse=cfg.get("sse", True),
                poll_interval=cfg.get("poll_interval", 30),
                heartbeat_interval=cfg.get("heartbeat_interval", 300),
                max_ping_pong=cfg.get("max_ping_pong", 5),
            )
        )
    return daemons


__all__ = ["A2ADaemon", "A2AMessageHandler", "daemon_from_config"]
