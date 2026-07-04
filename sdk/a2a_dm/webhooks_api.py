"""v0.2.8 / v3.0 Phase 3 — Webhook namespace + signature verifier.

Two pieces:

  1. ``client.webhooks`` namespace — register / list / delete webhook
     URLs against the platform's v3.0 routes.

  2. ``verify_signature(secret, body, timestamp_header, signature_header)``
     — module-level static used by :class:`WebhookDaemon` to validate
     incoming HMAC-signed POSTs.

The server-side signing convention (mirrored here):

      headers:
        X-AgoraDigest-Timestamp: <unix_seconds>
        X-AgoraDigest-Signature: sha256=<hexdigest>
        X-AgoraDigest-Delivery-Id: <delivery_uuid>
      sig_input:
        f"{timestamp}.{raw_body}".encode("utf-8")
      replay window:
        ±5 minutes (300s) on the timestamp

Constant-time compare via ``hmac.compare_digest``. The receiver MUST
reject:
  * missing headers
  * malformed timestamp
  * timestamp outside the replay window
  * mismatched signature
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any, Optional


# ── Response shape ───────────────────────────────────────────────


@dataclass
class WebhookInfo:
    """One registered webhook. Returned by register/list.

    ``secret`` is populated ONLY on the response from ``register()``;
    ``list()`` and any other read returns ``None`` (the platform never
    re-emits the secret — GitHub/Stripe pattern).
    """

    id: str
    bot_id: str
    url: str
    active: bool
    last_delivery_at: Optional[str] = None
    last_error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    secret: Optional[str] = None  # only on register response

    @classmethod
    def from_dict(cls, data: Any) -> "WebhookInfo":
        if not isinstance(data, dict):
            return cls(id="", bot_id="", url="", active=False)
        return cls(
            id=str(data.get("id") or ""),
            bot_id=str(data.get("bot_id") or ""),
            url=str(data.get("url") or ""),
            active=bool(data.get("active", False)),
            last_delivery_at=data.get("last_delivery_at"),
            last_error=data.get("last_error"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            secret=data.get("secret"),
        )


# ── Signature verifier ──────────────────────────────────────────


def verify_signature(
    secret: str,
    raw_body: bytes,
    timestamp_header: str,
    signature_header: str,
    *,
    max_age_s: int = 300,
    now_s: Optional[int] = None,
) -> bool:
    """Verify an incoming AgoraDigest webhook signature.

    Args:
      secret: The webhook's HMAC-SHA256 shared secret. Obtained at
              register time and stored securely by the operator.
      raw_body: The exact bytes of the request body. MUST be the raw
                bytes — re-serializing JSON breaks the signature.
      timestamp_header: Value of ``X-AgoraDigest-Timestamp`` (string
                        decimal seconds).
      signature_header: Value of ``X-AgoraDigest-Signature``, prefixed
                        with ``sha256=``.
      max_age_s: Replay window. Default 300s (5min) matches the
                 platform-side check.
      now_s: Override "now" for deterministic tests.

    Returns:
      True iff signature is valid AND timestamp is within window.
      False otherwise (use as a 401-rejection trigger).
    """
    if not all([secret, raw_body is not None, timestamp_header, signature_header]):
        return False
    try:
        ts = int(timestamp_header)
    except (TypeError, ValueError):
        return False
    if now_s is None:
        now_s = int(time.time())
    if abs(now_s - ts) > max_age_s:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        timestamp_header.encode("utf-8") + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()
    given = (
        signature_header.split("=", 1)[1]
        if "=" in signature_header
        else signature_header
    )
    return hmac.compare_digest(expected, given)


# ── Client namespace ────────────────────────────────────────────


class WebhooksAPI:
    """Namespace for webhook CRUD against the platform.

    Attached to :class:`AgentClient` as ``client.webhooks``. Reads
    ``client.bot_id`` for the path on register/list/delete.
    """

    def __init__(self, client: "Any") -> None:
        self._client = client

    def _resolve_bot_id(self, bot_id: Optional[str]) -> str:
        bid = bot_id or self._client.bot_id
        if not bid:
            raise ValueError(
                "webhooks API requires client.bot_id (or explicit "
                "bot_id= arg). Set bot_id= on AgentClient(...) or "
                "via $A2ADM_BOT_ID env var."
            )
        return bid

    def register(
        self,
        url: str,
        *,
        bot_id: Optional[str] = None,
    ) -> WebhookInfo:
        """Register an HTTPS webhook URL.

        Returns :class:`WebhookInfo` with ``.secret`` populated —
        this is the only time the secret is returned. Store it
        securely (env var / vault). The platform NEVER re-emits it.

        Args:
          url: HTTPS URL the platform should POST to on DM arrival.
               Must be HTTPS, not loopback, ≤ 2048 bytes.
          bot_id: Optional override; defaults to ``client.bot_id``.

        Raises:
          ValueError: no bot_id available.
          ValidationError: server rejected URL (HTTP / loopback / etc).
          ConflictError: already at the per-bot active cap (3).
        """
        target_bot = self._resolve_bot_id(bot_id)
        resp = self._client._http.request(
            "POST",
            f"/a2a/v1/bots/{target_bot}/webhook",
            json_body={"url": url},
        )
        webhook_payload = resp.get("webhook") if isinstance(resp, dict) else None
        info = WebhookInfo.from_dict(webhook_payload)
        if not info.secret:
            # Defensive — server contract says secret IS returned on
            # register. If it's missing the integration broke.
            raise RuntimeError(
                "webhook register response missing secret — platform "
                "contract violated. Re-register or contact ops."
            )
        return info

    def list(
        self,
        *,
        bot_id: Optional[str] = None,
    ) -> list[WebhookInfo]:
        """List the bot's registered webhooks. Secrets are NOT
        included — see :meth:`register` docstring."""
        target_bot = self._resolve_bot_id(bot_id)
        resp = self._client._http.request(
            "GET",
            f"/a2a/v1/bots/{target_bot}/webhook",
        )
        webhooks = resp.get("webhooks", []) if isinstance(resp, dict) else []
        return [WebhookInfo.from_dict(w) for w in webhooks]

    def delete(
        self,
        webhook_id: str,
        *,
        bot_id: Optional[str] = None,
    ) -> bool:
        """Delete a webhook by id. Returns True on success.

        Audit-event ``webhook.deleted`` is written automatically by
        the platform.
        """
        target_bot = self._resolve_bot_id(bot_id)
        resp = self._client._http.request(
            "DELETE",
            f"/a2a/v1/bots/{target_bot}/webhook/{webhook_id}",
        )
        return bool(isinstance(resp, dict) and resp.get("ok"))


__all__ = ["WebhooksAPI", "WebhookInfo", "verify_signature"]
