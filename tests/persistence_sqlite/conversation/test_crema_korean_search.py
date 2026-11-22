"""Korean past-chat search through the same read-only store used by Settings."""

import pytest

from persistence import SessionId
from persistence.conversation.messages import NewMessage
from persistence.conversation.sessions import NewSession
from persistence_assembly import open_persistence, open_persistence_reader, profile_scope


@pytest.mark.parametrize("read_only", [False, True])
def test_korean_search_with_writable_and_read_only_store(tmp_path, read_only):
    scope = profile_scope(home=tmp_path)
    sid = SessionId("korean-chat")
    with open_persistence(scope) as store:
        store.sessions.create(NewSession(sid, "api_server"))
        store.messages.append(sid, NewMessage(role="user", content="웅기가 shared default 프로필을 요청했다"))
        store.messages.append(sid, NewMessage(role="assistant", content="일본 MCP 후보 우선순위와 세금계산서를 정리했습니다"))
        store.messages.append(sid, NewMessage(role="user", content="graphiti daemon looks healthy"))
        store.messages.append(sid, NewMessage(role="assistant", content="修改youer服务端"))
        store.messages.append(sid, NewMessage(role="user", content="AI가 PC에서 DB를 Git으로 확인했다"))
        store.messages.append(sid, NewMessage(role="user", content="에러코드ab확인"))
        store.messages.append(sid, NewMessage(role="tool", content="일본 ab tool output", tool_name="terminal"))

    connection = open_persistence_reader(tmp_path / "state.db") if read_only else open_persistence(scope)
    with connection as store:
        for query, keyword in (
            ("웅기", "웅기"), ("일본", "일본"), ("세금*", "세금"), ("웅기*", "웅기"),
            ('"shared default" AND 웅기', "웅기"), ("우선순위", "우선순위"),
            ("가", "가"), ("graphiti", "graphiti"), ("youer", "youer"),
            ("AI", "AI"), ("PC", "PC"), ("DB", "DB"), ("ab", "ab"), ("ab*", "ab"), ("Git", "Git"),
        ):
            hits = store.search.search_messages(query)
            assert len(hits) == 1, query
            assert hits[0].session_id == sid
            assert keyword in hits[0].snippet, query
        assert len(store.search.search_messages("일본", role_filter=["tool"])) == 1
