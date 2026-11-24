"""Crema: scheduled tasks. A task is the engine's cron job (cron/jobs.py) whose origin is the chat that made it; at its
time the scheduler hands its prompt to that chat as a message (cron/scheduler.py run_job ->
agent/crema_file_turns.scheduled), so the work runs in that chat with its context and the user sees it there."""
from __future__ import annotations

import json
import hashlib
import hmac
import time
from contextvars import ContextVar
from typing import Any, Dict
from urllib.parse import urlencode

from tools.registry import registry, tool_error

PLATFORM = "crema"
SCHEDULED_HEADER = "X-Crema-Scheduled-Turn"
_scheduled_turn = ContextVar("crema_scheduled_turn", default=None)
MISSING_SOURCE = "이 예약의 원래 대화를 확인하지 못했습니다."


def scheduled_signature(key, occurrence):
    return hmac.new(key.encode(), ("crema-schedule:" + occurrence).encode(), hashlib.sha256).hexdigest()


def is_scheduled_request(request, profile):
    signature = request.headers.get(SCHEDULED_HEADER)
    occurrence = request.headers.get("Idempotency-Key", "")
    if not signature or not profile or not occurrence.startswith("crema-scheduled-"):
        return False
    from engine_keys import engine_key
    return hmac.compare_digest(signature.encode(), scheduled_signature(engine_key(profile), occurrence).encode())


def scheduled_turn():
    return _scheduled_turn.get()


def scheduled_pc_unavailable(reason):
    turn = scheduled_turn()
    if turn is not None:
        turn.setdefault("pc_unavailable", reason)


def missing_source(job):
    origin = job.get("origin") or {}
    return (job.get("paused_reason") in ("PC판 Crema 가져오기", MISSING_SOURCE)
            or (origin.get("platform") in ("crema", "api_server")
                and not (origin.get("session_id") or origin.get("chat_id"))))


