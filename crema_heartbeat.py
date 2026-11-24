"""Crema's per-conversation heartbeat, driven by its existing profile cron ticker."""

import asyncio
from contextvars import ContextVar
from uuid import uuid4

PATH = "/api/crema/heartbeat"
USAGE = "켜기 /heartbeat every 30m 할 일 · 멈추기 /heartbeat pause · 다시 시작 /heartbeat resume · 끄기 /heartbeat off"
STOPPED = "하트비트가 멈췄습니다."
current_turn = ContextVar("crema_heartbeat_turn", default=None)


def reference(job):
    revision = (job.get("origin") or {}).get("heartbeat")
    return {"id": job["id"], "revision": revision} if revision else None


def active(ref, session):
    if not isinstance(ref, dict) or not isinstance(ref.get("id"), str):
        return False
    from crema_startup import engine_input
    from cron import jobs
    from hermes_cli.config import load_config_readonly

    startup = engine_input(load_config_readonly())
    if not startup or not startup["schedules"].get("heartbeat"):
        return False
    job = jobs.get_job(ref["id"])
    if not job or not job.get("enabled") or reference(job) != ref:
        return False
    origin = job["origin"]
    if origin["platform"] == "crema":
        from agent.crema_file_turns import _directory
        return any(row["id"] == origin["chat_id"] and row["session"] == session for row in _directory())
    return origin["session_id"] == session


def desktop_session(job):
    from agent.crema_file_turns import _directory

    chat = job["origin"]["chat_id"]
    return next((row["session"] for row in _directory() if row["id"] == chat), "")


def command(home, *, server, chat, session, arg):
    from crema_startup import engine_input
    from cron import jobs
    from hermes_cli.config import load_config_readonly
    from hermes_cli.heartbeat import MIN_INTERVAL_SECONDS, format_interval, parse_interval

    startup = engine_input(load_config_readonly())
    if not startup or not startup["schedules"].get("heartbeat"):
        return {"error": "이 대화에서 하트비트를 사용할 수 없습니다."}
    if not all(isinstance(value, str) and 0 < len(value) <= 200 and "\0" not in value for value in (chat, session)):
        return {"error": "하트비트를 켤 대화를 확인해 주세요."}
    if not isinstance(arg, str):
        return {"error": USAGE}
    action = arg.strip().lower()
    schedule = prompt = None
    if action not in {"", "status", "pause", "resume", "off", "clear"}:
        parts = arg.strip().split(maxsplit=1)
        if parts[0].lower() == "every":
            parts = parts[1].split(maxsplit=1) if len(parts) == 2 else []
        try:
            seconds = parse_interval(parts[0]) if len(parts) == 2 else None
        except (OverflowError, ValueError):
            seconds = None
        if not seconds or seconds < MIN_INTERVAL_SECONDS or seconds % 60 or seconds > 365 * 86400:
            return {"error": "간격은 1분에서 365일 사이로 정해 주세요. " + USAGE}
        schedule, prompt = f"every {seconds // 60}m", parts[1]
        action = "set"

    if action in {"set", "resume"}:
        if server:
            from urllib.parse import urlencode
            from engine_home import ProfileAPIError, profile_api

            try:
                query = urlencode({"sessionId": session, "kind": "heartbeat"})
                context = profile_api(home, "/api/scheduled-context?" + query, timeout=5)
            except ProfileAPIError as error:
                if error.status in {403, 404, 409}:
                    return {"error": error.detail}
                raise
            if context["chatId"] != chat:
                return {"error": "하트비트를 켤 대화를 확인해 주세요."}
        else:
            from agent.crema_file_turns import _directory
            if not any(row["id"] == chat and row["session"] == session for row in _directory()):
                return {"error": "열려 있는 대화에서 하트비트를 켜 주세요."}

    with jobs.use_cron_store(home), jobs._jobs_lock():
        current = next((job for job in jobs.list_jobs(include_disabled=True)
                        if reference(job) and job["origin"]["chat_id"] == chat), None)
        if action in {"off", "clear"} and current:
            jobs.remove_job(current["id"])
            current = None
        elif action == "pause" and current:
            current = jobs.pause_job(current["id"])
            jobs.cancel_job_run(current["id"], reason=STOPPED)
        elif action == "set" or (action == "resume" and current and not current["enabled"]):
            origin = {"platform": "api_server" if server else "crema", "chat_id": chat,
                      "session_id": session, "heartbeat": uuid4().hex}
            if server:
                origin["profile"] = home.name
            schedule = schedule or current["schedule"]
            if current:
                jobs.cancel_job_run(current["id"], reason=STOPPED)
                current = jobs.update_job(current["id"], {
                    "origin": origin, "prompt": prompt or current["prompt"], "schedule": schedule,
                    "enabled": True, "state": "scheduled", "paused_at": None, "paused_reason": None,
                })
            else:
                current = jobs.create_job(prompt, schedule, name="하트비트", deliver="local",
                                          origin=origin, catch_up=False)
        if not current:
            return {"state": "off"}
        minutes = current["schedule"].get("minutes")
        interval = format_interval(minutes * 60) if minutes else current["schedule_display"]
        return {"state": "active" if current["enabled"] else "paused", "interval": interval, "prompt": current["prompt"]}


def install(app, *, server=False):
    from hermes_constants import get_hermes_home

    @app.post(PATH)
    async def heartbeat(body: dict):
        return await asyncio.to_thread(command, get_hermes_home(), server=server,
                                       chat=body.get("chat"), session=body.get("session"), arg=body.get("arg", ""))
