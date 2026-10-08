"use strict";

const { spawnSync } = require("node:child_process");
const path = require("node:path");

function invalidIdentity(value) {
  return !value || !Number.isInteger(value.pid) || value.pid <= 0
    || typeof value.createdAt !== "string" || !value.createdAt
    || typeof value.executablePath !== "string" || !path.isAbsolute(value.executablePath);
}

// This command observes only windows whose process id is the already-bound
// Electron owner. It neither obtains titles nor calls a focus-changing API.
const POWERSHELL = String.raw`
$ErrorActionPreference = 'Stop'
$pidValue = [int]$env:CHRIPTMAS_FOCUS_OWNER_PID
function Resolve-ExpectedCreationDate([string]$value) {
  if ($value -match '^/Date\((-?\d+)\)/$') { return [DateTimeOffset]::FromUnixTimeMilliseconds([int64]$Matches[1]).UtcDateTime.ToString('o') }
  try { return [DateTimeOffset]::Parse($value, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AllowWhiteSpaces).UtcDateTime.ToString('o') }
  catch { }
  try { return [Management.ManagementDateTimeConverter]::ToDateTime($value).ToUniversalTime().ToString('o') }
  catch { throw 'owner_creation_identity_invalid' }
}
$expectedCreatedAt = Resolve-ExpectedCreationDate ([string]$env:CHRIPTMAS_FOCUS_OWNER_CREATED_AT)
$expectedExecutable = [IO.Path]::GetFullPath([string]$env:CHRIPTMAS_FOCUS_OWNER_EXE)
$owner = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $pidValue) -ErrorAction Stop
if (-not $owner) { throw 'owner_process_missing' }
$actualCreatedAt = $owner.CreationDate.ToUniversalTime().ToString('o')
$actualExecutable = [IO.Path]::GetFullPath([string]$owner.ExecutablePath)
if ($actualCreatedAt -cne $expectedCreatedAt) { throw 'owner_creation_identity_mismatch' }
if (-not [string]::Equals($actualExecutable, $expectedExecutable, [StringComparison]::OrdinalIgnoreCase)) { throw 'owner_executable_identity_mismatch' }
Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class ChriptmasFocusWindowProbe {
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr hWnd, StringBuilder text, int maxCount);
  [DllImport("user32.dll", EntryPoint="GetWindow", SetLastError=true)] public static extern IntPtr GetWindowOwner(IntPtr hWnd, uint command);
  [DllImport("user32.dll", SetLastError=true)] public static extern bool GetGUIThreadInfo(uint threadId, ref GUITHREADINFO info);
  public const uint GW_OWNER = 4;
  [StructLayout(LayoutKind.Sequential)] public struct GUITHREADINFO {
    public int cbSize; public int flags; public IntPtr hwndActive; public IntPtr hwndFocus;
    public IntPtr hwndCapture; public IntPtr hwndMenuOwner; public IntPtr hwndMoveSize; public IntPtr hwndCaret;
    public RECT rcCaret;
  }
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int left; public int top; public int right; public int bottom; }
}
'@
$foreground = [ChriptmasFocusWindowProbe]::GetForegroundWindow()
$windows = [Collections.Generic.List[object]]::new()
$callback = [ChriptmasFocusWindowProbe+EnumWindowsProc]{ param([IntPtr]$hWnd, [IntPtr]$ignored)
  [uint32]$windowPid = 0
  [uint32]$threadId = [ChriptmasFocusWindowProbe]::GetWindowThreadProcessId($hWnd, [ref]$windowPid)
  if ($windowPid -ne $pidValue) { return $true }
  $classBuilder = [Text.StringBuilder]::new(256)
  [void][ChriptmasFocusWindowProbe]::GetClassName($hWnd, $classBuilder, $classBuilder.Capacity)
  $ownerHandle = [ChriptmasFocusWindowProbe]::GetWindowOwner($hWnd, [ChriptmasFocusWindowProbe]::GW_OWNER)
  $ownerKind = 'none'
  if ($ownerHandle -ne [IntPtr]::Zero) {
    [uint32]$ownerPid = 0
    [void][ChriptmasFocusWindowProbe]::GetWindowThreadProcessId($ownerHandle, [ref]$ownerPid)
    $ownerKind = if ($ownerPid -eq $pidValue) { 'owned' } else { 'external' }
  }
  $threadInfo = New-Object ChriptmasFocusWindowProbe+GUITHREADINFO
  $threadInfo.cbSize = [Runtime.InteropServices.Marshal]::SizeOf([type][ChriptmasFocusWindowProbe+GUITHREADINFO])
  $threadInfoAvailable = [ChriptmasFocusWindowProbe]::GetGUIThreadInfo($threadId, [ref]$threadInfo)
  $windows.Add([pscustomobject]@{
    handle = ('0x{0:X}' -f $hWnd.ToInt64())
    class_name = $classBuilder.ToString()
    visible = [ChriptmasFocusWindowProbe]::IsWindowVisible($hWnd)
    minimized = [ChriptmasFocusWindowProbe]::IsIconic($hWnd)
    owner_kind = $ownerKind
    foreground = ($hWnd -eq $foreground)
    thread_active = if ($threadInfoAvailable) { $threadInfo.hwndActive -eq $hWnd } else { $null }
    thread_focus = if ($threadInfoAvailable) { $threadInfo.hwndFocus -eq $hWnd } else { $null }
    thread_info_available = [bool]$threadInfoAvailable
  })
  return $true
}
[void][ChriptmasFocusWindowProbe]::EnumWindows($callback, [IntPtr]::Zero)
[pscustomobject]@{ status = 'ok'; owner_pid = $pidValue; foreground_is_owned = $windows.Where({ $_.foreground }).Count -gt 0; windows = @($windows) } | ConvertTo-Json -Compress -Depth 4
`;

function collectOwnedWindowFocusDiagnostics(identity, { timeout = 5000 } = {}) {
  if (process.platform !== "win32") return { status: "unsupported_platform" };
  if (invalidIdentity(identity)) throw new TypeError("owned_window_focus_identity_invalid");
  const result = spawnSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", POWERSHELL], {
    encoding: "utf8",
    windowsHide: true,
    timeout,
    env: {
      ...process.env,
      CHRIPTMAS_FOCUS_OWNER_PID: String(identity.pid),
      CHRIPTMAS_FOCUS_OWNER_CREATED_AT: identity.createdAt,
      CHRIPTMAS_FOCUS_OWNER_EXE: path.resolve(identity.executablePath),
    },
  });
  if (result.error?.code === "ETIMEDOUT") return { status: "timeout" };
  if (result.status !== 0) return { status: "unavailable", reason: String(result.stderr || "focus_window_probe_failed").trim().slice(0, 180) };
  try {
    const value = JSON.parse(String(result.stdout || "").trim());
    if (value?.status !== "ok" || value.owner_pid !== identity.pid || !Array.isArray(value.windows)) throw new Error("focus_window_probe_invalid");
    return value;
  } catch {
    return { status: "unavailable", reason: "focus_window_probe_invalid" };
  }
}

module.exports = { collectOwnedWindowFocusDiagnostics };
