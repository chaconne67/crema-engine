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
            ("웅기", "웅기"), ("세금*", "세금"), ("웅기*", "웅기"),
            ('"shared default" AND 웅기', "웅기"), ("우선순위", "우선순위"),
            ("graphiti", "graphiti"), ("youer", "youer"),
            ("AI", "AI"), ("PC", "PC"), ("DB", "DB"), ("Git", "Git"),
        ):
            hits = store.search.search_messages(query)
            assert len(hits) == 1, query
            assert hits[0].session_id == sid
            assert keyword in hits[0].snippet, query
        assert {hit.snippet for hit in store.search.search_messages("가")} == {
            "웅기가 shared default 프로필을 요청했다",
            "AI가 PC에서 DB를 Git으로 확인했다",
        }
        for query in ("일본", "일본 정리"):
            assert [(hit.role, hit.snippet) for hit in store.search.search_messages(query)] == [
                ("assistant", "일본 MCP 후보 우선순위와 세금계산서를 정리했습니다"),
            ]
        for query in ("ab", "ab*"):
            assert [(hit.role, hit.snippet) for hit in store.search.search_messages(query)] == [
                ("tool", "일본 >>>ab<<< tool output"),
            ]
        assert [(hit.role, hit.snippet) for hit in store.search.search_messages("일본", role_filter=["tool"])] == [
            ("tool", "일본 ab tool output"),
        ]


@pytest.mark.parametrize("read_only", [False, True])
def test_short_ascii_retry_keeps_ascii_word_boundaries(tmp_path, read_only):
    scope = profile_scope(home=tmp_path)
    sid = SessionId("mixed-words")
    with open_persistence(scope) as store:
        store.sessions.create(NewSession(sid, "api_server"))
        for role, content in (
            ("user", "에러코드ab확인"), ("assistant", "AI가 답을 만들었다"),
            ("user", "5G망에서 접속"), ("user", "3D프린터 주문"), ("user", "A1등급 받음"),
            ("user", "about the database"), ("user", "the table is stable"),
            ("user", "I said maintain the email"), ("user", "pcap file and dbx"),
            ("user", "코드1ab확인과 끝ab2확인"), ("user", "요금15G망"),
            ("tool", "AI가 계산을 끝냈다"),
        ):
            store.messages.append(sid, NewMessage(role=role, content=content))

    connection = open_persistence_reader(tmp_path / "state.db") if read_only else open_persistence(scope)
    with connection as store:
        for query, expected in (
            ("ab", [("user", "에러코드ab확인")]),
            ("ai", [("assistant", "AI가 답을 만들었다")]),
            ('"AI"', [("assistant", "AI가 답을 만들었다")]),
            ("5G", [("user", "5G망에서 접속")]),
            ("3D", [("user", "3D프린터 주문")]),
            ("A1", [("user", "A1등급 받음")]),
            ("PC", []), ("DB", []), ("x", []),
        ):
            hits = store.search.search_messages(query, limit=2)
            assert [(hit.role, hit.snippet) for hit in hits] == expected, query
