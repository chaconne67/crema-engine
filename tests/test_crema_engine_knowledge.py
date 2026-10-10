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


def test_a_review_that_fails_leaves_the_chat_to_be_distilled_again(store, monkeypatch):
    """The review worker reports a failure (it catches the error itself); the chat is not marked done (audit ER-4)."""
    monkeypatch.setattr("agent.background_review.spawn_background_review_thread",
                        lambda agent, messages, **_: (lambda: False, "prompt"))
    api = FakeApi({"agent-client-a": (turns(3), time.time())})
    assert crema_engine.distill(api, "agent-client-a")["ran"] is False
    assert store.distilled_count("agent-client-a") == 0
    monkeypatch.setattr("agent.background_review.spawn_background_review_thread",
                        lambda agent, messages, **_: (lambda: True, "prompt"))
    assert crema_engine.distill(api, "agent-client-a")["ran"] is True
    assert store.distilled_count("agent-client-a") == len(turns(3))


def test_the_review_worker_says_whether_it_ran_to_its_end(monkeypatch):
    from agent import background_review as br

    class Agent:
        provider = "p"
        failures = []

        def _emit_auxiliary_failure(self, what, error):
            self.failures.append(what)

    monkeypatch.setattr(br, "_parent_can_emit_tool_calls", lambda agent: True)
    monkeypatch.setattr(br, "_run_review_fork", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("provider outage")), raising=False)
    target, _ = br.spawn_background_review_thread(Agent(), turns(3), review_memory=True, task_cfg={})
    assert target() is False
    assert Agent.failures == ["background review"]


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



# -- the daily pass on each chat's own model, and the notebook upkeep (Crema-업그레이드-승인-흐름 3-2) --------------


def test_daily_pass_distills_each_left_chat_on_its_own_model(store, reviews, monkeypatch):
    now = time.time()
    api = FakeApi({f"agent-client-{i}": (turns(4), now - 3600 - i) for i in range(12)})
    rows = api.db.list_sessions_rich()
    for i, row in enumerate(rows):
        row.update(model=f"model-{i}", billing_provider="openai-codex")
    monkeypatch.setattr(api.db, "list_sessions_rich", lambda **_: rows)
    report = crema_engine.daily_once(api, now)
    assert report["distilled"] == crema_engine.DAILY_MAX_CHATS == 10
    assert [c["requested_model"] for c in api.created] == [f"model-{i}" for i in range(10)]
    assert {c["requested_provider"] for c in api.created} == {"openai-codex"}


@pytest.fixture()
def judge(monkeypatch):
    """The auxiliary model, faked: answers by the system prompt it gets and records the calls."""
    calls, answers = [], {}

    def aux(task, system, user, max_tokens=1500):
        calls.append({"task": task, "user": user})
        answer = answers.get(task)
        return answer(user) if callable(answer) else answer
    monkeypatch.setattr("tools.knowledge_tool._aux_json", aux)
    return calls, answers


def test_upkeep_marks_disagreeing_pages_for_a_check_and_never_merges(store, judge):
    calls, answers = judge
    store.write("decision/세금계산서-발행일", title="세금계산서 발행일", body="매달 10일에 발행한다", sources=["chat:a"])
    store.write("decision/세금계산서-발행", title="세금계산서 발행", body="매달 25일에 발행한다", sources=["chat:b"])
    answers["knowledge_upkeep"] = {"same_topic": True, "contradicts": True, "why": "발행일이 10일과 25일로 다르다"}
    report = crema_engine.notebook_upkeep(store, time.time())
    assert report["conflicts"] == 1 and report["merge_candidates"] == 0
    for slug, other in (("decision/세금계산서-발행일", "decision/세금계산서-발행"), ("decision/세금계산서-발행", "decision/세금계산서-발행일")):
        page = store.get(slug)
        assert page["status"] == "needs_review"
        assert any(f"[[{other}]]" in t["summary"] and "10일과 25일" in t["summary"] for t in page["timeline"])
    assert len(store.list()) == 2  # both pages are still there
    # The same pair with the same text is not judged again.
    n = len(calls)
    crema_engine.notebook_upkeep(store, time.time())
    assert len(calls) == n


