"""Crema: messages between chats through the crema_chats tool (tools/crema_chats_tool.py)."""
import json

from agent import crema_file_turns as turns
from tools.registry import registry
from toolsets import resolve_toolset

A, B = "agent-client-a", "agent-client-b"


class FakeApi:
    _run_statuses = {}

    def _ensure_store(self):
        raise AssertionError("titles come from the sidebar list")


def test_the_tool_lists_and_sends_through_the_registry(monkeypatch):
    import tools.crema_chats_tool  # noqa: F401 — registers the tool

    monkeypatch.setattr(turns, "_api", FakeApi())
    turns.set_chats([{"id": "a", "session": A, "title": "보고서"}, {"id": "b", "session": B, "title": "메일"}])
    turns.set_models([{"model": "gpt-6-luna", "provider": "openai-codex"}, {"model": "claude-opus-5-5", "provider": "anthropic"},
                      {"model": "gpt-6-luna", "provider": "openrouter"}, {"model": "no-provider"}])
    listed = json.loads(registry.dispatch("crema_chats", {"action": "list"}, session_id=A))
    assert listed == {"chats": [{"number": "b", "title": "메일", "project": "", "state": "idle"}]}
    models = json.loads(registry.dispatch("crema_chats", {"action": "models"}, session_id=A))
    assert models == {"models": ["claude-opus-5-5", "gpt-6-luna"]}
    sent = json.loads(registry.dispatch("crema_chats", {"action": "send", "to": "b", "message": "안녕"}, session_id=A))
    assert sent["sent_to"] == "메일" and sent["delivery"].startswith("Crema hands it over")
    assert "error" in json.loads(registry.dispatch("crema_chats", {"action": "read", "to": "z"}, session_id=A))
    assert "error" in json.loads(registry.dispatch("crema_chats", {"action": "send", "to": "a", "message": "x"}, session_id=A))
    assert "error" in json.loads(registry.dispatch("crema_chats", {"action": "other"}, session_id=A))


def test_crema_offers_the_tool():
    assert "crema_chats" in resolve_toolset("hermes-api-server")


def test_work_action_dispatches_request_ids_without_exposing_grant_actions(monkeypatch):
    import tools.crema_chats_tool as tool
    from agent import crema_collaboration

    seen = {}

    def create(session_id, to, message, **kwargs):
        seen.update(session_id=session_id, to=to, message=message, **kwargs)
        return {"ok": True, "work": {"request_id": kwargs["request_id"], "message_id": kwargs["message_id"]}}

    monkeypatch.setattr(crema_collaboration, "create", create)
    reply = json.loads(tool.crema_chats({"action": "work", "to": "b", "message": "확인",
                                         "request_id": "r1", "attempt_id": "a1", "message_id": "m1",
                                         "mode": "steer", "kind": "question"}, session_id=A))
    assert reply == {"ok": True, "work": {"request_id": "r1", "message_id": "m1"}}
    assert seen == {"session_id": A, "to": "b", "message": "확인", "mode": "steer", "kind": "question",
                    "request_id": "r1", "attempt_id": "a1", "message_id": "m1", "model": "", "revision": None,
                    "requirements": "", "parent_request_id": "", "target_request_id": "",
                    "target_attempt_id": "", "target_message_id": ""}
    actions = registry._tools["crema_chats"].schema["parameters"]["properties"]["action"]["enum"]
    assert "grant" not in actions and "revoke" not in actions
    assert "contacts" in actions and "work" in actions