def server_scheduled(job, *, cancel_event=None, extra_prompt=None):
    """Submit a profile-bound occurrence through the same session run API as the browser."""
    import httpx
    from agent import crema_file_turns
    from cron import jobs
    from crema_heartbeat import STOPPED, active, reference
    from cron.occurrences import scheduled_instant
    from engine_home import ProfileAPIError, profile_api
    from engine_keys import engine_key
    from hermes_constants import get_hermes_home
    from persistence_assembly import open_persistence, profile_scope
    from gateway.platforms.api_server_run_idempotency import TERMINAL_STATUSES
    from utils import atomic_json_write

    home = get_hermes_home()
    origin = job.get("origin") or {}
    session = origin.get("session_id") or origin.get("chat_id")
    name = job.get("name") or job["id"]
    heartbeat = reference(job)

    def stopped():
        return ((cancel_event is not None and cancel_event.is_set())
                or (heartbeat is not None and not active(heartbeat, session)))

    def result(status, text):
        return status == "completed", f"# {name}\n\n{text}\n", text, None if status == "completed" else text

    if heartbeat is not None and stopped():
        return result("failed", STOPPED)
    if not session or not session.startswith(crema_file_turns.CHAT_PREFIX) or origin.get("profile", home.name) != home.name:
        jobs.pause_job(job["id"], MISSING_SOURCE)
        return result("failed", MISSING_SOURCE)
    with open_persistence(profile_scope()) as store:
        owned = store.sessions.get(store.lineage.resolve_resume_target(session))
    if not owned:
        jobs.pause_job(job["id"], MISSING_SOURCE)
        return result("failed", MISSING_SOURCE)
    from crema_startup import engine_input
    from hermes_cli.config import load_config_readonly
    config = load_config_readonly()
    startup = engine_input(config)
    if not startup or not (startup["schedules"].get("heartbeat") if heartbeat is not None
                           else "crema_schedule" in startup["tools"]["chat"]):
        return result("failed", "이 계정은 개인 예약을 실행할 수 없습니다.")
    when = scheduled_instant(job.get("_scheduled_instant") or job.get("next_run_at"))
    if when is None:
        return result("failed", "예약 발생 시각을 확인하지 못했습니다.")
    occurrence = "crema-scheduled-" + hashlib.sha256(f"{job['id']}:{when}".encode()).hexdigest()
    outbox = home / "cron" / "crema-results" / (occurrence + ".json")
    submitted = outbox.with_suffix(".request")
    outbox.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if outbox.is_file():
        payload = json.loads(outbox.read_text(encoding="utf-8"))
    else:
        key = engine_key(home.name)
        body = {"session_id": session, "input": job["prompt"] + (f"\n\n{extra_prompt}" if extra_prompt else "")}
        if heartbeat is not None:
            body["crema_heartbeat"] = heartbeat
        headers = {"Authorization": f"Bearer {key}", "Idempotency-Key": occurrence,
                   SCHEDULED_HEADER: scheduled_signature(key, occurrence), "X-Crema-Theme": startup["themeId"]}
        api = crema_file_turns._api
        run_id = occurrence
        status = "unavailable"
        text = "예약을 시작할 수 없습니다."
        if submitted.is_file():
            body = json.loads(submitted.read_text(encoding="utf-8"))
        elif api is not None:
            try:
                query = urlencode({"sessionId": session, **({"kind": "heartbeat"} if heartbeat else {})})
                context = profile_api(home, f"/api/scheduled-context?{query}", timeout=5)
            except ProfileAPIError as error:
                if error.status == 404:
                    jobs.pause_job(job["id"], MISSING_SOURCE)
                    return result("failed", MISSING_SOURCE)
                if error.status != 409:
                    raise
                api = None
                text = "프로젝트의 현재 폴더를 확인하지 못해 예약을 맡지 못했습니다."
            else:
                project, model = context["project"], context["model"]
                if project is not None:
                    body["crema_project"] = project
                    path = project["path"]
                    if path.startswith("pc://"):
                        device, _, folder = path[5:].partition("/")
                        body["instructions"] = (
                            f"이 예약의 작업 폴더는 연결 PC의 {path}입니다. 폴더의 파일은 PC 도구로 다루세요. "
                            f"pc_files의 folder나 path에는 위 참조를 쓰고, "
                            f"pc_run을 사용한다면 pc에 {device}를, 명령에 PC 경로 {folder}를 쓰세요."
                        )
                if model is not None:
                    body.update({field: model[field] for field in ("model", "provider") if field in model})
                    options = {}
                    if model.get("reasoning"):
                        options["reasoning"] = {"effort": model["reasoning"]}
                    if model.get("fast"):
                        options["service_tier"] = "priority"
                    if options:
                        body["model_options"] = options
                # Lost acceptance is retried with the same request even if the chat changes.
                atomic_json_write(submitted, body, mode=0o600)
        if heartbeat is not None and stopped():
            submitted.unlink(missing_ok=True)
            return result("failed", STOPPED)
        if api is not None:
            url = f"http://127.0.0.1:{api._port}/p/{home.name}/v1/runs"
            with httpx.Client(headers=headers, timeout=15, trust_env=False) as client:
                reply = client.post(url, json=body)
                if reply.status_code == 202:
                    run_id = reply.json()["run_id"]
                    while True:
                        if stopped():
                            client.post(f"{url}/{run_id}/stop")
                            submitted.unlink(missing_ok=True)
                            return result("failed", "예약 실행을 중단했습니다.")
                        current = client.get(f"{url}/{run_id}")
                        current.raise_for_status()
                        run = current.json()
                        if run.get("status") in TERMINAL_STATUSES:
                            break
                        time.sleep(0.5)
                    if run.get("schedule_unavailable"):
                        text = run["schedule_unavailable"] + ("\n\n" + run["output"] if run.get("output") else "")
                    elif run.get("status") == "completed":
                        status, text = "completed", run.get("output") or "예약이 끝났습니다."
                    else:
                        status, text = "failed", "예약 실행을 마치지 못했습니다."
                elif reply.status_code == 404:
                    submitted.unlink(missing_ok=True)
                    jobs.pause_job(job["id"], MISSING_SOURCE)
                    return result("failed", MISSING_SOURCE)
                elif reply.status_code == 409:
                    code = reply.json().get("error", {}).get("code")
                    text = ("연결된 PC를 확인하지 못해 예약을 맡지 못했습니다."
                            if code == "scheduled_pc_unavailable" else
                            "프로젝트의 현재 폴더를 확인하지 못했거나 대화가 사용 중입니다. 예약을 맡지 못했습니다.")
        payload = {"jobId": job["id"], "scheduledAt": when, "sessionId": session, "runId": run_id,
                   "status": status, "text": text}
        if heartbeat is not None:
            if stopped():
                submitted.unlink(missing_ok=True)
                return result("failed", STOPPED)
            if status == "completed":
                payload["text"] = "하트비트\n\n" + text
            payload["heartbeat"] = heartbeat
        atomic_json_write(outbox, payload, mode=0o600)
    submitted.unlink(missing_ok=True)
    try:
        post_scheduled_result(home, outbox, payload)
    except ProfileAPIError as error:
        if error.status == 404:
            jobs.pause_job(job["id"], MISSING_SOURCE)
            return result("failed", MISSING_SOURCE)
        raise
    return result(payload["status"], payload["text"])


