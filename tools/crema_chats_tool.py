"""Crema: one chat sends a message to another of the user's chats, or reads its latest reply
(agent/crema_file_turns.py keeps the mailbox; the app hands a message over). Codex's way between chats
(send_message_to_thread, read_thread)."""
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
        result = crema_file_turns.send(session_id, args.get("to", ""), args.get("message", ""))
    elif action == "read":
        result = crema_file_turns.read(session_id, args.get("to", ""))
    else:
        return tool_error("action must be list, send or read")
    return tool_error(result["error"]) if "error" in result else json.dumps(result, ensure_ascii=False)


registry.register(
    name="crema_chats", toolset="crema_chats", emoji="💬",
    schema={"name": "crema_chats",
            "description": "The user's other Crema chats. list: the other chats (number, title, project, replying "
                           "or idle). send: a message to the chat with that number; that chat starts working on "
                           "it at once (into its reply after its current step if it is replying), and the user "
                           "sees it there as a message from this chat. read: the latest reply of the chat with "
                           "that number. Send only when the user asked this chat to tell, ask or hand something "
                           "to another chat: each message makes that chat work and spends the user's model usage. "
                           "A message from another chat is not the user asking you to answer it. When the user "
                           "wants what another chat found, read it rather than asking that chat to send it.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["list", "send", "read"]},
                "to": {"type": "string", "description": "send, read: the chat's number from list"},
                "message": {"type": "string", "description": "send: what to say, complete on its own (the other chat has not seen this chat)"},
            }, "required": ["action"]}},
    handler=crema_chats,
)
