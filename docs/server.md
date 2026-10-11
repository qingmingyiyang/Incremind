# 整个应用部署到服务器

> 🧪 实验性：2026-10-11 已在 Ubuntu 22.04 CPU 实例验证安装构建、设备配对、共享向量加载、计算、卸载与重新加载。HTTPS、GPU、多用户隔离及服务器备份恢复仍需专项验证。

整站部署把网页、资料、对话和模型调用放在服务器，浏览器或配对设备连接它。若只想把向量计算交给服务器、资料库继续留在电脑，使用另一条独立路径：[私有服务器计算向量](vector-server.md)。整站部署不要求先安装独立向量服务。

## 运行形态与目录

同一套程序用 `CHRIPTMAS_DEPLOY` 区分运行形态：

| 形态 | 用途 | 数据根 |
|---|---|---|
| `desktop`（默认） | 电脑本机，单用户 | 仓库下的 `runtime/`，或 `CHRIPTMAS_APP_ROOT` |
| `server` | 家庭或云服务器，多用户，受设备授权保护 | 必须显式设置 `CHRIPTMAS_APP_ROOT` |

本文与既有 [chriptmas.service](../deploy/chriptmas.service) 统一使用以下路径：

| 路径 | 内容 |
|---|---|
| `/opt/chriptmas` | 源码、Python 虚拟环境、前端构建产物 |
| `/var/lib/chriptmas/server/` | 用户、设备与服务器级配置 |
| `/var/lib/chriptmas/users/<用户>/` | 各用户独立的资料、对话、模型配置、加密密钥和向量缓存 |
| `/var/lib/chriptmas/models/google-embeddinggemma-2/` | 所有用户共用的文字向量模型资产 |
| `/etc/chriptmas/master-key` | 部署者保管的服务器主密钥 |
| `/var/backups/chriptmas` | 数据备份 |

普通用户互相看不到空间。管理员可以管理用户、设备和资源，并按既有权限进入用户空间；相应访问留痕对该用户可见。私密表示不向外部模型发送，不对服务器管理员隐藏。

## 安装环境与准备目录

