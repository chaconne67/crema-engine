"""Crema launcher: the stored reply leaves out its last NEXT_INPUT line (the app's next-input guess)."""
from crema_engine import drop_next_input


def test_drops_only_the_last_next_input_line():
    assert drop_next_input(response_text="문서를 고쳤습니다.\n\nNEXT_INPUT: 커밋해 줘\n") == "문서를 고쳤습니다."
    assert drop_next_input(response_text="고쳤습니다.\n`NEXT_INPUT: 커밋해 줘`") == "고쳤습니다."


def test_keeps_replies_without_a_last_line_guess():
    assert drop_next_input(response_text="그냥 답입니다.") is None
    # Named mid-reply or mid-line, it is the answer's own text.
    assert drop_next_input(response_text="NEXT_INPUT: 형식을 설명합니다.\n이어지는 설명") is None
    assert drop_next_input(response_text="표시는 NEXT_INPUT: 처럼 씁니다") is None
    # A reply that is only the line is kept rather than stored empty.
    assert drop_next_input(response_text="NEXT_INPUT: 커밋해 줘") is None


def test_drops_the_chat_title_line_with_it():
    assert drop_next_input(response_text="옮겼습니다.\nCHAT_TITLE: 결제 화면 디자인 변경\nNEXT_INPUT: 커밋해 줘\n") == "옮겼습니다."
    assert drop_next_input(response_text="옮겼습니다.\nCHAT_TITLE: -") == "옮겼습니다."
    # Only the end lines: a third one up is the answer's own text.
    assert drop_next_input(response_text="CHAT_TITLE: 설명\n본문\nNEXT_INPUT: -") == "CHAT_TITLE: 설명\n본문"
    assert drop_next_input(response_text="CHAT_TITLE: 제목\nNEXT_INPUT: -") is None


def test_logs_what_the_end_line_was(caplog):
    caplog.set_level("INFO", logger="crema_engine")
    drop_next_input(response_text="고쳤습니다.\nNEXT_INPUT: 커밋해 줘", session_id="agent-client-a")
    drop_next_input(response_text="승인해 주세요.\n**NEXT_INPUT:** -", session_id="agent-client-b")
    drop_next_input(response_text="그냥 답입니다.", session_id="agent-client-c")
    assert [r.getMessage() for r in caplog.records] == [
        "next input: session=agent-client-a given",
        "next input: session=agent-client-b none",
        "next input: session=agent-client-c missing",
    ]


def test_drops_end_lines_with_blank_lines_between_and_the_role_line():
    # 980a2e93 18:43 (state.db 61236): the title line above a blank line stayed in the stored reply.
    assert drop_next_input(response_text="시작하겠습니다.\n\nCHAT_TITLE: -\n\nNEXT_INPUT: 진행해 줘") == "시작하겠습니다."
    assert drop_next_input(response_text="사과, 초록\n\nCHAT_ROLE: 기능 시험\nCHAT_TITLE: 선택지 시험\nNEXT_INPUT: -") == "사과, 초록"
    # Only the end lines: a marker line above the answer's own text stays.
    assert drop_next_input(response_text="본문\nCHAT_TITLE: 설명\n본문 끝\n\nNEXT_INPUT: -") == "본문\nCHAT_TITLE: 설명\n본문 끝"


def test_logs_given_when_the_guess_follows_a_blank_line(caplog):
    caplog.set_level("INFO", logger="crema_engine")
    drop_next_input(response_text="답.\n\nCHAT_TITLE: -\n\nNEXT_INPUT: 커밋해 줘", session_id="agent-client-d")
    assert [r.getMessage() for r in caplog.records] == ["next input: session=agent-client-d given"]
