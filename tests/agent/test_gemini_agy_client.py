import contextlib
import os
import sys

import pytest

from agent import gemini_agy_client as agy
from hermes_cli.auth import AuthError, get_auth_status, resolve_external_process_provider_credentials
from providers import get_provider_profile


TOOL_REPLY = (
    '<tool_call>{"id":"read-1","type":"function","function":'
    '{"name":"read_file","arguments":"{\\"path\\": \\"fixture.txt\\"}"}}</tool_call>'
)


@pytest.fixture
def transport(monkeypatch):
    calls, finished = [], []
    serial = 0

    class Wire(agy._GeminiWire):
        @contextlib.contextmanager
        def _session(self, timeout_seconds, *, allow_file_requests=True, session_metadata=None):
            nonlocal serial
            assert allow_file_requests is False
            metadata = session_metadata({"agentCapabilities": {
                "_meta": {"antigravity": {"systemPrompt": {"append": True}}},
            }}) if session_metadata else None
            system_prompt = metadata["antigravity"]["systemPrompt"]["append"] if metadata else None
            serial += 1
            session_id = f"session-{serial}"
            self._active_processes.add(SimpleProcess())
            try:
                def request(method, params, *, text_parts=None, **kwargs):
                    if method != "session/prompt":
                        return {}
                    prompt = params["prompt"][0]["text"]
                    calls.append((session_id, prompt, system_prompt))
                    text_parts.append(TOOL_REPLY if len(calls) == 1 else "ORCHARD")
                    return {"stopReason": "end_turn", "usage": {
                        "inputTokens": 10, "cachedReadTokens": 25, "outputTokens": 8, "thoughtTokens": 3}}
                yield {"sessionId": session_id, "models": {"availableModels": [
                    {"modelId": "gemini-4-neon"}, {"modelId": "gemini-4-argon"}]}}, request
            finally:
                self._active_processes.clear()
                finished.append(session_id)

    class SimpleProcess:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, *, timeout):
            pass

    monkeypatch.setattr(agy, "_GeminiWire", Wire)
    yield calls, finished
    agy.close_gemini_sessions()


def complete(messages, *, client=None, session="chat-1", **kwargs):
    client = client or agy.GeminiACPClient(command="/fixture/agy", args=["acp"])
    return client.chat.completions.create(model=kwargs.pop("model", "gemini-4-neon"),
                                          messages=messages, crema_session_id=session, **kwargs)


def test_replays_only_tool_result_with_new_client_and_preserves_usage(transport):
    calls, _ = transport
    instructions = "Run reference"
    initial = [{"role": "system", "content": f"Fixture context\n\n{instructions}"},
               {"role": "user", "content": "Read the fixture."}]
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    reply = complete(initial, tools=tools, crema_run_instructions=instructions).choices[0].message
    assert reply.content == ""
    assert reply.tool_calls[0].id == "read-1"
    assert calls[0][1].endswith("System:\nFixture context\n\nUser:\nRead the fixture.")
    assert "Set arguments to a JSON object" in calls[0][1]
    assert instructions not in calls[0][1]
    assert calls[0][2] == instructions
    replay = {"role": "assistant", "content": "", "tool_calls": [{
        "id": "read-1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"fixture.txt"}'},
    }]}
    result = {"role": "tool", "tool_call_id": "read-1", "content": "ORCHARD"}
    chunks = complete([*initial, replay, result], tools=tools, stream=True, crema_run_instructions=instructions)
    assert calls[0][0] == calls[1][0]
    assert calls[1][1] == "Tool (call read-1):\nORCHARD"
    usage = chunks[-1].usage
    assert (usage.prompt_tokens, usage.prompt_tokens_details.cached_tokens, usage.completion_tokens) == (35, 25, 8)
    assert usage.completion_tokens_details.reasoning_tokens == 3


