"""Crema launcher: the services connected in Settings kept in step with the config list."""
import crema_engine
from hermes_cli.config import load_config, save_config
from hermes_constants import get_hermes_home


def test_a_service_taken_off_the_list_is_cut_off_and_its_sign_in_forgotten(monkeypatch):
    config = load_config()
    config["mcp_servers"] = {"notion": {"url": "https://mcp.notion.com/mcp", "auth": "oauth"}}
    save_config(config)
    tokens = get_hermes_home() / "mcp-tokens"
    tokens.mkdir(parents=True, exist_ok=True)
    for name in ("notion.json", "notion.client.json", "linear.json", "linear.client.json", "linear.meta.json"):
        (tokens / name).write_text("{}", encoding="utf-8")
    reconciled = []
    monkeypatch.setattr("tools.mcp_tool_discovery.reconcile_mcp_servers_with_config", lambda: reconciled.append(True) or {})

    crema_engine.sync_services()

    assert reconciled == [True]
    assert sorted(path.name for path in tokens.iterdir()) == ["notion.client.json", "notion.json"]


def test_a_retry_delay_from_an_earlier_failed_try_does_not_hold_the_new_connection(monkeypatch):
    from tools.mcp_tool_common import _core

    _core._server_connect_retry_after["notion"] = float("inf")
    monkeypatch.setattr("tools.mcp_tool_discovery.reconcile_mcp_servers_with_config", lambda: {})

    crema_engine.sync_services()

    assert "notion" not in _core._server_connect_retry_after
