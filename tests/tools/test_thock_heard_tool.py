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


def links(home, port, token="own"):
    (home / ".voicetype").mkdir(parents=True, exist_ok=True)
    (home / ".voicetype" / "control.json").write_text(json.dumps({"port": port, "token": token}))


def test_the_pcs_thock_hears_it(thock, tmp_path, monkeypatch):
    links(tmp_path, thock["port"])
    monkeypatch.setenv("HOME", str(tmp_path))
    assert call() == {"learned": True}
    assert thock["posts"] == [("/api/heard", "own", {"wrong": "수옥", "right": "쏙"})]


def test_no_thock_one_that_ended_or_one_without_the_route_says_to_carry_on(thock, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert "error" in call()
    links(tmp_path, 1)  # left by a Thock that ended
    assert "error" in call()
    links(tmp_path, thock["port"])
    thock["status"] = 404  # a Thock older than /api/heard
    assert "error" in call()


def test_crema_offers_the_tool_in_every_turn():
    from tools import tool_search
    import tools.thock_heard_tool  # noqa: F401 — registers the tool

    assert "thock_heard" in resolve_toolset("hermes-api-server")
    assert not tool_search.is_deferrable_tool_name("thock_heard", tool_search._DEFAULT_DEFERRED_TOOLS)
