# 私有服务器计算向量

这条部署路径只把文字向量计算交给你自己的服务器。工作台、资料库、原件和向量缓存仍在电脑上；服务器接收本次需要编码的文字，返回向量，不保存电脑的资料库或正文。

可以按使用需要选择两种部署，它们分别配置：

| 路径 | 运行位置与用途 | 入口 |
|---|---|---|
| 1 独立向量服务 | 电脑运行 Incremind，私有服务器提供文字向量算力 | 本文及 `deploy/incremind-vectors.service`、`deploy/Caddyfile.vectors` |
| 2 整个应用的服务器形态 | 网页、资料、用户空间和应用后端都在服务器 | [服务器部署](server.md)；与独立向量服务分别配置 |

本文对应第一条路径的实现与部署模板，尚未完成真实 Linux 服务器、HTTPS 和 GPU 验证。模板中的目录与域名需要按实际服务器填写。

## 计算与数据边界

```mermaid
flowchart LR
  A["电脑上的 Incremind<br/>资料库 · 检索 · 向量缓存 · 回执"] -->|"HTTPS + 推理密钥<br/>问题或资料文字"| B["独立向量服务<br/>EmbeddingGemma 2 文字模型"]
  B -->|"256 维向量"| A
```

- 当前只提供 EmbeddingGemma 2 的文字向量，输出 256 维，不提供录音转写、识图或回答生成。
- 客户端在请求中区分问题与资料，服务按对应方式编码；无需手工添加模型前缀。原有外接向量协议保持不变。
- 自建模式目前在提问时按需计算并填入电脑端缓存，没有远程后台全量补算入口。服务显示已加载，只表示模型可以计算，不表示电脑资料已全部完成索引。
- 服务器属于另一台机器，调用仍按外发处理。电脑端须开启向量用途的外发开关，私密资料及其派生内容仍按既有规则排除，调用回执留在电脑端。
- 向量服务不托管 Incremind 用户空间，不要求复制正式 `runtime/`，也不替代电脑端的备份。

## 准备服务器

准备 Python 3.12、支持 `LoadCredential` 的 systemd，以及支持 `request_body max_size` 的 Caddy。将包含独立向量服务入口的源码版本放到 `/opt/incremind`，然后在该目录建立环境：

```sh
cd /opt/incremind
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-web.txt -r requirements-local-vectors.txt
```

可选向量依赖统一维护在 [requirements-local-vectors.txt](../requirements-local-vectors.txt)，其中包括 torch、transformers 和 sentence-transformers；服务复用应用现有的模型安装与运行实现。部署模板默认 CPU。使用 GPU 时，先按服务器驱动准备兼容的 torch 包，再将设备设置为 `cuda` 或 `auto`，并给服务用户所需的设备访问权限。`cuda` 要求 CUDA 可用，`auto` 在可用时选择 CUDA、否则选择 CPU；本机尚未验证 GPU，不据此承诺显存需求或吞吐量。

为首次部署创建独立低权限用户和模型目录：

```sh
sudo useradd --system --home-dir /var/lib/incremind-vectors --shell /usr/sbin/nologin incremind-vectors
sudo install -d -m 0700 -o incremind-vectors -g incremind-vectors /var/lib/incremind-vectors
```

源码与虚拟环境应对该用户可读、可执行，由部署者维护；模型目录归服务用户写入。已有同名用户时沿用它。

### 先安装模型资产

模型由现有安装工具预先准备，服务启动不会自行下载：

```sh
sudo -u incremind-vectors -H env PYTHONPATH=/opt/incremind/src \
  /opt/incremind/.venv/bin/python /opt/incremind/tools/fetch_model.py \
  embedding --root /var/lib/incremind-vectors
```

安装工具从官方资产派生文字模型，并发布到：

```text
/var/lib/incremind-vectors/data/models/google-embeddinggemma-2/
```

已有完整的官方原始模型资产时，可以使用安装工具的 `--from /官方原始资产目录` 参数，由同一入口核验、派生并发布文字模型；该参数不接受已经裁剪的文字权重。下载与安装需要相应网络访问；模型资产和派生记录仍遵循上游许可。

### 配置推理密钥

推理密钥用于计算向量，不授予加载、卸载权限。把密钥写入服务器专用文件，不放进源码或命令行参数：

