"""Crema: scheduled tasks. A task is the engine's cron job (cron/jobs.py) whose origin is the chat that made it; at its
time the scheduler hands its prompt to that chat as a message (cron/scheduler.py run_job ->
agent/crema_file_turns.scheduled), so the work runs in that chat with its context and the user sees it there."""
from __future__ import annotations

import json
from typing import Any, Dict

from tools.registry import registry, tool_error

PLATFORM = "crema"


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
            job = jobs.create_job(args.get("task"), str(args.get("when") or ""), name=args.get("name") or None,
                                  deliver="local", origin={"platform": PLATFORM, "chat_id": chat})
            return json.dumps({"created": _view(dict(job, _this_chat=True))}, ensure_ascii=False)
        if action == "list":
            mine = [dict(j, _this_chat=(j.get("origin") or {}).get("chat_id") == chat)
                    for j in jobs.list_jobs(include_disabled=True) if (j.get("origin") or {}).get("platform") == PLATFORM]
            return json.dumps({"tasks": [_view(j) for j in mine]}, ensure_ascii=False)
        job_id = str(args.get("id") or "")
        if action == "update":
            updates = {k: v for k, v in (("prompt", args.get("task")), ("name", args.get("name"))) if v}
            if args.get("when"):
                updates["schedule"] = jobs.parse_schedule(str(args["when"]))
            job = jobs.update_job(job_id, updates) if updates else None
        elif action == "pause":
            job = jobs.pause_job(job_id)
        elif action == "resume":
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
