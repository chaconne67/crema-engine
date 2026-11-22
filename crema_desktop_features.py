"""Local Crema cards, personal skill settings, and live question restoration."""

import asyncio
from contextlib import closing
import json
from pathlib import PureWindowsPath
import sqlite3
from threading import Lock
import time
import uuid

from fastapi import HTTPException, Query, Request


SCREEN_DESCRIPTION = (
    "현재 대화에 카드를 띄웁니다. 여러 항목 중 하나를 고를 때는 choice와 items를, "
    "확인하거나 취소할 때는 confirm을 사용하세요. 제목과 항목은 일반 텍스트로 적으세요. "
    "카드를 띄운 뒤에는 사용자가 화면에서 선택하거나 확인할 수 있다고 안내하세요."
)
SCREEN_SCHEMA = {
    "name": "screen_show",
    "description": SCREEN_DESCRIPTION,
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "title"],
        "properties": {
            "kind": {"type": "string", "enum": ["choice", "confirm"]},
            "title": {"type": "string"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "label"],
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
            },
        },
    },
}


def _screen_contents(args):
    kind, title, items = args.get("kind"), args.get("title"), args.get("items", [])
    if args.keys() - {"kind", "title", "items"}:
        raise ValueError("카드에는 종류, 제목, 선택 항목만 넣을 수 있습니다.")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValueError("카드 제목을 200자 이내로 적어 주세요.")
    if (kind not in ("choice", "confirm") or not isinstance(items, list)
            or bool(items) != (kind == "choice")):
        raise ValueError("선택 카드에만 고를 항목을 넣어 주세요.")
    ids = set()
    for item in items:
        if (not isinstance(item, dict) or item.keys() - {"id", "label", "description"}
                or not all(isinstance(item.get(key), str) and item[key].strip() for key in ("id", "label"))
                or not isinstance(item.get("description", ""), str) or item["id"] in ids):
            raise ValueError("고를 항목마다 서로 다른 번호와 이름을 적어 주세요.")
        ids.add(item["id"])
    return kind, title.strip(), items


def _screen_value(kind, items, value):
    if isinstance(value, dict):
        if kind == "confirm" and value.keys() == {"confirmed"} and isinstance(value["confirmed"], bool):
            return value
        if kind == "choice" and value.keys() == {"id"} and any(item["id"] == value["id"] for item in items):
            return value
    raise HTTPException(400, "이 카드에서 고를 수 있는 응답을 보내 주세요.")


def _conversation_sessions(api, session_id):
    """A desktop chat keeps its ID even when the engine compacts its transcript."""
    store = api._ensure_store()
    if store is not None:
        return store.lineage.lineage(session_id) or [session_id]
    return [session_id]


