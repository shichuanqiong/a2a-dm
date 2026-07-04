"""SDK webhook API tests (v0.2.8 / v3.0 Phase 3).

Coverage:
  * verify_signature: valid sig, wrong secret, wrong body, expired
    timestamp, malformed timestamp, missing fields
  * client.webhooks.register: 200 path, secret returned, raises on
    missing-secret response
  * client.webhooks.list: maps server payload
  * client.webhooks.delete: ok flag
  * Constant-time compare is used (introspection)
"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest
import responses

from a2a_dm import AgentClient, WebhookInfo, verify_signature


# ── verify_signature ────────────────────────────────────────────


def _make_sig(secret: str, body: bytes, ts: int) -> tuple[str, str]:
    """Build a (timestamp, signature) pair the way the server would."""
    ts_str = str(ts)
    expected = hmac.new(
        secret.encode("utf-8"),
        ts_str.encode("utf-8") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return ts_str, f"sha256={expected}"


def test_verify_signature_valid():
    body = b'{"event":"a2a.message.sent"}'
    secret = "topsecret"
    ts = 1716820000
    ts_header, sig_header = _make_sig(secret, body, ts)
    assert verify_signature(secret, body, ts_header, sig_header, now_s=ts) is True


def test_verify_signature_wrong_secret():
    body = b'{"a":1}'
    ts = 1716820000
    ts_header, sig_header = _make_sig("right_secret", body, ts)
    assert verify_signature("WRONG", body, ts_header, sig_header, now_s=ts) is False


def test_verify_signature_tampered_body():
    """Even one byte changed in body must fail verification."""
    secret = "x"
    ts = 1716820000
    ts_header, sig_header = _make_sig(secret, b'{"a":1}', ts)
    assert verify_signature(secret, b'{"a":2}', ts_header, sig_header, now_s=ts) is False


def test_verify_signature_replay_window():
    """5-minute window — older than 300s must fail."""
    secret = "x"
    body = b"data"
    ts = 1716820000
    ts_header, sig_header = _make_sig(secret, body, ts)
    # Right at the edge: 300s is OK, 301s is NOT.
    assert verify_signature(secret, body, ts_header, sig_header, now_s=ts + 300) is True
    assert verify_signature(secret, body, ts_header, sig_header, now_s=ts + 301) is False
    # Also reject if timestamp is FROM THE FUTURE by > window (clock skew attack).
    assert verify_signature(secret, body, ts_header, sig_header, now_s=ts - 301) is False


def test_verify_signature_malformed_timestamp():
    body = b"x"
    assert verify_signature("s", body, "not-a-number", "sha256=abc") is False
    assert verify_signature("s", body, "", "sha256=abc") is False


def test_verify_signature_missing_fields():
    body = b"x"
    assert verify_signature("", body, "1", "sha256=abc") is False
    assert verify_signature("s", body, "1", "") is False


def test_verify_signature_uses_constant_time_compare():
    """Introspection — the verifier must use hmac.compare_digest, not
    `==`, to prevent timing side-channels. A simple grep of the
    module source is the most stable guard."""
    import inspect
    from a2a_dm import webhooks_api

    src = inspect.getsource(webhooks_api.verify_signature)
    assert "compare_digest" in src, (
        "verify_signature must use hmac.compare_digest for timing-"
        "safe comparison, not == or string match."
    )


# ── client.webhooks.register ────────────────────────────────────


@responses.activate
def test_webhooks_register_returns_secret_once():
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/bots/bestiedog/webhook",
        json={
            "ok": True,
            "webhook": {
                "id": "abc-123",
                "bot_id": "bestiedog",
                "url": "https://example.com/wh",
                "active": True,
                "last_delivery_at": None,
                "secret": "VERY_SECRET_VALUE",
            },
        },
        status=200,
    )
    client = AgentClient(token="bt_x", bot_id="bestiedog")
    info = client.webhooks.register("https://example.com/wh")
    assert isinstance(info, WebhookInfo)
    assert info.id == "abc-123"
    assert info.bot_id == "bestiedog"
    assert info.url == "https://example.com/wh"
    assert info.active is True
    assert info.secret == "VERY_SECRET_VALUE"


@responses.activate
def test_webhooks_register_raises_if_secret_missing():
    """Defensive — if the platform contract is violated and the
    register response lacks `secret`, raise loudly so the operator
    re-registers rather than silently storing None and failing later."""
    responses.add(
        responses.POST,
        "https://api.agoradigest.com/a2a/v1/bots/bestiedog/webhook",
        json={
            "ok": True,
            "webhook": {
                "id": "abc-123",
                "bot_id": "bestiedog",
                "url": "https://example.com/wh",
                "active": True,
                # No secret!
            },
        },
        status=200,
    )
    client = AgentClient(token="bt_x", bot_id="bestiedog")
    with pytest.raises(RuntimeError, match="secret"):
        client.webhooks.register("https://example.com/wh")


def test_webhooks_register_requires_bot_id():
    """ValueError when neither client.bot_id nor explicit bot_id."""
    client = AgentClient(token="bt_x")  # no bot_id
    with pytest.raises(ValueError, match="bot_id"):
        client.webhooks.register("https://example.com/wh")


# ── client.webhooks.list ───────────────────────────────────────


@responses.activate
def test_webhooks_list_excludes_secret():
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/a2a/v1/bots/bestiedog/webhook",
        json={
            "count": 2,
            "webhooks": [
                {"id": "w1", "bot_id": "bestiedog", "url": "https://a.com",
                 "active": True},
                {"id": "w2", "bot_id": "bestiedog", "url": "https://b.com",
                 "active": False},
            ],
        },
        status=200,
    )
    client = AgentClient(token="bt_x", bot_id="bestiedog")
    items = client.webhooks.list()
    assert len(items) == 2
    assert items[0].id == "w1" and items[0].active is True
    assert items[1].id == "w2" and items[1].active is False
    assert all(w.secret is None for w in items), (
        "list() must NEVER include secret — server only returns it on register"
    )


# ── client.webhooks.delete ─────────────────────────────────────


@responses.activate
def test_webhooks_delete_returns_true_on_ok():
    responses.add(
        responses.DELETE,
        "https://api.agoradigest.com/a2a/v1/bots/bestiedog/webhook/w1",
        json={"ok": True, "deleted_id": "w1"},
        status=200,
    )
    client = AgentClient(token="bt_x", bot_id="bestiedog")
    assert client.webhooks.delete("w1") is True


# ── End-to-end signing flow (sender ↔ receiver) ────────────────


def test_full_sign_then_verify_flow():
    """The hot path: server signs with a known secret + timestamp,
    receiver verifies with the same secret. This mirrors what
    actually happens in production:
      1. Platform builds payload bytes
      2. Server runs sign_payload (in dispatcher)
      3. POST headers + body to receiver
      4. Receiver runs verify_signature
    """
    # Same algorithm the server uses — recreate inline for the test.
    secret = "deadbeef"
    body = b'{"event":"a2a.message.sent","task":{"id":"x"}}'
    ts = int(time.time())
    ts_header = str(ts)
    digest = hmac.new(secret.encode("utf-8"),
                      ts_header.encode("utf-8") + b"." + body,
                      hashlib.sha256).hexdigest()
    sig_header = f"sha256={digest}"

    # Receiver verifies.
    assert verify_signature(secret, body, ts_header, sig_header) is True

    # Same body but receiver got truncated body — must fail.
    assert verify_signature(secret, body[:-1], ts_header, sig_header) is False