```sh
sudo install -d -m 0700 /etc/incremind-vectors
sudoedit /etc/incremind-vectors/api-key
sudo chmod 0600 /etc/incremind-vectors/api-key
```

文件只放一条推理密钥，不超过 4,096 字节。systemd 用 `LoadCredential` 将它提供给服务，服务通过 `CHRIPTMAS_VECTOR_API_KEY_FILE` 读取。直接启动时也支持 `CHRIPTMAS_VECTOR_API_KEY`，同一用途的两种方式互斥，缺少推理密钥时拒绝启动。

## 安装 systemd 与 HTTPS 入口

检查 [incremind-vectors.service](../deploy/incremind-vectors.service) 中的用户、源码路径、虚拟环境和数据根，确认与服务器一致，再安装：

```sh
cd /opt/incremind
sudo install -m 0644 deploy/incremind-vectors.service /etc/systemd/system/incremind-vectors.service
sudo systemctl daemon-reload
sudo systemctl enable --now incremind-vectors.service
```

模板只启动一个进程，监听 `127.0.0.1:8002`。不要加多 worker，每个进程都会各自持有一份模型。服务端由 systemd 的低权限用户运行，文件系统默认只读，持久化写入限制在 `/var/lib/incremind-vectors`；模板保留设备可见性，便于另行配置 GPU。

把 [Caddyfile.vectors](../deploy/Caddyfile.vectors) 的站点块加入服务器现有 Caddy 配置，并将 `vectors.example.com` 换成你的实际域名。域名应指向这台服务器，HTTPS 由 Caddy 提供，代理到本机 8002。模板限制请求体为 12,353,536 字节，约 11.78 MiB，与服务端保持一致；不要把 8002 直接开放到公网。应用已有 Caddy 站点可以与这个站点块共存。

在服务器完成配置后执行以下检查；本文制作时未运行这些部署命令：

```sh
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl --fail --show-error https://vectors.example.com/health
```

### 运行参数

| 环境变量 | 默认值或要求 | 含义 |
|---|---|---|
| `CHRIPTMAS_VECTOR_ROOT` | 模板为 `/var/lib/incremind-vectors` | 预先安装的模型所在数据根 |
| `CHRIPTMAS_VECTOR_API_KEY` / `CHRIPTMAS_VECTOR_API_KEY_FILE` | 必填其一 | 推理密钥或密钥文件 |
| `CHRIPTMAS_VECTOR_ADMIN_KEY` / `CHRIPTMAS_VECTOR_ADMIN_KEY_FILE` | 可选其一 | 独立管理密钥；未配时禁用远程加载、卸载 |
| `CHRIPTMAS_VECTOR_DEVICE` | `cpu` | `cpu`、`cuda` 或 `auto`，更改后重启服务 |
| `CHRIPTMAS_VECTOR_AUTOLOAD` | `1` | 启动后异步加载；设为 `0` 时保持未加载 |

服务器模板默认自动加载，与电脑应用启动默认未加载的行为分别控制。异步加载期间进程可以响应健康检查，但推理接口尚不可用；应读取运行状态确认已加载，不能仅凭服务进程在运行就判断可以向量化。

`GET /health` 仅在已加载时返回 `200`，其他运行状态返回 `503`；响应只含状态、原因码、设备和运行修订等安全信息。服务进程存活与模型就绪分别判断，加载期间健康检查失败不会让该 systemd 模板自动重启进程。

## 按需加载和卸载

默认只配置推理密钥，禁止通过远程接口加载或卸载。需要管理模型时，另行保存一条与推理密钥不同的管理密钥到 `/etc/incremind-vectors/admin-key`，权限同为 0600，再通过 `sudo systemctl edit incremind-vectors.service` 增加：

```ini
[Service]
LoadCredential=vector-admin-key:/etc/incremind-vectors/admin-key
Environment=CHRIPTMAS_VECTOR_ADMIN_KEY_FILE=%d/vector-admin-key
```

随后重启服务使配置生效。可选管理凭据放在单独的 override 中，基础模板不会因缺少管理密钥文件而启动失败。管理密钥只交给服务器管理工具，电脑的向量模型设置填写推理密钥。

接口使用以下授权：

