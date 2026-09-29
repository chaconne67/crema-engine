"""Crema: chats that change the same file take turns.

Each Crema chat's current work is a task on the Hermes kanban board (``hermes_cli.kanban_db``, in
HERMES_HOME), assignee ``crema``, ``session_id`` = the chat's engine session. The files it wrote are
``crema_file`` events on that task. When a chat is about to write a file an open task of another chat
wrote (the ``pre_tool_call`` hook):

1. an order already on the board (a link, set by a judge or by the user) decides;
2. the other chat is not replying and nothing of the file is left uncommitted (outside Git: it is not
   replying) → the file is handed over;
3. otherwise one auxiliary model call judges: ``a_first`` (the other chat first), ``b_first`` (this
   chat first), ``together``, or ``ask_user``. A judge that fails or answers out of contract asks the
   user as well — never a guessed order.

A refused write costs nothing while it waits. ``settle`` (on each ``GET /api/crema/turns`` from the
app) completes the task of a chat that others wait on once it is not replying and has nothing
uncommitted in its files; the board then promotes the waiting chats and the app starts them again.
Terminal commands are not refused (the files they touch are known only afterwards): in a Git folder
the files a command changed are recorded after it, and an overlap is told to both chats.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

ASSIGNEE = "crema"
# Crema's chats are agent-client-<chat id> (the app's desktop.js); other sessions are left alone.
CHAT_PREFIX = "agent-client-"
WRITE_TOOLS = ("write_file", "patch")
DECISIONS = ("a_first", "b_first", "together", "ask_user")
# Both attempts together stay under the pre_tool_call hook limit (plugins.hook_callback_timeout, 30s).
JUDGE_BUDGET_S = 25.0
CONTEXT_CHARS = 2000
_OPEN = ("done", "archived")

JUDGE_PROMPT = """Two conversations in Crema, a desktop assistant, work on the same user's computer at the same time. Both are about to change the same file. Decide which one changes it first so their edits do not overwrite or contradict each other. The user wrote both requests; neither conversation matters more by default.

For each conversation you get what the user asked (latest requests first), whether it is replying right now, and the files it changed. For the shared file you get A's changes that are not committed yet and the change B is about to make.

Decide one of:
- a_first: B waits until A is done with the file. For example B needs A's result, A is nearly done, or both change the same part.
- b_first: A waits for B. For example A needs B's result, or the user said B is urgent and B's change is small.
- together: both go on. Only when the changes are in clearly separate parts of the file and neither needs the other.
- ask_user: the requests want different results for the same thing, or one removes what the other changes; only the user can choose.