def post_heartbeat_notice(job, text):
    from cron.occurrences import scheduled_instant
    from crema_heartbeat import active, reference
    from engine_home import ProfileAPIError, profile_api
    from hermes_constants import get_hermes_home

    session = job["origin"]["session_id"]
    ref = reference(job)
    when = scheduled_instant(job.get("_scheduled_instant") or job.get("next_run_at"))
    if not when or not active(ref, session):
        return
    try:
        profile_api(get_hermes_home(), "/api/scheduled-results", {
            "jobId": job["id"], "scheduledAt": when, "sessionId": session, "runId": job["id"],
            "status": "failed", "text": text, "heartbeat": ref,
        }, timeout=5)
    except ProfileAPIError as error:
        if error.status not in {403, 404}:
            raise


def post_scheduled_result(home, outbox, payload):
    from crema_heartbeat import active
    from cron.jobs import use_cron_store
    from engine_home import ProfileAPIError, profile_api

    if ref := payload.get("heartbeat"):
        with use_cron_store(home):
            if not active(ref, payload["sessionId"]):
                outbox.unlink(missing_ok=True)
                return
    try:
        answer = profile_api(home, "/api/scheduled-results", payload, timeout=5)
        if not isinstance(answer, dict) or type(answer.get("posted")) is not bool:
            raise ValueError("invalid scheduled result response")
    except ProfileAPIError as error:
        if error.status in (401, 403, 404):
            outbox.unlink(missing_ok=True)
        raise
    outbox.unlink(missing_ok=True)


