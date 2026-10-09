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
                           "The other chat reads it after its current tool step if it is replying, else when it "
                           "next works; set wake only when it must start on it now. A message from another chat "
                           "says its number: answer with send to that number.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["list", "send"]},
                "to": {"type": "string", "description": "send: the chat's number from list"},
                "message": {"type": "string", "description": "send: what to say, complete on its own"},
                "wake": {"type": "boolean", "description": "send: start the other chat now if it is idle (default false)"},
            }, "required": ["action"]}},
    handler=crema_chats,
)
