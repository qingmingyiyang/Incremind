<#
Incremind 一键启动。由仓库根目录的 start.bat 调用。

第一次运行会准备 Python 和网页依赖；之后启动后端（8001）和网页（4173），
两者都就绪后打开浏览器。关闭这个窗口或按 Ctrl+C 即停止。

  -DataRoot <目录>  数据目录，默认是仓库下的 runtime\
  -NoBrowser        不自动打开浏览器
#>
param(
    [string]$DataRoot,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
try { $Host.UI.RawUI.WindowTitle = 'Incremind' } catch {}

$Root = Split-Path -Parent $PSScriptRoot
$Frontend = Join-Path $Root 'src\frontend'
$LogDir = Join-Path $Root 'logs'
$AppUrl = 'http://127.0.0.1:4173'
$HealthUrl = 'http://127.0.0.1:8001/api/health'
if (-not $DataRoot) { $DataRoot = Join-Path $Root 'runtime' }
$DataRoot = [IO.Path]::GetFullPath($DataRoot)

# Windows 默认的路径上限是 260 个字符，数据目录里最深的文件还要再占约 190 个。
if ($DataRoot.Length -gt 60) {
    Write-Host "[Incremind] 提示：数据目录路径有 $($DataRoot.Length) 个字符，太深可能出现路径超长错误。建议放在短路径下，例如 start.bat -DataRoot D:\IncremindData" -ForegroundColor Yellow
}

function Say([string]$Text) { Write-Host "[Incremind] $Text" }
function Fail([string]$Text) {
    Write-Host "[Incremind] $Text" -ForegroundColor Red
    exit 1
}
function Test-Ready([string]$Url) {
    try { return (Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2).StatusCode -eq 200 } catch { return $false }
}
function Test-Listening([int]$Port) {
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}
function Show-Log([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        Write-Host "---- $Path（最后 30 行）----"
        Get-Content -LiteralPath $Path -Tail 30 -Encoding UTF8 | ForEach-Object { Write-Host $_ }
    }
}
function Stop-Tree($Process) {
    if ($Process -and -not $Process.HasExited) {
        & taskkill.exe /PID $Process.Id /T /F 2>&1 | Out-Null
    }
}

# 已经在运行：只打开浏览器。
if ((Test-Ready $HealthUrl) -and (Test-Ready $AppUrl)) {
    Say "已经在运行：$AppUrl"
    if (-not $NoBrowser) { Start-Process $AppUrl }
    exit 0
}
foreach ($port in 8001, 4173) {
    if (Test-Listening $port) { Fail "端口 $port 被其他程序占用。先关掉占用它的程序，再重新运行。" }
}

# Python 3.12 环境。
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    Say '第一次运行：创建 Python 3.12 环境……'
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        & py.exe -3.12 -m venv (Join-Path $Root '.venv')
    } elseif ((Get-Command python.exe -ErrorAction SilentlyContinue) -and
              (& python.exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null) -eq '3.12') {
        & python.exe -m venv (Join-Path $Root '.venv')
    }
    if (-not (Test-Path -LiteralPath $Python)) {
        Fail '需要 Python 3.12：https://www.python.org/downloads/ （安装时勾选 Add python.exe to PATH），装好后重新运行。'
    }
}
$PyMarker = Join-Path $Root '.venv\.incremind-installed'
$PyInputs = 'requirements-web.txt', 'requirements-memory.txt' | ForEach-Object { Join-Path $Root $_ }
$PyStale = -not (Test-Path -LiteralPath $PyMarker)
if (-not $PyStale) {
    $installed = (Get-Item -LiteralPath $PyMarker).LastWriteTime
    $PyStale = [bool]($PyInputs | Where-Object { (Get-Item -LiteralPath $_).LastWriteTime -gt $installed })
}
if ($PyStale) {
    Say '安装 Python 依赖（第一次需要几分钟）……'
    & $Python -m pip install --disable-pip-version-check -r (Join-Path $Root 'requirements-web.txt')
    if ($LASTEXITCODE -ne 0) { Fail 'Python 依赖安装失败，原因见上面的输出。' }
    Set-Content -LiteralPath $PyMarker -Value (Get-Date -Format o)
}

