"""Crema: /once and /repeat make the chat's scheduled task from a fixed time form (crema_schedule_command.py)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from agent import crema_file_turns as turns
from cron import jobs
from crema_schedule_command import command, once, repeat

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 26, 10, 0, 0, tzinfo=KST)
A, B = "agent-client-a", "agent-client-b"


class FakeDB:
    def get_conversation_root(self, session_id):
        return session_id


class FakeApi:
    _run_statuses = {}

    def _ensure_session_db(self):
        return FakeDB()


@pytest.mark.parametrize("text, when", [
    ("30m 회의 자료 확인해 줘", "in 30m"),
    ("2시간 회의 자료 확인해 줘", "in 120m"),
    ("1d 회의 자료 확인해 줘", "in 1440m"),
    ("11:30 회의 자료 확인해 줘", "2026-11-26T11:30"),
    ("09:00 회의 자료 확인해 줘", "2026-11-27T09:00"),
    ("10:00 회의 자료 확인해 줘", "2026-11-27T10:00"),
    ("내일 09:00 회의 자료 확인해 줘", "2026-11-27T09:00"),
    ("오늘 18:00 회의 자료 확인해 줘", "2026-11-26T18:00"),
    ("2026-12-01 07:05 회의 자료 확인해 줘", "2026-12-01T07:05"),
])
def test_once_reads_a_duration_a_clock_time_or_a_date(text, when):
    assert once(text, NOW) == (when, "회의 자료 확인해 줘")


@pytest.mark.parametrize("text", ["30m", "09:00", "내일 09:00", "오늘 09:00 지난 시각", "0m 할 일", "366d 할 일",
                                  "25:00 할 일", "다음주 09:00 할 일", "2026-13-01 09:00 할 일", "곧 할 일"])
def test_once_refuses_a_missing_task_a_past_time_or_another_form(text):
    with pytest.raises(ValueError):
        once(text, NOW)


@pytest.mark.parametrize("text, when", [
    ("매일 09:00 메일 정리해 줘", "0 9 * * *"),
    ("평일 18:30 메일 정리해 줘", "30 18 * * 1-5"),
    ("주말 07:00 메일 정리해 줘", "0 7 * * 0,6"),
    ("월,수,금 09:00 메일 정리해 줘", "0 9 * * 1,3,5"),
    ("월수금 09:00 메일 정리해 줘", "0 9 * * 1,3,5"),
    ("일 21:00 메일 정리해 줘", "0 21 * * 0"),
])
def test_repeat_reads_days_and_a_clock_time(text, when):
    assert repeat(text) == (when, "메일 정리해 줘")


@pytest.mark.parametrize("text", ["매일 09:00", "매일 메일 정리", "매주 09:00 할 일", "월,x 09:00 할 일", "30m 할 일"])
def test_repeat_refuses_other_forms(text):
    with pytest.raises(ValueError):
        repeat(text)


@pytest.fixture
def crema(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("SERVICE_DIR", raising=False)
    build_input = tmp_path / "engine-input.json"
    tools = {"chat": ["crema_schedule"]}
    build_input.write_text(json.dumps({"themeIds": ["test"], "placement": "embedded",
                                       "engineInputs": {"test": {"tools": tools, "schedules": {}}}}))
    monkeypatch.setenv("CREMA_ENGINE_INPUT", str(build_input))
    monkeypatch.setattr(turns, "_api", FakeApi())
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: NOW)
    monkeypatch.setattr("hermes_time.now", lambda: NOW)
    turns.set_chats([{"id": "a", "session": A, "title": "메일", "project": ""},
                     {"id": "b", "session": B, "title": "보고서", "project": ""}])
    return tools


def test_commands_make_the_chats_own_tasks_and_list_them_by_kind(crema):
    made = command(session=A, kind="once", arg="30m 회의 자료 확인해 줘")["created"]
    stored = jobs.get_job(made["id"])
    assert stored["origin"] == {"platform": "crema", "chat_id": A} and stored["deliver"] == "local"
    assert stored["schedule"]["kind"] == "once" and stored["prompt"] == "회의 자료 확인해 줘"
    daily = command(session=A, kind="repeat", arg="평일 09:00 메일 정리해 줘")["created"]
    assert jobs.get_job(daily["id"])["schedule"] == {"kind": "cron", "expr": "0 9 * * 1-5", "display": "0 9 * * 1-5"}
    command(session=B, kind="repeat", arg="매일 18:00 보고서 정리해 줘")
    assert [task["id"] for task in command(session=A, kind="once", arg="")["tasks"]] == [made["id"]]
    assert [task["id"] for task in command(session=A, kind="repeat", arg=" ")["tasks"]] == [daily["id"]]
    assert "usage" in command(session=A, kind="repeat", arg="")


def test_commands_refuse_bad_input_a_foreign_session_and_a_theme_without_tasks(crema, monkeypatch):
    assert "error" in command(session=A, kind="once", arg="곧 할 일")
    assert "error" in command(session=A, kind="weekly", arg="매일 09:00 할 일")
    assert "error" in command(session="agent-client-gone", kind="once", arg="30m 할 일")
    assert jobs.list_jobs(include_disabled=True) == []

    without = jobs.CRON_DIR.parent / "engine-input-without-tasks.json"
    without.write_text(json.dumps({"themeIds": ["test"], "placement": "embedded",
                                   "engineInputs": {"test": {"tools": {"chat": []}, "schedules": {}}}}))
    monkeypatch.setenv("CREMA_ENGINE_INPUT", str(without))
    assert command(session=A, kind="once", arg="30m 할 일") == {"error": "이 대화에서 예약을 사용할 수 없습니다."}
