"""Crema carries Git for Windows in its installer; the app points the engine at its bash (CREMA_GIT_BASH)."""
from pathlib import Path

import pytest

from pm import shell


def test_carried_bash_comes_first():
    carried = r"C:\Crema\git\bin\bash.exe"
    installed = r"C:\Program Files\Git\bin\bash.exe"
    candidates = shell.windows_bash_candidates(None, {"CREMA_GIT_BASH": carried}, crema=True)
    assert candidates[:2] == [carried, installed]


@pytest.mark.platforms("windows")
def test_missing_carried_bash_falls_to_installed_git(tmp_path, monkeypatch):
    """A PC given an engine without the carried Git (a quick apply) still finds its own Git for Windows."""
    installed = tmp_path / "Program Files" / "Git" / "bin" / "bash.exe"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"")
    monkeypatch.setenv("CREMA_GIT_BASH", str(tmp_path / "nowhere" / "bash.exe"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.setattr(shell.shutil, "which", lambda name: None)
    monkeypatch.setattr("tools.environments.local_gitbash_probe._bash_starts", lambda p: Path(p) == installed)
    assert shell.bash(crema=True) == str(installed)
