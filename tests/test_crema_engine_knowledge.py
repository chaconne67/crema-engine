"""Crema launcher: distilling a quiet chat into the knowledge notebook, and the daily pass."""
import json
import time

import pytest

import crema_engine
from agent.knowledge_store import KnowledgeStore


class FakeDB:
    def __init__(self, chats):
        self.chats = chats  # id -> (messages, last_active)

    def get_messages_as_conversation(self, session_id, **_):
        return list(self.chats[session_id][0])

    def list_sessions_rich(self, **_):
        rows = [{"id": sid, "last_active": last, "message_count": len(msgs)} for sid, (msgs, last) in self.chats.items()]
        return sorted(rows, key=lambda r: r["last_active"], reverse=True)


class FakeMemorySessions:
    def __init__(self):
        self.checked_in = []

    def checkin(self, agent):
        self.checked_in.append(agent)


class FakeApi:
    def __init__(self, chats):
        self.db = FakeDB(chats)
        self._memory_sessions = FakeMemorySessions()
        self.created = []

    def _ensure_session_db(self):
        return self.db

    def _create_agent(self, **kwargs):
        self.created.append(kwargs)
        return object()


def turns(n, tool=False):
    msgs = []
    for i in range(n):
        msgs += [{"role": "user", "content": f"질문 {i}"}, {"role": "assistant", "content": f"답 {i}"}]
    if tool:
        msgs.append({"role": "tool", "content": "결과"})
    return msgs


@pytest.fixture()
def store(tmp_path, monkeypatch):
    KnowledgeStore._shared.clear()
    s = KnowledgeStore.open(tmp_path / "knowledge.db")
    monkeypatch.setattr(KnowledgeStore, "open", classmethod(lambda cls, path=None: s))
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {})
    yield s
    KnowledgeStore._shared.clear()


@pytest.fixture()
def reviews(monkeypatch, store):
    """The engine's review, faked: records its call and writes the page a real review would."""
    calls = []

    def spawn(agent, messages, review_memory=False, review_skills=False, focus=None, task_cfg=None, explicit=False):
        calls.append({"messages": messages, "focus": focus, "extra": task_cfg.get("extra_tools"), "memory": review_memory,
                      "skills": review_skills})

        def target():
            store.write("incident/세금-오류", title="세금계산서 오류", body="사업자번호 누락", sources=["chat:x"])
        return target, "prompt"
    monkeypatch.setattr("agent.background_review.spawn_background_review_thread", spawn)
    return calls


def test_a_quiet_chat_with_enough_new_turns_is_distilled_once(store, reviews):
    api = FakeApi({"agent-client-a": (turns(3), time.time())})
    out = crema_engine.distill(api, "agent-client-a", model="m", provider="p")
    assert out["ran"] and out["written"][0]["slug"] == "incident/세금-오류"
    assert api.created[0]["requested_model"] == "m" and api._memory_sessions.checked_in
    assert set(reviews[0]["extra"]) >= {"knowledge_search", "knowledge_write"} and reviews[0]["memory"] and reviews[0]["skills"]
    assert "language the user writes in" in reviews[0]["focus"]
    # Nothing new since: not again.
    assert crema_engine.distill(api, "agent-client-a")["ran"] is False


def test_a_review_tells_what_it_changed_in_memory_and_skills_and_each_can_be_undone(store, monkeypatch):
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    (home / "memories").mkdir(parents=True, exist_ok=True)
    (home / "memories" / "USER.md").write_text("이름: 주인님", encoding="utf-8")
    old_skill = home / "skills" / "release-crema" / "SKILL.md"
    old_skill.parent.mkdir(parents=True, exist_ok=True)
    old_skill.write_text("릴리스 절차 v1", encoding="utf-8")

    def spawn(agent, messages, **_):
        def target():  # what a real review would do through its memory and skill tools
            (home / "memories" / "USER.md").write_text("이름: 주인님\n선호: 결론 먼저", encoding="utf-8")
            old_skill.write_text("릴리스 절차 v2", encoding="utf-8")
            new_skill = home / "skills" / "gbrain-record" / "SKILL.md"
            new_skill.parent.mkdir(parents=True, exist_ok=True)
            new_skill.write_text("기록 전 본문 읽기", encoding="utf-8")
        return target, "prompt"
    monkeypatch.setattr("agent.background_review.spawn_background_review_thread", spawn)
    out = crema_engine.distill(FakeApi({"agent-client-a": (turns(3), time.time())}), "agent-client-a")
    assert [w["title"] for w in out["written"]] == ["나에 대한 기억"]
    assert sorted(w["title"] for w in out["learned"]) == ["gbrain-record", "release-crema"]

    for change in out["written"] + out["learned"]:
        assert crema_engine.undo_review_change(change["slug"])["result"] == "reverted"
    assert (home / "memories" / "USER.md").read_text(encoding="utf-8") == "이름: 주인님"
    assert old_skill.read_text(encoding="utf-8") == "릴리스 절차 v1"
    assert not (home / "skills" / "gbrain-record").exists()
    # Undone once: the same slug is no longer known.
    with pytest.raises(ValueError):
        crema_engine.undo_review_change(out["written"][0]["slug"])


