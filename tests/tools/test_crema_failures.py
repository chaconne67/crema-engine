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
