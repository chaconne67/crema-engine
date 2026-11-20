"""The engine's own failures for 오류 정보 보내기: where and what kind, never what was said or worked on."""
import logging
from collections import defaultdict

import pytest

from tools import crema_failures


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(crema_failures, "_waiting", defaultdict(list))
    monkeypatch.setattr(crema_failures, "_seen", defaultdict(set))
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

    on_end, on_tool, on_approval, on_error = crema_failures.observers()
    hooks = {"on_session_end": on_end, "post_tool_call": on_tool,
             "post_approval_response": on_approval, "api_request_error": on_error}
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


def test_unmet_and_native_failures_use_one_profile_queue(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from uuid import UUID
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    monkeypatch.setenv("SERVICE_DIR", str(tmp_path / "service"))
    developer, staff, jabis = (tmp_path / "profiles" / name for name in ("dev", "staff", "jabis"))
    expected = [("unmet", "no_tool files"), ("model", "provider auth"), ("tool", "file exit 7"),
                ("approval", "timeout"), ("turn", "unfinished"), ("settings", "GET 500")]
    for home in (developer, staff, jabis):
        token = set_hermes_home_override(home)
        try:
            crema_failures.crema_unmet({"why": "no_tool", "area": "files"})
            crema_failures._model_failed(provider="provider", reason="auth")
            crema_failures._tool_ended(tool_name="file", status="error", result={"exit_code": 7})
            crema_failures._approval_answered(choice="timeout")
            crema_failures._turn_ended(failed=True)
            crema_failures.record("settings", "GET 500")
            crema_failures.record("settings", "GET 500", ["never sent"])
            assert crema_failures.take() == {"os": "server", "failures": []}
        finally:
            reset_hermes_home_override(token)
    deliveries = {}
    initial = {}

    def report(home, path, row, timeout):
        assert path == "/api/diagnostics/report"
        assert set(row) == {"eventId", "stage", "code"}
        UUID(row["eventId"])
        if home == developer and not initial:
            initial.update(row)
            raise OSError("lost response")
        deliveries.setdefault(home, []).append(row)
        return {"recorded": home != developer}  # developer consent is off in Django

    monkeypatch.setitem(__import__("sys").modules, "engine_home", SimpleNamespace(
        HOME=tmp_path, ProfileAPIError=type("ProfileAPIError", (ValueError,), {}), profile_api=report))
    crema_failures.flush_server()
    assert developer not in deliveries
    crema_failures.flush_server()
    assert deliveries[developer][0] == initial
    assert set(deliveries) == {developer, staff, jabis}
    for rows in deliveries.values():
        assert [(row["stage"], row["code"]) for row in rows] == expected
    assert len({row["eventId"] for rows in deliveries.values() for row in rows}) == 18
