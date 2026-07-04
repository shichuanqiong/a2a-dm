"""Bounded LRU dedup set for daemon receivers.

Why this exists:
    The naive `_seen_ids: set[str]` pattern that several v0.2 daemon
    drafts shipped grew unbounded for the lifetime of a long-running
    daemon. After a week of activity that's hundreds of MB.

    The "clear when over N" pattern (`if len(d) > 10000: d.clear()`)
    is worse: the first task after the threshold becomes eligible for
    re-dispatch alongside the genuinely new arrivals.

What this is:
    A bounded set with insertion-order eviction. When the configured
    capacity is reached, the oldest entry is evicted in lock-step with
    the new entry so the membership check (`task_id in dedup`) stays
    O(1) AND the size never exceeds the cap by more than 1.

Why a deque+set pair rather than ``functools.lru_cache``:
    ``lru_cache`` keys on function arguments and recomputes; we need
    a pure presence test on opaque task IDs.

Thread-safety:
    NOT thread-safe. Callers that share an instance across threads
    (e.g. A2ADaemon's 3-layer fan-out) must wrap operations in their
    own lock. The daemon-internal usages either run on a single thread
    or already serialise via a ``_processed_lock``.
"""

from __future__ import annotations

from collections import deque
from typing import Iterator


class LRUSet:
    """Insertion-order-bounded set of opaque IDs.

    Args:
        max_size: Maximum entries retained. When ``add`` would exceed
            this, the oldest entry is removed first. Must be ≥ 1.
    """

    __slots__ = ("_max", "_set", "_deque")

    def __init__(self, max_size: int = 10_000) -> None:
        if max_size < 1:
            raise ValueError("max_size must be ≥ 1")
        self._max: int = int(max_size)
        self._set: set[str] = set()
        self._deque: deque[str] = deque(maxlen=max_size)

    def __contains__(self, item: str) -> bool:
        return item in self._set

    def __len__(self) -> int:
        return len(self._set)

    def __iter__(self) -> Iterator[str]:
        return iter(self._deque)

    def add(self, item: str) -> bool:
        """Insert *item*. Returns True if newly added, False if dup.

        On overflow, the oldest item is evicted from both the deque
        (automatic via maxlen) AND the set (manual sync). This keeps
        the two structures consistent: `item in self._set` ↔ `item in
        self._deque` for all observable moments.
        """
        if item in self._set:
            return False
        if len(self._deque) == self._max:
            # Deque is full → next append will silently pop the leftmost
            # item. We must mirror that eviction in the set BEFORE the
            # append so the set count stays bounded.
            evicted = self._deque[0]
            self._set.discard(evicted)
        self._deque.append(item)
        self._set.add(item)
        return True

    def clear(self) -> None:
        """Drop all entries. Mainly for tests + daemon stop()."""
        self._set.clear()
        self._deque.clear()


__all__ = ["LRUSet"]
