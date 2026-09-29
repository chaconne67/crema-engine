"""Crema knowledge store: GBrain's page model and hybrid keyword and meaning ranking on SQLite."""
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from agent import knowledge_embed
from agent.knowledge_store import KnowledgeStore, chunk_text, extract_links, slugify

REPO = Path(__file__).resolve().parent.parent.parent
SRC = REPO / "native" / "fts5_cjk" / "fts5_cjk.c"


@pytest.fixture(scope="session")
def cjk_so(tmp_path_factory):
    if shutil.which("gcc") is None or not SRC.exists():
        return None
    out = tmp_path_factory.mktemp("fts5cjk") / "libfts5_cjk.so"
    subprocess.run(["gcc", "-shared", "-fPIC", "-O2", f"-I{SRC.parent / 'vendor'}", str(SRC), "-o", str(out)],
                   check=True, capture_output=True)
    return out


@pytest.fixture()
def store(tmp_path, monkeypatch, cjk_so):
    if cjk_so:
        monkeypatch.setenv("HERMES_FTS5_CJK_SO", str(cjk_so))
    KnowledgeStore._shared.clear()
    s = KnowledgeStore.open(tmp_path / "knowledge.db")
    yield s
    KnowledgeStore._shared.clear()


def test_slugs_keep_hangul_and_paths():
    assert slugify("Incident / 세금계산서 발행 오류") == "incident/세금계산서-발행-오류"
    assert slugify("project/Crema  Engine") == "project/crema-engine"


def test_chunks_count_characters_for_korean():
    text = "가" * 700
    chunks = chunk_text(text)
    assert len(chunks) >= 2


def test_links_by_rule():
    body = "원인은 [[incident/세금-오류]]와 같다. 자세히는 [여기](reference/홈택스) 또는 [사이트](https://x.io)."
    assert extract_links(body) == ["incident/세금-오류", "reference/홈택스"]


def test_write_skips_same_content_and_keeps_versions(store):
    first = store.write("incident/세금 오류", title="세금계산서 발행 오류", body="원인: 사업자번호 누락", sources=["chat:a"])
    assert first["result"] == "created"
    assert store.write("incident/세금 오류", title="세금계산서 발행 오류", body="원인: 사업자번호 누락", sources=["chat:b"])["result"] == "unchanged"
    second = store.write("incident/세금 오류", title="세금계산서 발행 오류", body="원인: 사업자번호 누락. 해결: 거래처 정보 수정", sources=["chat:c"])
    assert second["result"] == "updated" and second["version"]
    page = store.get("incident/세금-오류")
    assert "해결" in page["body"] and set(page["sources"]) >= {"chat:a", "chat:c"}
    assert store.undo("incident/세금-오류")["result"] == "reverted"
    assert "해결" not in store.get("incident/세금-오류")["body"]


def test_write_requires_sources_and_known_kind(store):
    with pytest.raises(ValueError):
        store.write("incident/x", title="t", body="b", sources=[])
    with pytest.raises(ValueError):
        store.write("people/x", title="t", body="b", sources=["chat:a"])


def test_soft_delete_and_undo(store):
    store.write("feedback/보고서-말투", title="보고서는 존댓말로", body="사용자가 보고서 문체를 존댓말로 고쳐 달라고 함", sources=["chat:a"])
    store.delete("feedback/보고서-말투")
    assert store.search(["보고서"]) == []
    store.undo("feedback/보고서-말투")
    assert store.search(["보고서"])[0]["slug"] == "feedback/보고서-말투"


def test_a_page_the_user_deleted_stays_deleted_until_the_user_undoes(store):
    store.write("feedback/보고서-말투", title="보고서는 존댓말로", body="사용자가 보고서 문체를 존댓말로 고쳐 달라고 함", sources=["chat:a"])
    store.delete("feedback/보고서-말투")  # Settings → 기억
    with pytest.raises(ValueError, match="deleted by the user"):
        store.write("feedback/보고서-말투", title="보고서는 존댓말로", body="다시 배운 내용", sources=["chat:a"])
    assert store.search(["보고서"]) == [] and store.list() == []
    with pytest.raises(ValueError):
        store.get("feedback/보고서-말투")
    assert store.undo("feedback/보고서-말투")["result"] == "restored"
    assert store.write("feedback/보고서-말투", title="보고서는 존댓말로", body="다시 배운 내용", sources=["chat:a"])["result"] == "updated"
    assert store.get("feedback/보고서-말투")["body"] == "다시 배운 내용"


def test_undoing_a_new_page_is_the_users_delete(store):
    store.write("reference/새-주소", title="새 사이트 주소", body="지금 주소는 new.example", sources=["chat:a"])
    assert store.undo("reference/새-주소")["result"] == "deleted"
    with pytest.raises(ValueError, match="deleted by the user"):
        store.write("reference/새-주소", title="새 사이트 주소", body="지금 주소는 new.example", sources=["chat:b"])


