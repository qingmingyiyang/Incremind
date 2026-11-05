# Incremind Android

APK 复用服务器网页和现有分享接口。首次启用在 App 内扫描管理员签发的邀请。HTTPS 双向认证只向构建时指定的服务器提供该设备证书。私钥在 Android Keystore 内生成，待发送内容和回执存储在 App 私有目录。分享超时会沿同一提交编号重试，服务器要求核对时可打开工作台查看。

## 构建

使用 Android SDK 36、JDK 21 和 Gradle 9.1。指定已部署的 HTTPS 入口根地址，不附带端口或路径。App 通过标准 HTTPS 端口兑换邀请，通过 8443 使用工作台和上传。

```powershell
gradle :app:assembleDebug -PincremindOrigin=https://example.invalid
```

## 私有发行

在 Windows PowerShell 7.3 或更新版本运行 `release.ps1`。先在隔离工作树固定候选提交，再从私有配置提供服务器地址、预期签名证书指纹和现有签名保管目录。版本号与构建号由 `gradle.properties` 统一声明。

```powershell
$release = @{
    Origin = $privateOrigin
    ExpectedCommit = $approvedCommit
    ExpectedSignerSha256 = $registeredSigner
    SigningCustody = $existingDpapiDirectory
}
./release.ps1 @release -CheckOnly
./release.ps1 @release
```

签名保管目录包含当前 Windows 用户可解密的 `password.dpapi` 和原发行 `incremind.p12`。自动化也可以通过进程环境提供 `INCREMIND_ANDROID_SIGNING_STORE` 和 `INCREMIND_ANDROID_SIGNING_PASSWORD`，并省略 `SigningCustody`。别名默认 `incremind`，可通过 `INCREMIND_ANDROID_SIGNING_ALIAS` 指定。升级沿用服务端登记的发行证书。

脚本从 `JAVA_HOME` 和 Android SDK 读取工具。可传入 `-GradlePath` 指定已安装的 Gradle 9.1.0，否则按环境、PATH 和现有 Wrapper 缓存查找。预检会核对来源提交、版本、工具、目标地址格式、签名与归档占用，仅执行读取操作。

发行命令通过 Android SDK 校验安装包及签名，并写入 `work/android-releases/<版本>-<构建号>`，也可用 `-ArchiveRoot` 指定私有绝对路径。每次发行保留 APK、`release.json` 和独立的 `validation.json`。已有版本会拒绝覆盖。验证记录分别跟踪安装、公网激活、分享补传和撤销，需在实际验收后更新。
