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
import re
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
    # Chats that change the same file take turns (agent/crema_file_turns.py).
    from agent import crema_file_turns

    crema_file_turns.install(api)
    from hermes_cli.plugins import get_plugin_manager

    get_plugin_manager()._hooks.setdefault("transform_llm_output", []).append(drop_next_input)

    import uvicorn
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
    from hermes_cli.web_routers import audio, config_env, models, oauth, sessions, status, tools

    from crema_free import create_router as free_connection_router

    settings = FastAPI()
    settings.include_router(free_connection_router())
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
        slug = str(body.get("slug") or "")
        # A memory file or skill a quiet chat's review changed ("undo:<token>"), else a notebook page.
        undo = undo_review_change if slug.startswith("undo:") else KnowledgeStore.open().undo
        return await asyncio.to_thread(knowledge_call, undo, slug)

    @settings.post("/api/crema/distill")
    async def distill_chat(body: dict):
        """A chat went quiet (Crema says when): keep what it taught, if anything."""
        return await asyncio.to_thread(
            distill, api, str(body.get("session_id") or ""), str(body.get("model") or ""), str(body.get("provider") or ""))

    # Whose turn it is with a shared file: what the app shows and starts again (agent/crema_file_turns.py).
    @settings.get("/api/crema/turns")
    async def turns():
        await asyncio.to_thread(crema_file_turns.settle)
        return await asyncio.to_thread(crema_file_turns.state)

    @settings.post("/api/crema/turns/woken")
    async def turns_woken(body: dict):
        await asyncio.to_thread(crema_file_turns.woken, str(body.get("session_id") or ""))
        return {"ok": True}

    @settings.post("/api/crema/turns/order")
    async def turns_order(body: dict):
        try:
            await asyncio.to_thread(crema_file_turns.order, str(body.get("first") or ""), str(body.get("second") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True}

    @settings.post("/api/crema/turns/release")
    async def turns_release(body: dict):
        await asyncio.to_thread(crema_file_turns.release, str(body.get("session_id") or ""))
        return {"ok": True}

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


# Crema asks a reply to end with the user's likely next input on its own line (the app's desktop.js
# NEXT_INPUT_INSTRUCTION). The app takes it from the stream; the stored chat leaves it out so later
# turns do not read old guesses.
NEXT_INPUT_LINE = re.compile(r"(?:\A|\n)[ \t>*_`]*NEXT_INPUT:[^\n]*\s*\Z")


def drop_next_input(response_text: str = "", **_) -> str | None:
    """The `transform_llm_output` hook: the reply without its last NEXT_INPUT line, or None to keep it."""
    text = NEXT_INPUT_LINE.sub("", response_text or "").rstrip()
    return text if text and text != (response_text or "").rstrip() else None


KNOWLEDGE_TOOLS = ("knowledge_search", "knowledge_get", "knowledge_write")
# Crema's chats are agent-client-<chat id> (the app's desktop.js); only those are distilled.
CHAT_PREFIX = "agent-client-"
DISTILL_MIN_USER_TURNS = 3
DISTILL_FOCUS = (
    "This conversation has gone quiet. Keep only what a later conversation would reuse, in the "
    "language the user writes in. For the knowledge notebook: search it first (knowledge_search), "
    "then create or update one page per topic with knowledge_write — a problem with its cause and fix "
    "(incident/), a correction the user made (feedback/), a decision and why (decision/), how part of "
    "their work is done (project/), something looked up (reference/), a document or material made for or "
    "named by the user with its location (file/); link related pages as [[slug]]; "
    "add a dated line (action=timeline) when an existing topic had a new event. Facts about the user "
    "go to the memory tool. Nothing reusable: say 'Nothing to save.' and stop."
)


# What a quiet chat's review may change besides the notebook (docs Crema-자동작업-알림-원칙): the memory files
# and the skills. Their texts are read before it runs, so Crema can say what changed and undo it.
MEMORY_TITLES = {"USER.md": "나에 대한 기억", "MEMORY.md": "에이전트 메모"}
UNDO_DIR = "crema-undo"


def _review_texts() -> dict:
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    paths = [home / "memories" / name for name in MEMORY_TITLES]
    skills = home / "skills"
    if skills.is_dir():
        paths += sorted(skills.rglob("SKILL.md"))
    return {str(path): path.read_text(encoding="utf-8") for path in paths if path.is_file()}


def _review_changes(before: dict) -> tuple:
    """What the review changed since `before`: memory files as written entries, skills as learned ones, each with
    an undo slug ("undo:<token>") whose earlier text (None: the file is new) is kept under crema-undo/."""
    import uuid

    from hermes_constants import get_hermes_home

    after = _review_texts()
    undo_dir = get_hermes_home() / UNDO_DIR
    written, learned = [], []
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path):
            continue
        token = uuid.uuid4().hex
        undo_dir.mkdir(parents=True, exist_ok=True)
        (undo_dir / f"{token}.json").write_text(json.dumps({"path": path, "text": before.get(path)}, ensure_ascii=False), encoding="utf-8")
        name = os.path.basename(path)
        if name in MEMORY_TITLES:
            written.append({"slug": f"undo:{token}", "title": MEMORY_TITLES[name]})
        else:
            learned.append({"slug": f"undo:{token}", "title": os.path.basename(os.path.dirname(path))})
    return written, learned


