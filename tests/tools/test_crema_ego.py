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


@pytest.fixture(autouse=True)
def config(monkeypatch):
    """The engine's config.yaml, kept in memory."""
    from hermes_cli import config as hermes_config

    saved = {"skills": {"disabled": ["kept-off"]}}
    monkeypatch.setattr(hermes_config, "load_config", lambda: json.loads(json.dumps(saved)))
    monkeypatch.setattr(hermes_config, "save_config", lambda value: saved.update(json.loads(json.dumps(value))))
    return saved


def test_off_hides_the_skill_and_on_shows_it_again(monkeypatch, config):
    monkeypatch.setattr(crema_ego, "cli", lambda: "ego-browser")
    assert crema_ego.enable(False) == {"installed": True, "enabled": False, "progress": None}
    assert config["skills"]["disabled"] == ["kept-off", "ego-browser"]
    assert crema_ego.enable(False)["enabled"] is False  # off twice: still listed once
    assert config["skills"]["disabled"] == ["kept-off", "ego-browser"]
    assert crema_ego.enable(True) == {"installed": True, "enabled": True, "progress": None}
    # A skill the user turned off stays off.
    assert config["skills"]["disabled"] == ["kept-off"]


def _command(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    command = folder / ("ego-browser.exe" if os.name == "nt" else "ego-browser")
    command.write_text("")
    command.chmod(0o755)
    return command


def test_not_installed(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(crema_ego, "_user_path", lambda: "")
    assert crema_ego.state() == {"installed": False, "enabled": True, "progress": None}


def test_found_in_the_users_new_path_joins_the_engines(monkeypatch, tmp_path):
    # Installed after Crema started: only the user's PATH in Windows has it, and the agent's terminal needs it too.
    command = _command(tmp_path / "ego")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(crema_ego, "_user_path", lambda: str(command.parent))
    assert crema_ego.state() == {"installed": True, "enabled": True, "progress": None}
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
        self.closed = 0  # times ego lite's windows were closed
        self.seen = []  # where the install said it was, at each download report and each command it ran
        monkeypatch.setattr(crema_ego, "_home", lambda: self.home)
        monkeypatch.setattr(crema_ego, "cli", lambda: "ego-browser" if self.installed else None)

        def fetch(url, path, report):
            for blocks in (0, 3, 8):  # a 64 KB file in 8 KB blocks: 0%, 37%, 100%
                report(blocks, 8192, 65536)
                self.seen.append(crema_ego.state()["progress"])
            Path(path).write_bytes(b"MZ")

        monkeypatch.setattr(crema_ego.urllib.request, "urlretrieve", fetch)
        self.replies = {"list": listed, "import": imported or {"success": True, "new_profile_dirs": []}}

        def run(argv, **kw):
            self.seen.append(crema_ego.state()["progress"])
            if isinstance(argv, str):  # the installer
                self.ran.append(argv.split('" ', 1)[1])
                folder = self.home / "Application" / "0.6.0.16"
                folder.mkdir(parents=True)
                (folder / "ego-browser.exe").write_text("")
                return subprocess.CompletedProcess(argv, 0)
            if argv[0] == "powershell":
                self.closed += argv[-1] == crema_ego.CLOSE
                return subprocess.CompletedProcess(argv, 0, stdout=signer, stderr="")
            self.ran.append(argv[1:])
            self.installed = True  # its first start puts ego-browser on the user's PATH
            reply = self.replies["list" if argv[1:] == ["import", "list"] else "import"]
            kw["stdout"].write((reply if isinstance(reply, str) else json.dumps(reply)).encode())
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(crema_ego.subprocess, "run", run)


def test_install_sets_ego_lite_up_as_crema_chooses(monkeypatch, tmp_path, config):
    install = _Install(monkeypatch, tmp_path)
    config["skills"]["disabled"].append("ego-browser")  # turned off before: 설치하고 켜기 turns it on
    assert crema_ego.install() == {"installed": True, "enabled": True, "progress": None}
    assert install.ran[0] == f'--install="{crema_ego.TAG}" --silent'
    assert "needsadmin=False" in crema_ego.TAG
    # Its welcome screens skipped; the default browser's default profile brought along in place of the empty one;
    # ego lite not made the default browser.
    assert json.loads((install.home / "User Data" / "ego_config.json").read_text()) == {"not_first_run": True}
    assert install.ran[1:] == [["import", "list"], ["import", "--browser", "chrome", "--profile", "Default",
                                                    "--clear-existing-profiles", "--no-default"]]
    # The welcome page and the tabs brought along do not stay on the user's screen.
    assert install.closed == 2  # after the first start, and after ego lite starts again with the tabs


def test_bringing_the_sign_ins_along_failed_install_again_finishes_it(monkeypatch, tmp_path):
    install = _Install(monkeypatch, tmp_path, imported={"success": False, "result_code": 3})
    with pytest.raises(OSError, match="could not bring chrome"):
        crema_ego.install()
    # ego lite is there, but not as Crema sets it up: 설치 stays.
    assert crema_ego.state() == {"installed": False, "enabled": True, "progress": None}
    install.ran.clear()
    install.replies["import"] = {"success": True, "new_profile_dirs": []}
    assert crema_ego.install() == {"installed": True, "enabled": True, "progress": None}
    assert install.ran == [["import", "list"], ["import", "--browser", "chrome", "--profile", "Default",
                                                "--clear-existing-profiles", "--no-default"]]


def test_install_leaves_an_earlier_setup_as_it_is(monkeypatch, tmp_path):
    # Set up before (removed from Windows, its data kept): its profiles and sign-ins are the user's.
    install = _Install(monkeypatch, tmp_path)
    config = install.home / "User Data" / "ego_config.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"not_first_run": true, "kept": 1}')
    assert crema_ego.install() == {"installed": True, "enabled": True, "progress": None}
    assert install.ran[1:] == [["import", "list"]]
    assert config.read_text() == '{"not_first_run": true, "kept": 1}'
    assert install.closed == 0


def test_install_with_no_browser_to_bring_along(monkeypatch, tmp_path):
    install = _Install(monkeypatch, tmp_path, listed={"browser_sources": [], "success": True})
    assert crema_ego.install() == {"installed": True, "enabled": True, "progress": None}
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


def test_install_says_where_it_is_until_it_ends(monkeypatch, tmp_path, config):
    """The 웹 브라우저 조작 card shows each step: the download's percent, the signature check, the installer, the
    sign-ins brought along; nothing once it has ended."""
    install = _Install(monkeypatch, tmp_path)
    crema_ego.install()
    steps = [dict(seen) for seen in install.seen if seen]
    assert [(seen["step"], seen["percent"]) for seen in steps[:3]] == [("download", 0), ("download", 37), ("download", 100)]
    assert [seen["step"] for seen in steps[3:]] == ["check", "install", "install", "import", "import", "import"]
    assert crema_ego.state()["progress"] is None


def test_a_failed_install_leaves_no_step_behind(monkeypatch, tmp_path):
    _Install(monkeypatch, tmp_path, signer="")
    with pytest.raises(ValueError):
        crema_ego.install()
    assert crema_ego.state()["progress"] is None


class _Browser:
    def __init__(self, monkeypatch, tmp_path):
        self.spaces = {}
        self.calls = []
        self.window_calls = []
        self.visible = []
        self.launched = 0
        self.window = {"hwnd": 33, "pid": 44, "created": "100", "title": "owned", "visible": True, "marker": "Crema.Ego.test"}
        monkeypatch.setattr(crema_ego, "_work_path", lambda session: tmp_path / (session + ".json") if session else
                            (_ for _ in ()).throw(ValueError("conversation required")))
        monkeypatch.setattr(crema_ego, "cli", lambda: "ego-browser")
        monkeypatch.setattr(crema_ego, "_launch_hidden", self.launch)
        monkeypatch.setattr(crema_ego, "_sdk", self.sdk)
        monkeypatch.setattr(crema_ego, "_windows", self.windows)

    def launch(self):
        self.launched += 1

    def windows(self, action, **kw):
        self.window_calls.append((action, kw))
        if action == "inspect":
            return {"roots": [] if not self.launched else [44], "windows": self.visible}
        if action == "present":
            assert kw["space"] == self.spaces[1]["name"]
            assert kw["page_title"] == "Login fixture"
            return {"window": self.window, "normal_gui": True}
        if action in {"show", "hide"}:
            assert kw["window"]["hwnd"] == 33
            return {"window": self.window, "hidden": action == "hide"}
        raise AssertionError(action)

    def sdk(self, script, **_):
        self.calls.append(script)
        if "listTaskSpaces" in script and ".handOff()" not in script:
            return {"spaces": list(self.spaces.values())}
        if "const t=await taskSpace(\"Crema " in script:
            name = json.loads(script.split("taskSpace(", 1)[1].split(");", 1)[0])
            self.spaces[1] = {"id": 1, "name": name, "createdBy": "agent", "ownership": "agent"}
            return {"id": 1}
        if "p.title()" in script:
            return {"title": "Login fixture", "targetId": "target-one"}
        if ".handOff()" in script:
            self.spaces[1]["ownership"] = "agentDelegatedToUser"
            return {"handedOff": True}
        if "takeOverTaskSpace" in script:
            self.spaces[1]["ownership"] = "agent"
            return {"id": 1}
        if ".finish(" in script:
            del self.spaces[1]
            return {"finished": True}
        raise AssertionError(script)


def test_browser_starts_hidden_once_and_preserves_user_windows(monkeypatch, tmp_path):
    fake = _Browser(monkeypatch, tmp_path)
    fake.visible = [{"hwnd": 7, "visible": True}]  # another window stays exactly as it was
    first = crema_ego.browser("start", "chat-a")
    assert first["space_id"] == 1 and first["status"] == "working"
    assert crema_ego.browser("start", "chat-a") == first
    assert fake.launched == 1
    assert [action for action, _ in fake.window_calls] == ["inspect", "inspect"]
    assert not any(".handOff()" in call for call in fake.calls)


def test_browser_handoff_resume_same_space_and_normal_window(monkeypatch, tmp_path):
    fake = _Browser(monkeypatch, tmp_path)
    first = crema_ego.browser("start", "chat-a")
    shown = crema_ego.browser("handoff", "chat-a", page="p1")
    assert shown == {**first, "status": "waiting_for_user"}
    assert fake.spaces[1]["ownership"] == "agentDelegatedToUser"
    assert crema_ego.browser("resume", "chat-a", confirmed=True) == first
    assert fake.spaces[1]["ownership"] == "agent"
    assert [a for a, _ in fake.window_calls][-2:] == ["present", "hide"]
    crema_ego.browser("handoff", "chat-a", page="p1")
    assert [a for a, _ in fake.window_calls][-1] == "show"
    assert sum(a == "present" for a, _ in fake.window_calls) == 1
    crema_ego.browser("resume", "chat-a", confirmed=True)
    assert crema_ego.browser("finish", "chat-a") == {"status": "finished", "space_id": 1}
    assert not crema_ego._work_path("chat-a").exists()


def test_browser_waits_for_human_and_does_not_finish_their_window(monkeypatch, tmp_path):
    fake = _Browser(monkeypatch, tmp_path)
    crema_ego.browser("start", "chat-a")
    crema_ego.browser("handoff", "chat-a", page="p1")
    calls = len(fake.window_calls)
    with pytest.raises(ValueError, match="confirm"):
        crema_ego.browser("resume", "chat-a")
    with pytest.raises(ValueError, match="still has"):
        crema_ego.browser("finish", "chat-a")
    with pytest.raises(ValueError, match="with the user"):
        crema_ego.browser("start", "chat-a")
    assert len(fake.window_calls) == calls
    assert fake.spaces[1]["ownership"] == "agentDelegatedToUser"


def test_browser_does_not_claim_another_chat_or_replaced_space(monkeypatch, tmp_path):
    fake = _Browser(monkeypatch, tmp_path)
    crema_ego.browser("start", "chat-a")
    with pytest.raises(ValueError, match="No browser work"):
        crema_ego.browser("handoff", "chat-b", page="p1")
    fake.spaces[1]["name"] = "someone else's Space"
    before = len(fake.window_calls)
    with pytest.raises(ValueError, match="no longer exists"):
        crema_ego.browser("handoff", "chat-a", page="p1")
    assert len(fake.window_calls) == before


def test_failed_gui_selection_does_not_hand_control_to_user(monkeypatch, tmp_path):
    fake = _Browser(monkeypatch, tmp_path)
    crema_ego.browser("start", "chat-a")
    original = fake.windows
    def windows(action, **kw):
        if action == "present":
            raise OSError("Space picker unavailable")
        return original(action, **kw)
    monkeypatch.setattr(crema_ego, "_windows", windows)
    with pytest.raises(OSError, match="picker"):
        crema_ego.browser("handoff", "chat-a", page="p1")
    assert fake.spaces[1]["ownership"] == "agent"
    assert crema_ego._load_work("chat-a")["window"] is None


def test_failed_takeover_restores_the_users_normal_window(monkeypatch, tmp_path):
    fake = _Browser(monkeypatch, tmp_path)
    crema_ego.browser("start", "chat-a")
    crema_ego.browser("handoff", "chat-a", page="p1")
    original = fake.sdk
    def sdk(script, **_):
        if "takeOverTaskSpace" in script:
            raise OSError("Ego refused takeover")
        return original(script)
    monkeypatch.setattr(crema_ego, "_sdk", sdk)
    with pytest.raises(OSError, match="refused"):
        crema_ego.browser("resume", "chat-a", confirmed=True)
    assert [action for action, _ in fake.window_calls][-2:] == ["hide", "show"]
    assert crema_ego._load_work("chat-a")["phase"] == "waiting_for_user"


def test_start_cannot_ignore_disabled_browser(monkeypatch, tmp_path, config):
    fake = _Browser(monkeypatch, tmp_path)
    config["skills"]["disabled"].append("ego-browser")
    with pytest.raises(ValueError, match="Enable"):
        crema_ego.browser("start", "chat-a")
    assert fake.calls == [] and fake.launched == 0


def test_handoff_requires_existing_page_and_verified_gui(monkeypatch, tmp_path):
    fake = _Browser(monkeypatch, tmp_path)
    crema_ego.browser("start", "chat-a")
    with pytest.raises(ValueError, match="page label"):
        crema_ego.browser("handoff", "chat-a")
    original = fake.windows
    monkeypatch.setattr(crema_ego, "_windows", lambda action, **kw: {"window": fake.window} if action == "present"
                        else original(action, **kw))
    with pytest.raises(OSError, match="normal GUI"):
        crema_ego.browser("handoff", "chat-a", page="p1")
    assert fake.spaces[1]["ownership"] == "agent"


def test_browser_tool_rejects_a_missing_session(monkeypatch, tmp_path):
    _Browser(monkeypatch, tmp_path)
    result = json.loads(crema_ego.crema_ego({"action": "start"}))
    assert "conversation required" in str(result)


def test_crema_platform_exposes_the_single_browser_lifecycle():
    from toolsets import TOOLSETS
    assert "crema_ego" in TOOLSETS["hermes-api-server"]["tools"]


def test_unverified_start_cannot_become_success_on_retry(monkeypatch, tmp_path):
    fake = _Browser(monkeypatch, tmp_path)
    original = fake.windows
    inspected = 0
    def windows(action, **kw):
        nonlocal inspected
        if action == "inspect":
            inspected += 1
            if inspected == 2:
                fake.visible = [{"hwnd": 777, "visible": True}]
        return original(action, **kw)
    monkeypatch.setattr(crema_ego, "_windows", windows)
    with pytest.raises(OSError, match="became visible"):
        crema_ego.browser("start", "chat-a")
    with pytest.raises(OSError, match="not verified"):
        crema_ego.browser("start", "chat-a")
    assert crema_ego._load_work("chat-a")["phase"] == "blocked"
    with pytest.raises(ValueError, match="working agent-owned"):
        crema_ego.browser("handoff", "chat-a", page="p1")
    crema_ego.browser("finish", "chat-a")
    assert not crema_ego._work_path("chat-a").exists()


def test_replaced_window_cannot_take_control_back_from_human(monkeypatch, tmp_path):
    fake = _Browser(monkeypatch, tmp_path)
    crema_ego.browser("start", "chat-a")
    crema_ego.browser("handoff", "chat-a", page="p1")
    original = fake.windows
    def windows(action, **kw):
        if action == "hide":
            raise OSError("Ego window ownership changed")
        return original(action, **kw)
    monkeypatch.setattr(crema_ego, "_windows", windows)
    with pytest.raises(OSError, match="ownership changed"):
        crema_ego.browser("resume", "chat-a", confirmed=True)
    assert fake.spaces[1]["ownership"] == "agentDelegatedToUser"
