"""Crema: a step's thinking summary that came mostly in English, put into Korean for the person watching.

Crema shows the model's thinking in the work log as it streams (주인님 2026-11-01). Claude often thinks in
English even when asked to think in the user's language, so a finished step whose words are at least
ENGLISH_SHARE English is translated by the model chosen as 배운 것 정리 모델 (auxiliary.background_review).
"""

import logging
import re

logger = logging.getLogger(__name__)

# 주인님 2026-11-01: "생각 요약의 40% 이상이 영어면 한글로 번역".
ENGLISH_SHARE = 0.4

_HANGUL = re.compile(r"[가-힣]")
_LATIN = re.compile(r"[A-Za-z]")

PROMPT = (
    "You translate an AI agent's thinking summary into Korean for the person who watches the agent work in "
    "Crema. Keep its meaning, its first-person voice and its Markdown. Leave code, commands, file names, "
    "paths, numbers and product names as they are. Reply with the Korean text only."
)


def needs_translation(text: str) -> bool:
    """True when at least ENGLISH_SHARE of the words that carry letters are English: a word with any Hangul
    counts as Korean (``파일을``, ``api.py에서``), one with Latin letters only as English."""
    korean = english = 0
    for word in text.split():
        if _HANGUL.search(word):
            korean += 1
        elif _LATIN.search(word):
            english += 1
    return english > 0 and english / (english + korean) >= ENGLISH_SHARE


def translate(text: str) -> str:
    """The Korean text from 배운 것 정리 모델 (the main model when none is chosen); "" when the call fails,
    so the step keeps its English."""
    from agent.auxiliary_client import call_llm
    from agent.background_review import load_background_review_settings

    _, route = load_background_review_settings()
    try:
        response = call_llm(
            task="crema_thinking", provider=route.get("provider") or None, model=route.get("model") or None,
            messages=[{"role": "system", "content": PROMPT}, {"role": "user", "content": text}],
            max_tokens=4096, timeout=60)
        return (response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.warning("Thinking translation failed (%s): %s", type(exc).__name__, exc)
        return ""