def retry_scheduled_results():
    from engine_home import HOME

    for path in (HOME / "profiles").glob("*/cron/crema-results/*.json"):
        home = path.parents[2]
        if home.resolve().parent != (HOME / "profiles").resolve() or path.resolve() != path:
            continue
        try:
            post_scheduled_result(home, path, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue


def _view(job: dict) -> dict:
    return {"id": job["id"], "name": job.get("name"), "task": job.get("prompt"),
            "when": job.get("schedule_display"), "next_run": job.get("next_run_at"),
            "paused": not job.get("enabled", True) and job.get("state") != "completed",
            "done": job.get("state") == "completed", "this_chat": job.get("_this_chat", False),
            **({"last_error": job["last_error"]} if job.get("last_error") else {})}


def crema_schedule(args: Dict[str, Any], session_id: str = "", **_: Any) -> str:
    from agent import crema_file_turns
    from cron import jobs

    chat = crema_file_turns._chat(session_id)
    # A chat in the sidebar: the app's throwaway sessions (agent-client-aux-…, the first title) are Crema sessions
    # too, and a model there that ran the request anyway made the user's task twice (2026-11-05 PC check).
    if not chat or not any(row["session"] == chat for row in crema_file_turns._directory()):
        return tool_error("Only a Crema chat can schedule tasks.")
    action = args.get("action")
    try:
        if action == "create":
            origin = {"platform": PLATFORM, "chat_id": chat}
            if PLATFORM == "api_server":
                from hermes_constants import get_hermes_home
                origin.update(session_id=chat, profile=get_hermes_home().name)
            job = jobs.create_job(args.get("task"), str(args.get("when") or ""), name=args.get("name") or None,
                                  deliver="local", origin=origin)
            return json.dumps({"created": _view(dict(job, _this_chat=True))}, ensure_ascii=False)
        if action == "list":
            mine = [dict(j, _this_chat=(j.get("origin") or {}).get("chat_id") == chat)
                    for j in jobs.list_jobs(include_disabled=True) if (j.get("origin") or {}).get("platform") == PLATFORM
                    and not (j.get("origin") or {}).get("heartbeat")]
            return json.dumps({"tasks": [_view(j) for j in mine]}, ensure_ascii=False)
        job_id = str(args.get("id") or "")
        from crema_heartbeat import reference
        if action in {"update", "pause", "resume", "remove"} and (existing := jobs.resolve_job_ref(job_id)) and reference(existing):
            return tool_error("Use the heartbeat command in its conversation.")
        if action == "update":
            updates = {k: v for k, v in (("prompt", args.get("task")), ("name", args.get("name"))) if v}
            if args.get("when"):
                updates["schedule"] = jobs.parse_schedule(str(args["when"]))
            job = jobs.update_job(job_id, updates) if updates else None
        elif action == "pause":
            job = jobs.pause_job(job_id)
        elif action == "resume":
            if PLATFORM == "api_server" and (previous := jobs.get_job(job_id)) and missing_source(previous):
                return tool_error(MISSING_SOURCE)
            job = jobs.resume_job(job_id)
        elif action == "remove":
            return json.dumps({"removed": jobs.remove_job(job_id)})
        else:
            return tool_error("action must be create, list, update, pause, resume or remove")
    except ValueError as exc:
        return tool_error(str(exc))
    return json.dumps({"task": _view(job)}, ensure_ascii=False) if job else tool_error(
        f"No scheduled task '{job_id}' (or nothing to change). Call with action list for the ids.")


registry.register(
    name="crema_schedule", toolset="crema_schedule", emoji="⏰",
    schema={"name": "crema_schedule",
            "description": "Scheduled tasks: work Crema hands to THIS chat at a set time, once or again and again, as "
                           "if the user sent the task then; this chat does it with what it knows, and the user sees the "
                           "task and your reply here. Make one only when the user asks for work at a time or on a "
                           "repeat ('매일 아침 9시에 메일 정리해 줘', '30분 뒤에 알려 줘'); then tell them in one line what "
                           "runs when. It runs only while Crema is open; a time missed while Crema was closed runs once "
                           "when Crema opens again. Each run uses the user's model usage. create: when and task "
                           "(written so it makes sense on its own later), name optional. list: every scheduled task "
                           "(this_chat marks this chat's; done marks a one-time task that already ran). update: id and what changes (when, task, name). "
                           "pause, resume, remove: id.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["create", "list", "update", "pause", "resume", "remove"]},
                "when": {"type": "string", "description": "create, update: 'in 30m' or 'in 2h' (once), an ISO time "
                         "such as '2026-11-06T09:00' (once, local time), 'every 30m' / 'every 2h' (repeat), 'every "
                         "monday 9am', or a 5-field cron expression such as '0 9 * * *' (every day 9:00 local time)"},
                "task": {"type": "string", "description": "create, update: the work to do at that time"},
                "name": {"type": "string", "description": "create, update: a short name the user will recognize"},
                "id": {"type": "string", "description": "update, pause, resume, remove: the task id from list"},
            }, "required": ["action"]}},
    handler=crema_schedule,
)