def test_pages_deleted_before_anyone_was_recorded_count_as_the_users(store):
    store.write("reference/새-주소", title="새 사이트 주소", body="지금 주소는 new.example", sources=["chat:a"])
    store.conn.execute("UPDATE pages SET deleted_at = ? WHERE slug = 'reference/새-주소'", (time.time(),))
    with pytest.raises(ValueError, match="deleted by the user"):
        store.write("reference/새-주소", title="새 사이트 주소", body="지금 주소는 new.example", sources=["chat:b"])


def test_a_page_the_agent_deleted_comes_back_and_a_purged_slug_is_free(store):
    store.write("reference/옛-주소", title="옛 사이트 주소", body="예전 주소는 old.example", sources=["chat:a"])
    store.delete("reference/옛-주소", by="agent")
    assert store.write("reference/옛-주소", title="옛 사이트 주소", body="예전 주소는 old.example", sources=["chat:b"])["result"] == "updated"
    assert store.get("reference/옛-주소")["title"] == "옛 사이트 주소"
    store.delete("reference/옛-주소")  # now the user
    with pytest.raises(ValueError, match="deleted by the user"):
        store.write("reference/옛-주소", title="옛 사이트 주소", body="예전 주소는 old.example", sources=["chat:c"])
    store.conn.execute("UPDATE pages SET deleted_at = ? WHERE slug = 'reference/옛-주소'", (time.time() - 80 * 3600,))
    assert store.maintain()["purged"] == 1
    assert store.write("reference/옛-주소", title="옛 사이트 주소", body="예전 주소는 old.example", sources=["chat:d"])["result"] == "created"


def test_settings_lists_what_the_user_deleted_while_undo_still_brings_it_back(store):
    for slug, title in (("feedback/보고서-말투", "보고서는 존댓말로"), ("reference/옛-주소", "옛 사이트 주소"),
                        ("reference/새-주소", "새 사이트 주소"), ("project/세무", "세무 일정")):
        store.write(slug, title=title, body=f"{title} 내용", sources=["chat:a"])
    store.delete("reference/새-주소")
    store.conn.execute("UPDATE pages SET deleted_at = deleted_at - 60 WHERE slug = 'reference/새-주소'")
    store.delete("feedback/보고서-말투")  # Settings → 기억
    store.delete("reference/옛-주소", by="agent")  # the agent may write it again itself
    store.delete("project/세무")
    store.conn.execute("UPDATE pages SET deleted_at = ? WHERE slug = 'project/세무'", (time.time() - 73 * 3600,))
    deleted = store.list(deleted=True)  # over 72 hours is gone from view even before maintain() purges it
    assert [p["slug"] for p in deleted] == ["feedback/보고서-말투", "reference/새-주소"]
    assert deleted[0]["restorable_until"] - time.time() == pytest.approx(72 * 3600, abs=60)
    assert store.list() == []
    store.undo("feedback/보고서-말투")
    assert [p["slug"] for p in store.list(deleted=True)] == ["reference/새-주소"]
    assert [p["slug"] for p in store.list()] == ["feedback/보고서-말투"]


def test_search_korean_short_words_titles_and_exact(store, cjk_so):
    store.write("incident/세금계산서-오류", title="세금계산서 발행 오류",
                body="홈택스에서 세금계산서를 발행할 때 사업자번호 누락으로 실패했다. 거래처 정보를 고쳐 해결.", sources=["chat:a"])
    store.write("reference/부가세-일정", title="부가가치세 신고 일정",
                body="부가가치세 신고는 1월과 7월 25일까지.", sources=["https://www.nts.go.kr"])
    store.write("decision/보고서-형식", title="주간 보고서는 표로",
                body="주간 보고서는 표 한 장으로 정리하기로 했다. 이유: 읽는 사람이 빨리 비교.", sources=["chat:b"])
    if cjk_so:
        top = store.search(["세금"])
        assert top and top[0]["slug"] == "incident/세금계산서-오류"
    assert store.search(["부가가치세 신고"])[0]["slug"] == "reference/부가세-일정"
    exact = store.search(["주간 보고서는 표로"])
    assert exact[0]["slug"] == "decision/보고서-형식" and exact[0]["evidence"] == "exact"


def test_search_fuses_alternative_questions(store):
    store.write("incident/로그인-실패", title="관리자 로그인 실패", body="비밀번호 재설정 메일이 스팸함으로 감", sources=["chat:a"])
    results = store.search(["접속이 안 돼요", "로그인 실패"])
    assert results and results[0]["slug"] == "incident/로그인-실패"


