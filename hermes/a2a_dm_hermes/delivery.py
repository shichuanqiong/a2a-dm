"""Operator notifications — 3-tier delivery ladder (v0.1.2).

Replaces the v0.1.1 requirement to register a *second* Telegram bot.
When a DM lands and the agent could not be auto-woken, we tell the
operator through whichever path works first:

  Tier 1 — gateway ``deliver_only`` webhook route (reuses the TG bot
           the Hermes gateway already runs; zero extra setup).
  Tier 2 — ``hermes send --to <target>`` subprocess (same bot, CLI path).
  Tier 3 — legacy direct Telegram API via ``A2A_WAKE_TG_TOKEN`` +
           ``A2A_WAKE_TG_CHAT_ID`` (deprecated; kept for
           backwards-compat with v0.1.1 installs).

The ladder result is cached per-process: once a tier succeeds we keep
using it and stop probing the ones above it (reset on failure).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import urllib.error
import urllib.request
from typing import Optional

from a2a_dm_hermes import gatewaycfg

logger = logging.getLogger(__name__)

_TG_API_BASE = "https://api.telegram.org"
_TG_TIMEOUT_S = 5.0
_HERMES_SEND_TIMEOUT_S = 15.0


# ── Tier 3 — legacy direct Telegram (v0.1.1 path, deprecated) ─────


def _tg_configured() -> tuple[Optional[str], Optional[str]]:
    token = os.environ.get("A2A_WAKE_TG_TOKEN")
    chat_id = os.environ.get("A2A_WAKE_TG_CHAT_ID")
    if token and chat_id:
        return token, chat_id
    return None, None


def _tg_send(token: str, chat_id: str, text: str) -> bool:
    url = f"{_TG_API_BASE}/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text[:4000],
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TG_TIMEOUT_S) as resp:
            resp.read()
            return True
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.warning("a2a-dm: legacy TG push failed (%s)", exc)
        return False
    except Exception:  # noqa: BLE001
        logger.exception("a2a-dm: legacy TG push crashed")
        return False


# ── Tier 2 — hermes send CLI ──────────────────────────────────────


def _hermes_send(text: str) -> bool:
    """``hermes send`` wraps the gateway's send_message tool — it
    delivers through the bot the gateway already runs."""
    target = os.environ.get("A2A_WAKE_HOME")  # e.g. "telegram:-100123"
    cmd = ["hermes", "send"]
    if target:
        cmd += ["--to", target]
    cmd += [text[:4000]]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_HERMES_SEND_TIMEOUT_S,
        )
        if result.returncode == 0:
            return True
        logger.debug("a2a-dm: hermes send failed: %s", result.stderr[:300])
        return False
    except FileNotFoundError:
        logger.debug("a2a-dm: hermes CLI not on PATH")
        return False
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("a2a-dm: hermes send error (%s)", exc)
        return False


# ── Tier 1 — gateway deliver_only webhook route ───────────────────


def _gateway_notify(text: str) -> bool:
    if not gatewaycfg.webhook_platform_enabled():
        return False
    return gatewaycfg.post_route(
        gatewaycfg.NOTIFY_ROUTE, {"text": text[:4000]}
    )


# ── Public ladder ─────────────────────────────────────────────────


class HermesDelivery:
    """Sticky 3-tier notification ladder. Thread-safe."""

    _TIERS = (
        ("gateway-webhook", _gateway_notify),
        ("hermes-send", _hermes_send),
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sticky: Optional[int] = None  # index into ladder incl. tier 3

    def notify(self, text: str) -> bool:
        """Deliver *text* to the operator. Returns True if any tier
        succeeded. Never raises."""
        try:
            return self._notify(text)
        except Exception:  # noqa: BLE001
            logger.exception("a2a-dm: notification ladder crashed")
            return False

    def _notify(self, text: str) -> bool:
        with self._lock:
            start = self._sticky if self._sticky is not None else 0

        # Tiers 1-2
        for idx in range(start, len(self._TIERS)):
            name, fn = self._TIERS[idx]
            if fn(text):
                self._remember(idx)
                return True

        # Tier 3 — legacy direct TG
        tier3_idx = len(self._TIERS)
        if start <= tier3_idx:
            token, chat_id = _tg_configured()
            if token and chat_id:
                logger.info(
                    "a2a-dm: using deprecated A2A_WAKE_TG_TOKEN path — "
                    "prefer A2A_WAKE_HOME (reuses the gateway's own bot)."
                )
                if _tg_send(token, chat_id, text):
                    self._remember(tier3_idx)
                    return True

        self._forget()
        return False

    def _remember(self, idx: int) -> None:
        with self._lock:
            self._sticky = idx

    def _forget(self) -> None:
        with self._lock:
            self._sticky = None


_delivery = HermesDelivery()


def notify_operator(text: str) -> bool:
    """Module-level convenience wrapper around the shared ladder."""
    return _delivery.notify(text)
