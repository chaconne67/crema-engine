"""Crema's small, opt-in free connection wizard. No account creation or paid fallback.

Credentials stay in the engine; only a successful tool round trip is persisted. The
saved receipt is tied to the credential so replacing a key requires a new check.
"""
import asyncio
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter

PROVIDERS = {
    "openrouter": {
        "name": "OpenRouter", "env": "OPENROUTER_API_KEY",
        "base": "https://openrouter.ai/api/v1",
        "models": ["nvidia/nemotron-3-ultra-550b-a55b:free",
                   "nvidia/nemotron-3-super-120b-a12b:free",
                   "dots-studio/dots-3-note-preview:free"],
    },
    "gemini": {
        "name": "Google Gemini", "env": "GEMINI_API_KEY",
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": ["gemini-3.5-flash-lite"],
    },
}


def saved_key(provider):
    from hermes_cli.config import get_env_value
    key = get_env_value(PROVIDERS[provider]["env"]) or ""
    if provider == "gemini" and not key:
        key = get_env_value("GOOGLE_API_KEY") or ""
    return key


def receipts_path():
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "crema-free-connections.json"


def read_receipts():
    try:
        value = json.loads(receipts_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def fingerprint(key):
    return hashlib.sha256(key.encode()).hexdigest()


def connection_status():
    receipts = read_receipts()
    rows = []
    for provider, spec in PROVIDERS.items():
        key = saved_key(provider)
        record = receipts.get(provider, {})
        valid = bool(key and record.get("fingerprint") == fingerprint(key)
                     and record.get("model") in spec["models"])
        rows.append({"id": provider, "name": spec["name"], "models": spec["models"],
                     "has_key": bool(key), "verified": valid,
                     "model": record.get("model", "") if valid else "",
                     "checked_at": record.get("checked_at") if valid else None})
    return {"providers": rows}


def failure(code):
    return {"ok": False, "error": code}


def http_failure(status):
    return {401: "key", 402: "billing", 403: "access", 404: "model", 410: "model",
            413: "context", 429: "quota", 500: "busy", 502: "busy", 503: "busy",
            504: "timeout"}.get(status, "network")


# Synthetic instructions only: never send a real chat, file or user's system prompt.
PROBE_SYSTEM = ("Follow the user's language. Use the provided tool for facts. Do not invent results. "
                "After a tool result, answer briefly in Korean.\n") * 600
PROBE_TOOL = {"type": "function", "function": {"name": "crema_connection_check",
    "description": "Return the connection check result.",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                   "required": ["city"], "additionalProperties": False}}}


