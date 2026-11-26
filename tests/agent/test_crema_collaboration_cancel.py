import asyncio
from types import SimpleNamespace

import pytest

from agent import crema_file_turns as turns
from agent import crema_collaboration as collaboration


def test_cancel_descendants_requests_exact_run_stop_and_waits_for_receipt(monkeypatch):
    root_child = {"request_id": "child", "parent_request_id": "root", "sender": "a", "recipient": "b",
                  "status": "accepted", "run_id": "run-child", "message_id": "m1"}
    grandchild = {"request_id": "grandchild", "parent_request_id": "child", "sender": "b", "recipient": "c",
                  "status": "accepted", "run_id": "run-grandchild", "message_id": "m2"}
    unrelated = {"request_id": "other", "parent_request_id": "other-root", "sender": "a", "recipient": "c",
                 "status": "accepted", "run_id": "run-other", "message_id": "m3"}
    children = {"root": [root_child], "child": [grandchild], "grandchild": [], "other-root": [unrelated]}
    receipts = {
        ("child", "m1"): {"run_id": "run-child", "session_id": "b", "status": "running"},
        ("grandchild", "m2"): {"run_id": "run-grandchild", "session_id": "c", "status": "running"},
    }
    appended = []
    stop_requests = []
    monkeypatch.setattr(collaboration, "_child_work", lambda _conn, parent: children.get(parent, []))
    monkeypatch.setattr(collaboration, "_run_receipt", lambda work: receipts.get((work["request_id"], work["message_id"])))
    monkeypatch.setattr(collaboration, "_request_work_run_stop",
                        lambda _api, work: stop_requests.append(work["run_id"]) or True)
    monkeypatch.setattr(turns, "_task_for", lambda _conn, session: session)
    monkeypatch.setattr(collaboration, "_append_locked", lambda _conn, recipient, sender, work:
                        appended.append((recipient, sender, work)))
    monkeypatch.setattr(turns, "_api", SimpleNamespace())

    collaboration._cancel_descendants(object(), "root")

    assert stop_requests == ["run-child", "run-grandchild"]
    assert [row[2]["request_id"] for row in appended] == ["child", "grandchild"]
    assert all(row[2]["status"] == "stopping" for row in appended)
    assert all("종료를 기다리고 있습니다" in row[2]["waiting_reason"] for row in appended)


def test_cancel_descendants_requires_matching_terminal_execution_evidence(monkeypatch):
    missing = {"request_id": "missing", "parent_request_id": "root", "sender": "a", "recipient": "b",
               "status": "accepted", "run_id": "run-missing", "message_id": "m1"}
    stopped = {"request_id": "stopped", "parent_request_id": "root", "sender": "a", "recipient": "c",
               "status": "accepted", "run_id": "run-stopped", "message_id": "m2"}
    unrelated = {"request_id": "unrelated", "parent_request_id": "elsewhere", "sender": "a", "recipient": "d",
                 "status": "accepted", "run_id": "run-unrelated", "message_id": "m3"}
    children = {"root": [missing, stopped], "missing": [], "stopped": []}
    receipts = {("missing", "m1"): None,
                ("stopped", "m2"): {"run_id": "run-stopped", "session_id": "c", "status": "cancelled"}}
    appended = []
    stop_requests = []
    monkeypatch.setattr(collaboration, "_child_work", lambda _conn, parent: children.get(parent, []))
    monkeypatch.setattr(collaboration, "_run_receipt", lambda work: receipts.get((work["request_id"], work["message_id"])))
    monkeypatch.setattr(collaboration, "_request_work_run_stop",
                        lambda _api, work: stop_requests.append(work["run_id"]) or True)
    monkeypatch.setattr(turns, "_task_for", lambda _conn, session: session)
    monkeypatch.setattr(collaboration, "_append_locked", lambda _conn, recipient, sender, work:
                        appended.append(work))
    monkeypatch.setattr(turns, "_api", SimpleNamespace())

    collaboration._cancel_descendants(object(), "root")

    by_request = {work["request_id"]: work for work in appended}
    assert set(by_request) == {"missing", "stopped"}
    assert by_request["missing"]["status"] == "check_required"
    assert "증거를 확인할 수 없습니다" in by_request["missing"]["waiting_reason"]
    assert by_request["stopped"]["status"] == "cancelled"
    assert stop_requests == []


@pytest.mark.asyncio
async def test_request_work_run_stop_schedules_on_adapter_loop(monkeypatch):
    called = []

    async def stop_work_run(_api, work, *, _api_server):
        called.append((work["run_id"], _api_server))
        return {"status": "stopping"}

    from gateway.platforms import api_server, api_server_runs
    monkeypatch.setattr(api_server_runs, "stop_work_run", stop_work_run)
    api = SimpleNamespace(_work_loop=asyncio.get_running_loop())

    assert collaboration._request_work_run_stop(api, {"run_id": "run-target"}) is True
    await asyncio.sleep(0)
    assert called == [("run-target", api_server)]


@pytest.mark.asyncio
async def test_shared_stop_run_uses_authority_stop_path(monkeypatch):
    called = []

    async def authority_stop(adapter, run_id):
        called.append((adapter, run_id))
        return {"run_id": run_id, "status": "stopping"}

    from gateway.platforms import api_server, api_server_authority_runs, api_server_runs
    monkeypatch.setattr(api_server_authority_runs, "stop_run", authority_stop)
    api = SimpleNamespace(gateway_runner=SimpleNamespace(session_authority=object()))
    result = await api_server_runs.stop_run(api, "run-target", {"status": "running"}, None, None,
                                            _api_server=api_server)
    assert result == {"run_id": "run-target", "status": "stopping"}
    assert called == [(api, "run-target")]


def test_reconcile_stopping_updates_only_after_matching_terminal_receipt(monkeypatch):
    work = {"request_id": "child", "attempt_id": "attempt", "message_id": "message",
            "sender": "a", "recipient": "b", "run_id": "run-1", "status": "stopping"}
    receipts = {"run-1": {"run_id": "run-1", "session_id": "b", "status": "running"}}
    appended = []
    conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(turns, "_board", lambda: conn)
    monkeypatch.setattr(collaboration, "_all_work", lambda _conn: [work])
    monkeypatch.setattr(collaboration, "_run_receipt", lambda item: receipts[item["run_id"]])
    monkeypatch.setattr(turns, "_task_for", lambda _conn, session: session)
    monkeypatch.setattr(collaboration, "_append_locked", lambda _conn, recipient, sender, item:
                        appended.append(item))

    collaboration.reconcile_stopping()
    assert appended == []

    receipts["run-1"] = {"run_id": "run-1", "session_id": "b", "status": "cancelled", "output": "ended"}
    collaboration.reconcile_stopping()
    assert appended[0]["status"] == "cancelled"
    assert appended[0]["result"] == "ended"
