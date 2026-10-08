$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Write-Result([string] $Status, [string] $Source, [string] $Title, [string] $Artist, [string] $Playback) {
    [ordered]@{
        status = $Status
        source = $Source
        title = $Title
        artist = $Artist
        playback_status = $Playback
    } | ConvertTo-Json -Compress
}

function Normalize-Text([object] $Value, [int] $Maximum) {
    if ($null -eq $Value) { return "" }
    $text = [string]$Value
    $text = [regex]::Replace($text, "[\x00-\x1F\x7F]", " ")
    $text = [regex]::Replace($text, "\s+", " ").Trim()
    if ($text.Length -gt $Maximum) { $text = $text.Substring(0, $Maximum) }
    return $text
}

try {
    $runtimeAssembly = [IO.Path]::Combine(
        [Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory(),
        "System.Runtime.WindowsRuntime.dll"
    )
    Add-Type -Path $runtimeAssembly
    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object { $_.Name -eq "AsTask" -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 } |
        Select-Object -First 1
    if ($null -eq $asTask) { throw "winrt_bridge_unavailable" }

    function Await-WinRt([object] $Operation, [Type] $ResultType) {
        $task = $script:asTask.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
        if (-not $task.Wait(4500)) { throw "winrt_timeout" }
        return $task.Result
    }

    $managerType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
    $manager = Await-WinRt ($managerType::RequestAsync()) $managerType
    $session = $manager.GetCurrentSession()
    if ($null -eq $session) {
        Write-Result "empty" "" "" "" "closed"
        exit 0
    }

    $propertiesType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType=WindowsRuntime]
    $properties = Await-WinRt ($session.TryGetMediaPropertiesAsync()) $propertiesType
    $rawPlayback = [string]$session.GetPlaybackInfo().PlaybackStatus
    $playback = switch ($rawPlayback.ToLowerInvariant()) {
        "playing" { "playing" }
        "paused" { "paused" }
        "stopped" { "stopped" }
        "closed" { "closed" }
        default { "unknown" }
    }
    $title = Normalize-Text $properties.Title 160
    $artist = Normalize-Text $properties.Artist 160
    $source = Normalize-Text $session.SourceAppUserModelId 260
    Write-Result ($(if ($title) { "ready" } else { "empty" })) $source $title $artist $playback
} catch {
    Write-Result "unavailable" "" "" "" "unknown"
}
