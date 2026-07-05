"""Smoke tests for the a2a-dm Hermes plugin.

The tests use a fake ``ctx`` that records the register calls so we
don't need Hermes itself running. They verify:

  * Import doesn't blow up (with or without env vars).
  * ``register(ctx)`` wires all 12 tools + the hook + the slash command.
  * Every schema has a matching handler.
  * The wake queue drains cleanly and formats a sensible context block.
  * ``_wake_injection`` returns None on empty queue and a dict on full.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class FakeCtx:
    """Minimal ``ctx`` shim for testing register()."""

    def __init__(self):
        self.tools = {}
        self.hooks = {}
        self.commands = {}

    def register_tool(self, *, name, toolset, schema, handler):
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
        }

    def register_hook(self, event, callback):
        self.hooks.setdefault(event, []).append(callback)

    def register_command(self, name, *, handler, description=""):
        self.commands[name] = {"handler": handler, "description": description}


def test_import_does_not_require_env(monkeypatch):
    """The plugin package must import cleanly even without creds set."""
    monkeypatch.delenv("AGORADIGEST_TOKEN", raising=False)
    monkeypatch.delenv("AGORADIGEST_BOT_ID", raising=False)
    import importlib
    import a2a_dm_hermes

    importlib.reload(a2a_dm_hermes)
    assert a2a_dm_hermes.__version__


def test_register_wires_all_tools(monkeypatch):
    """register(ctx) must add every schema-handler pair to ctx."""
    # Prevent SSE from actually opening a connection during test.
    monkeypatch.setenv("AGORADIGEST_TOKEN", "")
    monkeypatch.setenv("AGORADIGEST_BOT_ID", "")

    import importlib
    import a2a_dm_hermes
    importlib.reload(a2a_dm_hermes)

    ctx = FakeCtx()
    a2a_dm_hermes.register(ctx)

    expected = {
        "a2a_send_dm", "a2a_reply", "a2a_get_inbox",
        "a2a_get_conversation", "a2a_list_friends", "a2a_add_friend",
        "a2a_send_group", "a2a_create_group", "a2a_list_groups",
        "a2a_invite_to_group", "a2a_accept_invite", "a2a_leave_group",
    }
    assert set(ctx.tools) == expected


def test_register_wires_hook_and_command(monkeypatch):
    monkeypatch.setenv("AGORADIGEST_TOKEN", "")
    monkeypatch.setenv("AGORADIGEST_BOT_ID", "")

    import importlib
    import a2a_dm_hermes
    importlib.reload(a2a_dm_hermes)

    ctx = FakeCtx()
    a2a_dm_hermes.register(ctx)

    assert "pre_llm_call" in ctx.hooks
    assert len(ctx.hooks["pre_llm_call"]) == 1
    assert "a2adm" in ctx.commands


def test_every_schema_has_handler():
    """No orphan schemas — the ``ALL_SCHEMAS`` list must match the
    ``HANDLERS`` dispatch table exactly."""
    from a2a_dm_hermes.schemas import ALL_SCHEMAS
    from a2a_dm_hermes.tools import HANDLERS

    schema_names = {n for n, _ in ALL_SCHEMAS}
    handler_names = set(HANDLERS.keys())
    assert schema_names == handler_names, (
        "schema/handler drift: "
        f"only in schemas: {schema_names - handler_names}, "
        f"only in handlers: {handler_names - schema_names}"
    )


def test_wake_injection_returns_none_when_empty(monkeypatch):
    monkeypatch.setenv("AGORADIGEST_TOKEN", "")
    monkeypatch.setenv("AGORADIGEST_BOT_ID", "")
    import importlib
    import a2a_dm_hermes
    importlib.reload(a2a_dm_hermes)

    from a2a_dm_hermes.runtime import WakeRuntime
    runtime = WakeRuntime.get()
    # Ensure queue empty.
    runtime.drain()

    result = a2a_dm_hermes._wake_injection()
    assert result is None


def test_wake_injection_formats_pending_dms(monkeypatch):
    monkeypatch.setenv("AGORADIGEST_TOKEN", "")
    monkeypatch.setenv("AGORADIGEST_BOT_ID", "")
    import importlib
    import a2a_dm_hermes
    importlib.reload(a2a_dm_hermes)

    from a2a_dm_hermes.runtime import WakeRuntime
    runtime = WakeRuntime.get()
    runtime.drain()  # start clean

    # Enqueue a fake wake — simulates SSEDaemon callback.
    fake_task = MagicMock()
    fake_task.id = "task-abcdef-123"
    fake_task.sender_bot_id = "laobaigan"
    fake_task.message.text = "yo, alive?"
    fake_task.is_group_message = False
    fake_task.group_id = None
    fake_task.created_at = "2026-07-04T09:00:00+00:00"

    runtime._on_wake(fake_task, None)
    result = a2a_dm_hermes._wake_injection()
    assert result is not None
    assert "context" in result
    assert "laobaigan" in result["context"]
    assert "yo, alive?" in result["context"]
    assert "a2a_reply" in result["context"]


def test_wake_injection_handles_group_message(monkeypatch):
    monkeypatch.setenv("AGORADIGEST_TOKEN", "")
    monkeypatch.setenv("AGORADIGEST_BOT_ID", "")
    import importlib
    import a2a_dm_hermes
    importlib.reload(a2a_dm_hermes)

    from a2a_dm_hermes.runtime import WakeRuntime
    runtime = WakeRuntime.get()
    runtime.drain()

    fake_task = MagicMock()
    fake_task.id = "task-uuid-999"
    fake_task.sender_bot_id = "bestiedog"
    fake_task.message.text = "new arxiv paper drop"
    fake_task.is_group_message = True
    fake_task.group_id = "group_ext_ml-abc12345"
    fake_task.created_at = "2026-07-04T09:00:00+00:00"

    runtime._on_wake(fake_task, None)
    result = a2a_dm_hermes._wake_injection()
    assert result is not None
    assert "a2a_send_group" in result["context"]
    assert "group_ext_ml-abc12345" in result["context"]


def test_tools_return_error_json_when_unconfigured(monkeypatch):
    """Every tool must return an ``{"error": ...}`` JSON string when
    creds are missing — Hermes should never see a Python exception."""
    monkeypatch.delenv("AGORADIGEST_TOKEN", raising=False)
    monkeypatch.delenv("AGORADIGEST_BOT_ID", raising=False)

    # Reset the cached client so the tool re-checks env.
    import importlib
    import a2a_dm_hermes.tools as t
    importlib.reload(t)

    import json
    for name, handler in t.HANDLERS.items():
        out = handler({})
        parsed = json.loads(out)
        assert "error" in parsed, (
            f"{name} did not return an error envelope without creds: {out!r}"
        )


def test_slash_command_returns_status(monkeypatch):
    monkeypatch.setenv("AGORADIGEST_TOKEN", "")
    monkeypatch.setenv("AGORADIGEST_BOT_ID", "")
    import importlib
    import a2a_dm_hermes
    importlib.reload(a2a_dm_hermes)

    out = a2a_dm_hermes._slash_a2adm("")
    assert "a2a-dm v" in out
    assert "wake queue" in out
