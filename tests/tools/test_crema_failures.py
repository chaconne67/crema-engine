"""The engine's own failures for 오류 정보 보내기: where and what kind, never what was said or worked on."""
import logging

import pytest

from tools import crema_failures


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(crema_failures, "_waiting", [])
    monkeypatch.setattr(crema_failures, "_seen", set())
    handler = crema_failures._Handler(logging.ERROR)
    logging.getLogger().addHandler(handler)
    yield
    logging.getLogger().removeHandler(handler)


def _bash_missing():
    raise RuntimeError("Git Bash not found. C:\\Users\\김철수\\secret.txt")


def test_an_error_logged_while_handling_an_exception_keeps_only_where_and_what_kind():
    try:
        _bash_missing()
    except RuntimeError:
        logging.getLogger("tools.terminal_tool").error("terminal_tool exception: %s", "rm -rf ~/내 문서")
    taken = crema_failures.take()
    assert taken["os"]
    [failure] = taken["failures"]
    assert (failure["stage"], failure["code"]) == ("tools.terminal_tool", "RuntimeError")
    assert failure["details"]["trace"][-1].startswith("tests/tools/test_crema_failures.py:")
    assert failure["details"]["trace"][-1].endswith(" _bash_missing")
    assert "김철수" not in str(taken) and "rm -rf" not in str(taken) and "Git Bash" not in str(taken)
    assert crema_failures.take()["failures"] == []  # taken once


def test_the_same_kind_waits_once_and_plain_errors_are_not_failures():
    for _ in range(3):
        try:
            _bash_missing()
        except RuntimeError:
            logging.getLogger("tools.file_operations").exception("read failed")
    logging.getLogger("run_agent").error("a provider answered 429")  # no exception: not a failure of the engine
    logging.getLogger("run_agent").warning("retrying", exc_info=(ValueError, ValueError("x"), None))
    assert [f["stage"] for f in crema_failures.take()["failures"]] == ["tools.file_operations"]


def test_frames_outside_the_engine_are_left_out_and_codes_stay_ascii():
    crema_failures.record("settings", "POST /api/crema/ego/install 502")
    crema_failures.record("설정", "오류")  # nothing ASCII left: nothing kept
    assert crema_failures.take()["failures"] == [
        {"stage": "settings", "code": "POST /api/crema/ego/install 502", "details": {}}]
    try:
        import json
        json.loads("{")
    except ValueError as exc:
        assert all(not line.startswith("json/") for line in crema_failures._frames(exc.__traceback__))


def test_the_moments_the_user_was_let_down_are_kept_by_kind():
    import json

    class Manager:
        _hooks = {}

    crema_failures.observe(Manager)
    hooks = {name: callbacks[-1] for name, callbacks in Manager._hooks.items()}
    hooks["on_session_end"](session_id="s", completed=True, failed=False, interrupted=False)  # a finished answer
    hooks["on_session_end"](completed=False, failed=False, interrupted=True, turn_exit_reason="interrupted")
    hooks["on_session_end"](completed=False, failed=True, interrupted=False,
                            turn_exit_reason="max_iterations_reached(90/90)")
    hooks["post_tool_call"](tool_name="read_file", result='{"content": "x"}', status="ok")
    hooks["post_tool_call"](tool_name="terminal", result=json.dumps({"output": "C:/Users/김철수", "exit_code": 127}),
                            status="error", error_message="bash: 비밀: not found")
    hooks["post_tool_call"](tool_name="mail", result='{"error": "SMTP 535 me@x.kr"}', status="error")
    hooks["post_tool_call"](tool_name="terminal", result='{"user_summary": "주인님이 거절"}', status="error")
    hooks["post_approval_response"](choice="deny")
    hooks["post_approval_response"](choice="timeout")
    hooks["api_request_error"](provider="openrouter", status_code=429, reason="rate_limit", retryable=True,
                               retry_count=1, max_retries=3)
    hooks["api_request_error"](provider="openrouter", status_code=401, reason="auth", retryable=False)
    taken = crema_failures.take()
    assert [(f["stage"], f["code"]) for f in taken["failures"]] == [
        ("turn", "stopped"), ("turn", "max_iterations_reached"), ("tool", "terminal exit 127"), ("tool", "mail"),
        ("approval", "timeout"), ("model", "openrouter auth")]
    assert "김철수" not in str(taken) and "SMTP" not in str(taken)


def test_a_step_that_raised_keeps_its_class_and_where():
    try:
        _bash_missing()
    except RuntimeError as exc:
        crema_failures.failed("connect.service", exc)
    [failure] = crema_failures.take()["failures"]
    assert (failure["stage"], failure["code"]) == ("connect.service", "RuntimeError")
    assert failure["details"]["trace"][-1].endswith(" _bash_missing")


def test_the_agent_records_a_request_crema_fell_short_of_by_kind_only():
    import json

    assert json.loads(crema_failures.crema_unmet({"why": "no_tool", "area": "documents"})) == {"recorded": True}
    assert "error" in json.loads(crema_failures.crema_unmet({"why": "엑셀 수식을 못 고침", "area": "documents"}))
    assert crema_failures.take()["failures"] == [{"stage": "unmet", "code": "no_tool documents", "details": {}}]


def test_crema_offers_the_unmet_tool_directly():
    from tools.tool_search import _DIRECT_SURFACE_TOOLSETS
    from toolsets import TOOLSETS

    assert "crema_unmet" in TOOLSETS["hermes-api-server"]["tools"]
    assert "crema_unmet" in _DIRECT_SURFACE_TOOLSETS
