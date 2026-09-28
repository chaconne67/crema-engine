"""Crema's engine: the Hermes run API and the settings routes Crema uses, in one process,
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
import logging
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
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
    from hermes_cli.web_routers import audio, config_env, models, oauth, sessions, status, tools

    settings = FastAPI()
    for router in (oauth.router, config_env.config_router, config_env.router, models.router, audio.router):
        settings.include_router(router)
    # Of these routers only what Crema shows: what the agent remembers (memory cards and learned
    # skills, to read, edit and delete), the past-chat search, and the image and video model lists.
    wanted = {"/api/learning/graph", "/api/learning/node", "/api/sessions/search", "/api/tools/toolsets/{name}/models"}
    for router in (status.router, sessions.search_router, tools.router):
        settings.router.routes.extend(route for route in router.routes if route.path in wanted)

    @settings.post("/api/crema/backup")
    async def backup(body: dict):
        """The engine folder (chats, memory, skills, settings) into the .zip Crema chose, without the
        API keys and sign-in tokens, so the copy can sit in any folder."""
        from argparse import Namespace
        from hermes_cli.backup import run_backup

        output = str(body.get("output") or "")
        if not output.lower().endswith(".zip"):
            raise HTTPException(status_code=400, detail="output must be a .zip path")

        def run() -> bool:
            try:
                return run_backup(Namespace(output=output, keep=0, no_secrets=True))
            except SystemExit:
                return False

        return {"ok": await asyncio.to_thread(run), "path": output}

    @settings.middleware("http")
    async def only_crema(request: Request, call_next):
        given = request.headers.get("X-Hermes-Session-Token", "")
        if not hmac.compare_digest(given.encode(), token.encode()):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(settings, host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        if serving.done():
            sys.exit("the settings API did not start")
        await asyncio.sleep(0.05)
    # Both listen now: Crema may call either as soon as it reads this line.
    print(json.dumps({"api": api._port, "settings": port}), flush=True)
    backfill = asyncio.create_task(asyncio.to_thread(index_past_chats))
    await serving
    backfill.cancel()


def index_past_chats() -> None:
    """Index the chats saved before the Korean/CJK two-letter search index existed; new messages are
    indexed as they are saved. Until this finishes, short CJK searches scan the chats instead."""
    from hermes_state import SessionDB

    try:
        db = SessionDB()
        try:
            if db.fts_optimize_available():
                db.optimize_fts_storage(vacuum=False)
        finally:
            db.close()
    except Exception:
        logging.getLogger(__name__).warning("indexing past chats failed", exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
