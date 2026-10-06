"""Crema: "백업해 줘" — the engine folder (chats, memory, skills, the knowledge notebook, settings) into one .zip
without the API keys and sign-in tokens (hermes_cli.backup with no_secrets), in the user's Documents\\Crema 백업
unless they name a folder. Crema's settings have no 기억 section: memory and its backup are asked for in the chat."""
from __future__ import annotations

import json
import sys
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from tools.registry import registry, tool_error

DOCUMENTS = "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"  # FOLDERID_Documents


def documents_folder() -> Path:
    """Where Windows keeps the user's Documents (a OneDrive move included), else ~/Documents, else home."""
    if sys.platform == "win32":
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        folder_id = GUID.from_buffer_copy(uuid.UUID(DOCUMENTS).bytes_le)
        found = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(found)) == 0:
            try:
                return Path(found.value)
            finally:
                ctypes.windll.ole32.CoTaskMemFree(found)
    documents = Path.home() / "Documents"
    return documents if documents.is_dir() else Path.home()


def crema_backup(args: Dict[str, Any], **_: Any) -> str:
    from hermes_cli.backup import run_backup

    folder = Path(args.get("folder") or documents_folder() / "Crema 백업").expanduser()
    output = folder / f"crema-backup-{datetime.now():%Y%m%d-%H%M%S}.zip"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        complete = run_backup(Namespace(output=str(output), keep=0, no_secrets=True))
    except (OSError, SystemExit) as exc:
        return tool_error(f"backup failed: {exc}")
    return json.dumps({"path": str(output), "complete": bool(complete),
                       "left_out": "API keys and sign-in tokens (connect them again after a restore)"},
                      ensure_ascii=False)


registry.register(
    name="crema_backup", toolset="backup", emoji="💾",
    schema={"name": "crema_backup",
            "description": "Back up everything Crema keeps on this computer — chats, memory, the knowledge notebook, "
                           "skills and settings — into one .zip, without API keys and sign-in tokens. Use it when the "
                           "user asks for a backup. Tell them the file's path; complete=false means some files could "
                           "not be read and the zip holds the rest.",
            "parameters": {"type": "object", "properties": {
                "folder": {"type": "string",
                           "description": "Only when the user names a folder; otherwise Documents\\Crema 백업."}}}},
    handler=crema_backup)
