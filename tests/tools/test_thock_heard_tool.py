"""Crema: words Thock misheard, told to Thock through the thock_heard tool (tools/thock_heard_tool.py)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tools.registry import registry
from toolsets import resolve_toolset


@pytest.fixture
def thock():
    """A Thock settings server that takes one token and learns what it is sent; `status` answers otherwise."""
    heard = {"posts": [], "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            heard["posts"].append((self.path, self.headers["X-Token"], body))
            self.send_response(heard["status"])
            self.end_headers()
            self.wfile.write(json.dumps({"learned": True}).encode())

        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    heard["port"] = server.server_address[1]
    yield heard
    server.shutdown()


def call(wrong="수옥", right="쏙"):
    import tools.thock_heard_tool  # noqa: F401 — registers the tool

    return json.loads(registry.dispatch("thock_heard", {"wrong": wrong, "right": right}))


def test_the_built_in_thock_hears_it_first(thock, tmp_path, monkeypatch):
    (tmp_path / "embedded.json").write_text(json.dumps({"port": thock["port"], "token": "built-in"}))
    home = tmp_path / "home"
    (home / ".voicetype").mkdir(parents=True)
    (home / ".voicetype" / "control.json").write_text(json.dumps({"port": 1, "token": "own"}))
    monkeypatch.setenv("CREMA_THOCK_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(home))
    assert call() == {"learned": True}
    assert thock["posts"] == [("/api/heard", "built-in", {"wrong": "수옥", "right": "쏙"})]


def test_a_thock_running_on_its_own_when_the_built_in_one_does_not_answer(thock, tmp_path, monkeypatch):
    (tmp_path / "embedded.json").write_text(json.dumps({"port": 1, "token": "built-in"}))  # left by one that ended
    home = tmp_path / "home"
    (home / ".voicetype").mkdir(parents=True)
    (home / ".voicetype" / "control.json").write_text(json.dumps({"port": thock["port"], "token": "own"}))
    monkeypatch.setenv("CREMA_THOCK_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(home))
    assert call() == {"learned": True}
    assert [post[1] for post in thock["posts"]] == ["own"]


def test_no_thock_or_one_without_the_route_says_to_carry_on(thock, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CREMA_THOCK_HOME", raising=False)
    assert "error" in call()
    (tmp_path / "embedded.json").write_text(json.dumps({"port": thock["port"], "token": "t"}))
    monkeypatch.setenv("CREMA_THOCK_HOME", str(tmp_path))
    thock["status"] = 404  # a Thock older than /api/heard
    assert "error" in call()


def test_crema_offers_the_tool_in_every_turn():
    from tools import tool_search
    import tools.thock_heard_tool  # noqa: F401 — registers the tool

    assert "thock_heard" in resolve_toolset("hermes-api-server")
    assert not tool_search.is_deferrable_tool_name("thock_heard", tool_search._DEFAULT_DEFERRED_TOOLS)
