"""Crema: every time the user was let down, kept for the app to send to Aishift when the user leaves 오류 정보 보내기 on
(crema docs Crema-설치도구-싣기와-오류보고-계획-2026-10-24.md, 4-1). Not only what broke: an answer that ended in an
error or was stopped (turn), a tool that did not do its work (tool), an approval nobody answered (approval), the AI
that could not be reached (model), a settings call or connection that did not finish (settings, connect.*), a request
the agent judged it could not do because Crema fell short (unmet, the crema_unmet tool), and an error the engine
logs while an exception is being handled (the logger's name).

Only where and what kind: the logger's name or the settings route, the exception's class, and the frames in the
engine's own source (file:line function). Never a log message, an argument, a path outside the engine, or anything
the user wrote or connected; the server takes ASCII codes only. Each kind once per engine run, at most LIMIT waiting."""
from __future__ import annotations

import json
import logging
import platform
import re
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, List

from tools.registry import registry, tool_error

ROOT = Path(__file__).resolve().parent.parent  # the engine source
UNSAFE = re.compile(r"[^A-Za-z0-9_.:/<>+ -]")
LIMIT = 50
_lock = threading.Lock()
_waiting: List[Dict[str, Any]] = []
_seen: set = set()


def _frames(tb) -> List[str]:
    """The last frames of the traceback that are the engine's own code."""
    lines = []
    for frame in traceback.extract_tb(tb):
        try:
            where = Path(frame.filename).resolve().relative_to(ROOT).as_posix()
        except ValueError:  # Python's own or a package's
            continue
        lines.append(UNSAFE.sub("", f"{where}:{frame.lineno} {frame.name}")[:120])
    return lines[-6:]


def record(stage: str, code: str, trace: List[str] = ()) -> None:
    stage, code = UNSAFE.sub("", stage)[:32], UNSAFE.sub("", code)[:48]
    if not (stage and code):
        return
    kind = (stage, code, tuple(trace[-1:]))
    with _lock:
        if kind in _seen or len(_waiting) >= LIMIT:
            return
        _seen.add(kind)
        _waiting.append({"stage": stage, "code": code, "details": {"trace": list(trace)} if trace else {}})


def failed(stage: str, exc: BaseException) -> None:
    """An exception that ended a step: its class and where in the engine it was raised."""
    record(stage, type(exc).__name__, _frames(exc.__traceback__))


def take() -> Dict[str, Any]:
    """The failures waiting (then none are), with this Windows's version for the report."""
    with _lock:
        taken = _waiting[:]
        _waiting.clear()
    return {"os": UNSAFE.sub("", platform.platform(terse=True))[:48], "failures": taken}


class _Handler(logging.Handler):
    def emit(self, entry: logging.LogRecord) -> None:
        # The exception logged with the error, or the one being handled where it was logged.
        exc = entry.exc_info[1] if entry.exc_info and entry.exc_info[0] else sys.exc_info()[1]
        if exc is not None:
            failed(entry.name, exc)


def watch() -> None:
    logging.getLogger().addHandler(_Handler(logging.ERROR))


# The engine's own moments (hermes_cli/plugins.py VALID_HOOKS), kinds only.
def _turn_ended(completed=None, failed=None, interrupted=None, turn_exit_reason=None, **_: Any) -> None:
    if interrupted:
        record("turn", "stopped")
    elif failed or completed is False:
        record("turn", str(turn_exit_reason or "unfinished").split("(")[0])


def _tool_ended(tool_name: str = "", result: Any = None, status: str = "", **_: Any) -> None:
    if status != "error":
        return
    try:
        data = result if isinstance(result, dict) else json.loads(result)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict) and data.get("user_summary"):
        return  # the user said no to an approval; one nobody answered is the approval's own record
    code = data.get("exit_code") if isinstance(data, dict) else None
    record("tool", f"{tool_name} exit {code}" if isinstance(code, int) else tool_name)


def _approval_answered(choice: str = "", **_: Any) -> None:
    if choice in ("timeout", "notify_failed"):
        record("approval", choice)


def _model_failed(provider: str = "", status_code=None, reason=None, retryable=None, retry_count=None,
                  max_retries=None, **_: Any) -> None:
    if retryable and retry_count is not None and max_retries is not None and retry_count < max_retries:
        return  # tried again
    record("model", f"{provider} {reason or status_code or 'error'}")


def observe(manager) -> None:
    """Into the plugin manager, as crema_engine.py adds its transform_llm_output."""
    for name, callback in (("on_session_end", _turn_ended), ("post_tool_call", _tool_ended),
                           ("post_approval_response", _approval_answered), ("api_request_error", _model_failed)):
        manager._hooks.setdefault(name, []).append(callback)


# The agent's own judgement that it could not do what was asked because of Crema (주인님 2026-10-24: a request
# Crema could not answer, like the PC it could not work on without Git Bash). Which kind only, never the request.
WHY = {
    "no_tool": "Crema has no tool or feature that does it",
    "broken": "a tool or feature Crema has failed or gave a wrong result",
    "blocked": "Windows, another program or a site refused, or lacks what is needed (permission, a program not "
               "installed, a sign-in wall)",
    "not_connected": "the service or account it needs is not connected to Crema",
    "other": "another shortcoming of Crema",
}
AREAS = {
    "files": "finding, moving or organising files and folders on the PC",
    "pc": "programs, windows and settings of the PC",
    "web": "websites and web search",
    "mail": "the user's mail",
    "calendar": "the user's calendar",
    "documents": "reading or making office files, PDFs and their content",
    "media": "images, audio and video",
    "service": "another service the agent works in",
    "other": "anything else",
}


def crema_unmet(args: Dict[str, Any], **_: Any) -> str:
    why, area = args.get("why"), args.get("area")
    if why not in WHY or area not in AREAS:
        return tool_error(f"why must be one of {', '.join(WHY)}, and area one of {', '.join(AREAS)}")
    record("unmet", f"{why} {area}")
    return json.dumps({"recorded": True})


registry.register(
    name="crema_unmet", toolset="crema_unmet", emoji="📝",
    schema={"name": "crema_unmet",
            "description": "Record that a request of the user's, or a part of it, is going undone because Crema "
                           "itself fell short, so the people who make Crema can fix it. Call it once per request, "
                           "right before you tell the user you cannot do it, when the cause is on Crema's side: no "
                           "tool or feature for it, a tool that did not work, the PC or a site that would not let "
                           "Crema, or a service not connected yet. Do not call it when the user decided not to go "
                           "on, when you decline for safety, or when a step failed and you still got the request "
                           "done another way. When unsure whether the cause is Crema's, record it: a shortcoming "
                           "not recorded is never fixed, and an extra record costs nothing. Only the two kinds are "
                           "kept, never what the user asked; nothing changes in the chat, so go on with your answer.",
            "parameters": {"type": "object", "properties": {
                "why": {"type": "string", "enum": list(WHY),
                        "description": "; ".join(f"{key}: {text}" for key, text in WHY.items())},
                "area": {"type": "string", "enum": list(AREAS),
                         "description": "what the request was about. "
                                        + "; ".join(f"{key}: {text}" for key, text in AREAS.items())},
            }, "required": ["why", "area"]}},
    handler=crema_unmet,
)