def undo_review_change(slug: str) -> dict:
    """Puts back a memory file or skill as it was before a quiet chat's review: a new skill is removed."""
    import re
    import shutil

    from hermes_constants import get_hermes_home

    token = slug.removeprefix("undo:")
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError(f"no change {slug}")
    record = get_hermes_home() / UNDO_DIR / f"{token}.json"
    if not record.is_file():
        raise ValueError(f"no change {slug}")
    kept = json.loads(record.read_text(encoding="utf-8"))
    path = kept["path"]
    if kept["text"] is not None:
        with open(path, "w", encoding="utf-8") as out:
            out.write(kept["text"])
    elif os.path.basename(path) == "SKILL.md":
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
    elif os.path.exists(path):
        os.remove(path)
    record.unlink()
    if os.path.basename(path) == "SKILL.md":
        from agent.prompt_builder import clear_skills_system_prompt_cache

        clear_skills_system_prompt_cache(clear_snapshot=True)
    return {"slug": slug, "result": "reverted"}


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

    from tools.knowledge_tool import plan_free

    if not session_id.startswith(CHAT_PREFIX) or not _remembering(load_config_readonly() or {}):
        return {"ran": False, "written": []}
    if plan_free():
        return {"ran": False, "written": [], "skipped": "free"}
    store = KnowledgeStore.open()
    messages = api._ensure_session_db().get_messages_as_conversation(
        session_id, include_ancestors=True, repair_alternation=True)
    done = store.distilled_count(session_id)
    fresh = messages[done:] if done <= len(messages) else messages
    if not fresh or (sum(1 for m in fresh if m.get("role") == "user") < DISTILL_MIN_USER_TURNS
                     and not any(m.get("role") == "tool" for m in fresh)):
        return {"ran": False, "written": []}
    started = time.time()
    before = _review_texts()
    agent = api._create_agent(session_id=session_id, requested_model=model or None, requested_provider=provider or None)
    task_cfg = dict(_background_review_task_config())
    task_cfg["extra_tools"] = list(task_cfg.get("extra_tools") or []) + list(KNOWLEDGE_TOOLS)
    # Skills are reviewed here too (skills.creation_nudge_interval is 0 in Crema): one place for what Crema learns.
    target, _ = spawn_background_review_thread(
        agent, messages, review_memory=True, review_skills=True, focus=DISTILL_FOCUS, task_cfg=task_cfg, explicit=True)
    try:
        reviewed = target()
    finally:
        api._memory_sessions.checkin(agent)
    # A review that failed or did not run leaves these messages for the next try (audit ER-4); what it
    # wrote before failing is still reported.
    if reviewed is not False:
        store.mark_distilled(session_id, len(messages))
    memory, learned = _review_changes(before)
    return {"ran": reviewed is not False, "written": store.changed_since(started) + memory, "learned": learned}


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