def test_upkeep_marks_a_close_page_as_a_merge_candidate_only(store, judge):
    calls, answers = judge
    store.write("incident/vpn-끊김", title="VPN 끊김", body="공유기 재시작으로 해결", sources=["chat:a"])
    store.write("incident/vpn-연결-오류", title="VPN 연결 오류", body="공유기를 껐다 켜서 해결", sources=["chat:b"])
    answers["knowledge_upkeep"] = {"same_topic": True, "contradicts": False, "why": "같은 VPN 문제와 해결"}
    report = crema_engine.notebook_upkeep(store, time.time())
    assert report["merge_candidates"] == 1 and report["conflicts"] == 0
    statuses = {p["slug"]: p["status"] for p in store.list()}
    assert sorted(statuses.values()) == ["active", "needs_review"]


def test_upkeep_writes_a_pattern_only_from_two_or_more_real_pages(store, judge):
    calls, answers = judge
    for i, body in enumerate(["표는 굵은 제목 없이", "보고는 결론부터", "보고는 결론을 먼저"]):
        store.write(f"feedback/지적-{i}", title=f"지적 {i}", body=body, sources=[f"chat:{i}"])
    answers["knowledge_upkeep"] = {"same_topic": False, "contradicts": False, "why": ""}
    answers["knowledge_patterns"] = {"patterns": [
        {"title": "결론부터 보고", "body": "보고는 결론을 먼저 쓴다.", "slugs": ["feedback/지적-1", "feedback/지적-2", "feedback/없는-쪽"]},
        {"title": "한 쪽만", "body": "근거가 하나뿐", "slugs": ["feedback/지적-0"]},
    ]}
    report = crema_engine.notebook_upkeep(store, time.time())
    assert report["patterns"] == 1
    page = store.get("feedback/결론부터-보고")
    assert "[[feedback/지적-1]]" in page["body"] and "[[feedback/지적-2]]" in page["body"]
    assert "없는-쪽" not in page["body"] and page["authority"] == "agent_observed"
    # Nothing new since: the patterns are not looked for again, and the pattern page is not its own evidence.
    n = len([c for c in calls if c["task"] == "knowledge_patterns"])
    crema_engine.notebook_upkeep(store, time.time())
    assert len([c for c in calls if c["task"] == "knowledge_patterns"]) == n
    # Something new: the patterns are read again, with the recorded one named so it is updated, not doubled.
    store.write("feedback/지적-3", title="지적 3", body="결론을 맨 위에", sources=["chat:3"])
    crema_engine.notebook_upkeep(store, time.time())
    last = [c for c in calls if c["task"] == "knowledge_patterns"][-1]
    assert "Patterns already recorded: 결론부터 보고" in last["user"] and "[feedback/결론부터-보고]" not in last["user"]


def test_upkeep_stays_within_its_daily_calls(store, judge):
    calls, answers = judge
    for i in range(20):
        store.write(f"reference/자료-{i}", title=f"자료 {i}", body=f"내용 {i}", sources=["t"])
    answers["knowledge_upkeep"] = {"same_topic": False, "contradicts": False, "why": ""}
    crema_engine.notebook_upkeep(store, time.time())
    assert len(calls) <= crema_engine.UPKEEP_MAX_CHECKS


def test_daily_pass_has_no_upkeep_on_the_free_plan_or_in_light_mode(store, reviews, judge, monkeypatch):
    calls, answers = judge
    store.write("decision/a", title="A 결정", body="10일", sources=["t"])
    store.write("decision/a-2", title="A 결정 다시", body="25일", sources=["t"])
    answers["knowledge_upkeep"] = {"same_topic": True, "contradicts": True, "why": "다름"}
    for config in ({"crema": {"free": True}}, {"knowledge": {"search_mode": "light"}}):
        store.set_maintenance_value("last_daily", {"at": 0})
        monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda config=config: config)
        report = crema_engine.daily_once(FakeApi({"agent-client-a": (turns(1), time.time() - 3600)}))
        assert "conflicts" not in report
    assert calls == []