Reply with JSON only: {"decision": "a_first|b_first|together|ask_user", "reason": "one sentence in the language the user writes in, naming what decided it"}"""

_api = None
_guard = threading.Lock()
_path_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
# tool_call_id -> Git state before a terminal command / what to tell the chat after it.
_before_terminal: dict[str, dict[str, float]] = {}
_after_terminal: dict[str, str] = {}


class JudgeError(RuntimeError):
    """The judge gave no answer inside the contract."""


def install(api) -> None:
    """Remember the run API (who is replying, session titles) and add the hooks."""
    global _api
    _api = api
    from hermes_cli.plugins import get_plugin_manager

    hooks = get_plugin_manager()._hooks
    for name, callback in (("pre_tool_call", before_tool), ("post_tool_call", after_tool),
                           ("transform_tool_result", tool_result), ("pre_llm_call", notes_for_turn)):
        hooks.setdefault(name, []).append(callback)


# ── the board ────────────────────────────────────────────────────────────────


def _kb():
    from hermes_cli import kanban_db

    return kanban_db


def _board():
    from hermes_cli.kanban_db_connect import connect

    return connect()


def _open_task(conn, session_id: str):
    tasks = [t for t in _kb().list_tasks(conn, assignee=ASSIGNEE, session_id=session_id) if t.status not in _OPEN]
    return max(tasks, key=lambda t: (t.created_at, t.id)) if tasks else None


def _task_for(conn, session_id: str):
    kb = _kb()
    return _open_task(conn, session_id) or kb.get_task(
        conn, kb.create_task(conn, title=_title(session_id), assignee=ASSIGNEE, session_id=session_id))


def _add_event(conn, task_id: str, kind: str, payload: dict) -> None:
    kb = _kb()
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, kind, payload)


def _key(path: str) -> str:
    """One spelling per file for comparing: Windows paths may differ in case only (C:\\Users, c:\\users)."""
    return os.path.normcase(path)


def _file_event(path: str, **more: Any) -> dict:
    return {"path": path, "key": _key(path), **more}


def _files(conn, task) -> list[str]:
    """Files the task holds: written (``crema_file``) and not handed over since (``crema_let_go``)."""
    held: dict[str, str] = {}
    for event in _kb().list_events(conn, task.id):
        payload = event.payload or {}
        if event.kind == "crema_file":
            held[payload["key"]] = payload["path"]
        elif event.kind == "crema_let_go":
            held.pop(payload.get("key"), None)
    return list(held.values())


def _holds(conn, task, path: str) -> bool:
    return _key(path) in {_key(p) for p in _files(conn, task)}


def _holders(conn, path: str, session_id: str) -> list:
    """Open tasks of other chats that hold ``path``."""
    kb = _kb()
    rows = conn.execute("SELECT DISTINCT task_id FROM task_events WHERE kind = 'crema_file' "
                        "AND json_extract(payload, '$.key') = ?", (_key(path),)).fetchall()
    tasks = [kb.get_task(conn, row[0]) for row in rows]
    return [t for t in tasks if t and t.assignee == ASSIGNEE and t.status not in _OPEN
            and t.session_id != session_id and _holds(conn, t, path)]


def _linked(conn, parent_id: str, child_id: str) -> bool:
    return parent_id in _kb().parent_ids(conn, child_id)


def _together(conn, task, other) -> bool:
    return any((e.payload or {}).get("with") == other.id for e in _kb().list_events(conn, task.id)
               if e.kind == "crema_together")


def _note(conn, task_id: str, text: str) -> None:
    """A note the chat reads at the start of its next turn (it does not start a turn)."""
    _add_event(conn, task_id, "crema_note", {"text": text})


# ── the run API, sessions and Git ────────────────────────────────────────────


def _session_db():
    return _api._ensure_session_db()


def _chat(session_id: str) -> Optional[str]:
    """The chat a session belongs to: its first session (agent-client-<chat id>, the app's name for it), which
    a long chat keeps across the sessions compression starts; None for a session that is not a Crema chat."""
    if not session_id or _api is None:
        return None
    if session_id.startswith(CHAT_PREFIX):
        return session_id
    try:
        root = _session_db().get_conversation_root(session_id)
    except Exception:
        logger.warning("crema turns: no conversation root for %s", session_id, exc_info=True)
        return None
    return root if root.startswith(CHAT_PREFIX) else None


def _replying(chat: str) -> bool:
    from gateway.platforms.api_server_run_idempotency import TERMINAL_STATUSES

    statuses = list(getattr(_api, "_run_statuses", {}).values())
    return any(s.get("status") not in TERMINAL_STATUSES and _chat(s.get("session_id") or "") == chat for s in statuses)


def _title(session_id: str) -> str:
    try:
        title = (_session_db().get_session(session_id) or {}).get("title")
    except Exception:
        title = None
    return title or session_id.removeprefix(CHAT_PREFIX)[:8]


def _requests(chat: str) -> list[str]:
    db = _session_db()
    latest = db.resolve_resume_session_id(chat) or chat  # a long chat's newest session
    messages = db.get_messages_as_conversation(latest, include_ancestors=True, repair_alternation=True)
    asked = [m.get("content") for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str)]
    return [text[:500] for text in reversed(asked[-3:])]


def _git(cwd: str, *args: str) -> Optional[str]:
    try:
        run = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return run.stdout if run.returncode == 0 else None


def _folder(path: str) -> str:
    folder = Path(path).parent
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    return str(folder)


def _uncommitted(path: str) -> bool:
    """True when Git has changes of the file not committed (or cannot say inside a work tree); False
    outside Git."""
    folder = _folder(path)
    if (_git(folder, "rev-parse", "--is-inside-work-tree") or "").strip() != "true":
        return False
    status = _git(folder, "status", "--porcelain", "--", path)
    return status is None or bool(status.strip())


def _dirty_files(cwd: str) -> Optional[dict[str, float]]:
    """Changed files of the Git work tree at ``cwd`` with their modified times; None outside Git."""
    root = (_git(cwd, "rev-parse", "--show-toplevel") or "").strip()
    listing = _git(cwd, "status", "--porcelain", "-z", "--untracked-files=all") if root else None
    if listing is None:
        return None
    found: dict[str, float] = {}
    for entry in listing.split("\0"):
        if len(entry) > 3:
            path = str(Path(root.strip(), entry[3:]).resolve())
            try:
                found[path] = os.path.getmtime(path)
            except OSError:
                found[path] = -1.0  # deleted
    return found


# ── the judge ────────────────────────────────────────────────────────────────


def _describe(conn, task, session_id: str) -> str:
    files = _files(conn, task) if task else []
    return (f"User's requests: {' / '.join(_requests(session_id)) or '(none)'}\n"
            f"Replying right now: {_replying(session_id)}\nFiles changed: {', '.join(files) or '(none)'}")


def _intended_change(tool_name: str, args: dict) -> str:
    if tool_name == "write_file":
        return "Replaces the whole file with:\n" + str(args.get("content", ""))[:CONTEXT_CHARS]
    if args.get("mode") == "patch":
        return str(args.get("patch", ""))[:CONTEXT_CHARS]
    return f"Replaces:\n{str(args.get('old_string', ''))[:CONTEXT_CHARS // 2]}\nwith:\n{str(args.get('new_string', ''))[:CONTEXT_CHARS // 2]}"


def judge(conn, holder, session_id: str, path: str, tool_name: str, args: dict) -> dict:
    """One auxiliary model call on the chat's own account; one retry that names the contract error."""
    import time

    from agent.auxiliary_client import call_llm

    diff = _git(_folder(path), "diff", "--", path)
    body = (f"Shared file: {path}\n\nConversation A\n{_describe(conn, holder, holder.session_id)}\n"
            f"A's uncommitted changes to the file:\n{(diff if diff is not None else '(not in Git)')[:CONTEXT_CHARS] or '(none)'}"
            f"\n\nConversation B\n{_describe(conn, _open_task(conn, session_id), session_id)}\n"
            f"The change B is about to make:\n{_intended_change(tool_name, args)}")
    messages = [{"role": "system", "content": JUDGE_PROMPT}, {"role": "user", "content": body}]
    deadline = time.monotonic() + JUDGE_BUDGET_S
    problem = ""
    for _ in range(2):
        left = deadline - time.monotonic()
        if left < 1:
            break
        try:
            response = call_llm(task="crema_turns", messages=messages, max_tokens=400, temperature=None, timeout=left)
            text = (response.choices[0].message.content or "").strip()
        except Exception as exc:
            raise JudgeError(f"{type(exc).__name__}: {exc}") from exc
        try:
            reply = json.loads(text.removeprefix("```json").removesuffix("```").strip())
            if not isinstance(reply, dict) or reply.get("decision") not in DECISIONS or not str(reply.get("reason") or "").strip():
                raise ValueError(f"needs decision in {list(DECISIONS)} and a reason")
            return {"decision": reply["decision"], "reason": str(reply["reason"]).strip()}
        except ValueError as exc:
            problem = f"{exc}: {text[:200]}"
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"That reply broke the format ({exc}). Reply with the JSON only."}]
    raise JudgeError(problem or "no time left")