def test_run_instructions_do_not_restore_context_removed_before_send(transport):
    calls, _ = transport
    initial = [{"role": "system", "content": "Selected context"}, {"role": "user", "content": "First"}]
    complete(initial, crema_run_instructions="Original run instructions")
    assert calls[0][1].endswith("System:\nSelected context\n\nUser:\nFirst")
    assert calls[0][2] is None


def test_service_policy_is_sent_once_as_metadata_and_keeps_live_session(transport, monkeypatch):
    calls, _ = transport
    monkeypatch.setitem(globals(), "TOOL_REPLY", "ORCHARD")
    prefix = "Fixture context"
    question = {"role": "user", "content": "Use pc://device/work for this question."}

    def wire(policy, instructions, history):
        section = "\n\n# Service instructions\n\n" + policy
        system = {"role": "system", "content": prefix + section + "\n\n" + instructions}
        complete([system, question, *history], crema_run_instructions=instructions, crema_service_policy={
            "start": len(prefix), "end": len(prefix + section), "section": section,
        })

    wire("Service rule one.", "Response metadata A", [])
    assert calls[0][2].count("Service rule one.") == 1
    assert "Service rule one." not in calls[0][1]
    assert "Response metadata A" in calls[0][1] and "Response metadata A" not in calls[0][2]
    assert calls[0][1].endswith("Use pc://device/work for this question.")

    history = [{"role": "assistant", "content": "ORCHARD", "reasoning_content": "Stored ACP reasoning"},
               {"role": "user", "content": "Follow-up"}]
    wire("Service rule one.", "Response metadata B", history)
    assert calls[0][0] == calls[1][0]
    assert calls[1][1].endswith("User:\nFollow-up")
    wire("Service rule two.", "Response metadata B", history)
    assert calls[2][0] != calls[1][0]
    assert calls[2][2].count("Service rule two.") == 1 and "Service rule one." not in calls[2][2]


def test_large_service_instructions_remain_whole_in_transcript(transport):
    calls, _ = transport
    service = "Service rules.\n" * 500
    complete([{"role": "system", "content": service}, {"role": "user", "content": "Latest"}],
             crema_service_policy={"start": 0, "end": len(service), "section": service})
    assert calls[0][2] == agy._HOST_POLICY
    assert service.rstrip() in calls[0][1]
    assert calls[0][1].endswith("User:\nLatest")


def test_long_transcript_preserves_text_usage_and_session(transport):
    calls, _ = transport
    body = "기록🌳" * 22000
    initial = [{"role": "user", "content": body},
               {"role": "assistant", "content": "Earlier answer"},
               {"role": "user", "content": "Latest question"}]
    completion = complete(initial)

    first_call = calls[0][1]
    last_call = calls[-1][1]
    assert len(calls) == 2
    assert all(len(prompt.encode()) <= 190_000 for _, prompt, _ in calls)
    transcript = first_call.split("\n\n", 1)[1] + last_call.split("\n\n", 1)[1]
    assert transcript.endswith(f"User:\n{body}\n\nAssistant:\nEarlier answer\n\nUser:\nLatest question")
    assert completion.choices[0].message.content == "ORCHARD"
    assert not completion.choices[0].message.tool_calls
    assert completion.usage.prompt_tokens == 70
    assert completion.usage.prompt_tokens_details.cached_tokens == 50
    assert completion.usage.completion_tokens == 16
    assert completion.usage.initial_prompt_tokens == 35

    complete([*initial, {"role": "assistant", "content": "ORCHARD"},
              {"role": "user", "content": "Follow-up"}])
    assert len(calls) == 3
    assert len({session for session, _, _ in calls}) == 1
    assert calls[-1][1] == "User:\nFollow-up"


