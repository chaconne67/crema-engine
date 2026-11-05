"""Crema: a /v1/runs request with ``no_tools`` (the app's throwaway turns: the first title, 자세히 물어보기) gets an
agent offered no tool at all, so a model that runs the user's request anyway cannot act on it (2026-11-05: a title
turn made the user's scheduled task a second time). A request without it keeps the platform's toolsets."""

from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from tests.gateway.test_api_server_run_work_folder import _wait_done
from tests.gateway.test_api_server_runs import _create_runs_app, _make_adapter


@pytest_asyncio.fixture
async def adapter():
    adapter = _make_adapter()
    yield adapter
    await adapter.disconnect()


@pytest.fixture
def made(monkeypatch, adapter):
    """The toolsets each agent the runs make is given, in order; the platform's own are terminal and file."""
    toolsets = []

    def agent(**kwargs):
        toolsets.append(kwargs["enabled_toolsets"])
        made = MagicMock()
        made.session_prompt_tokens = made.session_completion_tokens = made.session_total_tokens = 0
        made.run_conversation.return_value = {"final_response": "역할: 대화\n제목: 안녕"}
        return made

    monkeypatch.setattr("run_agent.AIAgent", agent)
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {"provider": "openai-codex"})
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "gpt-test")
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *_: {"terminal", "file"})
    monkeypatch.setattr(adapter, "_ensure_session_db", lambda: None)
    return toolsets


@pytest.mark.asyncio
async def test_a_run_with_no_tools_is_offered_none_and_others_keep_theirs(adapter, made):
    async with TestClient(TestServer(_create_runs_app(adapter))) as cli:
        for session, extra in (("aux-1", {"no_tools": True}), ("chat-1", {})):
            resp = await cli.post("/v1/runs", json={"input": "매일 9시에 알려 줘", "session_id": session, **extra})
            assert resp.status == 202
            assert (await _wait_done(cli, (await resp.json())["run_id"]))["status"] == "completed"
    assert made == [[], ["file", "terminal"]]


@pytest.mark.asyncio
async def test_no_tools_that_is_not_true_or_false_is_refused(adapter, made):
    async with TestClient(TestServer(_create_runs_app(adapter))) as cli:
        resp = await cli.post("/v1/runs", json={"input": "a", "session_id": "aux-2", "no_tools": "yes"})
        assert resp.status == 400
        assert (await resp.json())["error"]["code"] == "invalid_no_tools"
    assert made == []
