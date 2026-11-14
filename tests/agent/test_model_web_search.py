"""The model company's own web search when no search was chosen (Crema, 주인님 2026-11-09).

One answer, ``tools.web_tools.model_search_wanted``, decides it; each transport that hosts its company's search
swaps the client ``web_search`` function for it in place: Anthropic's server tool, Gemini 3's Google Search,
OpenAI Codex's and xAI's built-ins. A search key or a stored web selection keeps the function.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _fn(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": f"{name} tool",
                                             "parameters": {"type": "object", "properties": {}}}}


TOOLS = [_fn("read_file"), _fn("web_search"), _fn("terminal")]
_KEYS = ("TAVILY_API_KEY", "PERPLEXITY_API_KEY", "EXA_API_KEY", "PARALLEL_API_KEY", "KEENABLE_API_KEY",
         "FIRECRAWL_API_KEY", "FIRECRAWL_API_URL", "SEARXNG_URL", "BRAVE_SEARCH_API_KEY")


@pytest.fixture
def no_search_chosen(monkeypatch):
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("tools.web_tools._load_web_config", lambda: {})
    monkeypatch.setattr("tools.web_tools.read_selection", lambda _name: None)
    monkeypatch.setattr("tools.web_tools._is_tool_gateway_ready", lambda: False)


class TestModelSearchWanted:
    def test_nothing_chosen(self, no_search_chosen):
        from tools.web_tools import model_search_wanted
        assert model_search_wanted() is True

    def test_a_search_key_keeps_that_search(self, no_search_chosen, monkeypatch):
        from tools.web_tools import model_search_wanted
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "BSA-test")
        assert model_search_wanted() is False

    def test_a_stored_backend_keeps_that_search(self, no_search_chosen, monkeypatch):
        from tools.web_tools import model_search_wanted
        monkeypatch.setattr("tools.web_tools._load_web_config", lambda: {"search_backend": "tavily"})
        assert model_search_wanted() is False


class TestUsedUpKey:
    """A search key its provider refused as used up hands over to the model's search until it is back."""

    def test_brave_month_used_up_rests_until_its_reset(self):
        from plugins.web._common import rest_seconds
        headers = {"X-RateLimit-Remaining": "1, 0", "X-RateLimit-Reset": "1, 1419704"}
        assert rest_seconds(headers) == 1419704
        assert rest_seconds({"X-RateLimit-Remaining": "0, 1500", "X-RateLimit-Reset": "1, 1419704"}) == 1

    def test_used_up_key_hands_over_then_comes_back(self, no_search_chosen, monkeypatch):
        import tools.web_tools as web_tools
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "BSA-test")
        monkeypatch.setattr(web_tools, "_rested_until", {"brave-free": web_tools.time.time() + 3600})
        assert web_tools.model_search_wanted() is True
        web_tools._rested_until["brave-free"] = web_tools.time.time() - 1
        assert web_tools.model_search_wanted() is False

    def test_refusal_records_the_rest(self, monkeypatch):
        import tools.web_tools as web_tools
        monkeypatch.setattr(web_tools, "_rested_until", {})
        monkeypatch.setattr(web_tools, "_rescue_eligible", lambda _p: False)
        provider = SimpleNamespace(name="brave-free", search=lambda q, n: {
            "success": False, "error": "Brave Search returned HTTP 429", "rest_seconds": 600})
        web_tools._memoized_search(provider, "q-rest-test", 5)
        assert web_tools._rested_until["brave-free"] > web_tools.time.time() + 500


