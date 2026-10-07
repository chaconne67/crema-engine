"""Opt-in real-provider checks through the same local API as the desktop wizard.

Run only with an explicitly supplied test credential. The Gemini credential MUST
belong to a no-billing Free Tier project; API keys do not reveal their billing tier.
"""
import os

import httpx
import pytest
from fastapi import FastAPI

from crema_free import PROVIDERS, create_router

LIVE_KEYS = {
    "openrouter": os.environ.get("CREMA_TEST_OPENROUTER_KEY", ""),
    "gemini": os.environ.get("CREMA_TEST_GEMINI_FREE_KEY", ""),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", list(PROVIDERS))
async def test_live_wizard(provider):
    key = LIVE_KEYS[provider]
    if not key:
        pytest.skip("No explicit free-tier test key supplied")
    app = FastAPI()
    app.include_router(create_router())
    attempts = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        for model in PROVIDERS[provider]["models"]:
            result = (await client.post("/api/crema/free/verify", json={
                "provider": provider, "model": model, "key": key,
                "consent": True, "free_tier_confirmed": provider == "gemini",
            })).json()
            attempts.append({"model": model, "ok": result.get("ok"), "error": result.get("error")})
            if result.get("ok"):
                state = (await client.get("/api/crema/free/status")).json()
                assert any(r["id"] == provider and r["verified"] and r["model"] == model for r in state["providers"])
                assert key not in str(state)
                print(f"{provider}: real tool round trip + saved credential readback passed ({result['elapsed_ms']} ms)")
                return
    pytest.fail(str(attempts))
