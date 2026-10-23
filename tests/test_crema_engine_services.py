"""Crema's services (tools/crema_services.py): one way in for Settings and the agent, kept in step with the config list."""
import json
import threading

import pytest

from hermes_cli.config import load_config, save_config
from hermes_constants import get_hermes_home
from tools import crema_services


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

    crema_services.sync_services()

    assert reconciled == [True]
    assert sorted(path.name for path in tokens.iterdir()) == ["notion.client.json", "notion.json"]


def test_a_retry_delay_from_an_earlier_failed_try_does_not_hold_the_new_connection(monkeypatch):
    from tools.mcp_tool_common import _core

    _core._server_connect_retry_after["notion"] = float("inf")
    monkeypatch.setattr("tools.mcp_tool_discovery.reconcile_mcp_servers_with_config", lambda: {})

    crema_services.sync_services()

    assert "notion" not in _core._server_connect_retry_after


@pytest.fixture
def engine(monkeypatch):
    """No network, no browser, no live reconcile: what start() asked of the settings API and the browser."""
    config = load_config()
    config.pop("mcp_servers", None)
    save_config(config)
    seen = {"api": [], "opened": [], "synced": 0}
    flow = []  # what the settings API's sign-in answers, one per look

    def api(method, path):
        seen["api"].append(f"{method} {path}")
        return {"ok": True} if method == "DELETE" else flow.pop(0)

    def synced():
        seen["synced"] += 1

    monkeypatch.setattr(crema_services, "_api", api)
    monkeypatch.setattr(crema_services, "sync_services", synced)
    monkeypatch.setattr(crema_services.webbrowser, "open", lambda url: seen["opened"].append(url))
    monkeypatch.setattr(crema_services.time, "sleep", lambda _s: None)
    seen["flow"] = flow
    return seen


def finished(started):
    for thread in threading.enumerate():
        if thread.name == f"crema-service-{started['name']}":
            thread.join(5)
    return crema_services.job_state(started["id"])


def test_a_service_that_asks_no_sign_in_is_added_with_its_title_and_attached(engine, monkeypatch):
    monkeypatch.setattr(crema_services, "_asks_sign_in", lambda url: False)

    state = finished(crema_services.start("Higgsfield", "https://mcp.higgsfield.ai/mcp", "힉스필드"))

    assert state["status"] == "done" and engine["api"] == [] and engine["opened"] == [] and engine["synced"] == 1
    assert crema_services.listing() == [
        {"name": "higgsfield", "title": "힉스필드", "url": "https://mcp.higgsfield.ai/mcp", "enabled": True}]


def test_a_sign_in_opens_once_in_the_browser_and_is_followed_until_approved(engine, monkeypatch):
    monkeypatch.setattr(crema_services, "_asks_sign_in", lambda url: True)
    engine["flow"].extend([
        {"flow_id": "f1", "status": "authorization_required", "authorization_url": "https://higgsfield.ai/oauth?x=1"},
        {"flow_id": "f1", "status": "authorization_required", "authorization_url": "https://higgsfield.ai/oauth?x=1"},
        {"flow_id": "f1", "status": "approved"}])

    state = finished(crema_services.start("higgsfield", "https://mcp.higgsfield.ai/mcp"))

    assert state["status"] == "done"
    assert engine["api"] == ["POST /api/mcp/servers/higgsfield/auth", "GET /api/mcp/oauth/flows/f1", "GET /api/mcp/oauth/flows/f1"]
    assert engine["opened"] == ["https://higgsfield.ai/oauth?x=1"]
    assert load_config()["mcp_servers"]["higgsfield"]["auth"] == "oauth"


def test_a_failed_or_cancelled_sign_in_ends_it_and_takes_the_service_off(engine, monkeypatch):
    monkeypatch.setattr(crema_services, "_asks_sign_in", lambda url: True)
    engine["flow"].extend([{"flow_id": "f1", "status": "error", "error": "access_denied"}])

    state = finished(crema_services.start("higgsfield", "https://mcp.higgsfield.ai/mcp"))

    assert state == {"id": state["id"], "name": "higgsfield", "status": "failed", "error": "access_denied"}
    assert engine["api"][-1] == "DELETE /api/mcp/oauth/flows/f1"
    assert "higgsfield" not in (load_config().get("mcp_servers") or {})


@pytest.mark.parametrize("name, url", [("힉스", "https://mcp.higgsfield.ai/mcp"), ("higgsfield", "http://mcp.higgsfield.ai/mcp")])
def test_a_name_or_address_it_cannot_take_is_refused_before_anything_is_added(engine, name, url):
    with pytest.raises(ValueError):
        crema_services.start(name, url)
    assert not load_config().get("mcp_servers")


def test_turned_off_a_service_keeps_its_place_and_is_cut_off(engine, monkeypatch):
    monkeypatch.setattr(crema_services, "_asks_sign_in", lambda url: False)
    finished(crema_services.start("higgsfield", "https://mcp.higgsfield.ai/mcp", "힉스필드"))

    assert crema_services.set_enabled("higgsfield", False)
    assert crema_services.listing()[0]["enabled"] is False and engine["synced"] == 2
    assert crema_services.set_enabled("nothing", True) is False


def test_the_agent_connects_only_after_the_user_approves(engine, monkeypatch):
    monkeypatch.setattr(crema_services, "_asks_sign_in", lambda url: False)
    asked = []
    answer = ["was denied by the user."]
    monkeypatch.setattr("tools.approval_prompt.ask_once", lambda display, description, key: asked.append((display, description)) or answer[0])
    monkeypatch.setattr("tools.mcp_tool_discovery.get_mcp_status",
                        lambda: [{"name": "higgsfield", "status": "connected", "tools": 7}])
    args = {"action": "connect", "name": "higgsfield", "title": "힉스필드", "url": "https://mcp.higgsfield.ai/mcp"}

    refused = json.loads(crema_services.crema_services(args))
    assert "BLOCKED" in refused["error"] and not load_config().get("mcp_servers")

    answer[0] = None
    assert json.loads(crema_services.crema_services(args)) == {"connected": "higgsfield", "status": "connected", "tools": 7}
    assert asked == [("https://mcp.higgsfield.ai/mcp", "힉스필드 연결")] * 2
