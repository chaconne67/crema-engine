"""Crema carries Git for Windows in its installer; the app points the engine at its bash (CREMA_GIT_BASH)."""
from tools.environments import local


def test_carried_bash_comes_first(tmp_path, monkeypatch):
    carried = tmp_path / "git" / "bin" / "bash.exe"
    carried.parent.mkdir(parents=True)
    carried.write_bytes(b"")
    installed = tmp_path / "Program Files" / "Git" / "bin" / "bash.exe"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"")
    monkeypatch.setenv("CREMA_GIT_BASH", str(carried))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.setattr(local.shutil, "which", lambda name: None)
    assert local._windows_bash_candidates() == [str(carried), str(installed)]


def test_missing_carried_bash_falls_to_installed_git(tmp_path, monkeypatch):
    """A PC given an engine without the carried Git (a quick apply) still finds its own Git for Windows."""
    installed = tmp_path / "Program Files" / "Git" / "bin" / "bash.exe"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"")
    monkeypatch.setenv("CREMA_GIT_BASH", str(tmp_path / "nowhere" / "bash.exe"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.setattr(local.shutil, "which", lambda name: None)
    assert local._windows_bash_candidates() == [str(installed)]
