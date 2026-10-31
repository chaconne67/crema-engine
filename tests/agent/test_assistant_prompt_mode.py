"""agent.prompt_mode (FC-CRM ADR 0026): "assistant" builds a lean prompt; the default leaves Crema's untouched."""
from unittest.mock import patch

from agent.system_prompt import build_system_prompt
from tests.agent.test_system_prompt import _make_agent

_TOOLS = ["todo_list", "memory"]
_SOUL = "당신은 김FC님의 고객 관리 비서입니다."


def _prompt(**overrides):
    with patch("agent.system_prompt._pb.load_soul_md", return_value=_SOUL):
        return build_system_prompt(_make_agent(load_soul_identity=True, valid_tool_names=_TOOLS, **overrides))


def test_default_and_explicit_crema_mode_build_the_same_prompt():
    assert _prompt() == _prompt(_prompt_mode="crema")


def test_assistant_mode_keeps_soul_and_drops_crema_scaffolding():
    lean, full = _prompt(_prompt_mode="assistant"), _prompt()
    assert lean.startswith(_SOUL)
    assert "Conversation started:" in lean
    for crema_only in ("# How you work", "# Runtime environment", "Active profile", "# Parallel tool calls",
                       "Mid-turn user steering"):
        assert crema_only in full
        assert crema_only not in lean
    assert len(lean) < len(full) / 3
