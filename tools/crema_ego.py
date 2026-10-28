"""Crema: ego lite (crema docs Crema-ego-lite-브라우저-계획-2026-10-20.md), the browser the agent works in on websites,
with the user's own sign-ins, through its ego-browser command (the ego-browser skill says how). Crema does not ship
it: Settings → 부가 기능 연동 → 웹 브라우저 조작 installs it when the user turns that on, and Crema makes the choices ego lite's own
setup would ask for, as suits Crema (the user changes them in ego lite later if they like): no setup screens, the
sign-ins of the user's default browser brought along, ego lite neither the default browser nor started with Windows.
웹 브라우저 조작 off, the agent does not see the ego-browser skill."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
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
SKILL = "ego-browser"  # the bundled skill the agent works in ego lite with
_installing = threading.Lock()
# Where a running install is, for the 웹 브라우저 조작 card to show: {"step": "download", "percent": 42}, then "check",
# "install", "import" (the sign-ins brought along); empty when no install runs.
_progress: Dict[str, Any] = {}


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


def _enabled() -> bool:
    from hermes_cli.config import load_config

    return SKILL not in ((load_config().get("skills") or {}).get("disabled") or [])


def state() -> Dict[str, Any]:
    return {"installed": bool(cli()) and not _pending().exists(), "enabled": _enabled(), "progress": dict(_progress) or None}


def _downloaded(blocks: int, block_size: int, total: int) -> None:
    if total > 0:
        _progress.update(step="download", percent=min(100, blocks * block_size * 100 // total))


def enable(enabled: bool) -> Dict[str, Any]:
    """웹 브라우저 조작 on or off: off, the ego-browser skill is in skills.disabled, so the agent does not see it.
    The other skills the user turned off stay off."""
    from hermes_cli.config import load_config, save_config

    config = load_config()
    skills = config.setdefault("skills", {})
    disabled = [name for name in skills.get("disabled") or [] if name != SKILL]
    skills["disabled"] = disabled if enabled else disabled + [SKILL]
    save_config(config)
    return state()


def _ego(command: Path, *args: str, timeout: int = 120) -> Dict[str, Any]:
    """Runs ego-browser and reads the JSON it prints. The output goes through a file: ego lite, which the command
    starts when it is not running, would keep a pipe open and the wait for it would never end."""
    with tempfile.TemporaryFile() as out:
        subprocess.run([str(command), *args], stdout=out, stderr=subprocess.STDOUT, timeout=timeout, creationflags=NO_WINDOW, check=True)
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
    _progress.update(step="import", percent=None)
    _close_windows()  # its welcome page, closed before bringing the sign-ins along takes its minute
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
    _close_windows()


# ego lite opens a welcome page on its first start, and a few seconds after bringing the sign-ins along it starts
# again with the tabs it brought: windows the user did not ask for, closed after each. Closed, it keeps running in the background and
# shows a window again when the agent works in it. Windows are closed as they come, until none has come for 10 s.
CLOSE = ("$end = (Get-Date).AddSeconds(60); $quiet = (Get-Date).AddSeconds(15); "
         "while ((Get-Date) -lt $end -and (Get-Date) -lt $quiet) { "
         "$w = Get-Process ego -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 -and $_.Path -like ($env:EGO_HOME + '*') }; "
         "if ($w) { $w | ForEach-Object { [void]$_.CloseMainWindow() }; $quiet = (Get-Date).AddSeconds(10) }; "
         "Start-Sleep -Milliseconds 500 }")


def _close_windows() -> None:
    subprocess.run(["powershell", "-NoProfile", "-Command", CLOSE], env={**os.environ, "EGO_HOME": str(_home())},
                   capture_output=True, timeout=90, creationflags=NO_WINDOW)


def install() -> Dict[str, Any]:
    """Fetches ego lite's installer, installs it with no screens, and sets it up; or, installed, finishes setting it
    up. Then 웹 브라우저 조작 is on. A minute or two; a second call meanwhile waits for the first and finds it
    installed."""
    with _installing:
        try:
            _install()
        finally:
            _progress.clear()
        return state()


def _install() -> None:
    if not state()["installed"]:
        command = cli()
        if not command:
            _progress.update(step="download", percent=0)
            setup = Path(tempfile.mkdtemp(prefix="ego-lite-")) / "egoliteSetup.exe"
            urllib.request.urlretrieve(INSTALLER, setup, _downloaded)
            _progress.update(step="check", percent=None)
            signed = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "$s = Get-AuthenticodeSignature -LiteralPath $env:EGO_SETUP; if ($s.Status -eq 'Valid') { $s.SignerCertificate.Subject }"],
                env={**os.environ, "EGO_SETUP": str(setup)},  # a path with a quote in it stays out of the command text
                capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW).stdout
            if SIGNER not in signed:
                raise ValueError(f"ego lite's installer is not signed by Citro Labs: {signed.strip() or 'no valid signature'}")
            _progress.update(step="install", percent=None)
            subprocess.run(f'"{setup}" --install="{TAG}" --silent', check=True, timeout=300, creationflags=NO_WINDOW)
            commands = sorted(_home().glob("Application/*/ego-browser.exe"))
            if not commands:
                raise OSError(f"ego lite's installer finished, but there is no ego-browser in {_home() / 'Application'}")
            command = commands[-1]
        _set_up(Path(command))
    enable(True)


# Browser work follows one lifecycle here; the SDK still performs page operations.
# Only this controller creates the Space and presents/hides its normal GUI window.
_browser_lock = threading.Lock()


def _work_path(session_id: str) -> Path:
    import hashlib
    from hermes_constants import get_hermes_home

    if not session_id:
        raise ValueError("A Crema conversation is required for browser ownership")
    return get_hermes_home() / "cache" / "ego-work" / (hashlib.sha256(session_id.encode()).hexdigest() + ".json")


def _load_work(session_id: str) -> Optional[dict]:
    path = _work_path(session_id)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _save_work(session_id: str, work: dict) -> None:
    path = _work_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(work, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _windows(action: str, **payload) -> dict:
    from tools.crema_ego_windows import call

    return call(action, _home() / "Application" / "ego.exe", **payload)


def _launch_hidden() -> None:
    exe = _home() / "Application" / "ego.exe"
    if not exe.is_file():
        raise FileNotFoundError(f"Ego is not installed at {exe}")
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    # Ego closes with Crema (a restart, an update), so it starts as after a crash; Chromium's restore prompt would show.
    started = subprocess.Popen(
        [str(exe), "--startup-ego-browser-service", "--hide-crash-restore-bubble"], startupinfo=startup,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )
    # ego-browser starts its own Ego, without that flag, when it finds none up: wait for this one's hidden windows.
    deadline = time.monotonic() + 20
    while not any(window["pid"] == started.pid for window in _windows("inspect")["windows"]):
        if started.poll() is not None or time.monotonic() > deadline:
            raise OSError("Ego did not start in the background")
        time.sleep(0.5)


def _sdk(script: str, *, timeout: int = 6) -> dict:
    command = cli()
    if not command:
        raise FileNotFoundError("ego-browser is not installed")
    return _ego(Path(command), "nodejs", "-e", script, timeout=timeout)


def _space(work: dict) -> dict:
    found = _sdk("console.log(JSON.stringify({spaces:await listTaskSpaces()}))")["spaces"]
    exact = [space for space in found if space["id"] == work["id"] and space["name"] == work["name"]]
    if len(exact) != 1 or exact[0].get("createdBy") != "agent":
        raise ValueError("This conversation's Ego Space no longer exists; no other Space was used")
    return exact[0]


def _summary(work: dict) -> dict:
    return {"space_id": work["id"], "space_name": work["name"], "status": work["phase"]}


def browser(action: str, session_id: str, *, page: str = "", confirmed: bool = False) -> dict:
    """Lifecycle, not login detection: the model requests handoff and interprets the user's reply.
    State is per conversation. A timer, a URL word or a hidden window never confirms login."""
    import uuid

    with _browser_lock:
        work = _load_work(session_id)
        if action == "start":
            if not _enabled() or _pending().exists() or not cli():
                raise ValueError("Enable 웹 브라우저 조작 in Crema settings before starting browser work")
            if work:
                current = _space(work)
                if work["phase"] == "blocked":
                    raise OSError("Background startup was not verified; finish this failed task before starting again")
                if work["phase"] != "working" or current["ownership"] != "agent":
                    raise ValueError("The browser is with the user; wait for confirmation and resume it")
                return _summary(work)
            before = _windows("inspect")
            if not before["roots"]:
                _launch_hidden()
            name = "Crema " + uuid.uuid4().hex[:12]
            made = _sdk("const t=await taskSpace(" + json.dumps(name) + ");"
                        "console.log(JSON.stringify({id:t.spaceId}))", timeout=20)
            work = {"id": made["id"], "name": name, "phase": "working", "window": None}
            _save_work(session_id, work)
            visible_before = {w["hwnd"] for w in before["windows"] if w["visible"]}
            visible_after = {w["hwnd"] for w in _windows("inspect")["windows"] if w["visible"]}
            if visible_after - visible_before:
                work["phase"] = "blocked"
                _save_work(session_id, work)
                raise OSError("An Ego window became visible during background startup; browser work was not verified")
            return _summary(work)

        if action not in {"handoff", "resume", "finish"}:
            raise ValueError("action must be start, handoff, resume or finish")
        if not work:
            raise ValueError("No browser work belongs to this conversation; start it first")
        space = _space(work)
        sid = json.dumps(work["id"])
        if action == "handoff":
            if work["phase"] == "waiting_for_user":
                return _summary(work)
            if work["phase"] != "working" or space["ownership"] != "agent" or not page:
                raise ValueError("handoff needs a working agent-owned Space and its existing page label")
            info = _sdk("const t=await taskSpace(" + sid + ");const tabs=await t.tabs();"
                        "const label=" + json.dumps(page) + ";"
                        "if(!tabs.some(p=>p.label===label))throw new Error('Page label is not in this Space');"
                        "const p=t.page(label);await p.info();"
                        "await t.cdp('Target.activateTarget',{targetId:p.targetId});"
                        "console.log(JSON.stringify({title:await p.title(),targetId:p.targetId}))")
            if not info.get("title") or not info.get("targetId"):
                raise ValueError("Ego did not identify the page to display")
            if work["window"]:
                _windows("show", window=work["window"])
            else:
                shown = _windows("present", space=work["name"], page_title=info["title"])
                if not shown.get("normal_gui") or not shown.get("window"):
                    raise OSError("Ego did not verify a normal GUI window for the selected page")
                work["window"] = shown["window"]
            work["phase"] = "opening_for_user"
            _save_work(session_id, work)
            # The SDK result, not a successful window call, establishes human ownership.
            _sdk("await(await taskSpace(" + sid + ")).handOff();"
                 "const s=(await listTaskSpaces()).find(s=>s.id===" + sid + ");"
                 "if(s?.ownership!=='agentDelegatedToUser')throw new Error('Human handoff not confirmed');"
                 "console.log(JSON.stringify({handedOff:true}))")
            work["phase"] = "waiting_for_user"
            _save_work(session_id, work)
            return _summary(work)

        if action == "resume":
            if confirmed is not True:
                raise ValueError("Wait for the user to confirm they have finished; time is not confirmation")
            if work["phase"] not in {"waiting_for_user", "opening_for_user"} or not work["window"]:
                raise ValueError("This conversation has not handed a browser window to the user")
            if space["ownership"] not in {"agentDelegatedToUser", "agent"}:
                raise ValueError("Ego control changed outside this handoff; the user's Space was left untouched")
            _windows("hide", window=work["window"])
            try:
                call = "takeOverTaskSpace" if space["ownership"] == "agentDelegatedToUser" else "taskSpace"
                _sdk("const t=await " + call + "(" + sid + ");"
                     "console.log(JSON.stringify({id:t.spaceId}))")
                if _space(work)["ownership"] != "agent":
                    raise OSError("Ego did not return control to the agent")
            except Exception:
                _windows("show", window=work["window"])
                raise
            work["phase"] = "working"
            _save_work(session_id, work)
            return _summary(work)

        if work["phase"] not in {"working", "blocked"} or space["ownership"] != "agent":
            raise ValueError("The user still has the browser; do not finish or hide their handoff")
        if work["window"]:
            _windows("hide", window=work["window"])
        _sdk("await(await taskSpace(" + sid + ")).finish({keep:[]});"
             "console.log(JSON.stringify({finished:true}))")
        _work_path(session_id).unlink()
        return {"status": "finished", "space_id": work["id"]}


def crema_ego(args: Dict[str, Any], **kwargs) -> str:
    from tools.registry import tool_error

    try:
        result = browser(str(args.get("action") or ""), str(kwargs.get("session_id") or ""),
                         page=str(args.get("page") or ""), confirmed=args.get("confirmed") is True)
        return json.dumps(result, ensure_ascii=False)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        return tool_error(str(exc))


from tools.registry import registry

registry.register(
    name="crema_ego", toolset="crema_ego", emoji="🌐",
    schema={"name": "crema_ego",
            "description": "Crema's Ego browser lifecycle. start creates or returns this conversation's hidden "
                           "Space; use its numeric space_id with the installed ego-browser SDK for page work. "
                           "handoff shows the named existing page in that Space and gives control to the user "
                           "for login or verification. Then end your response and wait for the user's reply. "
                           "resume hides the same normal browser window and returns control only after the user "
                           "says they finished; confirmed must reflect that reply, never a timer or guess. "
                           "Inspect the page after resuming to see whether authentication succeeded. "
                           "finish releases this conversation's completed Space. Never use raw window handles, "
                           "SDK handOff/takeOver/finish or a different browser instead of these transitions.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["start", "handoff", "resume", "finish"]},
                "page": {"type": "string", "description": "handoff: existing SDK page label in the returned Space"},
                "confirmed": {"type": "boolean", "description": "resume: true only after the user's completion reply"},
            }, "required": ["action"]}},
    handler=crema_ego,
)
