"""Early compaction (주인님 2026-11-04): the summary is written in the background from 85% of the
compression threshold, and the compaction at the threshold uses it instead of making the user wait.

The summary prompt carries no fixed length target: the conversation decides how long it is.
"""

from __future__ import annotations

import copy
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from agent.context_compressor import ContextCompressor

SUMMARY = "## Goal\nEARLY_SUMMARY_TEXT"


def _compressor() -> ContextCompressor:
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        c = ContextCompressor(
            model="test/model", threshold_percent=0.85, protect_first_n=2, protect_last_n=2, quiet_mode=True,
        )
        _ = c.context_length
    return c


def _msgs(n: int, size: int = 60) -> list:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i} " + "x" * size}
        for i in range(n)
    ]


def _resp(text: str):
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = text
    return r


def _contents(messages: list) -> str:
    return "\n".join(str(m.get("content")) for m in messages)


def _wait_done(c: ContextCompressor) -> None:
    job = c._early_compaction
    assert job is not None
    job.thread.join(10)
    assert not job.thread.is_alive()


@pytest.fixture(autouse=True)
def _no_early_summary_left_between_tests():
    from agent.context_compressor import _EARLY_COMPACTIONS

    yield
    _EARLY_COMPACTIONS.clear()


def _reply_compressor(session_id: str) -> ContextCompressor:
    """The compressor of one reply: the gateway builds a new agent, so a new compressor, per reply."""
    c = _compressor()
    c.bind_session_state(None, session_id)
    return c


