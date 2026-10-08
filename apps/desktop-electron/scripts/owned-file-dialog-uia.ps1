[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateRange(1, [int]::MaxValue)][int]$OwnerPid,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$OwnerCreationDate,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$OwnerExecutablePath,
    [UInt64]$OwnerWindowHandle,
    [ValidateSet('open', 'open-directory', 'cancel', 'confirm-root-migration')][string]$Action = 'open',
    [string]$FixtureRoot,
    [string]$FixturePath,
    [ValidateRange(1, 30)][int]$TimeoutSeconds = 10,
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$RootMigrationTitle = [string](-join @([char]0x786e, [char]0x8ba4, [char]0x8fc1, [char]0x79fb, [char]0x672c, [char]0x5730, [char]0x6570, [char]0x636e, [char]0x6839, [char]0x76ee, [char]0x5f55))
$RootMigrationButton = [string](-join @([char]0x786e, [char]0x8ba4, [char]0x8fc1, [char]0x79fb))

function Stop-Closed([string]$Code) {
    [Console]::Error.WriteLine($Code)
    exit 2
}

function Resolve-AbsoluteFile([string]$PathValue, [string]$Code) {
    if ([string]::IsNullOrWhiteSpace($PathValue) -or -not [IO.Path]::IsPathRooted($PathValue)) { Stop-Closed $Code }
    try { return [IO.Path]::GetFullPath($PathValue) } catch { Stop-Closed $Code }
}

function Test-ExactProcessIdentity {
    $entry = Get-CimInstance Win32_Process -Filter "ProcessId = $OwnerPid" -ErrorAction SilentlyContinue
    if ($null -eq $entry -or [int]$entry.ProcessId -ne $OwnerPid -or [string]::IsNullOrWhiteSpace([string]$entry.CreationDate)) { Stop-Closed 'owner_process_unavailable' }
    $currentCreationDate = $entry.CreationDate.ToUniversalTime().ToString('o')
    if ([string]::IsNullOrWhiteSpace([string]$currentCreationDate) -or [string]$currentCreationDate -cne $OwnerCreationDate) { Stop-Closed 'owner_creation_identity_mismatch' }
    if ([string]::IsNullOrWhiteSpace([string]$entry.ExecutablePath)) { Stop-Closed 'owner_executable_unavailable' }
    $expectedExecutable = Resolve-AbsoluteFile $OwnerExecutablePath 'owner_executable_invalid'
    $actualExecutable = Resolve-AbsoluteFile ([string]$entry.ExecutablePath) 'owner_executable_unavailable'
    if (-not [string]::Equals($expectedExecutable, $actualExecutable, [StringComparison]::OrdinalIgnoreCase)) { Stop-Closed 'owner_executable_identity_mismatch' }
    return $entry
}

$native = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class OwnedFileDialogNative {
  public const uint GW_OWNER = 4;
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr parent, EnumWindowsProc callback, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
  [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr hWnd, uint command);
  [DllImport("user32.dll")] public static extern IntPtr GetParent(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern IntPtr GetDlgItem(IntPtr hDlg, int id);
  [DllImport("user32.dll")] public static extern int GetDlgCtrlID(IntPtr hWnd);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessage(IntPtr hWnd, uint message, IntPtr wParam, string lParam);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr hWnd, uint message, IntPtr wParam, IntPtr lParam);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr hWnd, StringBuilder text, int maxCount);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int maxCount);
  public static uint ProcessId(IntPtr handle) { uint value; GetWindowThreadProcessId(handle, out value); return value; }
  public static string ClassName(IntPtr handle) { var text = new StringBuilder(256); GetClassName(handle, text, text.Capacity); return text.ToString(); }
  public static string WindowText(IntPtr handle) { var text = new StringBuilder(512); GetWindowText(handle, text, text.Capacity); return text.ToString(); }
  public static IntPtr[] TopLevelWindows() { var windows = new List<IntPtr>(); EnumWindows((handle, state) => { windows.Add(handle); return true; }, IntPtr.Zero); return windows.ToArray(); }
  public static IntPtr[] ChildWindows(IntPtr parent) { var windows = new List<IntPtr>(); EnumChildWindows(parent, (handle, state) => { windows.Add(handle); return true; }, IntPtr.Zero); return windows.ToArray(); }
}
'@

