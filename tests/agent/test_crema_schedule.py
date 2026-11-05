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