# 网页依赖。
$Node = Get-Command node.exe -ErrorAction SilentlyContinue
if (-not $Node -or -not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    Fail '需要 Node.js 22 LTS（或 20.19 以上）：https://nodejs.org/ ，装好后重新运行。'
}
$NodeMarker = Join-Path $Frontend 'node_modules\.package-lock.json'
$Lock = Join-Path $Frontend 'package-lock.json'
if (-not (Test-Path -LiteralPath $NodeMarker) -or
    (Get-Item -LiteralPath $Lock).LastWriteTime -gt (Get-Item -LiteralPath $NodeMarker).LastWriteTime) {
    Say '安装网页依赖……'
    Push-Location $Frontend
    try { & npm.cmd ci --no-audit --no-fund } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { Fail '网页依赖安装失败，原因见上面的输出。' }
}

# 数据目录与配置。
New-Item -ItemType Directory -Force -Path (Join-Path $DataRoot 'config'), $LogDir | Out-Null
$Settings = Join-Path $DataRoot 'config\settings.toml'
if (-not (Test-Path -LiteralPath $Settings)) {
    $source = Join-Path $Root 'config\settings.toml'
    if (-not (Test-Path -LiteralPath $source)) { $source = Join-Path $Root 'config\settings.toml.example' }
    Copy-Item -LiteralPath $source -Destination $Settings
}
$FirstUse = -not (Test-Path -LiteralPath (Join-Path $DataRoot 'secrets.json'))

$env:PYTHONPATH = Join-Path $Root 'src'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:CHRIPTMAS_APP_ROOT = $DataRoot
$env:CHRIPTMAS_REPLAY_AGENT_LANCEDB_URI = Join-Path $DataRoot 'data\agent_graph\lancedb'

Say "数据目录：$DataRoot"
Say '启动中，第一次大约需要半分钟……'
$BackendLog = Join-Path $LogDir 'backend.log'
$UiLog = Join-Path $LogDir 'ui.log'
$backend = $null
$ui = $null
try {
    $backend = Start-Process -FilePath $Python -WorkingDirectory $Root -NoNewWindow -PassThru `
        -ArgumentList '-m', 'uvicorn', 'backend.memory_app.app:app', '--host', '127.0.0.1', '--port', '8001' `
        -RedirectStandardOutput (Join-Path $LogDir 'backend.out.log') -RedirectStandardError $BackendLog
    $ui = Start-Process -FilePath $Node.Source -WorkingDirectory $Frontend -NoNewWindow -PassThru `
        -ArgumentList 'node_modules\vite\bin\vite.js', '--host', '127.0.0.1', '--port', '4173', '--strictPort' `
        -RedirectStandardOutput $UiLog -RedirectStandardError (Join-Path $LogDir 'ui.err.log')

    $deadline = (Get-Date).AddMinutes(3)
    while (-not ((Test-Ready $HealthUrl) -and (Test-Ready $AppUrl))) {
        if ($backend.HasExited) { Show-Log $BackendLog; Fail '后端没能启动，原因见上面的日志。' }
        if ($ui.HasExited) { Show-Log $UiLog; Fail '网页没能启动，原因见上面的日志。' }
        if ((Get-Date) -gt $deadline) { Show-Log $BackendLog; Fail '等了 3 分钟还没就绪，原因见上面的日志。' }
        Start-Sleep -Milliseconds 500
    }

    Write-Host ''
    Say "已启动：$AppUrl"
    if ($FirstUse) { Say '第一次使用：在 设置 · 模型 里填入模型的 API Key（例如 DeepSeek）。' }
    Say "日志：$LogDir"
    Say '关闭这个窗口或按 Ctrl+C 即停止。'
    if (-not $NoBrowser) { Start-Process $AppUrl }

    while (-not $backend.HasExited -and -not $ui.HasExited) { Start-Sleep -Seconds 1 }
    if ($backend.HasExited) { Show-Log $BackendLog; Fail '后端意外退出，原因见上面的日志。' }
    Show-Log $UiLog
    Fail '网页意外退出，原因见上面的日志。'
} finally {
    Stop-Tree $ui
    Stop-Tree $backend
}
