"""Gemini subscription transport over an account's existing Antigravity ACP login."""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from agent.acp_openai_bridge import completion_to_stream_chunks, render_tool_bridge_sections
from agent.copilot_acp_client import (
    CopilotACPClient, _effective_timeout, _model_selection_request, _render_message_content,
    _session_model_ids, _INITIALIZE_PARAMS, _ROLE_LABELS,
)
from agent.message_sanitization import _sanitize_surrogates
from hermes_constants import get_hermes_home

log = logging.getLogger(__name__)

_MAX_SESSIONS = 8
_IDLE_SECONDS = 300
_PREAMBLE = (
    "Hermes is hosting this conversation and executes the tools listed here. "
    "When you need a tool, return a tool-call block as message text for Hermes to execute. "
    "The built-in Antigravity tools cannot perform actions in this conversation. "
    "Hermes sends each result in a Tool message with the call ID. "
    "When no tool is needed, return your answer as message text."
)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _fingerprint(message):
    if message.get("role") == "assistant" and message.get("tool_calls"):
        calls = []
        for call in message["tool_calls"]:
            function = call["function"]
            arguments = function["arguments"]
            try:
                # Hermes decodes and re-encodes the model's JSON arguments before replay.
                arguments = json.loads(arguments)
            except (ValueError, TypeError):
                pass
            calls.append({**call, "function": {**function, "arguments": arguments}})
        message = {**message, "tool_calls": calls}
    return hashlib.sha256(_json(message).encode("ascii")).digest()


def _transcript(messages):
    entries = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") not in (None, "text") for part in content
        ):
            raise ValueError("Google Gemini 구독에서 이 첨부 형식을 지원하지 않습니다.")
        label = _ROLE_LABELS.get(message.get("role"), "Context")
        if "tool_call_id" in message:
            label += f" (call {message['tool_call_id']})"
        body = _render_message_content(content)
        if calls := message.get("tool_calls"):
            body = f"{body}\n{_json(calls)}".strip()
        entries.append(f"{label}:\n{body}")
    return "\n\n".join(entries)


def _usage(result):
    usage = result.get("usage")
    if not isinstance(usage, dict):
        raise RuntimeError("Google Gemini 구독이 호출 사용량을 돌려주지 않았습니다.")
    uncached, cached, output, thoughts = (
        usage.get("inputTokens"), usage.get("cachedReadTokens", 0),
        usage.get("outputTokens"), usage.get("thoughtTokens", 0),
    )
    if any(type(value) is not int or value < 0 for value in (uncached, cached, output, thoughts)):
        raise RuntimeError("Google Gemini 구독이 올바른 호출 사용량을 돌려주지 않았습니다.")
    # Antigravity includes thoughts in outputTokens. Hermes' prompt total includes cache reads.
    return SimpleNamespace(
        prompt_tokens=uncached + cached, completion_tokens=output,
        total_tokens=uncached + cached + output,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=thoughts),
    )


