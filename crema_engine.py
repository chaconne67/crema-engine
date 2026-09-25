"""Crema's engine: the Hermes run API and the four settings routers Crema uses, in one process,
without the gateway or the dashboard.

Crema starts it with HERMES_HOME (Crema's own engine folder) and HERMES_DASHBOARD_SESSION_TOKEN
in the environment, reads the one JSON line it prints ({"api": port, "settings": port}), and stops
it when Crema closes. Both listeners are loopback only and take that one token: the run API as
`Authorization: Bearer`, the settings API as `X-Hermes-Session-Token`, as `hermes serve` does (its
routers check the same variable themselves).
"""
import asyncio
import hmac
import json
import os
import socket
import sys


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def main() -> None:
    token = os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN", "")
    if len(token) < 16:
        sys.exit("HERMES_DASHBOARD_SESSION_TOKEN is missing or too short")

    # Only the packages bundled with Crema (the `crema` extra); never install more at run time.
    os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"

    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    api = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": token, "host": "127.0.0.1", "port": free_port()}))
    if not await api.connect():
        sys.exit("the run API did not start")

    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from hermes_cli.web_routers import audio, config_env, models, oauth

    settings = FastAPI()
    for router in (oauth.router, config_env.config_router, config_env.router, models.router, audio.router):
        settings.include_router(router)

    @settings.middleware("http")
    async def only_crema(request: Request, call_next):
        given = request.headers.get("X-Hermes-Session-Token", "")
        if not hmac.compare_digest(given.encode(), token.encode()):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(settings, host="127.0.0.1", port=port, log_level="warning"))
    print(json.dumps({"api": api._port, "settings": port}), flush=True)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