class TestAnthropicSwap:
    def _tools(self, wanted: bool, *, is_oauth: bool = True, base_url=None):
        from agent.anthropic_adapter import build_anthropic_kwargs
        with patch("tools.web_tools.model_search_wanted", return_value=wanted):
            kwargs = build_anthropic_kwargs(model="claude-haiku-5-5", messages=[{"role": "user", "content": "hi"}],
                                            tools=TOOLS, max_tokens=1000, reasoning_config=None, is_oauth=is_oauth,
                                            base_url=base_url)
        return kwargs["tools"]

    def test_server_search_in_place_keeps_its_name_on_the_oauth_wire(self):
        tools = self._tools(True)
        assert tools[1] == {"type": "web_search_20250305", "name": "web_search"}
        assert [t["name"] for t in tools] == ["mcp__read_file", "web_search", "mcp__terminal"]

    def test_function_kept_when_a_search_was_chosen(self):
        tools = self._tools(False)
        assert all("type" not in t for t in tools)
        assert "mcp__web_search" in [t["name"] for t in tools]

    def test_third_party_endpoint_keeps_the_function(self):
        tools = self._tools(True, is_oauth=False, base_url="https://api.minimax.io/anthropic")
        assert all("type" not in t for t in tools)


class TestAnthropicSearchBlocksReplayInOrder:
    def test_search_then_tool_call_keeps_every_block_in_order(self):
        from agent.transports.anthropic import AnthropicTransport
        blocks = [
            SimpleNamespace(type="server_tool_use", id="srv_1", name="web_search", input={"query": "기준금리"}),
            SimpleNamespace(type="web_search_tool_result", tool_use_id="srv_1",
                            content=[{"type": "web_search_result", "url": "https://bok.or.kr", "title": "BOK",
                                      "encrypted_content": "e", "page_age": None}]),
            SimpleNamespace(type="tool_use", id="tu_1", name="read_file", input={"path": "a.txt"}),
        ]
        result = AnthropicTransport().normalize_response(SimpleNamespace(content=blocks, stop_reason="tool_use"))
        ordered = result.provider_data["anthropic_content_blocks"]
        assert [b["type"] for b in ordered] == ["server_tool_use", "web_search_tool_result", "tool_use"]
        assert ordered[1]["content"][0]["url"] == "https://bok.or.kr"
        assert [c.name for c in result.tool_calls] == ["read_file"]

    @pytest.mark.parametrize("tail", [
        SimpleNamespace(type="tool_use", id="tu_2", name="read_file", input={"path": "b.txt"}),
        SimpleNamespace(type="text", text="Soniox는 시간당 0.1달러입니다.", citations=None),
    ])
    def test_results_of_searches_started_in_the_previous_response_are_kept(self, tail):
        """Seen 2026-11-11, 11-13, 11-14: Claude called a tool and started searches in one response; their results
        opened the next response, and replaying that response without them made Anthropic refuse every later
        request ("tool use ... without a corresponding web_search_tool_result block")."""
        from agent.transports.anthropic import AnthropicTransport
        blocks = [
            SimpleNamespace(type="web_search_tool_result", tool_use_id="srv_1",
                            content=[{"type": "web_search_result", "url": "https://soniox.com/pricing", "title": "Soniox",
                                      "encrypted_content": "e", "page_age": None}]),
            tail,
        ]
        stop = "tool_use" if tail.type == "tool_use" else "end_turn"
        result = AnthropicTransport().normalize_response(SimpleNamespace(content=blocks, stop_reason=stop))
        ordered = result.provider_data["anthropic_content_blocks"]
        assert [b["type"] for b in ordered] == ["web_search_tool_result", tail.type]
        assert ordered[0]["tool_use_id"] == "srv_1"
        # What later turns read of it: the pages, not a failed search.
        assert result.provider_data["web_search_note"] == (
            "[Pages from the web search I started in my previous step: Soniox <https://soniox.com/pricing>]")

    def test_a_search_whose_pages_come_in_the_next_step_is_not_written_as_failed(self):
        """Seen 2026-11-14: noted as "failed: no result", Claude took its own search for failed and searched again."""
        from agent.transports.anthropic import AnthropicTransport
        blocks = [
            SimpleNamespace(type="tool_use", id="tu_1", name="terminal", input={"command": "date"}),
            SimpleNamespace(type="server_tool_use", id="srv_1", name="web_search", input={"query": "Soniox pricing"}),
        ]
        result = AnthropicTransport().normalize_response(SimpleNamespace(content=blocks, stop_reason="tool_use"))
        assert result.provider_data["web_search_note"] == (
            "[I searched the web for 'Soniox pricing' — its pages came with my next step]")

    def test_search_answer_keeps_its_query_and_sources_for_later_turns(self):
        """Seen 2026-11-09: with no trace of its search in the replayed history, Claude took back its answer."""
        from agent.chat_completion_helpers import build_assistant_message
        from agent.transports.anthropic import AnthropicTransport
        blocks = [
            SimpleNamespace(type="server_tool_use", id="srv_1", name="web_search", input={"query": "원달러 환율"}),
            SimpleNamespace(type="web_search_tool_result", tool_use_id="srv_1",
                            content=[{"type": "web_search_result", "url": "https://news.sbs.co.kr/a", "title": "SBS 뉴스",
                                      "encrypted_content": "e", "page_age": None}]),
            SimpleNamespace(type="text", text="1,347.1원입니다.", citations=[{
                "type": "web_search_result_location", "url": "https://news.sbs.co.kr/a", "title": "SBS 뉴스",
                "encrypted_index": "x", "cited_text": "오후 3시 30분 종가 1,347.1원"}]),
        ]
        result = AnthropicTransport().normalize_response(SimpleNamespace(content=blocks, stop_reason="end_turn"))
        from run_agent import AIAgent
        agent = object.__new__(AIAgent)
        agent.provider, agent.model, agent.base_url, agent.verbose_logging = "anthropic", "claude-haiku-5-5", "", False
        agent.reasoning_callback = agent.stream_delta_callback = agent._stream_callback = None
        msg = build_assistant_message(agent, result, "stop")
        assert msg["content"] == "1,347.1원입니다."
        assert msg["api_content"] == (
            "[I searched the web for '원달러 환율' — SBS 뉴스 <https://news.sbs.co.kr/a>]\n"
            "[Quoted from SBS 뉴스 <https://news.sbs.co.kr/a>: '오후 3시 30분 종가 1,347.1원']\n\n1,347.1원입니다.")
        assert "anthropic_content_blocks" not in (result.provider_data or {})


