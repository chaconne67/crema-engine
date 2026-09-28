"""Search quality of the knowledge notebook on a made-up Korean workbook (no user data): how often the
right page is in the top 3 for questions asked the way a user would ask them. ``queries`` holds what
the agent sends: the question's key words, then (as knowledge_search asks of it) up to two rephrasings.
The floors guard against ranking changes making things worse; the measured rates are in the Crema
plan (docs/Crema-기억-2단계-마스터플랜-2026-09-28.md)."""
import shutil
import subprocess
from pathlib import Path

import pytest

from agent.knowledge_store import KnowledgeStore

REPO = Path(__file__).resolve().parent.parent.parent
SRC = REPO / "native" / "fts5_cjk" / "fts5_cjk.c"

PAGES = [
    ("incident/세금계산서-발행-오류", "세금계산서 발행 오류", "홈택스에서 전자세금계산서 발행이 거래처 사업자번호 누락으로 실패했다. 거래처 정보에 사업자번호를 넣고 다시 발행해 해결."),
    ("incident/엑셀-한글-깨짐", "엑셀 CSV 한글 깨짐", "CSV를 엑셀로 열면 한글이 깨졌다. 원인은 인코딩. UTF-8 BOM으로 저장하거나 데이터 가져오기로 열어 해결."),
    ("incident/프린터-인쇄-안됨", "사무실 프린터 인쇄 안 됨", "인쇄 대기열이 멈춰 출력이 안 됐다. 스풀러 서비스를 다시 시작하고 대기열을 비워 해결."),
    ("incident/메일-첨부-용량", "메일 첨부 용량 초과", "25MB 넘는 파일을 메일에 첨부하니 반송됐다. 대용량 첨부(클라우드 링크)로 보내 해결."),
    ("incident/와이파이-끊김", "노트북 와이파이 자주 끊김", "절전 설정이 무선 어댑터를 꺼서 연결이 끊겼다. 전원 관리에서 어댑터 절전을 꺼서 해결."),
    ("feedback/보고서-존댓말", "보고서는 존댓말로", "사용자가 보고서 문체를 반말이 아닌 존댓말로 고쳐 달라고 했다. 모든 보고서는 합쇼체로 쓴다."),
    ("feedback/표보다-목록", "짧은 비교는 목록으로", "항목이 두세 개뿐인 비교는 표 대신 목록을 원한다고 사용자가 고쳐 줬다."),
    ("feedback/파일명-날짜", "파일 이름 앞에 날짜", "사용자는 결과 파일 이름을 20260928_제목 형식으로 날짜부터 쓰기를 원한다."),
    ("decision/주간보고-형식", "주간 보고서는 표 한 장", "주간 보고서는 표 한 장으로 정리하기로 했다. 이유: 팀장이 빨리 비교해 보기 위해."),
    ("decision/회의록-공유", "회의록은 당일 공유", "회의록은 회의 당일 오후 6시 전에 팀 채널에 공유하기로 결정."),
    ("reference/부가세-신고-기한", "부가가치세 신고 기한", "부가가치세 확정신고는 1월 25일과 7월 25일까지. 예정신고는 4월과 10월 25일."),
    ("reference/홈택스-메뉴", "홈택스 전자세금계산서 메뉴", "전자세금계산서는 홈택스 조회/발급 > 전자세금계산서 > 발급 메뉴에서 한다."),
    ("reference/지원사업-서류", "정부지원사업 신청 서류", "사업계획서, 사업자등록증 사본, 재무제표, 4대보험 가입자 명부가 필요하다."),
    ("project/월말-정산", "월말 정산 순서", "은행 거래내역 받기 → 카드 사용내역 맞추기 → 미수금 확인 → 정산표 작성 순서로 한다."),
    ("project/신입-온보딩", "신입 온보딩 준비", "계정 발급, 노트북 세팅, 보안 교육 일정을 입사 전날까지 준비한다."),
]