# ── before and after a tool ──────────────────────────────────────────────────


def _written_paths(tool_name: str, args: dict, task_id: str) -> list[str]:
    from tools.file_tools import _collect_v4a_header_paths, _resolve_or_none

    paths = [args["path"]] if args.get("path") else []
    if tool_name == "patch" and args.get("mode") == "patch" and args.get("patch"):
        collected = _collect_v4a_header_paths(args["patch"])
        if not isinstance(collected, str):
            paths += collected[0]
    resolved = (r for r in (_resolve_or_none(p, task_id) for p in paths) if r)
    return list({_key(r): r for r in resolved}.values())


def _terminal_cwd(args: dict, task_id: str) -> Optional[str]:
    from tools.file_tools import _resolve_or_none

    return args.get("workdir") or _resolve_or_none(".", task_id)


def before_tool(tool_name: str = "", args: Optional[dict] = None, session_id: str = "", task_id: str = "",
                tool_call_id: str = "", **_: Any) -> Optional[dict]:
    chat = _chat(session_id) if tool_name in (*WRITE_TOOLS, "terminal") else None
    if not chat:
        return None
    args, task_id = args or {}, task_id or session_id  # the live session resolves relative paths
    if tool_name == "terminal":
        cwd = _terminal_cwd(args, task_id)
        state = _dirty_files(cwd) if cwd else None
        if state is not None and tool_call_id:
            _before_terminal[tool_call_id] = state
        return None
    for path in _written_paths(tool_name, args, task_id):
        with _path_locks[_key(path)]:
            refusal = _turn_for(path, chat, tool_name, args)
        if refusal:
            return {"action": "block", "message": refusal}
    return None


