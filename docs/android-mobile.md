# Android 安装与授权

Android 安装包使用现有工作台。所有人安装同一个签名 APK，由服务器管理员给每个普通账号分别发放设备邀请。APK 包含服务器公开地址。手机扫码时在 Android Keystore 内生成密钥，验证通过的设备获得独立客户端证书。换机或卸载重装需要新邀请。

## 安装和分享

1. 安装管理员交付的签名 APK，打开 Incremind，在登录首页点扫描邀请。
2. 管理员在网页的设置 · 设备打开对应的普通账号，生成 Android 邀请。私下发送二维码，接收者在 App 内扫码。邀请有效期为 10 分钟，只能兑换一次。
3. 授权成功后打开工作台。日常从其他 App 的系统分享菜单选 Incremind，即可上传文字、链接、图片和文件。待发送列表显示本次上传状态，点原件回执打开工作台。整理状态继续由工作台显示。
4. 断网时 App 保留队列与提交编号，联网后可继续发送。设备已作废、账号停用或重新配发时，旧队列停止发送。管理员可在设置 · 设备作废单台手机。

首次激活前收到的分享会保存在手机。扫码后确认页面显示的接收账号，即可继续发送。连接失败时可重试或重新扫码，也可从分享记录查看已保存的内容。已兑换的新邀请可在网络恢复后继续激活，核验通过前可连接仍有效的原设备。原分享保持原账号和设备归属。

工作台加载完成后，App 会收起顶部连接区。需要查看分享记录时，在手机桌面长按 Incremind 图标，选择分享记录。

手机需支持受信任的 Android 硬件密钥证明。被拒绝时先在目标机型核对系统和硬件支持。证明失败无法领取证书，网页账号仍可按原方式使用。[Android 硬件密钥证明](https://developer.android.com/privacy-and-security/security-key-attestation)

## 服务器启用

继续使用受信任的公网 IPv4 HTTPS 证书。浏览器与 App 初次激活通过 443。已配发 App 的工作台及上传通过 8443，Nginx 验证手机证书后转发至现有回环服务。两处使用同一公网服务器证书，Certbot 原有续期和 Nginx 重载同时覆盖两处。

1. 准备发行版 APK 和稳定保管的 Android 发布签名。使用 Android SDK 的 `apksigner verify --print-certs` 核对 APK，记录已发布应用的 application ID 和签名证书的 64 位 SHA-256 十六进制值。更新 App 需要沿用该发布签名。签名私钥和密码只存于授权保管位置。[Android 应用签名](https://developer.android.com/studio/publish/app-signing)
2. 在服务器使用下列命令安装并初始化本机配置。将 `APP_ID` 和 `SIGNER_SHA256` 替换为经核对的发行版信息。

   ```sh
   sudo install -D -m 0755 deploy/android-mobile.py /usr/local/libexec/incremind/android-mobile.py
   sudo python3 /usr/local/libexec/incremind/android-mobile.py init APP_ID SIGNER_SHA256
   sudo install -D -m 0644 deploy/chriptmas-android-mobile.conf /etc/systemd/system/chriptmas.service.d/android-mobile.conf
   ```

   初始化将独立客户端 CA、Nginx 转发凭据、Android 信任根和撤销列表写入 `/etc/chriptmas/android-mobile`。应用服务账号只读，Nginx 转发片段仅管理员可读。保持这些文件的权限，将客户端 CA 私钥及相关配置纳入受控服务器备份。浏览器入口模板清除外来内部认证头，只有 8443 代理设置认证头。后端继续只监听回环。

3. 将 [独立移动入口模板](../deploy/nginx-android-mobile.conf) 安装为 Nginx 站点，将 `SERVER_IP` 换成当前公网地址。两条 `ssl_certificate` 路径与已上线的公网入口一致。更新现有 [443 模板](../deploy/nginx-ip.conf) 的两条内部认证头清除规则，保留公网地址、证书路径及原站点行为。确认云安全组和主机防火墙允许 TCP 8443，再启用新站点。先通过 `sudo nginx -t`，再执行 `sudo systemctl daemon-reload`、`sudo systemctl restart chriptmas` 和 `sudo systemctl reload nginx`。
4. 在测试账号发放邀请前，确认原网页在 443 仍可登录和上传。用普通浏览器打开 8443 应因缺少客户端证书而被拒绝。验收手机使用 443 扫码后，再验证 8443 工作台和系统分享。需要回退时先关闭 8443 站点，再移除应用服务的 Android 覆盖配置并重载 Nginx、重启应用，保留授权记录与 CA 备份。

## 维护 Android 撤销列表

服务器可访问 Google 官方 HTTPS 时，安装定时更新服务。服务每天检查两次，使用同目录原子替换；下载或校验失败时保留已有列表，应用拒绝用超过 48 小时的列表接受新设备。[Google 官方撤销列表说明](https://developer.android.com/privacy-and-security/security-key-attestation#certificate_revocation_status_list)

```sh
sudo install -m 0644 deploy/incremind-android-attestation.service /etc/systemd/system/
sudo install -m 0644 deploy/incremind-android-attestation.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now incremind-android-attestation.timer
```

服务器无法连接 Google 时，在可信的可联网电脑运行 `python deploy/android-mobile.py snapshot SNAPSHOT_DIR`。经受信任的 SSH 会话使用 `scp -p -r` 把整个快照目录传入服务器上的管理员专用目录，保留源时间戳。首次初始化追加 `--source SNAPSHOT_DIR`，日常执行 `sudo python3 /usr/local/libexec/incremind/android-mobile.py refresh --source SNAPSHOT_DIR`。至少每日传入一次新快照，导入端拒绝超过 36 小时的文件及早于已安装列表的快照。
