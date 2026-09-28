"""Crema knowledge tools: handlers, reranking and think through the auxiliary model, and the
always-on guidance."""
import json
from types import SimpleNamespace

import pytest

from agent.knowledge_store import KnowledgeStore
import tools.knowledge_tool as kt


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    KnowledgeStore._shared.clear()
    s = KnowledgeStore.open(tmp_path / "knowledge.db")
    monkeypatch.setattr(KnowledgeStore, "open", classmethod(lambda cls, path=None: s))
    yield s
    KnowledgeStore._shared.clear()


def _seed(store):
    for i in range(5):
        store.write(f"reference/주소-{i}", title=f"사이트 주소 {i}", body=f"사이트 주소 메모 {i}", sources=["chat:a"])
    store.write("incident/세금계산서-오류", title="세금계산서 발행 오류", body="사업자번호 누락이 원인. 거래처 정보 수정으로 해결.",
                sources=["chat:a"])


def test_write_adds_this_chat_as_a_source(store):
    out = json.loads(kt.knowledge_write({"action": "write", "slug": "feedback/존댓말", "title": "보고서는 존댓말",
                                         "body": "사용자가 보고서 문체를 존댓말로 고쳐 줌", "sources": [],
                                         "authority": "user_said"}, session_id="agent-client-x"))
    assert out["result"] == "created"
    assert store.get("feedback/존댓말")["sources"] == ["chat:agent-client-x"]


def test_write_errors_are_tool_errors():
    out = json.loads(kt.knowledge_write({"action": "write", "slug": "people/x", "title": "t", "body": "b"}))
    assert "error" in out


def test_timeline_status_and_delete(store):
    _seed(store)
    kt.knowledge_write({"action": "timeline", "slug": "incident/세금계산서-오류", "summary": "다시 발생, 같은 방법으로 해결",
                        "date": "2026-10-02"})
    kt.knowledge_write({"action": "status", "slug": "reference/주소-0", "status": "superseded"})
    kt.knowledge_write({"action": "delete", "slug": "reference/주소-1"})
    page = json.loads(kt.knowledge_get({"slug": "incident/세금계산서-오류"}))
    assert page["timeline"][0]["summary"] == "다시 발생, 같은 방법으로 해결"
    assert "error" in json.loads(kt.knowledge_get({"slug": "reference/주소-1"}))


def test_writing_a_page_the_user_deleted_tells_the_agent(store):
    _seed(store)
    store.delete("reference/주소-2")  # Settings → 기억
    out = json.loads(kt.knowledge_write({"action": "write", "slug": "reference/주소-2", "title": "사이트 주소 2",
                                         "body": "다시 배운 주소", "sources": ["chat:b"]}))
    assert "deleted by the user" in out["error"] and "another slug" in out["error"]
    assert "error" in json.loads(kt.knowledge_get({"slug": "reference/주소-2"}))
    # A page the agent deleted itself it may write again.
    kt.knowledge_write({"action": "delete", "slug": "reference/주소-3"})
    out = json.loads(kt.knowledge_write({"action": "write", "slug": "reference/주소-3", "title": "사이트 주소 3",
                                         "body": "다시 배운 주소", "sources": ["chat:b"]}))
    assert out["result"] == "updated"


def test_search_reranks_in_balanced_mode_and_keeps_order_on_failure(monkeypatch, store):
    _seed(store)
    monkeypatch.setattr(kt, "_mode", lambda: "balanced")
    monkeypatch.setattr(kt, "_aux_json", lambda *a, **k: {"order": [3, 0]})
    reranked = json.loads(kt.knowledge_search({"queries": ["사이트 주소"]}))["results"]
    monkeypatch.setattr(kt, "_aux_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")))
    fused = json.loads(kt.knowledge_search({"queries": ["사이트 주소"]}))["results"]
    assert reranked[0]["slug"] == fused[3]["slug"] and reranked[1]["slug"] == fused[0]["slug"]


def test_light_mode_never_calls_the_model(monkeypatch, store):
    _seed(store)
    monkeypatch.setattr(kt, "_mode", lambda: "light")
    monkeypatch.setattr(kt, "_aux_json", lambda *a, **k: pytest.fail("model called in light mode"))
    assert json.loads(kt.knowledge_search({"queries": ["사이트 주소", "홈페이지 주소"]}))["results"]


def test_think_answers_from_pages(monkeypatch, store):
    _seed(store)
    seen = {}

    def fake(task, system, user, max_tokens=0):
        seen["user"] = user
        return {"answer": "사업자번호 누락이 원인입니다 [incident/세금계산서-오류]", "pages": ["incident/세금계산서-오류"],
                "conflicts": "", "unknown": ""}
    monkeypatch.setattr(kt, "_aux_json", fake)
    out = json.loads(kt.knowledge_think({"question": "세금계산서 오류 원인"}))
    assert out["pages"] == ["incident/세금계산서-오류"] and "incident/세금계산서-오류" in seen["user"]


def test_guidance_is_in_the_system_prompt_when_the_tools_are_loaded():
    from agent.system_prompt import _tool_guidance_block
    agent = SimpleNamespace(valid_tool_names={"knowledge_search", "knowledge_get", "knowledge_write"})
    block = _tool_guidance_block(agent)
    assert block and "knowledge_search" in block and "language the user writes in" in block
    assert _tool_guidance_block(SimpleNamespace(valid_tool_names={"read_file"})) is None


def test_memory_is_written_in_the_users_language():
    from tools.memory_tool import MEMORY_SCHEMA
    from agent.background_review import _MEMORY_REVIEW_PROMPT
    assert "language the user writes in" in MEMORY_SCHEMA["description"]
    assert "language the user writes in" in _MEMORY_REVIEW_PROMPT
