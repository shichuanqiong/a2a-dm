"""Interval-based inbox poller.

Polls ``client.dm.inbox()`` every *interval_s* seconds and dispatches
``submitted``-state DMs to the registered handler. The dedup window is
a bounded LRU (default 10K entries) so the daemon's memory stays flat
indefinitely.

Quickstart::

    from a2a_dm import AgentClient
    from a2a_dm.daemon import InboxDaemon

    client = AgentClient(token="bt_...")

    def handler(task, daemon):
        print(f"Got DM: {task.message.text}")
        daemon.client.dm.reply(task.id, "Got it!")

    with InboxDaemon(client, handler=handler, interval_s=5.0):
        ...
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from a2a_dm.client import AgentClient
from a2a_dm.daemon._base import MessageHandler, _BaseDaemon
from a2a_dm.daemon._dedup import LRUSet
from a2a_dm.exceptions import TransportError

logger = logging.getLogger(__name__)


class InboxDaemon(_BaseDaemon):
    """Interval-based inbox poller.

    Args:
        client: Authenticated :class:`AgentClient`.
        handler: Optional callback ``handler(task, daemon) -> None``.
        interval_s: Polling interval in seconds (min 2, default 10).
        auto_ack: Auto-ack before dispatching (default True).
        dedup_size: LRU dedup capacity (default 10K). Tasks seen within
            this window aren't redispatched even if they reappear in
            the inbox list (e.g. because the previous ack hadn't yet
            been observed by the server).
    """

    def __init__(
        self,
        client: AgentClient,
        *,
        handler: Optional[MessageHandler] = None,
        interval_s: float = 10.0,
        auto_ack: bool = True,
        dedup_size: int = 10_000,
    ) -> None:
        super().__init__(client, handler=handler, auto_ack=auto_ack)
        if interval_s < 2.0:
            logger.info(
                "%s: interval_s=%.1f clamped to 2.0 (minimum)",
                self.name, interval_s,
            )
        self.interval_s = max(2.0, interval_s)
        # v0.2 fix — bounded LRU instead of unbounded set + parallel
        # deque. The agents' draft kept both a set (unbounded) and a
        # deque (bounded), which silently leaked memory: when the
        # deque popped its oldest entry, the corresponding set entry
        # was never removed.
        self._seen: LRUSet = LRUSet(max_size=dedup_size)

    def _run_loop(self) -> None:
        logger.info(
            "%s: polling every %.1fs (auto_ack=%s)",
            self.name, self.interval_s, self.auto_ack,
        )
        while not self._stop_event.is_set():
            poll_start = time.time()
            try:
                inbox = self.client.dm.inbox(include_acked=False)
                self.stats.poll_count += 1
                self.stats.last_poll_time = poll_start
                for task in inbox.pending:
                    if task.id in self._seen:
                        continue
                    logger.info(
                        "%s: DM from %s: %.50s",
                        self.name,
                        task.sender_bot_id or "?",
                        task.message.text if task.message else "",
                    )
                    dispatched = self._dispatch(task)
                    # v0.2.7 fix — when auto_ack=False, the user is
                    # taking explicit control of the task lifecycle.
                    # Marking _seen here would silently dedup the task
                    # before they call ack/submit, which means a
                    # handler that wants to defer (e.g. notify owner,
                    # wait for approval) never sees the task again
                    # after the first poll. So in auto_ack=False mode,
                    # the daemon ONLY adds to _seen when the user
                    # explicitly calls `daemon.mark_processed(task.id)`.
                    # When auto_ack=True (default), the ack has flipped
                    # the task to working-state so it won't reappear
                    # in /inbox?include_acked=false anyway — adding to
                    # _seen is just belt-and-suspenders.
                    if dispatched and self.auto_ack:
                        self._seen.add(task.id)
            except TransportError:
                logger.warning("%s: transport error, retrying", self.name)
                self.stats.errors += 1
            except Exception:
                logger.exception("%s: poll error", self.name)
                self.stats.errors += 1

            elapsed = time.time() - poll_start
            remaining = self.interval_s - elapsed
            if remaining > 0 and not self._stop_event.is_set():
                # Bumps the local heartbeat counter every poll so
                # /healthz consumers can detect a hung loop.
                self.stats.last_heartbeat = time.time()
                self._stop_event.wait(timeout=remaining)


__all__ = ["InboxDaemon"]