def test_distilling_does_not_bring_back_a_page_the_user_deleted(store, monkeypatch):
    import tools.knowledge_tool as kt
    store.write("incident/세금-오류", title="세금계산서 오류", body="사업자번호 누락", sources=["chat:agent-client-a"])
    store.delete("incident/세금-오류")  # Settings → 기억
    replies = []

    def spawn(agent, messages, focus=None, task_cfg=None, **_):
        def target():  # the review agent writing the page again through its tool
            replies.append(json.loads(kt.knowledge_write(
                {"action": "write", "slug": "incident/세금-오류", "title": "세금계산서 오류",
                 "body": "사업자번호 누락. 거래처 정보 수정으로 해결", "sources": []}, session_id="agent-client-a")))
        return target, "prompt"
    monkeypatch.setattr("agent.background_review.spawn_background_review_thread", spawn)
    out = crema_engine.distill(FakeApi({"agent-client-a": (turns(3), time.time())}), "agent-client-a")
    assert out["ran"] and out["written"] == []
    assert "deleted by the user" in replies[0]["error"]
    assert store.list() == [] and store.search(["세금계산서"]) == []


def test_short_chats_other_sessions_and_memory_off_are_left_alone(store, reviews, monkeypatch):
    api = FakeApi({"agent-client-short": (turns(1), 0), "cron_x": (turns(5), 0), "agent-client-tool": (turns(1, tool=True), 0)})
    assert crema_engine.distill(api, "agent-client-short")["ran"] is False
    assert crema_engine.distill(api, "cron_x")["ran"] is False
    assert crema_engine.distill(api, "agent-client-tool")["ran"] is True  # a tool call counts
    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda: {"memory": {"memory_enabled": False, "user_profile_enabled": False}})
    api.db.chats["agent-client-short"] = (turns(5), 0)
    assert crema_engine.distill(api, "agent-client-short")["ran"] is False


def test_daily_pass_waits_for_quiet_then_runs_once_a_day(store, reviews):
    now = time.time()
    api = FakeApi({"agent-client-a": (turns(4), now - 60)})
    assert crema_engine.daily_once(api, now) == {}  # someone is working
    api.db.chats["agent-client-a"] = (turns(4), now - 3600)
    report = crema_engine.daily_once(api, now)
    assert report["distilled"] == 1 and "purged" in report
    assert crema_engine.daily_once(api, now + 3600) == {}  # done today
    assert store.maintenance_value("last_daily")["distilled"] == 1


def test_daily_pass_off_only_empties_old_deletes(store, reviews, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"knowledge": {"nightly": False}})
    store.write("reference/a", title="A", body="a", sources=["t"])
    store.conn.execute("UPDATE pages SET confirmed_at = ?", (time.time() - 100 * 86400,))
    api = FakeApi({"agent-client-a": (turns(4), time.time() - 3600)})
    report = crema_engine.daily_once(api)
    assert report["needs_review"] == 0 and report["distilled"] == 0 and not reviews
    assert store.get("reference/a")["status"] == "active"


def test_daily_pass_skips_ai_steps_in_light_mode(store, reviews, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"knowledge": {"search_mode": "light"}})
    api = FakeApi({"agent-client-a": (turns(4), time.time() - 3600)})
    report = crema_engine.daily_once(api)
    assert report["distilled"] == 0 and not reviews


def test_free_plan_distills_nothing(store, reviews, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"crema": {"free": True}})
    assert crema_engine.distill(object(), "agent-client-free", "m", "p") == {"ran": False, "written": [], "skipped": "free"}
    assert reviews == []

