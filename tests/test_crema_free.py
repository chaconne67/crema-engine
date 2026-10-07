"""The free wizard's real settings routes, credential writes, profile isolation and PKCE listener."""
import asyncio
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI

import crema_free as free


@pytest.fixture
def api():
    app = FastAPI()
    app.include_router(free.create_router())
    return app


@pytest.fixture
def upstream(monkeypatch):
    calls = []
    real_client = httpx.AsyncClient
    mode = {"status": 200, "tools": True, "price": "0"}

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": model, "supported_parameters": ["tools"],
                "pricing": {"prompt": mode["price"], "completion": "0"}}
                for model in free.PROVIDERS["openrouter"]["models"]]})
        if mode["status"] != 200:
            return httpx.Response(mode["status"], json={"error": {"message": "secret-not-to-be-shown"}})
        body = json.loads(request.content)
        if body["messages"][-1]["role"] == "tool":
            message = {"role": "assistant", "content": "연결 확인 완료"}
        elif mode["tools"]:
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "t1", "type": "function",
                "function": {"name": "crema_connection_check", "arguments": '{"city":"서울"}'}}]}
        else:
            message = {"role": "assistant", "content": "I did not call a tool"}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    def client(**kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)
    monkeypatch.setattr(free.httpx, "AsyncClient", client)
    return calls, mode


def payload(provider="openrouter", **kwargs):
    return {"provider": provider, "model": free.PROVIDERS[provider]["models"][0],
            "key": "fake-key-for-tests", "consent": True, "free_tier_confirmed": True, **kwargs}


@pytest.mark.asyncio
async def test_real_routes_save_only_after_tool_roundtrip_and_preserve_default(api, upstream, tmp_path):
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    config = home / "config.yaml"
    config.write_text("# user setting\nmodel:\n  provider: openai-codex\n  default: existing-model\n", encoding="utf-8")
    before = config.read_bytes()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://local") as client:
        for provider in free.PROVIDERS:
            result = (await client.post("/api/crema/free/verify", json=payload(provider))).json()
            assert result["ok"], result
        rows = (await client.get("/api/crema/free/status")).json()["providers"]
    assert all(row["verified"] for row in rows)
    assert config.read_bytes() == before
    assert "fake-key-for-tests" not in json.dumps(rows)
    requests = [json.loads(r.content) for r in upstream[0] if r.method == "POST"]
    assert len(requests) == 4
    assert requests[1]["messages"][-1]["tool_call_id"] == "t1"
    assert requests[0]["provider"]["max_price"] == {"prompt": 0, "completion": 0}
    assert len(requests[0]["messages"][0]["content"]) > 50000


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error", [(401,"key"),(403,"access"),(429,"quota"),(503,"busy"),(410,"model")])
async def test_failure_does_not_save_or_expose_provider_error(upstream, status, error):
    upstream[1]["status"] = status
    result = await free.FreeWizard().verify(payload())
    assert result == {"ok": False, "error": error}
    assert not free.saved_key("openrouter")
    assert not free.read_receipts()


@pytest.mark.asyncio
async def test_paid_model_bad_tool_and_no_consent_are_refused(upstream):
    wizard = free.FreeWizard()
    for body in [payload(consent=False), payload("gemini", free_tier_confirmed=False),
                 payload(model="paid-model"), {**payload(), "provider": "groq"}]:
        assert not (await wizard.verify(body))["ok"]
    assert not upstream[0]
    upstream[1]["price"] = "0.1"
    assert (await wizard.verify(payload()))["error"] == "billing"
    upstream[1].update(price="0", tools=False)
    assert (await wizard.verify(payload()))["error"] == "tools"
    assert not free.saved_key("openrouter")


@pytest.mark.asyncio
async def test_existing_key_is_not_replaced_without_consent_and_rotation_invalidates_receipt(upstream):
    from hermes_cli.credential_lifecycle import save_provider_env_credential
    wizard = free.FreeWizard()
    assert (await wizard.verify(payload()))["ok"]
    before = len(upstream[0])
    assert (await wizard.verify(payload(key="different-key")))["error"] == "replace"
    assert len(upstream[0]) == before
    save_provider_env_credential("OPENROUTER_API_KEY", "different-key")
    assert not free.connection_status()["providers"][0]["verified"]


@pytest.mark.asyncio
async def test_gemini_uses_runtime_key_order_and_invalidates_multi_key_rotation(upstream):
    from hermes_cli.credential_lifecycle import save_provider_env_credential
    from hermes_cli.runtime_provider import resolve_runtime_provider
    assert (await free.FreeWizard().verify(payload("gemini")))["ok"]
    runtime = resolve_runtime_provider(requested="gemini", target_model=payload("gemini")["model"])
    assert runtime["api_key"] == free.saved_key("gemini") == "fake-key-for-tests"
    assert free.connection_status()["providers"][1]["verified"]
    # An alternate key may belong to a billed project. The real pool must not rotate to it.
    save_provider_env_credential("GEMINI_API_KEY", "different-project-key")
    assert free.saved_key("gemini") == "fake-key-for-tests"  # GOOGLE_API_KEY wins
    assert not free.connection_status()["providers"][1]["verified"]
    assert (await free.FreeWizard().verify(payload("gemini")))["error"] == "runtime"


@pytest.mark.asyncio
async def test_receipts_are_profile_scoped_a_b_a(upstream, tmp_path):
    from gateway.run import _profile_runtime_scope
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir(); b.mkdir()
    wizard = free.FreeWizard()
    with _profile_runtime_scope(a):
        assert (await wizard.verify(payload()))["ok"]
        assert free.connection_status()["providers"][0]["verified"]
    with _profile_runtime_scope(b):
        assert not any(row["verified"] for row in free.connection_status()["providers"])
        assert not free.read_receipts()
    with _profile_runtime_scope(a):
        assert free.connection_status()["providers"][0]["verified"]


@pytest.mark.asyncio
async def test_pkce_callback_wrong_path_cancel_and_no_secret_in_poll(monkeypatch, api):
    from hermes_cli import auth_openrouter
    real_client = httpx.AsyncClient
    exchange = []
    def swap(code, verifier):
        exchange.append((code, verifier))
        return "oauth-key-test"
    monkeypatch.setattr(auth_openrouter, "_openrouter_exchange_code", swap)
    async with real_client(transport=httpx.ASGITransport(app=api), base_url="http://local") as client:
        start = (await client.post("/api/crema/free/openrouter/start")).json()
        params = parse_qs(urlparse(start["auth_url"]).query)
        assert params["code_challenge_method"] == ["S256"]
        callback = params["callback_url"][0]
        async with real_client() as network:
            wrong = await network.get(callback + "-wrong?code=test-code")
            assert wrong.status_code == 404
            pending = (await client.get("/api/crema/free/openrouter/poll/" + start["session_id"])).json()
            assert pending == {"status": "pending"}
            assert (await network.get(callback + "?code=test-code")).status_code == 200
        for _ in range(30):
            result = (await client.get("/api/crema/free/openrouter/poll/" + start["session_id"])).json()
            if result["status"] == "approved":
                break
            await asyncio.sleep(0.05)
        assert result == {"status": "approved"}
        assert exchange[0][0] == "test-code"
        assert not free.saved_key("openrouter")
        await client.delete("/api/crema/free/openrouter/" + start["session_id"])
        assert (await client.get("/api/crema/free/openrouter/poll/" + start["session_id"])).json() == {"status":"expired"}
        assert not free.saved_key("openrouter")
