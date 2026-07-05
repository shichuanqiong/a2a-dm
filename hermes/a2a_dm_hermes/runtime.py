"""Background runtime — SSEDaemon + wake queue + auto-wake (v0.1.2).

The plugin starts an :class:`SSEDaemon` at plugin-load time. Every
inbound DM now takes THREE paths, in order of preference:

  1. **Auto-wake** (new in v0.1.2) — POST the DM to the gateway's own
     webhook adapter, which triggers a *real agent turn* immediately.
     The agent reads the DM and replies with no human in the loop.
     See :mod:`a2a_dm_hermes.autowake`.
  2. **Operator notification** — if auto-wake is disabled or the
     webhook platform isn't running, tell the human through the
     3-tier delivery ladder (gateway bot → ``hermes send`` → legacy
     second bot). See :mod:`a2a_dm_hermes.delivery`.
  3. **Next-turn injection** (always on, belt-and-suspenders) — the
     DM is queued and drained into ``pre_llm_call`` context on the
     next agent turn, whatever triggered it.

v0.1.2 also adds an inbox *fallback scan* (5-second cache) so DMs
that arrived while the SSE stream was disconnected are still injected
on the next turn, and a session-start seed for gateway reboots.

Design notes:

* **Leader-lock singleton.** ``fcntl.flock`` on
  ``~/.hermes/a2a-dm-ws.lock`` so multiple Hermes processes on one
  machine don't all open their own SSE.
* **Bounded queue.** 200-DM cap; overflow drops from the injection
  buffer only — DMs remain in the AgoraDigest inbox.
* **Best-effort everywhere.** SSE outages, wake failures, and notify
  failures are logged, never raised.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Deque, Optional

from a2a_dm import AgentClient
from a2a_dm.daemon import SSEDaemon

from a2a_dm_hermes.autowake import AutoWake
from a2a_dm_hermes.autowake import enabled as autowake_enabled
from a2a_dm_hermes.delivery import notify_operator

logger = logging.getLogger(__name__)


_WAKE_QUEUE_MAX = 200
_SEEN_MAX = 2048
_INBOX_CACHE_S = 5.0


def _format_notification(entry: dict) -> str:
    """Compact operator-facing notification for one wake entry."""
    text_preview = (entry.get("text") or "")[:200]
    sender = entry.get("sender_bot_id") or "?"
    task_id = entry.get("task_id") or ""
    if entry.get("group_id"):
        return (
            f"🔔 a2a-dm group message\n"
            f"From: @{sender}\n"
            f"Group: {entry['group_id']}\n"
            f"Text: {text_preview}\n\n"
            f"Reply into group via a2a_send_group\n"
            f"Task: {task_id[:12]}"
        )
    return (
        f"🔔 a2a-dm DM from @{sender}\n"
        f"Text: {text_preview}\n\n"
        f"Reply: a2a_reply on task {task_id[:12]}"
    )


def _entry_from_task(task) -> dict:
    return {
        "task_id":          task.id,
        "sender_bot_id":    task.sender_bot_id,
        "text":             (task.message.text if task.message else "")[:2000],
        "is_group_message": task.is_group_message,
        "group_id":         task.group_id,
        "created_at":       task.created_at,
    }


class WakeRuntime:
    """One-per-process singleton that owns the SSE connection + queue."""

    _instance: Optional["WakeRuntime"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._queue: Deque[dict] = deque(maxlen=_WAKE_QUEUE_MAX)
        self._queue_lock = threading.Lock()
        self._daemon: Optional[SSEDaemon] = None
        self._leader_fd = None  # kept alive to hold the flock
        self._client: Optional[AgentClient] = None
        self._autowake: Optional[AutoWake] = None
        # Task ids already queued / drained / auto-woken — stops the
        # inbox fallback scan from re-injecting handled DMs.
        self._seen: Deque[str] = deque(maxlen=_SEEN_MAX)
        self._seen_set: set[str] = set()
        self._last_inbox_scan: float = 0.0

    # ── Singleton accessor ────────────────────────────────────────

    @classmethod
    def get(cls) -> "WakeRuntime":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ── Leader-lock so N Hermes processes don't all open SSE ──────

    def _acquire_leader(self) -> bool:
        """Return True if this process becomes the SSE leader."""
        try:
            import fcntl  # POSIX only
        except ImportError:
            return True  # Windows — no flock; single-process assumption

        hermes_home = Path(
            os.environ.get("HERMES_HOME") or Path.home() / ".hermes"
        )
        hermes_home.mkdir(parents=True, exist_ok=True)
        lock_path = hermes_home / "a2a-dm-ws.lock"
        try:
            fd = open(lock_path, "w")  # noqa: SIM115 — kept for lifetime
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._leader_fd = fd
            fd.write(str(os.getpid()))
            fd.flush()
            return True
        except (BlockingIOError, OSError):
            logger.info(
                "a2a-dm: another Hermes process holds the SSE leader "
                "lock — running in follower mode (tools still work)."
            )
            return False

    # ── Public lifecycle ──────────────────────────────────────────

    def start(self) -> None:
        """Bring up the SSE daemon. No-op if follower / missing env."""
        if self._daemon is not None:
            return  # already started

        token = os.environ.get("AGORADIGEST_TOKEN")
        bot_id = os.environ.get("AGORADIGEST_BOT_ID")
        if not token or not bot_id:
            logger.warning(
                "a2a-dm: AGORADIGEST_TOKEN / AGORADIGEST_BOT_ID not set. "
                "Real-time wake disabled. Set them in ~/.hermes/.env "
                "and restart the gateway."
            )
            return

        if not self._acquire_leader():
            return

        self._client = AgentClient(token=token, bot_id=bot_id)
        self._autowake = AutoWake(bot_id)
        if autowake_enabled():
            # Register webhook routes eagerly so the first DM doesn't
            # pay the setup cost. Failure just logs + falls back.
            self._autowake.ensure_ready()

        self._daemon = SSEDaemon(
            self._client,
            bot_id=bot_id,
            handler=self._on_wake,
            fallback_interval_s=30.0,
            auto_ack=False,  # the agent's reply tools decide when to ack
        )
        self._daemon.start()
        logger.info(
            "a2a-dm: SSE wake runtime up (bot=%s, leader=%s, auto_wake=%s)",
            bot_id, self._leader_fd is not None, autowake_enabled(),
        )

    def stop(self) -> None:
        if self._daemon is not None:
            try:
                self._daemon.stop()
            except Exception:  # noqa: BLE001
                pass
            self._daemon = None
        if self._leader_fd is not None:
            try:
                self._leader_fd.close()
            except Exception:  # noqa: BLE001
                pass
            self._leader_fd = None

    # ── Seen-set helpers ──────────────────────────────────────────

    def _mark_seen(self, task_id: str) -> None:
        if not task_id or task_id in self._seen_set:
            return
        if len(self._seen) == self._seen.maxlen:
            oldest = self._seen[0]
            self._seen_set.discard(oldest)
        self._seen.append(task_id)
        self._seen_set.add(task_id)

    # ── SSE callback ──────────────────────────────────────────────

    def _on_wake(self, task, daemon) -> None:  # noqa: ARG002
        """Fired on every inbound DM (SSE or polling fallback).

        Queue for next-turn injection, then in a background thread try
        auto-wake (real turn now); if that can't run, notify the
        operator through the delivery ladder.
        """
        entry = _entry_from_task(task)
        with self._queue_lock:
            self._queue.append(entry)
            self._mark_seen(entry["task_id"])
        logger.debug(
            "a2a-dm: enqueued wake from %s (task=%s, group=%s)",
            entry["sender_bot_id"], entry["task_id"][:12],
            entry["group_id"] or "-",
        )

        # Network I/O off the SSE dispatch thread.
        threading.Thread(
            target=self._wake_or_notify,
            args=(entry,),
            daemon=True,
            name="a2a-dm-wake",
        ).start()

    def _wake_or_notify(self, entry: dict) -> None:
        try:
            if self._autowake is not None and self._autowake.wake(entry):
                # A real agent turn is running; its response is
                # delivered to the home channel by the gateway — an
                # extra ping here would be a double notification.
                return
            notify_operator(_format_notification(entry))
        except Exception:  # noqa: BLE001
            logger.exception("a2a-dm: wake-or-notify crashed")

    # ── Inbox fallback scan (v0.1.2) ──────────────────────────────

    def seed_from_inbox(self, *, force: bool = False, limit: int = 20) -> int:
        """Queue pending inbox tasks that we haven't seen yet.

        Called from the ``session:start`` hook (force=True) and from
        every ``pre_llm_call`` (5-second cache) as insurance against
        SSE gaps. Returns the number of newly queued entries.
        """
        if self._client is None:
            return 0
        now = time.monotonic()
        if not force and (now - self._last_inbox_scan) < _INBOX_CACHE_S:
            return 0
        self._last_inbox_scan = now
        try:
            view = self._client.dm.inbox(state="submitted", limit=limit)
        except Exception:  # noqa: BLE001
            logger.debug("a2a-dm: inbox fallback scan failed", exc_info=True)
            return 0
        added = 0
        with self._queue_lock:
            for task in view.tasks:
                if task.id in self._seen_set:
                    continue
                self._queue.append(_entry_from_task(task))
                self._mark_seen(task.id)
                added += 1
        if added:
            logger.info(
                "a2a-dm: inbox fallback scan queued %d missed DM(s)", added
            )
        return added

    # ── pre_llm_call drain ────────────────────────────────────────

    def drain(self) -> list[dict]:
        """Return + clear all pending wake entries."""
        with self._queue_lock:
            drained = list(self._queue)
            self._queue.clear()
        return drained

    def pending_count(self) -> int:
        with self._queue_lock:
            return len(self._queue)


# ── Formatting helper for injection ───────────────────────────────


def format_wake_context(entries: list[dict]) -> str:
    """Turn the drained queue into a compact system-context block."""
    if not entries:
        return ""
    lines = [
        f"You have {len(entries)} new agent-DM(s) waiting. Handle "
        "them alongside the user's message (or address the user "
        "first and then the DMs, whichever is more helpful):",
        "",
    ]
    for i, e in enumerate(entries, start=1):
        header = (
            f"[DM {i}] "
            f"from @{e['sender_bot_id']}"
            + (f" in group {e['group_id']}" if e.get("group_id") else "")
        )
        lines.append(header)
        lines.append(f"  text: {e['text']}")
        lines.append(f"  task_id: {e['task_id']}")
        if e.get("group_id"):
            lines.append(
                f"  reply via a2a_send_group(group_id='{e['group_id']}', text='...')"
            )
        else:
            lines.append(
                f"  reply via a2a_reply(task_id='{e['task_id']}', text='...')"
            )
        lines.append("")
    return "\n".join(lines).rstrip()
