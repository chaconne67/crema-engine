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
