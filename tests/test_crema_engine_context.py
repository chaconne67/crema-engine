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
    agent = SimpleNamespace(tools=[{"name": "terminal"}], context_compressor=SimpleNamespace(threshold_tokens=256_000))
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
    assert crema_engine.estimate_context(api, "agent-client-x") == {"used": 17_000, "max": 256_000}
    assert seen == [([], "기본 지시\n\n기억", agent.tools)]
    assert checked_in == [agent]


def test_continuing_chat_counts_its_stored_prompt_and_history(monkeypatch):
    seen, checked_in = _count(monkeypatch), []
    monkeypatch.setattr(system_prompt, "build_system_prompt_parts", lambda agent: (_ for _ in ()).throw(AssertionError))
    history = [{"role": "user", "content": "안녕"}, {"role": "assistant", "content": "네"}]
    api, _ = _api(FakeDb(history, prompt="처음 지시"), checked_in)
    assert crema_engine.estimate_context(api, "agent-client-x")["used"] == 17_002
    assert seen[0][:2] == (history, "처음 지시")


def test_crema_compresses_a_1m_chat_at_half_its_window():
    """주인님 2026-11-04: no absolute cap — a 1M-window chat stays whole to 500K, not upstream's 256K."""
    from unittest.mock import patch

    from agent.context_compressor import ContextCompressor
    from hermes_cli.config import DEFAULT_CONFIG

    compression = DEFAULT_CONFIG["compression"]
    assert compression["threshold_tokens"] is None
    with patch("agent.context_compressor.get_model_context_length", return_value=1_000_000):
        comp = ContextCompressor("model-a", threshold_percent=compression["threshold"],
                                 threshold_tokens_cap=compression["threshold_tokens"], quiet_mode=True)
        _ = comp.context_length
    assert comp.threshold_tokens == 500_000
