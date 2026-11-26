"""Request-scoped chat work is durable in the account board and distinct from legacy mail."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent import crema_file_turns as turns
from agent.crema_collaboration import create

A, B = "agent-client-a", "agent-client-b"
C = "agent-client-c"


@pytest.fixture
def collaboration_board(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(turns, "_api", SimpleNamespace(_run_statuses={}, _ensure_store=lambda: None))
    turns.set_chats([
        {"id": "a", "session": A, "title": "기획", "project_id": "project-1", "collaboration": 1},
        {"id": "b", "session": B, "title": "검토", "project_id": "project-1", "collaboration": 1,
         "parent": "a"},
        {"id": "c", "session": C, "title": "다른 검토", "project_id": "project-1", "collaboration": 1},
    ])
    turns.set_models([{"model": "review-model", "provider": "test"}])
    from agent import crema_collaboration
    receipts = {}
    monkeypatch.setattr(crema_collaboration, "_run_receipt",
                        lambda work: receipts.get((work["attempt_id"], work["message_id"])))
    return SimpleNamespace(receipts=receipts)


def _key(work):
    return {"session_id": work["recipient"], "request_id": work["request_id"],
            "attempt_id": work["attempt_id"], "message_id": work["message_id"]}


def test_three_levels_of_delegation_report_final_status_to_original_sender(collaboration_board):
    jobs = []
    for source, peer in [(A, "b"), (B, "a"), (A, "c")]:
        parent = jobs[-1]["request_id"] if jobs else ""
        job = create(source, peer, "담당 부분 확인", parent_request_id=parent)["work"]
        assert turns.collaborate({**_key(job), "action": "claim"})["claimed"]
        jobs.append(job)
    for job in jobs:
        run_id = "run_" + job["attempt_id"]
        collaboration_board.receipts[job["attempt_id"], job["message_id"]] = dict(
            run_id=run_id, session_id=job["recipient"], status="completed", output="확인 완료")
        assert turns.collaborate({**_key(job), "action": "report", "run_id": run_id, "status": "completed"})["ok"]
        expected = "completed" if job is jobs[-1] else "partial"
        assert turns.read(A, "b", jobs[0]["request_id"])["work"]["status"] == expected


def test_archived_child_can_receive_new_work_only_from_its_parent(collaboration_board):
    from tools.crema_chats_tool import crema_chats
    rows = turns._directory()
    turns.set_chats([{**row, "archived": row["session"] == B} for row in rows])
    args = {"action": "send", "to": "b", "message": "이전 업무에 이어서 확인해 주세요."}
    assert "error" in json.loads(crema_chats(args, session_id=C))
    work = json.loads(crema_chats(args, session_id=A))["work"]
    assert work["recipient"] == B
    assert turns.collaborate({**_key(work), "action": "claim"})["claimed"]


def test_work_claim_report_and_result_are_durable_and_idempotent(collaboration_board):
    sent = create(A, "b", "검토해 주세요", request_id="r1", attempt_id="a1", message_id="m1")
    assert sent["ok"] is True
    work = sent["work"]
    key = _key(work)

    state = turns.state()["messages"]
    incoming = next(item for row in state if row["session_id"] == B for item in row["items"] if item.get("work"))
    assert incoming["work"]["request_id"] == "r1"
    assert incoming["work"]["direction"] == "inbound"
    assert incoming["work"]["status"] == "queued"

    assert turns.collaborate({**key, "action": "claim"})["claimed"] is True
    duplicate = turns.collaborate({**key, "action": "claim"})
    assert duplicate["claimed"] is False
    assert duplicate["work"]["status"] == "check_required"
    pending = next(item["work"] for row in turns.state()["messages"] if row["session_id"] == B
                   for item in row["items"] if item.get("work", {}).get("message_id") == "m1")
    assert pending["status"] == "check_required"
    assert turns.collaborate({**key, "action": "validate", "project_id": "project-1"})["valid"] is True
    collaboration_board.receipts[("a1", "m1")] = {"run_id": 17, "session_id": B, "status": "running",
                                                    "runtime": {"model": "gpt-6-luna"}}
    recovered = turns.collaborate({**key, "action": "claim"})
    assert recovered["claimed"] is False and recovered["run_id"] == 17
    assert recovered["work"]["status"] == "accepted"
    stored = next(item["work"] for row in turns.state()["messages"] if row["session_id"] == B
                  for item in row["items"] if item.get("work", {}).get("message_id") == "m1")
    assert stored["run_id"] == 17
    assert turns.collaborate({**key, "action": "accepted", "run_id": 17}) == {"ok": True}
    collaboration_board.receipts[("a1", "m1")] = {"run_id": 17, "session_id": B, "status": "completed",
                                                    "output": "완료", "runtime": {"model": "gpt-6-luna"}}
    assert turns.collaborate({**key, "action": "report", "run_id": 17, "status": "completed",
                              "result": "browser lie"}) == {"ok": True}

    saved = turns.read(A, "b", "r1")
    assert saved["work"]["status"] == "completed"
    assert saved["work"]["result"] == "완료"
    outgoing = next(item for row in turns.state()["messages"] if row["session_id"] == A
                    for item in row["items"] if item.get("work"))
    assert outgoing["work"]["direction"] == "outbound"
    assert outgoing["work"]["status"] == "completed"


def test_work_messages_do_not_advance_legacy_upto_and_stale_attempt_cannot_report(collaboration_board):
    turns.send(A, "b", "legacy message")
    first = create(A, "b", "first", request_id="r2", attempt_id="a1", message_id="m2")["work"]
    row = next(row for row in turns.state()["messages"] if row["session_id"] == B)
    conn = turns._board()
    try:
        legacy_ids = [e.id for e in turns._session_events(conn, B)
                      if e.kind == "crema_note" and (e.payload or {}).get("handed")
                      and not (e.payload or {}).get("work")]
    finally:
        conn.close()
    assert row["upto"] == max(legacy_ids)
    assert len([item for item in row["items"] if item.get("work")]) == 1
    old_key = _key(first)
    turns.collaborate({**old_key, "action": "claim"})
    collaboration_board.receipts[("a1", "m2")] = {"run_id": 22, "session_id": B, "status": "running"}
    turns.collaborate({**old_key, "action": "accepted", "run_id": 22})
    collaboration_board.receipts[("a1", "m2")] = {"run_id": 22, "session_id": B, "status": "failed", "output": "retry"}
    turns.collaborate({**old_key, "action": "report", "run_id": 22, "status": "failed", "result": "retry"})
    retry_key = {**old_key, "attempt_id": "a2"}
    assert turns.collaborate({**retry_key, "action": "claim"})["claimed"] is True
    stale = turns.collaborate({**old_key, "action": "report", "status": "completed", "result": "late"})
    assert stale["ok"] is False
    turns.handed(B, row["upto"])
    still = turns.state()["messages"]
    assert any(item.get("work", {}).get("request_id") == "r2"
               for entry in still if entry["session_id"] == B for item in entry["items"])


def test_grant_is_user_scoped_and_revocation_blocks_unstarted_work(collaboration_board):
    grant = turns.collaborate({"action": "grant", "session_id": B, "peer_session_id": C,
                               "project_id": "project-1", "message_id": "user-instruction"},
                              trusted_user=True)
    assert grant["ok"] is True
    request_id = grant["request_id"]
    repeated_grant = turns.collaborate({"action": "grant", "session_id": B, "peer_session_id": C,
                                        "project_id": "project-1", "message_id": "user-instruction"},
                                       trusted_user=True)
    assert repeated_grant["request_id"] == request_id and repeated_grant["duplicate"] is True
    work = create(B, "c", "협의해 주세요", request_id=request_id, attempt_id="a1", message_id="m3")["work"]
    assert turns.collaborate({**_key(work), "action": "validate", "project_id": "project-1"})["valid"] is True
    revoked = turns.collaborate({"action": "revoke", "session_id": B, "peer_session_id": C,
                                 "project_id": "project-1", "message_id": "user-withdrawal",
                                 "request_id": request_id}, trusted_user=True)
    assert revoked["status"] == "revoked"
    assert "error" in create(B, "c", "철회 뒤 새 질문", request_id=request_id,
                              attempt_id="a2", message_id="m4")
    claimed = turns.collaborate({**_key(work), "action": "claim"})
    assert claimed["claimed"] is False
    assert claimed["work"]["reason"] == "permission revoked"


def test_completed_grant_expires_new_questions_but_keeps_its_saved_result_readable(collaboration_board):
    grant = turns.collaborate({"action": "grant", "session_id": B, "peer_session_id": C,
                               "project_id": "project-1", "message_id": "grant-message"},
                              trusted_user=True)
    request_id = grant["request_id"]
    work = create(B, "c", "요청한 검토", request_id=request_id, attempt_id="grant-attempt",
                  message_id="grant-work")["work"]
    key = _key(work)
    turns.collaborate({**key, "action": "claim"})
    collaboration_board.receipts[("grant-attempt", "grant-work")] = {
        "run_id": "grant-run", "session_id": C, "status": "completed", "output": "결과"}
    report = {**key, "action": "report", "run_id": "grant-run", "status": "completed"}
    turns.collaborate(report)
    assert turns.read(B, "c", request_id)["work"]["status"] == "completed"
    repeated = turns.collaborate({"action": "grant", "session_id": B, "peer_session_id": C,
                                  "project_id": "project-1", "message_id": "grant-message"},
                                 trusted_user=True)
    assert repeated["status"] == "completed"
    assert "authorization" in create(B, "c", "늦은 질문", request_id=request_id,
                                      attempt_id="late-attempt", message_id="late-message")["error"]
    assert turns.read(B, "c", request_id)["work"]["status"] == "completed"
    result_work = next(item["work"] for row in turns.state()["messages"] if row["session_id"] == B
                       for item in row["items"] if item.get("work", {}).get("kind") == "result")
    assert turns.collaborate({**_key(result_work), "action": "claim"})["claimed"] is True
    turns.collaborate(report)
    results = [item["work"] for row in turns.state()["messages"] if row["session_id"] == B
               for item in row["items"] if item.get("work", {}).get("kind") == "result"
               and item["work"].get("source_message_id") == "grant-work"]
    assert len(results) == 1


def test_completed_question_and_answer_do_not_close_the_grant_request(collaboration_board):
    grant = turns.collaborate({"action": "grant", "session_id": B, "peer_session_id": C,
                               "project_id": "project-1", "message_id": "conversation-grant"},
                              trusted_user=True)
    request_id = grant["request_id"]
    question = create(B, "c", "첫 번째 질문", kind="question", request_id=request_id,
                      attempt_id="q1-attempt", message_id="q1-message")["work"]
    turns.collaborate({**_key(question), "action": "claim"})
    collaboration_board.receipts[("q1-attempt", "q1-message")] = {
        "run_id": "q1-run", "session_id": C, "status": "completed", "output": "첫 답변"}
    turns.collaborate({**_key(question), "action": "report", "run_id": "q1-run", "status": "completed"})
    result_one = next(item["work"] for row in turns.state()["messages"] if row["session_id"] == B
                      for item in row["items"] if item.get("work", {}).get("kind") == "result")
    assert turns.collaborate({**_key(result_one), "action": "claim"})["claimed"] is True

    answer = create(C, "b", "첫 질문의 답", kind="answer", request_id=request_id,
                    attempt_id="answer-attempt", message_id="answer-message")["work"]
    turns.collaborate({**_key(answer), "action": "claim"})
    collaboration_board.receipts[("answer-attempt", "answer-message")] = {
        "run_id": "answer-run", "session_id": B, "status": "completed", "output": "답 전달 완료"}
    turns.collaborate({**_key(answer), "action": "report", "run_id": "answer-run", "status": "completed"})

    second = create(B, "c", "두 번째 질문", kind="question", request_id=request_id,
                    attempt_id="q2-attempt", message_id="q2-message")
    assert second["ok"] is True


def test_same_request_answer_and_post_result_question_are_not_self_children(collaboration_board):
    assigned = create(A, "b", "답을 확인해 주세요", request_id="r-answer-same",
                      attempt_id="a-answer-parent", message_id="m-answer-parent")["work"]
    turns.collaborate({**_key(assigned), "action": "claim"})
    answer = create(B, "a", "확인 결과입니다", request_id="r-answer-same", kind="answer",
                    attempt_id="a-answer", message_id="m-answer")
    assert answer["ok"] is True and answer["work"].get("parent_request_id") is None

    parent = create(A, "b", "1차 업무", request_id="r-followup-same",
                    attempt_id="a-followup-parent", message_id="m-followup-parent")["work"]
    parent_key = _key(parent)
    turns.collaborate({**parent_key, "action": "claim"})
    collaboration_board.receipts[("a-followup-parent", "m-followup-parent")] = {
        "run_id": "run-followup-parent", "session_id": B, "status": "completed", "output": "일부 결과"}
    assert turns.collaborate({**parent_key, "action": "report", "run_id": "run-followup-parent",
                              "status": "partial"}) == {"ok": True}
    result = next(item["work"] for row in turns.state()["messages"] if row["session_id"] == A
                  for item in row["items"] if item.get("work", {}).get("kind") == "result"
                  and item["work"].get("request_id") == "r-followup-same")
    assert turns.collaborate({**_key(result), "action": "claim"})["claimed"] is True
    follow_up = create(A, "b", "같은 업무의 보충 질문", request_id="r-followup-same", kind="question",
                       attempt_id="a-followup-question", message_id="m-followup-question")
    assert follow_up["ok"] is True and follow_up["work"].get("parent_request_id") is None


def test_review_uses_immutable_revision_and_same_reviewer(collaboration_board):
    work = create(A, "b", "초안을 검토해 주세요", kind="review", request_id="review-1",
                  attempt_id="review-a1", message_id="review-m1", model="review-model",
                  revision={"title": "설계", "content": "요구사항과 본문"}, requirements="모든 요구사항이 있는지")["work"]
    assert work["revision"] == work["review"]["revision"]
    assert "요구사항과 본문" in work["text"]
    alternate = create(A, "c", "수정본", kind="review", request_id="review-1",
                       attempt_id="review-a2", message_id="review-m2", model="review-model",
                       revision={"title": "설계", "content": "본문 수정"}, requirements="모든 요구사항이 있는지")
    assert "검토 대화" in alternate["error"]


def test_review_report_checks_the_receipt_and_delivers_findings_to_writer(collaboration_board):
    work = create(A, "b", "초안을 검토해 주세요", kind="review", request_id="review-2",
                  attempt_id="review-a1", message_id="review-m1", model="review-model",
                  revision={"title": "설계", "content": "빠진 항목과 설명"}, requirements="요구사항 확인")["work"]
    key = _key(work)
    turns.collaborate({**key, "action": "claim"})
    collaboration_board.receipts[("review-a1", "review-m1")] = {
        "run_id": "review-run", "session_id": B, "status": "running", "runtime": {"model": "review-model"}}
    assert turns.collaborate({**key, "action": "accepted", "run_id": "review-run"})["ok"] is True
    output = json.dumps({"revision": work["revision"], "verdict": "changes",
                         "findings": ["빠진 항목을 추가해 주세요."], "evidence": "빠진 항목과 설명"})
    collaboration_board.receipts[("review-a1", "review-m1")] = {
        "run_id": "review-run", "session_id": B, "status": "completed", "output": output,
        "runtime": {"model": "review-model"}}
    assert turns.collaborate({**key, "action": "report", "run_id": "review-run", "status": "completed",
                              "result": "untrusted browser output"})["ok"] is True
    saved = turns.read(A, "b", "review-2")["work"]
    assert saved["status"] == "partial"
    assert saved["review"]["findings"] == ["빠진 항목을 추가해 주세요."]
    writer = next(item["work"] for row in turns.state()["messages"] if row["session_id"] == A
                  for item in row["items"] if item.get("work", {}).get("kind") == "result")
    assert writer["source_status"] == "partial"
    assert writer["review_result"]["reviewed_revision"] == work["revision"]
    assert writer["review_result"]["findings"] == ["빠진 항목을 추가해 주세요."]
    assert writer["review_result"]["evidence"] == "빠진 항목과 설명"
    assert "model" not in writer and "review" not in writer
    assert "빠진 항목을 추가해 주세요." in writer["text"]

    revised = create(A, "b", "수정본을 다시 검토해 주세요", kind="review", request_id="review-2",
                     attempt_id="review-a2", message_id="review-m2", model="review-model",
                     revision={"title": "설계", "content": "빠진 항목을 보충한 설명"},
                     requirements="요구사항 확인")["work"]
    assert revised["review"]["reviewer"] == work["review"]["reviewer"]
    turns.collaborate({**_key(revised), "action": "claim"})
    collaboration_board.receipts[("review-a2", "review-m2")] = {
        "run_id": "review-run-2", "session_id": B, "status": "running", "runtime": {"model": "review-model"}}
    turns.collaborate({**_key(revised), "action": "accepted", "run_id": "review-run-2"})
    approved = json.dumps({"revision": revised["revision"], "verdict": "pass", "findings": [],
                           "evidence": "빠진 항목을 보충한 설명", "previous_evidence": "빠진 항목과 설명"})
    collaboration_board.receipts[("review-a2", "review-m2")] = {
        "run_id": "review-run-2", "session_id": B, "status": "completed", "output": approved,
        "runtime": {"model": "review-model"}}
    turns.collaborate({**_key(revised), "action": "report", "run_id": "review-run-2", "status": "completed"})
    approved_result = turns.read(A, "b", "review-2")["work"]
    assert approved_result["status"] == "completed"
    assert approved_result["review"]["reviewed_revision"] == revised["revision"]
    final_notice = next(item["work"] for row in turns.state()["messages"] if row["session_id"] == A
                        for item in row["items"] if item.get("work", {}).get("kind") == "result"
                        and item["work"].get("review_result", {}).get("reviewed_revision") == revised["revision"])
    assert final_notice["review_result"]["previous"]["revision"] == work["revision"]
    assert "빠진 항목과 설명" in final_notice["text"]


def test_late_pass_for_an_old_revision_cannot_finalize_a_newer_review(collaboration_board):
    old = create(A, "b", "1판", kind="review", request_id="r-latest-review",
                 attempt_id="a-review-old", message_id="m-review-old", model="review-model",
                 revision={"title": "계획", "content": "첫 판"}, requirements="요구사항")["work"]
    old_key = _key(old)
    turns.collaborate({**old_key, "action": "claim"})
    collaboration_board.receipts[(old["attempt_id"], old["message_id"])] = {
        "run_id": "run-review-old", "session_id": B, "status": "completed",
        "output": json.dumps({"revision": old["revision"], "verdict": "pass", "findings": [],
                              "evidence": "첫 판"}), "runtime": {"model": "review-model"}}
    newer = create(A, "b", "2판", kind="review", request_id="r-latest-review",
                   attempt_id="a-review-new", message_id="m-review-new", model="review-model",
                   revision={"title": "계획", "content": "두 번째 판"}, requirements="요구사항")["work"]
    assert turns.collaborate({**old_key, "action": "validate", "project_id": "project-1"})["valid"] is False
    stale = turns.collaborate({**old_key, "action": "report", "run_id": "run-review-old", "status": "completed"})
    assert stale["ok"] is False and "newer document revision" in stale["error"]
    assert turns.collaborate({**_key(newer), "action": "claim"})["claimed"] is True


def test_late_claim_of_an_old_document_revision_is_rejected_and_saved_as_stale(collaboration_board):
    old = create(A, "b", "1판", kind="review", request_id="r-late-claim",
                 attempt_id="a-old-claim", message_id="m-old-claim", model="review-model",
                 revision={"title": "계획", "content": "구판"}, requirements="기준")["work"]
    newer = create(A, "b", "2판", kind="review", request_id="r-late-claim",
                   attempt_id="a-new-claim", message_id="m-new-claim", model="review-model",
                   revision={"title": "계획", "content": "신판"}, requirements="기준")["work"]
    rejected = turns.collaborate({**_key(old), "action": "claim"})
    assert rejected["claimed"] is False and rejected["work"]["stale_revision"] is True
    assert rejected["work"]["status"] == "cancelled"
    assert turns.collaborate({**_key(newer), "action": "claim"})["claimed"] is True


def test_waiting_old_review_cannot_pass_after_a_new_revision_is_registered(collaboration_board):
    old = create(A, "b", "초안", kind="review", request_id="r-waiting-old",
                 attempt_id="a-waiting-old", message_id="m-waiting-old", model="review-model",
                 revision={"title": "계획", "content": "초안 본문"}, requirements="기준")["work"]
    key = _key(old)
    turns.collaborate({**key, "action": "claim"})
    collaboration_board.receipts[("a-waiting-old", "m-waiting-old")] = {
        "run_id": "run-waiting-old", "session_id": B, "status": "waiting_for_clarify",
        "output": "사용자 답을 기다립니다.", "runtime": {"model": "review-model"}}
    assert turns.collaborate({**key, "action": "report", "run_id": "run-waiting-old",
                              "status": "waiting"})["ok"] is True
    newer = create(A, "b", "수정본", kind="review", request_id="r-waiting-old",
                   attempt_id="a-current", message_id="m-current", model="review-model",
                   revision={"title": "계획", "content": "수정 본문"}, requirements="기준")["work"]
    collaboration_board.receipts[("a-waiting-old", "m-waiting-old")].update(
        status="completed", output=json.dumps({"revision": old["revision"], "verdict": "pass",
                                                "findings": [], "evidence": "초안 본문"}))
    late = turns.collaborate({**key, "action": "report", "run_id": "run-waiting-old", "status": "completed"})
    assert late["ok"] is False and "newer document revision" in late["error"]
    assert turns.read(A, "b", "r-waiting-old")["work"]["revision"] == newer["revision"]


def test_queued_old_review_result_cannot_be_received_after_new_revision_is_registered(collaboration_board):
    old = create(A, "b", "1판", kind="review", request_id="r-old-result",
                 attempt_id="a-old-result", message_id="m-old-result", model="review-model",
                 revision={"title": "계획", "content": "첫 판"}, requirements="기준")["work"]
    key = _key(old)
    turns.collaborate({**key, "action": "claim"})
    collaboration_board.receipts[("a-old-result", "m-old-result")] = {
        "run_id": "run-old-result", "session_id": B, "status": "completed",
        "output": json.dumps({"revision": old["revision"], "verdict": "pass", "findings": [],
                              "evidence": "첫 판"}), "runtime": {"model": "review-model"}}
    assert turns.collaborate({**key, "action": "report", "run_id": "run-old-result",
                              "status": "completed"})["ok"] is True
    old_result = next(item["work"] for row in turns.state()["messages"] if row["session_id"] == A
                      for item in row["items"] if item.get("work", {}).get("kind") == "result")
    newer = create(A, "b", "2판", kind="review", request_id="r-old-result",
                   attempt_id="a-new-result", message_id="m-new-result", model="review-model",
                   revision={"title": "계획", "content": "신판"}, requirements="기준")["work"]
    rejected = turns.collaborate({**_key(old_result), "action": "claim"})
    assert rejected["claimed"] is False
    assert turns.collaborate({**_key(old_result), "action": "validate", "project_id": "project-1"})["valid"] is False
    new_claim = turns.collaborate({**_key(newer), "action": "claim"})
    assert new_claim["claimed"] is True
    assert new_claim["work"]["revision"] == newer["revision"]


def test_read_request_keeps_latest_completed_review_after_rejecting_late_old_claim(collaboration_board):
    from agent.crema_collaboration import read_request

    old = create(A, "b", "구판 검토", kind="review", request_id="r-read-latest",
                 attempt_id="a-read-old", message_id="m-read-old", model="review-model",
                 revision={"title": "계획", "content": "구판"}, requirements="기준")["work"]
    latest = create(A, "b", "신판 검토", kind="review", request_id="r-read-latest",
                    attempt_id="a-read-new", message_id="m-read-new", model="review-model",
                    revision={"title": "계획", "content": "신판"}, requirements="기준")["work"]
    latest_key = _key(latest)
    assert turns.collaborate({**latest_key, "action": "claim"})["claimed"] is True
    collaboration_board.receipts[(latest["attempt_id"], latest["message_id"])] = {
        "run_id": "run-read-latest", "session_id": B, "status": "completed",
        "output": json.dumps({"revision": latest["revision"], "verdict": "pass", "findings": [],
                               "evidence": "신판", "previous_evidence": "구판"}),
        "runtime": {"model": "review-model"}}
    assert turns.collaborate({**latest_key, "action": "report", "run_id": "run-read-latest",
                              "status": "completed"})["ok"] is True

    late_claim = turns.collaborate({**_key(old), "action": "claim"})
    assert late_claim["claimed"] is False
    saved = read_request(A, "b", "r-read-latest")
    assert saved["work"]["message_id"] == latest["message_id"]
    assert saved["work"]["status"] == "completed"
    assert saved["work"]["revision"] == latest["revision"]


def test_untrusted_tool_cannot_create_grants(collaboration_board):
    request = {"action": "grant", "session_id": B, "peer_session_id": A,
               "project_id": "project-1", "message_id": "user-message"}
    assert turns.collaborate(request)["ok"] is False
    assert turns.collaborate({**request, "action": "revoke"})["ok"] is False


def test_two_browsers_can_claim_a_work_message_only_once(collaboration_board):
    work = create(A, "b", "한 번만 시작해 주세요", request_id="r-concurrent", attempt_id="a-concurrent",
                  message_id="m-concurrent")["work"]
    body = {**_key(work), "action": "claim"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: turns.collaborate(body), range(2)))
    assert sorted(reply["claimed"] for reply in replies) == [False, True]


def test_two_browsers_get_one_request_id_for_the_same_first_grant(collaboration_board):
    body = {"action": "grant", "session_id": B, "peer_session_id": C,
            "project_id": "project-1", "message_id": "same-user-action"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: turns.collaborate(body, trusted_user=True), range(2)))
    assert replies[0]["request_id"] == replies[1]["request_id"]
    assert sorted(reply.get("duplicate", False) for reply in replies) == [False, True]


def test_requested_model_mismatch_cannot_be_reported_as_complete(collaboration_board):
    work = create(A, "b", "검토 모델을 지켜 주세요", request_id="r-model", attempt_id="a-model",
                  message_id="m-model", model="review-model")["work"]
    key = _key(work)
    turns.collaborate({**key, "action": "claim"})
    collaboration_board.receipts[("a-model", "m-model")] = {
        "run_id": "run-wrong-model", "session_id": B, "status": "running",
        "runtime": {"model": "other-model"}}
    assert turns.collaborate({**key, "action": "accepted", "run_id": "run-wrong-model"})["ok"] is True
    collaboration_board.receipts[("a-model", "m-model")].update(
        status="completed", output="done")
    assert turns.collaborate({**key, "action": "report", "run_id": "run-wrong-model",
                              "status": "completed"})["ok"] is True
    result = turns.read(A, "b", "r-model")["work"]
    assert result["status"] == "failed"
    assert "실제 모델(other-model)" in result["result"]


def test_clients_without_collaboration_capability_do_not_receive_new_work(collaboration_board):
    create(A, "b", "신형 클라이언트용", request_id="r-new", attempt_id="a-new", message_id="m-new")
    turns.set_chats([{"id": "a", "session": A, "title": "기획"},
                     {"id": "b", "session": B, "title": "검토", "parent": "a"}])
    assert "messages" not in turns.state()


def test_cancelling_parent_only_cancels_its_linked_child_work(collaboration_board):
    parent = create(A, "b", "상위 업무", request_id="r-parent", attempt_id="a-parent",
                    message_id="m-parent")["work"]
    parent_key = _key(parent)
    assert turns.collaborate({**parent_key, "action": "claim"})["claimed"] is True
    child = create(B, "a", "하위 업무", request_id="r-child", attempt_id="a-child",
                   message_id="m-child", parent_request_id="r-parent")["work"]
    independent = create(A, "b", "독립 업무", request_id="r-independent", attempt_id="a-independent",
                         message_id="m-independent")["work"]
    collaboration_board.receipts[("a-parent", "m-parent")] = {
        "run_id": "run-parent", "session_id": B, "status": "cancelled", "output": "stopped"}
    assert turns.collaborate({**parent_key, "action": "report", "run_id": "run-parent",
                              "status": "cancelled"}) == {"ok": True}
    state = turns.state()["messages"]
    items = [item["work"] for row in state for item in row["items"] if item.get("work")]
    child_items = [work for work in items if work.get("message_id") == child["message_id"]]
    independent_items = [work for work in items if work.get("message_id") == independent["message_id"]]
    assert child_items[-1]["status"] == "cancelled"
    assert independent_items[-1]["status"] == "queued"


def test_parent_result_stays_open_while_a_child_waits_for_approval(collaboration_board):
    parent = create(A, "b", "상위 업무", request_id="r-parent-wait", attempt_id="a-parent-wait",
                    message_id="m-parent-wait")["work"]
    parent_key = _key(parent)
    turns.collaborate({**parent_key, "action": "claim"})
    waiting_child = create(B, "a", "승인 필요한 하위 업무", request_id="r-child-wait",
                           attempt_id="a-child-wait", message_id="m-child-wait",
                           parent_request_id="r-parent-wait")["work"]
    done_child = create(B, "a", "끝난 하위 업무", request_id="r-child-done",
                        attempt_id="a-child-done", message_id="m-child-done",
                        parent_request_id="r-parent-wait")["work"]
    turns.collaborate({**_key(waiting_child), "action": "claim"})
    turns.collaborate({**_key(done_child), "action": "claim"})
    collaboration_board.receipts[("a-parent-wait", "m-parent-wait")] = {
        "run_id": "run-parent-wait", "session_id": B, "status": "completed", "output": "내 작업 완료"}
    turns.collaborate({**parent_key, "action": "report", "run_id": "run-parent-wait", "status": "completed"})
    collaboration_board.receipts[("a-child-done", "m-child-done")] = {
        "run_id": "run-child-done", "session_id": A, "status": "completed", "output": "완료"}
    turns.collaborate({**_key(done_child), "action": "report", "run_id": "run-child-done",
                       "status": "completed"})
    collaboration_board.receipts[("a-child-wait", "m-child-wait")] = {
        "run_id": "run-child-wait", "session_id": A, "status": "waiting_for_approval", "output": ""}
    turns.collaborate({**_key(waiting_child), "action": "report", "run_id": "run-child-wait",
                       "status": "waiting"})
    result = turns.read(A, "b", "r-parent-wait")["work"]
    assert result["own_status"] == "completed"
    assert result["status"] == "waiting"
    assert {child["status"] for child in result["children"]} == {"completed", "waiting"}


def test_completed_parent_assignment_cannot_authorize_another_child_request(collaboration_board):
    parent = create(A, "b", "위임", request_id="r-finished-parent", attempt_id="a-finished-parent",
                    message_id="m-finished-parent")["work"]
    key = _key(parent)
    turns.collaborate({**key, "action": "claim"})
    collaboration_board.receipts[("a-finished-parent", "m-finished-parent")] = {
        "run_id": "run-finished-parent", "session_id": B, "status": "completed", "output": "끝"}
    turns.collaborate({**key, "action": "report", "run_id": "run-finished-parent", "status": "completed"})
    result = create(B, "a", "끝난 일에 하위 업무 추가", request_id="r-new-child", attempt_id="a-new-child",
                    message_id="m-new-child", parent_request_id="r-finished-parent")
    assert "active request" in result["error"]


def test_moved_chat_cannot_read_work_from_its_previous_project(collaboration_board):
    work = create(A, "b", "프로젝트 자료", request_id="r-moved", attempt_id="a-moved",
                  message_id="m-moved")["work"]
    turns.set_chats([{"id": "a", "session": A, "title": "기획", "project_id": "project-2", "collaboration": 1},
                     {"id": "b", "session": B, "title": "검토", "project_id": "project-1",
                      "collaboration": 1, "parent": "a"}])
    assert "not authorized" in turns.read(A, "b", "r-moved")["error"]


def test_same_unanswered_question_cannot_start_a_mutual_wait_cycle(collaboration_board):
    parent = create(A, "b", "협의할 일", request_id="r-question-parent", attempt_id="a-question-parent",
                    message_id="m-question-parent")["work"]
    turns.collaborate({**_key(parent), "action": "claim"})
    question = create(B, "a", "이 응답 형식을 유지하나요?", request_id="r-question-child",
                      attempt_id="a-question", message_id="m-question", kind="question",
                      parent_request_id="r-question-parent")["work"]
    question_key = _key(question)
    turns.collaborate({**question_key, "action": "claim"})
    collaboration_board.receipts[(question["attempt_id"], question["message_id"])] = {
        "run_id": "run-question", "session_id": A, "status": "waiting_for_clarify", "output": ""}
    turns.collaborate({**question_key, "action": "report", "run_id": "run-question", "status": "waiting"})
    duplicate = create(A, "b", "  이 응답 형식을 유지하나요?  ", request_id="r-question-parent",
                       attempt_id="a-question-back", message_id="m-question-back", kind="question")
    assert "already waiting" in duplicate["error"]
    create(A, "b", "응답 형식은 JSON입니다.", request_id="r-question-parent",
           attempt_id="a-question-answer", message_id="m-question-answer", kind="answer")
    follow_up = create(A, "b", "이 응답 형식을 유지하나요?", request_id="r-question-parent",
                       attempt_id="a-question-followup", message_id="m-question-followup", kind="question")
    assert follow_up["ok"] is True


def test_engine_owned_budget_exit_reaches_parent_as_partial_or_waiting(collaboration_board):
    ordinary = create(A, "b", "부분 작업", request_id="r-budget", attempt_id="a-budget",
                      message_id="m-budget")["work"]
    turns.collaborate({**_key(ordinary), "action": "claim"})
    collaboration_board.receipts[("a-budget", "m-budget")] = {
        "run_id": "run-budget", "session_id": B, "status": "failed", "output": "확인한 자료",
        "turn_exit_reason": "max_iterations_reached(12/12)"}
    turns.collaborate({**_key(ordinary), "action": "report", "run_id": "run-budget", "status": "failed"})
    assert turns.read(A, "b", "r-budget")["work"]["status"] == "partial"
    assert "실행 반복 한도" in turns.read(A, "b", "r-budget")["work"]["result"]

    review = create(A, "b", "문서 검토", kind="review", request_id="r-review-budget",
                    attempt_id="a-review-budget", message_id="m-review-budget", model="review-model",
                    revision={"title": "계획", "content": "본문"}, requirements="기준")["work"]
    turns.collaborate({**_key(review), "action": "claim"})
    collaboration_board.receipts[("a-review-budget", "m-review-budget")] = {
        "run_id": "run-review-budget", "session_id": B, "status": "failed", "output": "",
        "turn_exit_reason": "review_input_budget_exhausted"}
    turns.collaborate({**_key(review), "action": "report", "run_id": "run-review-budget", "status": "failed"})
    result = turns.read(A, "b", "r-review-budget")["work"]
    assert result["status"] == "waiting"
    assert "검토 입력 사용 한도" in result["review"]["reason"]


def test_interrupt_is_bound_to_the_exact_original_run_and_revalidated(collaboration_board):
    original = create(A, "b", "원래 작업", request_id="r-interrupt-target", attempt_id="a-target",
                      message_id="m-target")["work"]
    key = _key(original)
    turns.collaborate({**key, "action": "claim"})
    collaboration_board.receipts[("a-target", "m-target")] = {
        "run_id": "run-target", "session_id": B, "status": "running"}
    interrupt = create(A, "b", "새 지시", mode="interrupt", request_id="r-interrupt-new",
                       attempt_id="a-interrupt", message_id="m-interrupt",
                       target_request_id="r-interrupt-target", target_attempt_id="a-target",
                       target_message_id="m-target")["work"]
    assert interrupt["target_run_id"] == "run-target"
    collaboration_board.receipts[("a-target", "m-target")].update(status="cancelled")
    assert turns.collaborate({**key, "action": "report", "run_id": "run-target", "status": "cancelled"}) == {"ok": True}
    # Match the app order: target terminal, target report persisted, then receive and validate new work.
    assert turns.collaborate({**_key(interrupt), "action": "claim"})["claimed"] is True
    assert turns.collaborate({**_key(interrupt), "action": "validate", "project_id": "project-1"})["valid"] is True
    denied = create(A, "b", "잘못된 대상", mode="interrupt", request_id="r-other",
                    attempt_id="a-other", message_id="m-other", target_request_id="r-interrupt-target",
                    target_attempt_id="a-target", target_message_id="m-target")
    assert "already terminal" in denied["error"]


def test_interrupt_cannot_be_created_from_a_grant_without_an_assigned_run(collaboration_board):
    grant = turns.collaborate({"action": "grant", "session_id": B, "peer_session_id": C,
                               "project_id": "project-1", "message_id": "interrupt-grant"},
                              trusted_user=True)
    result = create(B, "c", "멈추고 새 일을 하세요", mode="interrupt", request_id=grant["request_id"],
                    attempt_id="a-no-target", message_id="m-no-target",
                    target_request_id=grant["request_id"], target_attempt_id="none",
                    target_message_id="none")
    assert "target" in result["error"]


def test_interrupt_stop_permission_does_not_authorize_an_unrelated_new_request(collaboration_board):
    grant = turns.collaborate({"action": "grant", "session_id": B, "peer_session_id": C,
                               "project_id": "project-1", "message_id": "switch-grant"},
                              trusted_user=True)
    request_id = grant["request_id"]
    original = create(B, "c", "원래 업무", request_id=request_id, attempt_id="a-switch-target",
                      message_id="m-switch-target")["work"]
    original_key = _key(original)
    turns.collaborate({**original_key, "action": "claim"})
    collaboration_board.receipts[("a-switch-target", "m-switch-target")] = {
        "run_id": "run-switch-target", "session_id": C, "status": "running"}
    unrelated = create(B, "c", "권한 없는 새 업무", mode="interrupt", request_id="other-request",
                       attempt_id="a-switch-other", message_id="m-switch-other",
                       target_request_id=request_id, target_attempt_id="a-switch-target",
                       target_message_id="m-switch-target")
    assert "no active user authorization" in unrelated["error"]
    continuation = create(B, "c", "같은 업무의 후속 지시", mode="interrupt", request_id=request_id,
                          attempt_id="a-switch-next", message_id="m-switch-next",
                          target_request_id=request_id, target_attempt_id="a-switch-target",
                          target_message_id="m-switch-target")["work"]
    collaboration_board.receipts[("a-switch-target", "m-switch-target")].update(status="cancelled")
    assert turns.collaborate({**original_key, "action": "report", "run_id": "run-switch-target",
                              "status": "cancelled"}) == {"ok": True}
    assert turns.collaborate({**_key(continuation), "action": "claim"})["claimed"] is True
    assert turns.collaborate({**_key(continuation), "action": "validate", "project_id": "project-1"})["valid"] is True
