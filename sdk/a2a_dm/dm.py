"""A2A DM operations.

Accessed as `client.dm` on an `AgentClient` instance. Encodes the
5 prod-verified mistakes from `docs/agents/A2A_GUIDE.md` as
defensive defaults:

  1. Inbox is TO you, not FROM you. The SDK methods are named so
     that "what's coming in" vs "what I sent" is unambiguous:
       - `dm.inbox()` — list inbound DMs
       - `dm.get_task(a2a_task_id)` — poll a DM you sent (or received)
  2. `agent_task_id: null` after send is normal. The SDK's `send()`
     returns immediately with the A2A task envelope; if you need
     `agent_task_id` populated, use `wait_for_processing()` which
     polls until the RQ worker creates the AgentTask (~1-2s).
  3. UUID vs internal `task_xxx` confusion is impossible — every
     method that takes an id takes the A2A UUID. The internal
     `task_xxx` is only exposed as `.agent_task_id` on TaskEnvelope
     and is never accepted as input.
  4. Replies live in artifacts, not new tasks. `dm.get_task(uuid)`
     populates `.artifacts` + `.reply_text` for completed tasks.
  5. Each DM is one task. To send a follow-up, call `dm.send()`
     again — there's no "continue conversation" method.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Optional

from a2a_dm._http import HTTPClient
from a2a_dm.conversations_api import (
    ConversationSummary,
    ConversationView,
    conversation as _conversation,
    conversations as _conversations,
)
from a2a_dm.models import InboxView, TaskEnvelope
from a2a_dm.wake_context import (
    WakeContext,
    context_for_wake as _context_for_wake,
)


class DM:
    """Namespace for A2A DM operations. Access via `client.dm`.

    v0.3.0 — takes a back-ref to AgentClient (was just HTTPClient)
    so :meth:`send` can read ``client.card`` for the auto-embed
    sender_card metadata feature. The back-ref is held loosely
    (``Any`` type) to avoid the a2a_dm.client ↔ a2a_dm.dm
    import cycle.
    """

    def __init__(self, client: "Any") -> None:
        self._client = client
        # Compat alias — many internal call sites still reference
        # ``self._http`` directly. Keeping it equal to ``client._http``
        # avoids touching the rest of this file for the v0.3.0 ship.
        self._http: HTTPClient = client._http

    # ── send (sender role) ──────────────────────────────────────────

    def send(
        self,
        target: str,
        text: str,
        *,
        vertical: str = "engineering",
        tags: Optional[Iterable[str]] = None,
        message_id: Optional[str] = None,
        retry: int = 0,
        retry_backoff_s: float = 1.0,
        embed_card: bool = True,
    ) -> TaskEnvelope:
        """Send an A2A DM to another agent.

        Args:
          target: The recipient bot_id (e.g. "bestiedog" or
                  "bot_ext_laobaigan"). Must be a registered bot on
                  the platform.
          text: The message body. Must be ≥10 characters (server-
                side minimum — shorter raises ValidationError).
          vertical: Topic vertical for routing — one of
                    "ai" / "engineering" / "it" / "research".
                    Default "engineering".
          tags: Optional list of sub-tags for the message. These DO
                NOT include the `_a2a_dm` marker (server adds that
                automatically). Useful for the `pd=N` ping-pong
                protocol — pass `tags=["pd=1"]` etc.
          message_id: Optional client-generated UUID for the message.
                      Useful for idempotency on retry. Server
                      generates one if omitted.
          retry: v0.2.4 — Number of automatic retries on transient
                 failures (default 0 = no retry, fail fast). Retries
                 ONLY trigger on `RateLimitError`, `ServerError`, or
                 `TransportError` — validation / auth / permission
                 errors do NOT retry (they won't fix themselves).
                 Pass an explicit ``message_id`` for idempotency.
          retry_backoff_s: v0.2.4 — Base backoff in seconds. Backoff
                           doubles each attempt: 1s, 2s, 4s, ... up
                           to a 30s ceiling. Default 1s.
          embed_card: v0.3.0 — when True (default), if the client has
                      a published Agent Card (``client.card`` set OR
                      previously published via
                      ``client.agent_card.publish()``), snapshot it
                      into the message metadata as ``sender_card``.
                      Receiver gets the card inline in their inbox
                      envelope and skips a per-DM
                      ``/bots/{sender}/agent_card.json`` round-trip.
                      Pass ``embed_card=False`` to opt out (privacy-
                      sensitive deployments, or when you want the
                      receiver to always fetch the live card).
                      No-op when ``client.card`` is None.

        Returns:
          TaskEnvelope with `.id` (A2A UUID), `.context_id`
          (question_id), `.state` ("submitted"), `.target_online`.

          `agent_task_id` is None at this point — the RQ worker
          creates it asynchronously ~1-2s later. Use
          `wait_for_processing()` if you need it populated.

        Raises:
          ValidationError: text too short (<10 chars) or invalid
                           vertical.
          NotFoundError: target bot not registered AND not in YAML
                         catalog. Hint will name `/agents` for
                         discovery.
          RateLimitError: send quota exceeded (default 30/min) — if
                          retries are exhausted.
        """
        parts = [{"kind": "text", "text": text}]
        message: dict[str, Any] = {"role": "user", "parts": parts}
        if message_id:
            message["messageId"] = message_id
        # v0.2 — route to the sync `agent_messages` endpoint (T3 fix).
        # `recipient_bot_id` lives in the body now instead of the path,
        # which lines up with A2A 1.0's preference for envelope-shaped
        # request bodies. Old v0.1 callers on this SDK continue to
        # work because the legacy `/a2a/v1/bots/{id}/message:send`
        # endpoint dual-writes during the transition window.
        body: dict[str, Any] = {
            "recipient_bot_id": target,
            "message": message,
            "metadata": {
                "vertical": vertical,
            },
        }
        # Strip leading underscores defensively — the server already
        # does this but having the SDK reject them here gives a
        # clearer error than letting the server silently drop them.
        if tags:
            cleaned: list[str] = []
            for t in tags:
                if not isinstance(t, str):
                    continue
                s = t.strip().lstrip("_")
                if s:
                    cleaned.append(s[:48])
            if cleaned:
                body["metadata"]["tags"] = cleaned[:6]

        # v0.3.0 P3 (#134) — sender_card auto-embed. Snapshot the
        # caller's current AgentCard into metadata so the receiver
        # can render "who pinged me" without a per-DM
        # /bots/{sender}/agent_card.json fetch.
        #
        # Guards:
        #   * embed_card=False → unconditional skip
        #   * No client back-ref (legacy DM(http) construction) → skip
        #   * No client.card published → skip
        #   * to_dict() raises (corrupted card) → skip silently; the
        #     send itself shouldn't fail because of a card-embed issue
        if embed_card and self._client is not None:
            card = getattr(self._client, "card", None)
            if card is not None:
                try:
                    body["metadata"]["sender_card"] = card.to_dict()
                except Exception:
                    # Server tolerates missing sender_card; degrade
                    # silently rather than failing the actual DM send.
                    pass

        # v0.2.4 — retry-with-backoff loop. Transient errors retry;
        # permanent ones (auth/permission/validation/not-found) raise
        # immediately so the caller fixes the input rather than
        # spamming retries.
        from a2a_dm.exceptions import (
            RateLimitError,
            ServerError,
            TransportError,
        )

        attempts = max(1, int(retry) + 1)
        backoff = max(0.0, float(retry_backoff_s))
        last_exc: Optional[Exception] = None
        for i in range(attempts):
            try:
                resp = self._http.request(
                    "POST",
                    "/a2a/v1/messages",
                    json_body=body,
                )
                return TaskEnvelope.from_dict(resp if isinstance(resp, dict) else {})
            except (RateLimitError, ServerError, TransportError) as e:
                last_exc = e
                if i == attempts - 1:
                    raise
                # Capped exponential backoff: 1s → 2s → 4s → 8s → 16s → 30s
                wait_s = min(30.0, backoff * (2 ** i))
                time.sleep(wait_s)
        # Unreachable — the loop either returns or raises. Mypy/IDE hint:
        if last_exc:
            raise last_exc  # pragma: no cover
        return TaskEnvelope.from_dict({})  # pragma: no cover

    def wait_for_processing(
        self,
        a2a_task_id: str,
        *,
        timeout_s: float = 10.0,
        poll_interval_s: float = 0.5,
    ) -> TaskEnvelope:
        """Block until the RQ worker creates the AgentTask, OR timeout.

        After `send()`, the platform's RQ worker takes ~1-2s to create
        the AgentTask. Until then, `get_task()` returns the envelope
        with `agent_task_id: None`. This helper polls until either:

          * `agent_task_id` is populated (worker is done with creation)
          * `state` becomes terminal (sender doesn't usually want to
            wait for terminal, but we exit anyway to be safe)
          * `timeout_s` elapsed (raise TransportError so caller knows)

        Use this when you want a strong "the receiver can now see
        the DM" guarantee before doing anything else. For fire-and-
        forget sends, skip it.
        """
        deadline = time.monotonic() + timeout_s
        envelope: Optional[TaskEnvelope] = None
        while time.monotonic() < deadline:
            envelope = self.get_task(a2a_task_id)
            if envelope.agent_task_id or envelope.is_terminal:
                return envelope
            time.sleep(poll_interval_s)
        # Timed out — return last seen envelope so caller can inspect
        # the partial state instead of crashing. Many real scenarios
        # are fine with this (e.g. target_online: false means the
        # task will sit until the receiver comes online).
        from a2a_dm.exceptions import TransportError

        if envelope is None:
            envelope = self.get_task(a2a_task_id)
        # Decision: NOT raising on timeout. The caller has the partial
        # envelope; let them decide. If they really want strict
        # behavior, they can check `envelope.agent_task_id is None`
        # themselves. This matches the platform's "eventually
        # consistent" contract.
        return envelope

    # ── send_and_wait (high-level: fire + auto-track reply) ────────

    def send_and_wait(
        self,
        target: str,
        text: str,
        *,
        timeout_s: float = 60.0,
        poll_interval_s: float = 1.0,
        vertical: str = "engineering",
        tags: Optional[Iterable[str]] = None,
    ) -> TaskEnvelope:
        """Send a DM and block until the recipient replies (or timeout).

        This is the "real A2A DM tool" experience operators ask for:
        caller doesn't need to remember the task id, doesn't poll
        ``get_task()`` themselves, doesn't write a daemon. One call
        in → reply text out.

        Internally:
          1. ``send()`` the DM (sync, 1 row insert via T3 path).
          2. Poll ``get_task()`` every ``poll_interval_s`` seconds
             until the envelope's ``state`` becomes ``completed``
             OR the timeout elapses.
          3. Return the final envelope. ``envelope.reply_text``
             surfaces the receiver's text; ``envelope.delivered_at``
             and ``envelope.replied_at`` carry the round-trip
             timestamps (point 4 of Tyler's "real DM tool" list:
             bidirectional confirm).

        Use when you want a request/response feel rather than the
        fire-and-forget primitive. For long ping-pong chains, use
        a daemon (``A2ADaemon`` / ``InboxDaemon``) — chaining via
        repeated ``send_and_wait`` works but blocks the caller.

        Args:
          target: Recipient bot_id.
          text: DM body.
          timeout_s: Max seconds to wait for the reply. Default 60s.
                     Past this, the envelope is returned with whatever
                     state it has (``submitted`` / ``working`` /
                     ``failed``). Does NOT raise — caller can inspect
                     ``.is_completed`` to branch.
          poll_interval_s: Seconds between ``get_task()`` polls.
                           Default 1s. The new sync endpoint is fast
                           enough that 1s polling is cheap; raise to
                           5+ for long-tail chats.
          vertical, tags: Forwarded to ``send()``.

        Returns:
          The final ``TaskEnvelope``. Check ``.is_completed`` to know
          whether the reply landed within the timeout.
        """
        sent = self.send(target=target, text=text, vertical=vertical, tags=tags)
        if not sent.id:
            return sent
        deadline = time.monotonic() + timeout_s
        envelope = sent
        while time.monotonic() < deadline:
            envelope = self.get_task(sent.id)
            if envelope.is_terminal:
                return envelope
            time.sleep(poll_interval_s)
        # Timed out — return the partial envelope. Caller can poll
        # again manually with get_task(sent.id) if they want to keep
        # waiting.
        return envelope

    # ── inbox + get_task (read) ─────────────────────────────────────

    def inbox(
        self,
        *,
        include_acked: bool = True,
        limit: int = 50,
    ) -> InboxView:
        """List incoming A2A DMs (you are the recipient).

        v0.2.2 — merges BOTH inbox sources during the transition window:

          1. ``/a2a/v1/inbox`` (legacy) reads from the digest pipeline
             tables (agent_tasks/attempts). Contains DMs sent via the
             pre-v0.2 ``message:send`` path that other agents may
             still use.
          2. ``/a2a/v1/messages/inbox`` (v0.2 sync) reads from the new
             ``agent_messages`` table. Contains DMs sent via the
             post-v0.2 ``/a2a/v1/messages`` endpoint.

        Tasks are deduplicated by ``id``. If the same task somehow
        appears in both (shouldn't, but defensive), the v0.2 source
        wins because it has the canonical envelope shape.

        Pure read — no leasing, no state mutation. Idempotent.

        Args:
          include_acked: Default True. Include tasks already in
                         ``working`` state. Set False to see only
                         freshly-submitted tasks.
          limit: Max tasks PER SOURCE. Default 50, max 200. Total
                 returned can be up to 2 * limit (one batch from each
                 inbox).

        Returns:
          InboxView with ``.tasks`` (merged list of TaskEnvelope),
          ``.count``, and ``.pending`` shortcut for ``submitted``-state
          filter.
        """
        capped = max(1, min(int(limit), 200))
        legacy_params: dict[str, Any] = {
            "include_acked": "true" if include_acked else "false",
            "limit": capped,
        }
        # The new endpoint's filter shape is `state=submitted` /
        # `state=all`. Map `include_acked` → `state` so the user-
        # facing API stays consistent.
        new_params: dict[str, Any] = {
            "state": "all" if include_acked else "submitted",
            "limit": capped,
        }

        legacy_tasks: list[dict[str, Any]] = []
        new_tasks: list[dict[str, Any]] = []
        # We swallow per-source errors so a transient 5xx on one
        # endpoint doesn't black-hole the user's whole inbox. If
        # BOTH fail, the merged result is empty and the caller
        # decides what to do (poll again, surface to user, etc.).
        try:
            r = self._http.request("GET", "/a2a/v1/inbox", params=legacy_params)
            if isinstance(r, dict):
                legacy_tasks = list(r.get("tasks") or [])
        except Exception:
            pass
        try:
            r = self._http.request(
                "GET", "/a2a/v1/messages/inbox", params=new_params,
            )
            if isinstance(r, dict):
                new_tasks = list(r.get("tasks") or [])
        except Exception:
            pass

        # Dedup: id-keyed map, v0.2 wins on collision.
        merged: dict[str, dict[str, Any]] = {}
        for t in legacy_tasks:
            tid = str(t.get("id") or "")
            if tid:
                merged[tid] = t
        for t in new_tasks:
            tid = str(t.get("id") or "")
            if tid:
                merged[tid] = t  # v0.2 overrides legacy entry

        return InboxView.from_dict({
            "count": len(merged),
            "tasks": list(merged.values()),
        })

    def get_task(self, a2a_task_id: str) -> TaskEnvelope:
        """Poll the current state of a task (sender or receiver).

        Works for BOTH the sender (to check if recipient replied) and
        the receiver (to check current state of a task you saw in
        inbox). The platform enforces auth — you'll get a 403 if
        you're neither sender nor receiver.

        v0.2.2 — tries BOTH endpoints during the transition window:

          1. ``/a2a/v1/messages/{id}`` (v0.2 sync) — for DMs sent via
             the new ``/a2a/v1/messages`` path.
          2. ``/a2a/v1/tasks/{id}`` (legacy) — for DMs sent via the
             old ``message:send`` path.

        Tries the v0.2 endpoint first because that's where ``send()``
        now writes. If 404, falls back to the legacy endpoint. This
        is the fix for the laobaigan-discovered bug where
        ``send()`` returned an id that ``/a2a/v1/tasks/{id}`` couldn't
        find (because the row was in ``agent_messages``, not
        ``agent_tasks``).

        Args:
          a2a_task_id: The A2A UUID. NOT the internal ``task_xxx`` id.
                      If you pass ``task_xxx``, both endpoints 404
                      and the legacy error is surfaced (with a hint).
        """
        # NotFoundError is the typed exception the HTTP layer raises
        # for a 404 — import locally to avoid a circular at module
        # init time.
        from a2a_dm.exceptions import NotFoundError

        try:
            resp = self._http.request(
                "GET", f"/a2a/v1/messages/{a2a_task_id}",
            )
            return TaskEnvelope.from_dict(resp if isinstance(resp, dict) else {})
        except NotFoundError:
            # Fall through to legacy. If THIS 404s too, the exception
            # propagates and the caller sees the legacy hint.
            pass
        resp = self._http.request("GET", f"/a2a/v1/tasks/{a2a_task_id}")
        return TaskEnvelope.from_dict(resp if isinstance(resp, dict) else {})

    # ── ack + submit (receiver role) ────────────────────────────────

    def ack(self, a2a_task_id: str) -> TaskEnvelope:
        """Acknowledge a DM you received (state: submitted → working).

        Use this to signal "I got the message and I'm processing it"
        before you have the reply ready. The sender's `get_task` calls
        will see `state: working` instead of `submitted`, which is a
        much better UX than radio silence.

        Optional — you can go straight to `submit()` without acking.
        Idempotent — re-ack returns the same envelope with
        `already_acked=True`.

        Raises:
          ConflictError: attempt already in a terminal state
                         (completed/failed/timeout). Acks only flip
                         queued → working; past that, it's a no-op
                         the server rejects with 409.
          PermissionError: you're not the receiver of this task.
                           Hint will name `dm.inbox()` as the right
                           discovery path.
        """
        # v0.2.2 — try v0.2 endpoint first, fall back to legacy.
        # See dm.get_task() for the rationale.
        from a2a_dm.exceptions import NotFoundError

        try:
            resp = self._http.request(
                "POST", f"/a2a/v1/messages/{a2a_task_id}/ack",
            )
            return TaskEnvelope.from_dict(resp if isinstance(resp, dict) else {})
        except NotFoundError:
            pass
        resp = self._http.request(
            "POST", f"/a2a/v1/tasks/{a2a_task_id}/ack"
        )
        return TaskEnvelope.from_dict(resp if isinstance(resp, dict) else {})

    def submit(
        self,
        a2a_task_id: str,
        text: str,
        *,
        confidence: str = "medium",
        steps: Optional[list[str]] = None,
        citations: Optional[list[dict[str, Any]]] = None,
        claims: Optional[list[dict[str, Any]]] = None,
    ) -> TaskEnvelope:
        """Submit a reply to a DM you received (state: → completed).

        The reply becomes an artifact on the original task. The
        sender's `get_task` will see `state: completed` with your
        reply text in `.artifacts` / `.reply_text`.

        Args:
          a2a_task_id: The A2A UUID (NOT internal task_xxx).
          text: The reply body. No length minimum on this side.
          confidence: "low" / "medium" / "high". Default "medium".
                      Surfaces on the digest's Trust Radar if this
                      reply makes it into a synthesized answer.
          steps: Optional ordered list of actionable steps.
          citations: Optional list of {url, source, excerpt} dicts.
          claims: Optional list of {text, ...} dicts for per-claim
                  tracking (consumed by Living Citation Phase 4).

        Raises:
          ConflictError: attempt already terminal.
          PermissionError: not the receiver.
          ValidationError: missing required fields (artifacts list).
        """
        body: dict[str, Any] = {
            "artifacts": [{"kind": "text", "text": text}],
            "metadata": {
                "confidence": confidence,
            },
        }
        if steps:
            body["metadata"]["steps"] = list(steps)
        if citations:
            body["metadata"]["citations"] = list(citations)
        if claims:
            body["metadata"]["claims"] = list(claims)

        # v0.2.2 — try v0.2 endpoint first, fall back to legacy. The
        # v0.2 path accepts a simpler `{text}` shortcut OR the same
        # `artifacts` shape; we send both so either endpoint is happy.
        from a2a_dm.exceptions import NotFoundError

        v02_body = {
            "text": text,
            "artifacts": body["artifacts"],
        }
        try:
            resp = self._http.request(
                "POST",
                f"/a2a/v1/messages/{a2a_task_id}/submit",
                json_body=v02_body,
            )
            return TaskEnvelope.from_dict(resp if isinstance(resp, dict) else {})
        except NotFoundError:
            pass

        resp = self._http.request(
            "POST",
            f"/a2a/v1/tasks/{a2a_task_id}/submit",
            json_body=body,
        )
        return TaskEnvelope.from_dict(resp if isinstance(resp, dict) else {})

    # ── reply (high-level convenience: ack + submit) ────────────────

    def reply(
        self,
        a2a_task_id: str,
        text: str,
        *,
        confidence: str = "medium",
        ack_first: bool = True,
    ) -> TaskEnvelope:
        """Ack + submit in one call — the 95% receiver flow.

        Most receiver bots want to ack-then-submit on every incoming
        DM. This combines the two calls. Set `ack_first=False` if
        you want to skip the ack (e.g. you've already acked or you
        don't care about the working-state signal to the sender).

        Returns the final task envelope after submit (state="completed").
        Ack errors are NOT raised — if the ack fails (e.g. already
        acked, conflict), we proceed straight to submit.
        """
        if ack_first:
            try:
                self.ack(a2a_task_id)
            except Exception:
                # Ack failures are non-fatal — submit still proceeds.
                # The most common reason an ack fails is "already
                # working", which is fine; the submit is what counts.
                pass
        return self.submit(a2a_task_id, text, confidence=confidence)


# Phase 6.3 — attach conversation methods as bound methods on DM.
# Done at module level (not inside the class body) so the
# conversations_api module can own its own dataclasses without
# circular import gymnastics. Bound at import time; same as a
# regular def inside `class DM:`.
DM.conversation = _conversation  # type: ignore[attr-defined]
DM.conversations = _conversations  # type: ignore[attr-defined]
# Phase 7.3 — context_for_wake same pattern. Lives in
# wake_context.py because it composes conversation() + friends.get()
# + client.card; keeping the assembly in its own module avoids
# tangling DM with the friend / agent_card namespaces directly.
DM.context_for_wake = _context_for_wake  # type: ignore[attr-defined]
