"""Crema's question and approval shape on the session-authority run path.

Every ``/v1/runs`` turn runs under the session authority. Its prompts and the answers it takes have
the authority's shape (``prompt_id``/``execution_generation``, one ``answer`` string); the Crema app
reads and answers ``clarify.request`` with ``clarify_id``/``questions`` and ``approval.request`` with
``request_id``/``pattern_key``. The run bridge gives each side its own shape.
"""
import json
from types import SimpleNamespace

import pytest

from gateway.platforms import api_server as api_server_mod
from gateway.platforms import api_server_runs as runs

CLARIFY = {"kind": "clarify", "prompt_id": "c1", "execution_generation": 3, "question": "이 범위로 진행할까요?",
           "choices": ["지메일 연결 진행 (Recommended)", "준비 상태만 확인"], "multi_select": False}
APPROVAL = {"kind": "approval", "prompt_id": "a1", "execution_generation": 3, "command": "send_mail to=a@b.c",
            "description": "mail send", "pattern_key": "mail_send", "choices": ["once", "deny"]}


def test_a_clarify_prompt_reaches_the_app_as_one_question_with_bare_choices():
    event = runs._crema_control_event("run-1", "clarify.request", CLARIFY)
    assert (event["event"], event["run_id"], event["clarify_id"]) == ("clarify.request", "run-1", "c1")
    assert event["questions"] == [{"qid": "c1", "question": "이 범위로 진행할까요?", "context": "",
                                   "choices": ["지메일 연결 진행", "준비 상태만 확인"], "descriptions": [],
                                   "multi_select": False}]


def test_the_app_card_drops_the_messaging_skip_hint():
    from agent.i18n import t
    prompt = {**CLARIFY, "question": CLARIFY["question"] + "\n" + t("gateway.clarify.skip_hint")}
    event = runs._crema_control_event("run-1", "clarify.request", prompt)
    assert event["questions"][0]["question"] == "이 범위로 진행할까요?"
    assert event["question"] == prompt["question"]


def test_an_approval_prompt_keeps_its_id_kind_and_choices():
    event = runs._crema_control_event("run-1", "approval.request", APPROVAL)
    assert (event["event"], event["request_id"], event["pattern_key"]) == ("approval.request", "a1", "mail_send")
    assert event["command"] == "send_mail to=a@b.c"
    assert event["choices"] == ["once", "deny"]


def test_a_settled_prompt_passes_as_it_is():
    event = runs._crema_control_event("run-1", "clarify.settled", {"prompt_id": "c1", "execution_generation": 3})
    assert (event["event"], event["prompt_id"]) == ("clarify.settled", "c1")


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "body", "given"), [
    ("clarify", {"clarify_id": "c1", "answers": {"c1": "준비 상태만 확인"}}, {"answer": "준비 상태만 확인"}),
    ("clarify", {"clarify_id": "c1", "answers": {"c1": ["가", "나"]}}, {"answer": json.dumps(["가", "나"], ensure_ascii=False)}),
    ("clarify", {"clarify_id": "c1", "text": "내일 다시 물어봐"}, {"answer": "내일 다시 물어봐"}),
    ("approval", {"request_id": "a1", "choice": "once"}, {"choice": "once"}),
])
async def test_the_apps_answer_reaches_the_authority_for_the_runs_generation(monkeypatch, kind, body, given):
    from gateway.platforms import api_server_authority_runs as authority_runs
    seen = []

    async def respond_run(adapter, run_id, answer, *, kind):
        seen.append((run_id, kind, answer))
        return {"status": "resolved"}

    monkeypatch.setattr(authority_runs, "respond_run", respond_run)
    monkeypatch.setattr(authority_runs, "run_admission", lambda adapter, run_id: (object(), {"generation": 3}))
    response = await runs._respond_authority_run(object(), "run-1", body, kind=kind, _api_server=api_server_mod)
    assert response.status == 200
    prompt_id = "c1" if kind == "clarify" else "a1"
    assert seen == [("run-1", kind, {"request_id": prompt_id, "execution_generation": 3, **given})]


@pytest.mark.parametrize(("prompt", "kind"), [(CLARIFY, "clarify"), (APPROVAL, "approval")])
def test_a_poll_shows_the_waiting_prompt_as_the_stream_showed_it(monkeypatch, prompt, kind):
    from gateway.platforms import api_server_authority_runs as authority_runs
    monkeypatch.setattr(authority_runs, "run_projection", lambda adapter, run_id: {
        "pending_controls": [prompt], "run_id": run_id, "status": "running", "execution_generation": 3})
    streamed = runs._crema_control_event("run-1", f"{kind}.request", prompt)
    adapter = SimpleNamespace(_run_statuses={"run-1": {"status": f"waiting_for_{kind}", kind: streamed}})
    polled = runs._durable_run_status(adapter, None, "run-1")
    assert polled["status"] == f"waiting_for_{kind}"
    assert polled[kind] is streamed