class TestEarlyCompaction:
    def test_start_returns_while_the_summary_is_still_being_written(self):
        c = _compressor()
        started, release = threading.Event(), threading.Event()

        def slow(**_kw):
            started.set()
            release.wait(10)
            return _resp(SUMMARY)

        with patch("agent.context_compressor.call_llm", side_effect=slow):
            c.start_early_compaction(_msgs(30), current_tokens=80_000)
            assert started.wait(10)
            # Reaching here while the summary call is blocked proves the caller does not wait.
            assert c._early_compaction.thread.is_alive()
            release.set()
            _wait_done(c)

    def test_compaction_uses_the_early_summary_and_keeps_turns_added_since(self):
        c = _compressor()
        calls = []

        def fake(**_kw):
            calls.append(1)
            return _resp(SUMMARY)

        msgs = _msgs(30)
        with patch("agent.context_compressor.call_llm", side_effect=fake):
            c.start_early_compaction(msgs, current_tokens=80_000)
            _wait_done(c)
            live = msgs + [{"role": "user", "content": "LATE_USER"}, {"role": "assistant", "content": "LATE_REPLY"}]
            out = c.compress(live, current_tokens=90_000)
        assert len(calls) == 1
        assert "EARLY_SUMMARY_TEXT" in _contents(out)
        assert [m["content"] for m in out[-2:]] == ["LATE_USER", "LATE_REPLY"]
        assert len(out) < len(live)

    def test_compaction_waits_for_a_running_early_summary_instead_of_writing_another(self):
        c = _compressor()
        calls, started, release = [], threading.Event(), threading.Event()

        def slow(**_kw):
            calls.append(1)
            started.set()
            release.wait(10)
            return _resp(SUMMARY)

        msgs = _msgs(30)
        with patch("agent.context_compressor.call_llm", side_effect=slow):
            c.start_early_compaction(msgs, current_tokens=80_000)
            assert started.wait(10)
            threading.Timer(0.3, release.set).start()
            out = c.compress(msgs + [{"role": "user", "content": "LATE_USER"}], current_tokens=90_000)
        assert len(calls) == 1
        assert "EARLY_SUMMARY_TEXT" in _contents(out)
        assert out[-1]["content"] == "LATE_USER"

    def test_waiting_for_the_early_summary_ends_when_the_host_cancels(self):
        from agent.auxiliary_client import AuxiliaryExplicitCancellation

        c = _compressor()
        started, release, cancelled = threading.Event(), threading.Event(), threading.Event()

        def slow(**_kw):
            started.set()
            release.wait(10)
            return _resp(SUMMARY)

        msgs = _msgs(30)
        with patch("agent.context_compressor.call_llm", side_effect=slow):
            c.start_early_compaction(msgs, current_tokens=80_000)
            assert started.wait(10)
            c._compression_cancelled_check = cancelled.is_set
            threading.Timer(0.3, cancelled.set).start()
            with pytest.raises(AuxiliaryExplicitCancellation):
                c.compress(msgs, current_tokens=90_000)
            release.set()

    def test_failed_early_summary_falls_back_to_a_normal_compaction(self):
        c = _compressor()
        replies = iter([RuntimeError("provider down"), _resp("## Goal\nSECOND_SUMMARY")])

        def fake(**_kw):
            r = next(replies)
            if isinstance(r, Exception):
                raise r
            return r

        msgs = _msgs(30)
        with patch("agent.context_compressor.call_llm", side_effect=fake):
            c.start_early_compaction(msgs, current_tokens=80_000)
            _wait_done(c)
            out = c.compress(msgs, current_tokens=90_000)
        assert "SECOND_SUMMARY" in _contents(out)

    def test_rewritten_transcript_is_not_given_the_early_summary(self):
        c = _compressor()
        calls = []

        def fake(**_kw):
            calls.append(1)
            return _resp(SUMMARY if len(calls) == 1 else "## Goal\nFRESH_SUMMARY")

        msgs = _msgs(30)
        with patch("agent.context_compressor.call_llm", side_effect=fake):
            c.start_early_compaction(msgs, current_tokens=80_000)
            _wait_done(c)
            rewritten = copy.deepcopy(msgs)
            rewritten[5]["content"] = "edited after the summary started"
            out = c.compress(rewritten, current_tokens=90_000)
        assert len(calls) == 2
        assert "FRESH_SUMMARY" in _contents(out)
        assert c._early_compaction is None

    def test_copied_and_pruned_transcript_still_takes_the_early_summary(self):
        # Compaction hands the compressor deep copies, and the proactive prune shortens old tool
        # results in place; neither makes the early summary stale.
        c = _compressor()
        calls = []

        def fake(**_kw):
            calls.append(1)
            return _resp(SUMMARY)

        msgs = _msgs(30)
        msgs[3] = {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]}
        msgs[4] = {"role": "tool", "tool_call_id": "call_1", "content": "file body " * 200}
        with patch("agent.context_compressor.call_llm", side_effect=fake):
            c.start_early_compaction(msgs, current_tokens=80_000)
            _wait_done(c)
            live = copy.deepcopy(msgs)
            live[4]["content"] = "[pruned tool result]"
            live[4]["_db_persisted"] = True
            out = c.compress(live, current_tokens=90_000)
        assert len(calls) == 1
        assert "EARLY_SUMMARY_TEXT" in _contents(out)


class TestEarlySummaryOutlivesTheReply:
    def test_the_next_reply_uses_the_summary_an_earlier_reply_wrote(self):
        calls = []

        def fake(**_kw):
            calls.append(1)
            return _resp(SUMMARY)

        msgs = _msgs(30)
        live = msgs + [{"role": "user", "content": "LATE_USER"}, {"role": "assistant", "content": "LATE_REPLY"}]
        with patch("agent.context_compressor.call_llm", side_effect=fake):
            first = _reply_compressor("S")
            first.start_early_compaction(msgs, current_tokens=80_000)
            _wait_done(first)
            second = _reply_compressor("S")
            second.start_early_compaction(live, current_tokens=82_000)
            out = second.compress(live, current_tokens=90_000)
        assert len(calls) == 1
        assert "EARLY_SUMMARY_TEXT" in _contents(out)
        assert [m["content"] for m in out[-2:]] == ["LATE_USER", "LATE_REPLY"]

    def test_the_next_reply_writes_a_new_summary_after_a_rewrite(self):
        msgs = _msgs(30)
        rewritten = copy.deepcopy(msgs)
        rewritten[5]["content"] = "edited after the summary started"
        with patch("agent.context_compressor.call_llm", return_value=_resp(SUMMARY)) as call:
            first = _reply_compressor("S")
            first.start_early_compaction(msgs, current_tokens=80_000)
            _wait_done(first)
            second = _reply_compressor("S")
            second.start_early_compaction(rewritten, current_tokens=80_000)
            _wait_done(second)
        assert call.call_count == 2

    def test_another_conversation_does_not_get_the_summary(self):
        msgs = _msgs(30)
        with patch("agent.context_compressor.call_llm", return_value=_resp(SUMMARY)):
            first = _reply_compressor("S")
            first.start_early_compaction(msgs, current_tokens=80_000)
            _wait_done(first)
        assert _reply_compressor("OTHER")._early_compaction is None


