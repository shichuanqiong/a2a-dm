"""v3.0 Phase 4 — Multi-bot OrchestratedDaemon.

One process, N bots, shared lifecycle.

The single-bot pattern is::

    from a2a_dm import AgentClient
    from a2a_dm.daemon.advanced import A2ADaemon

    client = AgentClient(token=..., bot_id=...)
    daemon = A2ADaemon(token=..., bot_id=..., on_message=handler)
    daemon.start()

For operators running multiple personas (Tyler runs bestiedog +
laobaigan + baolongbro on the same machine), spawning three daemons
and wiring start/stop by hand is tedious and error-prone. The
``OrchestratedDaemon`` wraps that pattern:

    from a2a_dm.daemon.advanced import OrchestratedDaemon

    def reply_for_bestiedog(task, text, pd):
        return f"echo (bestiedog): {text[:80]}"

    def reply_for_laobaigan(task, text, pd):
        return f"echo (laobaigan): {text[:80]}"

    orch = OrchestratedDaemon([
        {"token": "bt_aaa", "bot_id": "bestiedog",  "on_message": reply_for_bestiedog},
        {"token": "bt_bbb", "bot_id": "laobaigan",  "on_message": reply_for_laobaigan},
        {"token": "bt_ccc", "bot_id": "baolongbro", "on_message": None},
    ])
    orch.start()        # spawns 3 A2ADaemons concurrently
    # ... runs ...
    orch.stop()         # graceful shutdown of all

Design choices
--------------
* **No new daemon class** — each bot still gets a full A2ADaemon
  underneath. OrchestratedDaemon is a *manager*, not a daemon. This
  keeps the per-bot lifecycle (SSE reconnect, retry backoff, dedup
  LRUSet) exactly the same as the single-bot path.
* **One bot per entry** — no shared SSE stream or merged inbox.
  Bots are independent identities; merging would conflate audit
  trails and make ``mark_processed`` semantics ambiguous.
* **Aggregate stats** — ``orch.stats`` is a dict keyed by ``bot_id``,
  each value an :class:`DaemonStats`. A simple summary aggregator
  (``orch.stats_summary()``) returns total counts.
* **Per-bot daemon class choice** — defaults to :class:`A2ADaemon`
  (3-layer prod). Override per-entry with ``daemon_class=...``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from a2a_dm.client import AgentClient
from a2a_dm.daemon._base import DaemonStats
from a2a_dm.daemon.advanced._a2a import A2ADaemon, A2AMessageHandler

logger = logging.getLogger(__name__)


class OrchestratedDaemon:
    """Manages N independent daemons under one lifecycle handle.

    Args:
        configs: List of bot configs. Each entry is a dict with
                 at minimum ``token`` and ``bot_id``. Optional keys
                 (passed through to the underlying daemon):
                 ``on_message``, ``partner``, ``sse``,
                 ``poll_interval``, ``heartbeat_interval``,
                 ``max_ping_pong``, ``api_base``, ``daemon_class``.
        on_error: Optional callback ``(bot_id, exception) -> None``
                  fired when a daemon's run loop dies. Default logs
                  + tries to restart that single daemon (the others
                  keep running).
        restart_on_crash: When True (default), a crashed daemon is
                          auto-restarted after a short backoff. Set
                          False for deterministic test runs.

    Concurrency:
        Each bot's daemon runs on its own thread (inherited from
        ``_BaseDaemon``). The orchestrator's own thread does
        crash-watch + (optional) restart. ``start()`` returns
        immediately — daemons run in the background.
    """

    def __init__(
        self,
        configs: list[dict[str, Any]],
        *,
        on_error: Optional[Callable[[str, BaseException], None]] = None,
        restart_on_crash: bool = True,
        restart_backoff_s: float = 5.0,
    ) -> None:
        if not configs:
            raise ValueError(
                "OrchestratedDaemon requires at least one bot config. "
                "Use A2ADaemon directly for the single-bot case."
            )
        # Validate up front — easier to debug a bad config at
        # construction time than at thread-spawn time.
        seen_bot_ids: set[str] = set()
        for cfg in configs:
            if not isinstance(cfg, dict):
                raise TypeError(f"config must be dict, got {type(cfg).__name__}")
            if not cfg.get("token"):
                raise ValueError(f"config missing 'token': {cfg.get('bot_id', '?')}")
            if not cfg.get("bot_id"):
                raise ValueError(f"config missing 'bot_id': {cfg!r}")
            if cfg["bot_id"] in seen_bot_ids:
                raise ValueError(
                    f"duplicate bot_id in configs: {cfg['bot_id']!r}"
                )
            seen_bot_ids.add(cfg["bot_id"])

        self._configs: list[dict[str, Any]] = list(configs)
        self._on_error = on_error
        self._restart_on_crash = restart_on_crash
        self._restart_backoff_s = restart_backoff_s

        # Active daemons: bot_id → daemon instance.
        self._daemons: dict[str, A2ADaemon] = {}
        # Per-bot last restart times (rate-limit crash loops).
        self._last_restart: dict[str, float] = {}

        # Watcher thread + lifecycle.
        self._watcher_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ── lifecycle ────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return any(d.running for d in self._daemons.values())

    @property
    def bot_ids(self) -> list[str]:
        return [c["bot_id"] for c in self._configs]

    def start(self) -> None:
        """Spawn one daemon per config and the crash watcher."""
        if self.running:
            return
        self._stop_event.clear()
        for cfg in self._configs:
            self._spawn(cfg)
        if self._restart_on_crash:
            self._watcher_thread = threading.Thread(
                target=self._watch_loop,
                name="orchestrated-watcher",
                daemon=True,
            )
            self._watcher_thread.start()
        logger.info(
            "OrchestratedDaemon: started %d bots: %s",
            len(self._daemons), sorted(self._daemons.keys()),
        )

    def stop(self, timeout_s: float = 10.0) -> None:
        """Stop all daemons + the watcher. Idempotent."""
        if not self._daemons and self._watcher_thread is None:
            return
        logger.info("OrchestratedDaemon: stopping %d bots", len(self._daemons))
        self._stop_event.set()
        for bot_id, daemon in list(self._daemons.items()):
            try:
                daemon.stop(timeout_s=timeout_s)
            except Exception:
                logger.exception(
                    "OrchestratedDaemon: error stopping %s", bot_id,
                )
        self._daemons.clear()
        if self._watcher_thread is not None:
            self._watcher_thread.join(timeout=timeout_s)
            self._watcher_thread = None

    def __enter__(self) -> "OrchestratedDaemon":
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()

    # ── stats ─────────────────────────────────────────────────────

    @property
    def stats(self) -> dict[str, DaemonStats]:
        """Per-bot stats. Live reference — counters update in place."""
        return {bid: d.stats for bid, d in self._daemons.items()}

    def stats_summary(self) -> dict[str, Any]:
        """Aggregate counters across all bots — useful for ``/healthz``
        endpoints that want one number per metric."""
        totals = {
            "bots": len(self._daemons),
            "running_bots": sum(1 for d in self._daemons.values() if d.running),
            "messages_processed": 0,
            "poll_count": 0,
            "errors": 0,
        }
        for d in self._daemons.values():
            totals["messages_processed"] += d.stats.messages_processed
            totals["poll_count"] += d.stats.poll_count
            totals["errors"] += d.stats.errors
        return totals

    # ── internal ─────────────────────────────────────────────────

    def _spawn(self, cfg: dict[str, Any]) -> None:
        """Construct + start one daemon from a config dict."""
        bot_id = cfg["bot_id"]
        daemon_class = cfg.get("daemon_class", A2ADaemon)
        # Build kwargs by filtering known parameters. Unknown keys
        # are silently dropped so the config schema can evolve.
        kwargs: dict[str, Any] = {
            "token": cfg["token"],
            "bot_id": bot_id,
        }
        for k in (
            "partner", "sse", "poll_interval", "heartbeat_interval",
            "max_ping_pong", "on_message", "api_base", "dedup_size",
        ):
            if k in cfg:
                kwargs[k] = cfg[k]
        try:
            d = daemon_class(**kwargs)
            d.start()
            self._daemons[bot_id] = d
        except Exception as e:
            logger.exception(
                "OrchestratedDaemon: failed to spawn %s", bot_id,
            )
            if self._on_error:
                try:
                    self._on_error(bot_id, e)
                except Exception:
                    logger.exception(
                        "OrchestratedDaemon: on_error callback raised",
                    )

    def _watch_loop(self) -> None:
        """Crash-detection loop. Runs every 5s.

        For each daemon, if ``running`` is False AND we haven't
        restarted in ``restart_backoff_s`` seconds, respawn it. The
        backoff prevents a permanently-failing daemon from chewing
        CPU on tight restart loops.
        """
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=5.0)
            if self._stop_event.is_set():
                break
            now = time.time()
            for cfg in self._configs:
                bot_id = cfg["bot_id"]
                daemon = self._daemons.get(bot_id)
                if daemon is None or daemon.running:
                    continue
                # Daemon died. Rate-limit restarts.
                last = self._last_restart.get(bot_id, 0)
                if now - last < self._restart_backoff_s:
                    continue
                logger.warning(
                    "OrchestratedDaemon: %s crashed; restarting", bot_id,
                )
                self._last_restart[bot_id] = now
                # Drop the dead daemon ref so _spawn replaces cleanly.
                self._daemons.pop(bot_id, None)
                self._spawn(cfg)
                if self._on_error:
                    try:
                        self._on_error(bot_id, RuntimeError("daemon crashed; restarted"))
                    except Exception:
                        pass


__all__ = ["OrchestratedDaemon"]
