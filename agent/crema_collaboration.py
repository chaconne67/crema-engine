"""Request-scoped collaboration records stored in the existing account Kanban board."""
from __future__ import annotations

import asyncio
import uuid
import hashlib
import unicodedata
import json
from typing import Any


def _events(conn, session_id: str) -> list[dict]:
    from agent import crema_file_turns as turns
    return [e.payload or {} for e in turns._session_events(conn, session_id)
            if e.kind == "crema_note" and (e.payload or {}).get("work")]


def _all_work(conn) -> list[dict]:
    return [work for _, work in _all_work_events(conn)]


def _all_work_events(conn) -> list[tuple[int, dict]]:
    from agent import crema_file_turns as turns
    kb = turns._kb()
    latest = {}
    for task in kb.list_tasks(conn, assignee=turns.ASSIGNEE, include_archived=True):
        for event in kb.list_events(conn, task.id):
            work = (event.payload or {}).get("work")
            if not work or work.get("direction") == "outbound" or work.get("kind") == "result":
                continue
            key = (work.get("request_id"), work.get("message_id"))
            if not (key[0] and key[1]):
                continue
            prior = latest.get(key)
            if prior is None:
                latest[key] = (event.id, event.id, work)
            elif event.id > prior[1]:
                latest[key] = (prior[0], event.id, work)
    # Order by the message's first durable insertion, not the last status update. A late claim or
    # report for an old message must not make that old work newer than a later revision/request.
    return [(created_id, work) for created_id, _, work in sorted(latest.values(), key=lambda item: item[0])]


def _terminal_requests(conn) -> set[str]:
    latest = {}
    for work in _all_work(conn):
        request_id = work.get("request_id")
        if request_id and work.get("kind") in {"request", "review"}:
            latest[request_id] = work
    return {request_id for request_id, work in latest.items()
            if work.get("status") in {"completed", "failed", "cancelled"}}


def _child_work(conn, request_id: str) -> list[dict]:
    return [work for work in _all_work(conn) if work.get("parent_request_id") == request_id
            and work.get("kind") != "result"]


def _question_cycle(conn, sender: str, recipient: str, request_id: str, parent_request_id: str, text: str) -> bool:
    """Stop a repeated unanswered question in either direction without spending a model call."""
    scope = parent_request_id or request_id
    normalized = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    if not scope or not normalized:
        return False
    events = _all_work_events(conn)
    for question_id, question in events:
        if (question.get("kind") != "question" or question.get("status") != "waiting"
                or not (question.get("request_id") == scope or question.get("parent_request_id") == scope)
                or {question.get("sender"), question.get("recipient")} != {sender, recipient}
                or " ".join(unicodedata.normalize("NFKC", str(question.get("text") or ""))
                            .casefold().split()) != normalized):
            continue
        answered = any(event_id > question_id and work.get("kind") == "answer"
                       and (work.get("request_id") == question.get("request_id")
                            or work.get("parent_request_id") == question.get("request_id")
                            or work.get("request_id") == scope or work.get("parent_request_id") == scope)
                       and {work.get("sender"), work.get("recipient")} == {sender, recipient}
                       for event_id, work in events)
        if not answered:
            return True
    return False


def _review_is_latest(conn, work: dict) -> bool:
    reviews = [candidate for _, candidate in _all_work_events(conn)
               if candidate.get("request_id") == work.get("request_id")
               and candidate.get("kind") == "review" and candidate.get("sender") == work.get("sender")
               and candidate.get("recipient") == work.get("recipient")]
    return bool(reviews and reviews[-1].get("message_id") == work.get("message_id")
                and reviews[-1].get("revision") == work.get("revision"))


def _aggregate_parent_status(conn, request_id: str, own_status: str) -> tuple[str, list[dict]]:
    children = _child_work(conn, request_id)
    pending = [w for w in children if w.get("status") != "completed"]
    if not pending:
        return own_status, children
    if any(w.get("status") == "waiting" for w in pending):
        return "waiting", children
    return "partial", children


def _result_payload(work: dict) -> tuple[str, dict | None]:
    """Send review evidence to the writer without forcing the reviewer model on its next run."""
    review = work.get("review")
    if not review:
        return str(work.get("result") or ""), None
    summary = {key: review[key] for key in
               ("status", "reason", "verdict", "findings", "evidence", "reviewed_revision", "revision",
                "reviewer", "round") if key in review}
    summary["reviewed_model"] = review.get("model") or work.get("actual_model", "")
    if review.get("previous"):
        summary["previous"] = {key: review["previous"].get(key) for key in ("revision", "findings")
                               if key in review["previous"]}
        if review.get("previous_evidence"):
            summary["previous"]["evidence"] = review["previous_evidence"]
    return json.dumps({"result": work.get("result") or "", "review": summary}, ensure_ascii=False), summary


def _refresh_parent(conn, request_id: str, child: dict) -> None:
    """Propagate a child's recorded change through its active chain of delegation."""
    seen = set()
    while request_id and request_id not in seen:
        seen.add(request_id)
        updated = _refresh_one_parent(conn, request_id, child)
        if updated is None:
            break
        child = updated
        request_id = updated.get("parent_request_id")