@pytest.mark.parametrize("change", ["previous_text", "model", "tools", "retry", "profile", "instruction_split"])
def test_changed_conversation_contract_opens_a_new_session(transport, change, monkeypatch):
    calls, finished = transport
    initial = [{"role": "system", "content": "Base context\n\nRun instructions"},
               {"role": "user", "content": "First"}]
    reply = complete(initial).choices[0].message
    replay = [*initial, {"role": "assistant", "content": reply.content, "tool_calls": [
        {"id": call.id, "type": call.type, "function": call.function.model_dump()} for call in reply.tool_calls
    ]}, {"role": "tool", "tool_call_id": "read-1", "content": "ORCHARD"}]
    kwargs = {}
    if change == "previous_text":
        replay[1] = {"role": "user", "content": "Edited"}
    elif change == "model":
        kwargs["model"] = "gemini-4-argon"
    elif change == "tools":
        kwargs["tools"] = [{"type": "function", "function": {"name": "read_file"}}]
    elif change == "retry":
        agy.reset_gemini_session(str(agy.get_hermes_home()), "chat-1")
    elif change == "profile":
        monkeypatch.setattr(agy, "get_hermes_home", lambda: "/different/profile")
    elif change == "instruction_split":
        kwargs["crema_run_instructions"] = "Run instructions"
    complete(replay, **kwargs)
    assert calls[0][0] != calls[1][0]
    assert "User:\n" in calls[1][1]
    assert "Hermes is hosting" in calls[1][1]
    assert calls[1][2] == kwargs.get("crema_run_instructions")
    assert (calls[0][0] in finished) == (change != "profile")


@pytest.mark.parametrize("change", ["reply_markers", "edited_body", "different_chat", "other_transform"])
def test_crema_reply_markers_preserve_only_the_matching_session(transport, change, monkeypatch):
    from crema_engine import drop_next_input

    calls, _ = transport
    original = "ORCHARD\n\nNEXT_INPUT: Continue"
    monkeypatch.setitem(globals(), "TOOL_REPLY", original)
    initial = [{"role": "user", "content": "First"}]
    assert complete(initial).choices[0].message.content == original

    session_id = "different" if change == "different_chat" else "chat-1"
    output = f"Notice\n\n{original}" if change == "other_transform" else original
    stored_reply = drop_next_input(response_text=output, session_id=session_id)
    assert "NEXT_INPUT" not in stored_reply
    if change == "edited_body":
        stored_reply = "RIVER"
    complete([*initial, {"role": "assistant", "content": stored_reply},
              {"role": "user", "content": "Next"}])

    if change == "reply_markers":
        assert calls[0][0] == calls[1][0]
        assert calls[1][1] == "User:\nNext"
    else:
        assert calls[0][0] != calls[1][0]


def test_busy_session_is_not_reused_and_idle_oldest_is_evicted(monkeypatch, transport):
    monkeypatch.setattr(agy, "_MAX_SESSIONS", 2)
    calls, finished = transport
    conversation = (str(agy.get_hermes_home()), "busy")
    entry = agy._lease(conversation, (), ())
    try:
        complete([{"role": "user", "content": "Overlapping"}], session="busy")
        assert finished == ["session-1"]
        complete([{"role": "user", "content": "Old idle"}], session="old")
        complete([{"role": "user", "content": "New idle"}], session="new")
        assert finished == [calls[0][0], calls[1][0]]
    finally:
        agy._release(entry, ())


