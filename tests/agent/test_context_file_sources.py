"""Context files: Crema loads SOUL.md and CREMA.md from the profile home, and CREMA.md from the top of the
chat's work folder.

Crema does not depend on other agent programs, so the instruction files they read (upstream Hermes
``.hermes.md``/``HERMES.md``, Codex ``AGENTS.md`` chain, Claude Code ``CLAUDE.md``, Cursor ``.cursorrules`` and
``.cursor/rules/*.mdc``) never reach the prompt or the ``/context`` manifest, wherever they sit. The manifest
and ``build_context_files_prompt`` must agree: a file is reported ``loaded`` iff its content is in the prompt.
"""

import pytest

from agent.context_file_sources import list_context_file_sources, render_context_file_lines
from agent.prompt_builder import build_context_files_prompt

_OTHER_AGENT_FILES = (
    ".hermes.md", "HERMES.md", "AGENTS.override.md", "AGENTS.md", "agents.md", "CLAUDE.md", "claude.md",
    ".cursorrules", ".cursor/rules/a.mdc",
)


@pytest.fixture()
def project(tmp_path):
    """A git project with every other-agent instruction file in its root and in a subfolder."""
    root = tmp_path / "proj"
    sub = root / "pkg"
    (root / ".git").mkdir(parents=True)
    for directory in (root, sub):
        (directory / ".cursor" / "rules").mkdir(parents=True)
        for name in _OTHER_AGENT_FILES:
            (directory / name).write_text(f"foreign rules {name}")
    return root, sub


@pytest.mark.parametrize("where", ["root", "sub", "launch_dir"])
@pytest.mark.parametrize("allow_install_tree_fallback", [False, True])
def test_other_agent_instruction_files_never_load(project, tmp_path_factory, monkeypatch, where,
                                                  allow_install_tree_fallback):
    root, sub = project
    home = tmp_path_factory.mktemp("home")
    (home / "SOUL.md").write_text("identity text")
    if where == "launch_dir":
        monkeypatch.chdir(sub)
        cwd = None
    else:
        cwd = str(root if where == "root" else sub)

    prompt = build_context_files_prompt(cwd=cwd, home_override=home,
                                        allow_install_tree_fallback=allow_install_tree_fallback)
    assert "foreign rules" not in prompt
    assert "identity text" in prompt
    assert build_context_files_prompt(cwd=cwd, skip_soul=True, home_override=home,
                                      allow_install_tree_fallback=allow_install_tree_fallback) == ""

    sources = list_context_file_sources(home_override=home)
    assert [(s["label"], s["status"], s["loaded"]) for s in sources] == [("SOUL.md", "loaded", True)]


def test_soul_md_statuses_follow_the_builder(monkeypatch, tmp_path_factory):
    import agent.prompt_builder as pb

    home = tmp_path_factory.mktemp("home")
    assert list_context_file_sources(home_override=home) == []
    assert render_context_file_lines([]) == []

    (home / "SOUL.md").write_text("")
    assert [s["status"] for s in list_context_file_sources(home_override=home)] == ["empty"]

    monkeypatch.setattr(pb, "_get_context_file_max_chars", lambda *_a: 40)
    (home / "SOUL.md").write_text("x" * 100)
    (entry,) = list_context_file_sources(home_override=home)
    assert entry["status"] == "truncated" and entry["loaded"] is True
    assert "[...truncated SOUL.md" in build_context_files_prompt(home_override=home)
    # The truncation is also queued for the user; drain it so the shared ContextVar stays clean for later tests.
    assert any("SOUL.md" in w for w in pb.drain_truncation_warnings())

    # The user's own SOUL.md is flagged by the injection scan but still loads — manifest and prompt agree.
    monkeypatch.setattr(pb, "_get_context_file_max_chars", lambda *_a: 10_000)
    monkeypatch.setattr(pb, "_scan_for_threats", lambda content, scope: ["fake-pattern"] if "evil" in content else [])
    (home / "SOUL.md").write_text("evil identity text")
    (entry,) = list_context_file_sources(home_override=home)
    assert entry["status"] == "flagged" and entry["loaded"] is True
    prompt = build_context_files_prompt(home_override=home)
    assert "evil identity text" in prompt and "[BLOCKED: SOUL.md" not in prompt
    lines = render_context_file_lines([entry])
    assert lines[0] == "Context files" and "SOUL.md" in lines[1] and "review the file" in lines[1]


def test_crema_md_all_chats_then_this_projects(project, tmp_path_factory):
    root, sub = project
    home = tmp_path_factory.mktemp("home")
    (home / "SOUL.md").write_text("identity text")
    (home / "CREMA.md").write_text("rules for every chat")
    (root / "CREMA.md").write_text("rules for this project")

    prompt = build_context_files_prompt(cwd=str(root), skip_soul=True, home_override=home)
    assert prompt.index("rules for every chat") < prompt.index("rules for this project")
    assert "follow this project's" in prompt and "foreign rules" not in prompt
    assert [(s["label"], s["status"]) for s in list_context_file_sources(home_override=home, cwd=str(root))] == [
        ("SOUL.md", "loaded"), ("CREMA.md (all chats)", "loaded"), ("CREMA.md (this project's folder)", "loaded")]

    # Without a project file (or from a subfolder: folders above the work folder are never read) only the
    # all-chats one loads, and there is nothing to choose between.
    for cwd in (str(sub), None):
        prompt = build_context_files_prompt(cwd=cwd, skip_soul=True, home_override=home)
        assert "rules for every chat" in prompt and "rules for this project" not in prompt
        assert "follow this project's" not in prompt
    (home / "CREMA.md").unlink()
    prompt = build_context_files_prompt(cwd=str(root), skip_soul=True, home_override=home)
    assert "rules for this project" in prompt and "rules for every chat" not in prompt


def test_crema_md_in_the_home_is_read_once_when_it_is_the_work_folder(tmp_path):
    (tmp_path / "CREMA.md").write_text("rules for every chat")
    prompt = build_context_files_prompt(cwd=str(tmp_path), skip_soul=True, home_override=tmp_path)
    assert prompt.count("rules for every chat") == 1


def test_a_work_folders_crema_md_is_blocked_on_a_scanner_hit_the_users_own_is_not(project, tmp_path_factory,
                                                                                 monkeypatch):
    import agent.prompt_builder as pb

    root, _ = project
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(pb, "_scan_for_threats", lambda content, scope: ["fake-pattern"] if "evil" in content else [])
    (home / "CREMA.md").write_text("evil but mine")
    (root / "CREMA.md").write_text("evil from a download")

    prompt = build_context_files_prompt(cwd=str(root), skip_soul=True, home_override=home)
    assert "evil but mine" in prompt and "evil from a download" not in prompt
    assert [s["status"] for s in list_context_file_sources(home_override=home, cwd=str(root))] == [
        "flagged", "blocked"]