def _refresh_one_parent(conn, request_id: str, child: dict) -> dict | None:
    from agent import crema_file_turns as turns
    parents = [w for w in _all_work(conn) if w.get("request_id") == request_id
               and w.get("recipient") == child.get("sender") and w.get("direction") != "outbound"
               and w.get("kind") != "result"]
    if not parents:
        return
    parent = parents[-1]
    if parent.get("status") not in {"completed", "partial", "waiting"}:
        return
    own_status = parent.get("own_status", parent.get("status", "partial"))
    new_status, children = _aggregate_parent_status(conn, request_id, own_status)
    child_rows = [{"request_id": work.get("request_id"), "message_id": work.get("message_id"),
                   "status": work.get("status"), "kind": work.get("kind")} for work in children]
    if parent.get("status") == new_status and parent.get("children") == child_rows:
        return
    updated = {**parent, "status": new_status, "children": child_rows}
    from_task = turns._task_for(conn, parent["recipient"])
    to_task = turns._task_for(conn, parent["sender"])
    parent_authorized = _authorized(parent["recipient"], parent, conn)
    _append_locked(conn, from_task, to_task, updated)
    if not parent_authorized:
        return
    digest = hashlib.sha256(str(child.get("result") or "").encode()).hexdigest()
    message_id = uuid.uuid5(uuid.NAMESPACE_URL,
                            f"crema-parent:{request_id}:{child.get('attempt_id')}:{new_status}:{digest}").hex
    delivery_text, review_result = _result_payload(updated)
    response = {"request_id": request_id, "attempt_id": parent["attempt_id"], "message_id": message_id,
                "project_id": parent["project_id"], "sender": parent["recipient"],
                "recipient": parent["sender"], "mode": "queue", "kind": "result", "status": "queued",
                "text": delivery_text, "result": delivery_text,
                "source_message_id": parent["message_id"], "source_run_id": parent.get("run_id"),
                "source_status": new_status, "children": child_rows,
                **({"parent_request_id": parent["parent_request_id"]}
                   if parent.get("parent_request_id") else {}),
                **({"review_result": review_result} if review_result else {})}
    response_sender = turns._task_for(conn, parent["recipient"])
    response_recipient = turns._task_for(conn, parent["sender"])
    _append_locked(conn, response_recipient, response_sender, response)
    return updated


def _cancel_descendants(conn, request_id: str) -> None:
    """Request stop for linked descendants and mark cancelled only from terminal receipts."""
    from agent import crema_file_turns as turns
    queue, seen = [request_id], set()
    while queue:
        parent = queue.pop()
        if parent in seen:
            continue
        seen.add(parent)
        for work in _child_work(conn, parent):
            child_id = work.get("request_id")
            if child_id and child_id not in seen:
                queue.append(child_id)
            if work.get("status") in {"completed", "failed", "cancelled"}:
                continue
            receipt = _run_receipt(work)
            run_id = str(work.get("run_id") or (receipt or {}).get("run_id") or "")
            terminal_status = str((receipt or {}).get("status") or "")
            reason = "상위 요청이 중단됐습니다. 하위 실행 종료를 확인하고 있습니다."
            if work.get("status") == "queued" and not run_id and not receipt:
                # No claim or engine run exists yet; blocking future claim is sufficient.
                status = "cancelled"
                reason = "상위 요청이 중단되어 시작 전 하위 요청을 취소했습니다."
            elif receipt and str(receipt.get("session_id") or "") == str(work.get("recipient") or "") \
                    and run_id and (not work.get("run_id") or str(work["run_id"]) == run_id):
                if terminal_status in {"cancelled", "interrupted"}:
                    status = "cancelled"
                    reason = "상위 요청이 중단되어 하위 실행도 중단됐습니다."
                elif terminal_status in {"completed", "failed"}:
                    # The run ended before the stop arrived; preserve its actual outcome.
                    status = terminal_status
                    reason = "하위 실행이 상위 요청 중단 전에 끝났습니다."
                else:
                    try:
                        stop_requested = _request_work_run_stop(turns._api, {**work, "run_id": run_id})
                    except Exception:
                        stop_requested = False
                    if stop_requested:
                        status = "stopping"
                        reason = "상위 요청이 중단되어 하위 실행 종료를 기다리고 있습니다."
                    else:
                        status = "check_required"
                        reason = "하위 실행 중단을 요청하지 못했습니다. 상태를 확인해야 합니다."
            else:
                status = "check_required"
                reason = "하위 실행의 종료 증거를 확인할 수 없습니다. 상태를 확인해야 합니다."
            recipient_task = turns._task_for(conn, work.get("recipient", ""))
            sender_task = turns._task_for(conn, work.get("sender", ""))
            updated = {**work, "status": status, "waiting_reason": reason,
                       **({"run_id": run_id} if run_id else {})}
            _append_locked(conn, recipient_task, sender_task, updated)


def _request_work_run_stop(api, work: dict) -> bool:
    """Schedule the common API stop handler on its owning loop without blocking the board txn."""
    if not api or not work.get("run_id"):
        return False
    loop = getattr(api, "_work_loop", None)
    if loop is None or loop.is_closed():
        return False
    from gateway.platforms import api_server, api_server_runs
    coroutine = api_server_runs.stop_work_run(api, work, _api_server=api_server)
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    if current_loop is loop:
        loop.create_task(coroutine)
    else:
        asyncio.run_coroutine_threadsafe(coroutine, loop)
    return True


