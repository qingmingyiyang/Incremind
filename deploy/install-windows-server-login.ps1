param(
    [string]$PythonDirectory = (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312')
)

$ErrorActionPreference = 'Stop'
$launcherDirectory = Join-Path $env:LOCALAPPDATA 'Incremind\server-connection'
$pythonWindow = [System.IO.Path]::GetFullPath((Join-Path $PythonDirectory 'pythonw.exe'))
$pythonConsole = [System.IO.Path]::GetFullPath((Join-Path $PythonDirectory 'python.exe'))
$sourcePath = Join-Path $PSScriptRoot 'windows-server-login.py'
$desktopDirectory = [Environment]::GetFolderPath('DesktopDirectory')
# 解析 MSIX 和目录联接的实际路径，供 Explorer 启动的非包进程使用。
$resolvedPathsJson = & $pythonConsole -I -c 'import json, os, sys; print(json.dumps([os.path.realpath(path) for path in sys.argv[1:]]))' $pythonConsole $pythonWindow $launcherDirectory $sourcePath $desktopDirectory
if ($LASTEXITCODE -ne 0) {
    throw 'Could not resolve actual login launcher paths.'
}
$resolvedPaths = $resolvedPathsJson | ConvertFrom-Json
if ($resolvedPaths.Count -ne 5) {
    throw 'Unexpected actual login launcher paths.'
}
$pythonConsole, $pythonWindow, $launcherDirectory, $sourcePath, $desktopDirectory = $resolvedPaths
$installedPath = Join-Path $launcherDirectory 'windows-server-login.py'
foreach ($required in @($pythonWindow, $pythonConsole, $sourcePath,
    (Join-Path $launcherDirectory 'connection.json'),
    (Join-Path $launcherDirectory 'known_hosts'),
    (Join-Path $launcherDirectory 'ssh_run.py'))) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required launcher file is missing: $required"
    }
}

Copy-Item -LiteralPath $sourcePath -Destination $installedPath -Force
$shortcutWriter = New-Object -ComObject WScript.Shell
$launchers = @(
    @{ Name='Incremind 登录.lnk'; Flag='' },
    @{ Name='Incremind 服务器.lnk'; Flag=' --open' },
    @{ Name='Incremind 服务器首次配对.lnk'; Flag=' --pair' },
    @{ Name='Incremind 首次登录.lnk'; Flag=' --pair' }
)
foreach ($launcher in $launchers) {
    $shortcut = $shortcutWriter.CreateShortcut((Join-Path $desktopDirectory $launcher.Name))
    # 直接启动绝对路径 pythonw，避免命令解释器的路径和启动钩子影响。
    $shortcut.TargetPath = $pythonWindow
    $shortcut.Arguments = '-I "' + $installedPath + '"' + $launcher.Flag
    $shortcut.WorkingDirectory = $launcherDirectory
    $shortcut.Description = 'Incremind server login'
    $shortcut.WindowStyle = 1
    $shortcut.Save()
    $shortcutPath = Join-Path $desktopDirectory $launcher.Name
    if (-not (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) {
        throw "Shortcut was not saved: $shortcutPath"
    }
    if ((Get-Item -LiteralPath $shortcutPath).Length -le 0) {
        throw "Shortcut is empty: $shortcutPath"
    }
}
# 独立批处理入口用于确认桌面分派，并保留控制台诊断结果。
$repairPath = Join-Path $desktopDirectory 'Incremind 登录修复.cmd'
$batchMarkerPath = (Join-Path $launcherDirectory 'batch-started.txt').Replace('%', '%%')
$batchErrorPath = (Join-Path $launcherDirectory 'bootstrap-error.txt').Replace('%', '%%')
$batchPythonPath = $pythonConsole.Replace('%', '%%')
$batchScriptPath = $installedPath.Replace('%', '%%')
$batchDirectory = $launcherDirectory.Replace('%', '%%')
$repairLines = @(
    '@echo off',
    'setlocal DisableDelayedExpansion',
    ('if not exist "' + $batchDirectory + '\." goto missing_directory'),
    ('if not exist "' + $batchPythonPath + '" goto missing_python'),
    ('if not exist "' + $batchScriptPath + '" goto missing_script'),
    ('>"' + $batchMarkerPath + '" echo batch_started %DATE% %TIME%'),
    'echo Incremind login repair started. Keep this window open.',
    ('"' + $batchPythonPath + '" -I "' + $batchScriptPath + '" --pair 2>"' + $batchErrorPath + '"'),
    'set "incremind_login_exit=%ERRORLEVEL%"',
    'goto finished',
    ':missing_directory',
    'echo Login connection directory is missing.',
    'set "incremind_login_exit=2"',
    'goto finished',
    ':missing_python',
    'echo Login Python executable is missing.',
    'set "incremind_login_exit=3"',
    'goto finished',
    ':missing_script',
    'echo Login script is missing.',
    'set "incremind_login_exit=4"',
    ':finished',
    'echo Incremind login finished. Exit code: %incremind_login_exit%',
    'echo If no login window appeared, report the exit code.',
    'pause',
    'exit /b %incremind_login_exit%'
)
$repairText = ($repairLines -join "`r`n") + "`r`n"
if ($repairText -match '[^\x00-\x7F]') {
    throw 'Login repair command requires ASCII paths.'
}
[System.IO.File]::WriteAllText($repairPath, $repairText, [System.Text.Encoding]::ASCII)
if (-not (Test-Path -LiteralPath $repairPath -PathType Leaf)) {
    throw "Login repair command was not saved: $repairPath"
}
if ((Get-Item -LiteralPath $repairPath).Length -le 0) {
    throw "Login repair command is empty: $repairPath"
}
# 旧版一次性入口会过期，改用每次请求新码的快捷方式。
foreach ($expiredEntry in @(
    (Join-Path $desktopDirectory 'Incremind 首次登录.url'),
    (Join-Path $launcherDirectory 'first-login.html')
)) {
    if (Test-Path -LiteralPath $expiredEntry -PathType Leaf) {
        Remove-Item -LiteralPath $expiredEntry
    }
}
Write-Output 'Installed Incremind login shortcuts.'
