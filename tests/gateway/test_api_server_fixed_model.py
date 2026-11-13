"""An account that may not choose a model (the front proxy says so with a header) cannot name one in a request."""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.platforms.api_server import FIXED_MODEL_HEADER, fixed_model_middleware

FIXED = {FIXED_MODEL_HEADER: "1"}


async def _client():
    async def echo(request):
        return web.json_response({"read": await request.json()})

    app = web.Application(middlewares=[fixed_model_middleware])
    app.router.add_post("/v1/runs", echo)
    return TestClient(TestServer(app))


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", [
    {"model": "gpt-5.1"}, {"model_id": "gpt-5.1"}, {"provider": "openai"}, {"provider_id": "openai"},
    {"model_options": {"reasoning": {"effort": "high"}}}, {"model_options": {"fast": True}}])
async def test_a_fixed_model_account_cannot_name_a_model(choice):
    async with await _client() as client:
        response = await client.post("/v1/runs", json={"input": "hi", **choice}, headers=FIXED)
        assert response.status == 403
        assert (await response.json())["error"]["code"] == "model_choice_not_allowed"


@pytest.mark.asyncio
async def test_the_refusal_does_not_depend_on_the_content_type():
    async with await _client() as client:
        response = await client.post(
            "/v1/runs", data=b'{"input": "hi", "model": "gpt-5.1"}', headers={**FIXED, "Content-Type": "text/plain"})
        assert response.status == 403


@pytest.mark.asyncio
async def test_a_fixed_model_account_that_names_no_model_is_served_and_the_handler_reads_the_body():
    async with await _client() as client:
        response = await client.post("/v1/runs", json={"input": "hi"}, headers=FIXED)
        assert response.status == 200
        assert await response.json() == {"read": {"input": "hi"}}


@pytest.mark.asyncio
async def test_without_the_header_a_request_names_its_model_as_before():
    async with await _client() as client:
        response = await client.post("/v1/runs", json={"input": "hi", "model": "gpt-5.1", "provider": "openai"})
        assert response.status == 200
        assert (await response.json())["read"]["model"] == "gpt-5.1"
