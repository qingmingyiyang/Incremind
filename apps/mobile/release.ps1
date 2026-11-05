#Requires -Version 7.3
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Origin,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedCommit,
    [Parameter(Mandatory)][string]$ExpectedSignerSha256,
    [string]$SigningCustody,
    [string]$GradlePath,
    [string]$ArchiveRoot,
    [switch]$CheckOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$mobileRoot = $PSScriptRoot
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $mobileRoot '../..')).Path

function Confirm-Source {
    $commit = git --no-optional-locks -C $repoRoot rev-parse --verify HEAD
    if ($LASTEXITCODE -ne 0 -or $commit -cne $ExpectedCommit) { throw '当前提交与发行候选不一致。' }
    $trackedChanges = git --no-optional-locks -C $repoRoot status --porcelain=v1 --untracked-files=no
    if ($LASTEXITCODE -ne 0 -or $trackedChanges) { throw '发行候选必须使用干净的已提交源码。' }
    $untrackedApp = git --no-optional-locks -C $repoRoot ls-files --others --exclude-standard -- apps/mobile
    if ($LASTEXITCODE -ne 0 -or $untrackedApp) { throw 'Android 源码目录包含未提交文件。' }
}

function Resolve-Gradle {
    if ($GradlePath) { $candidate = $GradlePath }
    elseif ($env:GRADLE_HOME) { $candidate = Join-Path $env:GRADLE_HOME 'bin/gradle.bat' }
    else {
        $command = Get-Command gradle.bat -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) { $candidate = $command.Source }
        else {
            $cache = Join-Path $HOME '.gradle/wrapper/dists/gradle-9.1.0-all'
            if (!(Test-Path -LiteralPath $cache -PathType Container)) { throw '未找到 Gradle 9.1.0，请指定 GradlePath。' }
            $candidates = @(Get-ChildItem -LiteralPath $cache -Directory |
                ForEach-Object { Join-Path $_.FullName 'gradle-9.1.0/bin/gradle.bat' } |
                Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
            if ($candidates.Count -ne 1) { throw '请指定可用的 Gradle 9.1.0 路径。' }
            $candidate = $candidates[0]
        }
    }
    $gradle = (Resolve-Path -LiteralPath $candidate).Path
    $lib = Join-Path (Split-Path -Parent (Split-Path -Parent $gradle)) 'lib'
    if ((Split-Path -Leaf $gradle) -ne 'gradle.bat' -or
        !(Test-Path -LiteralPath (Join-Path $lib 'gradle-launcher-9.1.0.jar') -PathType Leaf)) {
        throw '发行构建要求 Gradle 9.1.0。'
    }
    return $gradle
}

$endpoint = $null
if (![Uri]::TryCreate($Origin, [UriKind]::Absolute, [ref]$endpoint) -or
    $endpoint.Scheme -ne 'https' -or $endpoint.HostNameType -notin 'IPv4', 'Dns' -or
    $Origin -cne "https://$($endpoint.IdnHost)" -or $endpoint.IdnHost -eq 'example.invalid') {
    throw '请提供实际 HTTPS 根地址，省略端口、路径和凭据。'
}
$signer = ($ExpectedSignerSha256 -replace ':', '').ToUpperInvariant()
if ($signer -cnotmatch '^[0-9A-F]{64}$') { throw '签名证书指纹必须是 64 位 SHA-256 十六进制。' }

