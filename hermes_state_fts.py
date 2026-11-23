"""Load Crema's CJK tokenizer for SQLite knowledge indexes and PC imports."""

import logging
import os
import sqlite3
from pathlib import Path

from hermes_constants import get_hermes_home
from sqlite_support import routed_sessions_setting

logger = logging.getLogger(__name__)


def load_fts5_cjk_extension(conn: sqlite3.Connection) -> bool:
    """Register cjk_unicode61 when the extension is installed and enabled."""
    try:
        setting = routed_sessions_setting("cjk_fts", "HERMES_CJK_FTS")
        if str(setting).strip().lower() in ("0", "false", "off", "no"):
            return False

        override = os.getenv("HERMES_FTS5_CJK_SO")
        path = Path(override).expanduser() if override else get_hermes_home() / "lib" / "libfts5_cjk.so"
        if not path.exists():
            return False
        conn.enable_load_extension(True)
        try:
            conn.load_extension(str(path))
        finally:
            conn.enable_load_extension(False)
    except Exception:  # health: allow BLE001 -- the optional tokenizer must fail open
        logger.warning("fts5_cjk extension load failed", exc_info=True)
        return False
    return True