准备 Linux、Python 3.12、满足 [README](../README.md#快速开始) 要求的 Node.js，以及支持 `LoadCredential` 的 systemd。将包含服务器实现的源码放到 `/opt/chriptmas`，由部署者安装依赖和构建前端：

```sh
cd /opt/chriptmas
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-web.txt
npm --prefix src/frontend ci
npm --prefix src/frontend run build
```

创建首次部署使用的低权限用户和数据目录；已有同名用户时沿用：

```sh
sudo useradd --system --home-dir /var/lib/chriptmas --shell /usr/sbin/nologin chriptmas
sudo install -d -m 0700 -o chriptmas -g chriptmas /var/lib/chriptmas
```

源码、虚拟环境和 `src/frontend/dist` 应对服务用户可读、可执行，由部署者维护。用户空间由应用按需创建，不需要复制电脑正式 `runtime/` 到服务器。

### 主密钥

准备一条有效的 Fernet 主密钥，仅保存到服务器专用文件：

```sh
sudo install -d -m 0700 /etc/chriptmas
sudoedit /etc/chriptmas/master-key
sudo chmod 0600 /etc/chriptmas/master-key
```

文件中填写通过安全方式准备的密钥，不在仓库、命令行参数或日志中保存。systemd 通过 `LoadCredential=chriptmas-master-key:...` 向服务提供它。程序也支持 `CHRIPTMAS_SERVER_MASTER_KEY` 环境变量，服务模板使用凭据文件。

各用户的模型密钥以主密钥加密，文件权限为 0600，保存后不能经界面或接口读回；缺少或无效的主密钥会使密钥保存失败。恢复服务器后仍需要原主密钥，单独妥善保管。Windows 本机形态仍使用 DPAPI。

## 服务器共享向量

服务器形态中，**设置 · 模型 · 向量 · 本机** 指服务器本机算力，浏览器所在电脑不加载模型。所有用户共用一个编码器和一份模型资产，资料与向量缓存仍在各自空间内。

### 安装共享模型

CPU 实例需要文字向量时，先从 [PyTorch 官方 CPU 源](https://pytorch.org/get-started/locally/)安装 torch 与 torchvision，再由仓库维护的可选依赖清单补齐依赖，避免下载 CUDA 软件包。两步安装完成后，用同一资产安装工具指定服务器形态：

```sh
cd /opt/chriptmas
.venv/bin/python -m pip install --no-deps "torch>=2.7" "torchvision>=0.22" \
  --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python -m pip install -r requirements-local-vectors.txt
sudo -u chriptmas -H /opt/chriptmas/.venv/bin/python /opt/chriptmas/tools/fetch_model.py \
  embedding --deployment server --root /var/lib/chriptmas
```

服务器模式将资产安装到 `<服务器根>/models/google-embeddinggemma-2`。工具默认仍是电脑模式，安装到 `<数据根>/data/models`；独立向量服务也使用该默认布局。整站部署时保留 `--deployment server`，避免把模型放到应用不会读取的位置。

安装工具从官方资产派生文字模型，最终约 580 MB，256 维；服务启动不自行下载。已经准备好完整官方原始资产时可加 `--from /原始资产目录`，由原工具核验、派生和发布，该参数不接受已经裁剪的文字权重。未安装向量依赖或模型时，应用仍可使用关键词检索。

### 选择 CPU 或 GPU

[chriptmas.service](../deploy/chriptmas.service) 默认配置 `CHRIPTMAS_VECTOR_DEVICE=cpu`。可在 systemd override 中将它改为 `cuda` 或 `auto`，重启服务后生效：

```ini
[Service]
Environment=CHRIPTMAS_VECTOR_DEVICE=auto
```

`cuda` 要求兼容的 CUDA torch 包和可用设备，`auto` 在可用时选择 CUDA、否则选择 CPU。部署者按服务器驱动准备 torch，并给服务用户必要的设备访问权限。不要添加会隐藏 GPU 的 `PrivateDevices=true`。本轮没有 GPU 实测，不据此承诺显存占用或吞吐量。

### 加载、索引与卸载

- 应用每次启动默认未加载；选择本机模式、提问或每日整理均不会隐式加载模型。独立向量服务的自动加载配置不用于整站应用。
- 管理员在 **设置 · 模型 · 向量 · 本机** 安装、加载或卸载共享资源；普通用户可以查看状态和选择使用，不能执行这些管理操作。加载与卸载沿用资源管理权限及运行修订检查，影响所有使用本机向量的用户。
- 加载完成后，宿主为当前已经初始化、尚未关闭且尚未进入回收的本机模式用户应用启动各自的后台索引，沿用用户作业队列和配额，不为此创建闲置用户空间。模型已加载不表示所有用户的资料瞬间完成索引；每位用户看到自己的索引进度和缓存。
- 模型未加载、正在加载或卸载时，提问按关键词检索；已就绪后问题现算向量，资料使用各自已有缓存，缺失部分等待后台补算。
- 卸载阻止新的向量计算，等待在途批次结束后释放模型引用，保留权重和用户缓存。运行库可能继续保留部分内存，重新加载需要时间。
- 用户空间因闲置而回收时不卸载共享模型；空间关闭后不再启动它的索引任务。应用宿主退出时统一结束工作并卸载共享模型。

本机向量在服务器进程内计算，不向另一项向量服务发送资料。生成、转写、识图和重排按各用户自己的模型配置与外发开关运行；使用共享向量不等于所有功能都已离线。本机模式中的私密资料仍按原本机计算规则处理。

Linux 上的识图可选 [RapidOCR](https://github.com/RapidAI/RapidOCR)：安装 `requirements-vision.txt`，按对应提供器配置预先准备 `det.onnx`、`cls.onnx`、`rec.onnx`、`keys.txt` 和 `font.ttf`。这些是向量以外的可选用途，模型资产不会因启动网页而自动下载。

## 启动与 HTTPS

确认服务用户、源码目录、数据根和主密钥文件均与模板一致，再安装服务：

```sh
cd /opt/chriptmas
sudo install -m 0644 deploy/chriptmas.service /etc/systemd/system/chriptmas.service
sudo systemctl daemon-reload
sudo systemctl enable --now chriptmas.service
```

`backend.memory_app.serve` 只监听 `127.0.0.1:8001`，由同一进程托管 `src/frontend/dist`，网页与 API 同源；缺少前端构建产物时拒绝启动。保持单进程运行，共享模型由该宿主管理，不加多个 HTTP worker。

将 [Caddyfile.public](../deploy/Caddyfile.public) 或 [Caddyfile.lan](../deploy/Caddyfile.lan) 的站点块加入现有 Caddy 配置：公网模板的 `chriptmas.example.com` 换成实际域名；局域网模板的 `chriptmas.lan` 换成可解析的主机名，并让客户端信任 Caddy 本地 CA。两份模板均代理到 8001，关闭流式响应缓冲。只对外开放 HTTPS 入口，不直接开放 8001。

如果使用不同目录或端口，应同步改环境变量、systemd、Caddy、安装和备份命令；本文保留模板既有的 `/opt/chriptmas` 与 `/var/lib/chriptmas`。

没有域名时，可先通过 SSH 隧道私下访问。在自己的电脑运行：

```sh
ssh -N -L 127.0.0.1:18473:127.0.0.1:8001 user@SERVER_IP
```

保持隧道连接，再用同一台电脑的浏览器打开 `http://127.0.0.1:18473`。此时首次配对命令的 `--url` 也使用这个地址。隧道通过 SSH 加密，应用后台仍只监听服务器 loopback；其他设备需要自己的连接方式，公网 HTTPS 则按上面的 Caddy 配置单独准备。

## 配对设备与用户

使用 HTTPS 入口或上面的 SSH 隧道后，在服务器本机以服务用户运行首次管理员设备配对：

```sh
sudo -u chriptmas -H /opt/chriptmas/.venv/bin/python /opt/chriptmas/tools/server_admin.py \
  pair --root /var/lib/chriptmas --url https://chriptmas.example.com
```

命令会在本机终端显示包含一次性凭据的配对链接，十分钟内有效，只能兑换一次。用浏览器打开或扫码，填设备名称完成配对；不要将链接写入日志或公开。设备钥匙只在兑换时返回一次，之后请求通过设备授权，不使用独立向量服务的推理密钥。

在 **设置 · 设备** 添加或作废设备，作废立即生效；二维码在本地生成。管理员在同一分组新建用户、给用户添加设备、设置空间与后台时长配额，或进入其空间；普通用户只能操作自己的空间。各用户分别在 **设置 · 模型** 配置生成等用途，并按用途一次开启外发。

## 备份与恢复

备份复用在线 SQLite 快照，产物带 UTC 日期。先准备低权限用户可写的备份目录：

```sh
sudo install -d -m 0700 -o chriptmas -g chriptmas /var/backups/chriptmas
sudo -u chriptmas -H /opt/chriptmas/.venv/bin/python /opt/chriptmas/tools/backup.py \
  --root /var/lib/chriptmas --output /var/backups/chriptmas
```

只备份某一用户，或将已有快照恢复到新目录：

```sh
sudo -u chriptmas -H /opt/chriptmas/.venv/bin/python /opt/chriptmas/tools/backup.py \
  --root /var/lib/chriptmas --user local-user --output /var/backups/chriptmas
sudo install -d -m 0700 -o chriptmas -g chriptmas /var/lib/chriptmas-restores
sudo -u chriptmas -H /opt/chriptmas/.venv/bin/python /opt/chriptmas/tools/backup.py \
  --restore /var/backups/chriptmas/SNAPSHOT --to /var/lib/chriptmas-restores/restore-target
```

恢复目标必须尚不存在，包括空目录也会被拒绝；为恢复后的正式部署选择与旧数据分开的目录，并同步修改服务配置。服务器主密钥位于 `/etc/chriptmas`，不在数据快照里，恢复后仍需要原密钥；Windows 跨机器恢复需要重新填写模型密钥。

[chriptmas-backup.service](../deploy/chriptmas-backup.service) 和 [chriptmas-backup.timer](../deploy/chriptmas-backup.timer) 提供每日备份模板，路径与本文一致。安装后可使用 timer 调度；备份、恢复和权限的完整链路仍需在实际服务器确认。

本文提供部署步骤与当前实现边界，制作过程中未运行部署命令、模型调用或远程验证。真实服务器上线还需确认设备配对、用户隔离、密钥保存、加载/卸载、各用户索引、带引用提问和恢复后的运行情况。
