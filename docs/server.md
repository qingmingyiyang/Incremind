# 整个应用部署到服务器

> 🧪 实验性：2026-10-12 已在 Ubuntu 22.04 CPU 实例验证安装构建、设备配对、共享向量加载、计算、卸载与重新加载、公网 HTTPS 和多用户接口隔离，用户已完成首次浏览器登录并进入工作台。全部业务网页操作、GPU 及服务器备份恢复仍需专项验证。

整站部署把网页、资料、对话和模型调用放在服务器，浏览器或配对设备连接它。若只想把向量计算交给服务器、资料库继续留在电脑，使用另一条独立路径：[私有服务器计算向量](vector-server.md)。整站部署不要求先安装独立向量服务。

## 公网流量与服务器使用

部署、远程开发和日常运维默认优先节省服务器发往公网的数据。在不影响资料完整、隐私边界、功能验收和备份恢复的前提下，把计算与筛选留在服务器，只传必要的结果。

### 阿里云 CDT 计费边界

2026-10-12 核验：已接入 CDT 的受支持按流量计费 BGP（多线）资源，可共享账号每月中国内地 20 GB、非中国内地 200 GB 免费公网流量。按云资源所在地域使用对应额度，同账号的多台实例及适用产品共享；按自然月刷新，当月余额不结转。固定带宽和 BGP（多线）精品线路不适用这份额度。部署前检查实际线路、计费方式与 CDT 接入状态，不能仅凭服务器属于阿里云就假定享受免费额度。[官方额度说明](https://help.aliyun.com/zh/cdt/product-overview/cdt-public-network-traffic-free-quota-increased-from-200gb-month-to-220gb-month)、[官方公网流量计费说明](https://help.aliyun.com/zh/cdt/internet-data-transfers/)。

CDT 这份额度只计算服务器公网出方向流量，入方向不计费也不消耗额度；额度耗尽后继续按量收费，不会自动断网。以下按业务数据的主要方向判断，请求、应答等协议开销仍按实际出方向计入。[官方计费说明](https://help.aliyun.com/zh/cdt/internet-data-transfers/)。

| 操作 | 服务器方向 | 对这份免费额度的影响 |
|---|---|---|
| 电脑上传资料或代码包到服务器 | 入方向 | 上传的主体数据不消耗 |
| 服务器从网上下载依赖、软件或模型 | 入方向 | 下载的主体数据不消耗 |
| 电脑下载服务器上的文件、日志或备份 | 出方向 | 消耗 |
| 服务器向访客返回网页、图片、流式回答或向量 | 出方向 | 消耗 |
| 服务器向公网模型 API 发送提示词、代码、原文或附件 | 出方向 | 消耗；模型返回的数据为入方向 |
| 同一台服务器上应用、向量服务与工具经 loopback 通信 | 本机通信 | 不经过公网 |

SSH 隧道、VPN 和端口转发用于保护或连接通信，不自动免除公网计费。CDT 免费额度也不抵扣模型调用、存储等其他费用；跨资源传输是否另有网络费用，按实际产品和路径核验。

### 部署与日常操作

1. 优先在服务器构建、测试、检索和评测。保留服务器上的完整证据，先按文件、字段、时间段和数量筛选，再返回摘要与必要片段；出现失败时提取相关原文，不用摘要代替验收证据。
2. 复用已有源码、依赖、模型和下载缓存；代码沿既有发布流程同步固定版本或必要增量。大文件按需传输，适合压缩的先压缩，避免反复把整库、模型和安装包下载到电脑。
3. Codex 远程开发优先传输指令、差异和结果；浏览器检查按既定验收范围保留截图，避免反复传送整屏和录屏。同机应用调用走 `127.0.0.1`，有适用私网路径时优先用私网；向公网模型提交内容继续遵守外发开关、私密规则和回执要求。
4. 备份先在服务器生成、校验并按既定保留策略维护；必要的异地备份照常做，压缩和增量优先，不为节流取消恢复保障。外传前确认数据范围、授权和预计大小。
5. 常规部署、批量评测及持续远程操作按上述节流规则直接执行，无需专门核对账单或等待用户提供 CDT 余额（用户2026-10-12澄清）。大文件外传先估算必要范围与大小；健康与进度检查采用有界等待和退避。发现明确的额度或费用问题时报告实际证据并减少可省的传输；计费、线路、付费资源或停服操作按用户授权执行。

本文记录的是操作原则和经核验的计费规则，未读取当前阿里云账号账单，也未替账号开通 CDT、设置告警或调整网络配置。下次部署时重新核对政策与账号实际状态。

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

准备 Linux、Python 3.12、满足 [README](../README.zh-CN.md#快速开始) 要求的 Node.js，以及支持 `LoadCredential` 的 systemd。将包含服务器实现的源码放到 `/opt/chriptmas`，由部署者安装依赖和构建前端：

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

### 没有域名时直接使用公网 IP

没有域名也可以申请公网 IP 的 HTTPS 证书。Let’s Encrypt 的 IP 证书有效期为六天，Certbot 5.4 起支持通过 webroot 验证；需要自动续期并在成功后重新加载代理。[官方说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)。

公网入方向只需放行 TCP 80 和 443，并保留已有 SSH 规则；8001 继续只监听 loopback。80 用于证书验证和跳转，应用经 443 访问。在独立虚拟环境 `/opt/incremind-acme` 安装 Certbot 5.4 或更高版本，并先让 Nginx 将 `/.well-known/acme-challenge/` 映射到 `/var/lib/incremind-acme`。将下面的 `SERVER_IP` 换成实例公网 IPv4：

```sh
sudo /opt/incremind-acme/bin/certbot certonly --dry-run \
  --non-interactive --agree-tos --register-unsafely-without-email \
  --preferred-profile shortlived --webroot --webroot-path /var/lib/incremind-acme \
  --ip-address SERVER_IP --cert-name incremind-ip
```

测试成功后去掉 `--dry-run` 申请正式证书。使用 [nginx-ip.conf](../deploy/nginx-ip.conf) 替换临时验证站点，并替换其中的 `SERVER_IP`；通过 `nginx -t` 后重新加载。模板保留同源 HTTPS、WebSocket 与流式响应，压缩网页资源；仅成功返回的带版本静态资源长期缓存，页面与 API 不缓存。不要同时启用 Caddy 与 Nginx 的相同监听端口。

将 [incremind-acme-reload](../deploy/incremind-acme-reload) 安装到 `/usr/local/sbin/incremind-acme-reload`，权限 0755；将 [续期服务](../deploy/incremind-acme-renew.service) 和 [续期定时器](../deploy/incremind-acme-renew.timer) 安装到 `/etc/systemd/system/`，权限 0644：

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now incremind-acme-renew.timer
sudo /opt/incremind-acme/bin/certbot renew --dry-run --cert-name incremind-ip \
  --deploy-hook /usr/local/sbin/incremind-acme-reload --run-deploy-hooks
```

定时器每十二小时检查一次，只有到期需要续签时才申请；每次检查成功后都校验并重新加载 Nginx，应用进程继续运行。重新加载失败会使服务显示失败，下一轮检查会再次尝试，避免证书已更新但代理一直使用旧证书。公网 IP 变更时要重新签发匹配的新证书并更新入口。直接打开 `https://SERVER_IP`，首次设备配对命令的 `--url` 使用同一地址，之后浏览器和手机直接连接服务器。

### 临时 SSH 隧道

也可以通过 SSH 隧道私下访问。在自己的电脑运行：

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

当前使用管理员邀请与设备配对登录。配对码在生成时已经绑定目标用户；填写设备名称只是在标记浏览器，不会选择或改变账号。它验证持有授权凭据，不能证明真人身份；有效链接的持有人可以兑换该用户的访问权限。首次管理员链接由部署者在服务器受控环境生成，只给管理员本人使用。其他人使用管理员为其独立用户生成的链接，普通用户之间由后端限制空间访问；管理员按现有权限可以进入各用户空间并留痕。

浏览器将设备凭据保存在本机站点存储中。十分钟有效期只适用于尚未兑换的邀请，已配对的浏览器之后打开同一 HTTPS 地址可以直接进入；设备作废、用户停用或清除站点数据会失去访问权限。换浏览器或使用新的无痕窗口需要重新配对。同一个浏览器配置对本站点保存一套身份，测试另一个用户时使用其自己的浏览器、独立浏览器配置或无痕窗口。重新生成链接不会提前撤销旧的未使用链接，旧链接仍按原十分钟期限失效。

在 **设置 · 设备** 添加或作废设备，作废后新请求失去访问权限；二维码在本地生成。管理员在同一分组新建用户、给用户添加设备、设置空间与后台时长配额，或进入其空间；普通用户只能操作自己的空间。各用户分别在 **设置 · 模型** 配置生成等用途，并按用途一次开启外发。

### 本次部署的日常登录

固定入口为部署时配置的 HTTPS 域名或公网 IP，可以收藏。首次配对成功后，在同一浏览器直接打开这个地址；不用再次点击首次登录，也不用保留配对链接。应用由服务器后台托管，已配对的手机可以直接访问该地址，电脑关闭后仍可使用。

管理员电脑上的 **Incremind 登录** 提供两个按钮：打开网页用于日常访问，重新登录用于需要新邀请的浏览器；**Incremind 首次登录** 直接生成新邀请并打开浏览器。填写设备名称并点配对后进入自己的空间，邀请过期时重新生成即可。已配对设备也可在设置 · 设备为自己的新设备生成邀请。

这些本机入口复用现有 SSH 管理连接签发管理员邀请，应由管理员本人保管；普通用户使用管理员为其用户生成的邀请。邀请绑定目标用户，设备名称不会改变所属空间。管理员可以按现有权限进入其他用户空间，访问记录对相应用户可见。

入口实现为 [windows-server-login.py](../deploy/windows-server-login.py)，安装器为 [install-windows-server-login.ps1](../deploy/install-windows-server-login.ps1)。安装前需要本机既有连接目录中的 `connection.json`、`known_hosts`、`ssh_run.py` 及配置指向的 SSH 私钥。安装器通过 Python 解析 MSIX 重定向和目录联接的实际路径，快捷方式及诊断 CMD 使用解析后的路径；登录脚本从自身实际父目录读取连接文件。这样从桌面启动也能找到同一份配置。诊断状态只记录固定阶段与异常类型，不记录邀请、设备钥匙或子进程输出。

## Codex 远程开发与维护

2026-10-12 用户将当前部署范围收窄为应用网页直接使用，Codex 与开发网页部署暂停。以下保留为后续维护参考。

部署目标是电脑关闭后，应用与已经派发的 Codex 任务仍在服务器运行。Codex CLI 可以作为后台执行引擎，用户通过网页发任务、看进度和结果，无需操作命令行。执行时模型负责判断，服务器上的 Codex 及工具负责读写文件、运行命令和构建验证；通常通过 ChatGPT 账号或 API 调用 OpenAI 模型。服务器需要能访问对应服务，安装 Codex 不会把模型权重或当前电脑的全部工具配置一并迁入。[Codex CLI](https://learn.chatgpt.com/docs/codex/cli)、[身份验证](https://learn.chatgpt.com/docs/auth)。

1. 使用独立开发账号与工作目录，例如该账号下的 `~/projects/ChriptmasAgent`。正式应用沿用 `/opt/chriptmas`，正式资料沿用 `/var/lib/chriptmas`；开发冒烟使用独立临时数据根。开发验证通过后，按固定版本更新正式应用，保留备份和撤回路径。
2. 在服务器安装并登录 Codex，确保远程用户的登录 shell 能从 `PATH` 找到 `codex`。无图形界面时可用 `codex login --device-auth`，在自己的浏览器完成验证；该方法需要账号或工作空间已启用设备代码登录。[官方安装与登录](https://learn.chatgpt.com/docs/codex/cli)、[无界面登录说明](https://learn.chatgpt.com/docs/auth)。
3. 电脑开机时可另用桌面 SSH 连接操作：在电脑的 SSH 配置中添加服务器别名，确认能正常 SSH 登录，再在桌面应用的 **设置 · 连接 · SSH** 添加该主机，选择服务器上的开发项目。桌面应用经 SSH 启动远程 Codex app server，不向公网直接开放它的监听端口。这条可选链路不替代电脑关机后的独立运行和访问入口。[官方 SSH 连接说明](https://learn.chatgpt.com/docs/remote-connections#connect-to-an-ssh-host)。
4. 同步必要的项目规则与上下文，在服务器重新配置所需 skills、MCP、插件与依赖；Windows 绝对路径、PowerShell 命令和虚拟环境入口要按 Linux 实际路径调整。迁移已有聊天时，先在目标主机登记同一 Git 仓库项目，再使用应用的移交功能；只复制源码不会自动移交聊天。[官方远程环境与聊天移交说明](https://learn.chatgpt.com/docs/remote-connections)。
5. 由服务器后台服务接收并保存任务，再作为受管作业调用 `codex exec`，明确权限、超时、退出状态与结果位置；服务器用 systemd 等方式托管服务，使任务执行不依赖提交任务的 SSH 会话或网页连接。任务完成后保存结果并等待下一项，客户端失联不等同显式取消；持续工作还需配置调度和恢复。手机经桌面主机访问 SSH 项目的链路仍依赖该桌面主机在线，应另有手机直接访问服务器的入口。[非交互运行](https://learn.chatgpt.com/docs/non-interactive-mode)、[远程连接](https://learn.chatgpt.com/docs/remote-connections)。

### 电脑关机后的验收

- 应用通过服务器 HTTPS 入口访问，或使用访问设备自己的 SSH 隧道，不借用已关机电脑上的隧道；图形操作验收使用服务器网页入口。
- 在独立临时数据根提交一个有明确结束条件的小任务，关闭电脑客户端并断开其连接，再用手机直接连接服务器，确认服务可达、任务继续、进度与完成结果可回读；之后由用户关闭电脑复验同一路径。
- 任务状态、日志、结果和必要会话记录保存在服务器，重连可以回读；真实 Codex 登录、调用、取消和重连必须单独验证，不能用模拟任务代替全部验收。
- 目前记录的是部署要求，本次尚未配置或验收服务器后台任务与网页控制链路。仅安装 CLI、建立桌面 SSH 连接或运行一次前台命令均不构成关机后可用的证明。

以上属于开发维护环境。工作台调用 Codex、共享用户的任务隔离及 MCP 记忆接入，另按 [外部 agent 接入](agents.md) 验收；安装 CLI 或建立 SSH 连接不能代替这些功能的真实验收。远程操作均遵守本文的公网流量规则。

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

2026-10-12 实际部署：Ubuntu 22.04 实例的应用与 Nginx 已设置开机启动，通过 HTTPS 公网入口访问，8001 保持 loopback。受信任 TLS、页面与健康检查、未授权接口拒绝、HTTPS 同源判断、跨源拒绝、版本资源缓存、错误响应不缓存及 gzip 共十一项检查通过；CSS 从 86694 字节压缩为 20083 字节。IP 证书正式签发、续期定时器及续期演练通过，应用进程由 PID 1 托管。

实际公网接口验收 42 项登录与权限检查、24 项原件隔离检查全部通过，覆盖管理员、两个普通用户的配对、各自项目与设备、同一项目编号下原件的读写隔离、越权拒绝、管理员访问留痕、设备作废与用户停用。两个临时用户已停用，七个测试设备已作废；仅在隔离测试空间留存少量验收资料，正式资料和模型配置未修改，模型调用为零。

Windows 入口已修复 MSIX 路径重定向并通过独立审查，用户已实际配对并进入工作台；服务器只读检查确认新设备属于启用中的管理员，并已发出认证请求。完整业务网页操作本轮未逐项重跑。验证摘要位于忽略目录 `work/server-codex-web-20261012/` 的 `live-login-results.json`、`live-source-isolation-results.json`、`public-https-checks.json` 和 `owner-login-confirmed.log`。Codex 远程开发方案按用户要求继续暂停。
