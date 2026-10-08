param([int]$Port = 8001)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot 'runtime'
New-Item -ItemType Directory -Path (Join-Path $runtimeRoot 'config') -Force | Out-Null
$runtimeSettings = Join-Path $runtimeRoot 'config/settings.toml'
if (-not (Test-Path -LiteralPath $runtimeSettings)) {
    $settingsSource = Join-Path $projectRoot 'config/settings.toml'
    if (-not (Test-Path -LiteralPath $settingsSource)) {
        $settingsSource = Join-Path $projectRoot 'config/settings.toml.example'
    }
    if (-not (Test-Path -LiteralPath $settingsSource)) {
        throw 'Missing settings.toml and settings.toml.example in the project config directory.'
    }
    Copy-Item -LiteralPath $settingsSource -Destination $runtimeSettings
}
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:CHRIPTMAS_APP_ROOT = $runtimeRoot
$env:CHRIPTMAS_REPLAY_AGENT_LANCEDB_URI = Join-Path $runtimeRoot 'data/agent_graph/lancedb'
$pythonExe = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Create the project Python environment before starting.' }
& $pythonExe -m uvicorn backend.memory_app.app:app --host 127.0.0.1 --port $Port
