"""Crema uses only the connections made in Crema: keys in its .env, sign-ins in its auth.json.

Nothing from the environment Crema was started with (keys exported for other tools, an upstream Hermes
install's settings), nothing from other programs' credential files, and no other program's CLI.
"""
import json
import os
import subprocess

import pytest

import crema_engine


@pytest.fixture
def environ_restored():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def test_inherited_keys_and_hermes_settings_are_dropped_and_crema_keys_load(environ_restored, tmp_path):
    from hermes_cli.env_loader import load_hermes_dotenv

    (tmp_path / ".env").write_text("OPENAI_API_KEY=crema-key\n", encoding="utf-8")
    inherited = {
        "OPENAI_API_KEY": "other-openai", "ANTHROPIC_API_KEY": "other-anthropic",
        "OPENROUTER_API_KEY": "other-openrouter", "GEMINI_API_KEY": "other-gemini", "XAI_API_KEY": "other-xai",
        "NOUS_API_KEY": "other-nous", "TAVILY_API_KEY": "other-tavily",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-other", "ANTHROPIC_TOKEN": "sk-ant-oat01-other",
        "COPILOT_GITHUB_TOKEN": "gho_other", "GH_TOKEN": "gho_other", "GITHUB_TOKEN": "gho_other",
        "HERMES_ENABLE_PROJECT_PLUGINS": "1", "HERMES_GIT_BASH_PATH": "C:/other/bash.exe",
        "HERMES_BUNDLED_SKILLS": "/other/skills", "HERMES_COPILOT_ACP_COMMAND": "copilot", "TERMINAL_ENV": "docker",
    }
    kept = {
        "HERMES_HOME": str(tmp_path), "HERMES_DASHBOARD_SESSION_TOKEN": "t" * 32, "HERMES_FTS5_CJK_SO": "cjk.dll",
        "CREMA_EMBED_MODEL": "model", "PATH": os.environ.get("PATH", ""), "HTTPS_PROXY": "http://proxy:8080",
    }
    os.environ.update({**inherited, **kept})

    crema_engine.use_only_crema_connections()
    load_hermes_dotenv(hermes_home=tmp_path)

    assert os.environ["OPENAI_API_KEY"] == "crema-key"
    assert os.environ["TERMINAL_ENV"] == "local"  # Crema's own terminal setting, bridged from its config
    assert not (set(inherited) - {"OPENAI_API_KEY", "TERMINAL_ENV"}) & set(os.environ)
    assert {name: os.environ.get(name) for name in kept} == kept


def test_adopting_external_logins_fails_closed(monkeypatch):
    from agent.credential_sources import adopt_external_logins_enabled

    def broken():
        raise ValueError("config.yaml does not parse")

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", broken)
    assert adopt_external_logins_enabled() is False
    for config in ({}, {"auth": "yes"}, {"auth": {}}):
        monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda config=config: config)
        assert adopt_external_logins_enabled() is False
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"auth": {"adopt_external_logins": True}})
    assert adopt_external_logins_enabled() is True


def _no_spawn(monkeypatch):
    def spawn(*args, **kwargs):
        raise AssertionError(f"spawned {args[0] if args else kwargs.get('args')}")

    monkeypatch.setattr(subprocess, "run", spawn)
    monkeypatch.setattr(subprocess, "Popen", spawn)


def test_copilot_cli_login_and_cli_are_not_used(monkeypatch, tmp_path):
    from hermes_cli import auth, models

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".copilot").mkdir()
    (tmp_path / ".copilot" / "config.json").write_text(json.dumps({"copilotTokens": {"github.com": "gho_cli"}}))
    (tmp_path / ".config" / "github-copilot").mkdir(parents=True)
    (tmp_path / ".config" / "github-copilot" / "hosts.json").write_text(json.dumps({"github.com": {"oauth_token": "gho_x"}}))
    monkeypatch.setattr("shutil.which", lambda name, *a, **k: f"/usr/bin/{name}")
    _no_spawn(monkeypatch)

    status = auth.get_external_process_provider_status("copilot-acp")
    assert not status.get("configured") and not status.get("logged_in") and not status.get("auth_verified")
    with pytest.raises(auth.AuthError):
        auth.resolve_external_process_provider_credentials("copilot-acp")
    assert models._copilot_cli_config_tokens() == []
    from providers import get_provider_profile

    assert get_provider_profile("copilot-acp").fetch_models() is None


def test_qwen_cli_login_is_not_read_or_rewritten(monkeypatch, tmp_path):
    from hermes_cli import auth

    monkeypatch.setenv("HOME", str(tmp_path))
    creds = tmp_path / ".qwen" / "oauth_creds.json"
    creds.parent.mkdir()
    creds.write_text(json.dumps({"access_token": "qwen-cli", "refresh_token": "r", "expiry_date": 0}))
    before = creds.read_bytes()
    monkeypatch.setattr("hermes_cli.auth_constants.httpx.post", lambda *a, **k: pytest.fail("refreshed the Qwen CLI login"))

    with pytest.raises(auth.AuthError):
        auth.resolve_qwen_runtime_credentials()
    assert auth.get_qwen_auth_status()["logged_in"] is False
    assert creds.read_bytes() == before


def test_claude_oauth_requests_never_run_claude(monkeypatch):
    import agent.anthropic_adapter as adapter

    class SDK:  # stands in for the anthropic package: the client gets the headers it would be built with
        Omit = object

        @staticmethod
        def Anthropic(**kwargs):
            return kwargs

    _no_spawn(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda name, *a, **k: pytest.fail(f"looked for {name}"))
    monkeypatch.setattr(adapter, "_get_anthropic_sdk", lambda: SDK)

    client = adapter.build_anthropic_client("sk-ant-oat01-crema", "https://api.anthropic.com")

    assert client["default_headers"]["user-agent"] == f"claude-code/{adapter._CLAUDE_CODE_VERSION} (external, cli)"