# (queries the agent sends, the page that answers)
QUESTIONS = [
    (["세금계산서 발행 오류"], "incident/세금계산서-발행-오류"),
    (["세금계산서 발행", "전자세금계산서 실패"], "incident/세금계산서-발행-오류"),
    (["거래처 사업자번호"], "incident/세금계산서-발행-오류"),
    (["엑셀 한글 깨짐"], "incident/엑셀-한글-깨짐"),
    (["CSV 글자 깨짐", "엑셀 인코딩"], "incident/엑셀-한글-깨짐"),
    (["프린터 안 됨", "인쇄 대기열"], "incident/프린터-인쇄-안됨"),
    (["출력이 안 돼", "프린터 인쇄"], "incident/프린터-인쇄-안됨"),
    (["메일 첨부 반송", "첨부 용량"], "incident/메일-첨부-용량"),
    (["큰 파일 메일로 보내기", "대용량 첨부"], "incident/메일-첨부-용량"),
    (["와이파이 끊김"], "incident/와이파이-끊김"),
    (["인터넷 연결 끊김", "무선 연결"], "incident/와이파이-끊김"),
    (["보고서 말투"], "feedback/보고서-존댓말"),
    (["보고서 문체", "존댓말"], "feedback/보고서-존댓말"),
    (["비교 표 목록"], "feedback/표보다-목록"),
    (["파일 이름 규칙", "파일명 날짜"], "feedback/파일명-날짜"),
    (["주간 보고서 형식"], "decision/주간보고-형식"),
    (["주간보고 어떻게 쓰기로", "주간 보고서 표"], "decision/주간보고-형식"),
    (["회의록 언제 공유"], "decision/회의록-공유"),
    (["회의 기록 공유", "회의록"], "decision/회의록-공유"),
    (["부가세 신고 기한"], "reference/부가세-신고-기한"),
    (["부가가치세 언제까지", "부가세 신고"], "reference/부가세-신고-기한"),
    (["홈택스 전자세금계산서 메뉴"], "reference/홈택스-메뉴"),
    (["세금계산서 발급 어디서", "홈택스 메뉴"], "reference/홈택스-메뉴"),
    (["지원사업 서류"], "reference/지원사업-서류"),
    (["정부 과제 신청 준비물", "지원사업 신청 서류"], "reference/지원사업-서류"),
    (["월말 정산"], "project/월말-정산"),
    (["달 마감 순서", "월말 정산 순서"], "project/월말-정산"),
    (["신입 온보딩"], "project/신입-온보딩"),
    (["새로 온 직원 준비", "입사 준비"], "project/신입-온보딩"),
    (["세금"], "incident/세금계산서-발행-오류"),
]


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    if shutil.which("gcc") is None or not SRC.exists():
        pytest.skip("no C toolchain / tokenizer source")
    so = tmp_path_factory.mktemp("cjk") / "libfts5_cjk.so"
    subprocess.run(["gcc", "-shared", "-fPIC", "-O2", f"-I{SRC.parent / 'vendor'}", str(SRC), "-o", str(so)],
                   check=True, capture_output=True)
    mp = pytest.MonkeyPatch()
    mp.setenv("HERMES_FTS5_CJK_SO", str(so))
    KnowledgeStore._shared.clear()
    s = KnowledgeStore.open(tmp_path_factory.mktemp("kq") / "knowledge.db")
    for slug, title, body in PAGES:
        s.write(slug, title=title, body=body, sources=["test"])
    yield s
    KnowledgeStore._shared.clear()
    mp.undo()


def _rate(store, questions, first_only=False):
    hits = 0
    for queries, answer in questions:
        results = store.search(queries[:1] if first_only else queries, limit=8)
        hits += answer in [r["slug"] for r in results[:3]]
    return hits / len(questions)


def test_top3_rate_with_the_agents_rephrasings(store):
    rate = _rate(store, QUESTIONS)
    print(f"\nknowledge search top-3 (with rephrasings): {rate:.0%}")
    assert rate >= 0.9


def test_top3_rate_on_the_first_words_alone(store):
    rate = _rate(store, QUESTIONS, first_only=True)
    print(f"\nknowledge search top-3 (first words only): {rate:.0%}")
    assert rate >= 0.7


# Asked in words the pages do not use: where keyword search stops and meaning search (plan step 3)
# would help. Measured and printed, not asserted.
HARD = [
    (["돈 계산 마무리"], "project/월말-정산"),
    (["출력기 먹통"], "incident/프린터-인쇄-안됨"),
    (["글자가 이상하게 나와"], "incident/엑셀-한글-깨짐"),
    (["높임말"], "feedback/보고서-존댓말"),
    (["세금 내는 날"], "reference/부가세-신고-기한"),
    (["첫 출근 준비물"], "project/신입-온보딩"),
    (["인터넷이 자꾸 나가"], "incident/와이파이-끊김"),
    (["회의 끝나고 정리한 것 보내기"], "decision/회의록-공유"),
]


def test_top3_rate_in_other_words(store):
    rate = _rate(store, HARD)
    print(f"\nknowledge search top-3 (other words): {rate:.0%}")