Confirm-Source
$properties = Get-Content -Raw -LiteralPath (Join-Path $mobileRoot 'gradle.properties')
$nameMatch = [regex]::Matches($properties, '(?m)^incremindVersionName=([0-9]+\.[0-9]+\.[0-9]+)\r?$')
$codeMatch = [regex]::Matches($properties, '(?m)^incremindVersionCode=([1-9][0-9]*)\r?$')
if ($nameMatch.Count -ne 1 -or $codeMatch.Count -ne 1) { throw '发行版本配置无效。' }
$versionName = $nameMatch[0].Groups[1].Value
$versionCode = [int]$codeMatch[0].Groups[1].Value
$versionLabel = "$versionName-$versionCode"
if (!$ArchiveRoot) { $ArchiveRoot = Join-Path $repoRoot 'work/android-releases' }
if (![IO.Path]::IsPathFullyQualified($ArchiveRoot)) { throw '请指定绝对归档目录。' }
$ArchiveRoot = [IO.Path]::GetFullPath($ArchiveRoot)
$relativeArchive = [IO.Path]::GetRelativePath($repoRoot, $ArchiveRoot).Replace('\', '/')
if (![IO.Path]::IsPathRooted($relativeArchive) -and $relativeArchive -ne '..' -and !$relativeArchive.StartsWith('../')) {
    git --no-optional-locks -C $repoRoot check-ignore --quiet -- "$relativeArchive/"
    if ($LASTEXITCODE -ne 0) { throw '仓库内的归档目录必须被 Git 忽略。' }
}
$final = Join-Path $ArchiveRoot $versionLabel
if (Test-Path -LiteralPath $final) { throw '此发行版本的归档已存在，请核对现有记录。' }

$gradle = Resolve-Gradle
if (!$env:JAVA_HOME -or !(Test-Path -LiteralPath (Join-Path $env:JAVA_HOME 'release') -PathType Leaf) -or
    !(Select-String -LiteralPath (Join-Path $env:JAVA_HOME 'release') -Pattern '^JAVA_VERSION="21(?:\.|")' -Quiet)) {
    throw '请将 JAVA_HOME 指向 JDK 21。'
}
$keytool = Join-Path $env:JAVA_HOME 'bin/keytool.exe'
$sdk = if ($env:ANDROID_HOME) { $env:ANDROID_HOME } else { $env:ANDROID_SDK_ROOT }
if (!$sdk) { throw '请配置 Android SDK 目录。' }
$aapt = Join-Path $sdk 'build-tools/36.0.0/aapt.exe'
$apksigner = Join-Path $sdk 'build-tools/36.0.0/apksigner.bat'
foreach ($required in @($keytool, $aapt, $apksigner, (Join-Path $sdk 'platforms/android-36/android.jar'))) {
    if (!(Test-Path -LiteralPath $required -PathType Leaf)) { throw 'JDK 或 Android SDK 36 工具不完整。' }
}

$signingNames = @('INCREMIND_ANDROID_SIGNING_STORE', 'INCREMIND_ANDROID_SIGNING_PASSWORD', 'INCREMIND_ANDROID_SIGNING_ALIAS')
$previous = @{}
foreach ($name in $signingNames) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
try {
    if ($SigningCustody) {
        if ($previous[$signingNames[0]] -or $previous[$signingNames[1]]) { throw '请只提供一种发行签名来源。' }
        $custody = (Resolve-Path -LiteralPath $SigningCustody).Path
        $env:INCREMIND_ANDROID_SIGNING_STORE = Join-Path $custody 'incremind.p12'
        $passwordBlob = Get-Content -Raw -LiteralPath (Join-Path $custody 'password.dpapi')
        $protectedPassword = ConvertTo-SecureString $passwordBlob.Trim()
        $env:INCREMIND_ANDROID_SIGNING_PASSWORD = ConvertFrom-SecureString $protectedPassword -AsPlainText
        $protectedPassword.Dispose()
    }
    if (!$env:INCREMIND_ANDROID_SIGNING_STORE -or !$env:INCREMIND_ANDROID_SIGNING_PASSWORD) {
        throw '请通过进程环境或现有 DPAPI 保管目录提供发行签名。'
    }
    $env:INCREMIND_ANDROID_SIGNING_STORE = (Resolve-Path -LiteralPath $env:INCREMIND_ANDROID_SIGNING_STORE).Path
    $alias = if (![string]::IsNullOrWhiteSpace($env:INCREMIND_ANDROID_SIGNING_ALIAS)) { $env:INCREMIND_ANDROID_SIGNING_ALIAS } else { 'incremind' }
    $env:INCREMIND_ANDROID_SIGNING_ALIAS = $alias
    $storeArgs = @('-J-Duser.language=en', '-J-Duser.country=US', '-list', '-v',
        '-keystore', $env:INCREMIND_ANDROID_SIGNING_STORE, '-alias', $alias, '-storepass:env', 'INCREMIND_ANDROID_SIGNING_PASSWORD')
    $storeInfo = & $keytool @storeArgs 2>&1
    if ($LASTEXITCODE -ne 0) { throw '无法读取指定的发行签名。' }
    $storeText = $storeInfo -join [Environment]::NewLine
    $storeSigner = [regex]::Match($storeText, '(?m)^\s*SHA256:\s*([0-9A-F:]+)').Groups[1].Value -replace ':', ''
    if ($storeText -notmatch '(?m)^Entry type: PrivateKeyEntry\s*$' -or $storeSigner -cne $signer) {
        throw '签名别名缺少私钥或证书与预期不符。'
    }
    if ($CheckOnly) {
        [pscustomobject]@{
            CheckOnly = $true; Commit = $ExpectedCommit; Version = $versionLabel
            Origin = $Origin; SignerSha256 = $signer; ArchiveAvailable = $true
        }
        return
    }

    $buildRoot = Join-Path (Join-Path $repoRoot 'work/android-release-builds') ([Guid]::NewGuid().ToString('N'))
    $buildArgs = @('-p', $mobileRoot, '--no-daemon', '--console=plain', ':app:privateReleaseApk', ':app:lintRelease',
        "-PincremindOrigin=$Origin", "-PincremindReleaseBuildDir=$buildRoot",
        "-PincremindVersionName=$versionName", "-PincremindVersionCode=$versionCode")
    & $gradle @buildArgs
    if ($LASTEXITCODE -ne 0) { throw 'Android 发行构建或 Lint 检查失败。' }
    $buildConfig = Join-Path $buildRoot 'generated/source/buildConfig/release/app/incremind/mobile/BuildConfig.java'
    $compiledOrigins = @(Select-String -LiteralPath $buildConfig -Pattern '\bSERVER_ORIGIN\b')
    $expectedOriginLine = 'public static final String SERVER_ORIGIN = "' + $Origin + '";'
    if ($compiledOrigins.Count -ne 1 -or $compiledOrigins[0].Line.Trim() -cne $expectedOriginLine) {
        throw '生成的发行服务器地址与预期不符。'
    }
    $packages = @(Get-ChildItem -LiteralPath (Join-Path $buildRoot 'outputs/apk/release') -Filter '*.apk' -File)
    if ($packages.Count -ne 1) { throw '发行构建没有生成唯一安装包。' }
    $apk = $packages[0].FullName
    $badging = & $aapt dump badging $apk 2>&1
    $expectedBadging = "package: name='app.incremind.mobile' versionCode='$versionCode' versionName='$versionName'"
    if ($LASTEXITCODE -ne 0 -or !($badging | Where-Object { ([string]$_).StartsWith($expectedBadging) })) {
        throw '安装包的应用标识或版本不符合发行候选。'
    }
    $verified = & $apksigner verify --verbose --print-certs $apk 2>&1
    $apkSigners = [regex]::Matches(($verified -join [Environment]::NewLine), '(?im)^Signer #[0-9]+ certificate SHA-256 digest:\s*([a-f0-9]{64})\s*$')
    if ($LASTEXITCODE -ne 0 -or $apkSigners.Count -ne 1 -or $apkSigners[0].Groups[1].Value.ToUpperInvariant() -cne $signer) {
        throw '安装包签名验证失败。'
    }
    Confirm-Source
    $stage = Join-Path $ArchiveRoot ('.pending-' + [Guid]::NewGuid().ToString('N'))
    [void][IO.Directory]::CreateDirectory($stage)
    $apkName = "Incremind-Android-$versionLabel.apk"
    [IO.File]::Copy($apk, (Join-Path $stage $apkName), $false)
    [ordered]@{
        commit = $ExpectedCommit; application_id = 'app.incremind.mobile'; version_name = $versionName
        version_code = $versionCode; origin = $Origin; signer_sha256 = $signer; apk = $apkName
        archived_at = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stage 'release.json') -Encoding utf8
    [ordered]@{
        package = 'verified'; signature = 'verified'; lint = 'verified'; device_install = 'pending'; public_activation = 'pending'
        share_and_retry = 'pending'; revocation = 'pending'
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stage 'validation.json') -Encoding utf8
    # 在同一目录内发布完整归档，目标已存在时移动会失败。
    [IO.Directory]::Move($stage, $final)
    Write-Output $final
} finally {
    foreach ($name in $signingNames) {
        Set-Item -LiteralPath "Env:$name" -Value $previous[$name]
    }
}
