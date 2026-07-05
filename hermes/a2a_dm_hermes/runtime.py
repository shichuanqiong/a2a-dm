"""Background runtime — SSEDaemon + wake queue.

This is the piece that makes a2a-dm feel *real-time* inside Hermes.
The plugin starts an :class:`SSEDaemon` at plugin-load time. It
opens a persistent SSE connection to ``/agents/stream`` and enqueues
every inbound DM into a thread-safe wake queue. When Hermes fires
``pre_llm_call`` (once per agent turn), the plugin drains the queue
and injects the pending DMs as context — so the LLM sees them
alongside the user's message and knows to respond via the typed
tools (``a2a_reply`` / ``a2a_send_group``).

Design notes:

* **Leader-lock singleton.** Uses ``fcntl.flock`` on
  ``~/.hermes/a2a-dm-ws.lock`` so multiple Hermes processes on the
  same machine don't all open their own SSE — only the leader does,
  followers stay quiet. Same pattern AgentChat's plugin uses.
* **Bounded queue.** 200-DM cap; if the LLM turn is slow and the
  network is fast, older DMs are dropped from the injection buffer
  (they still show up in ``a2a_get_inbox``, they're not lost). This
  keeps prompt-token cost bounded.
* **Best-effort.** SSE outages don't wedge the agent — the plugin
  logs and continues. The DMs are still safely in the AgoraDigest
  inbox; the ``a2a_get_inbox`` tool still works.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from pathlib import Path
from typing import Any, Deque, Optional

from a2a_dm import AgentClient
from a2a_dm.daemon import SSEDaemon

logger = logging.getLogger(__name__)


_WAKE_QUEUE_MAX = 200


class WakeRuntime:
    """One-per-process singleton that owns the SSE connection + queue."""

    _instance: Optional["WakeRuntime"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._queue: Deque[dict] = deque(maxlen=_WAKE_QUEUE_MAX)
        self._queue_lock = threading.Lock()
        self._daemon: Optional[SSEDaemon] = None
        self._leader_fd = None  # kept alive to hold the flock

    # ── Singleton accessor ────────────────────────────────────────

    @classmethod
    def get(cls) -> "WakeRuntime":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ── Leader-lock so N Hermes processes don't all open SSE ──────

    def _acquire_leader(self) -> bool:
        """Return True if this process becomes the SSE leader.

        Uses fcntl.flock on ``~/.hermes/a2a-dm-ws.lock``. If another
        process already holds the lock we quietly become a follower —
        the plugin's tools still work through HTTP, we just don't
        open a duplicate SSE stream.
        """
        try:
            import fcntl  # POSIX only
        except ImportError:
            # Windows — no flock. Just always run; users rarely have
            # multiple Hermes processes on Windows anyway.
            return True

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
        """Bring up the SSE daemon. No-op if we're a follower or if
        env credentials are missing."""
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

        client = AgentClient(token=token, bot_id=bot_id)
        self._daemon = SSEDaemon(
            client,
            bot_id=bot_id,
            handler=self._on_wake,
            fallback_interval_s=30.0,
            auto_ack=False,  # let the agent's a2a_reply / a2a_send_group
                             # decide when to ack; auto-ack would race
                             # with the next agent turn.
        )
        self._daemon.start()
        logger.info(
            "a2a-dm: SSE wake runtime up (bot=%s, leader=%s)",
            bot_id, self._leader_fd is not None,
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

    # ── SSE callback: enqueue for pre_llm_call injection ──────────

    def _on_wake(self, task, daemon) -> None:  # noqa: ARG002
        """SSEDaemon handler — fired on every inbound DM.

        We do NOT auto-reply here. The point is to wake the *agent*,
        not to answer on its behalf. The queue drains on the next
        ``pre_llm_call`` turn.
        """
        entry = {
            "task_id":          task.id,
            "sender_bot_id":    task.sender_bot_id,
            "text":             (task.message.text if task.message else "")[:2000],
            "is_group_message": task.is_group_message,
            "group_id":         task.group_id,
            "created_at":       task.created_at,
        }
        with self._queue_lock:
            self._queue.append(entry)
        logger.debug(
            "a2a-dm: enqueued wake from %s (task=%s, group=%s)",
            task.sender_bot_id, task.id[:12], task.group_id or "-",
        )

    # ── pre_llm_call drain ────────────────────────────────────────

    def drain(self) -> list[dict]:
        """Return + clear all pending wake entries. Called on every
        agent turn."""
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
