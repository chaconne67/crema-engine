"""Crema: the /once and /repeat commands (주인님 2026-11-26: 한 번 실행, 정한 시각에 반복, 간격 반복(하트비트) 셋).

A command makes the same scheduled task the chat's own crema_schedule tool makes (tools/crema_schedule_tool.py), so it
runs in that chat as any scheduled task does. The time comes in a fixed form read here, so no model is called; a time
said in other words is still asked of the chat."""

import asyncio
import json
import re
from datetime import datetime, timedelta

PATH = "/api/crema/schedule"
USAGE = {
    "once": "한 번: /once 30m 할 일 · /once 09:00 할 일 · /once 내일 09:00 할 일 · /once 2026-11-27 09:00 할 일",
    "repeat": "시각 반복: /repeat 매일 09:00 할 일 · /repeat 평일 18:00 할 일 · /repeat 월,수,금 09:00 할 일",
}
DAYS = {"매일": "*", "평일": "1-5", "주말": "0,6"}
WEEKDAYS = {"일": 0, "월": 1, "화": 2, "수": 3, "목": 4, "금": 5, "토": 6}
UNITS = {"m": 1, "분": 1, "h": 60, "시간": 60, "d": 1440, "일": 1440}
LONGEST = 365 * 1440


def _clock(text):
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if match and int(match[1]) < 24 and int(match[2]) < 60:
        return int(match[1]), int(match[2])
    return None


def _split(text, count):
    """The first ``count`` words and the task after them; the task is required."""
    parts = text.split(maxsplit=count)
    if len(parts) <= count or not parts[count].strip():
        return None, None
    return parts[:count], parts[count].strip()


def once(text, now):
    """``(when, task)`` for the crema_schedule tool. A clock time alone is its next one."""
    words, task = _split(text, 1)
    if words:
        duration = re.fullmatch(r"(\d+)\s*(m|분|h|시간|d|일)", words[0])
        if duration:
            minutes = int(duration[1]) * UNITS[duration[2]]
            if not 0 < minutes <= LONGEST:
                raise ValueError("시간은 1분에서 365일 사이로 정해 주세요.")
            return f"in {minutes}m", task
        clock = _clock(words[0])
        if clock:
            at = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
            return (at if at > now else at + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"), task
    words, task = _split(text, 2)
    if words and (clock := _clock(words[1])):
        day = {"오늘": 0, "내일": 1, "모레": 2}.get(words[0])
        try:
            date = (now + timedelta(days=day)).date() if day is not None else datetime.strptime(words[0], "%Y-%m-%d").date()
        except ValueError:
            date = None
        if date:
            at = now.replace(year=date.year, month=date.month, day=date.day, hour=clock[0], minute=clock[1],
                             second=0, microsecond=0)
            if at <= now:
                raise ValueError("이미 지난 시각입니다.")
            return at.strftime("%Y-%m-%dT%H:%M"), task
    raise ValueError("언제 할지와 할 일을 적어 주세요.")


def repeat(text):
    """``(when, task)``: days and a clock time as a cron expression in the engine's time zone."""
    words, task = _split(text, 2)
    clock = words and _clock(words[1])
    if clock:
        days = DAYS.get(words[0])
        if days is None:
            names = [name for name in re.split(r"[,\s]+", words[0]) if name]
            names = names if len(names) > 1 else list(words[0])
            if names and all(name in WEEKDAYS for name in names):
                days = ",".join(str(number) for number in sorted({WEEKDAYS[name] for name in names}))
        if days:
            return f"{clock[1]} {clock[0]} * * {days}", task
    raise ValueError("요일과 시각, 할 일을 적어 주세요.")


def command(*, session, kind, arg):
    from crema_startup import engine_input
    from cron import jobs
    from hermes_cli.config import load_config_readonly
    from tools.crema_schedule_tool import crema_schedule

    startup = engine_input(load_config_readonly())
    if not startup or "crema_schedule" not in startup["tools"]["chat"]:
        return {"error": "이 대화에서 예약을 사용할 수 없습니다."}
    if kind not in USAGE or not isinstance(arg, str) or len(arg) > 8000:
        return {"error": "예약 명령을 확인해 주세요."}
    if not isinstance(session, str) or not 0 < len(session) <= 200 or "\0" in session:
        return {"error": "예약을 만들 대화를 확인해 주세요."}
    text = arg.strip()
    if not text:
        listed = json.loads(crema_schedule({"action": "list"}, session_id=session))
        if "error" in listed:
            return {"error": "이 대화의 예약을 읽지 못했습니다.", "usage": USAGE[kind]}
        mine = [task for task in listed["tasks"] if task["this_chat"] and not task["done"]
                and (task["when"] or "").startswith("once") == (kind == "once")]
        return {"tasks": mine, "usage": USAGE[kind]}
    try:
        when, task = once(text, jobs._hermes_now()) if kind == "once" else repeat(text)
    except ValueError as error:
        return {"error": f"{error} {USAGE[kind]}"}
    made = json.loads(crema_schedule({"action": "create", "when": when, "task": task}, session_id=session))
    if "error" in made:
        return {"error": "예약을 만들지 못했습니다: " + made["error"]}
    return {"created": made["created"]}


def install(app):
    @app.post(PATH)
    async def schedule(body: dict):
        return await asyncio.to_thread(command, session=body.get("session"), kind=body.get("kind"),
                                       arg=body.get("arg", ""))
