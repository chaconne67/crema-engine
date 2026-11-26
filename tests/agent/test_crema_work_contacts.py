import json

from agent import crema_file_turns as turns
from tools.crema_chats_tool import crema_chats


def test_contacts_use_project_identity_and_actual_file_claim_without_reading_messages(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(turns, "_api", object())
    rows = [{"id": name, "session": "agent-client-" + name, "title": name,
             "project": "같은 표시명", "project_id": project, "role": role, "collaboration": 1}
            for name, project, role in [("origin", "p1", "팀장"), ("mobile", "p1", "모바일"),
                                        ("owner", "p1", "문서"), ("elsewhere", "p2", "모바일")]]
    other_home=tmp_path/"other-account"
    other_home.mkdir()
    with monkeypatch.context() as other:
        other.setenv("HERMES_HOME", str(other_home))
        turns.set_chats([{**rows[0], "id": "foreign", "session": "agent-client-foreign", "title": "Other account"}])
    rows.append({**rows[1], "id": "restricted", "session": "agent-client-restricted", "parent": "another-parent"})
    turns.set_chats(rows)
    path = str(tmp_path / "spec.md")
    assert turns._claim(path, "agent-client-owner")
    result = json.loads(crema_chats({"action": "contacts", "topic": "모바일 검토", "path": path},
                                   session_id="agent-client-origin"))
    assert [(row["number"], row["holds_file"]) for row in result["contacts"]] == [("owner", True), ("mobile", False), ("restrict", False)]
    assert not next(row for row in result["contacts"] if row["number"] == "restrict")["can_contact"]
    assert "foreign" not in {row["number"] for row in result["contacts"]}
    assert all(not row["authorized_requests"] for row in result["contacts"])


def test_blocked_write_asks_the_file_holder_once_and_keeps_the_file_turn(monkeypatch, tmp_path):
    from agent.crema_collaboration import _events
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(turns, "_api", object())
    owner, worker = "agent-client-owner", "agent-client-worker"
    turns.set_chats([{"id": session.removeprefix("agent-client-"), "session": session,
                      "title": session, "project_id": "p", "collaboration": 1} for session in (owner, worker)])
    path = str(tmp_path / "plan.md")
    assert turns._claim(path, owner)
    with turns._board() as conn:
        turns._kb().link_tasks(conn, turns._task_for(conn, owner).id, turns._task_for(conn, worker).id)
    for _ in range(2):
        assert "Not written" in turns._turn_for(path, worker, "write_file", {})
    with turns._board() as conn:
        questions = [note["work"] for note in _events(conn, owner) if note["work"]["kind"] == "question"]
        assert len(questions) == 1
        assert questions[0]["sender"] == worker and path in questions[0]["text"]
        assert [task.session_id for task in turns._holders(conn, path, worker)] == [owner]


def test_new_file_conflict_gets_an_answer_before_the_judge_can_allow_a_write(monkeypatch, tmp_path):
    from agent import crema_collaboration as collaboration
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(turns, "_api", object())
    owner, worker = "agent-client-owner", "agent-client-worker"
    turns.set_chats([{"id": session.removeprefix("agent-client-"), "session": session,
                      "title": session, "project_id": "p", "collaboration": 1} for session in (owner, worker)])
    path = str(tmp_path / "login.md")
    assert turns._claim(path, owner)
    monkeypatch.setattr(turns, "_replying", lambda session: True)
    judged = []
    def judge(*args):
        judged.append(True)
        return {"decision": "together", "reason": "different sections"}
    monkeypatch.setattr(turns, "judge", judge)
    assert "Not written" in turns._turn_for(path, worker, "write_file", {})
    assert not judged
    assert not any(row["session_id"] == worker for row in turns.state()["wake"])
    # An independent wake or a previously open client can mark the chat woken while its holder is replying.
    turns.woken(worker)
    with turns._board() as conn:
        question = collaboration._events(conn, owner)[-1]["work"]
    body = {key: question[key] for key in ("request_id", "attempt_id", "message_id")}
    body["session_id"] = owner
    assert turns.collaborate({**body, "action": "claim"})["claimed"]
    monkeypatch.setattr(collaboration, "_run_receipt", lambda _: {
        "run_id": "run_owner", "session_id": owner, "status": "completed", "output": "두 번째 절을 수정 중입니다."})
    assert turns.collaborate({**body, "action": "report", "status": "completed", "run_id": "run_owner"})["ok"]
    assert turns._turn_for(path, worker, "write_file", {}) is None
    assert judged == [True]