async def probe(provider, model, key):
    """Two bounded inference calls, including a real tool-result continuation; no retries."""
    spec = PROVIDERS[provider]
    started = time.monotonic()
    headers = {"Authorization": "Bearer " + key}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            if provider == "openrouter":
                catalog = await client.get(spec["base"] + "/models", headers=headers)
                if catalog.status_code != 200:
                    return failure(http_failure(catalog.status_code))
                listed = next((m for m in catalog.json().get("data", []) if m.get("id") == model), None)
                if not listed or "tools" not in listed.get("supported_parameters", []):
                    return failure("model")
                pricing = listed.get("pricing", {})
                if any(float(pricing.get(k, -1)) != 0 for k in ("prompt", "completion")):
                    return failure("billing")
            messages = [{"role": "system", "content": PROBE_SYSTEM},
                        {"role": "user", "content": "서울로 crema_connection_check 도구를 호출하세요."}]
            body = {"model": model, "messages": messages, "tools": [PROBE_TOOL],
                    "max_tokens": 1024, "stream": False}
            if provider == "openrouter":
                body["provider"] = {"max_price": {"prompt": 0, "completion": 0}, "require_parameters": True}
            response = await client.post(spec["base"] + "/chat/completions", headers=headers, json=body)
            if response.status_code != 200:
                return failure(http_failure(response.status_code))
            data = response.json()
            if data.get("error"):
                return failure(http_failure(data["error"].get("code")))
            message = data["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            if len(calls) != 1 or calls[0].get("function", {}).get("name") != "crema_connection_check":
                return failure("tools")
            call = calls[0]
            args = json.loads(call["function"]["arguments"])
            if args.get("city", "").lower() not in ("서울", "seoul"):
                return failure("tools")
            # Preserve provider-specific reasoning fields when continuing a tool call.
            messages += [message, {"role": "tool", "tool_call_id": call["id"],
                                   "content": "연결 정상. 사용자에게 '연결 확인 완료'라고 답하세요."}]
            response = await client.post(spec["base"] + "/chat/completions", headers=headers, json=body)
            if response.status_code != 200:
                return failure(http_failure(response.status_code))
            data = response.json()
            if data.get("error"):
                return failure(http_failure(data["error"].get("code")))
            final = data["choices"][0]["message"]
            if final.get("tool_calls") or "연결" not in (final.get("content") or ""):
                return failure("reply")
        return {"ok": True, "model": model, "elapsed_ms": round((time.monotonic() - started) * 1000)}
    except httpx.TimeoutException:
        return failure("timeout")
    except httpx.HTTPError:
        return failure("network")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return failure("reply")


class FreeWizard:
    """One wizard per Crema engine instance, with short-lived, cancellable PKCE state."""
    def __init__(self):
        self.login = None
        self.lock = asyncio.Lock()

    def cancel(self):
        if self.login:
            self.login["result"]["error"] = "cancelled"
            self.login["key"] = ""
            self.login = None

    async def start(self):
        from hermes_cli.auth_constants import OPENROUTER_AUTH_URL, _openrouter_err
        from hermes_cli.auth_device_flow import (
            _pkce_code_verifier, _pkce_code_challenge, _make_loopback_callback_handler,
            _bind_loopback_callback_server, _serve_loopback_callback)
        from hermes_cli.auth_openrouter import _openrouter_exchange_code

        self.cancel()
        nonce = secrets.token_urlsafe(24)
        verifier = _pkce_code_verifier()
        path = "/callback/" + nonce
        handler, result = _make_loopback_callback_handler(path, display_name="Crema / OpenRouter")
        server = _bind_loopback_callback_server("127.0.0.1", 0, handler,
            err=_openrouter_err, bind_failed_code="bind")
        state = {"id": nonce, "result": result, "status": "pending", "key": ""}
        self.login = state

        async def receive():
            try:
                callback = await asyncio.to_thread(_serve_loopback_callback, server, result,
                    timeout_seconds=300, err=_openrouter_err, timeout_code="expired")
                if self.login is not state:
                    return
                if callback.get("error"):
                    state["status"] = "denied"
                    return
                key = await asyncio.to_thread(_openrouter_exchange_code, callback["code"], verifier)
                if self.login is state:
                    state.update(key=key, status="approved")
            except Exception:
                # Never expose an exchange exception: it can contain credentials/response bodies.
                if self.login is state:
                    state["status"] = "expired"

        state["task"] = asyncio.create_task(receive())
        asyncio.get_running_loop().call_later(300, lambda: self.cancel() if self.login is state else None)
        return {"session_id": nonce, "auth_url": OPENROUTER_AUTH_URL + "?" + urlencode({
            "callback_url": f"http://127.0.0.1:{server.server_address[1]}{path}",
            "code_challenge": _pkce_code_challenge(verifier), "code_challenge_method": "S256",
            "key_label": "Crema"})}

    async def verify(self, body):
        provider, model = body.get("provider"), body.get("model")
        if provider not in PROVIDERS or model not in PROVIDERS[provider]["models"]:
            return failure("model")
        if body.get("consent") is not True:
            return failure("consent")
        if provider == "gemini" and body.get("free_tier_confirmed") is not True:
            return failure("billing")
        if self.lock.locked():
            return failure("checking")
        async with self.lock:
            old = saved_key(provider)
            login = self.login
            key = str(body.get("key") or "").strip()
            if body.get("session_id"):
                if (provider != "openrouter" or not login or login["id"] != body["session_id"]
                        or login["status"] != "approved"):
                    return failure("login")
                key = login["key"]
            key = key or old
            if not key:
                return failure("key")
            if old and old != key and body.get("replace") is not True:
                return failure("replace")
            result = await probe(provider, model, key)
            if not result["ok"]:
                return result
            # Closing/cancelling an OAuth flow wins over an in-flight check.
            if body.get("session_id") and self.login is not login:
                return failure("login")
            if saved_key(provider) != old:
                return failure("changed")
            from hermes_cli.credential_lifecycle import save_provider_env_credential
            from utils import atomic_json_write
            try:
                if key != old:
                    await asyncio.to_thread(save_provider_env_credential, PROVIDERS[provider]["env"], key)
                if saved_key(provider) != key:
                    return failure("save")
                records = read_receipts()
                records[provider] = {"fingerprint": fingerprint(key), "model": model,
                                     "checked_at": time.time()}
                atomic_json_write(receipts_path(), records)
                if read_receipts().get(provider) != records[provider]:
                    return failure("save")
            except Exception:
                return failure("save")
            if body.get("session_id"):
                self.cancel()
            return {**result, "provider": provider, "checked_at": records[provider]["checked_at"]}


def create_router():
    wizard = FreeWizard()
    router = APIRouter(prefix="/api/crema/free")

    @router.get("/status")
    async def status():
        return connection_status()

    @router.post("/openrouter/start")
    async def start():
        try:
            return await wizard.start()
        except Exception:
            return failure("login")

    @router.get("/openrouter/poll/{session_id}")
    async def poll(session_id: str):
        state = wizard.login
        return {"status": state["status"] if state and state["id"] == session_id else "expired"}

    @router.delete("/openrouter/{session_id}")
    async def cancel(session_id: str):
        if wizard.login and wizard.login["id"] == session_id:
            wizard.cancel()
        return {"ok": True}

    @router.post("/verify")
    async def verify(body: dict):
        return await wizard.verify(body)

    return router
