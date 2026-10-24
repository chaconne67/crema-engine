"""Crema: the engine's own failures, kept for the app to send to Aishift when the user leaves 오류 정보 보내기 on
(crema docs Crema-설치도구-싣기와-오류보고-계획-2026-10-24.md). A failure is an error the engine logs while an
exception is being handled (a tool that could not run, a step that broke) or a settings call that answered 5xx.

Only where and what kind: the logger's name or the settings route, the exception's class, and the frames in the
engine's own source (file:line function). Never a log message, an argument, a path outside the engine, or anything
the user wrote or connected; the server takes ASCII codes only. Each kind once per engine run, at most LIMIT waiting."""
from __future__ import annotations

import logging
import platform
import re
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, List

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


def take() -> Dict[str, Any]:
    """The failures waiting (then none are), with this Windows's version for the report."""
    with _lock:
        taken = _waiting[:]
        _waiting.clear()
    return {"os": UNSAFE.sub("", platform.platform(terse=True))[:48], "failures": taken}


class _Handler(logging.Handler):
    def emit(self, entry: logging.LogRecord) -> None:
        # The exception logged with the error, or the one being handled where it was logged.
        kind, _, tb = entry.exc_info if entry.exc_info and entry.exc_info[0] else sys.exc_info()
        if kind is not None:
            record(entry.name, kind.__name__, _frames(tb))


def watch() -> None:
    logging.getLogger().addHandler(_Handler(logging.ERROR))