def reconcile_stopping() -> None:
    """Refresh stopping work only when its exact engine receipt has become terminal."""
    from agent import crema_file_turns as turns
    conn = turns._board()
    try:
        for work in _all_work(conn):
            if work.get("status") not in {"stopping", "check_required"} or not work.get("run_id"):
                continue
            receipt = _run_receipt(work)
            if (not receipt or str(receipt.get("run_id") or "") != str(work["run_id"])
                    or str(receipt.get("session_id") or "") != str(work.get("recipient") or "")):
                continue
            run_status = str(receipt.get("status") or "")
            if run_status in {"cancelled", "interrupted"}:
                status, reason = "cancelled", "하위 엔진 실행이 종료된 것을 확인했습니다."
            elif run_status in {"completed", "failed"}:
                status, reason = run_status, "하위 엔진 실행이 중단 요청 전에 끝난 것을 확인했습니다."
            else:
                continue
            result = receipt.get("output") or receipt.get("error") or work.get("result") or ""
            if work.get("status") == status and work.get("result") == result:
                continue
            updated = {**work, "status": status, "waiting_reason": reason, "result": result,
                       "reported_run_status": run_status}
            _append_locked(conn, turns._task_for(conn, work.get("recipient", "")),
                           turns._task_for(conn, work.get("sender", "")), updated)
    finally:
        conn.close()


def create(session_id: str, to: str, text: str, *, mode: str = "queue", kind: str = "request",
           request_id: str = "", attempt_id: str = "", message_id: str = "",
           model: str = "", revision: Any = None, requirements: str = "",
           parent_request_id: str = "", start: dict | None = None,
           target_request_id: str = "", target_attempt_id: str = "", target_message_id: str = "") -> dict:
    """Persist a work message after the existing addressed-chat check."""
    from agent import crema_file_turns as turns
    if mode not in {"queue", "steer", "interrupt"}:
        return {"error": "mode must be queue, steer or interrupt"}
    if kind not in {"request", "question", "answer", "result", "review"}:
        return {"error": "unsupported collaboration message kind"}
    text = str(text or "").strip()
    if not text or len(text) > turns.MESSAGE_MAX:
        return {"error": "work text is empty or too long"}
    me, target, problem = _resolve_target(session_id, to)
    if problem:
        return {"error": problem}
    rows = turns._directory()
    sender = next((r for r in rows if r["session"] == me), None)
    if not sender or sender.get("collaboration") != 1 or target.get("collaboration") != 1:
        return {"error": "request-scoped collaboration is not enabled for both chats"}
    project_id = str(sender.get("project_id") or "")
    if not project_id or project_id != str(target.get("project_id") or ""):
        return {"error": "the chats do not belong to the same identified project"}
    model = str(model or "").strip()
    if model and model not in turns.models():
        return {"error": f"No model '{model}' in the user's model menu ({', '.join(turns.models()) or 'none'})."}
    conn = turns._board()
    try:
        target_work = None
        if mode == "interrupt":
            target_work, problem = _interrupt_target(conn, me, target["session"], target_request_id,
                                                     target_attempt_id, target_message_id)
            if problem:
                return {"error": problem}
        elif any((target_request_id, target_attempt_id, target_message_id)):
            return {"error": "target work fields are only valid for interrupt mode"}
        if not _works_with(me, target["session"]):
            grants = _active_grants(conn, me, target["session"], project_id)
            if request_id:
                if not any(g.get("request_id") == request_id for g in grants):
                    return {"error": "no active user authorization for that request"}
            elif len(grants) == 1:
                request_id = grants[0]["request_id"]
            elif len(grants) > 1:
                return {"error": "choose the authorized request_id for this chat"}
            else:
                return {"error": "these chats need a user-authorized collaboration request"}
        if not parent_request_id:
            active_parents = _active_parent_requests(conn, me)
            if request_id and request_id in active_parents:
                # A reply or follow-up question on the assigned request is not its own child.
                parent_request_id = ""
            elif len(active_parents) == 1:
                parent_request_id = active_parents[0]
            elif len(active_parents) > 1:
                return {"error": "choose parent_request_id for the task this subtask belongs to"}
        if parent_request_id and not _owns_parent_request(conn, me, parent_request_id):
            return {"error": "parent_request_id is not an active request assigned to this chat"}
        if parent_request_id and request_id and parent_request_id == request_id:
            return {"error": "a work request cannot be its own parent"}
        if kind == "question" and _question_cycle(conn, me, target["session"],
                                                   request_id, parent_request_id, text):
            return {"error": "this question is already waiting in this work; report the mutual wait to the requester"}
    finally:
        conn.close()
    request_id = request_id or str(uuid.uuid4())
    review = None
    if kind == "review":
        from agent.crema_work_review import prepare_review, review_prompt
        previous = None
        conn = turns._board()
        try:
            prior = [candidate for _, candidate in _all_work_events(conn)
                     if candidate.get("request_id") == request_id and candidate.get("sender") == me
                     and candidate.get("kind") == "review" and candidate.get("review")]
            previous = prior[-1]["review"] if prior else None
        finally:
            conn.close()
        try:
            review = prepare_review(revision, requirements=requirements, model=model,
                                    reviewer=target["session"], previous=previous)
        except (ValueError, KeyError) as exc:
            return {"error": str(exc)}
        revision = review["revision"]
        text = review_prompt(review)
    work = {"request_id": request_id, "attempt_id": attempt_id or str(uuid.uuid4()),
            "message_id": message_id or str(uuid.uuid4()),
            "project_id": project_id,
            "sender": me, "recipient": target["session"], "mode": mode, "kind": kind,
            "status": "queued", "text": text,
            **({"parent_request_id": parent_request_id} if parent_request_id else {}),
            **({"model": model} if model else {}), **({"revision": revision} if revision is not None else {}),
            **({"start": start} if start else {}),
            **({"target_request_id": target_request_id, "target_attempt_id": target_attempt_id,
                "target_message_id": target_message_id, "target_run_id": target_work["run_id"]}
               if target_work else {}),
            **({"review": review} if review else {})}
    conn = turns._board()
    try:
        recipient_task = turns._task_for(conn, target["session"])
        sender_task = turns._task_for(conn, me)
        kb = turns._kb()
        with kb.write_txn(conn):
            existing = [p["work"] for p in _events(conn, target["session"])
                        if p["work"].get("message_id") == work["message_id"]]
            if existing:
                old = existing[-1]
                if (old.get("request_id"), old.get("attempt_id")) != (request_id, work["attempt_id"]):
                    return {"error": "message_id is already used by another work attempt"}
                return {"ok": True, "work": old, "duplicate": True}
            payload = {"text": text, "message": text, "sender": me,
                       "sender_chat": (sender or {}).get("id", ""), "sender_title": turns._title(me),
                       "sender_project": work["project_id"], "handed": True, "work": work}
            _append_locked(conn, recipient_task, sender_task, work, recipient_payload=payload)
    finally:
        conn.close()
    return {"ok": True, "work": work}


