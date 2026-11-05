#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

code_revision=c1a1dcb312ee9f691404f127785510453c0d9350
model_revision=c708407f9d2b4df658f2543d9132b62bb7221df4
root=${CHRIPTMAS_OXR_ROOT:-/var/lib/chriptmas-oxr}
gpu=${CHRIPTMAS_OXR_GPU:-0}
container=chriptmas-oxr
network=chriptmas-oxr-local
image="chriptmas-oxr:${code_revision:0:12}"
docker=(docker --host unix:///var/run/docker.sock)

fail() { printf 'OXR: %s\n' "$*" >&2; exit 1; }

check_disk() {
    local path=$1 minimum=$2 available
    available=$(df --output=avail -BM "$path" | awk 'NR == 2 {sub(/M$/, "", $1); print $1}')
    [[ $available =~ ^[0-9]+$ && $available -ge $minimum ]] || fail "需在 $path 留出至少 ${minimum} MiB。"
}

check_docker_network_policy() {
    local version pid binary config=/etc/docker/daemon.json index argument
    local -a arguments
    version=$("${docker[@]}" version --format '{{.Server.Version}}') || fail '无法访问本机 Docker Engine。'
    [[ $version =~ ^([0-9]+)\.[0-9]+\.[0-9]+ && ${BASH_REMATCH[1]} -ge 28 && $version != *-rc* ]] \
        || fail '需要 Docker Engine 28 或更新的正式版本。'
    pid=$(systemctl show docker.service --property=MainPID --value) || fail '无法核对本机 Docker 服务。'
    [[ $pid =~ ^[1-9][0-9]*$ ]] || fail '需要由 systemd 管理的本机 Docker 服务。'
    binary=$(readlink "/proc/$pid/exe") || fail '无法读取本机 Docker 进程。'
    [[ ${binary##*/} == dockerd ]] || fail 'Docker 服务进程身份不匹配。'
    mapfile -d '' -t arguments < "/proc/$pid/cmdline" || fail '无法核对本机 Docker 启动参数。'
    for ((index=1; index<${#arguments[@]}; index++)); do
        argument=${arguments[index]}
        case $argument in
            --config-file) config=${arguments[++index]} ;;
            --config-file=*) config=${argument#*=} ;;
            --allow-direct-routing=false|--iptables=true) ;;
            --allow-direct-routing|--allow-direct-routing=*|--iptables=*)
                fail 'Docker 须关闭直连路由并保留防火墙规则。' ;;
        esac
    done
    if [[ $config != /* ]]; then config="/proc/$pid/cwd/$config"; fi
    if [[ -e $config ]]; then
        python3.12 -c 'import json, sys; cfg = json.load(open(sys.argv[1], encoding="utf-8")); sys.exit(cfg.get("allow-direct-routing", False) is not False or cfg.get("iptables", True) is not True)' "$config" \
            || fail 'Docker 须关闭直连路由并保留防火墙规则。'
    fi
}

check_oxr_network() {
    local attached=$1 observed
    observed=$("${docker[@]}" network inspect --format \
        '{{.Driver}}|{{.Scope}}|{{.Internal}}|{{.EnableIPv6}}|{{index .Options "com.docker.network.bridge.gateway_mode_ipv4"}}|{{index .Options "com.docker.network.bridge.trusted_host_interfaces"}}|{{index .Options "com.docker.network.bridge.enable_icc"}}|{{index .Labels "io.chriptmas.component"}}|{{range .Containers}}{{.Name}},{{end}}' \
        "$network") || fail '无法核对 OXR 专用网络。'
    [[ $observed == "bridge|local|false|false|nat||false|oxr|$attached" ]] \
        || fail 'OXR 专用网络的驱动、隔离配置或已连接容器不匹配。'
}

preflight() {
    local command details card capability total free storage anchor app_root storage_dev anchor_dev containerd_dev listeners
    [[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || fail '需要 Linux x86_64 主机。'
    command -v nvidia-smi >/dev/null || fail '需要可用的 NVIDIA GPU 和驱动。'
    details=$(nvidia-smi -i "$gpu") || fail '无法读取指定 NVIDIA GPU。'
    [[ $details =~ CUDA\ Version:\ ([0-9]+) && ${BASH_REMATCH[1]} -ge 13 ]] || fail 'NVIDIA 驱动须兼容 CUDA 13。'
    card=$(nvidia-smi -i "$gpu" --query-gpu=uuid,compute_cap,memory.total,memory.free --format=csv,noheader,nounits | tr -d ' ')
    IFS=, read -r gpu_uuid capability total free <<< "$card"
    [[ $gpu_uuid =~ ^GPU-[a-fA-F0-9-]+$ && $card != *$'\n'* && $capability =~ ^([0-9]+)\.[0-9]+$ \
        && ${BASH_REMATCH[1]} -ge 8 && $total =~ ^[0-9]+$ && $free =~ ^[0-9]+$ \
        && $total -ge 23000 && $free -ge 20000 ]] || fail '需要支持 BF16 的单张 24 GB 级显卡，当前可用显存至少 20000 MiB。'
    for command in docker git python3.12 curl ss systemctl; do
        command -v "$command" >/dev/null || fail "缺少 $command。"
    done
    check_docker_network_policy
    storage=$("${docker[@]}" info --format '{{.DockerRootDir}}') || fail '无法访问本机 Docker Engine。'
    [[ -d $storage ]] || fail '无法核对 Docker 数据目录的可用空间。'
    "${docker[@]}" info --format '{{range $name, $_ := .Runtimes}}{{println $name}}{{end}}' | grep -Fx nvidia >/dev/null \
        || fail 'Docker 尚未配置 NVIDIA Container Toolkit。'
    [[ $root = /* && $root != / && $root != *,* && $root != *:* ]] || fail 'OXR 数据目录必须是有效的 Linux 绝对路径。'
    root=$(realpath -m -- "$root")
    app_root=$(realpath -m -- "${CHRIPTMAS_APP_ROOT:-/var/lib/chriptmas}")
    [[ $root != / && $root != "$app_root" && $root != "$app_root/"* && $app_root != "$root/"* ]] \
        || fail 'OXR 数据目录须与应用资料目录分开。'
    anchor=$root
    if [[ ! -d $anchor ]]; then anchor=$(dirname -- "$root"); fi
    [[ -d $anchor ]] || fail 'OXR 数据目录的父目录须已存在。'
    storage_dev=$(stat -c %d "$storage")
    anchor_dev=$(stat -c %d "$anchor")
    if [[ $storage_dev == "$anchor_dev" ]]; then
        check_disk "$storage" 40960
    else
        check_disk "$storage" 32768
        check_disk "$anchor" 8192
    fi
    if [[ -d /var/lib/containerd ]]; then
        containerd_dev=$(stat -c %d /var/lib/containerd)
        if [[ $containerd_dev != "$storage_dev" ]]; then
            if [[ $containerd_dev == "$anchor_dev" ]]; then
                check_disk /var/lib/containerd 40960
            else
                check_disk /var/lib/containerd 32768
            fi
        fi
    fi
    if "${docker[@]}" container inspect "$container" >/dev/null 2>&1; then
        fail "容器 $container 已存在，请先读取其状态。"
    fi
    if "${docker[@]}" network inspect "$network" >/dev/null 2>&1; then check_oxr_network ''; fi
    listeners=$(ss -H -ltn 'sport = :8000') || fail '无法核对本机监听端口。'
    [[ -z $listeners ]] || fail '宿主机端口 8000 已被占用。'
    printf 'OXR 预检通过。GPU %s，显存 %s MiB 可用，Docker 数据目录 %s。\n' "$gpu_uuid" "$free" "$storage"
}

install_oxr() {
    local source weights tools vllm_image
    preflight
    umask 077
    mkdir -p -- "$root"
    source="$root/source-${code_revision:0:12}"
    weights="$root/weights-${model_revision:0:12}"
    tools="$root/download-tools"
    if [[ ! -e $source ]]; then git init -q "$source"; fi
    [[ -d $source/.git ]] || fail 'OXR 源码目录缺少独立的 Git 仓库。'
    if git -C "$source" rev-parse --verify HEAD >/dev/null 2>&1; then
        [[ $(git -C "$source" rev-parse HEAD) == "$code_revision" && -z $(git -C "$source" status --porcelain) ]] \
            || fail 'OXR 源码目录有其他版本或本地改动。'
    else
        git -C "$source" fetch --depth 1 https://github.com/SoMarkAI/OXR.git "$code_revision"
        git -C "$source" switch --detach "$code_revision"
    fi
    vllm_image=$(awk -F= '/^ARG VLLM_IMAGE=/ {print $2}' "$source/docker/Dockerfile.allinone")
    [[ -n $vllm_image ]] || fail '上游 Dockerfile 缺少固定的 vLLM 基础镜像。'
    if ! "${docker[@]}" image inspect "$vllm_image" >/dev/null 2>&1; then
        "${docker[@]}" pull "$vllm_image"
    fi
    "${docker[@]}" run --rm --pull=never --gpus "device=$gpu_uuid" --entrypoint nvidia-smi "$vllm_image" -L
    "${docker[@]}" build -f "$source/docker/Dockerfile.allinone" -t "$image" "$source"

    if [[ ! -x $tools/bin/hf ]]; then
        python3.12 -m venv "$tools"
        "$tools/bin/python" -m pip install huggingface_hub
    fi
    env -u HF_TOKEN -u HUGGING_FACE_HUB_TOKEN HF_ENDPOINT=https://huggingface.co HF_HUB_OFFLINE=0 \
        HF_HUB_DISABLE_IMPLICIT_TOKEN=1 HF_HUB_DISABLE_TELEMETRY=1 \
        "$tools/bin/hf" download SoMarkAI/OXR-1.0 --revision "$model_revision" --local-dir "$weights"
    if ! "${docker[@]}" network inspect "$network" >/dev/null 2>&1; then
        "${docker[@]}" network create --driver bridge --ipv6=false --label io.chriptmas.component=oxr \
            --opt com.docker.network.bridge.gateway_mode_ipv4=nat \
            --opt com.docker.network.bridge.enable_icc=false "$network" >/dev/null
    fi
    check_oxr_network ''
    "${docker[@]}" run -d --pull=never --name "$container" --restart unless-stopped --stop-timeout 40 \
        --network "$network" --publish 127.0.0.1:8000:8000/tcp --ipc private --shm-size=2g \
        --gpus "device=$gpu_uuid" --log-driver local \
        --mount "type=bind,source=$weights,target=/models/oxr-model,readonly" \
        -e OXR_API_HOST=0.0.0.0 -e VLLM_HOST_IP=127.0.0.1 -e HF_HUB_OFFLINE=1 -e PIP_NO_INDEX=1 \
        -e DO_NOT_TRACK=1 -e HF_HUB_DISABLE_TELEMETRY=1 \
        -e OXR__LAYOUT_ANALYZE_MODEL__BATCH_SIZE=1 -e OXR__LAYOUT_ANALYZE_MODEL__MAX_CONCURRENCY=1 \
        -e OXR__OXR_MODEL__MAX_CONCURRENCY=1 \
        -e HTTP_PROXY= -e HTTPS_PROXY= -e ALL_PROXY= -e http_proxy= -e https_proxy= -e all_proxy= \
        "$image" >/dev/null
    printf 'OXR 已启动，初始化完成后执行 %s ready。\n' "$0"
}

ready() {
    local status
    check_docker_network_policy
    status=$("${docker[@]}" container inspect --format \
        '{{.Config.Image}} {{.HostConfig.NetworkMode}} {{.HostConfig.IpcMode}} {{.State.Health.Status}} {{range $name, $_ := .NetworkSettings.Networks}}{{$name}},{{end}}' "$container") \
        || fail '没有找到 OXR 容器或上游健康检查。'
    [[ $status == "$image $network private healthy $network," ]] || fail "容器尚未就绪。当前镜像、网络及状态为 $status。"
    check_oxr_network "$container,"
    "${docker[@]}" container inspect --format '{{json .NetworkSettings.Ports}}' "$container" \
        | python3.12 -c 'import json, sys; ports = {name: values for name, values in json.load(sys.stdin).items() if values}; sys.exit(ports != {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]})' \
        || fail 'OXR 实际端口映射须只包含宿主机 127.0.0.1:8000。'
    curl --noproxy '*' --fail --silent --show-error --max-time 5 http://127.0.0.1:8000/openapi.json \
        | python3.12 -c 'import json, sys; sys.exit(0 if "post" in json.load(sys.stdin)["paths"].get("/v1/parse/sync", {}) else 1)' \
        || fail '同机 API 未报告 POST /v1/parse/sync。'
    printf 'OXR API 可访问。http://127.0.0.1:8000/v1\n'
}

case ${1:-} in
    preflight) preflight ;;
    install) install_oxr ;;
    ready) ready ;;
    *) fail '用法 bash deploy/oxr-local.sh {preflight|install|ready}' ;;
esac
