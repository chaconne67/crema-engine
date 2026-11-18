"""A run's question remains answerable without its original event stream."""

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms.api_server_runs import _http_routes
from tools import clarify_gateway


@pytest.mark.asyncio
async def test_pending_clarify_status_matches_stream_and_is_cleared_after_reply(monkeypatch):
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    agent = MagicMock()

    def converse(*args, **kwargs):
        answer = agent.clarify_callback("Pick a tone", ["Casual", "Formal"])
        return {"final_response": f"Tone: {answer}"}

    agent.run_conversation.side_effect = converse
    monkeypatch.setattr(adapter, "_create_agent", lambda **kwargs: agent)
    app = web.Application()
    app.router.add_routes([web.route(method, path, handler) for method, path, handler in _http_routes(adapter)])

    async with TestClient(TestServer(app)) as client:
        response = await client.post("/v1/runs", json={"input": "Set my preferences"})
        assert response.status == 202
        run_id = (await response.json())["run_id"]
        url = f"/v1/runs/{run_id}"
        task = adapter._active_run_tasks[run_id]
        try:
            async with client.get(f"{url}/events") as stream:
                assert stream.status == 200
                async with asyncio.timeout(5):
                    async for line in stream.content:
                        if line.startswith(b"data: "):
                            event = json.loads(line[6:])
                            if event["event"] == "clarify.request":
                                break
                    else:
                        pytest.fail("The event stream ended without a question")

            status = await (await client.get(url)).json()
            assert status["status"] == "waiting_for_clarify"
            assert status["clarify"] == event
            assert event["title"] == ""
            assert event["questions"][0]["choices"] == ["Casual", "Formal"]
            reply = await client.post(f"{url}/clarify", json={
                "clarify_id": status["clarify"]["clarify_id"], "answers": {"q0": "Formal"},
            })
            assert reply.status == 200
            await asyncio.wait_for(task, timeout=5)
            status = await (await client.get(url)).json()
            assert status["status"] == "completed"
            assert status["output"] == "Tone: Formal"
            assert "clarify" not in status
        finally:
            if not task.done():
                clarify_gateway.clear_session(run_id)
                await asyncio.wait_for(task, timeout=5)
