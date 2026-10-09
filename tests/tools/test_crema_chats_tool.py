"""Crema: messages between chats through the crema_chats tool (tools/crema_chats_tool.py)."""
import json

from agent import crema_file_turns as turns
from tools.registry import registry
from toolsets import resolve_toolset

A, B = "agent-client-a", "agent-client-b"


class FakeApi:
    _run_statuses = {}

    def _ensure_session_db(self):
        raise AssertionError("titles come from the sidebar list")


def test_the_tool_lists_and_sends_through_the_registry(monkeypatch):
    import tools.crema_chats_tool  # noqa: F401 — registers the tool

    monkeypatch.setattr(turns, "_api", FakeApi())
    turns.set_chats([{"id": "a", "session": A, "title": "보고서"}, {"id": "b", "session": B, "title": "메일"}])
    listed = json.loads(registry.dispatch("crema_chats", {"action": "list"}, session_id=A))
    assert listed == {"chats": [{"number": "b", "title": "메일", "project": "", "state": "idle"}]}
    sent = json.loads(registry.dispatch("crema_chats", {"action": "send", "to": "b", "message": "안녕"}, session_id=A))
    assert sent == {"sent_to": "메일", "delivery": "it reads this when it next works"}
    assert "error" in json.loads(registry.dispatch("crema_chats", {"action": "send", "to": "a", "message": "x"}, session_id=A))
    assert "error" in json.loads(registry.dispatch("crema_chats", {"action": "other"}, session_id=A))


def test_crema_offers_the_tool():
    assert "crema_chats" in resolve_toolset("hermes-api-server")