def _interrupt_target(conn, sender: str, recipient: str, request_id: str,
                      attempt_id: str, message_id: str, *, allow_stopped: bool = False) -> tuple[dict | None, str | None]:
    if not all((request_id, attempt_id, message_id)):
        return None, "interrupt requires the exact target request, attempt and message IDs"
    target = next((work for work in _all_work(conn)
                   if work.get("request_id") == request_id and work.get("attempt_id") == attempt_id
                   and work.get("message_id") == message_id and work.get("sender") == sender
                   and work.get("recipient") == recipient), None)
    if not target:
        return None, "interrupt target is not an active work item assigned by this chat to that recipient"
    receipt = _run_receipt(target)
    if (not receipt or str(receipt.get("session_id") or "") != recipient or not receipt.get("run_id")
            or (target.get("run_id") and str(target["run_id"]) != str(receipt["run_id"]))):
        return None, "interrupt target has no matching durable run"
    if target.get("status") in {"completed", "failed", "cancelled"}:
        if (allow_stopped and target.get("status") == "cancelled"
                and receipt.get("status") in {"cancelled", "interrupted"}):
            return {**target, "run_id": receipt["run_id"]}, None
        return None, "interrupt target work is already terminal"
    if receipt.get("status") in {"completed", "failed", "cancelled", "interrupted"}:
        return None, "interrupt target run is already terminal"
    return {**target, "run_id": receipt["run_id"]}, None


def send_compatible(session_id: str, to: str, text: str, *, mode: str = "queue", kind: str = "request",
                    request_id: str = "", attempt_id: str = "", message_id: str = "",
                    target_request_id: str = "", target_attempt_id: str = "", target_message_id: str = "") -> dict:
    """Use request work only when both chats advertise the new protocol; preserve legacy callers."""
    from agent import crema_file_turns as turns
    me, target, problem = _resolve_target(session_id, to)
    if problem:
        return {"error": problem}
    rows = turns._directory()
    sender = next((row for row in rows if row["session"] == me), None)
    sender_new = bool(sender and sender.get("collaboration") == 1)
    target_new = target.get("collaboration") == 1
    sender_project, target_project = str((sender or {}).get("project_id") or ""), str(target.get("project_id") or "")
    if mode == "interrupt" and not (sender_new and target_new and sender_project and sender_project == target_project):
        return {"error": "interrupt requires collaboration-enabled chats in the same identified project"}
    if not sender_new or not sender_project:
        return turns.send(session_id, to, text)
    if not target_new:
        return {"error": "the receiving chat does not support request-scoped work; no new work was started"}
    if not target_project:
        return turns.send(session_id, to, text)
    if sender_project != target_project:
        return {"error": "the chats belong to different projects; no new work was started"}
    return create(session_id, to, text, mode=mode, kind=kind, request_id=request_id,
                  attempt_id=attempt_id, message_id=message_id, target_request_id=target_request_id,
                  target_attempt_id=target_attempt_id, target_message_id=target_message_id)


def collaborate(body: dict[str, Any], *, trusted_user: bool = False) -> dict:
    """Claim, validate and report a request attempt through append-only board events."""
    from agent import crema_file_turns as turns
    action, session = str(body.get("action") or ""), str(body.get("session_id") or "")
    request_id, attempt_id, message_id = (str(body.get(k) or "") for k in
                                          ("request_id", "attempt_id", "message_id"))
    if action in {"grant", "revoke"}:
        if not trusted_user:
            return {"ok": False, "error": "only an authenticated user may change collaboration grants"}
        return _grant(body, action)
    if action not in {"claim", "validate", "accepted", "report"}:
        return {"ok": False, "error": "unsupported collaboration action"}
    if not all((session, request_id, attempt_id, message_id)):
        return {"ok": False, "error": "session_id, request_id, attempt_id and message_id are required"}
    conn = turns._board()
    try:
        if action == "claim":
            return _claim(conn, session, request_id, attempt_id, message_id)
        notes = _events(conn, session)
        records = [p["work"] for p in notes if p["work"].get("request_id") == request_id
                   and p["work"].get("message_id") == message_id]
        if not records:
            return {"ok": False, "error": "collaboration message was not found for this session"}
        work = records[-1]
        if work.get("recipient") != session:
            return {"ok": False, "error": "collaboration message belongs to another session"}
        attempts = [w for w in records if w.get("attempt_id") == attempt_id and w.get("status") != "queued"]
        current = attempts[-1] if attempts else work
        if action == "validate":
            valid = _authorized(session, work, conn)
            if work.get("kind") == "review":
                valid = valid and _review_is_latest(conn, work)
            if work.get("mode") == "interrupt":
                target, problem = _interrupt_target(conn, work.get("sender", ""), session,
                                                    work.get("target_request_id", ""),
                                                    work.get("target_attempt_id", ""),
                                                    work.get("target_message_id", ""), allow_stopped=True)
                valid = valid and not problem and target.get("run_id") == work.get("target_run_id")
            if work.get("model") and str(body.get("model") or "") != work["model"]:
                valid = False
            if work.get("revision") is not None and body.get("revision") != work["revision"]:
                valid = False
            if str(body.get("project_id") or work.get("project_id") or "") != work.get("project_id"):
                valid = False
            return {"ok": True, "valid": valid}
        return _update_attempt(conn, session, request_id, attempt_id, message_id, action, body)
    finally:
        conn.close()


