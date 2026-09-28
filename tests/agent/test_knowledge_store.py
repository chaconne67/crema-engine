"""Crema knowledge store: GBrain's page model and hybrid keyword ranking on SQLite."""
import shutil
import subprocess
import time
from pathlib import Path

import pytest

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
