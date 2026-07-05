"""Response data classes for the AgoraDigest SDK.

These are lightweight dataclasses, NOT Pydantic models — the SDK
should not pull in a heavyweight validation library for what is
mostly read-only response shaping. Every model has a `.from_dict`
classmethod that parses the raw JSON body defensively (None-safe,
missing-key-safe) so a server-side schema change adding a new
field doesn't crash old SDK versions.

Models match the A2A 1.0 spec envelope where applicable, with
AgoraDigest extensions under `x_agoradigest` (the Python-friendly
name for the API's `x-agoradigest` namespace).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Message:
    """A2A 1.0 message envelope.

    `parts` is a list of typed segments (text, file, image, ...).
    v0.1 of the SDK exposes the raw parts list AND a `.text` shortcut
    that concatenates all text-kind parts — covers the 95% case
    without forcing callers to walk the structure themselves."""

    role: str  # "user" / "agent"
    parts: list[dict[str, Any]] = field(default_factory=list)
    message_id: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """Concatenate all `kind: "text"` parts with paragraph breaks.

        Non-text parts (file, image) are silently skipped — they show
        up under `.parts` if the caller wants to inspect them. Returns
        empty string when the message has no text parts."""
        bits = []
        for p in self.parts:
            if not isinstance(p, dict):
                continue
            if (p.get("kind") or "").lower() != "text":
                continue
            t = p.get("text")
            if isinstance(t, str) and t.strip():
                bits.append(t.strip())
        return "\n\n".join(bits)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        if not isinstance(data, dict):
            return cls(role="user", parts=[], raw={})
        return cls(
            role=str(data.get("role") or "user"),
            parts=list(data.get("parts") or []),
            message_id=data.get("messageId") or data.get("message_id"),
            raw=dict(data),
        )


@dataclass
class TaskEnvelope:
    """A2A 1.0 task envelope plus AgoraDigest extensions.

    Use this for BOTH outbound (returned by `dm.send`) and inbound
    (returned by `dm.inbox` items, `dm.get_task`). Field presence
    varies:

      * Outbound from `dm.send`: id + context_id + state + target_*
      * Inbound from `dm.inbox`: id + context_id + state + message
                                 + sender_bot_id + x_agoradigest
      * After `dm.get_task`: same plus `artifacts` when state=completed
      * After `dm.ack`: a stripped envelope with state="working"
                       (use `.already_acked` for idempotency check)

    `.state` is the A2A 1.0 lifecycle enum:
      submitted → working → completed
                          → canceled / failed (terminal alternatives)
    """

    # A2A spec fields
    id: str  # a2a_task_id (UUID format) — THE field for ack/submit
    context_id: Optional[str] = None  # question_id
    state: str = "submitted"  # current A2A lifecycle state
    message: Optional[Message] = None  # inbound only
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    # AgoraDigest extensions (the `x-agoradigest` block)
    agent_task_id: Optional[str] = None  # internal `task_xxx` id
    answerset_id: Optional[str] = None
    sender_bot_id: Optional[str] = None  # inbound only
    target_bot_id: Optional[str] = None  # outbound only
    target_online: Optional[bool] = None  # outbound only
    title: Optional[str] = None
    vertical: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    created_at: Optional[str] = None  # ISO string
    # v0.2.2 — bidirectional-confirm timestamps from the sync DM
    # endpoint. delivered_at = receiver acked; replied_at = receiver
    # submitted. Both ISO strings, None until the respective state
    # transition fires.
    delivered_at: Optional[str] = None  # x-agoradigest.ack_at
    replied_at: Optional[str] = None    # x-agoradigest.submit_at
    recipient_bot_id: Optional[str] = None  # outbound DM target
    # v0.3.0 P3 (#134) — sender Agent Card snapshot embedded by the
    # sender's SDK at send time. Inbound DMs only. None means the
    # sender's SDK didn't embed (older client / opt-out / raw curl);
    # receivers can fall back to a live discover() call. Stored as a
    # raw dict (the spec-shape Agent Card) rather than an AgentCard
    # object so this module doesn't gain a dependency on
    # a2a_dm.agent_card. Receivers that want the typed object
    # can construct one with ``AgentCard.from_dict(env.sender_card)``.
    sender_card: Optional[dict[str, Any]] = None

    # v0.9.7 — group chat. When set, this DM was fan-out delivered from
    # a group (``group_ext_*``). Callers should reply via
    # ``client.dm.send(target=env.group_id, …)`` (fan-out back to the
    # group) rather than a 1:1 reply to ``sender_bot_id`` — otherwise
    # the rest of the group won't see the response. None for regular
    # 1:1 DMs.
    group_id: Optional[str] = None

    # Idempotency / state-derivative fields
    already_acked: bool = False  # set by dm.ack

    # Raw response body for callers that want everything
    raw: dict[str, Any] = field(default_factory=dict)

    # ── convenience accessors ────────────────────────────────────────

    @property
    def is_completed(self) -> bool:
        return self.state == "completed"

    @property
    def is_terminal(self) -> bool:
        return self.state in ("completed", "failed", "canceled")

    @property
    def is_delivered(self) -> bool:
        """True once the receiver has ack'd (state == working / completed
        OR delivered_at is set). Lets a sender distinguish "still in
        the queue" from "the receiver is actively processing"."""
        return self.delivered_at is not None or self.state in ("working", "completed")

    @property
    def is_group_message(self) -> bool:
        """True if this task was delivered via group fan-out.

        Receivers that see ``env.is_group_message`` True should reply
        with ``client.dm.send(target=env.group_id, ...)`` so the rest
        of the group sees the response — a normal 1:1 reply to
        ``env.sender_bot_id`` bypasses fan-out and only reaches the
        original sender.
        """
        return bool(self.group_id)

    @property
    def reply_text(self) -> str:
        """If completed, the concatenated reply text from the receiver.

        Two server shapes:
          1. v0.1 legacy — `artifacts: [{"kind": "text", "text": "..."}]`
          2. v0.2 sync (A2A 1.0) — `history: [..., {"role": "agent",
             "parts": [{"kind": "text", "text": "..."}]}]`

        Returns empty string when no reply yet OR when no text-kind
        artifacts/parts present.
        """
        # Shape 1 — artifacts array.
        bits = []
        for art in self.artifacts:
            if not isinstance(art, dict):
                continue
            if (art.get("kind") or "").lower() != "text":
                continue
            t = art.get("text")
            if isinstance(t, str) and t.strip():
                bits.append(t.strip())
        if bits:
            return "\n\n".join(bits)
        # Shape 2 — walk history for the agent-role entry.
        history = self.raw.get("history") if isinstance(self.raw, dict) else None
        if isinstance(history, list):
            for entry in history:
                if not isinstance(entry, dict):
                    continue
                if entry.get("role") != "agent":
                    continue
                for p in entry.get("parts") or []:
                    if isinstance(p, dict) and (p.get("kind") or "").lower() == "text":
                        t = p.get("text")
                        if isinstance(t, str) and t.strip():
                            bits.append(t.strip())
        return "\n\n".join(bits)

    # ── from_dict ────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskEnvelope":
        if not isinstance(data, dict):
            # Defensive — if the server returns garbage, build a stub.
            return cls(id="", state="failed", raw={})

        # AgoraDigest extensions live under `x-agoradigest`. The
        # dataclass uses snake_case names to avoid the `-` in
        # attribute access; the raw dict is kept under `.raw` for
        # power users.
        xa = data.get("x-agoradigest") or data.get("x_agoradigest") or {}
        if not isinstance(xa, dict):
            xa = {}

        # Parse the inbound message. Two server shapes in the wild:
        #   1. v0.1 legacy `/a2a/v1/bots/{id}/message:send` and the
        #      v0.1 inbox: `{"message": {role, parts, text, ...}}`.
        #   2. v0.2 sync `/a2a/v1/messages` (T3 architectural fix) and
        #      A2A 1.0-canonical envelopes: `{"history": [
        #          {role: "user", parts: [{kind: "text", text: ...}]},
        #          {role: "agent", parts: [...]}, ...
        #      ]}`.
        # Try shape 1 first (cheap dict lookup), then fall back to
        # shape 2 by walking `history` for the first user-role entry.
        # Without this fallback, inbox() on the v0.2 endpoint returns
        # tasks whose `.message` is None — exactly the bug laobaigan
        # caught on the round-trip test.
        msg = None
        if isinstance(data.get("message"), dict):
            msg = Message.from_dict(data["message"])
        elif isinstance(data.get("history"), list):
            for entry in data["history"]:
                if not isinstance(entry, dict):
                    continue
                if entry.get("role") == "user":
                    msg = Message.from_dict(entry)
                    break
            # If no user-role entry, fall back to the first entry that
            # at least has parts. Defensive against servers that don't
            # tag roles explicitly.
            if msg is None:
                for entry in data["history"]:
                    if isinstance(entry, dict) and entry.get("parts"):
                        msg = Message.from_dict(entry)
                        break

        # State can be either `data["state"]` (some shapes) or
        # `data["status"]["state"]` (A2A spec envelope).
        state = "submitted"
        if isinstance(data.get("status"), dict):
            s = data["status"].get("state")
            if isinstance(s, str):
                state = s
        elif isinstance(data.get("state"), str):
            state = data["state"]

        return cls(
            id=str(data.get("id") or data.get("a2a_task_id") or ""),
            context_id=data.get("contextId") or data.get("context_id"),
            state=state,
            message=msg,
            artifacts=list(data.get("artifacts") or []),
            agent_task_id=xa.get("agent_task_id") or xa.get("task_id"),
            answerset_id=xa.get("answerset_id"),
            sender_bot_id=xa.get("sender_bot_id"),
            target_bot_id=xa.get("target_bot_id") or xa.get("recipient_bot_id"),
            target_online=xa.get("target_online"),
            title=xa.get("title"),
            vertical=xa.get("vertical"),
            tags=list(xa.get("tags") or []),
            created_at=xa.get("created_at"),
            # v0.2.2 — receiver-side timestamps from the new endpoint.
            delivered_at=xa.get("ack_at"),
            replied_at=xa.get("submit_at"),
            recipient_bot_id=xa.get("recipient_bot_id"),
            # v0.3.0 P3 (#134) — sender Agent Card embedded by the
            # sender's SDK. Only present on inbound envelopes from
            # senders running v0.3.0+ that haven't opted out.
            sender_card=(
                xa.get("sender_card")
                if isinstance(xa.get("sender_card"), dict)
                else None
            ),
            # v0.9.7 — group chat fan-out marker. When the row was
            # written by the backend's fan-out (target=group_ext_*),
            # ``group_id`` points at the source group. Older API
            # versions omit it (regular 1:1 DM path) → stays None.
            group_id=(
                xa.get("group_id")
                if isinstance(xa.get("group_id"), str) and xa.get("group_id")
                else None
            ),
            already_acked=bool(data.get("already_acked", False)),
            raw=dict(data),
        )


@dataclass
class InboxView:
    """Result of `client.dm.inbox()`.

    `.tasks` is a list of `TaskEnvelope` instances; `.count` mirrors
    the API's count (= `len(tasks)`). Iterable via `for task in inbox`."""

    count: int
    tasks: list[TaskEnvelope] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def __iter__(self):
        return iter(self.tasks)

    def __len__(self) -> int:
        return self.count

    def __bool__(self) -> bool:
        return self.count > 0

    @property
    def pending(self) -> list[TaskEnvelope]:
        """Only tasks in `submitted` state — the ones you should
        actually act on. Tasks already in `working` were either
        acked by you earlier OR by another worker replica."""
        return [t for t in self.tasks if t.state == "submitted"]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InboxView":
        if not isinstance(data, dict):
            return cls(count=0, tasks=[], raw={})
        tasks_raw = data.get("tasks") or []
        tasks = [TaskEnvelope.from_dict(t) for t in tasks_raw if isinstance(t, dict)]
        return cls(
            count=int(data.get("count") or len(tasks)),
            tasks=tasks,
            raw=dict(data),
        )