def _append_locked(conn, recipient_task, sender_task, work: dict, *, recipient_payload: dict | None = None) -> None:
    from agent import crema_file_turns as turns
    kb = turns._kb()
    kb._append_event(conn, recipient_task.id, "crema_note", recipient_payload or {"text": "", "work": work})
    if sender_task.id != recipient_task.id:
        kb._append_event(conn, sender_task.id, "crema_note", {"text": "", "work": {**work, "direction": "outbound"}})


def _claim(conn, session: str, request_id: str, attempt_id: str, message_id: str) -> dict:
    from agent import crema_file_turns as turns
    kb = turns._kb()
    recipient_task = turns._task_for(conn, session)
    with kb.write_txn(conn):
        notes = _events(conn, session)
        records = [p["work"] for p in notes if p["work"].get("request_id") == request_id
                   and p["work"].get("message_id") == message_id]
        if not records:
            return {"ok": False, "error": "collaboration message was not found for this session"}
        work, latest = records[0], records[-1]
        if work.get("recipient") != session:
            return {"ok": False, "error": "collaboration message belongs to another session"}
        attempts = [w for w in records if w.get("attempt_id") == attempt_id and w.get("status") != "queued"]
        if attempts:
            return _recover_claim(conn, recipient_task, attempts[-1], session)
        if latest.get("status") in {"claimed", "accepted", "stopping"}:
            return _recover_claim(conn, recipient_task, latest, session)
        if not _authorized(session, work, conn):
            return {"ok": True, "claimed": False, "work": {**latest, "status": "cancelled",
                                                                 "reason": "permission revoked"}}
        if work.get("kind") == "review" and not _review_is_latest(conn, work):
            stale = {**latest, "status": "cancelled", "stale_revision": True,
                     "reason": "a newer document revision is awaiting review"}
            sender_task = turns._task_for(conn, work.get("sender", ""))
            _append_locked(conn, recipient_task, sender_task, stale)
            return {"ok": True, "claimed": False, "work": stale}
        if work.get("mode") == "interrupt":
            target, problem = _interrupt_target(conn, work.get("sender", ""), session,
                                                work.get("target_request_id", ""),
                                                work.get("target_attempt_id", ""),
                                                work.get("target_message_id", ""), allow_stopped=True)
            if problem or target.get("run_id") != work.get("target_run_id"):
                return {"ok": True, "claimed": False,
                        "work": {**latest, "status": "cancelled", "reason": problem or "target run changed"}}
        sender_task = turns._task_for(conn, work.get("sender", ""))
        claimed = {**work, "attempt_id": attempt_id, "status": "claimed"}
        _append_locked(conn, recipient_task, sender_task, claimed)
        return {"ok": True, "claimed": True, "work": claimed}


def _recover_claim(conn, recipient_task, current: dict, session: str) -> dict:
    from agent import crema_file_turns as turns
    receipt = _run_receipt({**current, "recipient": session})
    run_id = current.get("run_id") or (receipt or {}).get("run_id")
    updated = dict(current)
    if run_id:
        updated["run_id"] = run_id
        if updated.get("status") in {"claimed", "check_required"}:
            updated["status"] = "accepted"
    elif updated.get("status") in {"claimed", "accepted", "stopping", "check_required"}:
        updated["status"] = "check_required"
    if updated != current:
        sender_task = turns._task_for(conn, current.get("sender", ""))
        _append_locked(conn, recipient_task, sender_task, updated)
    return {"ok": True, "claimed": False, **({"run_id": run_id} if run_id else {}), "work": updated}


