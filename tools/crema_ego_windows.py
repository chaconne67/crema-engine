"""Windows lifecycle for Crema-owned Ego windows. Space display uses Ego's own
Space picker; a hidden task HWND is never made visible directly."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


# UI labels and the Text click are from the installed Ego Space picker. Each action
# uses a fresh accessibility element and verifies its result before proceeding.
_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$r = $env:CREMA_EGO_WINDOW_REQUEST | ConvertFrom-Json
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class EgoWindows {
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr p);
  [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint x, uint y, uint d, UIntPtr e);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern bool SetProp(IntPtr h, string name, IntPtr value);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr GetProp(IntPtr h, string name);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out Rect r);
  [DllImport("user32.dll")] public static extern bool SystemParametersInfo(uint action, uint param, out Rect r, uint ini);
  public struct Rect { public int Left, Top, Right, Bottom; }

  public static IntPtr[] All() {
    var found = new List<IntPtr>();
    EnumWindows((h,p) => { found.Add(h); return true; }, IntPtr.Zero);
    return found.ToArray();
  }
}
"@
[void][EgoWindows]::SetProcessDPIAware()
# A PC has hundreds of windows; each owner process is looked up once per call (about 4 s a scan otherwise).
$owners=@{}
function Window-Record($h) {
  [uint32]$ownerId=0
  [void][EgoWindows]::GetWindowThreadProcessId($h,[ref]$ownerId)
  if (!$owners.ContainsKey($ownerId)) {
    $owners[$ownerId]=$null
    try { $found=Get-Process -Id $ownerId -ErrorAction Stop; if ($found.Path -ieq $r.exe) { $owners[$ownerId]=$found } } catch {}
  }
  $proc=$owners[$ownerId]
  if (!$proc) { return }
  $kind=New-Object Text.StringBuilder 256
  [void][EgoWindows]::GetClassName($h,$kind,256)
  if ($kind.ToString() -ne 'Chrome_WidgetWin_1') { return }
  $name=New-Object Text.StringBuilder 1024
  [void][EgoWindows]::GetWindowText($h,$name,1024)
  @{hwnd=$h.ToInt64(); pid=$ownerId; created=$proc.StartTime.ToUniversalTime().ToFileTimeUtc().ToString();
    title=$name.ToString(); visible=[EgoWindows]::IsWindowVisible($h); minimized=[EgoWindows]::IsIconic($h)}
}
function Windows { @([EgoWindows]::All() | ForEach-Object { Window-Record $_ }) }
function Check-Window($saved) {
  $now=Window-Record ([IntPtr][long]$saved.hwnd)
  if (!$now -or $now.pid -ne $saved.pid -or $now.created -ne $saved.created) {
    throw 'Ego window ownership changed; no window action was performed'
  }
  if ($saved.marker -and [EgoWindows]::GetProp([IntPtr][long]$saved.hwnd,$saved.marker).ToInt64() -ne 1) {
    throw 'Ego window identity was recycled; no window action was performed'
  }
  $now
}
function Root($w) { [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr][long]$w.hwnd) }
function Elements($root, $name, $kind) {
  $a=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty,$name)
  $b=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,$kind)
  $condition=New-Object System.Windows.Automation.AndCondition($a,$b)
  @($root.FindAll([System.Windows.Automation.TreeScope]::Descendants,$condition) | Where-Object {
    !$_.Current.IsOffscreen -and $_.Current.IsEnabled -and $_.Current.BoundingRectangle.Width -gt 0
  })
}
function Wait-For($test, $seconds, $message) {
  $until=[DateTime]::UtcNow.AddSeconds($seconds)
  do {
    $value=& $test
    if ($null -ne $value -and $value -ne $false) { return $value }
    Start-Sleep -Milliseconds 50
  } while ([DateTime]::UtcNow -lt $until)
  throw $message
}
function One-Element($w,$name,$kind) {
  $found=Elements (Root (Check-Window $w)) $name $kind
  if ($found.Count -eq 1) { return $found[0] }
  if ($found.Count -gt 1) { throw "Ego control is ambiguous: $name" }
}
function Click-Control($w,$element) {
  [void](Check-Window $w)
  if (!(Root $w).Current.IsEnabled) { throw 'Ego has a modal dialog; no click was performed' }
  $invoke=$null
  if ($element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern,[ref]$invoke)) {
    $invoke.Invoke()
    return
  }
  [void][EgoWindows]::SetForegroundWindow([IntPtr][long]$w.hwnd)
  [void](Wait-For { [EgoWindows]::GetForegroundWindow().ToInt64() -eq $w.hwnd } 0.5 ('Ego could not take foreground focus for '+$element.Current.Name+'; no click was performed'))
  $rect=$element.Current.BoundingRectangle
  if ($element.Current.IsOffscreen -or !$element.Current.IsEnabled -or $rect.Width -le 0 -or $rect.Height -le 0) {
    throw 'The Ego control is no longer clickable'
  }
  [void][EgoWindows]::SetCursorPos([int]($rect.X+$rect.Width/2),[int]($rect.Y+$rect.Height/2))
  [EgoWindows]::mouse_event(2,0,0,0,[UIntPtr]::Zero)
  [EgoWindows]::mouse_event(4,0,0,0,[UIntPtr]::Zero)
}
# A hidden Ego window still showed as a blank box where it stood (2026-10-29), so hidden ones wait off-screen and
# a shown one comes back to the middle of the screen. 0x15: keep size and order, do not activate.
function Park($hwnd) { [void][EgoWindows]::SetWindowPos([IntPtr][long]$hwnd,[IntPtr]::Zero,-32000,-32000,0,0,0x15) }
function Center($hwnd) {
  $box=New-Object EgoWindows+Rect
  $area=New-Object EgoWindows+Rect
  [void][EgoWindows]::GetWindowRect([IntPtr][long]$hwnd,[ref]$box)
  [void][EgoWindows]::SystemParametersInfo(0x30,0,[ref]$area,0)
  $x=$area.Left+[Math]::Max(0,[int](($area.Right-$area.Left-($box.Right-$box.Left))/2))
  $y=$area.Top+[Math]::Max(0,[int](($area.Bottom-$area.Top-($box.Bottom-$box.Top))/2))
  [void][EgoWindows]::SetWindowPos([IntPtr][long]$hwnd,[IntPtr]::Zero,$x,$y,0,0,0x15)
}
function Hide-Owned($saved) {
  $now=Check-Window $saved
  [void][EgoWindows]::ShowWindow([IntPtr][long]$now.hwnd,0)
  if ([EgoWindows]::IsWindowVisible([IntPtr][long]$now.hwnd)) { throw 'Ego did not hide its owned window' }
  Park $now.hwnd
}
$hostWindow=$null
$shown=$null
try {
  if ($r.action -eq 'inspect') {
    $roots=@(Get-CimInstance Win32_Process -Filter "Name='ego.exe'" | Where-Object {
      $_.ExecutablePath -ieq $r.exe -and $_.CommandLine -notmatch '(^|\s)--type='
    } | ForEach-Object { $_.ProcessId })
    @{roots=$roots; windows=@(Windows)} | ConvertTo-Json -Depth 5 -Compress
    exit 0
  }
  if ($r.action -in @('hide','show') -and !$r.window.marker) {
    throw 'Only a verified normal Crema GUI window can be shown or hidden'
  }
  if ($r.action -eq 'hide') {
    Hide-Owned $r.window
    @{hidden=$true} | ConvertTo-Json -Compress
    exit 0
  }
  if ($r.action -eq 'show') {
    $w=Check-Window $r.window
    # Only a normal GUI window produced by 'present' is accepted by the Python caller.
    Center $w.hwnd
    [void][EgoWindows]::ShowWindow([IntPtr][long]$w.hwnd,9)
    [void][EgoWindows]::SetForegroundWindow([IntPtr][long]$w.hwnd)
    if (![EgoWindows]::IsWindowVisible([IntPtr][long]$w.hwnd)) { throw 'Ego did not show its owned GUI window' }
    @{window=$w} | ConvertTo-Json -Depth 4 -Compress
    exit 0
  }
  if ($r.action -ne 'present') { throw 'Unknown Ego window action' }
  $before=@(Windows | Where-Object { $_.visible } | ForEach-Object { $_.hwnd })
  $launch=New-Object Diagnostics.ProcessStartInfo
  $launch.FileName=$r.exe
  $launch.Arguments=''
  $launch.UseShellExecute=$false
  $launch.CreateNoWindow=$true
  [void][Diagnostics.Process]::Start($launch)
  $hostWindow=Wait-For {
    $found=@(Windows | Where-Object { $_.visible -and $_.hwnd -notin $before } | Where-Object {
      (Elements (Root $_) 'Open Space (Alt+S)' ([System.Windows.Automation.ControlType]::Button)).Count -eq 1
    })
    if ($found.Count -eq 1) { $found[0] }
    elseif ($found.Count -gt 1) { throw 'More than one new Ego host window appeared' }
  } 7 'Ego did not create a new normal host window; existing user windows were not selected'
  Center $hostWindow.hwnd  # a new Ego window takes the place of the last one, which may be off-screen
  $open=One-Element $hostWindow 'Open Space (Alt+S)' ([System.Windows.Automation.ControlType]::Button)
  if (!$open) { throw 'Ego Space picker is unavailable' }
  Click-Control $hostWindow $open
  $space=Wait-For {
    # The active task footer repeats the Space name. Scope the name to the
    # picker anchored by its observed 'Create a new Space' button.
    $cards=@(Elements (Root $hostWindow) 'Create a new Space' ([System.Windows.Automation.ControlType]::Button))
    $matches=@{}
    foreach($create in $cards) {
      $card=[System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($create)
      foreach($found in (Elements $card $r.space ([System.Windows.Automation.ControlType]::Text))) {
        $matches[($found.GetRuntimeId() -join ',')]=$found
      }
    }
    if ($matches.Count -eq 1) { return @($matches.Values)[0] }
    if ($matches.Count -gt 1) { throw 'The requested Space is ambiguous inside the picker' }
  } 3 'The requested Ego Space was not in the picker'
  Click-Control $hostWindow $space
  $separate=Wait-For {
    One-Element $hostWindow 'Open Space in new window' ([System.Windows.Automation.ControlType]::Button)
  } 3 'Ego did not offer its normal Space window command'
  $beforeSeparate=@(Windows | Where-Object { $_.visible } | ForEach-Object { $_.hwnd })
  Click-Control $hostWindow $separate
  $shown=Wait-For {
    $found=@(Windows | Where-Object { $_.visible -and $_.hwnd -notin $beforeSeparate } | Where-Object {
      (Elements (Root $_) $r.page_title ([System.Windows.Automation.ControlType]::Document)).Count -eq 1
    })
    if ($found.Count -eq 1) { $found[0] }
    elseif ($found.Count -gt 1) { throw 'The displayed Ego page is ambiguous' }
  } 5 'Ego did not show the selected page in its own normal window'
  Center $shown.hwnd
  $marker='Crema.Ego.'+$r.space
  if (![EgoWindows]::SetProp([IntPtr][long]$shown.hwnd,$marker,[IntPtr]1)) {
    throw 'Could not mark the verified Ego window as owned'
  }
  $shown.marker=$marker
  Hide-Owned $hostWindow
  $hostWindow=$null
  [void][EgoWindows]::SetForegroundWindow([IntPtr][long]$shown.hwnd)
  @{window=$shown; normal_gui=$true} | ConvertTo-Json -Depth 5 -Compress
}
catch {
  if ($shown) {
    try { Hide-Owned $shown } catch { [Console]::Error.WriteLine($_.Exception.Message) }
  }
  if ($hostWindow) {
    try { Hide-Owned $hostWindow } catch { [Console]::Error.WriteLine($_.Exception.Message) }
  }
  [Console]::Error.WriteLine($_.Exception.Message)
  exit 1
}
'''


def call(action: str, exe: Path, **payload) -> dict:
    if os.name != "nt":
        raise OSError("Ego window control requires Windows")
    request = {"action": action, "exe": str(exe), **payload}
    completed = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _SCRIPT],
        env={**os.environ, "CREMA_EGO_WINDOW_REQUEST": json.dumps(request, ensure_ascii=False)},
        capture_output=True, encoding="utf-8", errors="replace", timeout=22,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if completed.returncode:
        raise OSError(completed.stderr.strip() or "Ego window control failed")
    return json.loads(completed.stdout)
