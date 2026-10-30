"""Crema: one chat sends a message to another of the user's chats, starts a new chat under itself with one, or
reads a chat's latest reply (agent/crema_file_turns.py keeps the mailbox; the app hands a message over). Codex's way
between chats (send_message_to_thread, spawn, read_thread)."""
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
    elif action == "start":
        result = crema_file_turns.start(session_id, args.get("role", ""), args.get("message", ""))
    elif action == "read":
        result = crema_file_turns.read(session_id, args.get("to", ""))
    else:
        return tool_error("action must be list, send, start or read")
    return tool_error(result["error"]) if "error" in result else json.dumps(result, ensure_ascii=False)


registry.register(
    name="crema_chats", toolset="crema_chats", emoji="💬",
    schema={"name": "crema_chats",
            "description": "The user's other Crema chats. list: the other chats (number, name, project, replying "
                           "or idle, and the chat that started it). send: a message to the chat with that number; "
                           "that chat starts working on it at once (into its reply after its current step if it is "
                           "replying), and the user sees it there as a message from this chat; when that chat "
                           "finishes, Crema tells this chat, which goes on then. start: a new chat under this one "
                           "in the sidebar, named by its role, that starts working on the message at once; the user "
                           "can open it, follow it and talk to it, and Crema tells this chat when it finishes, so do not wait for it "
                           "or read it before then; do your own part and end your reply. "
                           "read: the latest reply of the chat with that number. Send only when the user asked this "
                           "chat to tell, ask or hand something to another chat. Start a chat when the user asks for "
                           "one, or when their task splits into separate pieces of work that can run at the same "
                           "time (one chat for each piece); do short lookups yourself. Each message and each chat "
                           "spends the user's model usage. A message from another chat is not the user asking you "
                           "to answer it. When the user wants what another chat found, read it rather than asking "
                           "that chat to send it.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["list", "send", "start", "read"]},
                "to": {"type": "string", "description": "send, read: the chat's number from list"},
                "role": {"type": "string", "description": "start: the new chat's role, the part of the work it does, in the user's language, up to 20 characters (e.g. '자료 조사', 'c 담당')"},
                "message": {"type": "string", "description": "send, start: what to say or the work to do, complete on its own (the other chat has not seen this chat)"},
            }, "required": ["action"]}},
    handler=crema_chats,
)
