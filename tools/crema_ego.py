"""Crema: ego lite (crema docs Crema-ego-lite-브라우저-계획-2026-10-20.md), the browser the agent works in on websites,
with the user's own sign-ins, through its ego-browser command (the ego-browser skill says how). Crema does not ship
it: Settings → 부가 기능 연동 → 웹 사이트 installs it when the user picks it, and Crema makes the choices ego lite's own
setup would ask for, as suits Crema (the user changes them in ego lite later if they like): no setup screens, the
sign-ins of the user's default browser brought along, ego lite neither the default browser nor started with Windows."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

# The Windows installer ego lite links from its README (github.com/citrolabs/ego-lite); the same address carries each
# new version. It is run only when Windows says this company signed it.
INSTALLER = "https://cdn.ego.app/setup/windows/x64/egoliteSetup-Y7MbxKIuhzFB.exe"
SIGNER = "O=CITRO LABS PTE. LIMITED"
# What the installer carries in itself, but installing for this Windows user only (needsadmin=False): in the user's
# own folder, with no administrator prompt.
TAG = "appguid={712B23BF-E734-4038-BE42-D5736B6D9570}&appname=ego%20lite&needsadmin=False&ap=-arch_x64-statsdef_1"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_installing = threading.Lock()


def _home() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Citro Labs" / "ego lite"


def _user_path() -> str:
    """The user's PATH as Windows has it now: ego lite adds its folder after Crema started."""
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


def _pending() -> Path:
    """There while ego lite is set up as far as its first start but the user's sign-ins are not brought along yet
    (that failed): 설치 again finishes it."""
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "ego-lite-import-pending"


def state() -> Dict[str, Any]:
    return {"installed": bool(cli()) and not _pending().exists()}


def _ego(command: Path, *args: str) -> Dict[str, Any]:
    """Runs ego-browser and reads the JSON it prints. The output goes through a file: ego lite, which the command
    starts when it is not running, would keep a pipe open and the wait for it would never end."""
    with tempfile.TemporaryFile() as out:
        subprocess.run([str(command), *args], stdout=out, stderr=subprocess.STDOUT, timeout=120, creationflags=NO_WINDOW)
        out.seek(0)
        text = out.read().decode("utf-8", "replace").strip()
    try:
        return json.loads(text)
    except ValueError:
        raise OSError(f"ego-browser {' '.join(args)}: {text[:300] or 'no output'}") from None


def _set_up(command: Path) -> None:
    """ego lite's first start (which puts ego-browser on the user's PATH), set up as its welcome screens would be.
    Set up once only: ego lite marks a finished setup in ego_config.json, and its profiles and sign-ins from then on
    are the user's (a setup of theirs, from an earlier install, stays)."""
    config = _home() / "User Data" / "ego_config.json"
    if not config.exists():
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('{"not_first_run": true}')  # what ego lite writes when its setup is finished
        _pending().touch()
    sources = _ego(command, "import", "list")["browser_sources"]
    if not _pending().exists():
        return
    source = next((item for item in sources if item["is_default_browser"]), sources[0] if sources else None)
    profile = next((item for item in source["profiles"] if item["is_default"]), None) if source else None
    if profile:
        # In place of ego lite's empty first profile (nothing in it yet); --no-default, or ego lite makes itself the
        # default browser.
        done = _ego(command, "import", "--browser", source["browser_source"], "--profile", profile["dir_name"],
                    "--clear-existing-profiles", "--no-default")
        if not done.get("success"):
            raise OSError(f"ego lite could not bring {source['browser_source']}'s sign-ins along: {done}")
    _pending().unlink()


def install() -> Dict[str, Any]:
    """Fetches ego lite's installer, installs it with no screens, and sets it up; or, installed, finishes setting it
    up. A minute or two; a second call meanwhile waits for the first and finds it installed."""
    with _installing:
        if state()["installed"]:
            return state()
        command = cli()
        if not command:
            setup = Path(tempfile.mkdtemp(prefix="ego-lite-")) / "egoliteSetup.exe"
            urllib.request.urlretrieve(INSTALLER, setup)
            signed = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "$s = Get-AuthenticodeSignature -LiteralPath $env:EGO_SETUP; if ($s.Status -eq 'Valid') { $s.SignerCertificate.Subject }"],
                env={**os.environ, "EGO_SETUP": str(setup)},  # a path with a quote in it stays out of the command text
                capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW).stdout
            if SIGNER not in signed:
                raise ValueError(f"ego lite's installer is not signed by Citro Labs: {signed.strip() or 'no valid signature'}")
            subprocess.run(f'"{setup}" --install="{TAG}" --silent', check=True, timeout=300, creationflags=NO_WINDOW)
            commands = sorted(_home().glob("Application/*/ego-browser.exe"))
            if not commands:
                raise OSError(f"ego lite's installer finished, but there is no ego-browser in {_home() / 'Application'}")
            command = commands[-1]
        _set_up(Path(command))
        return state()
