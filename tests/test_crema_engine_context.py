"""The context ring's estimate before a chat's first reply: instructions, tools and the chat so far."""
from types import SimpleNamespace

import crema_engine
from agent import model_metadata, system_prompt


class FakeDb:
    def __init__(self, messages, prompt=None):
        self.messages, self.prompt = messages, prompt

    def get_messages_as_conversation(self, session_id, **_):
        return self.messages

    def get_session(self, session_id):
        return {"system_prompt": self.prompt}


def _api(db, checked_in):
    agent = SimpleNamespace(tools=[{"name": "terminal"}], context_compressor=SimpleNamespace(context_length=500_000))
    return SimpleNamespace(
        _ensure_session_db=lambda: db, _create_agent=lambda **_: agent,
        _memory_sessions=SimpleNamespace(checkin=checked_in.append)), agent


def _count(monkeypatch):
    seen = []

    def estimate(messages, *, system_prompt="", tools=None):
        seen.append((messages, system_prompt, tools))
        return 17_000 + len(messages)
    monkeypatch.setattr(model_metadata, "estimate_request_tokens_rough", estimate)
    return seen


def test_new_chat_counts_the_prompt_built_now_and_tools(monkeypatch):
    seen, checked_in = _count(monkeypatch), []
    monkeypatch.setattr(system_prompt, "build_system_prompt_parts",
                        lambda agent: {"stable": "기본 지시", "context": "", "volatile": "기억"})
    api, agent = _api(FakeDb([]), checked_in)
    assert crema_engine.estimate_context(api, "agent-client-x") == {"used": 17_000, "max": 500_000}
    assert seen == [([], "기본 지시\n\n기억", agent.tools)]
    assert checked_in == [agent]


def test_continuing_chat_counts_its_stored_prompt_and_history(monkeypatch):
    seen, checked_in = _count(monkeypatch), []
    monkeypatch.setattr(system_prompt, "build_system_prompt_parts", lambda agent: (_ for _ in ()).throw(AssertionError))
    history = [{"role": "user", "content": "안녕"}, {"role": "assistant", "content": "네"}]
    api, _ = _api(FakeDb(history, prompt="처음 지시"), checked_in)
    assert crema_engine.estimate_context(api, "agent-client-x")["used"] == 17_002
    assert seen[0][:2] == (history, "처음 지시")
