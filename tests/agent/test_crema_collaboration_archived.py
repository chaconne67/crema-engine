from types import SimpleNamespace

import pytest

from agent import crema_collaboration as collaboration
from agent import crema_file_turns as turns


PARENT = "agent-client-parent"
CHILD = "agent-client-child"
OTHER = "agent-client-other"


@pytest.fixture
def archived_child_board(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(turns, "_api", SimpleNamespace(_run_statuses={}, _ensure_store=lambda: None))
    turns.set_chats([
        {"id": "parent", "session": PARENT, "title": "부모", "project_id": "p1", "collaboration": 1},
        {"id": "child", "session": CHILD, "title": "보관 자식", "project_id": "p1", "collaboration": 1,
         "parent": "parent", "archived": True},
        {"id": "other", "session": OTHER, "title": "다른 대화", "project_id": "p1", "collaboration": 1},
    ])
    turns.set_models([{"model": "review-model", "provider": "test"}])
    return None


def test_archived_child_is_polled_claimed_and_read_by_parent_only(archived_child_board):
    queued = collaboration.create(PARENT, "child", "후속 검토를 해 주세요.",
                                  request_id="follow-up", attempt_id="a1", message_id="m1")
    assert queued["ok"] is True
    work = queued["work"]

    state = turns.state()
    child_row = next(row for row in state["messages"] if row["session_id"] == CHILD)
    received = next(item["work"] for item in child_row["items"] if item.get("work"))
    assert received["request_id"] == "follow-up"
    assert turns.collaborate({"action": "claim", "session_id": CHILD,
                              "request_id": work["request_id"], "attempt_id": work["attempt_id"],
                              "message_id": work["message_id"]})["claimed"] is True

    saved = turns.read(PARENT, "child", "follow-up")
    assert saved["work"]["request_id"] == "follow-up"
    denied = turns.read(OTHER, "child", "follow-up")
    assert "error" in denied
    assert "error" in collaboration.create(OTHER, "child", "무관한 후속 지시")


def test_archived_child_poll_does_not_include_other_chats_work(archived_child_board):
    own = collaboration.create(PARENT, "child", "부모가 맡긴 일", request_id="own", message_id="own-message")["work"]
    other = collaboration.create(OTHER, "parent", "다른 대화의 일", request_id="other", message_id="other-message")["work"]

    state = turns.state()
    child_row = next(row for row in state["messages"] if row["session_id"] == CHILD)
    messages = [item["work"]["message_id"] for item in child_row["items"] if item.get("work")]
    assert own["message_id"] in messages
    assert other["message_id"] not in messages