def _update_attempt(conn, session: str, request_id: str, attempt_id: str, message_id: str,
                    action: str, body: dict) -> dict:
    from agent import crema_file_turns as turns
    kb = turns._kb()
    recipient_task = turns._task_for(conn, session)
    with kb.write_txn(conn):
        records = [p["work"] for p in _events(conn, session) if p["work"].get("request_id") == request_id
                   and p["work"].get("message_id") == message_id]
        if not records or records[0].get("recipient") != session:
            return {"ok": False, "error": "collaboration message was not found for this session"}
        work, latest = records[0], records[-1]
        if latest.get("attempt_id") != attempt_id:
            return {"ok": False, "error": "attempt is no longer current"}
        current = next((w for w in reversed(records) if w.get("attempt_id") == attempt_id), None)
        if not current or current.get("status") == "queued":
            return {"ok": False, "error": "attempt has not been claimed"}
        if action == "accepted":
            if body.get("run_id") is None:
                return {"ok": False, "error": "run_id is required"}
            if current.get("status") in {"accepted", "completed", "failed", "cancelled", "waiting"}:
                return {"ok": True}
            receipt = _run_receipt({**current, "recipient": session})
            if not receipt or str(receipt.get("run_id")) != str(body["run_id"]):
                return {"ok": False, "error": "run does not match the durable receipt for this work attempt"}
            actual_model = _receipt_model(receipt)
            updated = {**current, "status": "accepted", "run_id": receipt["run_id"],
                       **({"actual_model": actual_model} if actual_model else {})}
        else:
            if current.get("kind") == "review" and not _review_is_latest(conn, current):
                return {"ok": False, "error": "a newer document revision is awaiting review; this result cannot finalize it"}
            status = str(body.get("status") or "")
            if status not in {"completed", "waiting", "partial", "failed", "cancelled", "stopping"}:
                return {"ok": False, "error": "unsupported report status"}
            receipt = _run_receipt({**current, "recipient": session})
            if not receipt or str(receipt.get("run_id")) != str(body.get("run_id") or ""):
                return {"ok": False, "error": "run does not match the durable receipt for this work attempt"}
            receipt_status = str(receipt.get("status") or "")
            if current.get("reported_run_status") == receipt_status:
                return {"ok": True}
            if receipt_status in {"waiting_for_approval", "waiting_for_clarify"}:
                status = "waiting"
            elif receipt_status == "completed":
                if status not in {"completed", "waiting", "partial"}:
                    return {"ok": False, "error": "report status conflicts with the completed run"}
            elif receipt_status == "failed":
                status = "failed"
            elif receipt_status in {"cancelled", "interrupted"}:
                status = "cancelled"
            elif receipt_status in {"running", "stopping"} and status == "stopping" and receipt.get("shutdown_requested_at"):
                status = "stopping"
            else:
                return {"ok": False, "error": "the durable run has not reached a reportable state"}
            result = receipt.get("output") if isinstance(receipt.get("output"), str) else ""
            limit_reason = _turn_limit_reason(receipt)
            if status == "waiting" and not result:
                reason = ("사용자 승인을 기다리고 있습니다." if receipt_status == "waiting_for_approval"
                          else "질문에 대한 답변을 기다리고 있습니다.")
                result = reason
            elif status == "failed" and not result:
                result = str(receipt.get("error") or "실행이 실패했습니다.")
            if limit_reason:
                if work.get("kind") == "review":
                    status, result = "waiting", limit_reason
                else:
                    status = "partial" if result else "waiting"
                    result = f"{result}\n\n{limit_reason}".strip()
            actual_model = _receipt_model(receipt)
            if work.get("model") and not current.get("review"):
                if not actual_model:
                    status, result = "waiting", "실제로 사용한 모델을 확인하지 못해 결과를 검증할 수 없습니다."
                elif actual_model != work["model"]:
                    status, result = "failed", (f"요청한 모델({work['model']})과 실제 모델({actual_model})이 달라 "
                                                   "요청별 결과를 확정하지 않았습니다.")
            if current.get("status") == status and current.get("result") == result:
                return {"ok": True}
            updated = {**current, "status": status, "result": result,
                       "reported_run_status": receipt_status,
                       **({"actual_model": actual_model} if actual_model else {})}
            if current.get("review"):
                if limit_reason:
                    outcome = {"status": "waiting", "reason": limit_reason, "findings": []}
                else:
                    from agent.crema_work_review import finish_review
                    outcome = finish_review(current["review"], actual_model=actual_model, output=result)
                updated["review"] = {**current["review"], **outcome}
                if current["review"].get("previous") and outcome.get("status") in {"completed", "partial"}:
                    try:
                        prior_quote = json.loads(result).get("previous_evidence")
                    except (ValueError, AttributeError, TypeError):
                        prior_quote = None
                    previous_content = current["review"]["previous"].get("content", "")
                    if isinstance(prior_quote, str) and prior_quote and prior_quote in previous_content:
                        updated["review"]["previous_evidence"] = prior_quote
                updated["status"] = outcome["status"]
                updated["result"] = outcome.get("reason", "")
            own_status = updated["status"]
            if work.get("kind") != "result" and own_status in {"completed", "partial"}:
                updated["own_status"] = own_status
                updated["status"], children = _aggregate_parent_status(conn, request_id, own_status)
                updated["children"] = [{"request_id": child.get("request_id"),
                                         "message_id": child.get("message_id"),
                                         "status": child.get("status"), "kind": child.get("kind")}
                                        for child in children]
        sender_task = turns._task_for(conn, work.get("sender", ""))
        delivery_authorized = _authorized(session, work, conn)
        _append_locked(conn, recipient_task, sender_task, updated)
        if updated.get("status") == "cancelled":
            _cancel_descendants(conn, request_id)
        if action == "report" and status != "stopping" and work.get("kind") != "result" \
                and delivery_authorized:
            delivery_id = uuid.uuid5(uuid.NAMESPACE_URL,
                                     f"crema-work:{request_id}:{attempt_id}:{message_id}:{status}:"
                                     f"{hashlib.sha256(updated['result'].encode()).hexdigest()}").hex
            result_text, review_result = _result_payload(updated)
            if len(result_text) > turns.MESSAGE_MAX:
                result_text = result_text[:turns.MESSAGE_MAX - 96] + (
                    f"\n\n[Result shortened. Read request {request_id} for the full saved result.] ")
            response = {"request_id": request_id, "attempt_id": attempt_id, "message_id": delivery_id,
                        "project_id": work["project_id"], "sender": session, "recipient": work["sender"],
                        "mode": "queue", "kind": "result", "status": "queued", "text": result_text,
                        "result": result_text, "source_message_id": message_id,
                        "source_run_id": updated.get("run_id"), "source_status": updated.get("status"),
                        **({"parent_request_id": work["parent_request_id"]}
                           if work.get("parent_request_id") else {}),
                        **({"revision": updated["revision"]} if updated.get("revision") is not None else {}),
                        **({"review_result": review_result} if review_result else {})}
            response_sender = turns._task_for(conn, session)
            response_recipient = turns._task_for(conn, work["sender"])
            _append_locked(conn, response_recipient, response_sender, response)
        if work.get("parent_request_id"):
            _refresh_parent(conn, work["parent_request_id"], updated)
        return {"ok": True}


