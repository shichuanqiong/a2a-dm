"""Shared helpers for talking to the local Hermes gateway.

Both :mod:`delivery` (operator notifications) and :mod:`autowake`
(agent-turn injection) need the same three things:

  * where the gateway's webhook adapter listens (host/port from
    ``~/.hermes/config.yaml``, defaulting to 127.0.0.1:8644),
  * a per-install HMAC secret for our dynamic routes,
  * route registration in ``~/.hermes/webhook_subscriptions.json``
    (Hermes hot-reloads that file on every POST, mtime-gated).

Everything here is best-effort and side-effect-light: failures return
None / False and the callers fall back to their next tier.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets as _secrets
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 8644
_SUBSCRIPTIONS_FILE = "webhook_subscriptions.json"
_SECRET_FILE = "a2a-dm-webhook.secret"
_HTTP_TIMEOUT_S = 5.0

# Route names we own inside the gateway's webhook adapter.
WAKE_ROUTE = "a2a-dm-wake"
NOTIFY_ROUTE = "a2a-dm-notify"


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def load_gateway_config() -> dict:
    """Parse ``~/.hermes/config.yaml``; empty dict on any failure."""
    cfg_path = hermes_home() / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml  # PyYAML is already a plugin dependency

        return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        logger.debug("a2a-dm: could not parse config.yaml", exc_info=True)
        return {}


def webhook_base_url() -> str:
    """Base URL of the gateway webhook adapter (no trailing slash).

    ``A2A_WAKE_WEBHOOK_URL`` overrides everything (useful when the
    gateway runs in a container). Otherwise read the port from
    config.yaml, default 8644. We always target 127.0.0.1 — the POST
    originates on the same machine as the gateway.
    """
    override = os.environ.get("A2A_WAKE_WEBHOOK_URL")
    if override:
        return override.rstrip("/")
    cfg = load_gateway_config()
    port = _DEFAULT_PORT
    try:
        port = int(
            (((cfg.get("platforms") or {}).get("webhook") or {}).get("port"))
            or _DEFAULT_PORT
        )
    except (TypeError, ValueError):
        pass
    return f"http://127.0.0.1:{port}"


def webhook_platform_enabled() -> bool:
    """True if config.yaml has a ``platforms.webhook`` section that is
    not explicitly disabled. (If the adapter isn't running our POSTs
    fail anyway; this just avoids noisy retries.)"""
    cfg = load_gateway_config()
    section = (cfg.get("platforms") or {}).get("webhook")
    if section is None:
        return False
    if isinstance(section, dict) and section.get("enabled") is False:
        return False
    return True


def get_or_create_secret() -> str:
    """Per-install HMAC secret, persisted with 0600 perms."""
    path = hermes_home() / _SECRET_FILE
    try:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = _secrets.token_hex(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return value
    except OSError:
        # Unwritable home — fall back to an ephemeral secret. Routes
        # written by a previous run won't match, so callers will see
        # 401s and drop to their next tier. Logged once here.
        logger.warning(
            "a2a-dm: cannot persist webhook secret under %s; "
            "using ephemeral secret", path,
        )
        return _secrets.token_hex(32)


def _wake_home_target() -> tuple[str, dict]:
    """Resolve where agent-turn responses / notifications should land.

    ``A2A_WAKE_HOME`` accepts ``platform`` or ``platform:chat_id``
    (e.g. ``telegram:-100123``). Default: ``telegram`` with no chat_id,
    which makes the gateway use its configured home channel.
    """
    raw = (os.environ.get("A2A_WAKE_HOME") or "telegram").strip()
    if ":" in raw:
        platform, chat_id = raw.split(":", 1)
        return platform, {"chat_id": chat_id}
    return raw, {}


def desired_routes(bot_id: str) -> dict:
    """The two dynamic routes we want registered in the gateway."""
    secret = get_or_create_secret()
    deliver, deliver_extra = _wake_home_target()

    wake_prompt = (
        "[a2a-dm wake] Another agent sent you a message while you were "
        "idle. You are @" + bot_id + " on the a2a-dm network.\n\n"
        "From: @{sender_bot_id}\n"
        "Group: {group_id}\n"
        "Text: {text}\n"
        "Task: {task_id}\n\n"
        "Handle it now. If Group is empty this is a 1:1 DM — reply via "
        "a2a_reply(task_id='{task_id}', text=...). If Group is set, reply "
        "into the group via a2a_send_group(group_id='{group_id}', "
        "text=...). Then check a2a_get_inbox once for anything else "
        "pending. Keep replies useful and concise."
    )

    routes: dict[str, dict] = {
        WAKE_ROUTE: {
            "secret": secret,
            "prompt": wake_prompt,
            "skills": ["a2a-dm"],
            "deliver": deliver,
            "deliver_extra": deliver_extra,
        },
        NOTIFY_ROUTE: {
            "secret": secret,
            "prompt": "{text}",
            "deliver_only": True,
            "deliver": deliver,
            "deliver_extra": deliver_extra,
        },
    }
    return routes


def ensure_routes(bot_id: str) -> bool:
    """Merge our routes into ``webhook_subscriptions.json``.

    Returns True if the file now contains up-to-date routes. Existing
    unrelated routes are preserved; our routes are overwritten in
    place so prompt/deliver changes ship with plugin upgrades.
    """
    path = hermes_home() / _SUBSCRIPTIONS_FILE
    try:
        existing: dict[str, Any] = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except json.JSONDecodeError:
                logger.warning(
                    "a2a-dm: %s is not valid JSON; leaving it untouched",
                    path,
                )
                return False
        desired = desired_routes(bot_id)
        if all(existing.get(k) == v for k, v in desired.items()):
            return True
        existing.update(desired)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
        logger.info("a2a-dm: registered gateway webhook routes %s + %s",
                    WAKE_ROUTE, NOTIFY_ROUTE)
        return True
    except OSError:
        logger.warning("a2a-dm: cannot write %s", path, exc_info=True)
        return False


def post_route(
    route: str,
    payload: dict,
    *,
    request_id: Optional[str] = None,
) -> bool:
    """Signed POST to a gateway webhook route. True on 2xx.

    ``request_id`` becomes ``X-Request-ID`` which the gateway uses as
    its idempotency key — pass the a2a task_id so SSE + polling can't
    double-trigger a turn.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    signature = hmac.new(
        get_or_create_secret().encode(), body, hashlib.sha256
    ).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": signature,
    }
    if request_id:
        headers["X-Request-ID"] = request_id
    url = f"{webhook_base_url()}/webhooks/{route}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            ok = 200 <= resp.status < 300
            if not ok:
                logger.warning(
                    "a2a-dm: gateway route %s returned %s", route, resp.status
                )
            return ok
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.debug("a2a-dm: POST %s failed (%s)", url, exc)
        return False
    except Exception:  # noqa: BLE001
        logger.exception("a2a-dm: POST %s crashed", url)
        return False
