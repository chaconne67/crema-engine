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


def test_assistant_reads_service_own_and_project_instructions_once(tmp_path):
    from agent import system_prompt as sp
    home = tmp_path / "profile"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    (home / ".service-instructions.md").write_text("SERVICE_SENTINEL")
    (home / "CREMA.md").write_text("OWN_SENTINEL")
    (project / "CREMA.md").write_text("PROJECT_SENTINEL")
    # Real context loader and prompt builder. Only environment/identity probes are isolated.
    with patch.object(sp, "_agent_home", return_value=home), patch.object(sp, "resolve_context_cwd", return_value=str(project)):
        prompt = _prompt(_prompt_mode="assistant")
    assert prompt.count("SERVICE_SENTINEL") == 1
    assert prompt.count("OWN_SENTINEL") == 1
    assert prompt.count("PROJECT_SENTINEL") == 1
    assert prompt.index("SERVICE_SENTINEL") < prompt.index("OWN_SENTINEL") < prompt.index("PROJECT_SENTINEL")
    assert "# How you work" not in prompt and "# Runtime environment" not in prompt


def test_common_instructions_are_profile_scoped_and_use_existing_truncation(tmp_path):
    from agent.prompt_builder import build_context_files_prompt
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    # More than the plugin-section limit: the native context-file limit applies instead.
    (a / ".service-instructions.md").write_text("공통 지침\n" * 900 + "COMMON_END")
    (a / "CREMA.md").write_text("PRIVATE_A")
    (b / "CREMA.md").write_text("PRIVATE_B")
    first = build_context_files_prompt(skip_soul=True, home_override=a)
    second = build_context_files_prompt(skip_soul=True, home_override=b)
    assert "COMMON_END" in first and "PRIVATE_A" in first and "PRIVATE_B" not in first
    assert "PRIVATE_B" in second and "PRIVATE_A" not in second and "COMMON_END" not in second