def _run_receipt(work: dict) -> dict | None:
    from agent import crema_file_turns as turns
    from gateway.platforms.api_server_runs import work_run_receipt
    return work_run_receipt(turns._api, work)


def _receipt_model(receipt: dict) -> str:
    runtime = receipt.get("runtime")
    return str((runtime or {}).get("model") or receipt.get("model") or "") if isinstance(runtime, dict) else str(receipt.get("model") or "")


def _turn_limit_reason(receipt: dict) -> str:
    """Translate only engine-owned budget exit codes; provider text has no stable quota contract."""
    reason = str(receipt.get("turn_exit_reason") or "")
    if reason == "review_input_budget_exhausted":
        return "검토 입력 사용 한도에 도달해 판정을 마치지 못했습니다."
    if reason == "budget_exhausted" or reason.startswith("max_iterations_reached("):
        return "실행 반복 한도에 도달했습니다. 남은 작업을 확인한 뒤 이어 주세요."
    return ""


def read_request(session_id: str, to: str, request_id: str) -> dict:
    """Read the durable result of one request without relying on the recipient's latest chat reply."""
    from agent import crema_file_turns as turns
    reconcile_stopping()
    me, target, problem = _resolve_target(session_id, to)
    if problem:
        return {"error": problem}
    conn = turns._board()
    try:
        rows = turns._directory()
        source = next((row for row in rows if row["session"] == me), None)
        recipient = next((row for row in rows if row["session"] == target["session"]), None)
        if source and not recipient:
            recipient = next((row for row in turns._closed(source)
                              if row["session"] == target["session"]), None)
        project_id = str((source or {}).get("project_id") or "")
        if (not source or not recipient or source.get("collaboration") != 1 or recipient.get("collaboration") != 1
                or not project_id or project_id != str(recipient.get("project_id") or "")):
            return {"error": "this chat is not authorized to read that request"}
        # _all_work is ordered by each message's first durable insertion. Status updates to an
        # older attempt (for example, rejecting a stale review claim) must not supersede a newer
        # original request when the sender reads its durable result.
        matches = [work for work in _all_work(conn)
                   if work.get("request_id") == request_id and work.get("sender") == me
                   and work.get("recipient") == target["session"]
                   and work.get("project_id") == project_id]
        if not matches:
            return {"error": f"No collaboration request '{request_id}' for that chat."}
        original_requests = [work for work in matches if work.get("kind") in {"request", "review"}]
        # A sender may read its own saved result after the grant or parent assignment expires;
        # this does not authorize another message or expose work sent by the target.
        return {"chat": target["title"], "work": (original_requests or matches)[-1]}
    finally:
        conn.close()


def _authorized(session_id: str, work: dict, conn) -> bool:
    from agent import crema_file_turns as turns
    sender = work.get("sender", "")
    rows = turns._directory()
    recipient = next((r for r in rows if r["session"] == session_id), None)
    source = next((r for r in rows if r["session"] == sender), None)
    if source and not recipient:
        recipient = next((row for row in turns._closed(source) if row["session"] == session_id), None)
    if (not recipient or not source or recipient.get("collaboration") != 1 or source.get("collaboration") != 1
            or not work.get("project_id") or recipient.get("project_id") != work.get("project_id")
            or source.get("project_id") != work.get("project_id")):
        return False
    if work.get("kind") == "result":
        original = next((item for item in _all_work(conn)
                         if item.get("request_id") == work.get("request_id")
                         and item.get("message_id") == work.get("source_message_id")
                         and item.get("sender") == session_id and item.get("recipient") == sender
                         and item.get("project_id") == work.get("project_id")), None)
        if not original:
            return False
        if original.get("kind") == "review" and not _review_is_latest(conn, original):
            return False
        if _works_with(session_id, sender):
            return True
        return _grant_not_revoked(conn, session_id, sender, work.get("project_id", ""),
                                  work.get("request_id", ""))
    if _works_with(sender, session_id):
        return True
    return any(g.get("request_id") == work.get("request_id")
               for g in _active_grants(conn, sender, session_id, work.get("project_id", "")))


def _grant_not_revoked(conn, first_session: str, second_session: str, project_id: str, request_id: str) -> bool:
    from agent import crema_file_turns as turns
    latest = None
    for owner in {first_session, second_session}:
        for event in turns._session_events(conn, owner):
            grant = (event.payload or {}).get("grant")
            if (grant and grant.get("request_id") == request_id and grant.get("project_id") == project_id
                    and {grant.get("sender"), grant.get("recipient")} == {first_session, second_session}
                    and (latest is None or event.id > latest[0])):
                latest = (event.id, grant)
    return bool(latest and not latest[1].get("revoked"))


