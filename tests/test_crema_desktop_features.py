"""Exercise the desktop settings and chat tool contracts in a temporary Crema home."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

from aiohttp import web
from aiohttp.test_utils import TestClient as RunClient, TestServer
from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

import crema_startup
from crema_desktop_features import install
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms.api_server_runs import _http_routes
from tools import clarify_gateway
from tools.registry import registry


SCREENS = "/api/crema/local-screens"
SKILLS = "/api/crema/personal-skills"
PENDING = "/api/crema/pending-question"


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_SKILLS", str(tmp_path / "bundle"))
    path = tmp_path / "build-input.json"
    monkeypatch.setenv("CREMA_ENGINE_INPUT", str(path))

    def setup(names=(), *, brain="pc", api=None, media=None, features=None):
        if features is None:
            features = {"chatScreens": "screens" in names or "screens" in (media or ()),
                        "personalSkills": "skills" in names, "restorePendingClarify": "clarify" in names}
        path.write_text(json.dumps({
            "placement": "embedded" if brain == "pc" else "remote",
            "themeIds": ["fixture"],
            "engineInputs": {"fixture": {"brain": brain, "desktopFeatures": features, "tools": {
                "chat": list(names), "chatWithMedia": list(names if media is None else media),
            }, "connectionPolicy": {"source": "local"}, "hooks": {}}},
        }), encoding="utf-8")
        crema_startup._deployment.cache_clear()
        settings = FastAPI()
        api = APIServerAdapter(PlatformConfig(enabled=True)) if api is None else api
        install(settings, api, crema_startup)
        return settings, api

    yield setup, home
    if "screen_show" in registry.get_tool_names_for_toolset("screens"):
        registry.deregister("screen_show")
    crema_startup._deployment.cache_clear()


def test_local_screen_show_list_answer_and_restore(desktop, monkeypatch, tmp_path):
    setup, _ = desktop
    settings, api = setup(media=["screens"])
    db = api._ensure_session_db()
    chat = "agent-client-card"
    db.create_session(chat, "api_server")
    db.end_session(chat, "compression")
    db.create_session("compacted", "api_server", parent_session_id=chat)

    def show(args, session="compacted"):
        return json.loads(registry.dispatch("screen_show", args, session_id=session))

    items = [{"id": "one", "label": "<img src=x onerror=alert(1)>", "description": "첫 항목"}]
    choice = {"kind": "choice", "title": "<선택>", "items": items}
    assert "error" in show(choice, None)
    assert "error" in show({**choice, "chat": "foreign"})
    assert "error" in show({**choice, "html": "<p>CRM</p>"})
    assert "error" in show({**choice, "items": items * 2})
    assert "error" in show({**choice, "items": [{**items[0], "crm_id": "crm-1"}]})
    created = show(choice)
    assert created.keys() == {"id", "detail"}
    screen_id = created["id"]
    other = show({"kind": "confirm", "title": "취소할까요?"}, "agent-client-other")["id"]

    with TestClient(settings) as client:
        row, = client.get(SCREENS, params={"chat": chat}).json()["screens"]
        assert row == {"id": screen_id, "chat": chat, **choice, "created": row["created"], "done": False}
        assert isinstance(row["created"], int) and row["created"] > 1_000_000_000_000
        assert client.get(SCREENS, params={"chat": "foreign"}).json() == {"screens": []}
        assert {row["id"] for row in client.get(SCREENS, params=[
            ("chat", chat), ("chat", "agent-client-other"), ("chat", chat),
        ]).json()["screens"]} == {screen_id, other}
        route = f"{SCREENS}/{screen_id}"
        assert client.post(route, content="not json").status_code == 400
        assert client.post(route, json={"value": {"id": "unknown"}}).status_code == 400
        assert client.post(route, json={"value": {"confirmed": True}}).status_code == 400
        assert client.post(f"{SCREENS}/unknown", json={"value": {"id": "one"}}).status_code == 404
        with ThreadPoolExecutor(max_workers=2) as workers:
            first, second = workers.map(lambda _: client.post(route, json={"value": {"id": "one"}}), range(2))
        assert sorted((first.status_code, second.status_code)) == [200, 409]
        answered = next(out.json() for out in (first, second) if out.is_success)
        assert answered["id"] == screen_id and answered["done"] is True
        assert answered["result"] == {"id": "one"}
        assert answered["said"] == f"{items[0]['label']}만 골랐습니다."
        assert f"{screen_id}번" in answered["prompt"]
        assert answered["prompt"].endswith(f"{answered['said']}\n고른 항목(id: 이름):\none: {items[0]['label']}")
        assert client.post(f"{SCREENS}/{other}", json={"value": {"confirmed": 1}}).status_code == 400
        cancelled = client.post(f"{SCREENS}/{other}", json={"value": {"confirmed": False}}).json()
        assert cancelled["id"] == other and cancelled["done"] is True
        assert cancelled["result"] == {"confirmed": False}
        assert cancelled["said"] == "취소했습니다."
        assert cancelled["prompt"].endswith(cancelled["said"])
        confirm = show({"kind": "confirm", "title": "계속할까요?"}, "agent-client-other")["id"]
        accepted = client.post(f"{SCREENS}/{confirm}", json={"value": {"confirmed": True}}).json()
        assert accepted["result"] == {"confirmed": True}
        assert accepted["said"] == "확인했습니다."
        assert accepted["prompt"].endswith(accepted["said"])

    restarted, _ = setup(["screens"], api=api)
    with TestClient(restarted) as client:
        assert client.get(SCREENS, params={"chat": chat}).json() == {
            "screens": [{**row, "done": True, "result": {"id": "one"}}],
        }
    foreign_home = tmp_path / "foreign-home"
    foreign_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(foreign_home))
    isolated, _ = setup(["screens"])
    with TestClient(isolated) as client:
        assert client.get(SCREENS, params={"chat": chat}).json() == {"screens": []}
        assert client.post(route, json={"value": {"id": "one"}}).status_code == 404


def test_personal_skill_management_uses_local_store_and_preserves_slash_list(desktop, tmp_path):
    from agent.skill_commands import get_skill_commands
    from tools.skills_hub import HubLockFile
    from tools.skills_sync import sync_skills

    setup, home = desktop

    def text(name, description):
        return f"---\nname: {name}\ndescription: {description}\n---\n\n작업을 정리합니다.\n"

    bundled = Path(os.environ["HERMES_BUNDLED_SKILLS"])
    for name in ("crema-guide", "ego-browser"):
        package = bundled / name
        package.mkdir(parents=True)
        (package / "SKILL.md").write_text(text(name, "제품 안내"), encoding="utf-8")
    sync_skills(quiet=True)
    before = get_skill_commands()
    settings, _ = setup(["skills"])
    with TestClient(settings) as client:
        expected = {"name": "my-skill", "description": "내 스킬", "kind": "own", "common": False,
                    "has_original": False, "editable": True}
        content = text("my-skill", "내 스킬")
        assert client.post(SKILLS, json={"action": "create", "name": "my-skill", "content": content}).json() == {
            "success": True, "message": "Skill 'my-skill' created.",
        }
        assert client.get(SKILLS, params={"name": "my-skill"}).json() == {**expected, "text": content}
        rows = {row["name"]: row for row in client.get(SKILLS).json()["skills"]}
        assert rows["my-skill"] == expected
        assert rows["crema-guide"]["common"] and not rows["crema-guide"]["editable"]
        assert get_skill_commands().keys() == before.keys() | {"/my-skill"}
        changed = text("my-skill", "바뀐 스킬")
        assert client.post(SKILLS, json={"action": "patch", "name": "my-skill", "content": changed}).json()["success"]
        assert client.get(SKILLS, params={"name": "my-skill"}).json()["text"] == changed
        assert client.post(SKILLS, json={"action": "patch", "name": "my-skill",
                                         "content": text("crema-guide", "다른 이름")}).json()["success"] is False
        assert client.post(SKILLS, json={"action": "delete", "name": "../my-skill"}).status_code == 400
        assert client.post(SKILLS, json={"action": "create", "name": "nul", "content": text("nul", "안내")}).status_code == 400
        assert client.post(SKILLS, json={"action": "install", "url": "https://example.invalid"}).status_code == 404
        for name in ("crema-guide", "ego-browser"):
            assert client.post(SKILLS, json={"action": "delete", "name": name}).status_code == 403
        assert client.post(SKILLS, json={"action": "patch", "name": "crema-guide", "content": changed}).status_code == 403

        version = text("ego-browser", "개인 사본")
        assert client.post(SKILLS, json={"action": "patch", "name": "ego-browser", "content": version}).json()["success"]
        assert (bundled / "ego-browser/SKILL.md").read_text(encoding="utf-8") == text("ego-browser", "제품 안내")
        backup = home / "skills/ego-browser.bak"
        backup.mkdir()
        (backup / "SKILL.md").write_text(text("ego-browser.bak", "별개 스킬"), encoding="utf-8")
        assert client.post(SKILLS, json={"action": "delete", "name": "ego-browser"}).status_code == 409
        assert (backup / "SKILL.md").read_text(encoding="utf-8") == text("ego-browser.bak", "별개 스킬")
        assert client.post(SKILLS, json={"action": "delete", "name": "ego-browser.bak"}).json()["success"]
        assert client.post(SKILLS, json={"action": "delete", "name": "ego-browser"}).json()["success"]
        assert client.get(SKILLS, params={"name": "ego-browser"}).json()["common"]

        hub = HubLockFile()
        hub.record_install("my-skill", source="fixture", identifier="fixture", trust_level="community",
                           scan_verdict="clean", skill_hash="fixture", install_path="my-skill", files=["SKILL.md"])
        assert client.get(SKILLS, params={"name": "my-skill"}).json()["kind"] == "installed"
        assert client.post(SKILLS, json={"action": "delete", "name": "my-skill"}).json()["success"]
        assert client.get(SKILLS, params={"name": "my-skill"}).status_code == 404
        assert hub.get_installed("my-skill") is None
        assert get_skill_commands().keys() == before.keys()

        from hermes_cli.config import load_config, save_config

        external = tmp_path / "external"
        external.mkdir()
        config = load_config()
        config.setdefault("skills", {})["create_dir"] = str(external)
        save_config(config)
        assert client.post(SKILLS, json={"action": "create", "name": "my-skill", "content": content}).status_code == 403
        assert list(external.iterdir()) == []

    assert not (home / "skills/my-skill").exists()


@pytest.mark.linux_only
def test_personal_skills_refuse_links(desktop, tmp_path):
    setup, home = desktop
    settings, _ = setup(["skills"])
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    package = home / "skills/linked"
    package.mkdir(parents=True)
    (package / "SKILL.md").symlink_to(outside)
    with TestClient(settings) as client:
        assert client.get(SKILLS).json() == {"skills": []}
        assert client.get(SKILLS, params={"name": "linked"}).status_code == 403
        assert client.post(SKILLS, json={"action": "delete", "name": "linked"}).status_code == 403
        assert outside.read_text(encoding="utf-8") == "outside"


@pytest.mark.asyncio
async def test_pending_question_is_bound_to_live_run_and_session(desktop, monkeypatch):
    setup, _ = desktop
    settings, api = setup(["clarify"])
    agent = MagicMock()
    questions = [{"qid": "q", "question": "어떤 색인가요?", "context": "색을 고르세요.",
                  "choices_offered": ["빨강", "파랑"], "descriptions": ["따뜻함", "차가움"], "multi_select": False}]

    def converse(*args, **kwargs):
        reply = agent.clarify_callback("색", None, questions=questions)
        return {"final_response": f"{reply['answers']['q']}으로 정했습니다."}

    agent.run_conversation.side_effect = converse
    monkeypatch.setattr(api, "_create_agent", lambda **kwargs: agent)
    run_api = web.Application()
    run_api.router.add_routes([web.route(method, path, handler) for method, path, handler in _http_routes(api)])
    async with (
        RunClient(TestServer(run_api)) as runs,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=settings), base_url="http://settings") as reader,
    ):
        session_id = "agent-client-live"
        response = await runs.post("/v1/runs", json={"input": "색을 물어봐 줘", "session_id": session_id})
        assert response.status == 202
        run_id = (await response.json())["run_id"]
        run_task = api._active_run_tasks[run_id]
        try:
            async with runs.get(f"/v1/runs/{run_id}/events") as stream:
                async with asyncio.timeout(5):
                    async for line in stream.content:
                        if line.startswith(b"data: "):
                            event = json.loads(line[6:])
                            if event["event"] == "clarify.request":
                                break
                    else:
                        pytest.fail("The stream ended before the question")

            async def read(session=session_id, run=None):
                params = {"session_id": session, **({"run_id": run} if run else {})}
                response = await reader.get(PENDING, params=params)
                assert response.status_code == 200
                return response.json()

            pending = {"run_id": run_id, "clarify_id": event["clarify_id"], "title": "색", "questions": [{
                "qid": "q", "question": "어떤 색인가요?", "context": "색을 고르세요.",
                "choices": ["빨강", "파랑"], "descriptions": ["따뜻함", "차가움"], "multi_select": False,
            }]}
            assert await read() == {"pending": pending}
            assert await read(run=run_id) == {"pending": pending, "run": {"status": "waiting_for_clarify"}}
            assert await read(session="agent-client-gone", run=run_id) == {"pending": None}

            url = f"/v1/runs/{run_id}/clarify"
            reply = {"clarify_id": pending["clarify_id"], "answers": {"q": "파랑"}}
            assert (await runs.post(url, json=reply)).status == 200
            assert (await runs.post(url, json=reply)).status == 409
            await asyncio.wait_for(run_task, timeout=5)
            assert await read() == {"pending": None}
            assert await read(run=run_id) == {
                "pending": None, "run": {"status": "completed", "output": "파랑으로 정했습니다."},
            }
            restarted, _ = setup(["clarify"])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://settings") as new:
                assert (await new.get(PENDING, params={"session_id": session_id, "run_id": run_id})).json() == {
                    "pending": None,
                }
        finally:
            if not run_task.done():
                clarify_gateway.clear_session(run_id)
                await asyncio.wait_for(run_task, timeout=5)


@pytest.mark.parametrize("brain,features", [
    ("server", {"chatScreens": True, "personalSkills": True, "restorePendingClarify": True}),
    ("pc", {"chatScreens": False, "personalSkills": False, "restorePendingClarify": False}),
])
def test_local_features_absent_when_engine_input_does_not_select_them(desktop, brain, features):
    setup, home = desktop
    settings, _ = setup(["screens", "skills", "clarify"], brain=brain, features=features)
    with TestClient(settings) as client:
        for route in (SCREENS, SKILLS, f"{PENDING}?session_id=agent-client-card"):
            assert client.get(route).status_code == 404
    assert "screen_show" not in registry.get_tool_names_for_toolset("screens")
    assert not (home / "local-screens.sqlite3").exists()


@pytest.mark.asyncio
async def test_engine_startup_installs_local_features_behind_the_settings_token(desktop, monkeypatch):
    import threading

    import crema_engine
    from tools import crema_services

    # Keep the real settings construction and middleware. The worker and listener are
    # covered independently, and the test does not need to contact a provider.
    setup, _ = desktop
    setup(["screens", "skills", "clarify"])
    ready, close = asyncio.Event(), asyncio.Event()
    settings = None

    class InProcessSettings:
        started = False

        def __init__(self, config):
            nonlocal settings
            settings = config.app

        async def serve(self):
            self.started = True
            ready.set()
            await close.wait()

    async def run_api_connected(self):
        return True

    start_thread = threading.Thread.start

    def skip_cron(thread):
        if thread.name != "crema-cron-ticker":
            start_thread(thread)

    token = "isolated-settings-test-token"
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", token)
    monkeypatch.setattr(crema_startup, "initialize", lambda **kwargs: None)
    monkeypatch.setattr(crema_engine, "use_only_crema_connections", lambda: None)
    monkeypatch.setattr(crema_engine, "index_past_chats", lambda: None)
    monkeypatch.setattr(crema_services, "sync_services", lambda: None)
    monkeypatch.setattr(crema_services, "SETTINGS", {})
    monkeypatch.setattr(APIServerAdapter, "connect", run_api_connected)
    monkeypatch.setattr(threading.Thread, "start", skip_cron)
    monkeypatch.setattr("uvicorn.Server", InProcessSettings)

    task = asyncio.create_task(crema_engine.main())
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=settings), base_url="http://settings") as client:
            paths = {SCREENS: {"screens": []}, SKILLS: {"skills": []},
                     f"{PENDING}?session_id=agent-client-card": {"pending": None}}
            for path, result in paths.items():
                assert (await client.get(path)).status_code == 401
                assert (await client.get(path, headers={"X-Hermes-Session-Token": token})).json() == result
        from model_tools import get_tool_definitions

        assert [tool["function"]["name"] for tool in get_tool_definitions(
            ["screens"], quiet_mode=True, skip_tool_search_assembly=True)] == ["screen_show"]
    finally:
        close.set()
        await asyncio.wait_for(task, timeout=5)
