"""Ping-pong protocol helpers for A2A DM multi-round chains.

The convention:
    A sender wants to chain N rounds of DMs. Each message in the chain
    carries a ``pd=N`` tag where *N* is the current depth. A receiver
    that sees ``pd=k`` replies with ``pd=k+1`` until ``k`` reaches the
    configured ``max_rounds`` ceiling.

Why the ceiling matters:
    Two daemons replying to each other with no terminator condition
    will ping-pong forever. The ``pd`` counter is the terminator —
    when either side sees ``pd >= max_rounds``, it stops sending the
    next round but still ACKs the inbound message.

Usage inside an A2ADaemon handler::

    from a2a_dm.daemon.advanced import extract_pd, should_continue

    def my_handler(task, text, pd):
        if pd >= 0 and should_continue(pd, max_rounds=5):
            return f"Round {pd + 1} reply"
        return "thanks, stopping"
"""

from __future__ import annotations

from typing import Optional

from a2a_dm.models import TaskEnvelope


def extract_pd(task: TaskEnvelope) -> int:
    """Extract the ping-pong depth from a task's tags or metadata.

    Returns:
        The ``pd`` value as int, or ``-1`` if not found (i.e. this DM
        is NOT part of a ping-pong chain).
    """
    # AgoraDigest carries inbound metadata under ``x-agoradigest`` in
    # the A2A 1.0 task envelope; the SDK lifts the platform-specific
    # bits onto convenience attrs but keeps the raw dict for forward
    # compat with v0.x tag conventions.
    xag = getattr(task, "x_agoradigest", None) or {}
    if isinstance(xag, dict):
        tags = xag.get("tags", [])
        if isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, str) and tag.startswith("pd="):
                    try:
                        return int(tag.split("=", 1)[1])
                    except ValueError:
                        # malformed pd tag — treat as absent
                        pass

    # Fallback: some senders put pd into the top-level metadata block
    # rather than the tags array.
    meta = getattr(task, "metadata", None) or {}
    if isinstance(meta, dict):
        pd = meta.get("pd")
        if pd is not None:
            try:
                return int(pd)
            except (ValueError, TypeError):
                pass

    return -1


def should_continue(pd: int, max_rounds: int = 5) -> bool:
    """Whether a ping-pong chain at depth *pd* should send the next round.

    Args:
        pd: Current depth (from :func:`extract_pd`).
        max_rounds: Maximum allowed rounds (default 5).

    Returns:
        True if the chain should continue (``0 <= pd < max_rounds``).
    """
    return 0 <= pd < max_rounds


def next_round(pd: int, max_rounds: int = 5) -> Optional[int]:
    """Get the next ping-pong round number, or None if the chain ends.

    Args:
        pd: Current depth.
        max_rounds: Maximum allowed rounds.

    Returns:
        ``pd + 1`` if the chain should continue, else ``None``.
    """
    if not should_continue(pd, max_rounds):
        return None
    return pd + 1


__all__ = ["extract_pd", "should_continue", "next_round"]
