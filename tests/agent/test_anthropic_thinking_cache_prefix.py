"""Crema (주인님 2026-10-31, 2026-11-01): each Anthropic request extends the one before byte for byte.

Stripping thinking from earlier messages changed the conversation there, so the prompt cache missed from
that point and the call rewrote the whole conversation: first within a turn's tool steps, then on the first
two calls after each new user message.
"""

import json

from agent.anthropic_message_convert import convert_messages_to_anthropic


def _step(n: int) -> list:
    return [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"toolu_{n}", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}],
         "anthropic_content_blocks": [
             {"type": "thinking", "thinking": f"step {n}", "signature": f"sig-{n}"},
             {"type": "tool_use", "id": f"toolu_{n}", "name": "terminal", "input": {}}]},
        {"role": "tool", "tool_call_id": f"toolu_{n}", "content": f"output {n}"},
    ]


def _wire(messages: list) -> list:
    _system, converted = convert_messages_to_anthropic(messages, base_url=None, model="claude-opus-5-5")
    return [json.dumps(m, sort_keys=True, default=str) for m in converted]


def test_a_tool_step_extends_the_previous_request_unchanged():
    earlier = [{"role": "user", "content": "이전 일"}, *_step(0), {"role": "assistant", "content": "끝났습니다"}]
    turn = [{"role": "user", "content": "파일 셋을 고쳐 줘"}]
    first = _wire(earlier + turn + _step(1))
    second = _wire(earlier + turn + _step(1) + _step(2))
    assert second[:len(first)] == first


def test_a_new_user_message_extends_the_previous_request_unchanged():
    turn = [{"role": "user", "content": "이전 일"}, *_step(0), *_step(1), {"role": "assistant", "content": "끝났습니다"}]
    before = _wire(turn)
    first = _wire(turn + [{"role": "user", "content": "다음 일"}])
    second = _wire(turn + [{"role": "user", "content": "다음 일"}] + _step(2))
    assert first[:len(before)] == before
    assert second[:len(first) - 1] == first[:-1]
    # The earlier turn keeps its signed thinking, as the model wrote it.
    assert any('"sig-0"' in m for m in second) and any('"sig-1"' in m for m in second)