Add-Type -TypeDefinition $native -ErrorAction Stop
$owner = Test-ExactProcessIdentity
if ($ValidateOnly) {
    [pscustomobject]@{ status = 'identity_validated'; owner_pid = $OwnerPid } | ConvertTo-Json -Compress
    exit 0
}
Add-Type -AssemblyName UIAutomationClient -ErrorAction Stop
Add-Type -AssemblyName UIAutomationTypes -ErrorAction Stop

if ($OwnerWindowHandle -eq 0) { Stop-Closed 'owner_window_required' }
$ownerHandle = [IntPtr]::new([int64]$OwnerWindowHandle)
function Test-ExactOwnerWindow {
    if ([OwnedFileDialogNative]::ProcessId($ownerHandle) -ne [uint32]$OwnerPid -or [OwnedFileDialogNative]::ClassName($ownerHandle) -ne 'Chrome_WidgetWin_1') { Stop-Closed 'owner_window_identity_mismatch' }
}
Test-ExactOwnerWindow

$fixtureRoot = Resolve-AbsoluteFile $FixtureRoot 'fixture_root_invalid'
$fixturePath = Resolve-AbsoluteFile $FixturePath 'fixture_path_invalid'
$fixtureMustBeDirectory = $Action -in @('open-directory', 'confirm-root-migration')
if (-not (Test-Path -LiteralPath $fixtureRoot -PathType Container)) { Stop-Closed 'fixture_unavailable' }
if ($fixtureMustBeDirectory -and -not (Test-Path -LiteralPath $fixturePath -PathType Container)) { Stop-Closed 'fixture_unavailable' }
if (-not $fixtureMustBeDirectory -and -not (Test-Path -LiteralPath $fixturePath -PathType Leaf)) { Stop-Closed 'fixture_unavailable' }
$rootPrefix = $fixtureRoot.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if (-not $fixturePath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) { Stop-Closed 'fixture_outside_root' }
if (((Get-Item -LiteralPath $fixtureRoot -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -or ((Get-Item -LiteralPath $fixturePath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { Stop-Closed 'fixture_reparse_point' }

$knownPids = [Collections.Generic.HashSet[uint32]]::new()
[void]$knownPids.Add([uint32]$OwnerPid)
$pendingPids = [Collections.Generic.Queue[uint32]]::new()
$pendingPids.Enqueue([uint32]$OwnerPid)
while ($pendingPids.Count -gt 0 -and $knownPids.Count -lt 64) {
    $parentPid = $pendingPids.Dequeue()
    foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $parentPid" -ErrorAction SilentlyContinue)) {
        $childPid = [uint32]$child.ProcessId
        if ($knownPids.Add($childPid)) { $pendingPids.Enqueue($childPid) }
    }
}
if ($knownPids.Count -ge 64) { Stop-Closed 'descendant_scope_ambiguous' }

function Test-OwnedByMainWindow([IntPtr]$Handle) {
    $current = $Handle
    $seen = [Collections.Generic.HashSet[IntPtr]]::new()
    for ($depth = 0; $depth -lt 16 -and $current -ne [IntPtr]::Zero; $depth += 1) {
        if (-not $seen.Add($current)) { return $false }
        if ($current -eq $ownerHandle) { return $true }
        $current = [OwnedFileDialogNative]::GetWindow($current, [OwnedFileDialogNative]::GW_OWNER)
    }
    return $false
}

function Find-OwnedFileDialog {
    $matches = @()
    foreach ($handle in [OwnedFileDialogNative]::TopLevelWindows()) {
        if (-not $knownPids.Contains([OwnedFileDialogNative]::ProcessId($handle))) { continue }
        if ([OwnedFileDialogNative]::ClassName($handle) -ne '#32770') { continue }
        if (-not (Test-OwnedByMainWindow $handle)) { continue }
        if ($Action -eq 'open-directory') {
            $matches += $handle
        } else {
            try {
                $element = [System.Windows.Automation.AutomationElement]::FromHandle($handle)
                if ($null -ne $element) { $matches += $element }
            } catch {}
        }
    }
    if ($matches.Count -gt 1) { Stop-Closed 'file_dialog_ambiguous' }
    return $matches | Select-Object -First 1
}

function Test-DirectChildOfDialog([IntPtr]$Handle) {
    return Test-ChildOfHandle $Handle $dialogHandle
}

function Test-ChildOfHandle([IntPtr]$Handle, [IntPtr]$Ancestor) {
    $current = $Handle
    for ($depth = 0; $depth -lt 8 -and $current -ne [IntPtr]::Zero; $depth += 1) {
        if ($current -eq $Ancestor) { return $true }
        $current = [OwnedFileDialogNative]::GetParent($current)
    }
    return $false
}

function Find-OwnedMigrationDialog {
    $matches = @()
    foreach ($handle in [OwnedFileDialogNative]::TopLevelWindows()) {
        if (-not $knownPids.Contains([OwnedFileDialogNative]::ProcessId($handle))) { continue }
        if ([OwnedFileDialogNative]::ClassName($handle) -ne '#32770' -or -not (Test-OwnedByMainWindow $handle)) { continue }
        if ([OwnedFileDialogNative]::WindowText($handle) -ceq $RootMigrationTitle) { $matches += $handle }
    }
    if ($matches.Count -gt 1) { Stop-Closed 'root_migration_dialog_ambiguous' }
    return $matches | Select-Object -First 1
}

$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
$dialog = $null
if ($Action -eq 'confirm-root-migration') {
    while ([DateTime]::UtcNow -lt $deadline -and $null -eq $dialog) {
        $dialog = Find-OwnedMigrationDialog
        if ($null -eq $dialog) { Start-Sleep -Milliseconds 100 }
    }
    if ($null -eq $dialog) { Stop-Closed 'owned_root_migration_dialog_unavailable' }
    [void](Test-ExactProcessIdentity)
    Test-ExactOwnerWindow
    $dialogHandle = [IntPtr]$dialog
    if (-not $knownPids.Contains([OwnedFileDialogNative]::ProcessId($dialogHandle)) -or -not (Test-OwnedByMainWindow $dialogHandle) -or [OwnedFileDialogNative]::ClassName($dialogHandle) -ne '#32770' -or [OwnedFileDialogNative]::WindowText($dialogHandle) -cne $RootMigrationTitle) { Stop-Closed 'owned_root_migration_dialog_identity_changed' }
    $buttons = @()
    foreach ($child in [OwnedFileDialogNative]::ChildWindows($dialogHandle)) {
        if ([OwnedFileDialogNative]::ClassName($child) -eq 'Button' -and (Test-DirectChildOfDialog $child) -and [OwnedFileDialogNative]::WindowText($child) -ceq $RootMigrationButton) { $buttons += $child }
    }
    if ($buttons.Count -ne 1) { Stop-Closed 'root_migration_confirm_control_unavailable' }
    [void](Test-ExactProcessIdentity)
    Test-ExactOwnerWindow
    if (-not (Test-OwnedByMainWindow $dialogHandle) -or [OwnedFileDialogNative]::WindowText($dialogHandle) -cne $RootMigrationTitle -or [OwnedFileDialogNative]::WindowText($buttons[0]) -cne $RootMigrationButton) { Stop-Closed 'owned_root_migration_dialog_identity_changed' }
    [void][OwnedFileDialogNative]::SendMessage($buttons[0], 0x00F5, [IntPtr]::Zero, [IntPtr]::Zero)
    [pscustomobject]@{ status = 'invoked'; action = $Action; owner_pid = $OwnerPid; mode = 'root_migration_confirm' } | ConvertTo-Json -Compress
    exit 0
}
while ([DateTime]::UtcNow -lt $deadline -and $null -eq $dialog) {
    $dialog = Find-OwnedFileDialog
    if ($null -eq $dialog) { Start-Sleep -Milliseconds 100 }
}
if ($null -eq $dialog) { Stop-Closed 'owned_file_dialog_unavailable' }

# The dialog may have waited behind unrelated windows. Revalidate the exact
# process, main HWND and owner chain immediately before any UIA action.
[void](Test-ExactProcessIdentity)
Test-ExactOwnerWindow
$dialogHandle = if ($Action -eq 'open-directory') { [IntPtr]$dialog } else { [IntPtr]::new([int64]$dialog.Current.NativeWindowHandle) }
if (-not $knownPids.Contains([OwnedFileDialogNative]::ProcessId($dialogHandle)) -or -not (Test-OwnedByMainWindow $dialogHandle)) { Stop-Closed 'owned_file_dialog_identity_changed' }

function Write-KnownControlMetadata {
    $metadata = @()
    foreach ($knownId in @('1', '2', '1148')) {
        $controls = $dialog.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::AutomationIdProperty, $knownId))
        foreach ($control in $controls) {
            $metadata += [pscustomobject]@{
                automation_id = $control.Current.AutomationId
                control_type = $control.Current.ControlType.ProgrammaticName
                patterns = @($control.GetSupportedPatterns() | ForEach-Object { $_.ProgrammaticName })
            }
        }
    }
    [Console]::Error.WriteLine(($metadata | ConvertTo-Json -Compress))
}

function Find-ExactKnownAction([string]$AutomationId) {
    $condition = [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::AutomationIdProperty, $AutomationId)
    do {
        $matches = @()
        foreach ($candidate in $dialog.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)) {
            if ($candidate.Current.ControlType -notin @([System.Windows.Automation.ControlType]::Button, [System.Windows.Automation.ControlType]::SplitButton)) { continue }
            try { [void]$candidate.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern); $matches += $candidate } catch {}
        }
        if ($matches.Count -eq 1) { return $matches[0] }
        if ([DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 100 }
    } while ([DateTime]::UtcNow -lt $deadline)
    return $null
}

function Get-ExactNativeDialogControl([int]$ControlId, [string]$ExpectedClass, [string]$Code) {
    $control = [OwnedFileDialogNative]::GetDlgItem($dialogHandle, $ControlId)
    if ($control -eq [IntPtr]::Zero -or [OwnedFileDialogNative]::GetDlgCtrlID($control) -ne $ControlId -or [OwnedFileDialogNative]::GetParent($control) -ne $dialogHandle -or -not [string]::Equals([OwnedFileDialogNative]::ClassName($control), $ExpectedClass, [StringComparison]::OrdinalIgnoreCase)) {
        Write-NativeControlMetadata
        Stop-Closed $Code
    }
    return $control
}

function Get-ExactNestedNativeControl([IntPtr]$Parent, [int]$ControlId, [string]$ExpectedClass, [string]$Code) {
    $matches = @()
    foreach ($child in [OwnedFileDialogNative]::ChildWindows($Parent)) {
        if ([OwnedFileDialogNative]::GetDlgCtrlID($child) -ne $ControlId) { continue }
        if (-not [string]::Equals([OwnedFileDialogNative]::ClassName($child), $ExpectedClass, [StringComparison]::OrdinalIgnoreCase)) { continue }
        if (-not (Test-ChildOfHandle $child $Parent) -or -not (Test-DirectChildOfDialog $child)) { continue }
        $matches += $child
    }
    if ($matches.Count -ne 1) { Write-NativeControlMetadata; Stop-Closed $Code }
    return $matches[0]
}

function Write-NativeControlMetadata {
    $metadata = @()
    foreach ($controlId in @(1, 2, 1148)) {
        $direct = [OwnedFileDialogNative]::GetDlgItem($dialogHandle, $controlId)
        if ($direct -eq [IntPtr]::Zero) {
            $metadata += [pscustomobject]@{ requested_control_id = $controlId; source = 'GetDlgItem'; present = $false }
        } else {
            $metadata += [pscustomobject]@{ requested_control_id = $controlId; source = 'GetDlgItem'; present = $true; control_id = [OwnedFileDialogNative]::GetDlgCtrlID($direct); control_class = [OwnedFileDialogNative]::ClassName($direct); parent_chain_to_dialog = Test-DirectChildOfDialog $direct }
        }
        foreach ($child in [OwnedFileDialogNative]::ChildWindows($dialogHandle)) {
            if ([OwnedFileDialogNative]::GetDlgCtrlID($child) -eq $controlId) {
                $metadata += [pscustomobject]@{ requested_control_id = $controlId; source = 'EnumChildWindows'; present = $true; control_id = [OwnedFileDialogNative]::GetDlgCtrlID($child); control_class = [OwnedFileDialogNative]::ClassName($child); parent_chain_to_dialog = Test-DirectChildOfDialog $child }
            }
        }
    }
    if ($Action -eq 'open-directory') {
        foreach ($child in [OwnedFileDialogNative]::ChildWindows($dialogHandle)) {
            $controlClass = [OwnedFileDialogNative]::ClassName($child)
            if ($controlClass -cnotin @('Edit', 'ComboBox', 'ComboBoxEx32')) { continue }
            $metadata += [pscustomobject]@{
                source = 'EnumChildWindows'
                control_id = [OwnedFileDialogNative]::GetDlgCtrlID($child)
                control_class = $controlClass
                parent_chain_to_dialog = Test-ChildOfHandle $child $dialogHandle
                direct_child_of_dialog = ([OwnedFileDialogNative]::GetParent($child) -eq $dialogHandle)
            }
        }
    }
    [Console]::Error.WriteLine(($metadata | ConvertTo-Json -Compress))
}

function Invoke-NativeDialogFallback {
    [void](Test-ExactProcessIdentity)
    Test-ExactOwnerWindow
    if ([OwnedFileDialogNative]::ClassName($dialogHandle) -ne '#32770' -or -not (Test-OwnedByMainWindow $dialogHandle)) { Stop-Closed 'owned_file_dialog_identity_changed' }
    if ($Action -eq 'open') {
        $filenameContainer = Get-ExactNativeDialogControl 1148 'ComboBoxEx32' 'dialog_filename_control_unavailable'
        $filename = Get-ExactNestedNativeControl $filenameContainer 1148 'Edit' 'dialog_filename_control_unavailable'
        $setResult = [OwnedFileDialogNative]::SendMessage($filename, 0x000C, [IntPtr]::Zero, $fixturePath)
        if ($setResult -eq [IntPtr]::Zero) { Stop-Closed 'dialog_filename_set_failed' }
    } elseif ($Action -eq 'open-directory') {
        $directory = Get-ExactNativeDialogControl 1152 'Edit' 'dialog_directory_control_unavailable'
        if ([OwnedFileDialogNative]::ClassName($directory) -cne 'Edit' -or [OwnedFileDialogNative]::GetParent($directory) -ne $dialogHandle) { Write-NativeControlMetadata; Stop-Closed 'dialog_directory_control_unavailable' }
        $setResult = [OwnedFileDialogNative]::SendMessage($directory, 0x000C, [IntPtr]::Zero, $fixturePath)
        if ($setResult -eq [IntPtr]::Zero) { Stop-Closed 'dialog_directory_set_failed' }
    }
    $buttonId = if ($Action -in @('open', 'open-directory')) { 1 } else { 2 }
    $button = Get-ExactNativeDialogControl $buttonId 'Button' 'dialog_action_control_unavailable'
    [void][OwnedFileDialogNative]::SendMessage($button, 0x00F5, [IntPtr]::Zero, [IntPtr]::Zero)
    [pscustomobject]@{ status = 'invoked'; action = $Action; owner_pid = $OwnerPid; mode = 'native_control' } | ConvertTo-Json -Compress
    exit 0
}

if ($Action -eq 'open-directory') {
    Invoke-NativeDialogFallback
}

$buttonId = if ($Action -in @('open', 'open-directory')) { '1' } else { '2' }
$button = Find-ExactKnownAction $buttonId
if ($null -eq $button) {
    Write-KnownControlMetadata
    Invoke-NativeDialogFallback
}

if ($Action -in @('open', 'open-directory')) {
    do {
        $valueCandidates = @()
        $allFilenameControls = $dialog.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::AutomationIdProperty, '1148'))
        foreach ($candidate in $allFilenameControls) {
            if ($candidate.Current.ControlType -notin @([System.Windows.Automation.ControlType]::Edit, [System.Windows.Automation.ControlType]::ComboBox)) { continue }
            try { $valueCandidates += [pscustomobject]@{ Element = $candidate; Pattern = $candidate.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern) } } catch {}
        }
        if ($valueCandidates.Count -ne 1 -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 100 }
    } while ($valueCandidates.Count -ne 1 -and [DateTime]::UtcNow -lt $deadline)
    if ($valueCandidates.Count -ne 1) { Write-KnownControlMetadata; Invoke-NativeDialogFallback }
    try { ([System.Windows.Automation.ValuePattern]$valueCandidates[0].Pattern).SetValue($fixturePath) } catch { Stop-Closed 'dialog_filename_set_failed' }
}
try {
    $invokePattern = $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    ([System.Windows.Automation.InvokePattern]$invokePattern).Invoke()
} catch { Stop-Closed 'dialog_action_invoke_failed' }
[pscustomobject]@{ status = 'invoked'; action = $Action; owner_pid = $OwnerPid } | ConvertTo-Json -Compress
