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
import time


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

    # The knowledge notebook (agent/knowledge_store.py) for Settings → 기억: list, read, correct
    # (a correction is the user's word), delete and undo.
    from agent.knowledge_store import KnowledgeStore

    @settings.get("/api/crema/knowledge")
    async def knowledge_list(type: str = ""):
        store = KnowledgeStore.open()
        pages = await asyncio.to_thread(store.list, type or None)
        return {"pages": pages, "deleted": await asyncio.to_thread(store.list, deleted=True),
                "last_daily": store.maintenance_value("last_daily"),
                "meaning": await asyncio.to_thread(store.vector_status)}

    def knowledge_call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @settings.get("/api/crema/knowledge/page")
    async def knowledge_page(slug: str):
        return await asyncio.to_thread(knowledge_call, KnowledgeStore.open().get, slug)

    @settings.put("/api/crema/knowledge/page")
    async def knowledge_edit(body: dict):
        store = KnowledgeStore.open()
        page = await asyncio.to_thread(knowledge_call, store.get, str(body.get("slug") or ""))
        return await asyncio.to_thread(
            knowledge_call, store.write, page["slug"], title=str(body.get("title") or page["title"]),
            body=str(body.get("body") or ""), sources=["settings"], authority="user_said",
            status=str(body.get("status") or "active"), tags=page["tags"])

    @settings.delete("/api/crema/knowledge/page")
    async def knowledge_delete(body: dict):
        return await asyncio.to_thread(knowledge_call, KnowledgeStore.open().delete, str(body.get("slug") or ""))

    @settings.post("/api/crema/knowledge/undo")
    async def knowledge_undo(body: dict):
        return await asyncio.to_thread(knowledge_call, KnowledgeStore.open().undo, str(body.get("slug") or ""))

    @settings.post("/api/crema/distill")
    async def distill_chat(body: dict):
        """A chat went quiet (Crema says when): keep what it taught, if anything."""
        return await asyncio.to_thread(
            distill, api, str(body.get("session_id") or ""), str(body.get("model") or ""), str(body.get("provider") or ""))

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
    daily = asyncio.create_task(daily_pass(api))
    await serving
    backfill.cancel()
    daily.cancel()


KNOWLEDGE_TOOLS = ("knowledge_search", "knowledge_get", "knowledge_write")
# Crema's chats are agent-client-<chat id> (the app's desktop.js); only those are distilled.
CHAT_PREFIX = "agent-client-"
DISTILL_MIN_USER_TURNS = 3
DISTILL_FOCUS = (
    "This conversation has gone quiet. Keep only what a later conversation would reuse, in the "
    "language the user writes in. For the knowledge notebook: search it first (knowledge_search), "
    "then create or update one page per topic with knowledge_write — a problem with its cause and fix "
    "(incident/), a correction the user made (feedback/), a decision and why (decision/), how part of "
    "their work is done (project/), something looked up (reference/); link related pages as [[slug]]; "
    "add a dated line (action=timeline) when an existing topic had a new event. Facts about the user "
    "go to the memory tool. Nothing reusable: say 'Nothing to save.' and stop."
)


def _remembering(config: dict) -> bool:
    memory = config.get("memory") or {}
    return memory.get("memory_enabled", True) is not False or memory.get("user_profile_enabled", True) is not False


def distill(api, session_id: str, model: str = "", provider: str = "") -> dict:
    """Run the engine's background review on one quiet chat, focused on the knowledge notebook, when
    it has something new: at least a few user turns or a tool call since it was last distilled. The
    review runs on the chat's model (or the configured auxiliary one) — the user's own account."""
    from agent.background_review import _background_review_task_config, spawn_background_review_thread
    from agent.knowledge_store import KnowledgeStore
    from hermes_cli.config import load_config_readonly

    if not session_id.startswith(CHAT_PREFIX) or not _remembering(load_config_readonly() or {}):
        return {"ran": False, "written": []}
    store = KnowledgeStore.open()
    messages = api._ensure_session_db().get_messages_as_conversation(
        session_id, include_ancestors=True, repair_alternation=True)
    done = store.distilled_count(session_id)
    fresh = messages[done:] if done <= len(messages) else messages
    if not fresh or (sum(1 for m in fresh if m.get("role") == "user") < DISTILL_MIN_USER_TURNS
                     and not any(m.get("role") == "tool" for m in fresh)):
        return {"ran": False, "written": []}
    started = time.time()
    agent = api._create_agent(session_id=session_id, requested_model=model or None, requested_provider=provider or None)
    task_cfg = dict(_background_review_task_config())
    task_cfg["extra_tools"] = list(task_cfg.get("extra_tools") or []) + list(KNOWLEDGE_TOOLS)
    target, _ = spawn_background_review_thread(
        agent, messages, review_memory=True, focus=DISTILL_FOCUS, task_cfg=task_cfg, explicit=True)
    try:
        target()
    finally:
        api._memory_sessions.checkin(agent)
    store.mark_distilled(session_id, len(messages))
    return {"ran": True, "written": store.changed_since(started)}


DAILY_EVERY_S = 20 * 3600
DAILY_IDLE_S = 30 * 60
DAILY_MAX_CHATS = 3


async def daily_pass(api) -> None:
    """Once a day while Crema is open and quiet (GBrain's dream cycle): the notebook's rule steps, then
    — unless knowledge.nightly is off or knowledge.search_mode is light — distill up to a few quiet
    chats that were left before Crema said they went quiet (e.g. Crema was closed)."""
    while True:
        await asyncio.sleep(600)
        try:
            await asyncio.to_thread(daily_once, api)
        except Exception:
            logging.getLogger(__name__).warning("the daily knowledge pass failed", exc_info=True)


def daily_once(api, now: float = None) -> dict:
    from agent.knowledge_store import KnowledgeStore
    from hermes_cli.config import load_config_readonly

    now = now or time.time()
    store = KnowledgeStore.open()
    last = store.maintenance_value("last_daily")
    if last and now - last.get("at", 0) < DAILY_EVERY_S:
        return {}
    sessions = api._ensure_session_db().list_sessions_rich(limit=50, order_by_last_active=True)
    chats = [s for s in sessions if str(s.get("id", "")).startswith(CHAT_PREFIX)]
    if chats and now - float(chats[0].get("last_active") or 0) < DAILY_IDLE_S:
        return {}  # someone is working: another time
    knowledge = (load_config_readonly() or {}).get("knowledge") or {}
    tidy = knowledge.get("nightly", True) is not False
    report = store.maintain(review=tidy)
    report["distilled"] = 0
    if tidy and knowledge.get("search_mode", "balanced") != "light":
        for chat in chats:
            if report["distilled"] >= DAILY_MAX_CHATS or now - float(chat.get("last_active") or 0) > 7 * 86400:
                break
            if int(chat.get("message_count") or 0) > store.distilled_count(chat["id"]):
                if distill(api, chat["id"]).get("ran"):
                    report["distilled"] += 1
    report["at"] = now
    store.set_maintenance_value("last_daily", report)
    return report


def index_past_chats() -> None:
    """Index the chats saved before the Korean/CJK two-letter search index existed; new messages are
    indexed as they are saved. Until this finishes, short CJK searches scan the chats instead. Then
    give knowledge pages written before meaning search (or under another model) their vectors; until
    then the notebook finds them by words."""
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
    try:
        from agent.knowledge_store import KnowledgeStore

        KnowledgeStore.open().fill_vectors()
    except Exception:
        logging.getLogger(__name__).warning("knowledge vectors backfill failed", exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