def _turn_for(path: str, session_id: str, tool_name: str, args: dict) -> Optional[str]:
    """None when this chat may write ``path`` now; otherwise why it waits (also recorded on the board)."""
    conn = _board()
    try:
        for holder in _holders(conn, path, session_id):
            me = _task_for(conn, session_id)
            if _linked(conn, me.id, holder.id) or _together(conn, me, holder):
                continue
            if _linked(conn, holder.id, me.id):
                return _wait(conn, me, holder, path, "", ask=False)
            if not _replying(holder.session_id) and not _uncommitted(path):
                _add_event(conn, holder.id, "crema_let_go", _file_event(path, to=session_id))
                _note(conn, holder.id, f"[Crema] The chat '{_title(session_id)}' went on changing {path}, which this "
                                       "chat changed before. Read it again before changing it.")
                continue
            try:
                verdict = judge(conn, holder, session_id, path, tool_name, args)
            except JudgeError as exc:
                logger.warning("crema turns: judge failed for %s: %s", path, exc)
                return _wait(conn, me, holder, path, f"The order could not be judged ({exc}).", ask=True)
            decision, reason = verdict["decision"], verdict["reason"]
            _add_event(conn, me.id, "crema_judged", {"path": path, "with": holder.id, **verdict})
            if decision == "together":
                for task, other in ((me, holder), (holder, me)):
                    _add_event(conn, task.id, "crema_together", {"with": other.id, "path": path, "reason": reason})
                continue
            if decision == "ask_user":
                return _wait(conn, me, holder, path, reason, ask=True)
            first, second = (holder, me) if decision == "a_first" else (me, holder)
            try:
                _kb().link_tasks(conn, first.id, second.id)
            except ValueError as exc:  # the other order is already on the board: a cycle
                return _wait(conn, me, holder, path, f"{reason} ({exc})", ask=True)
            if decision == "a_first":
                return _wait(conn, me, holder, path, reason, ask=False)
            _note(conn, holder.id, f"[Crema] The chat '{_title(session_id)}' changes {path} first: {reason} Do not "
                                   f"change {path} until Crema says it is done; other work can go on.")
        return None
    finally:
        conn.close()


def _wait(conn, me, holder, path: str, reason: str, *, ask: bool) -> str:
    _add_event(conn, me.id, "crema_waiting", {"holder": holder.id, "holder_session": holder.session_id,
                                              "path": path, "reason": reason, "ask": ask})
    other = _title(holder.session_id)
    why = f"the user is asked whether this chat or '{other}' changes it first" if ask else f"the chat '{other}' changes it first"
    return " ".join(part for part in (
        f"Not written: {path} — {why}.", reason, "Crema starts this chat again when it is this chat's turn.",
        f"Until then do not change {path} in any other way (terminal, scripts). Finish other work that does not "
        f"touch it, tell the user this chat is waiting for '{other}', and end your turn.") if part)