class TestSummaryLengthIsLeftToTheConversation:
    def test_prompt_names_no_token_target(self):
        c = _compressor()
        captured = {}

        def fake(**kw):
            captured.update(kw)
            return _resp(SUMMARY)

        with patch("agent.context_compressor.call_llm", side_effect=fake):
            c._generate_summary(_msgs(12), focus_topic="the deploy")
        prompt = captured["messages"][0]["content"]
        assert "Target ~" not in prompt
        assert "Spend up to" not in prompt
        assert "token budget" not in prompt


def _tool_defs(*names: str) -> list:
    return [
        {"type": "function", "function": {"name": n, "description": n, "parameters": {"type": "object", "properties": {}}}}
        for n in names
    ]


@pytest.fixture()
def agent():
    from run_agent import AIAgent

    with (
        patch("model_tools.get_tool_definitions", return_value=_tool_defs("web_search")),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
            skip_context_files=True, skip_memory=True, max_iterations=10,
        )
    a.client = MagicMock()
    a._cached_system_prompt = "You are helpful."
    a.compression_enabled = True
    compressor = MagicMock()
    compressor.threshold_tokens = 500_000
    compressor.context_length = 1_000_000
    compressor.awaiting_real_usage_after_compression = False
    compressor.should_compress.return_value = False
    compressor.should_compress_info.return_value = (False, None)
    compressor.prune_tool_results_only.side_effect = lambda messages, **kw: (messages, 0)
    # The real start rule, on the double's threshold.
    compressor.should_start_early_compaction.side_effect = lambda tokens: ContextCompressor.should_start_early_compaction(
        compressor, tokens
    )
    compressor._early_compaction = None
    a.context_compressor = compressor
    return a


def _after_tools(agent, messages):
    from agent.turn_preflight import compress_after_tool_results

    return compress_after_tool_results(
        agent, messages=messages, system_message="system", user_message="go", active_system_prompt="system",
        conversation_history=[], compression_attempts=0, max_compression_attempts=3, effective_task_id=None,
        final_response="", turn_exit_reason=None, current_turn_user_idx=0,
    )


def _db_agent(db):
    """A real AIAgent on the "EARLY" session with a real compressor, as the gateway builds per reply."""
    from run_agent import AIAgent

    a = AIAgent(
        api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1", model="test/model",
        quiet_mode=True, session_db=db, session_id="EARLY", skip_context_files=True, skip_memory=True,
    )
    a._compression_feasibility_checked = True
    c = _compressor()
    c.bind_session_state(db, "EARLY")
    a.context_compressor = c
    return a


