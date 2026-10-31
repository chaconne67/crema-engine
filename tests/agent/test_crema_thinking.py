"""Crema: which thinking summaries are put into Korean, and by which model."""

from types import SimpleNamespace
from unittest.mock import patch

from agent import crema_thinking


def test_a_summary_with_at_least_forty_percent_english_words_is_translated():
    assert crema_thinking.needs_translation("I'm checking whether the process is alive.")
    # 3 English words of 7: 43%.
    assert crema_thinking.needs_translation("Checking the log 로그가 멈춘 이유를 찾습니다")
    # 2 of 7: 29%, and a word with Hangul (`api.py에서`) counts as Korean.
    assert not crema_thinking.needs_translation("`api.py에서` 콜백을 찾았다. callback 이름은 run_cb 이다")
    assert not crema_thinking.needs_translation("로그가 7분째 멈춰 있어 프로세스를 다시 봅니다.")
    assert not crema_thinking.needs_translation("12:30 → 12:45")


def test_translation_uses_the_learning_review_model_and_keeps_english_when_it_fails():
    calls = []

    def call_llm(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=" 로그를 먼저 봅니다. "))])

    route = (True, {"provider": "anthropic", "model": "claude-haiku-5-5"})
    with patch("agent.background_review.load_background_review_settings", return_value=route), \
            patch("agent.auxiliary_client.call_llm", side_effect=call_llm):
        assert crema_thinking.translate("I'll check the log first.") == "로그를 먼저 봅니다."
    assert (calls[0]["provider"], calls[0]["model"]) == ("anthropic", "claude-haiku-5-5")
    assert calls[0]["messages"][1]["content"] == "I'll check the log first."

    with patch("agent.background_review.load_background_review_settings", return_value=(True, {})), \
            patch("agent.auxiliary_client.call_llm", side_effect=RuntimeError("down")):
        assert crema_thinking.translate("I'll check the log first.") == ""