def after_tool(tool_name: str = "", args: Optional[dict] = None, result: Any = None, session_id: str = "",
               task_id: str = "", tool_call_id: str = "", **_: Any) -> None:
    chat = _chat(session_id) if tool_name in WRITE_TOOLS or tool_call_id in _before_terminal else None
    if not chat:
        return
    args, task_id, session_id = args or {}, task_id or session_id, chat
    if tool_name in WRITE_TOOLS:
        try:
            if json.loads(result).get("error"):
                return
        except (TypeError, ValueError, AttributeError):
            return
        paths = _written_paths(tool_name, args, task_id)
    elif tool_name == "terminal" and tool_call_id in _before_terminal:
        before = _before_terminal.pop(tool_call_id)
        cwd = _terminal_cwd(args, task_id)
        after = (_dirty_files(cwd) if cwd else None) or {}
        paths = [p for p, mtime in after.items() if before.get(p) != mtime]
    else:
        return
    if not paths:
        return
    conn = _board()
    try:
        me = _task_for(conn, session_id)
        for path in paths:
            if not _holds(conn, me, path):
                _add_event(conn, me.id, "crema_file", _file_event(path))
        if tool_name == "terminal":
            clashes = [(p, h) for p in paths for h in _holders(conn, p, session_id)
                       if not _linked(conn, me.id, h.id) and not _together(conn, me, h)]
            for path, holder in clashes:
                _note(conn, holder.id, f"[Crema] A command in the chat '{_title(session_id)}' changed {path}, which "
                                       "this chat is changing. Read it again before changing it.")
            if clashes:
                _after_terminal[tool_call_id] = "[Crema] This command changed files another chat is changing: " + ", ".join(
                    f"{p} ('{_title(h.session_id)}')" for p, h in clashes) + ". Tell the user; do not undo the other chat's work."
    finally:
        conn.close()


def tool_result(tool_name: str = "", result: Any = None, tool_call_id: str = "", **_: Any) -> Optional[str]:
    notice = _after_terminal.pop(tool_call_id, None) if tool_name == "terminal" else None
    return f"{result}\n\n{notice}" if notice and isinstance(result, str) else None


def notes_for_turn(session_id: str = "", **_: Any) -> Optional[dict]:
    """The chat's unread notes, once, at the start of its turn."""
    session_id = _chat(session_id)
    if not session_id:
        return None
    conn = _board()
    try:
        kb = _kb()
        tasks = kb.list_tasks(conn, assignee=ASSIGNEE, session_id=session_id, include_archived=True)
        events = [e for t in tasks for e in kb.list_events(conn, t.id)]
        read = max((e.payload or {}).get("upto", 0) for e in events if e.kind == "crema_notes_read") if any(
            e.kind == "crema_notes_read" for e in events) else 0
        notes = sorted((e for e in events if e.kind == "crema_note" and e.id > read), key=lambda e: e.id)
        if not notes:
            return None
        _add_event(conn, _task_for(conn, session_id).id, "crema_notes_read", {"upto": notes[-1].id})
        return {"context": "\n".join((e.payload or {}).get("text", "") for e in notes)}
    finally:
        conn.close()


# ── what the app reads and does ──────────────────────────────────────────────


def _last(events, kinds):
    found = [e for e in events if e.kind in kinds]
    return found[-1] if found else None


