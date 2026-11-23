"""Server account policy gates both kinds of cron runs before model work."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cron import scheduler


class ProfileAPIError(ValueError):
    policy_denial = "read_only"
    detail = "이용이 끝나 자료를 읽거나 내려받을 수 있습니다."


@pytest.fixture
def account_refresh(monkeypatch):
    refresh = Mock()
    monkeypatch.setenv("SERVICE_DIR", "/service")
    monkeypatch.setitem(sys.modules, "engine_home", SimpleNamespace(
        ProfileAPIError=ProfileAPIError, refresh_account_profile=refresh))
    return refresh


@pytest.mark.parametrize("origin", [None, {"platform": "api_server"}])
def test_read_only_service_job_never_prepares_prompt(account_refresh, monkeypatch, origin):
    account_refresh.side_effect = ProfileAPIError
    prepare = Mock(side_effect=AssertionError("read-only job reached prompt preparation"))
    monkeypatch.setattr(scheduler, "_prepare_job_prompt", prepare)
    monkeypatch.setattr("gateway.session_cron.current_execution", lambda: (None, "cron-test", "test-only", None))
    result = scheduler.run_job({"id": "test-only", "origin": origin})
    assert result == (False, "", "", ProfileAPIError.detail)
    account_refresh.assert_called_once()
    prepare.assert_not_called()


def test_active_service_uses_existing_cron_path(account_refresh, monkeypatch):
    expected = (True, "existing output", "ok", None)
    prepare = Mock(return_value=(expected, ""))
    monkeypatch.setattr(scheduler, "_prepare_job_prompt", prepare)
    monkeypatch.setattr("gateway.session_cron.current_execution", lambda: (None, "cron-test", "test-only", None))
    assert scheduler.run_job({"id": "test-only"}) == expected
    account_refresh.assert_called_once()
    prepare.assert_called_once()


def test_pc_cron_does_not_require_service_module(monkeypatch):
    monkeypatch.delenv("SERVICE_DIR", raising=False)
    monkeypatch.setitem(sys.modules, "engine_home", None)
    expected = (True, "pc output", "ok", None)
    monkeypatch.setattr(scheduler, "_prepare_job_prompt", Mock(return_value=(expected, "")))
    monkeypatch.setattr("gateway.session_cron.current_execution", lambda: (None, "cron-test", "test-only", None))
    assert scheduler.run_job({"id": "test-only"}) == expected
