"""Crema launcher: the stored reply leaves out its end lines (the app's chat title and role)."""
from crema_engine import drop_end_lines


def test_drops_the_chat_title_line():
    assert drop_end_lines(response_text="옮겼습니다.\nCHAT_TITLE: 결제 화면 디자인 변경\n") == "옮겼습니다."
    assert drop_end_lines(response_text="옮겼습니다.\n`CHAT_TITLE: 결제 화면`") == "옮겼습니다."
    assert drop_end_lines(response_text="옮겼습니다.\nCHAT_TITLE: -") == "옮겼습니다."


def test_keeps_replies_without_end_lines():
    assert drop_end_lines(response_text="그냥 답입니다.") is None
    # Named mid-reply or mid-line, it is the answer's own text.
    assert drop_end_lines(response_text="CHAT_TITLE: 형식을 설명합니다.\n이어지는 설명") is None
    assert drop_end_lines(response_text="표시는 CHAT_TITLE: 처럼 씁니다") is None
    # A reply that is only the line is kept rather than stored empty.
    assert drop_end_lines(response_text="CHAT_TITLE: 제목") is None


def test_drops_end_lines_with_blank_lines_between_and_the_role_line():
    # 980a2e93 18:43 (state.db 61236): the title line above a blank line stayed in the stored reply.
    assert drop_end_lines(response_text="시작하겠습니다.\n\nCHAT_ROLE: 기능 시험\n\nCHAT_TITLE: -") == "시작하겠습니다."
    assert drop_end_lines(response_text="사과, 초록\n\nCHAT_ROLE: 기능 시험\nCHAT_TITLE: 선택지 시험") == "사과, 초록"
    # Only the end lines: a marker line above the answer's own text stays.
    assert drop_end_lines(response_text="본문\nCHAT_TITLE: 설명\n본문 끝\n\nCHAT_TITLE: -") == "본문\nCHAT_TITLE: 설명\n본문 끝"