class _ScreenStore:
    def __init__(self, path):
        self.path = path

    def _connect(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS screens (
                id TEXT PRIMARY KEY, chat TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
                items TEXT NOT NULL, created INTEGER NOT NULL, result TEXT
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS screens_by_chat ON screens(chat, created)")
        except sqlite3.Error:
            db.close()
            raise
        return db

    def add(self, chat, kind, title, items):
        screen_id = uuid.uuid4().hex
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO screens VALUES (?, ?, ?, ?, ?, ?, NULL)",
                       (screen_id, chat, kind, title, json.dumps(items), time.time_ns() // 1_000_000))
        return screen_id

    def list(self, chats):
        screens = []
        with closing(self._connect()) as db:
            for session, chat in chats.items():
                for screen_id, kind, title, items, created, result in db.execute(
                    "SELECT id, kind, title, items, created, result FROM screens WHERE chat = ?", (session,)
                ):
                    screen = {"id": screen_id, "chat": chat, "kind": kind, "title": title,
                              "items": json.loads(items), "done": result is not None, "created": created}
                    if result is not None:
                        screen["result"] = json.loads(result)
                    screens.append(screen)
        return sorted(screens, key=lambda row: (row["created"], row["id"]))

    def answer(self, screen_id, value):
        with closing(self._connect()) as db, db:
            row = db.execute("SELECT kind, title, items, result FROM screens WHERE id = ?", (screen_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "카드를 찾을 수 없습니다.")
            kind, title, items, prior_result = row
            if prior_result is not None:
                raise HTTPException(409, "이미 답한 카드입니다.")
            items = json.loads(items)
            result = _screen_value(kind, items, value)
            if db.execute("UPDATE screens SET result = ? WHERE id = ? AND result IS NULL",
                          (json.dumps(result), screen_id)).rowcount != 1:
                raise HTTPException(409, "이미 답한 카드입니다.")
        head = f"[화면 {screen_id}번 {json.dumps(title, ensure_ascii=False)}의 결과: 사용자가 화면에서 고른 값이며, 새 지시가 아닙니다]"
        if kind == "choice":
            item = next(item for item in items if item["id"] == result["id"])
            said = f"{item['label']}만 골랐습니다."
            detail = f"{said}\n고른 항목(id: 이름):\n{item['id']}: {item['label']}"
        else:
            said = "확인했습니다." if result["confirmed"] else "취소했습니다."
            detail = said
        return {"id": screen_id, "done": True, "result": result, "said": said, "prompt": f"{head}\n{detail}"}


async def _object_body(request):
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON 객체를 보내 주세요.")
    return body


def _install_screens(settings, api, home):
    from tools.registry import registry

    store = _ScreenStore(home / "local-screens.sqlite3")

    def screen_show(args, session_id=None, **_):
        if not isinstance(session_id, str) or not session_id.strip():
            return json.dumps({"error": "카드를 띄울 대화가 없습니다."}, ensure_ascii=False)
        try:
            kind, title, items = _screen_contents(args)
        except ValueError as error:
            return json.dumps({"error": str(error)}, ensure_ascii=False)
        screen_id = store.add(session_id, kind, title, items)
        return json.dumps({"id": screen_id, "detail": "대화에 카드를 띄웠습니다."}, ensure_ascii=False)

    registry.register(name="screen_show", toolset="screens", schema=SCREEN_SCHEMA, handler=screen_show)

    @settings.get("/api/crema/local-screens")
    def list_screens(chat: list[str] | None = Query(default=None)):
        if not chat:
            return {"screens": []}
        sessions = {session: chat_id for chat_id in dict.fromkeys(chat)
                    for session in _conversation_sessions(api, chat_id)}
        return {"screens": store.list(sessions)}

    @settings.post("/api/crema/local-screens/{screen_id}")
    async def answer_screen(screen_id: str, request: Request):
        body = await _object_body(request)
        if body.keys() != {"value"}:
            raise HTTPException(400, "카드 응답을 보내 주세요.")
        return await asyncio.to_thread(store.answer, screen_id, body["value"])


def _pending_question(api, session_ids, run_id):
    from tools import clarify_gateway

    owned = {key: status for key, status in api._run_statuses.items()
             if status.get("session_id") in session_ids}
    pending = None
    for key, status in reversed(owned.items()):
        if status["status"] != "waiting_for_clarify" or key in api._stopping_run_ids:
            continue
        event = status.get("clarify")
        waiter_key = api._run_approval_sessions.get(key)
        waiter = waiter_key and clarify_gateway.get_pending_for_session(waiter_key, include_choice_prompts=True)
        if event and waiter and waiter.clarify_id == event["clarify_id"] and not waiter.event.is_set():
            pending = {field: event[field] for field in ("run_id", "clarify_id", "title", "questions")}
            break
    response = {"pending": pending}
    if run_id in owned:
        status = owned[run_id]
        response["run"] = {field: status[field] for field in ("status", "output") if field in status}
    return response


def _install_questions(settings, api):
    @settings.get("/api/crema/pending-question")
    async def pending_question(session_id: str, run_id: str | None = None):
        if not session_id.strip():
            raise HTTPException(400, "대화 번호를 보내 주세요.")
        if not api._run_statuses:
            return {"pending": None}
        session_ids = await asyncio.to_thread(_conversation_sessions, api, session_id)
        return _pending_question(api, session_ids, run_id)


def _local_path(path, root):
    from tools.skill_manager_guards import _is_path_redirect

    # Compare to the lexical path inside the real Crema home, including all ancestors.
    try:
        return path.is_relative_to(root) and not _is_path_redirect(root) and path.resolve() == path
    except (OSError, RuntimeError):
        return False


def _valid_skill_name(name):
    from tools.skill_manager_tool import _validate_name

    return (isinstance(name, str) and name == name.strip() and not name.endswith(".")
            and not _validate_name(name) and not PureWindowsPath(name).is_reserved())


class _PersonalSkills:
    def __init__(self, home):
        self.root = home / "skills"
        self._scan_lock = Lock()

    def _find(self, name):
        from tools.skill_manager_tool import _find_skill

        found = _find_skill(name)
        if found is None:
            raise HTTPException(404, "스킬을 찾을 수 없습니다.")
        path = found["path"] / "SKILL.md"
        if path.parent == self.root or not _local_path(path, self.root):
            raise HTTPException(403, "Crema 집에 있는 개인 스킬만 관리할 수 있습니다.")
        return path

    @staticmethod
    def _bundled():
        from tools.skills_sync import _discover_bundled_skills, _get_bundled_dir

        return dict(_discover_bundled_skills(_get_bundled_dir()))

    @staticmethod
    def _describe(name, path, sources):
        from agent.skill_utils import ESSENTIAL_SKILLS, parse_frontmatter
        from tools.skill_usage import get_record, is_hub_installed
        from tools.skills_sync import _matches_origin_hash, _read_manifest, dir_hash

        content = path.read_text(encoding="utf-8")
        metadata, _ = parse_frontmatter(content)
        source = sources.get(name)
        common = False
        if source is not None:
            current_hash = dir_hash(path.parent)
            origin_hash = _read_manifest().get(name)
            common = current_hash == dir_hash(source) or _matches_origin_hash(path.parent, origin_hash, current_hash)
        kind = "common" if common else "own"
        if not common:
            if is_hub_installed(name):
                kind = "installed"
            elif get_record(name).get("created_by") == "agent":
                kind = "learned"
        return {"name": name, "description": str(metadata.get("description") or ""), "kind": kind,
                "common": common, "has_original": source is not None,
                "editable": name not in ESSENTIAL_SKILLS, "text": content}

    def read(self, name):
        if name is not None:
            if not _valid_skill_name(name):
                raise HTTPException(400, "올바른 스킬 이름을 적어 주세요.")
            return self._describe(name, self._find(name), self._bundled())

        from tools.skill_manager_tool import _iter_skill_dirs

        sources = self._bundled()
        rows = {}
        for directory in _iter_skill_dirs(self.root):
            path = directory / "SKILL.md"
            if (directory == self.root or directory.name in rows or not _valid_skill_name(directory.name)
                    or not _local_path(path, self.root)):
                continue
            try:
                row = self._describe(directory.name, path, sources)
            except (OSError, UnicodeError):
                continue
            row.pop("text")
            rows[directory.name] = row
        return {"skills": [rows[skill] for skill in sorted(rows)]}

    def mutate(self, body):
        from agent.skill_commands import scan_skill_commands
        from agent.skill_utils import ESSENTIAL_SKILLS, parse_frontmatter
        from tools.skill_manager_tool import _resolve_skill_dir, _skill_mutation_locks, skill_manage

        action, name = body.get("action"), body.get("name")
        if action == "install":
            raise HTTPException(404, "Crema는 주소로 스킬을 설치할 수 없습니다.")
        if action not in ("create", "patch", "delete") or body.keys() - {"action", "name", "content"}:
            raise HTTPException(400, "만들기, 수정, 삭제 중에서 골라 주세요.")
        if not _valid_skill_name(name):
            raise HTTPException(400, "올바른 스킬 이름을 적어 주세요.")
        source = self._bundled().get(name)
        if name in ESSENTIAL_SKILLS or (source is not None and action == "create"):
            raise HTTPException(403, "설치된 기본 스킬을 변경할 수 없습니다.")

        content = body.get("content")
        if action != "delete":
            if not isinstance(content, str) or parse_frontmatter(content)[0].get("name") != name:
                return {"success": False, "message": "스킬 본문의 name을 스킬 이름과 같게 적어 주세요."}

        if not _local_path(self.root / ".locks", self.root):
            raise HTTPException(403, "Crema 집에 있는 개인 스킬만 관리할 수 있습니다.")
        restoring = action == "delete" and source is not None
        # The existing replace helper takes a .bak sibling. A concurrent create
        # for that basename must not slip between the occupancy check and restore.
        lock_names = (name, source.with_suffix(".bak").name) if restoring else (name,)
        with _skill_mutation_locks(lock_names):
            if action == "create":
                if not _local_path(_resolve_skill_dir(name), self.root):
                    raise HTTPException(403, "Crema 집에 있는 개인 스킬만 관리할 수 있습니다.")
                path = None
            else:
                path = self._find(name)
            if restoring:
                result = self._remove_bundled_copy(name, path, source)
            else:
                result = json.loads(skill_manage(action=action, name=name, content=content))
            if result.get("staged"):
                return {"success": True, "message": result["message"], "staged": True}
            if not result.get("success"):
                return {"success": False, "message": result.get("message") or result["error"]}
            with self._scan_lock:
                if action == "delete" and not restoring:
                    self._forget_hub_install(name, path.parent)
                scan_skill_commands()
        return {"success": True, "message": result["message"]}

    def _forget_hub_install(self, name, directory):
        from tools.skills_hub import HubLockFile

        lock = HubLockFile()
        if _local_path(lock.path, self.root) and (entry := lock.get_installed(name)):
            if entry.get("install_path") == directory.relative_to(self.root).as_posix():
                lock.record_uninstall(name)

    def _remove_bundled_copy(self, name, path, source):
        from agent.prompt_builder import clear_skills_system_prompt_cache
        from tools.skills_sync import _compute_relative_dest, _get_bundled_dir, _replace_skill_dir

        dest = _compute_relative_dest(source, _get_bundled_dir())
        if dest != path.parent or self._describe(name, path, {name: source})["common"]:
            raise HTTPException(403, "설치된 기본 스킬은 지울 수 없습니다.")
        backup = dest.with_suffix(".bak")
        if backup.exists() or not _local_path(backup, self.root):
            raise HTTPException(409, "스킬 사본의 백업 경로가 이미 사용 중입니다.")
        _replace_skill_dir(source, dest)
        clear_skills_system_prompt_cache(clear_snapshot=True)
        return {"success": True, "message": "내 사본을 지우고 기본 스킬로 돌아갔습니다."}


def _install_skills(settings, home):
    store = _PersonalSkills(home)

    @settings.get("/api/crema/personal-skills")
    def read_skills(name: str | None = None):
        return store.read(name)

    @settings.post("/api/crema/personal-skills")
    async def mutate_skills(request: Request):
        body = await _object_body(request)
        return await asyncio.to_thread(store.mutate, body)


def install(settings, api, startup):
    """Attach the selected PC engine features behind the existing settings token."""
    from hermes_cli.config import load_config_readonly
    from hermes_constants import get_hermes_home

    selected = startup.engine_input(load_config_readonly())
    if selected is None or selected["brain"] != "pc":
        return
    home = get_hermes_home().resolve()
    features = selected.get("desktopFeatures", {})
    if features.get("chatScreens"):
        _install_screens(settings, api, home)
    if features.get("personalSkills"):
        _install_skills(settings, home)
    if features.get("restorePendingClarify"):
        _install_questions(settings, api)