@pytest.mark.skipif(os.name != "posix", reason="Krema host-only subscription")
def test_subscription_catalog_is_profile_gated_and_loaded_only_after_probe(tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    profile = get_provider_profile("gemini-agy")
    monkeypatch.setenv("HERMES_GEMINI_AGY_COMMAND", sys.executable)
    monkeypatch.setattr(agy, "probe_models", lambda *args, **kwargs: ["gemini-4-neon"])
    token = set_hermes_home_override(str(tmp_path))
    try:
        with pytest.raises(PermissionError):
            profile.connect_subscription()
        with pytest.raises(AuthError, match="Krema 개발자"):
            resolve_external_process_provider_credentials("gemini-agy")
        (tmp_path / ".service-config.yaml").write_text("crema_gemini_subscription: true\n")
        assert not get_auth_status("gemini-agy").get("configured")
        assert profile.connect_subscription() == ["gemini-4-neon"]
        assert get_auth_status("gemini-agy")["configured"]
        assert resolve_external_process_provider_credentials("gemini-agy")["command"] == sys.executable
        profile.disconnect_subscription()
        assert profile.fetch_models() is None
        assert not get_auth_status("gemini-agy").get("configured")
    finally:
        reset_hermes_home_override(token)



@pytest.mark.skipif(os.name != "posix", reason="Krema host-only subscription")
def test_subscription_usage_reads_agy_usage_command(tmp_path, monkeypatch):
    import json
    import subprocess
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from agent.account_usage import fetch_account_usage
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    answer = {"command": {"name": "usage", "data": {"groups": [
        {"name": "Gemini Models", "buckets": [
            {"name": "Weekly Limit Remaining", "window": "weekly", "remaining_fraction": 0.75,
             "reset_time": "2026-11-28T09:59:45Z"},
            {"name": "Five Hour Limit Remaining", "window": "5h", "remaining_fraction": 0.25,
             "reset_time": "2026-11-21T14:59:45Z"}]},
        {"name": "Gemini 4 Argon", "buckets": [
            {"name": "Daily Limit", "window": "daily", "remaining_fraction": 1, "reset_time": "2026-11-22T00:00:00Z"}]},
    ]}}}
    ran = []

    def run(command, **kwargs):
        ran.append(command)
        return SimpleNamespace(stdout=json.dumps(answer))

    monkeypatch.setenv("HERMES_GEMINI_AGY_COMMAND", sys.executable)
    monkeypatch.setattr(subprocess, "run", run)
    token = set_hermes_home_override(str(tmp_path))
    try:
        assert fetch_account_usage("gemini-agy") is None  # not a Krema developer, not connected: agy is not run
        assert ran == []
        (tmp_path / ".service-config.yaml").write_text("crema_gemini_subscription: true\n")
        (tmp_path / "gemini-agy-models.json").write_text('["gemini-4-neon"]')
        windows = fetch_account_usage("gemini-agy").windows
    finally:
        reset_hermes_home_override(token)
    assert ran == [[sys.executable, "-p", "/usage", "--output-format", "json"]]
    assert [(w.label, round(w.used_percent), w.reset_at) for w in windows] == [
        ("주간 한도 · Gemini Models", 25, datetime(2026, 11, 28, 9, 59, 45, tzinfo=timezone.utc)),
        ("5시간 한도 · Gemini Models", 75, datetime(2026, 11, 21, 14, 59, 45, tzinfo=timezone.utc)),
        ("Daily Limit · Gemini 4 Argon", 0, datetime(2026, 11, 22, tzinfo=timezone.utc)),
    ]


def test_expired_idle_entry_is_not_reused_before_timer_runs(transport, monkeypatch):
    calls, finished = transport
    initial = [{"role": "user", "content": "First"}]
    complete(initial, session="throwaway")
    complete(initial)
    monkeypatch.setattr(agy, "_IDLE_SECONDS", 0)
    complete([*initial, {"role": "assistant", "content": "ORCHARD"}, {"role": "user", "content": "Next"}])
    assert calls[1][0] != calls[2][0]
    assert calls[1][0] in finished


@pytest.mark.skipif(os.name != "posix", reason="Krema host-only subscription")
def test_subscription_probe_failure_gives_login_action_and_keeps_catalog_empty(tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    profile = get_provider_profile("gemini-agy")
    monkeypatch.setenv("HERMES_GEMINI_AGY_COMMAND", sys.executable)
    (tmp_path / ".service-config.yaml").write_text("crema_gemini_subscription: true\n")

    def unauthorized(*args, **kwargs):
        raise RuntimeError("ACP: Authentication required")

    monkeypatch.setattr(agy, "probe_models", unauthorized)
    token = set_hermes_home_override(str(tmp_path))
    try:
        with pytest.raises(RuntimeError, match="main 서버에서 agy를 실행해 로그인"):
            profile.connect_subscription()
        assert profile.fetch_models() is None
    finally:
        reset_hermes_home_override(token)
