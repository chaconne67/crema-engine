"""Crema: the approval box says in Korean what a held command would do (agent/crema_approval_words.py)."""
import asyncio
from types import SimpleNamespace

import pytest

from agent.crema_approval_words import GENERAL, KOREAN, korean_description
from tools import approval_detection


def test_every_dangerous_command_the_engine_names_has_korean_words():
    # An upstream merge that adds a pattern fails here instead of showing the general words.
    names = [description for _, description in approval_detection.DANGEROUS_PATTERNS]
    names += [approval_detection._PARSER_LIMIT_DESCRIPTION, approval_detection._GATEWAY_LIFECYCLE_SPLICE_DESCRIPTION]
    assert [name for name in names if name not in KOREAN] == []


def test_joined_findings_are_each_put_into_korean_once():
    assert korean_description("recursive delete; recursive delete (long flag); git force push (rewrites remote history)") \
        == "폴더째 지우기 · 원격 저장소 기록 덮어쓰기(강제 푸시)"


def test_korean_words_stay_and_an_unknown_english_name_reads_as_a_dangerous_command():
    assert korean_description("메일 보내기") == "메일 보내기"
    assert korean_description("Plugin requires approval for some_tool") == GENERAL
    assert korean_description("") == ""


@pytest.mark.asyncio
async def test_the_approval_event_the_app_shows_is_in_korean_and_keeps_the_engines_key():
    from gateway.platforms.api_server_runs import _make_approval_notify

    host = SimpleNamespace(_set_run_status=lambda *_, **__: None)
    server = SimpleNamespace(_approval_event_choices=lambda **_: ["once", "deny"])
    queue = asyncio.Queue()
    notify = _make_approval_notify(host, SimpleNamespace(run_id="run-1", queue=queue), _api_server=server)
    notify({"command": "rm -rf build", "description": "recursive delete", "pattern_key": "recursive delete"})
    event = await asyncio.wait_for(queue.get(), 1)
    assert event["description"] == "폴더째 지우기"
    assert event["pattern_key"] == "recursive delete"
