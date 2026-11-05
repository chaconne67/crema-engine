"""Crema: a run's clarify question goes out as one clarify.request event and the run waits for
POST /v1/runs/{id}/clarify (picked answers, or words written instead of picking)."""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from tools.clarify_tool import clarify_tool

QUESTIONS = [{"question": "How should the app split editions?", "choices": ["One build", "A build each"]},
             {"question": "Who picks the edition?", "choices": ["The account", "The installer"]}]


def _app(adapter):
    app = web.Application()
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    app.router.add_get("/v1/runs/{run_id}/events", adapter._handle_run_events)
    app.router.add_post("/v1/runs/{run_id}/clarify", adapter._handle_run_clarify)
    return app


def _asking_agent():
    """An agent whose turn calls the real clarify tool with the callback the run gave it."""
    agent = MagicMock()
    agent.session_prompt_tokens = agent.session_completion_tokens = agent.session_total_tokens = 0
    agent.run_conversation.side_effect = lambda **_: {
        "final_response": clarify_tool(question="", questions=QUESTIONS, callback=agent.clarify_callback)}
    return agent


async def _ask(cli):
    """Start a run and read its stream up to the question."""
    run_id = (await (await cli.post("/v1/runs", json={"input": "plan it"})).json())["run_id"]
    events = await cli.get(f"/v1/runs/{run_id}/events")
    async for line in events.content:
        if line.startswith(b"data:") and (event := json.loads(line[5:]))["event"] == "clarify.request":
            return run_id, event
    raise AssertionError("no clarify.request")


async def _output(cli, run_id):
    for _ in range(60):
        status = await (await cli.get(f"/v1/runs/{run_id}")).json()
        if status["status"] == "completed":
            return [r["user_response"] for r in json.loads(status["output"])["responses"]]
        await asyncio.sleep(0.05)
    raise AssertionError("run did not complete")


@pytest.mark.asyncio
async def test_picked_answers_reach_the_tool():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    with patch.object(adapter, "_create_agent", return_value=_asking_agent()):
        async with TestClient(TestServer(_app(adapter))) as cli:
            run_id, event = await _ask(cli)
            # Every question in one event, the choices bare (the client marks the first as recommended).
            assert [q["choices"] for q in event["questions"]] == [q["choices"] for q in QUESTIONS]
            assert (await (await cli.get(f"/v1/runs/{run_id}")).json())["status"] == "waiting_for_clarify"
            reply = await cli.post(f"/v1/runs/{run_id}/clarify", json={
                "clarify_id": event["clarify_id"], "answers": {"q0": "One build", "q1": "The installer"}})
            assert reply.status == 200
            assert await _output(cli, run_id) == ["One build", "The installer"]


@pytest.mark.asyncio
async def test_written_words_answer_every_question_and_a_stale_id_is_refused():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    with patch.object(adapter, "_create_agent", return_value=_asking_agent()):
        async with TestClient(TestServer(_app(adapter))) as cli:
            run_id, event = await _ask(cli)
            stale = await cli.post(f"/v1/runs/{run_id}/clarify", json={"clarify_id": "old", "text": "all A"})
            assert (stale.status, (await stale.json())["error"]["code"]) == (409, "clarify_not_pending")
            words = "all recommended, the second one B"
            await cli.post(f"/v1/runs/{run_id}/clarify", json={"clarify_id": event["clarify_id"], "text": words})
            assert await _output(cli, run_id) == [words, words]
