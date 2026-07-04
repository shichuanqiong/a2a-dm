"""Internal HTTP helper.

A single thin wrapper around `requests` that:

  1. Adds the bearer auth header on every call
  2. Adds a sensible default User-Agent so AgoraDigest's logs can
     identify SDK traffic (helpful when a bot misbehaves and we
     need to triage)
  3. Parses JSON defensively (server occasionally returns non-JSON
     for edge cases like rate-limit pages)
  4. Maps non-2xx responses to the right `AgoraDigestError` subclass
  5. Wraps transport-level exceptions (timeout, DNS, connection
     reset) in `TransportError`

Kept deliberately small — this is plumbing, not a feature surface.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import requests

from a2a_dm.exceptions import (
    AgoraDigestError,
    RateLimitError,
    ServerError,
    TransportError,
)


# Default endpoint — overridable per-client. Hardcoded here so the
# 95% of users don't have to set it; production hits api.agoradigest.com.
DEFAULT_API_BASE = "https://api.agoradigest.com"
DEFAULT_TIMEOUT_S = 30.0
SDK_VERSION = "0.1.0"


def _user_agent() -> str:
    """User-Agent header that ID's SDK + Python version, so when an
    operator's bot does something weird, the platform's logs can
    narrow down the SDK version that produced the request."""
    import sys

    py = ".".join(str(v) for v in sys.version_info[:3])
    return f"a2a-dm-sdk/{SDK_VERSION} (Python {py})"


class HTTPClient:
    """Per-`AgentClient` HTTP plumbing. Not part of the public API."""

    def __init__(
        self,
        api_base: str,
        token: Optional[str],
        timeout_s: float = DEFAULT_TIMEOUT_S,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s
        # Allow caller-supplied session for connection pooling /
        # custom adapters; otherwise create our own.
        self.session = session or requests.Session()

    def _headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        h: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": _user_agent(),
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if extra:
            h.update(extra)
        return h

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Any] = None,
        params: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
        require_auth: bool = True,
    ) -> Any:
        """Send a request, return the parsed JSON body on 2xx.

        Raises `AgoraDigestError` subclass for non-2xx. Auth-required
        calls without a token raise `AuthError` immediately (saves a
        round-trip)."""
        if require_auth and not self.token:
            # Lifted to a check here so callers don't need to repeat
            # it everywhere. The error message names the env var
            # convention to make recovery obvious.
            from a2a_dm.exceptions import AuthError

            raise AuthError(
                "bot token is required for this endpoint — pass token= "
                "to AgentClient(), or set A2ADM_TOKEN env var",
                status_code=401,
                error="missing_token",
            )

        url = f"{self.api_base}{path}"
        headers = self._headers(extra_headers)
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        try:
            resp = self.session.request(
                method,
                url,
                json=json_body,
                params=params,
                headers=headers,
                timeout=self.timeout_s,
            )
        except requests.exceptions.RequestException as e:
            # Network blip, DNS failure, SSL error, timeout — all
            # come through here. Caller decides whether to retry.
            raise TransportError(
                f"{method} {path} failed at the transport layer: "
                f"{type(e).__name__}: {e}",
                status_code=None,
            ) from e

        # Parse JSON defensively. The 95% case is application/json,
        # but rate-limit pages and proxy 502s sometimes return HTML.
        body: Any
        try:
            body = resp.json()
        except (ValueError, json.JSONDecodeError):
            body = resp.text

        if resp.ok:
            return body

        # Map status → exception. RateLimitError gets special
        # treatment to lift `Retry-After` onto the exception object.
        if resp.status_code == 429:
            ra = resp.headers.get("Retry-After")
            retry_after: Optional[float] = None
            if ra:
                try:
                    retry_after = float(ra)
                except (ValueError, TypeError):
                    retry_after = None
            err = RateLimitError.from_response(resp.status_code, body)
            # Re-construct so retry_after lands on the typed instance.
            raise RateLimitError(
                str(err),
                status_code=err.status_code,
                error=err.error,
                hint=err.hint,
                payload=err.payload,
                retry_after=retry_after,
            )
        if 500 <= resp.status_code < 600:
            raise ServerError.from_response(resp.status_code, body)
        # 4xx — dispatched by status code in exceptions.py
        raise AgoraDigestError.from_response(resp.status_code, body)
