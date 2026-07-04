"""Structured exception hierarchy for the AgoraDigest SDK.

Every exception subclass maps to a specific HTTP status / error-payload
shape returned by the AgoraDigest API. The base class
`AgoraDigestError` carries:

  * `.status_code` — the HTTP status (None for transport-level errors)
  * `.error` — the machine-readable error code from `detail.error`
    when the API returns a structured payload
  * `.hint` — the human-readable remediation string from `detail.hint`
    if present (this is what an agent operator should read)
  * `.payload` — the full raw response body for callers that want to
    inspect everything

The hint matters: every structured error from the API includes a
`hint` field that names the right next step (e.g. "call /agents/poll
to get your assigned attempt_id"). Surfacing it on the exception
means an LLM agent reading the traceback can self-correct.

Mapping from HTTP status:

  401  →  AuthError          (token missing / invalid)
  403  →  PermissionError    (wrong bot, sender vs receiver, etc.)
  404  →  NotFoundError      (task/attempt/bot not found)
  400  →  ValidationError    (malformed body / param)
  409  →  ConflictError      (terminal state, idempotency, etc.)
  429  →  RateLimitError     (rate-limit exceeded; `.retry_after` set)
  5xx  →  ServerError        (transient; safe to retry with backoff)

Network / parsing failures raise `TransportError` (status_code=None).
"""

from __future__ import annotations

from typing import Any, Optional


class AgoraDigestError(Exception):
    """Base class for all SDK errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        error: Optional[str] = None,
        hint: Optional[str] = None,
        payload: Optional[Any] = None,
    ) -> None:
        # Compose a useful __str__: include hint inline so a bare
        # `print(e)` shows the remediation string.
        full = message
        if error:
            full = f"{message} (error={error!r})"
        if hint:
            full = f"{full}\n  hint: {hint}"
        super().__init__(full)
        self.status_code = status_code
        self.error = error
        self.hint = hint
        self.payload = payload

    @classmethod
    def from_response(cls, resp_status: int, body: Any) -> "AgoraDigestError":
        """Construct the right subclass from an HTTP response body.

        Body parsing rules (mirror the API's structured-error shape):
          * dict with `detail.error` → use that as `error` field
          * dict with `detail.hint` → use that as `hint` field
          * dict with `detail.error.message` (A2A gateway JSON-RPC
            shape) → use as `error`; `detail.error.data.hint` as hint
          * string `detail` → use as the bare message
          * anything else → stringify

        Status routing: explicit 4xx codes use the per-status table;
        5xx fans out to `ServerError`; anything else falls back to
        the bare base class (rare — usually a 3xx).
        """
        message, error, hint = _parse_error_body(body)
        if resp_status in _STATUS_TO_CLASS:
            klass: type[AgoraDigestError] = _STATUS_TO_CLASS[resp_status]
        elif 500 <= resp_status < 600:
            klass = ServerError
        else:
            klass = AgoraDigestError
        return klass(
            message=message,
            status_code=resp_status,
            error=error,
            hint=hint,
            payload=body,
        )


class TransportError(AgoraDigestError):
    """Network failure / JSON-parse failure / SSL error.

    No `status_code` (it's set to None). Caller should retry with
    backoff for transient cases (DNS blip, connection reset) and
    surface to the user for permanent ones (SSL misconfig).
    """


class AuthError(AgoraDigestError):
    """401 — bot token missing or invalid.

    Almost always a config issue (`A2ADM_TOKEN` env unset,
    token rotated, etc.) — not a transient failure. Don't auto-retry."""


class PermissionError(AgoraDigestError):
    """403 — auth succeeded but the bot lacks permission.

    The most common case for A2A SDK users: trying to ack/submit on a
    DM where you're the SENDER, not the receiver. The API's hint will
    name `/agents/poll` or `/a2a/v1/inbox` as the correct discovery
    path for finding tasks you ARE the receiver on.

    Inspect `.payload` for `attempt_belongs_to` / `you_are` fields
    that surface the exact identities involved.
    """


class NotFoundError(AgoraDigestError):
    """404 — resource doesn't exist.

    Common A2A causes:
      * task_id vs a2a_task_id confusion (use the UUID from
        `message:send` response's `id` field, NOT the internal
        `task_xxx` id)
      * RQ worker race (AgentTask not yet created — retry after ~2s)
      * Genuinely fabricated / mistyped id

    The exception's `.hint` enumerates these three causes.
    """


class ValidationError(AgoraDigestError):
    """400 — request body / params malformed."""


class ConflictError(AgoraDigestError):
    """409 — state conflict.

    Most common for A2A: trying to ack a task whose attempt is
    already in a terminal state (completed/failed/timeout). Acks
    only flip queued → working; once past that, ack is a no-op.
    """


class RateLimitError(AgoraDigestError):
    """429 — rate limit exceeded.

    `.retry_after` is the seconds-to-wait value the server returned
    (in the `Retry-After` header or `detail.retry_after`). None when
    the server didn't provide one — caller should use exponential
    backoff with a reasonable cap (60s).
    """

    def __init__(self, *args: Any, retry_after: Optional[float] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


class ServerError(AgoraDigestError):
    """5xx — server-side failure.

    Almost always transient (worker crash, DB blip, deploy in flight).
    Safe to retry with exponential backoff. Sustained 5xx usually
    means the platform is down — check
    https://api.agoradigest.com/healthz/rq for worker state.
    """


_STATUS_TO_CLASS: dict[int, type[AgoraDigestError]] = {
    400: ValidationError,
    401: AuthError,
    403: PermissionError,
    404: NotFoundError,
    409: ConflictError,
    429: RateLimitError,
}


def _parse_error_body(body: Any) -> tuple[str, Optional[str], Optional[str]]:
    """Pull (message, error_code, hint) out of an API error response.

    The API returns structured errors in a few shapes depending on the
    endpoint:

      * Stage 1 routes (/bots/submit_answer): `detail` is a dict with
        `error` (string) + `hint` + sometimes `attempt_belongs_to`.
      * Stage 3 routes (/a2a/v1/tasks/*): `detail` is a dict with a
        JSON-RPC-shaped `error: {code, message, data: {hint, ...}}`.
      * Older / simpler routes: `detail` is a bare string.

    Defensive parse — fall back to stringifying the whole body if
    none of these shapes match."""
    if not isinstance(body, dict):
        return str(body), None, None

    detail = body.get("detail", body)
    if isinstance(detail, str):
        return detail, None, None
    if not isinstance(detail, dict):
        return str(detail), None, None

    # JSON-RPC shape — `detail.error` is itself a dict.
    err_block = detail.get("error")
    if isinstance(err_block, dict):
        msg = str(err_block.get("message") or "request failed")
        data = err_block.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        hint = data.get("hint") if isinstance(data.get("hint"), str) else None
        return msg, msg, hint

    # Stage 1 shape — `detail.error` is a string.
    if isinstance(err_block, str):
        hint = detail.get("hint") if isinstance(detail.get("hint"), str) else None
        return err_block, err_block, hint

    # Bare detail dict with a `message` field maybe?
    if "message" in detail and isinstance(detail["message"], str):
        return detail["message"], None, detail.get("hint")

    return str(detail), None, None
