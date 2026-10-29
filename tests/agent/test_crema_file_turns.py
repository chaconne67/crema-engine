"""Crema: chats that change the same file take turns (agent/crema_file_turns.py)."""
import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from agent import crema_file_turns as turns

A, B = "agent-client-a", "agent-client-b"


class FakeDB:
    def __init__(self):
        self.roots, self.tips = {}, {}  # a long chat's later sessions (compression) -> the chat, and back

    def get_conversation_root(self, session_id):
        return self.roots.get(session_id, session_id)

    def resolve_resume_session_id(self, session_id):
        return self.tips.get(session_id, session_id)

    def get_session(self, session_id):
        return {"title": {A: "보고서", B: "메일"}.get(session_id, "")}

    def get_messages_as_conversation(self, session_id, **_):
        return [{"role": "user", "content": f"{session_id} 요청"}]


class FakeApi:
    def __init__(self):
        self._run_statuses = {}
        self.db = FakeDB()

    def _ensure_session_db(self):
        return self.db

    def replying(self, session_id, on=True):
        self._run_statuses[session_id] = {"session_id": session_id, "status": "running" if on else "completed"}


@pytest.fixture
def api(monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(turns, "_api", fake)
    return fake


@pytest.fixture
def judge(monkeypatch):
    """Answers queued replies (a dict is sent as JSON, an Exception is raised); records each call."""
    replies, calls = [], []

    def call_llm(**kwargs):
        calls.append(kwargs)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

    monkeypatch.setattr("agent.auxiliary_client.call_llm", call_llm)
    return SimpleNamespace(replies=replies, calls=calls)


@pytest.fixture
def repo(tmp_path):
    run = lambda *args: subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (tmp_path / "report.py").write_text("v1\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "start")
    return SimpleNamespace(path=tmp_path, run=run)


@pytest.fixture
def folders():
    """The folder a chat works in, as the app registers it for each run (gateway/platforms/api_server_runs.py)."""
    from tools.terminal_tool import clear_task_env_overrides, register_task_env_overrides

    yield lambda session, folder: register_task_env_overrides(session, {"cwd": str(folder), "cwd_source": "session"})
    for session in (A, B):
        clear_task_env_overrides(session)


def write(session, path, content="x\n"):
    """A write_file call through both hooks, as the engine runs it; the refusal or None."""
    args = {"path": str(path), "content": content}
    refusal = turns.before_tool("write_file", args, session_id=session, task_id=session)
    if refusal:
        return refusal["message"]
    path.write_text(content, encoding="utf-8")
    turns.after_tool("write_file", args, json.dumps({"bytes_written": len(content)}), session_id=session, task_id=session)
    return None


def test_a_file_no_other_chat_holds_is_written_without_a_judge(api, judge, tmp_path):
    assert write(A, tmp_path / "a.txt") is None
    assert write(B, tmp_path / "b.txt") is None
    assert judge.calls == []
    assert turns.state() == {"waiting": [], "ask": [], "wake": []}


def test_other_sessions_are_left_alone(api, judge, tmp_path):
    assert turns.before_tool("write_file", {"path": str(tmp_path / "a.txt")}, session_id="cli-1") is None
    turns.after_tool("write_file", {"path": str(tmp_path / "a.txt")}, "{}", session_id="cli-1")
    assert turns.state() == {"waiting": [], "ask": [], "wake": []}


def test_a_long_chat_keeps_its_turns_after_compression_starts_a_new_session(api, judge, repo):
    api.db.roots["20260929_a2"], api.db.tips[A] = A, "20260929_a2"
    report = repo.path / "report.py"
    assert write("20260929_a2", report, "v2\n") is None
    api.replying("20260929_a2")
    judge.replies.append({"decision": "a_first", "reason": "A 먼저."})
    assert "'보고서'" in write(B, report, "v3\n")
    assert "20260929_a2 요청" in judge.calls[0]["messages"][1]["content"]
    assert [(w["session_id"], w["holder_session"]) for w in turns.state()["waiting"]] == [(B, A)]
    assert "'메일'" not in (turns.notes_for_turn(session_id="20260929_a2") or {}).get("context", "")


def test_a_quiet_chat_with_the_file_committed_hands_it_over_and_is_told_once(api, judge, repo):
    report = repo.path / "report.py"
    assert write(A, report, "v2\n") is None
    repo.run("commit", "-qam", "A")
    assert write(B, report, "v3\n") is None
    assert judge.calls == []
    note = turns.notes_for_turn(session_id=A)["context"]
    assert "'메일'" in note and str(report) in note
    assert turns.notes_for_turn(session_id=A) is None


def test_a_first_makes_b_wait_until_a_is_quiet_and_committed_then_b_is_woken(api, judge, repo):
    report = repo.path / "report.py"
    assert write(A, report, "v2\n") is None
    api.replying(A)
    judge.replies.append({"decision": "a_first", "reason": "A가 거의 끝났습니다."})
    refusal = write(B, report, "v3\n")
    assert "'보고서'" in refusal and "A가 거의 끝났습니다." in refusal and "end your turn" in refusal
    assert report.read_text(encoding="utf-8") == "v2\n"
    # B asks again while A still works: the order stands, no second judge.
    assert write(B, report, "v3\n")
    assert len(judge.calls) == 1
    turns.settle()
    assert [w["session_id"] for w in turns.state()["waiting"]] == [B]

    api.replying(A, on=False)
    turns.settle()  # quiet but not committed: A is told, B still waits
    assert "commit" in turns.notes_for_turn(session_id=A)["context"]
    assert [w["session_id"] for w in turns.state()["waiting"]] == [B]

    repo.run("commit", "-qam", "A")
    turns.settle()
    wake = turns.state()["wake"]
    assert [w["session_id"] for w in wake] == [B]
    assert wake[0]["after"][0]["title"] == "보고서" and "report.py" in wake[0]["after"][0]["result"]
    turns.woken(B)
    assert turns.state() == {"waiting": [], "ask": [], "wake": []}
    assert write(B, report, "v3\n") is None


def test_b_first_lets_b_write_and_holds_a_back(api, judge, repo):
    report = repo.path / "report.py"
    write(A, report, "v2\n")
    api.replying(A)
    judge.replies.append({"decision": "b_first", "reason": "급한 한 줄 수정입니다."})
    assert write(B, report, "v3\n") is None
    assert "'메일'" in turns.notes_for_turn(session_id=A)["context"]
    assert "'메일'" in write(A, report, "v4\n")
    assert len(judge.calls) == 1


def test_together_is_judged_once_for_the_pair(api, judge, repo):
    report = repo.path / "report.py"
    write(A, report, "v2\n")
    api.replying(A)
    judge.replies.append({"decision": "together", "reason": "서로 다른 함수입니다."})
    assert write(B, report, "v3\n") is None
    assert write(B, report, "v4\n") is None
    assert write(A, report, "v5\n") is None
    assert len(judge.calls) == 1


def test_two_chats_reaching_a_new_file_before_either_writes_take_turns(api, judge, repo):
    """The first chat holds the file from the moment it may write, so the second is judged (audit ER-2)."""
    notes = repo.path / "notes.md"
    api.replying(A)
    api.replying(B)
    first = {"path": str(notes), "content": "A\n"}
    assert turns.before_tool("write_file", first, session_id=A, task_id=A, tool_call_id="a1") is None
    judge.replies.append({"decision": "a_first", "reason": "먼저 시작한 대화입니다."})
    refusal = turns.before_tool("write_file", {"path": str(notes), "content": "B\n"}, session_id=B, task_id=B,
                                tool_call_id="b1")
    assert refusal and "'보고서'" in refusal["message"]
    assert len(judge.calls) == 1


def test_a_write_that_fails_gives_its_file_back(api, judge, repo):
    notes = repo.path / "notes.md"
    api.replying(A)
    args = {"path": str(notes), "content": "A\n"}
    assert turns.before_tool("write_file", args, session_id=A, task_id=A, tool_call_id="a1") is None
    turns.after_tool("write_file", args, json.dumps({"error": "disk full"}), session_id=A, task_id=A, tool_call_id="a1")
    assert write(B, notes, "B\n") is None
    assert judge.calls == []


def test_together_on_one_file_is_judged_again_on_another(api, judge, repo):
    """Being judged able to share report.py says nothing about another file (audit ER-3)."""
    report, other = repo.path / "report.py", repo.path / "other.py"
    write(A, report, "v2\n")
    write(A, other, "o1\n")
    api.replying(A)
    judge.replies.append({"decision": "together", "reason": "서로 다른 함수입니다."})
    assert write(B, report, "v3\n") is None
    judge.replies.append({"decision": "a_first", "reason": "같은 줄을 고칩니다."})
    assert "'보고서'" in write(B, other, "o2\n")
    assert len(judge.calls) == 2
    assert other.read_text(encoding="utf-8") == "o1\n"


def test_ask_user_waits_for_the_users_order(api, judge, repo):
    report = repo.path / "report.py"
    write(A, report, "v2\n")
    api.replying(A)
    judge.replies.append({"decision": "ask_user", "reason": "같은 키를 서로 다른 이름으로 바꿉니다."})
    assert "the user is asked" in write(B, report, "v3\n")
    ask = turns.state()["ask"]
    assert [(a["session_id"], a["holder_session"], a["reason"]) for a in ask] == [(B, A, "같은 키를 서로 다른 이름으로 바꿉니다.")]

    turns.order(B, A)
    state = turns.state()
    assert state["ask"] == [] and [w["session_id"] for w in state["wake"]] == [B]


def test_the_user_can_let_a_waiting_chat_go_on(api, judge, repo):
    report = repo.path / "report.py"
    write(A, report, "v2\n")
    api.replying(A)
    judge.replies.append({"decision": "a_first", "reason": "A 먼저."})
    assert write(B, report, "v3\n")
    turns.release(B)
    assert [w["session_id"] for w in turns.state()["wake"]] == [B]
    assert write(B, report, "v3\n") is None
    assert "let the chat '메일'" in turns.notes_for_turn(session_id=A)["context"]


@pytest.mark.parametrize("replies", [
    ["not json", {"decision": "maybe", "reason": "?"}],
    [RuntimeError("provider down")],
])
def test_a_judge_without_an_answer_in_contract_asks_the_user(api, judge, repo, replies):
    report = repo.path / "report.py"
    write(A, report, "v2\n")
    api.replying(A)
    judge.replies.extend(replies)
    assert "the user is asked" in write(B, report, "v3\n")
    ask = turns.state()["ask"]
    assert len(ask) == 1 and "could not be judged" in ask[0]["reason"]
    assert report.read_text(encoding="utf-8") == "v2\n"


def test_the_judge_sees_both_requests_the_uncommitted_diff_and_the_change(api, judge, repo):
    report = repo.path / "report.py"
    write(A, report, "v2\n")
    api.replying(A)
    judge.replies.append({"decision": "a_first", "reason": "r"})
    write(B, report, "v3\n")
    body = judge.calls[0]["messages"][1]["content"]
    assert f"{A} 요청" in body and f"{B} 요청" in body
    assert "+v2" in body and "v3" in body
    assert judge.calls[0]["task"] == "crema_turns"


def test_a_terminal_command_that_changes_a_held_file_is_told(api, judge, repo, folders):
    report = repo.path / "report.py"
    write(A, report, "v2\n")
    folders(B, repo.path)
    args = {"command": "sed -i s/v2/v9/ report.py"}
    turns.before_tool("terminal", args, session_id=B, task_id=B, tool_call_id="t1")
    report.write_text("v9\n", encoding="utf-8")
    turns.after_tool("terminal", args, "ok", session_id=B, task_id=B, tool_call_id="t1")
    told = turns.tool_result("terminal", "ok", tool_call_id="t1")
    assert told.startswith("ok\n\n[Crema]") and "'보고서'" in told
    assert "'메일'" in turns.notes_for_turn(session_id=A)["context"]


def test_a_command_that_rewrites_a_file_unchanged_leaves_it_to_its_chat(api, judge, repo, folders):
    # A commit or sync in a shared repository touches files it leaves the same; 2026-10-28 three chats took a file
    # only another chat had changed, and that chat then waited on its own uncommitted change.
    report = repo.path / "report.py"
    write(A, report, "v2\n")
    folders(B, repo.path)
    args = {"command": "git pull --rebase --autostash"}
    turns.before_tool("terminal", args, session_id=B, task_id=B, tool_call_id="t2")
    later = time.time() + 60
    os.utime(report, (later, later))
    turns.after_tool("terminal", args, "ok", session_id=B, task_id=B, tool_call_id="t2")
    assert turns.files_since(B, 0) == []
    assert turns.notes_for_turn(session_id=A) is None


def test_a_command_records_only_files_in_its_chats_folder(api, judge, repo, folders):
    # Projects that share one repository: another project's file that changes meanwhile is not this chat's.
    (repo.path / "fc").mkdir()
    (repo.path / "voucher").mkdir()
    plan = repo.path / "fc" / "plan.md"
    folders(B, repo.path / "fc")
    args = {"command": "python write_plan.py"}
    turns.before_tool("terminal", args, session_id=B, task_id=B, tool_call_id="t3")
    write(A, repo.path / "voucher" / "boards.py", "new\n")
    plan.write_text("x\n", encoding="utf-8")
    turns.after_tool("terminal", args, "ok", session_id=B, task_id=B, tool_call_id="t3")
    assert turns.files_since(B, 0) == [{"path": str(plan.resolve()), "made": None}]


def test_install_puts_the_turn_rule_in_the_engines_pre_tool_call(api, judge, repo, monkeypatch):
    from hermes_cli.plugins import _dispatch_pre_tool_call_hooks, get_plugin_manager

    hooks = get_plugin_manager()._hooks
    before = {name: list(hooks.get(name, [])) for name in ("pre_tool_call", "post_tool_call", "transform_tool_result", "pre_llm_call")}
    try:
        turns.install(api)
        report = repo.path / "report.py"
        write(A, report, "v2\n")
        api.replying(A)
        judge.replies.append({"decision": "a_first", "reason": "A 먼저."})
        message, _ = _dispatch_pre_tool_call_hooks("write_file", {"path": str(report), "content": "v3\n"},
                                                   session_id=B, task_id=B, tool_call_id="c1", turn_id="t1")
        assert message and "'보고서'" in message
    finally:
        for name, callbacks in before.items():
            hooks[name] = callbacks


# ── messages between chats (crema_chats) ─────────────────────────────────────

C = "agent-client-c"


@pytest.fixture
def sidebar(api):
    turns.set_chats([{"id": "a", "session": A, "title": "보고서", "project": "회사"},
                     {"id": "b", "session": B, "title": "메일", "project": ""},
                     {"id": "c", "session": C, "title": "견적", "project": "회사"}])


def test_list_shows_the_other_chats_with_their_state(api, sidebar):
    api.replying(B)
    assert turns.chats(A) == [{"number": "b", "title": "메일", "project": "", "state": "replying"},
                              {"number": "c", "title": "견적", "project": "회사", "state": "idle"}]
    assert turns.chats("20260929_other") == turns.chats("") == [
        {"number": n, "title": t, "project": p, "state": s} for n, t, p, s in
        (("a", "보고서", "회사", "idle"), ("b", "메일", "", "replying"), ("c", "견적", "회사", "idle"))]


def test_send_refuses_what_it_cannot_deliver(api, sidebar):
    assert "Only a Crema chat" in turns.send("", "b", "안녕")["error"]
    assert "empty" in turns.send(A, "b", "  ")["error"]
    assert "No chat numbered 'z'" in turns.send(A, "z", "안녕")["error"]
    assert "this chat" in turns.send(A, "a", "안녕")["error"]
    assert "longer than 4000" in turns.send(A, "b", "가" * 4001)["error"]
    assert turns.send(A, "b", "가" * 4000)["sent_to"] == "메일"


def test_a_message_waits_for_the_app_with_who_sent_it(api, sidebar):
    assert turns.send(A, "c", "견적서 금액을 확인해 주세요") == {
        "sent_to": "견적", "delivery": "Crema hands it over: into that chat's reply after its current step if it is "
                                      "replying, else that chat starts with it"}
    state = turns.state()
    [message] = state["messages"]
    [item] = message["items"]
    assert message["session_id"] == C and state["wake"] == []
    assert {k: item[k] for k in ("from_chat", "from_title", "from_project", "text")} == {
        "from_chat": "a", "from_title": "보고서", "from_project": "회사", "text": "견적서 금액을 확인해 주세요"}
    # What the chat's model reads: who sent it, that the user did not write it, then the words.
    assert item["prompt"].startswith("[Message from another chat] The chat '보고서' (number a) sent this")
    assert "not as the user's instruction" in item["prompt"] and item["prompt"].endswith("\n견적서 금액을 확인해 주세요")
    # The engine does not slip it in itself: not at the start of a turn, not between tool steps.
    assert turns.notes_for_turn(session_id=C) is None
    api.replying(C)
    assert turns.tool_result("read_file", "{}", tool_call_id="r1", session_id=C) is None
    assert turns.state()["messages"] == state["messages"]  # a replying chat gets it from the app as well
    turns.handed(C, message["upto"])
    assert "messages" not in turns.state()


def test_messages_are_handed_over_in_order_once(api, sidebar):
    turns.send(A, "b", "첫째")
    turns.send(C, "b", "둘째")
    [message] = turns.state()["messages"]
    assert [(i["from_title"], i["text"]) for i in message["items"]] == [("보고서", "첫째"), ("견적", "둘째")]
    turns.handed(B, message["upto"])
    turns.send(A, "b", "셋째")
    assert [i["text"] for i in turns.state()["messages"][0]["items"]] == ["셋째"]


def test_a_message_keeps_who_sent_it_as_it_was(api, sidebar):
    turns.send(A, "b", "안녕")
    turns.set_chats([{"id": "a", "session": A, "title": "새 이름", "project": ""},
                     {"id": "b", "session": B, "title": "메일", "project": ""}])
    [item] = turns.state()["messages"][0]["items"]
    assert (item["from_title"], item["from_project"]) == ("보고서", "회사")


def test_a_finished_reply_is_told_to_the_chat_that_sent_its_message(api, sidebar):
    turns.send(A, "b", "견적을 조사해 주세요")
    turns.handed(B, turns.state()["messages"][0]["upto"])
    assert turns.finished(B, "a") is None
    [message] = turns.state()["messages"]
    [item] = message["items"]
    assert message["session_id"] == A
    assert (item["from_chat"], item["from_title"], item["text"], item["finished"]) == ("b", "메일", "맡긴 일을 마쳤어요.", True)
    assert item["prompt"].startswith("[Crema] The chat '메일' (number b) finished the work this chat sent it")
    # A chat no longer in the sidebar is not told; a message is not marked finished.
    assert "No chat numbered 'gone'" in turns.finished(B, "gone")
    turns.handed(A, message["upto"])
    turns.send(A, "b", "하나 더")
    assert "finished" not in turns.state()["messages"][0]["items"][0]


def test_crema_notes_still_reach_the_chat_and_messages_do_not_mix_with_them(api, sidebar):
    turns.send(A, "b", "안녕")
    conn = turns._board()
    try:
        turns._note(conn, turns._task_for(conn, B).id, "[Crema] 파일을 다시 읽으세요.")
    finally:
        conn.close()
    assert turns.notes_for_turn(session_id=B) == {"context": "[Crema] 파일을 다시 읽으세요."}
    assert turns.state()["messages"][0]["items"][0]["text"] == "안녕"


def test_a_chat_waiting_for_a_file_gets_its_messages_once_it_goes_on(api, judge, repo, sidebar):
    report = repo.path / "report.py"
    write(A, report, "v2\n")
    api.replying(A)
    judge.replies.append({"decision": "a_first", "reason": "A 먼저."})
    assert write(B, report, "v3\n")
    turns.send(C, "b", "급해요")
    state = turns.state()
    assert [w["session_id"] for w in state["waiting"]] == [B] and "messages" not in state
    turns.release(B)
    assert turns.state()["messages"][0]["session_id"] == B


def test_read_gives_the_other_chats_latest_reply(api, sidebar, monkeypatch):
    conversation = [{"role": "user", "content": "요청"}, {"role": "assistant", "content": "첫 답"},
                    {"role": "user", "content": "또"}, {"role": "assistant", "content": "마지막 답"},
                    {"role": "tool", "content": "{}"}]
    monkeypatch.setattr(api.db, "get_messages_as_conversation", lambda session_id, **_: conversation)
    assert turns.read(A, "b") == {"chat": "메일", "replying": False, "reply": "마지막 답"}
    api.replying(B)
    assert turns.read(A, "b")["replying"] is True
    assert "No chat numbered 'z'" in turns.read(A, "z")["error"]
    assert "this chat" in turns.read(A, "a")["error"]
    monkeypatch.setattr(api.db, "get_messages_as_conversation", lambda session_id, **_: conversation[:1])
    assert "has no reply yet" in turns.read(A, "b")["error"]


def test_titles_come_from_the_sidebar(api, sidebar):
    turns.set_chats([{"id": "a", "session": A, "title": "새 이름", "project": ""}])
    assert turns._title(A) == "새 이름"
    assert turns._title(B) == "메일"  # not in the sidebar list: the engine session's title


# ── the files a reply wrote (files_since) ────────────────────────────────────


def wrote(session, path, content="x\n", call="c"):
    """A write_file call through both hooks with its tool call id, as the engine runs it."""
    args = {"path": str(path), "content": content}
    assert turns.before_tool("write_file", args, session_id=session, task_id=session, tool_call_id=call) is None
    path.write_text(content, encoding="utf-8")
    turns.after_tool("write_file", args, json.dumps({"bytes_written": len(content)}), session_id=session, task_id=session,
                     tool_call_id=call)


def test_files_since_lists_what_the_chat_made_or_changed_once_each(api, judge, repo):
    memo, report = repo.path / "메모.txt", repo.path / "report.py"
    wrote(A, memo, "첫 줄\n", "w1")
    wrote(A, memo, "둘째 줄\n", "w2")
    wrote(A, report, "v2\n", "w3")
    assert turns.files_since(A, 0) == [{"path": str(memo), "made": True}, {"path": str(report), "made": False}]
    # Another chat's files are not this chat's; a later reply starts empty.
    assert turns.files_since(B, 0) == []
    import time
    assert turns.files_since(A, int(time.time()) + 2) == []
    # A file gone since is left out.
    memo.unlink()
    assert turns.files_since(A, 0) == [{"path": str(report), "made": False}]
    assert turns.files_since("not-a-crema-chat", 0) == []


def test_files_since_lists_a_terminal_commands_files_without_saying_made(api, judge, repo, folders):
    folders(A, repo.path)
    args = {"command": "echo x > notes.md"}
    turns.before_tool("terminal", args, session_id=A, task_id=A, tool_call_id="t9")
    (repo.path / "notes.md").write_text("x\n", encoding="utf-8")
    turns.after_tool("terminal", args, "ok", session_id=A, task_id=A, tool_call_id="t9")
    assert turns.files_since(A, 0) == [{"path": str((repo.path / "notes.md").resolve()), "made": None}]
