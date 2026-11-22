"""Crema browser actions and runtime adapters retained across the Hermes update."""

import json
import subprocess
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("success", [True, False])
def test_browser_type_masks_backend_echoes(monkeypatch, success):
    from agent.display import redact_tool_args_for_display
    from tools import browser_tool, browser_tool_session

    typed = "sk-proj-" + "x" * 48
    masked = redact_tool_args_for_display("browser_type", {"text": typed})["text"]
    assert masked != typed
    calls = []

    def fill(task_id, command, args):
        calls.append((task_id, command, args))
        return {
            "success": success,
            "error": f"Could not fill {typed}",
            "fallback_warning": f"Retried {typed}",
            "browser_engine_fallback": {"reason": f"First attempt: {typed}"},
        }

    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser_tool, "_blocked_private_page_action", lambda *_: None)
    monkeypatch.setattr(browser_tool, "_last_session_key", lambda key: key)
    monkeypatch.setattr(browser_tool_session, "_run_browser_command", fill)

    raw = browser_tool.browser_type("e1", typed, task_id="typing")
    assert calls == [(browser_tool._browser_task_key("typing"), "fill", ["@e1", typed])]
    assert typed not in raw
    result = json.loads(raw)
    assert result["success"] is success
    assert result["fallback_warning"] == f"Retried {masked}"
    assert result["browser_engine_fallback"] == {"reason": f"First attempt: {masked}"}
    if success:
        assert result["typed"] == masked
    else:
        assert result["error"] == f"Could not fill {masked}"


@pytest.mark.parametrize("parent_session, port", [(None, 41234), ("mine", None)])
def test_crema_bot_desktop_attaches_only_to_another_browser(monkeypatch, tmp_path, parent_session, port):
    import psutil
    from tools import browser_launcher, browser_tool_session
    from tools.bot_desktop import browser, runtime

    monkeypatch.setattr(runtime, "published_env", lambda: {"DISPLAY": ":37"})
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(
        browser_launcher, "running_instance",
        lambda user_data_dir: (41234, "/devtools/browser/dock", 999) if user_data_dir == str(tmp_path) else None,
    )
    env = {"AGENT_BROWSER_SESSION": parent_session} if parent_session else {}
    parent = SimpleNamespace(environ=lambda: env)
    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(parent=lambda: parent))
    session = {"session_name": "mine", "features": {"local": True}}
    assert browser_tool_session._bot_desktop_attach_port(session) == port


@pytest.mark.parametrize("allow", [True, False])
def test_crema_chromium_download_uses_lazy_install_policy(monkeypatch, allow):
    from tools import browser_tool, crema_browser_install

    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "0" if allow else "1")
    monkeypatch.setattr(browser_tool, "_chromium_autoinstall_attempted", False)
    monkeypatch.setattr(browser_tool, "_cached_chromium_installed", False)
    monkeypatch.setattr(crema_browser_install, "_running_in_docker", lambda: False)
    monkeypatch.setattr(crema_browser_install, "_find_agent_browser", lambda: browser_tool.NPX_AGENT_BROWSER_SENTINEL)
    monkeypatch.setattr(crema_browser_install, "_resolve_npx_bin", lambda: "/test/npx")
    monkeypatch.setattr(crema_browser_install, "_chromium_installed", lambda: True)
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(crema_browser_install.subprocess, "run", run)
    assert crema_browser_install._maybe_autoinstall_chromium() is allow
    assert commands == ([["/test/npx", "--ignore-scripts", "-y", browser_tool.AGENT_BROWSER_NPX_SPEC, "install"]]
                        if allow else [])
