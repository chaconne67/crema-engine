"""Profile file roots guard file tools and local vision reads."""

import asyncio
import json

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.file_tools import patch_tool, read_file_tool, search_tool, write_file_tool
from tools.file_tools_paths import _resolve_path_for_task
from tools.skills_tool import skill_view


@pytest.fixture
def staff_profile(tmp_path):
    home = tmp_path / "profile"
    workspace = tmp_path / "workspace"
    uploads = tmp_path / "uploads"
    cache = home / "cache"
    for directory in (home, workspace, uploads, cache):
        directory.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "file_access_roots:\n"
        f"  - {workspace}\n"
        f"  - {uploads}\n"
        f"  - {cache}\n",
        encoding="utf-8",
    )
    token = set_hermes_home_override(home)
    try:
        yield home, workspace, uploads, cache
    finally:
        reset_hermes_home_override(token)


def test_profile_roots_allow_workspace_and_reject_parent_traversal(staff_profile):
    _, workspace, _, _ = staff_profile
    allowed = workspace / "notes.txt"
    assert _resolve_path_for_task(str(allowed)) == allowed
    with pytest.raises(RuntimeError, match="file_access_roots"):
        _resolve_path_for_task(str(workspace / ".." / "outside.txt"))


def test_profile_roots_reject_symlink_escape(staff_profile, tmp_path):
    _, workspace, _, _ = staff_profile
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    link = workspace / "linked.txt"
    link.symlink_to(outside)
    with pytest.raises(RuntimeError, match="file_access_roots"):
        _resolve_path_for_task(str(link))


def test_missing_roots_preserve_developer_path_behavior(tmp_path):
    profile = tmp_path / "developer-profile"
    profile.mkdir()
    token = set_hermes_home_override(profile)
    outside = tmp_path / "developer-file.txt"
    try:
        assert _resolve_path_for_task(str(outside)) == outside
    finally:
        reset_hermes_home_override(token)


def test_read_write_and_patch_direct_entries_reject_paths_outside_profile(staff_profile, tmp_path):
    _, _, _, _ = staff_profile
    outside = tmp_path / "outside.txt"
    outside.write_text("original", encoding="utf-8")

    assert "file_access_roots" in json.loads(read_file_tool(str(outside))).get("error", "")
    assert "file_access_roots" in json.loads(
        write_file_tool(str(outside), "changed")).get("error", "")
    assert "file_access_roots" in json.loads(
        patch_tool(path=str(outside), old_string="original", new_string="changed")).get("error", "")
    assert outside.read_text(encoding="utf-8") == "original"


def test_search_direct_entry_rejects_outside_path(staff_profile, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    result = json.loads(search_tool("needle", path=str(outside)))
    assert "file_access_roots" in result.get("error", "")


def test_search_multi_root_input_validates_each_root_before_search(staff_profile, monkeypatch):
    from tools import file_tools, terminal_tool

    _, workspace, _, _ = staff_profile
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    terminal_tool.record_session_cwd("default", str(workspace))
    monkeypatch.setattr(
        file_tools, "_get_file_ops",
        lambda task_id="default": pytest.fail("search ran before every root was checked"),
    )

    result = json.loads(search_tool("needle", path="missing /etc"))

    assert "file_access_roots" in result.get("error", "")


def test_search_does_not_read_symlink_file_before_result_filter(staff_profile, monkeypatch):
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations
    from tools import file_tools
    from tools import terminal_tool

    _, workspace, _, _ = staff_profile
    outside_secret = "unique-private-symlink-content"
    outside = workspace.parent / "secret.txt"
    outside.write_text(outside_secret, encoding="utf-8")
    (workspace / "linked.txt").symlink_to(outside)
    env = LocalEnvironment(cwd=str(workspace))
    ops = ShellFileOperations(env)
    commands = []
    run_rg = ShellFileOperations._run_rg_bounded

    def record_rg(self, words, fetch_limit, timeout, **kwargs):
        commands.extend(words)
        return run_rg(self, words, fetch_limit, timeout, **kwargs)

    monkeypatch.setattr(ShellFileOperations, "_run_rg_bounded", record_rg)
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id="default": ops)
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})

    result = json.loads(search_tool(outside_secret, path=str(workspace)))
    file_result = json.loads(search_tool("linked.txt", target="files", path=str(workspace)))

    assert result.get("total_count") == 0
    assert outside_secret not in json.dumps(result)
    assert file_result.get("total_count") == 0
    assert any("--no-follow" in command for command in commands)


def test_search_grep_fallback_does_not_read_symlink_file(staff_profile, tmp_path, monkeypatch):
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations
    from tools import file_tools, terminal_tool

    _, workspace, _, _ = staff_profile
    outside_secret = "private-content-via-symlink-grep-fallback"
    outside = workspace.parent / "grep-secret.txt"
    outside.write_text(outside_secret, encoding="utf-8")
    (workspace / "linked-grep.txt").symlink_to(outside)
    ops = ShellFileOperations(LocalEnvironment(cwd=str(workspace)))
    monkeypatch.setattr(ops, "_has_command", lambda command: command == "grep")
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id="default": ops)
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})

    result = json.loads(search_tool(outside_secret, path=str(workspace)))

    assert result.get("total_count") == 0
    assert outside_secret not in json.dumps(result)


def test_vision_local_file_read_rejects_outside_profile(staff_profile, tmp_path):
    from tools.image_source import ResolveContext, SourceUnsafe, resolve_image_source

    outside = tmp_path / "outside.png"
    outside.write_bytes(b"private image bytes")
    with pytest.raises(SourceUnsafe, match="file_access_roots"):
        asyncio.run(resolve_image_source(str(outside), ResolveContext()))


def test_video_local_file_read_rejects_outside_profile(staff_profile, tmp_path):
    from tools.vision_tools import _materialize_video

    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"private video bytes")
    with pytest.raises(RuntimeError, match="file_access_roots"):
        asyncio.run(_materialize_video(str(outside), None, []))


def test_skill_view_keeps_content_but_skips_inline_shell_for_scoped_profile(
    staff_profile, tmp_path, monkeypatch,
):
    import tools.skills_tool as skills_tool

    home, _, _, _ = staff_profile
    marker = tmp_path / "inline-shell-ran"
    (home / "config.yaml").write_text(
        "file_access_roots: []\n"
        "skills:\n  inline_shell: true\n",
        encoding="utf-8",
    )
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "shell-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: shell-skill\ndescription: test\n---\n"
        f"Inline result: !`touch {marker}`\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", skills_dir)

    result = json.loads(skill_view("shell-skill", preprocess=True))

    assert result["success"] is True
    assert f"!`touch {marker}`" in result["content"]
    assert not marker.exists()


def test_shared_skill_preprocessor_skips_inline_shell_for_scoped_profile(staff_profile, tmp_path):
    from agent.skill_preprocessing import preprocess_skill_content

    home, _, _, _ = staff_profile
    marker = tmp_path / "slash-inline-shell-ran"
    (home / "config.yaml").write_text(
        "file_access_roots: []\n"
        "skills:\n  inline_shell: true\n",
        encoding="utf-8",
    )

    content = preprocess_skill_content(
        f"Inline result: !`touch {marker}`", tmp_path, skills_cfg={"inline_shell": True})

    assert f"!`touch {marker}`" in content
    assert not marker.exists()
