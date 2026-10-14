"""Crema: tell Thock a word it misheard in the user's message (Thock plan 잘못-들은-말-배우기, 2026-10-13). Thock counts
the pair like the user's own fix in a field, only when the word is in one of its dictations of the last 30 minutes."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any, Dict

from tools.registry import registry, tool_error


def thock_heard(args: Dict[str, Any], **_: Any) -> str:
    """Sent to the PC's one Thock, through its settings page port and token (~/.voicetype/control.json)."""
    body = json.dumps({"wrong": args.get("wrong", ""), "right": args.get("right", "")}).encode("utf-8")
    try:
        link = json.loads((Path.home() / ".voicetype" / "control.json").read_text(encoding="utf-8"))
        request = urllib.request.Request(f"http://127.0.0.1:{link['port']}/api/heard", body,
                                         {"X-Token": link["token"], "Content-Type": "application/json"})
        return json.dumps({"learned": bool(json.load(urllib.request.urlopen(request, timeout=3))["learned"])})
    except (OSError, ValueError, KeyError, TypeError):  # no Thock, one that ended, signed out, or one without /api/heard
        return tool_error("Thock did not take it (not running here, or too old); carry on")


registry.register(
    name="thock_heard", toolset="thock", emoji="👂",
    schema={"name": "thock_heard",
            "description": "Tell Thock, the voice typing that may have written the user's message, about a word it "
                           "misheard, so it learns to write it right. Call it when context makes clear that a word "
                           "in the user's message is a different, similar-sounding word from the one the user meant, "
                           "once per pair, before you answer. Do not try to tell whether the message was dictated or "
                           "typed: Thock keeps a pair only when the wrong word is in its dictations of the last 30 "
                           "minutes and learns it only when it comes twice, so a pair you skip costs more than one "
                           "you send. Send only mishearings: a typo, spacing or wording you read past is not one, "
                           "since Thock learns how words sound. If Thock does not take it, carry on with the answer.",
            "parameters": {"type": "object", "properties": {
                "wrong": {"type": "string", "description": "the word exactly as written in the message, without a "
                                                           "Korean particle (수옥이 → 수옥)"},
                "right": {"type": "string", "description": "the word the user meant, without a particle"},
            }, "required": ["wrong", "right"]}},
    handler=thock_heard,
)