| 接口 | 授权与行为 |
|---|---|
| `GET /health` | 无密钥，仅公开安全的服务状态，不返回路径或密钥 |
| `GET /v1/models` | Bearer 推理密钥，查看提供的模型与 `embedding_profile` |
| `POST /v1/embeddings` | Bearer 推理密钥，提交文字并返回向量 |
| `GET /admin/runtime` | Bearer 推理密钥，只读模型运行状态与修订 |
| `POST /admin/load` | Bearer 管理密钥，使用最新 `expected_revision` 请求加载 |
| `POST /admin/unload` | Bearer 管理密钥，使用最新 `expected_revision` 请求卸载 |

管理请求先读取运行状态，将返回的 `runtime_revision` 作为 `expected_revision` 提交。请求体只接受这个字段，最多 1,024 字节。状态已变化时返回 `409`，重新读取再操作，避免并发操作覆盖彼此。设置 `AUTOLOAD=0` 时，须预先配置管理密钥才能远程加载，或修改启动配置后重启。

```json
{"expected_revision": "上次读取的 runtime_revision"}
```

卸载保留模型文件与电脑端向量缓存，阻止新的计算，并等待正在运行的批次结束后释放模型引用。进程内存是否立即全部归还操作系统取决于运行库；重新加载仍需要时间。服务重启会再次遵循 `AUTOLOAD` 设置。

## 电脑端连接

在电脑的 **设置 · 模型 · 向量** 选择 **自建**，填写：

| 字段 | 示例 |
|---|---|
| 地址 | `https://vectors.example.com/v1` |
| 模型 | `google/embeddinggemma-2` |
| 密钥 | 服务器的推理密钥 |
| 向量外发 | 开启，按此用途一次授权 |

地址包含 `/v1`，客户端负责追加接口路径。保存后向量计算走自有服务器；生成、转写、识图和重排仍按各自用途配置运行。选择自建不会为资料开启其他用途的外发，也不会自动解除私密。

`POST /v1/embeddings` 接受 `model`、`embedding_profile`、`input`，以及整批 `input_type` 或逐项 `input_types`。文字最多 128 项，每项最多 24,000 个字符，JSON 请求体同时受字节上限约束。混合问题与资料时使用与 `input` 等长的 `input_types`，值为 `query` 或 `document`；电脑端已经按此契约发送。

`embedding_profile` 绑定模型、问题/资料角色协议、维度和向量策略版本，由电脑端自动发送；以 `GET /v1/models` 返回的值为准，当前为 `google/embeddinggemma-2:selfhosted-roles@1:256:vector@1`。电脑与服务器须采用一致的编码约定；缺少或不匹配时在编码前返回 `400 embedding_profile_mismatch`，避免只凭相同模型名混用不同向量缓存。更新客户端或服务时同步核对该版本。

队列最多接纳 64 个尚未完成的任务，混合问题与资料的一个 HTTP 请求占两个任务。问题优先，资料按小批次让出执行机会。电脑端沿用 35 秒 HTTP 超时；繁忙或超时时本轮向量检索可能降级，服务已接纳的计算仍可能继续。该限制尚未经过真实远程压力验证。

### 常见状态

| 状态 | 处理方式 |
|---|---|
| 未加载、加载中或卸载中，推理返回 `503` | 查看 `/admin/runtime`，等待加载完成或由管理员加载 |
| 排队已满，返回 `429` | 减少同时请求，稍后重试；增加 HTTP worker 会重复占用模型内存 |
| 输入超限，返回 `413` | 缩小本次请求；服务与代理均有请求体限制 |
| 返回 `400 embedding_profile_mismatch` | 核对电脑与服务的模型、角色协议、维度和向量策略版本 |
| 推理密钥可用但管理操作被拒绝 | 管理操作须使用单独配置的管理密钥 |
| 电脑仍按关键词检索 | 检查自建配置、外发开关和服务运行状态；已加载不代表电脑端所有资料已有缓存 |

CPU、GPU 资源、HTTPS、加载/卸载和电脑提问的完整联通需要在实际服务器上验证。当前文档和模板提供部署入口，不能据此视为部署已完成。

模板语法参考：[Caddy 请求体限制](https://caddyserver.com/docs/caddyfile/directives/request_body)、[systemd 凭据文件与 `%d` 目录](https://github.com/systemd/systemd-stable/blob/v255-stable/docs/CREDENTIALS.md)。
