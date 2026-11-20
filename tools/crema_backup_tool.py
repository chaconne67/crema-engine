"""Crema: "백업해 줘" — the engine folder (chats, memory, skills, the knowledge notebook, settings) into one .zip
without the API keys and sign-in tokens (hermes_cli.backup with no_secrets), in the user's Documents\\Crema 백업
unless they name a folder. Crema's settings have no 기억 section: memory and its backup are asked for in the chat."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
import zipfile
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from tools.registry import registry, tool_error

DOCUMENTS = "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"  # FOLDERID_Documents
INCLUDED = {"SOUL.md", "CREMA.md", "knowledge.db", "memory", "memories", "knowledge",
            "sessions", "state.db", "skills", "cron", "config.yaml"}
LEFT_OUT = "API keys and sign-in tokens (connect them again after a restore)"


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

    if os.environ.get("SERVICE_DIR"):
        return _server_backup(args)
    folder = Path(args.get("folder") or documents_folder() / "Crema 백업").expanduser()
    output = folder / f"crema-backup-{datetime.now():%Y%m%d-%H%M%S}.zip"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        complete = run_backup(Namespace(output=str(output), keep=0, no_secrets=True))
    except (OSError, SystemExit) as exc:
        return tool_error(f"backup failed: {exc}")
    return json.dumps({"path": str(output), "complete": bool(complete),
                       "left_out": LEFT_OUT},
                      ensure_ascii=False)


def _server_backup(args):
    from engine_home import HOME, profile_api
    from hermes_cli.backup import BackupInProgressError
    from hermes_cli.config import load_config_readonly
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    if args or home.is_symlink() or home.resolve().parent != (HOME / "profiles").resolve():
        return tool_error("백업은 현재 계정의 자료만 대상으로 합니다.")
    temporary = None
    try:
        identity = profile_api(home, "/api/engine-profile", timeout=5)
        policy = identity.get("serverPolicy") or {}
        if "user_data_export" not in policy.get("features", ()):
            return tool_error("이 계정은 사용자 자료를 내보낼 수 없습니다.")
        uploads = Path(identity["uploads"])
        folder = uploads / ".user-exports"
        if (not uploads.is_absolute() or uploads.resolve() != uploads or folder.is_symlink()
                or str(folder) != load_config_readonly().get("crema_user_export_dir")):
            raise ValueError("invalid profile export folder")
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        identifier = str(uuid.uuid4())
        with tempfile.NamedTemporaryFile(dir=folder, suffix=".tmp", delete=False) as staged:
            temporary = Path(staged.name)
        complete = _write_server_zip(home, temporary)
        with zipfile.ZipFile(temporary) as archive:
            if not archive.namelist() or archive.testzip() is not None:
                raise ValueError("invalid profile export archive")
        temporary.replace(folder / f"{identifier}.zip")
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, BackupInProgressError):
        return tool_error("사용자 자료 ZIP을 만들지 못했습니다.")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return json.dumps({"export_id": identifier, "download_url": f"/api/user-exports/{identifier}",
                       "complete": complete, "left_out": LEFT_OUT}, ensure_ascii=False)


def _write_server_zip(home, output):
    # Reuse the native backup walk, credential redaction and SQLite snapshot writer.
    # Its CLI entry backs up the process root and can collect external provider state.
    from hermes_cli import backup

    errors = []
    with backup._backup_operation_lock(home), zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        entries = []
        for source, relative in backup._iter_backup_files(home, output):
            if (relative.parts[0] not in INCLUDED or source.stat(follow_symlinks=False).st_nlink != 1
                    or relative.name in backup._CREDENTIAL_FILE_NAMES
                    or backup._CREDENTIAL_DIR_NAMES.intersection(relative.parts[:-1])
                    or relative.name.startswith(".env") or source.resolve() != source):
                continue
            if relative.name == "config.yaml":
                cleaned = backup._config_without_credentials(source)
                if cleaned is None:
                    errors.append(relative)
                else:
                    archive.writestr(relative.as_posix(), cleaned)
                continue
            entries.append((source, relative))
        backup._write_zip_entries(archive, entries, output, on_db_failure=errors.append,
                                  on_error=lambda path, error: errors.append(path),
                                  on_progress=lambda count: None, track_bytes=False)
    return not errors


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