def _grant(body: dict, action: str) -> dict:
    from agent import crema_file_turns as turns
    session, request_id, peer = (str(body.get(k) or "") for k in
                                 ("session_id", "request_id", "peer_session_id"))
    user_message_id, project_id = str(body.get("message_id") or ""), str(body.get("project_id") or "")
    if not session or not peer or not user_message_id or not project_id:
        return {"ok": False, "error": "session_id, peer_session_id, project_id and message_id are required"}
    rows = turns._directory()
    current = next((r for r in rows if r["session"] == session), None)
    other = next((r for r in rows if r["session"] == peer), None)
    if (not current or not other or current.get("collaboration") != 1 or other.get("collaboration") != 1
            or current.get("project_id") != project_id or other.get("project_id") != project_id):
        return {"ok": False, "error": "both chats must belong to the selected collaboration project"}
    conn = turns._board()
    try:
        kb = turns._kb()
        with kb.write_txn(conn):
            grants = _active_grants(conn, session, peer, project_id)
            history = {}
            for owner in {session, peer}:
                for event in turns._session_events(conn, owner):
                    grant = (event.payload or {}).get("grant")
                    if not grant or grant.get("sender") != session or grant.get("recipient") != peer \
                            or grant.get("project_id") != project_id or not grant.get("request_id"):
                        continue
                    key = grant["request_id"]
                    if key not in history or event.id > history[key][0]:
                        history[key] = (event.id, grant)
            if action == "grant":
                repeated = next(((key, value[1]) for key, value in history.items()
                                 if value[1].get("user_message_id") == user_message_id), None)
                if repeated:
                    old_id, old_grant = repeated
                    if request_id and request_id != old_id:
                        return {"ok": False, "error": "user message already authorized another request_id"}
                    old_status = ("revoked" if old_grant.get("revoked") else
                                  "completed" if old_id in _terminal_requests(conn) else "active")
                    return {"ok": True, "request_id": old_id, "session_id": session,
                            "peer_session_id": peer, "project_id": project_id,
                            "user_message_id": user_message_id,
                            "status": old_status, "duplicate": True}
            if action == "revoke" and not request_id:
                if len(grants) != 1:
                    return {"ok": False, "error": "request_id is required unless exactly one active request exists"}
                request_id = grants[0]["request_id"]
            if action == "revoke" and not any(g.get("request_id") == request_id for g in grants):
                return {"ok": False, "error": "active authorization was not found"}
            request_id = request_id or str(uuid.uuid4())
            current_task, other_task = turns._task_for(conn, session), turns._task_for(conn, peer)
            payload = {"request_id": request_id, "user_message_id": user_message_id,
                       "sender": session, "recipient": peer, "project_id": project_id,
                       "revoked": action == "revoke"}
            kb._append_event(conn, current_task.id, "crema_note", {"text": "", "grant": payload})
            if current_task.id != other_task.id:
                kb._append_event(conn, other_task.id, "crema_note", {"text": "", "grant": payload})
            result = {"ok": True, "request_id": request_id, "session_id": session,
                      "peer_session_id": peer, "project_id": project_id,
                      "user_message_id": user_message_id,
                      "status": "revoked" if action == "revoke" else "active"}
        return result
    finally:
        conn.close()


def _resolve_target(session_id: str, to: str):
    from agent import crema_file_turns as turns
    me = turns._chat(session_id)
    if not me:
        return None, None, "Only a Crema chat can send to or read another chat."
    rows = turns._directory()
    found = [row for row in rows if str(to or "").strip()
             and row["id"].startswith(str(to).strip())]
    if not found and str(to or "").strip():
        found = [row for row in turns._closed(turns._mine(rows, me)) if row["id"].startswith(str(to).strip())]
    if len(found) != 1:
        return me, None, (f"No chat numbered '{to}'" + (" (more than one starts so)" if found else "")
                          + ". Call crema_chats with action list for the numbers.")
    target = found[0]
    if target["session"] == me:
        return me, None, "That is this chat."
    return me, target, None


def _works_with(first_session: str, second_session: str) -> bool:
    from agent import crema_file_turns as turns
    rows = turns._directory()
    first = next((r for r in rows if r["session"] == first_session), None)
    second = next((r for r in rows if r["session"] == second_session), None)
    if first and not second:
        second = next((row for row in turns._closed(first) if row["session"] == second_session), None)
    return bool(first and second and turns._works_with(first, second))


def _active_parent_requests(conn, session_id: str) -> list[str]:
    latest = {}
    for payload in _events(conn, session_id):
        work = payload["work"]
        if work.get("recipient") == session_id and work.get("direction") != "outbound":
            latest[work.get("request_id")] = work
    active = {request for request, work in latest.items()
              if request and work.get("status") in {"claimed", "accepted", "waiting", "partial", "stopping"}}
    return sorted(active - _terminal_requests(conn))


def _owns_parent_request(conn, session_id: str, request_id: str) -> bool:
    if request_id in _active_parent_requests(conn, session_id):
        return True
    from agent import crema_file_turns as turns
    latest = None
    for event in turns._session_events(conn, session_id):
        grant = (event.payload or {}).get("grant")
        if grant and grant.get("sender") == session_id and grant.get("request_id") == request_id \
                and (latest is None or event.id > latest[0]):
            latest = (event.id, grant)
    return bool(latest and not latest[1].get("revoked") and request_id not in _terminal_requests(conn))


def _active_grants(conn, first_session: str, second_session: str, project_id: str) -> list[dict]:
    from agent import crema_file_turns as turns
    latest = {}
    for session in {first_session, second_session}:
        for event in turns._session_events(conn, session):
            grant = (event.payload or {}).get("grant")
            if not grant:
                continue
            pair = {grant.get("sender"), grant.get("recipient")}
            if pair != {first_session, second_session} or grant.get("project_id") != project_id:
                continue
            key = grant.get("request_id")
            if key and (key not in latest or event.id > latest[key][0]):
                latest[key] = (event.id, grant)
    terminal = _terminal_requests(conn)
    return [grant for request_id, (_, grant) in latest.items()
            if not grant.get("revoked") and request_id not in terminal]