def settle() -> None:
    """Complete the task of each chat others wait on once it is not replying and nothing of its files is
    uncommitted; the board promotes the waiting chats. A chat left with uncommitted files is told once."""
    conn = _board()
    try:
        kb = _kb()
        for task in [t for t in kb.list_tasks(conn, assignee=ASSIGNEE) if t.status not in _OPEN]:
            waiting = [c for c in kb.child_ids(conn, task.id) if kb.get_task(conn, c).status not in _OPEN]
            if not waiting or _replying(task.session_id):
                continue
            files = _files(conn, task)
            dirty = [p for p in files if _uncommitted(p)]
            if dirty:
                events = kb.list_events(conn, task.id)
                if not _last(events, ("crema_nudged",)):
                    _add_event(conn, task.id, "crema_nudged", {"files": dirty})
                    _note(conn, task.id, "[Crema] Another chat waits for " + ", ".join(dirty) + ". When this chat's "
                                         "work on them is finished, commit them so the other chat can go on.")
                continue
            commits = sorted({head.strip() for head in (_git(_folder(p), "rev-parse", "--short", "HEAD") for p in files) if head})
            kb.complete_task(conn, task.id, result=f"files: {', '.join(files) or '-'}; commits: {', '.join(commits) or '-'}",
                             metadata={"files": files, "commits": commits})
        kb.recompute_ready(conn)
    finally:
        conn.close()


def state() -> dict:
    """Chats that wait (and why), chats the user must order, and chats whose turn came (to start again)."""
    conn = _board()
    try:
        kb = _kb()
        waiting, ask, wake = [], [], []
        for task in [t for t in kb.list_tasks(conn, assignee=ASSIGNEE) if t.status not in _OPEN]:
            events = kb.list_events(conn, task.id)
            wait = _last(events, ("crema_waiting", "crema_woken"))
            if not wait or wait.kind == "crema_woken":
                continue
            info = dict(wait.payload or {})
            info.update(session_id=task.session_id, holder_title=_title(info.get("holder_session", "")))
            parents = [kb.get_task(conn, p) for p in kb.parent_ids(conn, task.id)]
            ahead = [p for p in parents if p.status not in _OPEN]
            holder = info.get("holder") or ""
            answered = _last(events, ("crema_waiting", "crema_released")).kind == "crema_released" or any(
                _linked(conn, a, b) for a, b in ((task.id, holder), (holder, task.id)))
            if info.get("ask") and not answered:
                ask.append(info)
            elif ahead:  # the chat it waits for now (the first holder may have finished meanwhile)
                info.update(holder=ahead[0].id, holder_session=ahead[0].session_id, holder_title=_title(ahead[0].session_id))
                waiting.append(info)
            else:
                done = [p for p in parents if p.status == "done"]
                info["after"] = [{"title": _title(p.session_id), "result": p.result} for p in done]
                wake.append(info)
        return {"waiting": waiting, "ask": ask, "wake": wake}
    finally:
        conn.close()


def woken(session_id: str) -> None:
    conn = _board()
    try:
        task = _open_task(conn, session_id)
        if task:
            _add_event(conn, task.id, "crema_woken", {})
    finally:
        conn.close()


def order(first_session: str, second_session: str) -> None:
    """The user's order: ``first_session`` changes the files first (replaces the opposite order)."""
    conn = _board()
    try:
        kb = _kb()
        first, second = _task_for(conn, first_session), _task_for(conn, second_session)
        kb.unlink_tasks(conn, second.id, first.id)
        kb.link_tasks(conn, first.id, second.id)
        _add_event(conn, first.id, "crema_ordered", {"before": second.id})
    finally:
        conn.close()


def release(session_id: str) -> None:
    """The user lets the chat go on without waiting: its orders are removed and the files it waits for
    are handed over to it."""
    conn = _board()
    try:
        kb = _kb()
        task = _open_task(conn, session_id)
        if not task:
            return
        wait = _last(kb.list_events(conn, task.id), ("crema_waiting",))
        for parent in kb.parent_ids(conn, task.id):
            kb.unlink_tasks(conn, parent, task.id)
        payload = (wait.payload or {}) if wait else {}
        holder = kb.get_task(conn, payload["holder"]) if payload.get("holder") else None
        if holder and _holds(conn, holder, payload["path"]):
            _add_event(conn, holder.id, "crema_let_go", _file_event(payload["path"], to=session_id))
            _note(conn, holder.id, f"[Crema] The user let the chat '{_title(session_id)}' change {payload['path']} "
                                   "without waiting. Read it again before changing it.")
        _add_event(conn, task.id, "crema_released", {})
    finally:
        conn.close()
