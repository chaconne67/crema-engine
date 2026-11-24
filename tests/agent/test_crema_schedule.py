"""Crema: scheduled tasks are cron jobs handed to the chat that made them (tools/crema_schedule_tool.py,
agent/crema_file_turns.scheduled, cron/scheduler.py run_job)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from agent import crema_file_turns as turns
from cron import jobs
from cron.scheduler import run_job
from tools.crema_schedule_tool import crema_schedule

A, B = "agent-client-a", "agent-client-b"
NOW = datetime(2026, 11, 5, 9, 0, 0, tzinfo=timezone.utc)


class FakeDB:
    def get_conversation_root(self, session_id):
        return session_id

    def get_session(self, session_id):
        return {"title": {A: "메일", B: "보고서"}.get(session_id, "")}


class FakeApi:
    _run_statuses = {}

    def _ensure_session_db(self):
        return FakeDB()


@pytest.fixture
def crema(tmp_path, monkeypatch):
    monkeypatch.setattr(turns, "_api", FakeApi())
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: NOW)
    monkeypatch.setattr("hermes_time.now", lambda: NOW)
    turns.set_chats([{"id": "a", "session": A, "title": "메일", "project": ""},
                     {"id": "b", "session": B, "title": "보고서", "project": ""}])


def call(session, **args):
    return json.loads(crema_schedule(args, session_id=session))


def handed(session):
    return [item for entry in turns.state().get("messages", []) if entry["session_id"] == session
            for item in entry["items"]]


def test_a_chat_makes_lists_pauses_and_removes_its_task(crema):
    made = call(A, action="create", when="every 30m", task="받은 메일을 정리해 알려 줘", name="메일 정리")["created"]
    stored = jobs.get_job(made["id"])
    assert stored["origin"] == {"platform": "crema", "chat_id": A} and stored["deliver"] == "local"
    assert made["when"] == "every 30m" and not made["paused"] and made["this_chat"]
    assert [t["this_chat"] for t in call(B, action="list")["tasks"]] == [False]
    assert call(A, action="pause", id=made["id"])["task"]["paused"]
    assert not call(A, action="resume", id=made["id"])["task"]["paused"]
    assert call(A, action="update", id=made["id"], when="every 2h")["task"]["when"] == jobs.parse_schedule("every 2h")["display"]
    assert call(A, action="remove", id=made["id"]) == {"removed": True}
    assert call(A, action="list") == {"tasks": []}


def test_only_a_crema_chat_schedules(crema):
    assert "error" in call("20261105_cli", action="create", when="in 30m", task="x")
    assert "error" in call("agent-client-aux-1", action="create", when="in 30m", task="x")
    assert jobs.load_jobs() == []


def test_a_run_hands_the_task_to_the_chat_that_made_it(crema):
    made = call(A, action="create", when="in 30m", task="회의 자료 확인", name="회의 준비")["created"]
    job = jobs.get_job(made["id"])
    job["next_run_at"] = NOW.isoformat()
    ok, doc, final, error = run_job(job)
    assert ok and error is None and A in doc and final
    [item] = handed(A)
    assert item["text"] == "회의 자료 확인" and item["from_title"] == "예약 작업" and item["from_chat"] == ""
    assert item["scheduled"] == {"name": "회의 준비"} and "Scheduled task" in item["prompt"]
    assert handed(B) == []


def test_a_run_missed_while_closed_says_when_it_was_due(crema):
    made = call(A, action="create", when="every 1h", task="정리", name="정리")["created"]
    job = jobs.get_job(made["id"])
    due = (NOW - timedelta(hours=3)).isoformat()
    job["last_dispatch"] = {"scheduled_at": due}
    run_job(job)
    [item] = handed(A)
    assert item["scheduled"] == {"name": "정리", "due": due} and "while Crema was closed" in item["prompt"]


def test_a_run_for_a_chat_gone_from_crema_fails(crema):
    made = call(A, action="create", when="in 30m", task="x")["created"]
    turns.set_chats([{"id": "b", "session": B, "title": "보고서", "project": ""}])
    ok, _, _, error = run_job(jobs.get_job(made["id"]))
    assert not ok and "no longer in Crema" in error and handed(A) == []


def test_a_one_shot_missed_while_closed_still_runs_once(crema):
    made = call(A, action="create", when="in 30m", task="x")["created"]
    jobs.save_jobs([dict(jobs.get_job(made["id"]), next_run_at=(NOW - timedelta(hours=5)).isoformat())])
    assert [j["id"] for j in jobs.get_due_jobs()] == [made["id"]]


def test_service_job_and_import_pause_keep_existing_lanes(crema, monkeypatch):
    from types import SimpleNamespace
    import sys
    import cron.scheduler as scheduler
    from tools import crema_schedule_tool

    monkeypatch.setenv("SERVICE_DIR", "/test/service")
    monkeypatch.setitem(sys.modules, "engine_home", SimpleNamespace(
        ProfileAPIError=ValueError, refresh_account_profile=lambda home: None))
    monkeypatch.setattr(crema_schedule_tool, "PLATFORM", "api_server")
    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda: {"crema_personal_schedule_mode": "conversation"})
    service = jobs.create_job("managed schedule", "every 1h")
    imported = jobs.create_job("from Crema", "every 1h")
    jobs.pause_job(imported["id"], "PC판 Crema 가져오기")
    assert "error" in call(A, action="resume", id=imported["id"])
    assert not jobs.get_job(imported["id"])["enabled"]
    made = call(A, action="create", when="every 1h", task="personal")["created"]
    personal = jobs.get_job(made["id"])
    assert personal["origin"]["session_id"] == A

    managed_result = (True, "independent", "managed", None)
    personal_result = (True, "conversation", "personal", None)
    dispatched = []
    monkeypatch.setattr(scheduler, "_prepare_job_prompt", lambda *args, **kwargs: (managed_result, None))
    monkeypatch.setattr("gateway.session_cron.current_execution", lambda: (None, "cron-test", service["id"], None))
    monkeypatch.setattr(crema_schedule_tool, "server_scheduled",
                        lambda job, **kwargs: dispatched.append(job["id"]) or personal_result)
    assert run_job(service) == managed_result
    assert run_job(personal) == personal_result
    assert dispatched == [personal["id"]]


@pytest.mark.parametrize(("project", "external_worker"), [
    (None, False),
    ({"id": "docs", "path": "/current"}, False),
    ({"id": "docs", "path": "pc://own-pc/C:/current"}, False),
    pytest.param(None, True, id="external-worker-without-api"),
])
def test_scheduled_result_retries_the_same_occurrence_without_restarting_the_turn(
    tmp_path, monkeypatch, project, external_worker
):
    import sys
    from types import SimpleNamespace
    from cron import scheduler
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools import crema_schedule_tool as schedule

    home = tmp_path / "profiles/own"
    home.mkdir(parents=True)
    host = "192.0.2.23" if external_worker else "0.0.0.0"
    request_host = host if external_worker else "127.0.0.1"
    session_api = SimpleNamespace(_port=8754)
    monkeypatch.setattr(turns, "_api", None if external_worker else session_api)
    sent, posted = [], []

    class Client:
        def __init__(self, *, headers, **_):
            self.headers = headers

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url, *, json):
            assert url == f"http://{request_host}:8754/p/own/v1/runs"
            assert json["session_id"] == A and json["input"] == "작업"
            assert json.get("crema_project") == project
            if project and project["path"].startswith("pc://"):
                assert project["path"] in json["instructions"]
            assert (json["provider"], json["model"], json["model_options"]) == (
                "custom:own", "current-chat-model", {"reasoning": {"effort": "high"}, "service_tier": "priority"})
            occurrence = self.headers["Idempotency-Key"]
            assert self.headers[schedule.SCHEDULED_HEADER] == schedule.scheduled_signature("key", occurrence)
            sent.append(occurrence)
            return SimpleNamespace(status_code=202, json=lambda: {"run_id": "run-once"})

        def get(self, url):
            assert url.endswith("/run-once")
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {"status": "completed", "output": "끝"})

    def call_profile(profile, path, payload=None, **_):
        assert profile == home
        if path == "/api/scheduled-context?sessionId=agent-client-a":
            return {"project": project, "model": {"provider": "custom:own", "model": "current-chat-model",
                                                  "reasoning": "high", "fast": True}}
        assert path == "/api/scheduled-results"
        posted.append(payload)
        if len(posted) == 1:
            raise OSError("reply lost")
        return {"posted": False, "chatId": "a", "rev": 2}

    error = type("ProfileAPIError", (ValueError,), {})
    monkeypatch.setitem(sys.modules, "engine_home", SimpleNamespace(
        HOST=host, PORT=8754, ProfileAPIError=error, profile_api=call_profile,
        refresh_account_profile=lambda _: None))
    monkeypatch.setitem(sys.modules, "engine_keys", SimpleNamespace(engine_key=lambda _: "key"))
    monkeypatch.setattr("crema_startup.engine_input", lambda _: {
        "themeId": "test", "tools": {"chat": ["crema_schedule"]}})
    monkeypatch.setattr("httpx.Client", Client)
    if external_worker:
        monkeypatch.setenv("SERVICE_DIR", str(tmp_path))
        monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                            lambda: {"crema_personal_schedule_mode": "conversation"})
        monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
        monkeypatch.setattr("crema_startup.initialize", lambda: None)
        monkeypatch.setattr("cron.executions.adopt_claimed_execution", lambda _: {"status": "running"})
        monkeypatch.setattr(scheduler, "run_one_job", lambda job, **_: run_job(job)[0])

    def fire(job):
        if not external_worker:
            return schedule.server_scheduled(job)[0]
        payload = tmp_path / "worker.json"
        payload.write_text(json.dumps({
            "job": {**job, "execution_id": "external-worker"},
            "profile_home": str(home),
            "multiplex_active": True,
        }), encoding="utf-8")
        return scheduler._run_external_worker_payload(payload, tmp_path / "worker.ready")

    token = set_hermes_home_override(home)
    try:
        _seed_scheduled_session(home)
        job = {"id": "job", "prompt": "작업",
               "origin": {"platform": "api_server", "session_id": A, "profile": "own"},
               "_scheduled_instant": "2026-11-20T18:00:00+09:00"}
        with pytest.raises(OSError, match="lost"):
            fire(job)
        assert len(sent) == 1
        assert fire(job)
        assert len(sent) == 1
        assert posted[0] == posted[1] == {"jobId": "job", "scheduledAt": "2026-11-20T09:00:00+00:00",
                                          "sessionId": A, "runId": "run-once", "status": "completed", "text": "끝"}
    finally:
        reset_hermes_home_override(token)


def test_scheduled_pc_offline_posts_without_starting_a_run(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools import crema_schedule_tool as schedule

    class ProfileAPIError(ValueError):
        pass

    def profile_api(home, path, payload=None, **_):
        if path == f"/api/scheduled-context?sessionId={A}":
            return {"project": {"id": "docs", "path": "pc://own-pc/C:/current"}, "model": None}
        assert path == "/api/scheduled-results"
        assert payload["status"] == "unavailable" and "PC" in payload["text"] and "맡지 못했습니다" in payload["text"]
        return {"posted": True}

    class Client:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url, *, json):
            assert json["crema_project"]["path"] == "pc://own-pc/C:/current"
            return SimpleNamespace(status_code=409, json=lambda: {"error": {"code": "scheduled_pc_unavailable"}})

    monkeypatch.setattr(turns, "_api", SimpleNamespace(_port=8754))
    monkeypatch.setitem(sys.modules, "engine_home", SimpleNamespace(
        HOST="0.0.0.0", PORT=8754, ProfileAPIError=ProfileAPIError, profile_api=profile_api))
    monkeypatch.setitem(sys.modules, "engine_keys", SimpleNamespace(engine_key=lambda _: "key"))
    monkeypatch.setattr("crema_startup.engine_input", lambda _: {
        "themeId": "test", "tools": {"chat": ["crema_schedule"]}})
    monkeypatch.setattr("httpx.Client", Client)
    token = set_hermes_home_override(tmp_path)
    try:
        _seed_scheduled_session(tmp_path)
        job = {"id": "job", "prompt": "작업", "origin": {"session_id": A},
               "_scheduled_instant": "2026-11-20T18:00:00+09:00"}
        assert not schedule.server_scheduled(job)[0]
    finally:
        reset_hermes_home_override(token)


def _seed_scheduled_session(home):
    from persistence.conversation.sessions import NewSession
    from persistence_assembly import open_persistence, profile_scope

    with open_persistence(profile_scope(home=home)) as store:
        store.sessions.create(NewSession(A, "api_server"))
