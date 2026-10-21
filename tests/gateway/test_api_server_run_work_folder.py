"""Crema: a /v1/runs run works in the folder its request names (``cwd``: the chat's project folder,
else Crema's own folder), never in the folder the engine process started in or the home folder.

The terminal tool, relative paths of the file tools and the agent's own "current working directory"
(the system prompt line) all follow that folder, per run: one engine serves many chats at once.
"""

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from tests.gateway.test_api_server_runs import _create_runs_app, _make_adapter


def _folder_agent(seen, *, gate=None, during=None):
    """A stand-in agent whose turn records where its tools would work."""
    agent = MagicMock()
    agent.session_prompt_tokens = agent.session_completion_tokens = agent.session_total_tokens = 0

    def run(user_message=None, conversation_history=None, task_id=None):
        from agent.runtime_cwd import resolve_agent_cwd
        from tools import terminal_tool
        from tools.file_tools_paths import _resolve_path_for_task

        if gate is not None:
            gate.wait(timeout=5)
        plan = terminal_tool._plan_execution(
            "pwd", task_id=task_id, timeout=None, background=False, _host_local=False)
        seen[user_message] = {
            "agent": str(resolve_agent_cwd()),
            "terminal": plan.cwd,
            "file": str(_resolve_path_for_task("note.txt", task_id)),
        }
        if during is not None:
            during(task_id)
        return {"final_response": "done"}

    agent.run_conversation.side_effect = run
    return agent


async def _wait_done(cli, run_id):
    for _ in range(100):
        status = await (await cli.get(f"/v1/runs/{run_id}")).json()
        if status["status"] in {"completed", "failed", "cancelled"}:
            return status
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish")


@pytest.fixture
def engine_start_folder(tmp_path, monkeypatch):
    """The folder the engine was started in (TERMINAL_CWD), which a run naming its own folder must not use."""
    start = tmp_path / "start"
    start.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(start))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    return start


@pytest_asyncio.fixture
async def adapter():
    """An adapter closed after the test (its stores and session DBs), so the suite does not run out of files."""
    adapter = _make_adapter()
    yield adapter
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_run_works_in_the_folder_it_names(tmp_path, engine_start_folder, adapter):
    project = tmp_path / "project"
    project.mkdir()
    seen = {}
    async with TestClient(TestServer(_create_runs_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=_folder_agent(seen)):
            resp = await cli.post("/v1/runs", json={"input": "a", "session_id": "chat-a", "cwd": str(project)})
            assert resp.status == 202
            status = await _wait_done(cli, (await resp.json())["run_id"])
    assert status["status"] == "completed"
    assert seen["a"] == {"agent": str(project), "terminal": str(project), "file": str(project / "note.txt")}


@pytest.mark.asyncio
async def test_concurrent_runs_keep_their_own_folders(tmp_path, engine_start_folder, adapter):
    folders = {name: tmp_path / name for name in ("a", "b")}
    for folder in folders.values():
        folder.mkdir()
    seen = {}
    # Both turns are inside their run at the same time before either reads its folder.
    gate = threading.Barrier(2)
    async with TestClient(TestServer(_create_runs_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", side_effect=lambda **_: _folder_agent(seen, gate=gate)):
            runs = []
            for name, folder in folders.items():
                resp = await cli.post(
                    "/v1/runs", json={"input": name, "session_id": f"chat-{name}", "cwd": str(folder)})
                assert resp.status == 202
                runs.append((await resp.json())["run_id"])
            for run_id in runs:
                assert (await _wait_done(cli, run_id))["status"] == "completed"
    for name, folder in folders.items():
        assert seen[name] == {"agent": str(folder), "terminal": str(folder), "file": str(folder / "note.txt")}


@pytest.mark.asyncio
async def test_a_chat_moved_to_another_folder_works_there_next_turn(tmp_path, engine_start_folder, adapter):
    """A chat dragged out of its project (or into another) works in the new folder from its next turn,
    even after its last turn moved into a subfolder of the old one."""
    old, new = tmp_path / "old", tmp_path / "new"
    (old / "sub").mkdir(parents=True)
    new.mkdir()
    seen = {}

    def cd_into_sub(task_id):
        from tools.terminal_tool import record_session_cwd

        record_session_cwd(task_id, str(old / "sub"))

    async with TestClient(TestServer(_create_runs_app(adapter))) as cli:
        for name, folder, during in (("first", old, cd_into_sub), ("second", new, None)):
            with patch.object(adapter, "_create_agent", return_value=_folder_agent(seen, during=during)):
                resp = await cli.post(
                    "/v1/runs", json={"input": name, "session_id": "chat-moved", "cwd": str(folder)})
                assert resp.status == 202
                assert (await _wait_done(cli, (await resp.json())["run_id"]))["status"] == "completed"
    assert seen["second"] == {"agent": str(new), "terminal": str(new), "file": str(new / "note.txt")}


@pytest.mark.asyncio
@pytest.mark.parametrize("cwd", ["relative/folder", "missing", "", 5])
async def test_a_folder_that_is_not_there_is_refused_before_any_work(tmp_path, engine_start_folder, adapter, cwd):
    """The run never falls back to another folder: a cwd that is not an existing absolute folder is a 400."""
    if cwd == "missing":
        cwd = str(tmp_path / "missing")
    async with TestClient(TestServer(_create_runs_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent") as create:
            resp = await cli.post("/v1/runs", json={"input": "a", "session_id": "chat-a", "cwd": cwd})
            assert resp.status == 400
            assert (await resp.json())["error"]["code"] == "invalid_cwd"
    create.assert_not_called()