@pytest.fixture()
def agent_with_db(tmp_path):
    """A real AIAgent on a real SessionDB with a real compressor: the path every compaction takes
    (AIAgent._compress_context → progress-timeout worker → compress_context → compress)."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("EARLY", source="desktop")
    patches = (
        patch("model_tools.get_tool_definitions", return_value=_tool_defs("web_search")),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    )
    for p in patches:
        p.start()
    try:
        yield _db_agent(db), db
    finally:
        for p in patches:
            p.stop()


def _stoppable_call(started: threading.Event, release: threading.Event, ticks: bool = False):
    """A summary call that, like the real protected provider call, ends on the calling thread's
    explicit-stop source and reports stream progress while it runs."""
    from agent.auxiliary_client import (
        AuxiliaryExplicitCancellation, _capture_aux_cancel_check, _notify_aux_progress,
    )

    def call(**_kw):
        started.set()
        cancel = _capture_aux_cancel_check()
        while not release.wait(0.05):
            if cancel is not None and cancel():
                raise AuxiliaryExplicitCancellation()
            if ticks:
                _notify_aux_progress()
        return _resp(SUMMARY)

    return call


class TestEarlyCompactionStops:
    def test_explicit_stop_ends_the_early_summary_call(self):
        c = _compressor()
        started, release, stop = threading.Event(), threading.Event(), threading.Event()
        with patch("agent.context_compressor.call_llm", side_effect=_stoppable_call(started, release)):
            c.start_early_compaction(_msgs(30), current_tokens=80_000, cancel_event=stop)
            assert started.wait(10)
            stop.set()
            c._early_compaction.thread.join(5)
            assert not c._early_compaction.thread.is_alive()
        assert c._early_compaction.assembly is None
        release.set()

    def test_stopping_the_next_reply_before_the_threshold_ends_the_shared_summary(self):
        started, release = threading.Event(), threading.Event()
        first_stop, second_stop = threading.Event(), threading.Event()
        msgs = _msgs(30)
        with patch("agent.context_compressor.call_llm", side_effect=_stoppable_call(started, release)) as call:
            first = _reply_compressor("S")
            first.start_early_compaction(msgs, current_tokens=80_000, cancel_event=first_stop)
            assert started.wait(10)
            job = first._early_compaction
            second = _reply_compressor("S")
            second.start_early_compaction(msgs + [{"role": "user", "content": "LATE_USER"}], current_tokens=82_000,
                                          cancel_event=second_stop)
            second.start_early_compaction(msgs + [{"role": "user", "content": "LATE_USER"}], current_tokens=83_000,
                                          cancel_event=second_stop)
            assert second._early_compaction is job
            second_stop.set()
            job.thread.join(5)
            alive = job.thread.is_alive()
            release.set()
        assert not alive
        assert call.call_count == 1

    def test_a_rewrite_ends_the_summary_it_replaces(self):
        started, release = threading.Event(), threading.Event()
        msgs = _msgs(30)
        rewritten = copy.deepcopy(msgs)
        rewritten[5]["content"] = "edited after the summary started"
        with patch("agent.context_compressor.call_llm", side_effect=_stoppable_call(started, release)):
            first = _reply_compressor("S")
            first.start_early_compaction(msgs, current_tokens=80_000)
            assert started.wait(10)
            old = first._early_compaction
            _reply_compressor("S").start_early_compaction(rewritten, current_tokens=80_000)
            old.thread.join(5)
            alive = old.thread.is_alive()
            release.set()
        assert not alive

    def test_the_wait_passes_the_summary_progress_to_the_waiting_attempt(self):
        from agent.auxiliary_client import aux_progress_hook

        c = _compressor()
        started, release = threading.Event(), threading.Event()
        seen = []
        msgs = _msgs(30)
        with patch("agent.context_compressor.call_llm", side_effect=_stoppable_call(started, release, ticks=True)):
            c.start_early_compaction(msgs, current_tokens=80_000)
            assert started.wait(10)
            threading.Timer(1.0, release.set).start()
            with aux_progress_hook(lambda: seen.append(1)):
                out = c.compress(msgs, current_tokens=90_000)
        assert seen
        assert "EARLY_SUMMARY_TEXT" in _contents(out)


class TestEarlyCompactionOnTheRealPath:
    def test_agent_compaction_takes_the_early_summary(self, agent_with_db):
        agent, _db = agent_with_db
        calls = []

        def fake(**_kw):
            calls.append(1)
            return _resp(SUMMARY)

        msgs = _msgs(30, 2000)  # long enough that a summary shrinks it
        with patch("agent.context_compressor.call_llm", side_effect=fake):
            agent.context_compressor.start_early_compaction(msgs, current_tokens=80_000)
            _wait_done(agent.context_compressor)
            out, _prompt = agent._compress_context(msgs + [{"role": "user", "content": "LATE_USER"}], "sys",
                                                   approx_tokens=90_000)
        assert len(calls) == 1
        assert "EARLY_SUMMARY_TEXT" in _contents(out)
        assert out[-1]["content"] == "LATE_USER"

    def test_the_next_replys_agent_takes_the_early_summary(self, agent_with_db):
        first, db = agent_with_db
        calls = []

        def fake(**_kw):
            calls.append(1)
            return _resp(SUMMARY)

        msgs = _msgs(30, 2000)
        with patch("agent.context_compressor.call_llm", side_effect=fake):
            first.context_compressor.start_early_compaction(msgs, current_tokens=80_000)
            _wait_done(first.context_compressor)
            second = _db_agent(db)
            out, _prompt = second._compress_context(msgs + [{"role": "user", "content": "LATE_USER"}], "sys",
                                                    approx_tokens=90_000)
        assert len(calls) == 1
        assert "EARLY_SUMMARY_TEXT" in _contents(out)
        assert out[-1]["content"] == "LATE_USER"

    def test_stop_while_the_compaction_waits_returns_promptly(self, agent_with_db):
        agent, _db = agent_with_db
        started, release = threading.Event(), threading.Event()
        msgs = _msgs(30, 2000)
        with patch("agent.context_compressor.call_llm", side_effect=_stoppable_call(started, release)):
            agent.context_compressor.start_early_compaction(
                msgs, current_tokens=80_000, cancel_event=agent._hard_interrupt_requested,
            )
            assert started.wait(10)
            owner = threading.Thread(target=lambda: agent._compress_context(msgs, "sys", approx_tokens=90_000))
            owner.start()
            time.sleep(0.5)
            agent.hard_interrupt()
            owner.join(5)
            alive = owner.is_alive()
            release.set()
            owner.join(10)
        assert not alive

    def test_stopping_the_next_reply_while_it_waits_ends_the_shared_summary(self, agent_with_db):
        first, db = agent_with_db
        started, release = threading.Event(), threading.Event()
        msgs = _msgs(30, 2000)
        with patch("agent.context_compressor.call_llm", side_effect=_stoppable_call(started, release)):
            first.context_compressor.start_early_compaction(
                msgs, current_tokens=80_000, cancel_event=first._hard_interrupt_requested,
            )
            assert started.wait(10)
            job = first.context_compressor._early_compaction
            second = _db_agent(db)
            owner = threading.Thread(target=lambda: second._compress_context(msgs, "sys", approx_tokens=90_000))
            owner.start()
            time.sleep(0.5)
            second.hard_interrupt()
            owner.join(5)
            job.thread.join(5)
            alive = job.thread.is_alive()
            release.set()
            owner.join(10)
        assert not alive

    def test_failed_early_summary_leaves_no_cooldown_and_the_threshold_compaction_runs(self, agent_with_db):
        from agent.conversation_compression import _automatic_compression_gate_blocks

        agent, db = agent_with_db
        replies = iter([RuntimeError("provider down"), _resp("## Goal\nSECOND_SUMMARY")])

        def fake(**_kw):
            r = next(replies)
            if isinstance(r, Exception):
                raise r
            return r

        msgs = _msgs(30, 2000)  # long enough that a summary shrinks it
        with patch("agent.context_compressor.call_llm", side_effect=fake):
            agent.context_compressor.start_early_compaction(msgs, current_tokens=80_000)
            _wait_done(agent.context_compressor)
            assert not db.get_compression_failure_cooldown("EARLY")
            assert not _automatic_compression_gate_blocks(agent, False)
            out, _prompt = agent._compress_context(msgs, "sys", approx_tokens=90_000)
        assert "SECOND_SUMMARY" in _contents(out)


class TestPostToolStartsEarlyCompaction:
    def test_starts_from_85_percent_of_the_threshold(self, agent):
        agent.context_compressor.last_prompt_tokens = 430_000
        messages = [{"role": "user", "content": "go"}]
        with patch("agent.usage_anchor.anchored_context_tokens", return_value=None):
            _after_tools(agent, messages)
        agent.context_compressor.start_early_compaction.assert_called_once()
        assert agent.context_compressor.start_early_compaction.call_args.args[0] is messages
        assert agent.context_compressor.start_early_compaction.call_args.kwargs["cancel_event"] is agent._hard_interrupt_requested

    def test_does_not_start_below_85_percent(self, agent):
        agent.context_compressor.last_prompt_tokens = 300_000
        with patch("agent.usage_anchor.anchored_context_tokens", return_value=None):
            _after_tools(agent, [{"role": "user", "content": "go"}])
        agent.context_compressor.start_early_compaction.assert_not_called()
