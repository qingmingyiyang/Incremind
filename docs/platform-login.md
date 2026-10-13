# 手机分享与网站登录

在小红书或 B 站分享视频、笔记时，通过系统分享入口或复制链接送进工作台。平台登录态在 设置 · 网站 管理。每位用户空间分别保存，导入后该用户下一次读取对应站点时自动使用；未保存登录态时继续原有公开读取。

Android 安装网页应用、iPhone 快捷指令和多图视频的发送步骤见 [手机分享](mobile-share.md)。手机只需完成导入或扫码，后续资料读取由服务器使用已保存登录态完成。

## Cookie 导入

展开 B站 或 小红书，可以粘贴 Cookie 请求头并点 导入，也可以选择 `cookies.txt` 或浏览器导出的 Cookie JSON 文件。文件选择后直接导入，只接受当前所选平台的域，文件最大 64 KiB，最多 200 条。JSON 支持 Cookie 数组或带 `cookies` 数组的对象；Netscape 文件支持 `#HttpOnly_` 行。已经过期的条目不会使用，全部过期时拒绝导入。

导入使用页面读取到的修订替换登录态；格式、域、有效期或修订检查失败时保留旧值。删除登录移除当前登录态，不删除已存入的资料。Cookie 的使用期由网站决定，网站要求重新认证时需要更新。

## 真实扫码

B站 使用 [B站 Passport 实际签发接口](https://passport.bilibili.com/x/passport-login/web/qrcode/generate) 生成二维码，由 B站 App 扫码并确认。页面最多等待 180 秒，每次轮询间隔至少 3 秒，最多 60 次。可随时关闭；过期后重新生成。同一部手机使用时，可以长按保存二维码图片，再在对应 App 的扫一扫中从相册选择图片，前提是该 App 支持相册识码；也可以在另一块屏幕上展示二维码供手机扫描。

小红书使用独立、临时的浏览器上下文加载 [官方网页](https://www.xiaohongshu.com/explore)，运行网站自己的登录流程。服务器需要安装可选 Playwright 运行环境和 Chromium。环境不可用时扫码按钮不可用，Cookie 导入仍可使用。验证码、风险提示或平台拒绝访问时报告未完成，不模拟签名或绕过校验。

在仓库根目录使用应用的 Python 环境安装可选依赖和浏览器。Windows 命令：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-platform-login.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

Linux 服务器命令：

```sh
./.venv/bin/python -m pip install -r requirements-platform-login.txt
./.venv/bin/python -m playwright install chromium
```

缺少 Chromium 系统库时按 Playwright 的安装诊断由服务器维护者补齐；安装依赖不代表已经完成平台登录。浏览器资产留在服务器复用，无需下载到手机。

二维码会话绑定当前用户空间与设备，不使用已有浏览器资料。扫码确认之后，只有加密保存和修订检查完成才显示 已保存。扫码开始后手动更新登录态，会使旧二维码的保存失败，避免覆盖新登录态。关闭应用会清理未完成的登录会话，重新打开时可重新扫码。

## 保存和读取边界

Cookie 只进入原有 `ModelConfiguration.secrets`，Windows 使用 DPAPI，服务器使用已有服务器密钥存储。SQLite 旁路集合只保存秘密引用、保存时间和修订；接口不返回保存值，前端不写浏览器存储，Cookie 不进入原件、回执、日志和模型输入。状态 已保存 表示本地存在未过期条目，不表示已经向平台验证了账号或保证账号持续有效。

凭据只用于 HTTPS 下的 B站 `api.bilibili.com`、`www.bilibili.com` 与小红书 `xiaohongshu.com`、`www.xiaohongshu.com`，并继续检查 Cookie 自身的域与路径。短链解析、字幕 CDN、视频 CDN 和重定向不携带 Cookie。读取到的文本或最终整理输入反射已使用的 Cookie 时，拒绝保存这一响应。

B站保持字幕优先，字幕不可用时沿用原音频转写。小红书保持页面笔记内容及原视频转写流程。登录态仅提供当前账号获准读取的内容，平台登录不会替代资料外发开关、私密规则或认识确认。

## 验证范围

已实际核验 B站官方匿名二维码生成与未扫码等待状态；已在临时数据根核验 DPAPI 加密、有限格式解析、修订冲突、设备隔离、删除、定域 Cookie 注入和反射拦截。扫码确认及 Cookie 返回的成功分支使用合成平台响应验证，没有登录真实账号，没有读取用户现有浏览器数据。

小红书匿名官方浏览器实跑得到 HTTP 200 的短异常网页，没有登录控件，也没有触发签发二维码的接口，因此没有取得真实二维码，不能声称扫码已可用。现有证据无法确定异常原因，也不能认定为验证码。实现保留重新尝试入口；当前可用的登录态提供方式是 Cookie 导入。浏览器流程的 SQLite/DPAPI、修订冲突、线程归属、取消/过期、无 Cookie 资源请求和拒绝重定向已通过离线控制，不能代替官方网络或真机账号验收。
