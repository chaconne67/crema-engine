"""Run with CREMA_APP_SOURCE pointing at the app checkout and its recorded engine snapshots."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import shutil
import socket
import threading
from pathlib import Path

import pytest

from crema_startup import hook_id, initialize

ENGINE = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(("provider", "mode"), [
    ("openai-codex", "codex_responses"),
    ("openrouter", "chat_completions"),
    ("custom:crema", "chat_completions"),
])
def test_change_review_uses_the_active_chat_connection(monkeypatch, provider, mode):
    from types import SimpleNamespace
    from agent import auxiliary_client as aux
    from crema_change_principles.scripts import reviewer

    monkeypatch.setitem(__import__("sys").modules, "reviewer", reviewer)
    from crema_change_principles.scripts import crema_gate

    key, base, model = "fixture-active-key", "https://chat.invalid/v1", "chat-model"
    opened = []

    def make_client(**kwargs):
        opened.append(kwargs)
        return SimpleNamespace(**kwargs)

    def request(client, kwargs, **_):
        assert kwargs["model"] == model
        raw = getattr(client, "_real_client", client)
        assert (raw.api_key, raw.base_url) == (key, base)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])

    monkeypatch.setattr(aux, "_create_openai_client", make_client)
    monkeypatch.setattr(aux, "_create_with_progress_once", request)
    monkeypatch.setattr("hermes_cli.runtime_provider._get_named_custom_provider", lambda name:
                        {"base_url": "https://other.invalid/v1", "api_key": "other"} if name == "custom:crema" else None)
    runtime = {"api_key": key, "base_url": base, "api_mode": mode}
    settings = {"provider": provider, "reasoning": None}
    assert crema_gate.ask(runtime, "crema", model, settings, "판정", "답", {}, 5) == "{}"
    assert len(opened) == 1

    def fail(*args, **kwargs):
        raise RuntimeError(key)

    monkeypatch.setattr(aux, "_create_with_progress_once", fail)
    with pytest.raises(RuntimeError, match="연결된 모델에서 판정 결과를 받지 못했습니다") as error:
        crema_gate.ask(runtime, "crema", model, settings, "판정", "답", {}, 5)
    assert key not in str(error.value)
    assert len(opened) == 2


def test_change_principles_reuses_plan_and_commit_gate(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import sys
    import crema_engine

    config = {"crema_theme": "krema-developer", "terminal": {"cwd": str(tmp_path)}}
    active = {"allowed": True}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    monkeypatch.setattr("crema_startup.engine_input", lambda _: {
        "brain": "server", "hookOptions": {"changePrinciples": {"default": False}}})
    monkeypatch.setitem(sys.modules, "engine_home", SimpleNamespace(profile_has=lambda *_: active["allowed"]))
    events = []
    decisions = iter([("hold", "plan missing"), None, ("hold", "commit check"), None, ("notice", "read only")])
    monkeypatch.setattr(crema_engine, "_change_principles_decision",
                        lambda event: events.append(event) or next(decisions))
    call = crema_engine.change_principles_hook
    assert call("write_file", args={"path": "product.py"}, session_id="own") is None
    assert events == []
    config["crema_change_principles"] = {"enabled": True}
    assert call("write_file", args={"path": "product.py"}, session_id="own") == {
        "action": "block", "message": "plan missing"}
    assert call("write_file", args={"path": "product.py"}, session_id="own") is None
    assert call("terminal", args={"command": "git commit"}, session_id="own") == {
        "action": "block", "message": "commit check"}
    assert call("terminal", args={"command": "git commit"}, session_id="own") is None
    assert call("patch", args={"path": "product.py"}, session_id="own") == {
        "action": "context", "message": "read only"}
    assert events[0]["cwd"] == str(tmp_path) and events[0]["session_id"] == "own"
    assert events[0]["transcript_path"] is None and events[2]["tool_input"] == {"command": "git commit"}
    active["allowed"] = False
    assert call("write_file", args={"path": "product.py"}, session_id="own") is None
    assert len(events) == 5


@pytest.fixture
def app(monkeypatch):
    source = os.environ.get("CREMA_APP_SOURCE")
    if not source:
        pytest.skip("CREMA_APP_SOURCE is required for the four application theme snapshots")
    root = Path(source)
    monkeypatch.syspath_prepend(str(root / "services/_server"))
    # The pinned probe has an empty OS home, hence no operator's Playwright cache.
    monkeypatch.setattr("tools.browser_tool_install._chromium_search_roots", lambda: [])
    monkeypatch.setattr("tools.browser_tool._cached_chromium_installed", None)

    def offline(*_args, **_kwargs):
        raise RuntimeError("the theme snapshot is offline")

    monkeypatch.setattr(socket.socket, "connect", offline)
    monkeypatch.setattr(socket, "getaddrinfo", offline)
    monkeypatch.setattr("hermes_cli.auth_codex._import_codex_cli_tokens", lambda: None)
    return root


def setup_input(app, tmp_path, deployment, theme, monkeypatch):
    from theme_spec import build_input, load_catalog

    data = build_input(load_catalog(app, engine_dir=ENGINE), deployment)
    startup = data["engineInputs"][theme]
    path = tmp_path / "build-input.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    bundled = tmp_path / "skills"
    for skill, source in data["skillSources"][theme].items():
        root = ENGINE if source["root"] == "engine" else app
        shutil.copytree((root / source["path"]).parent, bundled / skill)
    monkeypatch.setenv("CREMA_ENGINE_INPUT", str(path))
    monkeypatch.setenv("HERMES_BUNDLED_SKILLS", str(bundled))
    expected = json.loads((app / f"docs/theme-baseline/{theme}.json").read_text(encoding="utf-8"))["engine"]
    return startup, expected


def tool_snapshot(config, surface):
    from agent.skill_utils import parse_config_string_list
    from cron.scheduler import _resolve_cron_disabled_toolsets, _resolve_cron_enabled_toolsets
    from hermes_cli.tools_config import _get_platform_tools
    from model_tools import _select_tool_names, get_tool_definitions

    if surface == "cron":
        enabled = sorted(_resolve_cron_enabled_toolsets({}, config))
        disabled = sorted(_resolve_cron_disabled_toolsets(config))
    else:
        enabled = sorted(_get_platform_tools(config, surface))
        disabled = sorted(parse_config_string_list(config.get("agent", {}).get("disabled_toolsets")))
    selected = sorted(_select_tool_names(enabled, disabled, quiet_mode=True))
    before, after = [
        sorted(tool["function"]["name"] for tool in get_tool_definitions(
            enabled, disabled, quiet_mode=True, skip_tool_search_assembly=skip))
        for skip in (True, False)
    ]
    return {"enabledToolsets": enabled, "disabledToolsets": disabled, "selected": selected,
            "schemasBeforeToolSearchWithoutCredentials": before, "schemasAfterToolSearchWithoutCredentials": after,
            "selectedWithoutSchema": sorted(set(selected) - set(before))}


def check_tools(config, surface, expected):
    for field, observed in tool_snapshot(config, surface).items():
        wanted = expected[field]
        assert observed == wanted, (field, sorted(set(observed) - set(wanted)), sorted(set(wanted) - set(observed)))


def check_registration(home, startup, expected):
    from hermes_cli.plugins import get_plugin_manager
    from tools.skills_sync import _dir_hash, _iter_active_skill_mds

    manager = get_plugin_manager()
    plugins = {name: {"enabled": entry.enabled, "loaded": entry.module is not None, "deferred": entry.deferred}
               for name, entry in manager._plugins.items()}
    assert plugins == expected["plugins"]
    wanted = {event: list(callbacks) for event, callbacks in startup["hooks"].items()}
    diagnostics = startup["hookOptions"].get("profileHooks")
    if diagnostics:
        wanted["on_session_end"][:0] = diagnostics["on_session_end"]
        wanted["post_tool_call"][1:1] = diagnostics["post_tool_call"]
        wanted["api_request_error"] = diagnostics["api_request_error"]
        wanted["post_approval_response"] = diagnostics["post_approval_response"]
        # The browser notice and storage cleanup compose within its one replacement callback.
        assert len(manager._hooks["transform_llm_output"]) == 1
        emitted = manager._hooks["transform_llm_output"][0]
        assert emitted(response_text="Reply\n[[NEXT_INPUT: x]]", turn_id="snapshot") == "Reply"
    if "changePrinciples" in startup["hookOptions"]:
        wanted["pre_tool_call"][:0] = ["engine/crema_engine.py:change_principles_hook"]
        wanted["pre_llm_call"][:0] = ["engine/crema_engine.py:change_principles_start"]
    assert {event: [hook_id(callback, home) for callback in callbacks]
            for event, callbacks in manager._hooks.items()} == wanted
    assert [{"path": path.relative_to(home / "skills").as_posix(), "bundleSha256": _dir_hash(path.parent)}
            for path in _iter_active_skill_mds(sort=True)] == expected["skills"]


def test_desktop_launch_preserves_tools_and_hooks(app, tmp_path, monkeypatch):
    import crema_engine
    from gateway.platforms.api_server import APIServerAdapter
    from hermes_cli.config import load_config
    from tools import crema_failures

    home = Path(os.environ["HERMES_HOME"])
    startup, expected = setup_input(app, tmp_path, "crema-engine", "crema", monkeypatch)
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "offline-desktop-fixture")
    driver = tmp_path / "cua_driver/bin/cua-driver.exe"
    driver.parent.mkdir(parents=True)
    (driver.parent.parent / "__init__.py").touch()
    driver.touch(mode=0o700)
    monkeypatch.syspath_prepend(str(tmp_path))

    class Started(Exception):
        pass

    async def connect(api):
        return True

    start = threading.Thread.start

    def stop_at_ticker(thread):
        if thread.name == "crema-cron-ticker":
            raise Started
        return start(thread)

    monkeypatch.setattr(APIServerAdapter, "connect", connect)
    monkeypatch.setattr(crema_engine, "free_port", lambda: 8650)
    monkeypatch.setattr(threading.Thread, "start", stop_at_ticker)
    monkeypatch.setattr(asyncio.BaseEventLoop, "run_in_executor", lambda loop, *_: loop.create_future())
    root = logging.getLogger()
    try:
        with pytest.raises(Started):
            asyncio.run(crema_engine.main())
        initialize()
        check_registration(home, startup, expected)
        assert sum(isinstance(handler, crema_failures._Handler) for handler in root.handlers) == 1
        config = load_config()
        expected["chat"]["enabledToolsets"] = sorted([*expected["chat"]["enabledToolsets"], "screens"])
        check_tools(config, "api_server", expected["chat"])
        # Exercise the saved overlay produced by the desktop's media settings.
        optional = set(startup["tools"]["chatWithMedia"]) - set(startup["tools"]["chat"])
        config["platform_toolsets"] = {"api_server": ["hermes-api-server", *sorted(optional)]}
        media_chat = expected["chatWithMediaAndComputerSelected"]
        media_chat["enabledToolsets"] = sorted([*media_chat["enabledToolsets"], "screens"])
        check_tools(config, "api_server", media_chat)
        from cron.jobs import list_jobs
        assert list_jobs(include_disabled=True) == expected["defaultSchedules"]

        from hermes_cli.plugins import get_plugin_manager
        (home / "config.yaml").write_text(
            "hooks_auto_accept: true\nhooks:\n  on_session_end:\n  - command: approved-fixture\n"
            "    timeout: 1\n", encoding="utf-8")
        initialize()
        initialize()
        on_end = get_plugin_manager()._hooks["on_session_end"]
        assert hook_id(on_end[0], home) == startup["hooks"]["on_session_end"][0]
        assert [callback.__name__ for callback in on_end[1:]] == ["shell_hook[on_session_end:approved-fixture]"]
    finally:
        for handler in list(root.handlers):
            if isinstance(handler, crema_failures._Handler):
                root.removeHandler(handler)


@pytest.mark.parametrize(("deployment", "service", "theme"), [
    ("krema-engine", "dev", "krema-developer"),
    ("krema-engine", "dev", "krema-staff"),
    ("jabis-engine", "fc", "jabis"),
])
def test_gateway_profile_preserves_tools_hooks_and_schedules(app, tmp_path, monkeypatch, deployment, service, theme):
    from cron.jobs import list_jobs, use_cron_store
    from gateway.run_startup import GatewayStartupMixin
    from hermes_cli.config import load_config

    monkeypatch.setenv("SERVICE_DIR", str(app / f"services/{service}"))
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://service.invalid")
    monkeypatch.setenv("ENGINE_KEY_SECRET", "offline-gateway-fixture")
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    import engine_home
    monkeypatch.setattr(engine_home, "HOME", Path(os.environ["HERMES_HOME"]))
    monkeypatch.setattr(engine_home, "SERVICE", app / f"services/{service}")
    monkeypatch.setattr(engine_home, "profile_api", lambda *_args, **_kwargs: {"briefing_time": "09:00"})
    startup, expected = setup_input(app, tmp_path, deployment, theme, monkeypatch)
    home = engine_home.prepare_profile("u1", {"themeId": theme, "workspace": str(tmp_path / "work"),
                                            "uploads": str(tmp_path / "uploads")})
    from engine_keys import engine_key
    from gateway.run import _profile_runtime_scope

    with _profile_runtime_scope(home, hydrate_secrets=False):
        GatewayStartupMixin._register_config_hooks("test profile hooks")
        GatewayStartupMixin._register_config_hooks("duplicate test profile hooks")
        check_registration(home, startup, expected)
        config = load_config()
        assert config["plugins"]["enabled"] == expected["configuredPlugins"]
        chat = expected["chat"]
        if "backup" in chat["disabledToolsets"]:
            chat["disabledToolsets"].remove("backup")
            for field, item in (
                ("enabledToolsets", "backup"), ("selected", "crema_backup"),
                ("schemasBeforeToolSearchWithoutCredentials", "crema_backup"),
            ):
                chat[field] = sorted([*chat[field], item])
            field = "schemasAfterToolSearchWithoutCredentials"
            if "tool_search" not in chat[field]:
                chat[field] = sorted([*chat[field], "crema_backup"])
        check_tools(config, "api_server", chat)
        check_tools(config, "cron", expected["cron"])
        from agent import outbound_webhooks
        from hermes_cli.plugins import get_plugin_manager
        deliveries = []
        monkeypatch.setattr(outbound_webhooks, "_enqueue", deliveries.append)
        webhook = get_plugin_manager()._hooks["on_session_end"][-1]
        webhook(session_id="fixture-session")
        delivery, = deliveries
        signature = hmac.new(engine_key(home.name).encode(), delivery["body"], hashlib.sha256).hexdigest()
        assert delivery["headers"]["X-Hermes-Signature-256"] == "sha256=" + signature
        with use_cron_store(home):
            jobs = {job["name"]: job for job in list_jobs(include_disabled=True)}
        assert sorted(jobs) == sorted(job["name"] for job in expected["defaultSchedules"])
        for item in expected["defaultSchedules"]:
            job = jobs[item["name"]]
            source = app / "services" / service / item["prompt"]
            assert hashlib.sha256(source.read_bytes()).hexdigest() == item["promptSha256"]
            assert job["prompt"] == source.read_text(encoding="utf-8").strip()
            assert job["schedule"]["expr"] == item["installedSchedule"]
