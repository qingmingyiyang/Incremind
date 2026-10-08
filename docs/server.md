# 服务器部署

> 🧪 实验性：Linux 服务器形态有完整的部署模板和自动化测试，但还没有在真实服务器上做过完整验收。个人使用推荐电脑本机形态（[README 快速开始](../README.md#快速开始)）。

同一套程序有两种形态，用 `CHRIPTMAS_DEPLOY` 区分：

| 形态 | 用途 | 数据根 |
|---|---|---|
| `desktop`（默认） | 电脑本机，一个用户，不需要设备钥匙 | 仓库下的 `runtime/`，或 `CHRIPTMAS_APP_ROOT` |
| `server` | 家庭服务器或云服务器，多用户，每个请求都要设备钥匙 | 必须显式设置 `CHRIPTMAS_APP_ROOT` |

server 形态的数据布局：`<服务器根>/server/` 放用户、设备和服务器级配置；`<服务器根>/users/<用户>/` 是每个用户完整的数据根，模型配置和密钥也在里面。

## 安装与启动

在 Linux 上建立 Python 3.12 虚拟环境，安装 `requirements-web.txt`；前端在 `src/frontend` 下执行 `npm ci` 和 `npm run build`。从仓库根目录启动：

```sh
export PYTHONPATH="$PWD/src"
export CHRIPTMAS_DEPLOY=server
export CHRIPTMAS_APP_ROOT=/var/lib/incremind
.venv/bin/python -m backend.memory_app.serve --port 8001
```

- 应用只监听 `127.0.0.1`。server 形态由同一进程托管 `src/frontend/dist`，网页和接口同源；缺少前端构建产物时拒绝启动。
- HTTPS 和对外端口交给前置的反向代理。`deploy/Caddyfile.lan` 用局域网自签证书（客户端需要信任它的 CA），`deploy/Caddyfile.public` 用域名占位符。两份模板都代理到 `127.0.0.1:8001`，并关闭响应缓冲，流式回答不会被攒着不发。
- `deploy/chriptmas.service` 以低权限用户运行，通过 systemd `LoadCredential` 读取主密钥 `chriptmas-master-key`，也支持环境变量 `CHRIPTMAS_SERVER_MASTER_KEY`。主密钥是 Fernet 格式，不要放进仓库、日志或备份。
- 用户的模型密钥用主密钥加密存在各自的数据根里，文件权限 0600，保存后谁都读不回来；缺主密钥时拒绝保存。Windows 上沿用 DPAPI。
- Linux 上的本机识图可选 [RapidOCR](https://github.com/RapidAI/RapidOCR)（Apache-2.0）：安装 `requirements-vision.txt`，把 `det.onnx`、`cls.onnx`、`rec.onnx`、`keys.txt`、`font.ttf` 预先放到用户数据根的 `data/models/rapidocr/`。程序不会自行下载。

## 配对设备

在服务器本机生成第一台管理员设备的配对链接（十分钟内有效，只能用一次）：

```sh
.venv/bin/python tools/server_admin.py pair --root /var/lib/incremind --url https://你的域名
```

链接只显示在本机终端，不写日志。用浏览器打开或扫码，填设备名称完成配对；设备钥匙只在兑换时返回一次。之后在 **设置 · 设备** 里添加或作废设备，作废立即生效。二维码在本地生成，不调用外部服务。

## 备份与恢复

备份复用在线 SQLite 快照，产物带 UTC 日期；可以备份整个服务器，也可以只备份一个用户：

```sh
.venv/bin/python tools/backup.py --root /var/lib/incremind --output /var/backups/incremind
.venv/bin/python tools/backup.py --root /var/lib/incremind --user local-user --output /var/backups/incremind
.venv/bin/python tools/backup.py --restore /var/backups/incremind/SNAPSHOT --to /var/lib/incremind-restored
```

- 恢复目标必须是新目录，已存在的目录（包括空目录）一律拒绝。
- 服务器主密钥不在快照里；恢复后仍需要原主密钥。Windows 跨机器恢复后需要重新填模型密钥。
- `deploy/chriptmas-backup.service` 和 `chriptmas-backup.timer` 是每日备份的模板。
