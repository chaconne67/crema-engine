"""Real Windows smoke for the registered Crema Ego lifecycle, using a local fixture.
No real account or credential. 'cycle' simulates the human completion signal.
'prepare'/'handoff'/'resume' allow one brief actual UI inspection between calls."""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("action", choices=["prepare", "handoff", "resume", "finish", "cycle"])
parser.add_argument("--out", required=True)
parser.add_argument("--candidate", help="Validated server artifact directory; omit for installed-code verification")
args = parser.parse_args()
out = Path(args.out)
out.mkdir(parents=True, exist_ok=True)
# Isolate controller records and configuration, not the user's Ego login profile.
os.environ["HERMES_HOME"] = str(out / "engine-home")
if args.candidate:
    for name in ("crema_ego_windows", "crema_ego"):
        spec = importlib.util.spec_from_file_location("tools." + name, Path(args.candidate) / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules["tools." + name] = module
        spec.loader.exec_module(module)
from tools import crema_ego as ego

from contextlib import contextmanager
import ctypes as c
from ctypes import wintypes as w
import threading

@contextmanager
def observe_hidden_work():
    """Observe top-level Ego visibility during the real registered path."""
    u, k = c.WinDLL("user32", use_last_error=True), c.WinDLL("kernel32", use_last_error=True)
    u.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
    u.GetAncestor.argtypes, u.GetAncestor.restype = [w.HWND, w.UINT], w.HWND
    u.IsWindowVisible.argtypes = [w.HWND]
    k.OpenProcess.argtypes, k.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
    k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
    k.CloseHandle.argtypes = [w.HANDLE]
    callback_type = c.WINFUNCTYPE(None, c.c_void_p, w.DWORD, w.HWND, w.LONG, w.LONG, w.DWORD, w.DWORD)
    u.SetWinEventHook.argtypes = [w.DWORD, w.DWORD, w.HMODULE, callback_type, w.DWORD, w.DWORD, w.DWORD]
    u.SetWinEventHook.restype = c.c_void_p
    u.UnhookWinEvent.argtypes = [c.c_void_p]
    events, errors = [], []
    ready, stop = threading.Event(), threading.Event()
    exe = str(ego._home() / "Application" / "ego.exe").casefold()
    @callback_type
    def callback(hook, kind, hwnd, obj, child, tid, stamp):
        if not hwnd or obj or child or u.GetAncestor(hwnd, 2) != hwnd:
            return
        pid = w.DWORD()
        u.GetWindowThreadProcessId(hwnd, c.byref(pid))
        handle = k.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return
        try:
            name, size = c.create_unicode_buffer(32768), w.DWORD(32768)
            if k.QueryFullProcessImageNameW(handle, 0, name, c.byref(size)) and name.value.casefold() == exe:
                events.append({"event": kind, "hwnd": int(hwnd), "visible": bool(u.IsWindowVisible(hwnd))})
        finally:
            k.CloseHandle(handle)
    def run():
        hook = u.SetWinEventHook(0x8000, 0x8003, None, callback, 0, 0, 0)
        if not hook:
            errors.append(c.get_last_error())
        ready.set()
        if not hook:
            return
        msg = w.MSG()
        try:
            while not stop.is_set():
                while u.PeekMessageW(c.byref(msg), None, 0, 0, 1):
                    u.TranslateMessage(c.byref(msg))
                    u.DispatchMessageW(c.byref(msg))
                stop.wait(.005)
        finally:
            u.UnhookWinEvent(hook)
    thread = threading.Thread(target=run)
    thread.start()
    if not ready.wait(5) or errors:
        stop.set()
        thread.join(5)
        raise RuntimeError("Windows visibility observer did not start")
    try:
        yield events
    finally:
        stop.set()
        thread.join(5)

session = "ego-windows-smoke-" + out.name
result_path = out / "result.json"
result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
def save():
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
def call(action, **kwargs):
    reply = json.loads(ego.crema_ego({"action": action, **kwargs}, session_id=session))
    if reply.get("error"):
        raise RuntimeError(reply)
    return reply

try:
    if args.action in {"prepare", "cycle"}:
        fixture = out / "login-fixture.html"
        fixture.write_text("""<!doctype html><meta charset="utf-8">
<title>Crema 로그인 인계 시험</title>
<style>body{font:20px system-ui;background:#faf8f5;color:#29231d;margin:70px}
main{max-width:720px;padding:36px;border:1px solid #ddd;border-radius:18px;background:white}
input,button{font:inherit;padding:12px;margin:12px 0}button{cursor:pointer}p{line-height:1.7}</style>
<main><h1>Crema 로그인 인계 시험</h1>
<p>실제 계정이나 비밀번호를 사용하지 않는 시험 페이지입니다.</p>
<label>작업 상태 <input id="probe-input" value="before"></label><br>
<button id="probe-continue" onclick="document.querySelector('#probe-status').textContent='probe-continued';document.title='Crema 로그인 완료 시험'">시험 로그인 완료</button>
<p id="probe-status">probe-ready</p></main>""", encoding="utf-8")
        with observe_hidden_work() as events:
            result["before"] = ego._windows("inspect")
            result["start"] = call("start")
            sid = result["start"]["space_id"]
            result["page"] = ego._sdk(
                "const t=await taskSpace(" + str(sid) + ");const p=t.page('p1');"
                "await p.goto(" + json.dumps(fixture.as_uri()) + ");"
                "await p.waitForSelector('#probe-status',{state:'visible',timeout:3000});"
                "await p.fill('#probe-input','preserved-through-handoff');"
                "console.log(JSON.stringify({id:t.spaceId,value:await p.evaluate(()=>document.querySelector('#probe-input').value)}))")
            result["hidden_before_handoff"] = ego._windows("inspect")
            before_ids = {w["hwnd"] for w in result["before"]["windows"] if w["visible"]}
            after_ids = {w["hwnd"] for w in result["hidden_before_handoff"]["windows"] if w["visible"]}
            assert not after_ids - before_ids, "New visible window during hidden page work"
        result["hidden_events"] = events
        assert not any(e["visible"] for e in events), "Ego became visible during background work"
        result["prepared"] = True
        save()
    if args.action in {"handoff", "cycle"}:
        assert result.get("prepared"), "Run prepare before handoff"
        started = time.monotonic()
        result["handoff"] = call("handoff", page="p1")
        result["handoff_seconds"] = round(time.monotonic() - started, 3)
        result["shown"] = ego._windows("inspect")
        result["owned_window"] = ego._load_work(session)["window"]
        assert result["handoff"]["status"] == "waiting_for_user"
        assert any(w["hwnd"] == result["owned_window"]["hwnd"] and w["visible"] for w in result["shown"]["windows"])
        save()
    if args.action in {"resume", "cycle"}:
        started = time.monotonic()
        # Test-only completion signal. This is never evidence of a real account login.
        result["resume"] = call("resume", confirmed=True)
        result["resume_seconds"] = round(time.monotonic() - started, 3)
        sid = result["resume"]["space_id"]
        result["after_page"] = ego._sdk(
            "const t=await taskSpace(" + str(sid) + ");const p=t.page('p1');"
            "console.log(JSON.stringify({id:t.spaceId,value:await p.evaluate(()=>document.querySelector('#probe-input').value),"
            "status:await p.evaluate(()=>document.querySelector('#probe-status').textContent)}))")
        assert result["after_page"]["id"] == result["start"]["space_id"]
        assert result["after_page"]["value"] == "preserved-through-handoff"
        result["hidden_after_resume"] = ego._windows("inspect")
        assert not any(w["hwnd"] == result["owned_window"]["hwnd"] and w["visible"] for w in result["hidden_after_resume"]["windows"])
        save()
    if args.action in {"finish", "cycle"}:
        result["finish"] = call("finish")
        result["after"] = ego._windows("inspect")
        save()
    print(json.dumps({k: v for k, v in result.items() if k in
                      {"prepared", "start", "handoff", "handoff_seconds", "resume", "resume_seconds", "after_page", "finish"}},
                     ensure_ascii=False))
except Exception as exc:
    result["error"] = str(exc)
    save()
    # Bounded test recovery only. A failed test is not a completed login.
    work = ego._load_work(session)
    if work and work.get("window"):
        try:
            ego._windows("hide", window=work["window"])
            result["test_recovery_hidden"] = True
        except Exception as recovery:
            result["test_recovery_error"] = str(recovery)
        save()
    raise
