"""Crema: one chat sends a message to another of the user's chats (agent/crema_file_turns.py keeps the mailbox
and delivers it). Claude Code's way between peer sessions, with Codex's choice of a note only or a note that
starts the other chat."""
from __future__ import annotations

import json
from typing import Any, Dict

from tools.registry import registry, tool_error


def crema_chats(args: Dict[str, Any], session_id: str = "", **_: Any) -> str:
    from agent import crema_file_turns

    action = args.get("action")
    if action == "list":
        return json.dumps({"chats": crema_file_turns.chats(session_id)}, ensure_ascii=False)
    if action == "send":
        result = crema_file_turns.send(session_id, args.get("to", ""), args.get("message", ""), bool(args.get("wake")))
        return tool_error(result["error"]) if "error" in result else json.dumps(result, ensure_ascii=False)
    return tool_error("action must be list or send")


registry.register(
    name="crema_chats", toolset="crema_chats", emoji="💬",
    schema={"name": "crema_chats",
            "description": "Talk with the user's other Crema chats. list: the other chats (number, title, project, "
                           "replying or idle). send: a message to the chat with that number. Use it when the user "
                           "asks to tell or ask another chat something, or this work needs another chat's result. "
                           "A replying chat reads a message after its current tool step; an idle chat reads it only "
                           "when it next works (the user writes to it, or a message wakes it). Your answer to a "
                           "message a chat sent you wakes it on its own, as it waits for the answer; so after "
                           "asking, you need not wait: the answer reaches this chat. For other messages, set wake "
                           "when the other chat should act on it before the user next writes to it (a request or "
                           "a question), and leave it off for information it can read later. A message from "
                           "another chat says its number: answer with send to that number.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["list", "send"]},
                "to": {"type": "string", "description": "send: the chat's number from list"},
                "message": {"type": "string", "description": "send: what to say, complete on its own"},
                "wake": {"type": "boolean", "description": "send: start the other chat now if it is idle (default false; an answer to its message wakes it anyway)"},
            }, "required": ["action"]}},
    handler=crema_chats,
)
