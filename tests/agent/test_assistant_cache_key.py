"""agent.prompt_mode: assistant shares one prompt_cache_key across profiles and conversations (FC-CRM ADR 0026)."""
from types import SimpleNamespace

from agent.chat_completion_helpers import _assistant_cache_key_override

_TOOLS = [{"type": "function", "name": "memory"}, {"type": "function", "name": "todo_list"}]


def _agent(mode, stable="# FC 비서\n같은 앞부분", sid="s1"):
    return SimpleNamespace(_prompt_mode=mode, _cached_system_prompt_static=stable, session_id=sid)


def test_default_mode_leaves_request_overrides_untouched():
    overrides = {"service_tier": "flex"}
    assert _assistant_cache_key_override(_agent("crema"), overrides, _TOOLS) is overrides
    assert _assistant_cache_key_override(SimpleNamespace(), None, _TOOLS) is None


def test_assistant_mode_key_ignores_conversation_but_follows_prefix_and_tools():
    a = _assistant_cache_key_override(_agent("assistant", sid="fc-a-1"), None, _TOOLS)["prompt_cache_key"]
    b = _assistant_cache_key_override(_agent("assistant", sid="fc-b-9"), {}, list(reversed(_TOOLS)))["prompt_cache_key"]
    assert a == b and a.startswith("pck_") and len(a) <= 64
    assert _assistant_cache_key_override(_agent("assistant", stable="다른 지침"), None, _TOOLS)["prompt_cache_key"] != a
    assert _assistant_cache_key_override(_agent("assistant"), None, _TOOLS[:1])["prompt_cache_key"] != a


def test_explicit_override_wins():
    assert _assistant_cache_key_override(_agent("assistant"), {"prompt_cache_key": "mine"}, _TOOLS) == {
        "prompt_cache_key": "mine"}
