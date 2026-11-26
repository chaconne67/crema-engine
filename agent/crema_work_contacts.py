"""List project collaborators without granting access to their conversations."""
import uuid

from agent import crema_file_turns as turns
from agent.crema_collaboration import _active_grants


def contacts(session_id, *, topic="", path=""):
    session = turns._chat(session_id)
    directory = turns._directory()
    me = next((row for row in directory if row["session"] == session), None)
    if not me or not me.get("project_id") or me.get("collaboration") != 1:
        return {"contacts": []}
    conn = turns._board()
    try:
        holders = {task.session_id for task in turns._holders(conn, path, session)} if path else set()
        candidates = []
        for row in directory:
            if (row["session"] == session or row.get("collaboration") != 1
                    or row.get("project_id") != me["project_id"] or row.get("archived")):
                continue
            grants = _active_grants(conn, session, row["session"], me["project_id"])
            candidates.append({"number": turns._number(row), "role": row.get("role", ""), "title": row["title"],
                               "holds_file": row["session"] in holders,
                               "can_contact": turns._works_with(me, row) or bool(grants),
                               "authorized_requests": [grant["request_id"] for grant in grants]})
        # The board establishes file ownership. The agent decides which role fits the task.
        candidates.sort(key=lambda peer: (not peer["holds_file"], not (peer["role"] and peer["role"] in topic)))
        return {"contacts": candidates}
    finally:
        conn.close()


def ask_file_holder(conn, waiting, holder, path):
    """Ask once per blocked file turn, using the same request and delivery checks as a manual question."""
    from agent.crema_collaboration import _events, create
    directory = turns._directory()
    sender = next((row for row in directory if row["session"] == waiting.session_id), None)
    peer = next((row for row in directory if row["session"] == holder.session_id), None)
    if (not sender or not peer or sender.get("collaboration") != 1 or peer.get("collaboration") != 1
            or not sender.get("project_id") or sender["project_id"] != peer.get("project_id")):
        return None
    file_key = turns._key(path)
    releases = [event.id for event in turns._kb().list_events(conn, holder.id)
                if event.kind == "crema_let_go" and (event.payload or {}).get("key") == file_key]
    key = uuid.uuid5(uuid.NAMESPACE_URL, repr((waiting.session_id, holder.id, file_key, max(releases, default=0))))
    if turns._works_with(sender, peer):
        request_id = str(key)
    else:
        grants = _active_grants(conn, waiting.session_id, holder.session_id, sender["project_id"])
        if len(grants) != 1:
            return None
        request_id = grants[0]["request_id"]
    message_id = str(uuid.uuid5(key, "message"))
    existing = [note["work"] for note in _events(conn, holder.session_id)
                if note["work"].get("message_id") == message_id and note["work"].get("sender") == waiting.session_id]
    if existing:
        return existing[-1]
    reply = create(waiting.session_id, peer["id"],
                   f"{path} 파일의 수정 차례를 기다리고 있습니다. 변경 중인 부분과 작업을 마칠 시점을 알려 주세요.",
                   mode="queue", kind="question", request_id=request_id,
                   attempt_id=str(uuid.uuid5(key, "attempt")), message_id=message_id)
    return reply.get("work") if reply.get("ok") else None
