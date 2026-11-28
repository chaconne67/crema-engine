"""Published Krema skill names and compatibility with installations from before the rename."""
from pathlib import Path
import json
import logging
import re

from utils import atomic_write_bytes, atomic_write_text

logger = logging.getLogger(__name__)

LEGACY_NAMES = {f"crema-{suffix}": f"krema-{suffix}" for suffix in ("guide", "appdesign", "webpage", "slides")}
_CALL_ALIASES = {**LEGACY_NAMES, **{f"crema-{suffix}": f"krema-{suffix}" for suffix in (
    "addon-evaluation", "product-planning", "engine-change", "fc-release", "ui-change-delivery", "error-summary",
)}}
_SKILL_REF = re.compile(r"(?<![A-Za-z0-9_-])(" + "|".join(LEGACY_NAMES) + r")(?![A-Za-z0-9_-])")


def current_name(name: str) -> str:
    """Also accept an old category/name in a saved command or scheduled task."""
    prefix, sep, leaf = name.rpartition("/")
    return prefix + sep + _CALL_ALIASES.get(leaf, leaf)


def _update_references(folder: Path) -> None:
    # Only the skill identifiers in Markdown change. Preserve CRLF, BOM and user edits.
    for path in sorted(folder.rglob("*.md"), key=lambda path: path == folder / "SKILL.md"):
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(folder.resolve()):
            continue
        raw = path.read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        updated = _SKILL_REF.sub(lambda match: LEGACY_NAMES[match[0]], content)
        if updated != content:
            atomic_write_bytes(path, updated.encode())


def migrate_profile(root: Path, manifest: dict, bundled: dict) -> None:
    """Retain deletion markers, user-edited copies and installed optional slides on upgrade."""
    from tools.skills_sync import _matches_origin_hash
    from tools.skill_usage import read_skill_name

    from tools.skills_sync_optional import _read_hub_install_paths
    owned_by_hub = _read_hub_install_paths()
    parents = (root, root / "controlroom", root / ".archive")
    for old, new in LEGACY_NAMES.items():
        for parent in parents:
            if parent.is_symlink():
                continue
            previous, dest = parent / old, parent / new
            if previous.relative_to(root).as_posix() in owned_by_hub:
                continue
            if previous.is_symlink() or dest.is_symlink():
                continue
            if previous.exists():
                if not (previous / "SKILL.md").is_file() or (previous / "SKILL.md").is_symlink():
                    continue
                if read_skill_name(previous / "SKILL.md", "") not in (old, new):
                    continue
                if dest.exists():
                    logger.warning("Kept both skill folders after a name collision: %s and %s", previous, dest)
                    continue
                previous.rename(dest)
            marker = dest / "SKILL.md"
            if marker.is_file() and not marker.is_symlink() and read_skill_name(marker, "") == old:
                # The existing sync replaces a pristine bundled copy using its original hash.
                # Keep its bytes intact until that comparison. Other copies retain their contents.
                pristine = (dest == bundled.get(new) and
                            _matches_origin_hash(dest, manifest.get(old, manifest.get(new, ""))))
                if not pristine:
                    _update_references(dest)
        if old in manifest and not any((parent / old).exists() for parent in parents):
            manifest.setdefault(new, manifest.pop(old))

    usage = root / ".usage.json"
    if usage.is_file() and not usage.is_symlink():
        from tools.skill_usage import skill_file_lock

        with skill_file_lock(usage.with_suffix(".json.lock")):
            try:
                records = json.loads(usage.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as error:
                logger.warning("Could not migrate skill usage at %s: %s", usage, error)
                return
            if isinstance(records, dict):
                moved = False
                for old, new in LEGACY_NAMES.items():
                    if old in records and new not in records and not any(
                        (parent / old).exists() for parent in parents
                    ):
                        records[new] = records.pop(old)
                        moved = True
                if moved:
                    atomic_write_text(usage, json.dumps(records, ensure_ascii=False, indent=2), preserve_mode=True)
