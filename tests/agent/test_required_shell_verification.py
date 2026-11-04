from unittest.mock import patch
import pytest
from agent import shell_hooks
from hermes_cli.plugins import PluginManager


@pytest.mark.parametrize("code,stdout,timeout", [
    (1, "", False), (0, "", False), (0, "{}", False),
    (0, "garbage", False), (0, '{"action":"allow"}', True),
])
def test_required_verification_failure_is_a_veto(code, stdout, timeout):
    spec = shell_hooks.ShellHookSpec(event="pre_verify", command="guard", fail_closed=True)
    result = shell_hooks._evaluate_result(spec, dict(error=None, timed_out=timeout,
        elapsed_seconds=1, returncode=code, stdout=stdout, stderr=""))
    assert result["action"] == "block"


def test_required_verification_explicit_allow():
    spec = shell_hooks.ShellHookSpec(event="pre_verify", command="guard", fail_closed=True)
    assert shell_hooks._evaluate_result(spec, dict(error=None, timed_out=False,
        elapsed_seconds=1, returncode=0, stdout='{\"action\":\"allow\"}', stderr="")) is None


def test_callback_error_is_not_swallowed_by_dispatcher():
    manager = PluginManager()
    def broken(**kwargs):
        raise RuntimeError("required checker crashed")
    broken.__hermes_fail_closed__ = True
    manager._hooks["pre_verify"] = [broken]
    result = manager.invoke_hook("pre_verify")
    assert result[0]["action"] == "block"


def test_shell_timeout_covers_its_model_not_global_callback_default():
    spec = shell_hooks.ShellHookSpec(event="pre_verify", command="guard", timeout=120, fail_closed=True)
    callback = shell_hooks._make_callback(spec)
    assert callback.__hermes_timeout__ == 125
    assert callback.__hermes_fail_closed__ is True


def test_required_detection_uses_actual_delivery_profile():
    from types import SimpleNamespace
    from hermes_cli import plugins
    required = SimpleNamespace(__hermes_fail_closed__=True)
    active = SimpleNamespace(iter_hook_callbacks=lambda event: (required,))
    with patch.object(plugins, "_delivery_manager", return_value=active):
        assert plugins.has_required_hook("pre_verify") is True