class TestGeminiSwap:
    def _request(self, wanted: bool, model: str = "gemini-3-flash-preview"):
        from agent.gemini_native_adapter import build_gemini_request
        with patch("tools.web_tools.model_search_wanted", return_value=wanted):
            return build_gemini_request(messages=[{"role": "user", "content": "hi"}], tools=TOOLS, model=model)

    def test_google_search_beside_the_functions(self):
        request = self._request(True)
        names = [d["name"] for d in request["tools"][0]["functionDeclarations"]]
        assert names == ["read_file", "terminal"]
        assert request["tools"][1] == {"googleSearch": {}}
        assert request["toolConfig"]["includeServerSideToolInvocations"] is True

    def test_function_kept_when_a_search_was_chosen(self):
        request = self._request(False)
        assert len(request["tools"]) == 1 and "toolConfig" not in request
        assert "web_search" in [d["name"] for d in request["tools"][0]["functionDeclarations"]]

    def test_gemini_2_keeps_the_function(self):
        request = self._request(True, model="gemini-2.5-flash")
        assert len(request["tools"]) == 1
        assert "web_search" in [d["name"] for d in request["tools"][0]["functionDeclarations"]]


class TestCodexAndXai:
    def test_openai_codex_uses_its_own_search(self, monkeypatch):
        import agent.transports.codex as codex_mod
        monkeypatch.setattr("tools.web_tools.model_search_wanted", lambda: True)
        assert codex_mod._openai_prefers_native_web_search() is True

    def test_xai_uses_its_own_search(self, monkeypatch):
        import agent.transports.codex as codex_mod
        monkeypatch.setattr("tools.web_tools.model_search_wanted", lambda: True)
        assert codex_mod._xai_prefers_native_web_search() is True
