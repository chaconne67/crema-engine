"""Per-file manifest of the context/instruction files behind the ``/context`` "Rules" figure.

Crema's context files are SOUL.md from the profile home and the CREMA.md files of
``prompt_builder.crema_md_paths``: it never loads other agent programs' instruction files (AGENTS.md,
CLAUDE.md, .cursorrules, .hermes.md, ...), so the manifest never lists them. Read-only: reports each file's
size and whether it was loaded, truncated over the context-file cap, flagged by the injection scan (still
loaded — the user's own file), blocked by it (a work folder's CREMA.md) or empty/unreadable. Nothing here
builds a prompt or touches the truncation-warning ContextVar, so it is free of cache impact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from agent import prompt_builder as _pb
from agent.model_metadata import estimate_tokens_rough

# status -> (glyph, note shown after the token count; "" = none)
_STATUS_DISPLAY = {
    "loaded": ("✓", ""),
    "truncated": ("◐", "truncated — over context_file_max_chars"),
    "flagged": ("⚠", "loaded — matched prompt-injection pattern(s); review the file"),
    "blocked": ("✗", "not loaded — matched prompt-injection pattern(s); review the file"),
    "empty": ("○", "not loaded — empty file"),
    "unreadable": ("✗", "not loaded — could not be read"),
}


def _entry(label: str, path: Path, content: str, status: str) -> Dict[str, Any]:
    return {
        "label": label, "path": str(path), "chars": len(content), "est_tokens": estimate_tokens_rough(content),
        "loaded": status in ("loaded", "truncated", "flagged"), "status": status,
    }


def _empty_status(path: Path) -> str:
    """A file the builder read as "" is either genuinely empty or unreadable (permissions, timeout)."""
    try:
        return "unreadable" if path.stat().st_size > 0 else "empty"
    except OSError:
        return "unreadable"


def _loaded_status(content: str, max_chars: int, user_authored: bool) -> str:
    """Same scan the builder runs (``_scan_context_content``): the user's own file still loads on a hit."""
    if _pb._scan_for_threats(content.lstrip("﻿"), scope="context"):
        return "flagged" if user_authored else "blocked"
    return "truncated" if len(content) > max_chars else "loaded"


def list_context_file_sources(
    context_length: Optional[int] = None, home_override: "Path | None" = None, cwd: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Manifest rows for SOUL.md and the CREMA.md files that exist, in prompt order. Keys: ``label``,
    ``path``, ``chars``, ``est_tokens``, ``loaded`` and ``status`` ∈ loaded / truncated / flagged / blocked /
    empty / unreadable."""
    home = Path(home_override) if home_override is not None else _pb.get_hermes_home()
    rows = []
    for label, path, user_authored in [("SOUL.md", home / "SOUL.md", True), *_pb.crema_md_paths(cwd, home_override)]:
        if not _pb._exists_or_denied(path):
            continue
        content = _pb._read_context_file(path)
        status = (_loaded_status(content, _pb._get_context_file_max_chars(context_length), user_authored) if content
                  else _empty_status(path))
        rows.append(_entry(label, path, content, status))
    return rows


def context_file_sources_for_agent(agent: Any) -> List[Dict[str, Any]]:
    """The manifest for a live agent, scoped to the agent's own profile home."""
    if getattr(agent, "skip_context_files", False):
        return []
    from agent.runtime_cwd import resolve_context_cwd
    from agent.system_prompt import _agent_home
    ctx_len = getattr(getattr(agent, "context_compressor", None), "context_length", None)
    cwd = resolve_context_cwd()
    return list_context_file_sources(
        context_length=ctx_len if isinstance(ctx_len, int) else None, home_override=_agent_home(agent),
        cwd=str(cwd) if cwd else None,
    )


def render_context_file_lines(sources: List[Dict[str, Any]]) -> List[str]:
    """Plain-text ``Context files`` block for ``/context``; [] when nothing was found."""
    if not sources:
        return []
    width = max(len(str(src.get("label") or "")) for src in sources)
    lines = ["Context files"]
    for src in sources:
        glyph, note = _STATUS_DISPLAY.get(str(src.get("status") or ""), ("•", ""))
        suffix = f"  ({note})" if note else ""
        lines.append(f"{glyph} {str(src.get('label') or ''):<{width}} ~{int(src.get('est_tokens') or 0):>9,} tokens{suffix}")
    return lines