def test_a_poll_after_the_stream_closed_still_shows_the_prompt(monkeypatch):
    from gateway.platforms import api_server_authority_runs as authority_runs
    monkeypatch.setattr(authority_runs, "run_projection", lambda adapter, run_id: {
        "pending_controls": [CLARIFY], "run_id": run_id, "status": "running", "execution_generation": 3})
    polled = runs._durable_run_status(SimpleNamespace(_run_statuses={}), None, "run-1")
    assert polled["status"] == "waiting_for_clarify"
    assert polled["clarify"]["clarify_id"] == "c1"


async def _run_event(client, path, name, headers):
    async with client.get(path + "/events", headers=headers) as stream:
        async for raw in stream.content:
            line = raw.decode().strip()
            if line.startswith("data: ") and json.loads(line[6:]).get("event") == name:
                return json.loads(line[6:])
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(("pc_approval", "choice"), [(False, "once"), (True, "once"), (True, "deny")])
async def test_the_app_sees_and_answers_a_runs_approval_and_question(api, owner, monkeypatch, pc_approval, choice):
    """The whole path: ``POST /v1/runs`` → authority turn → the app-shaped events on the run's stream and poll →
    the app-shaped answers resolve the turn's real waiters."""
    import asyncio
    from gateway.session_results import execution_result
    from tests.gateway.test_api_unknown_resolution import _AUTH_HEADERS, _runs_client
    from tests.gateway.test_chat_completions_approval import session_row
    from tools import clarify_gateway
    from tools.approval_gateway_wait import _await_gateway_decision
    monkeypatch.setattr("tools.approval_context._get_approval_timeout", lambda: 10)
    approval_data = ({"command": "PC 자료 가져오기", "description": "PC판 자료 사본", "pattern_key": "pc-task:42",
                      "pc_task": 42, "allow_session": False, "allow_permanent": False} if pc_approval else
                     {"command": "send_mail to=a@b.c", "description": "mail send", "pattern_key": "mail_send"})
    got = {}

    async def handle(event):
        sid = event.source.chat_id
        live, generation = owner.sessions[sid], session_row(owner.persistence, sid)['runtime_generation']
        got["approval"] = await asyncio.to_thread(
            _await_gateway_decision, live.route, lambda data: owner.register_approval(sid, generation, live.route, data),
            approval_data)
        entry = clarify_gateway.register(clarify_id="cq1", session_key="k", question="어느 쪽으로 할까요?",
                                         choices=["가 (Recommended)", "나"], multi_select=False)
        owner.register_clarify(sid, generation, entry)
        got["answer"] = await asyncio.to_thread(clarify_gateway.wait_for_response, "cq1", 10)
        execution_result.get()['result'] = {'final_response': 'ok', 'messages': []}
        return 'ok'

    owner.runner._handle_message = handle
    client = _runs_client(api)
    await client.start_server()
    try:
        accepted = await client.post('/v1/runs', json={'input': 'hi', 'session_id': 'api-session'}, headers=_AUTH_HEADERS)
        assert accepted.status == 202, await accepted.text()
        path = '/v1/runs/' + (await accepted.json())['run_id']
        approval = await asyncio.wait_for(_run_event(client, path, "approval.request", _AUTH_HEADERS), 10)
        assert approval["pattern_key"] == approval_data["pattern_key"] and approval["request_id"], approval
        polled = await (await client.get(path, headers=_AUTH_HEADERS)).json()
        assert polled["status"] == "waiting_for_approval" and polled["approval"]["request_id"] == approval["request_id"]
        for card in (approval, polled["approval"]):
            if pc_approval:
                assert card.get("pc_task") == 42
                assert card["choices"] == ["once", "deny"]
            else:
                assert "pc_task" not in card
        answered = await client.post(path + "/approval", json={"choice": choice, "request_id": approval["request_id"]},
                                     headers=_AUTH_HEADERS)
        assert answered.status == 200, await answered.text()
        question = await asyncio.wait_for(_run_event(client, path, "clarify.request", _AUTH_HEADERS), 10)
        assert question["clarify_id"] == "cq1" and question["questions"][0]["choices"] == ["가", "나"], question
        polled = await (await client.get(path, headers=_AUTH_HEADERS)).json()
        assert polled["status"] == "waiting_for_clarify" and polled["clarify"]["clarify_id"] == "cq1"
        answered = await client.post(path + "/clarify", json={"clarify_id": "cq1", "answers": {"cq1": "나"}},
                                     headers=_AUTH_HEADERS)
        assert answered.status == 200, await answered.text()
        for _ in range(100):
            if "answer" in got:
                break
            await asyncio.sleep(0.05)
        assert got["approval"]["choice"] == choice and got["answer"] == "나"
    finally:
        for task in list(api._active_run_tasks.values()):
            task.cancel()
        await client.close()
