"""Service trial policy must run before a cron job prepares prompts or constructs an agent."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from cron import scheduler


def test_read_only_service_job_never_prepares_prompt(monkeypatch):
    monkeypatch.setenv("SERVICE_DIR", "/service")
    monkeypatch.setitem(sys.modules, "fc_lifecycle", SimpleNamespace(writable=lambda home: False))
    prepare = Mock(side_effect=AssertionError("read-only job reached prompt preparation"))
    monkeypatch.setattr(scheduler, "_prepare_job_prompt", prepare)
    result = scheduler.run_job({"id": "test-only"})
    assert result[0] is False
    assert "이용이 끝나" in result[3]
    prepare.assert_not_called()


def test_active_service_uses_existing_cron_path(monkeypatch):
    monkeypatch.setenv("SERVICE_DIR", "/service")
    monkeypatch.setitem(sys.modules, "fc_lifecycle", SimpleNamespace(writable=lambda home: True))
    expected = (True, "existing output", "ok", None)
    prepare = Mock(return_value=(expected, ""))
    monkeypatch.setattr(scheduler, "_prepare_job_prompt", prepare)
    monkeypatch.setattr("gateway.session_cron.current_execution", lambda: (None, "cron-test", "test-only", None))
    assert scheduler.run_job({"id": "test-only"}) == expected
    prepare.assert_called_once()


def test_pc_cron_does_not_require_service_module(monkeypatch):
    monkeypatch.delenv("SERVICE_DIR", raising=False)
    monkeypatch.setitem(sys.modules, "fc_lifecycle", None)
    expected = (True, "pc output", "ok", None)
    monkeypatch.setattr(scheduler, "_prepare_job_prompt", Mock(return_value=(expected, "")))
    monkeypatch.setattr("gateway.session_cron.current_execution", lambda: (None, "cron-test", "test-only", None))
    assert scheduler.run_job({"id": "test-only"}) == expected