def test_timeline_links_backlinks_and_related(store):
    store.write("project/월말-정산", title="월말 정산", body="순서는 [[reference/은행-내역]] 받기부터.", sources=["chat:a"])
    store.write("reference/은행-내역", title="은행 거래내역 받는 법", body="인터넷뱅킹에서 엑셀로 받는다.", sources=["chat:a"])
    store.add_timeline("project/월말-정산", "9월 정산 끝", date="2026-09-30", source="chat:x")
    assert store.add_timeline("project/월말-정산", "9월 정산 끝", date="2026-09-30")["result"] == "unchanged"
    page = store.get("reference/은행-내역")
    assert page["backlinks"] == ["project/월말-정산"]
    project = store.get("project/월말-정산")
    assert project["timeline"][0]["summary"] == "9월 정산 끝"
    hit = store.search(["월말 정산"])[0]
    assert hit["slug"] == "project/월말-정산" and "reference/은행-내역" in hit.get("related", [])


def test_status_weights_and_maintenance(store, monkeypatch):
    store.write("reference/옛-주소", title="옛 사이트 주소", body="예전 주소는 old.example", sources=["chat:a"])
    store.write("reference/새-주소", title="새 사이트 주소", body="지금 주소는 new.example", sources=["chat:a"])
    store.set_status("reference/옛-주소", "superseded")
    ranked = [p["slug"] for p in store.search(["사이트 주소"])]
    assert ranked.index("reference/새-주소") < ranked.index("reference/옛-주소")
    store.conn.execute("UPDATE pages SET confirmed_at = ? WHERE slug = 'reference/새-주소'", (time.time() - 100 * 86400,))
    store.delete("reference/옛-주소")
    store.conn.execute("UPDATE pages SET deleted_at = ? WHERE slug = 'reference/옛-주소'", (time.time() - 80 * 3600,))
    report = store.maintain()
    assert report["purged"] == 1 and report["needs_review"] == 1
    assert store.get("reference/새-주소")["status"] == "needs_review"


class FakeEmbedder:
    """Meaning by concept groups, so a question in other words lands on the right page."""
    GROUPS = (("프린터", "인쇄", "출력"), ("와이파이", "무선", "인터넷"), ("세금", "부가세", "신고"))

    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0

    def encode(self, texts, kind="passage"):
        self.calls += 1
        if self.fail:
            raise RuntimeError("model broke")
        out = np.full((len(texts), len(self.GROUPS) + 1), 0.01, dtype=np.float32)
        for i, text in enumerate(texts):
            for g, words in enumerate(self.GROUPS):
                out[i, g] += sum(text.count(w) for w in words)
        return out / np.linalg.norm(out, axis=1, keepdims=True)


@pytest.fixture()
def meaning(monkeypatch):
    def use(embedder):
        knowledge_embed.set_embedder(embedder)
        return embedder
    yield use
    knowledge_embed.set_embedder(None)


def _two_pages(store):
    store.write("incident/프린터", title="사무실 프린터 인쇄 안 됨", body="스풀러를 다시 시작해 해결.", sources=["t"])
    store.write("incident/와이파이", title="노트북 와이파이 끊김", body="어댑터 절전을 꺼서 해결.", sources=["t"])


def test_meaning_finds_a_question_in_other_words(store, meaning):
    meaning(FakeEmbedder())
    _two_pages(store)
    assert store.vector_status()["vectors"] == store.vector_status()["chunks"] == 2
    top = store.search(["출력 문제"])[0]
    assert top["slug"] == "incident/프린터" and top["evidence"] == "meaning" and "프린터" in top["snippet"]
    assert store.search(["출력 문제"], type_="feedback") == []


def test_deleted_pages_leave_meaning_search_and_undo_brings_them_back(store, meaning):
    meaning(FakeEmbedder())
    _two_pages(store)
    store.search(["출력 문제"])  # vectors cached
    store.delete("incident/프린터")
    assert "incident/프린터" not in [r["slug"] for r in store.search(["출력 문제"])]
    store.undo("incident/프린터")
    assert store.search(["출력 문제"])[0]["slug"] == "incident/프린터"


def test_without_a_working_model_words_still_work_and_vectors_fill_later(store, meaning):
    meaning(FakeEmbedder(fail=True))
    _two_pages(store)
    assert store.vector_status()["vectors"] == 0
    assert store.search(["프린터"])[0]["slug"] == "incident/프린터"
    assert store.search(["출력 문제"]) == []
    good = meaning(FakeEmbedder())
    assert store.fill_vectors() == 2
    assert store.search(["출력 문제"])[0]["slug"] == "incident/프린터"
    good.calls = 0
    assert store.fill_vectors() == 0 and good.calls == 0


def test_no_model_means_keyword_search(store, meaning, monkeypatch):
    monkeypatch.delenv("CREMA_EMBED_MODEL", raising=False)
    knowledge_embed.set_embedder(None)
    _two_pages(store)
    assert store.vector_status() == {"on": False, "chunks": 2, "vectors": 0}
    assert store.fill_vectors() == 0
    assert store.search(["와이파이"])[0]["evidence"] in ("title", "keyword")