@dataclass(eq=False)
class _Session:
    conversation: tuple | None
    descriptor: tuple
    stack: ExitStack = field(default_factory=ExitStack)
    prefix: tuple = ()
    session_id: str = ""
    request: Callable | None = None
    wire: CopilotACPClient | None = None
    idle_since: float = 0
    timer: threading.Timer | None = None

    def close(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None
        self.stack.close()


_lock = threading.Lock()
_sessions: set[_Session] = set()


def _expire(entry, timer):
    with _lock:
        if entry.timer is timer and entry in _sessions:
            entry.close()
            _sessions.remove(entry)


@atexit.register
def close_gemini_sessions():
    with _lock:
        for entry in _sessions:
            if entry.wire is not None:
                entry.wire.close()
            # Idle stacks have no active owner. Busy owners unwind their own contexts.
            if entry.timer is not None:
                entry.close()
        _sessions.clear()


def reset_gemini_session(profile, session_id=None):
    with _lock:
        for entry in tuple(_sessions):
            if (entry.conversation is None or entry.conversation[0] != profile
                    or (session_id is not None and entry.conversation[1] != session_id)):
                continue
            if entry.timer is None:
                # An in-flight request can finish, but its previous transcript is retired.
                entry.conversation = None
            else:
                entry.close()
                _sessions.remove(entry)


def record_gemini_display_trim(session_id: str, original: str, stored: str) -> None:
    """Match the display-trimmed reply that Crema will replay for this ACP turn."""
    conversation = (str(get_hermes_home()), session_id)
    original_digest = _fingerprint({"role": "assistant", "content": original})
    with _lock:
        for entry in _sessions:
            if (entry.conversation == conversation and entry.timer is not None
                    and entry.prefix[-1:] == (original_digest,)):
                entry.prefix = (*entry.prefix[:-1], _fingerprint({"role": "assistant", "content": stored}))
                break


def _lease(conversation, descriptor, messages):
    with _lock:
        if conversation is not None:
            for entry in tuple(_sessions):
                if entry.conversation != conversation:
                    continue
                if entry.timer is None:
                    # Keep an overlapping call off the first call's session and history.
                    conversation = None
                    break
                if (entry.descriptor == descriptor and len(entry.prefix) < len(messages)
                        and messages[:len(entry.prefix)] == entry.prefix
                        and time.monotonic() - entry.idle_since < _IDLE_SECONDS
                        and any(proc.poll() is None for proc in entry.wire._active_processes)):
                    entry.timer.cancel()
                    entry.timer = None
                    return entry
                entry.close()
                _sessions.remove(entry)
        if len(_sessions) >= _MAX_SESSIONS:
            oldest = min((entry for entry in _sessions if entry.timer is not None),
                         key=lambda entry: entry.idle_since, default=None)
            if oldest is None:
                raise RuntimeError("Google Gemini 구독이 여러 요청에 응답 중입니다. 잠시 뒤 다시 시도해 주세요.")
            oldest.close()
            _sessions.remove(oldest)
        entry = _Session(conversation, descriptor)
        _sessions.add(entry)
        return entry


def _release(entry, prefix):
    with _lock:
        if entry not in _sessions:
            entry.close()
            return
        if prefix and entry.conversation is not None:
            entry.prefix = prefix
            entry.idle_since = time.monotonic()
            timer = threading.Timer(_IDLE_SECONDS, lambda: _expire(entry, timer))
            timer.daemon = True
            entry.timer = timer
            timer.start()
        else:
            entry.close()
            _sessions.remove(entry)


class _GeminiWire(CopilotACPClient):
    _label = "Google Gemini 구독"
    _inherit_credentials = False
    _initialize_params = {**_INITIALIZE_PARAMS, "clientCapabilities": {}}
    _file_access_unavailable = "Hermes executes its own tools."


def _open_wire(entry, command, args, timeout, *, system_prompt=None):
    def session_metadata(initialized):
        capabilities = initialized.get("agentCapabilities", {}).get("_meta", {}).get("antigravity", {})
        if capabilities.get("systemPrompt", {}).get("append") is not True:
            raise RuntimeError("Antigravity ACP does not support systemPrompt.append")
        return {"antigravity": {"systemPrompt": {"append": system_prompt}}}

    cwd = entry.stack.enter_context(TemporaryDirectory(prefix="crema-agy-"))
    entry.wire = _GeminiWire(command=command, args=args, acp_cwd=cwd)
    session, entry.request = entry.stack.enter_context(
        entry.wire._session(min(timeout, 15.0), allow_file_requests=False,
                            session_metadata=session_metadata if system_prompt else None))
    entry.session_id = session["sessionId"]
    # An agy installation may have accept-edits as a default. Keep ACP approvals
    # explicit so the bridge's permission denial applies.
    for option in session.get("configOptions") or ():
        if option.get("category") == "mode" and option.get("currentValue") != "default":
            entry.request("session/set_config_option", {
                "sessionId": entry.session_id, "configId": option["id"], "value": "default",
            })
            break
    return session


def probe_models(command, args, timeout):
    entry = _lease(None, (), ())
    try:
        return _session_model_ids(_open_wire(entry, command, args, timeout))
    finally:
        _release(entry, ())


class GeminiACPClient(CopilotACPClient):
    """A request client leases a conversation-scoped ACP process from the engine."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._requests = set()

    def close(self):
        with self._active_process_lock:
            self.is_closed = True
            active = tuple(entry.wire for entry in self._requests if entry.wire is not None)
        for wire in active:
            wire.close()

    def _create_chat_completion(
        self, *, model=None, messages=None, timeout=None, tools=None, tool_choice=None,
        stream=False, crema_session_id=None, crema_run_instructions=None, **_,
    ):
        messages = messages or []
        fingerprints = tuple(_fingerprint(message) for message in messages)
        conversation = (str(get_hermes_home()), crema_session_id) if crema_session_id else None
        instructions = _sanitize_surrogates((crema_run_instructions or "").rstrip())
        instructions_digest = hashlib.sha256(instructions.encode()).digest()
        descriptor = (self._acp_command, tuple(self._acp_args), model, _json([tools, tool_choice]), instructions_digest)
        entry = _lease(conversation, descriptor, fingerprints)
        new_prefix = ()
        timeout_seconds = _effective_timeout(timeout)
        deadline = time.monotonic() + timeout_seconds
        try:
            with self._active_process_lock:
                if self.is_closed:
                    raise RuntimeError("Google Gemini 구독 요청이 닫혔습니다.")
                self._requests.add(entry)
            if entry.request is None:
                prompt_messages = messages
                if instructions:
                    # The main loop appends run instructions to the system row.
                    # Sending the entire Hermes system row as ACP metadata can
                    # trigger an upstream 429, so move only the instructions.
                    first = messages[0] if messages else {}
                    content = first.get("content") if first.get("role") == "system" else None
                    suffix = "\n\n" + instructions
                    if content == instructions.lstrip():
                        prompt_messages = messages[1:]
                        instructions = instructions.lstrip()
                    elif isinstance(content, str) and content.endswith(suffix):
                        prompt_messages = [
                            {**first, "content": content.removesuffix(suffix).rstrip()}, *messages[1:]
                        ]
                    else:
                        # A context engine can replace the outgoing system row.
                        # Forward only the context it selected for this call.
                        instructions = ""
                tool_sections = render_tool_bridge_sections(tools, tool_choice, arguments_as_object=True)
                session = _open_wire(entry, self._acp_command, self._acp_args, timeout_seconds,
                                     system_prompt=instructions)
                if self.is_closed:
                    raise RuntimeError("Google Gemini 구독 요청이 닫혔습니다.")
                if (selection := _model_selection_request(session, model)) is None:
                    raise RuntimeError(f"Google Gemini 구독에서 {model} 모델을 사용할 수 없습니다.")
                entry.request(*selection, deadline=deadline)
                prompt = "\n\n".join([_PREAMBLE, *tool_sections, _transcript(prompt_messages)])
            else:
                prompt = _transcript(messages[len(entry.prefix):])
            text, reasoning = [], []
            result = entry.request(
                "session/prompt", {"sessionId": entry.session_id, "prompt": [{"type": "text", "text": prompt}]},
                text_parts=text, reasoning_parts=reasoning, deadline=deadline,
            )
            stop = result.get("stopReason") if isinstance(result, dict) else None
            if stop not in ("end_turn", "max_tokens"):
                raise RuntimeError("Google Gemini 구독 응답이 끝나지 않았습니다.")
            usage = _usage(result)
            completion = self._completion("".join(text), "".join(reasoning), model, False, usage=usage,
                                          finish_reason="length" if stop == "max_tokens" else None)
            message = completion.choices[0].message
            reply = {"role": "assistant", "content": message.content}
            if message.tool_calls:
                reply["tool_calls"] = [
                    {"id": call.id, "type": call.type, "function": call.function.model_dump()}
                    for call in message.tool_calls
                ]
            if stop == "end_turn":
                new_prefix = (*fingerprints, _fingerprint(reply))
            log.info("Gemini ACP session=%s reused=%s input=%s cached=%s output=%s",
                     hashlib.sha256(entry.session_id.encode()).hexdigest()[:12], bool(entry.prefix),
                     usage.prompt_tokens - usage.prompt_tokens_details.cached_tokens,
                     usage.prompt_tokens_details.cached_tokens, usage.completion_tokens)
            return completion_to_stream_chunks(completion) if stream else completion
        finally:
            with self._active_process_lock:
                self._requests.discard(entry)
                if self.is_closed:
                    new_prefix = ()
            _release(entry, new_prefix)
