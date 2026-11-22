"""pm.shell(): the one place Hermes resolves the shell it runs commands with.

Owned by pm because the shell is a bundled tool on Windows (Git for Windows
carries bash.exe), and the store is the authority on whether it exists.
Callers that need bash (the terminal backend, `_find_bash`) call this instead
of hunting fixed locations.

Resolution order:
  1. Windows: the git Package's staged bash (via facts.json) — the store
     structurally guarantees it in a bundle; no hunt.
  2. Windows: explicit override, Program Files, per-user and PortableGit,
     then PATH —
     minus C:\\Windows\\System32\bash.exe (the WSL launcher stub, first on PATH
     on most machines; #116818) and WindowsApps\bash.exe (an MSIX alias that
     only spawns inside its package). See windows_bash_candidates().
  3. Provisioned PATH: shutil.which("bash") — the store dirs are on the
     process PATH after pm.activate() ran.
  4. POSIX fallback table for non-bundle / daemon-launch PATH edge cases
     (/usr/bin/bash, /bin/bash, $SHELL, /bin/sh). A systemd/cron-launched
     gateway may have a minimal PATH and macOS /bin/bash is not on PATH by
     default, so `which` alone is not enough there.
"""

from __future__ import annotations

import ntpath
import os
import shutil
import subprocess
from collections.abc import Mapping

from pm import paths
from pm.lock import Facts


def _staged_bash() -> str | None:
    """The git Package's bash.exe under the store (Windows bundles only)."""
    import platform

    if platform.system() != "Windows":
        return None
    facts_path = paths.facts_path()
    if not facts_path.is_file():
        return None
    try:
        facts = Facts(facts_path)
        fact = facts.get("git")
        if fact is None or "entry" not in fact:
            return None
        entry = paths.store_root() / fact["entry"]
        for candidate in (
            entry / "usr" / "bin" / "bash.exe",
            entry / "bin" / "bash.exe",
        ):
            if candidate.is_file():
                return str(candidate)
    except Exception:
        return None
    return None


# PATH dirs whose bash.exe is not a shell: System32 holds the WSL launcher
# stub (prints "no installed distributions", exits 1) and WindowsApps holds
# MSIX execution aliases that fail with WinError 5 from an arbitrary process.
_WINDOWS_BASH_STUB_DIRS = ("system32", "windowsapps")


def windows_bash_candidates(
    on_path: str | None, env: Mapping[str, str], *, crema: bool = False,
) -> list[str]:
    """Ordered bash.exe candidates for a Windows host, as pure data: the
    explicit override and Git for Windows roots first, then ``on_path``
    (the ``shutil.which("bash")`` result) unless it is a stub. System32
    precedes Git on most PATHs (#116818)."""
    programfiles = env.get("ProgramFiles", r"C:\Program Files")
    candidates = []
    override = env.get("CREMA_GIT_BASH" if crema else "HERMES_GIT_BASH_PATH")
    if override:
        candidates.append(override)
    roots = [ntpath.join(programfiles, "Git")]
    if env.get("ProgramFiles(x86)"):
        roots.append(ntpath.join(env["ProgramFiles(x86)"], "Git"))
    elif crema:
        roots.append(r"C:\Program Files (x86)\Git")
    if env.get("LOCALAPPDATA"):
        if not crema:
            roots.append(ntpath.join(env["LOCALAPPDATA"], "hermes", "git"))
        roots.append(ntpath.join(env["LOCALAPPDATA"], "Programs", "Git"))
    for root in roots:
        candidates.extend((ntpath.join(root, "bin", "bash.exe"),
                           ntpath.join(root, "usr", "bin", "bash.exe")))
    if on_path:
        norm = ntpath.normpath(on_path).lower()
        foreign = env.get("LOCALAPPDATA")
        foreign = ntpath.normpath(ntpath.join(foreign, "hermes")).lower() + "\\" if foreign else ""
        if not any(stub in norm for stub in _WINDOWS_BASH_STUB_DIRS) and not (crema and foreign and norm.startswith(foreign)):
            candidates.append(on_path)
    return list(dict.fromkeys(candidates))


def _bash_starts(candidate: str) -> bool:
    """An existing bash.exe can still be broken or be a launcher stub."""
    try:
        return subprocess.run(
            [candidate, "-c", "exit 0"], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def bash(*, crema: bool = False) -> str | None:
    """Resolve the bash binary to use, or None if none is available."""
    staged = _staged_bash() if not crema else None
    if staged and _bash_starts(staged):
        return staged

    on_path = shutil.which("bash")
    if os.name == "nt":
        if crema:
            from tools.environments.local_gitbash_probe import (
                _bash_probe_details_cache, _bash_starts as probe,
                _git_bash_aslr_help, _looks_like_msys_spawn_failure, _mandatory_aslr_enabled,
            )
            candidates = [p for p in windows_bash_candidates(on_path, os.environ, crema=True)
                          if os.path.isfile(p)]
            for candidate in candidates:
                if probe(candidate):
                    return candidate
            if candidates:
                details = "\n".join(_bash_probe_details_cache[p] for p in candidates
                                    if _bash_probe_details_cache.get(p))
                if _mandatory_aslr_enabled() is True or _looks_like_msys_spawn_failure(details):
                    raise RuntimeError(_git_bash_aslr_help(candidates[0], details))
                return candidates[0]
            return None
        return next((c for c in windows_bash_candidates(on_path, os.environ)
                     if os.path.isfile(c) and _bash_starts(c)), None)
    if on_path:
        return on_path

    # POSIX fallbacks for minimal-PATH daemon launches / macOS /bin/bash.
    for candidate in (
        "/usr/bin/bash",
        "/bin/bash",
        os.environ.get("SHELL"),
        "/bin/sh",
    ):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return (os.environ.get("SHELL") or "/bin/sh") if crema else None
