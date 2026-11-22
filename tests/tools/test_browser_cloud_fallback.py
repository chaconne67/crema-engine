"""Tests for cloud browser provider runtime fallback to local Chromium.

Covers the fallback logic in ``browser_session.get_session_info()`` when a cloud provider
is configured but fails at runtime (issue #10883).
"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools.bot_desktop import placement
from tools import browser_launcher
from tools import browser_session as bs
from tools import browser_session_cloud as bs_cloud

_LOCAL_CDP = "ws://127.0.0.1:9333/devtools/browser/local"


@pytest.mark.platforms("posix")
def test_agent_browser_npx_uses_extended_path_before_system_path(tmp_path, monkeypatch):
    from tools import browser_tool, browser_tool_session, crema_browser_install

    engine, system = tmp_path / "engine", tmp_path / "system"
    for directory in (engine, system):
        directory.mkdir()
        npx = directory / "npx"
        npx.write_text("#!/bin/sh\nexit 0\n")
        npx.chmod(0o755)
    companion = engine / "npx-test-node"
    companion.write_text("#!/bin/sh\nexit 0\n")
    companion.chmod(0o755)
    (engine / "npx").write_text("#!/usr/bin/env npx-test-node\n")
    monkeypatch.setattr(crema_browser_install, "find_node_executable", lambda _name: None)
    monkeypatch.setattr(crema_browser_install, "_browser_candidate_path_dirs", lambda: [str(engine)])
    monkeypatch.setattr(browser_tool, "_agent_browser_resolved", False)
    monkeypatch.setenv("PATH", str(system))

    browser = crema_browser_install._find_agent_browser(validate=False)
    assert browser == browser_tool.NPX_AGENT_BROWSER_SENTINEL
    assert browser_tool_session._agent_browser_argv(browser)[0] == str(engine / "npx")

    (engine / "npx").write_text("#!/bin/sh\nexit 1\n")
    assert browser_tool_session._agent_browser_argv(browser)[0] == str(system / "npx")


def test_fenced_browser_toolset_and_task_identity_follow_the_active_profile(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from agent.secret_scope import reset_multiplex_context, set_multiplex_context
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from model_tools import get_tool_definitions
    from tools import browser_tool, browser_use_cli

    for profile, allow_private in (("a", "false"), ("b", "true")):
        home = tmp_path / profile
        home.mkdir()
        (home / "config.yaml").write_text(f"browser:\n  allow_private_urls: {allow_private}\n", encoding="utf-8")

    # Stand in for the two installed browser executables. The resolver, config and registry are real.
    monkeypatch.setattr(browser_tool, "check_browser_routed_requirements", lambda _action: True)
    monkeypatch.setattr(browser_use_cli, "_find_cli", lambda: ["browser-harness"])

    @contextmanager
    def scoped(profile):
        home = set_hermes_home_override(tmp_path / profile)
        multiplex = set_multiplex_context(True)
        try:
            yield
        finally:
            reset_multiplex_context(multiplex)
            reset_hermes_home_override(home)

    def selection(profile, *, raw=False):
        with scoped(profile):
            definitions = get_tool_definitions(["browser", "terminal"], quiet_mode=True, skip_tool_search_assembly=raw)
            names = {item["function"]["name"] for item in definitions}
            return names, browser_tool._browser_task_key("same-task")

    fenced, task_a = selection("a")
    other, task_b = selection("b")
    assert "browser_navigate" in fenced
    assert not fenced.intersection({"browser_exec", "browser_cdp"})
    assert not selection("a", raw=True)[0].intersection({"browser_exec", "browser_cdp"})
    assert "browser_navigate" not in other
    assert "browser_exec" in other
    assert task_a != task_b
    assert selection("a") == (fenced, task_a)


def _reset_session_state(monkeypatch):
    """Clear caches so each test starts fresh; the local browser is a stub launcher browser."""
    monkeypatch.setattr(bs, "_active_sessions", {})
    monkeypatch.setattr("tools.browser_session_lifecycle.start_cleanup_thread", lambda: None)
    monkeypatch.setattr("tools.browser_session_lifecycle.update_session_activity", lambda t: None)
    monkeypatch.setattr("tools.browser_session_cdp.ensure_cdp_supervisor", lambda task_id: None)
    monkeypatch.setattr("tools.bot_desktop.runtime.tool_placement", lambda: placement.GATEWAY)  # host browser
    monkeypatch.setattr(browser_launcher, "launch_chromium",
                        lambda key, **kw: (SimpleNamespace(cdp_url=_LOCAL_CDP, pid=4242), None))


class TestCloudProviderRuntimeFallback:
    """Tests for get_session_info cloud -> local fallback."""

    def test_cloud_failure_falls_back_to_local(self, monkeypatch):
        """When cloud provider.create_session raises, fall back to local."""
        _reset_session_state(monkeypatch)

        provider = Mock()
        provider.create_session.side_effect = RuntimeError("401 Unauthorized")
        monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: provider)
        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: None)

        session = bs.get_session_info("task-1")

        assert session["fallback_from_cloud"] is True
        assert "401 Unauthorized" in session["fallback_reason"]
        assert session["fallback_provider"] == "Mock"
        assert session["features"]["local"] is True
        assert session["cdp_url"] == _LOCAL_CDP


    def test_no_provider_uses_local_directly(self, monkeypatch):
        """When no cloud provider is configured, local mode is used with no fallback markers."""
        _reset_session_state(monkeypatch)

        monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: None)
        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: None)

        session = bs.get_session_info("task-4")

        assert session["features"]["local"] is True
        assert "fallback_from_cloud" not in session


    def test_cloud_returns_invalid_session_triggers_fallback(self, monkeypatch):
        """Cloud provider returning None or empty dict triggers fallback."""
        _reset_session_state(monkeypatch)

        provider = Mock()
        provider.create_session.return_value = None
        monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: provider)
        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: None)

        session = bs.get_session_info("task-7")

        assert session["fallback_from_cloud"] is True
        assert "invalid session" in session["fallback_reason"]
