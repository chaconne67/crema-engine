"""Crema ego lite (tools/crema_ego.py): finding its ego-browser command, fetching and opening its installer."""
import os
import subprocess
from pathlib import Path

import pytest

from tools import crema_ego


def _command(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    command = folder / ("ego-browser.exe" if os.name == "nt" else "ego-browser")
    command.write_text("")
    command.chmod(0o755)
    return command


def test_not_installed(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(crema_ego, "_user_path", lambda: "")
    assert crema_ego.state() == {"installed": False}


def test_found_in_the_users_new_path_joins_the_engines(monkeypatch, tmp_path):
    # Installed after Crema started: only the user's PATH in Windows has it, and the agent's terminal needs it too.
    command = _command(tmp_path / "ego")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(crema_ego, "_user_path", lambda: str(command.parent))
    assert crema_ego.state() == {"installed": True}
    assert os.environ["PATH"].split(os.pathsep)[0] == str(command.parent)


class _Install:
    def __init__(self, monkeypatch, signer):
        self.opened = []
        monkeypatch.setattr(crema_ego.urllib.request, "urlretrieve", lambda url, path: Path(path).write_bytes(b"MZ"))
        monkeypatch.setattr(crema_ego.subprocess, "run",
                            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=signer, stderr=""))
        monkeypatch.setattr(crema_ego.os, "startfile", self.opened.append, raising=False)


def test_install_opens_the_installer_citro_labs_signed(monkeypatch):
    install = _Install(monkeypatch, "CN=CITRO LABS PTE. LIMITED, O=CITRO LABS PTE. LIMITED, L=Singapore, C=SG\n")
    assert crema_ego.install() == {"started": True}
    assert [path.name for path in install.opened] == ["egoliteSetup.exe"]


@pytest.mark.parametrize("signer", ["", "CN=Someone Else, O=Someone Else\n"])
def test_install_refuses_an_installer_not_signed_by_citro_labs(monkeypatch, signer):
    install = _Install(monkeypatch, signer)
    with pytest.raises(ValueError, match="not signed by Citro Labs"):
        crema_ego.install()
    assert install.opened == []
