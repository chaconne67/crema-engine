"""Crema: ego lite (crema docs Crema-ego-lite-브라우저-계획-2026-10-20.md), the browser the agent works in on websites,
with the user's own sign-ins, through its ego-browser command (the ego-browser skill says how). Crema does not ship
it: Settings → 부가 기능 연동 → 웹 사이트 fetches ego lite's own installer when the user picks it, and the user goes
through its setup (whether to bring Chrome's sign-ins along is theirs to choose)."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

# The Windows installer ego lite links from its README (github.com/citrolabs/ego-lite); the same address carries each
# new version. It is run only when Windows says this company signed it.
INSTALLER = "https://cdn.ego.app/setup/windows/x64/egoliteSetup-Y7MbxKIuhzFB.exe"
SIGNER = "O=CITRO LABS PTE. LIMITED"


def _user_path() -> str:
    """The user's PATH as Windows has it now: ego lite's setup adds its folder after Crema started."""
    if os.name != "nt":
        return ""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return os.path.expandvars(winreg.QueryValueEx(key, "Path")[0])
    except OSError:
        return ""


def cli() -> Optional[str]:
    """The ego-browser command. Found only in the user's new PATH, its folder joins this engine's, so the agent's
    terminal (started from it) finds the command too."""
    if found := shutil.which("ego-browser"):
        return found
    found = shutil.which("ego-browser", path=_user_path())
    if found:
        os.environ["PATH"] = str(Path(found).parent) + os.pathsep + os.environ.get("PATH", "")
    return found


def state() -> Dict[str, Any]:
    return {"installed": bool(cli())}


def install() -> Dict[str, Any]:
    """Fetches ego lite's installer and opens it on the user's screen."""
    setup = Path(tempfile.mkdtemp(prefix="ego-lite-")) / "egoliteSetup.exe"
    urllib.request.urlretrieve(INSTALLER, setup)
    signed = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "$s = Get-AuthenticodeSignature -LiteralPath $env:EGO_SETUP; if ($s.Status -eq 'Valid') { $s.SignerCertificate.Subject }"],
        env={**os.environ, "EGO_SETUP": str(setup)},  # a path with a quote in it stays out of the command text
        capture_output=True, text=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    if SIGNER not in signed:
        raise ValueError(f"ego lite's installer is not signed by Citro Labs: {signed.strip() or 'no valid signature'}")
    os.startfile(setup)  # noqa: S606 — the installer asks Windows for the rights it needs
    return {"started": True}
