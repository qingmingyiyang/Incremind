# 在服务器运行 SoMark OXR

SoMark OXR 由 SoMark Team 提供。ChriptmasAgent 通过同机 API 使用图片和 PDF 识别。部署前阅读适用于当前用途的商业许可、线上服务署名及竞争性托管 API 条款。[SoMark OXR 许可证](https://raw.githubusercontent.com/SoMarkAI/OXR/c1a1dcb312ee9f691404f127785510453c0d9350/LICENSE)

## 检查现有服务器

在 Linux x86_64 主机上运行。官方要求 NVIDIA GPU 支持 BF16，驱动兼容 CUDA 13，低并发从单张 24 GB 级显卡起步，长文档或更高并发推荐单张 48 GB 或以上。现有服务器的历史验收使用 CPU，须以当前机器的预检结果决定能否安装。[SoMark OXR 官方部署说明](https://raw.githubusercontent.com/SoMarkAI/OXR/c1a1dcb312ee9f691404f127785510453c0d9350/README.md)

从服务器上的 ChriptmasAgent 代码目录执行：

```bash
sudo bash deploy/oxr-local.sh preflight
```

预检只读取 GPU 和驱动、本机 Docker 与 NVIDIA Container Toolkit、磁盘及端口。需要由 systemd 管理的 Docker Engine 28 或更新的正式版本，关闭直连路由并保留 Docker 防火墙规则。预检会检查服务启动参数和配置，仅检查宿主机端口 8000。脚本默认选 GPU 0，可通过 `CHRIPTMAS_OXR_GPU` 选择另一张卡。脚本保留至少 20000 MiB 当前可用显存。同一文件系统为 Docker 镜像、构建缓存和权重预留 40 GiB，分盘时分别预留 32 GiB 和 8 GiB。以上预留是部署检查阈值。若预检失败，保留当前应用和识图设置，待原服务器具备对应条件后继续。[Docker 端口发布说明](https://docs.docker.com/engine/network/port-publishing/) · [NVIDIA 容器运行检查](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/sample-workload.html)

## 安装与核验

```bash
sudo bash deploy/oxr-local.sh install
sudo bash deploy/oxr-local.sh ready
```

安装使用固定上游 Dockerfile。脚本先验证 Docker 容器可见指定 GPU，再构建官方镜像并下载固定模型。模型仓库声明的权重和附属文件约 3.16 GB。镜像及构建缓存留在本机 Docker，源码和权重默认放在 `/var/lib/chriptmas-oxr`，可通过 `CHRIPTMAS_OXR_ROOT` 指定专用绝对目录。容器只读挂载权重，并配置 Docker 重启恢复。首次启动需加载模型，可用 `sudo docker logs --tail 30 chriptmas-oxr` 查看状态，模型初始化完成后再执行 `ready`。[上游镜像定义](https://raw.githubusercontent.com/SoMarkAI/OXR/c1a1dcb312ee9f691404f127785510453c0d9350/docker/Dockerfile.allinone) · [固定权重版本](https://huggingface.co/SoMarkAI/OXR-1.0/tree/c708407f9d2b4df658f2543d9132b62bb7221df4)

容器使用独立 IPv4 NAT 网络和独立共享内存。只向宿主机的 `127.0.0.1:8000` 发布解析 API。vLLM、版面分析和推理内部连接留在容器网络内。部署将版面分析每批页数和版面、识别服务并发各设为 1。`ready` 检查实际网络、端口映射、三项官方健康探针和解析接口声明。要验证实际解析，使用隔离的合成 PDF，确认返回的 Markdown 包含样本文字。[上游启动器](https://raw.githubusercontent.com/SoMarkAI/OXR/c1a1dcb312ee9f691404f127785510453c0d9350/docker/allinone_entrypoint.py) · [vLLM 容器共享内存说明](https://docs.vllm.ai/en/stable/deployment/docker/)

```bash
curl --noproxy '*' --fail-with-body --max-time 180 \
  -F 'file=@synthetic.pdf' -F 'api_key=dummy' -F 'output_formats=markdown' \
  http://127.0.0.1:8000/v1/parse/sync
```

## 接入与停用

通过合成资料核验后，在获授权的账号的 **设置 · 模型 · 识图** 选择外接，地址填 `http://127.0.0.1:8000/v1`，模型填 `OXR-1.0`，密钥填占位值 `dummy`。该地址供同一台 Linux 主机上的应用使用。使用现有识图外发开关、私密校验和逐次回执。同机请求直接连接 OXR，忽略环境代理。图片和 PDF 沿现有入口处理，Word 沿原文档解析流程处理。[模型配置与外发规则](models.md#somark-oxr-10)

停用时先将相关账号切回原识图配置，再执行 `sudo docker stop chriptmas-oxr`。再次启用可执行 `sudo docker start chriptmas-oxr` 并重新检查 `ready`。
