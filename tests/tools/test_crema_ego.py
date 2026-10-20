"""Crema ego lite (tools/crema_ego.py): finding its ego-browser command; installing it with no screens and setting it
up as Crema chooses."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from tools import crema_ego


@pytest.fixture(autouse=True)
def _pending(monkeypatch, tmp_path):
    monkeypatch.setattr(crema_ego, "_pending", lambda: tmp_path / "ego-lite-import-pending")


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


LIST = {"browser_sources": [
    {"browser_source": "edge", "is_default_browser": False, "profiles": [{"dir_name": "Default", "is_default": True}]},
    {"browser_source": "chrome", "is_default_browser": True, "profiles": [
        {"dir_name": "Profile 4", "is_default": False}, {"dir_name": "Default", "is_default": True}]},
], "success": True}


class _Install:
    """ego lite's installer and ego-browser as the engine meets them; the installer puts ego-browser in place."""

    def __init__(self, monkeypatch, tmp_path, signer="CN=CITRO LABS PTE. LIMITED, O=CITRO LABS PTE. LIMITED, C=SG\n",
                 listed=LIST, imported=None):
        self.home = tmp_path / "ego lite"
        self.ran = []  # the installer's command line, then each ego-browser call's arguments
        self.installed = False
        monkeypatch.setattr(crema_ego, "_home", lambda: self.home)
        monkeypatch.setattr(crema_ego, "cli", lambda: "ego-browser" if self.installed else None)
        monkeypatch.setattr(crema_ego.urllib.request, "urlretrieve", lambda url, path: Path(path).write_bytes(b"MZ"))
        self.replies = {"list": listed, "import": imported or {"success": True, "new_profile_dirs": []}}

        def run(argv, **kw):
            if isinstance(argv, str):  # the installer
                self.ran.append(argv.split('" ', 1)[1])
                folder = self.home / "Application" / "0.6.0.16"
                folder.mkdir(parents=True)
                (folder / "ego-browser.exe").write_text("")
                return subprocess.CompletedProcess(argv, 0)
            if argv[0] == "powershell":
                return subprocess.CompletedProcess(argv, 0, stdout=signer, stderr="")
            self.ran.append(argv[1:])
            self.installed = True  # its first start puts ego-browser on the user's PATH
            reply = self.replies["list" if argv[1:] == ["import", "list"] else "import"]
            kw["stdout"].write((reply if isinstance(reply, str) else json.dumps(reply)).encode())
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(crema_ego.subprocess, "run", run)


def test_install_sets_ego_lite_up_as_crema_chooses(monkeypatch, tmp_path):
    install = _Install(monkeypatch, tmp_path)
    assert crema_ego.install() == {"installed": True}
    assert install.ran[0] == f'--install="{crema_ego.TAG}" --silent'
    assert "needsadmin=False" in crema_ego.TAG
    # Its welcome screens skipped; the default browser's default profile brought along in place of the empty one;
    # ego lite not made the default browser.
    assert json.loads((install.home / "User Data" / "ego_config.json").read_text()) == {"not_first_run": True}
    assert install.ran[1:] == [["import", "list"], ["import", "--browser", "chrome", "--profile", "Default",
                                                    "--clear-existing-profiles", "--no-default"]]


def test_bringing_the_sign_ins_along_failed_install_again_finishes_it(monkeypatch, tmp_path):
    install = _Install(monkeypatch, tmp_path, imported={"success": False, "result_code": 3})
    with pytest.raises(OSError, match="could not bring chrome"):
        crema_ego.install()
    # ego lite is there, but not as Crema sets it up: 설치 stays.
    assert crema_ego.state() == {"installed": False}
    install.ran.clear()
    install.replies["import"] = {"success": True, "new_profile_dirs": []}
    assert crema_ego.install() == {"installed": True}
    assert install.ran == [["import", "list"], ["import", "--browser", "chrome", "--profile", "Default",
                                                "--clear-existing-profiles", "--no-default"]]


def test_install_leaves_an_earlier_setup_as_it_is(monkeypatch, tmp_path):
    # Set up before (removed from Windows, its data kept): its profiles and sign-ins are the user's.
    install = _Install(monkeypatch, tmp_path)
    config = install.home / "User Data" / "ego_config.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"not_first_run": true, "kept": 1}')
    assert crema_ego.install() == {"installed": True}
    assert install.ran[1:] == [["import", "list"]]
    assert config.read_text() == '{"not_first_run": true, "kept": 1}'


def test_install_with_no_browser_to_bring_along(monkeypatch, tmp_path):
    install = _Install(monkeypatch, tmp_path, listed={"browser_sources": [], "success": True})
    assert crema_ego.install() == {"installed": True}
    assert install.ran[1:] == [["import", "list"]]


@pytest.mark.parametrize("listed, imported", [
    ("Please complete the onboarding process first.", None),
    (LIST, {"success": False, "result_code": 3}),
])
def test_install_says_when_ego_lite_does_not_go_along(monkeypatch, tmp_path, listed, imported):
    _Install(monkeypatch, tmp_path, listed=listed, imported=imported)
    with pytest.raises(OSError, match="onboarding|could not bring chrome"):
        crema_ego.install()


@pytest.mark.parametrize("signer", ["", "CN=Someone Else, O=Someone Else\n"])
def test_install_refuses_an_installer_not_signed_by_citro_labs(monkeypatch, tmp_path, signer):
    install = _Install(monkeypatch, tmp_path, signer=signer)
    with pytest.raises(ValueError, match="not signed by Citro Labs"):
        crema_ego.install()
    assert install.ran == []
