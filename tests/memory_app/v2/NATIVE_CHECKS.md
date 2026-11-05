# Codex 原生受管 API 检查

`native_external_codex_api_check.py` 是显式集成检查，默认 pytest 文件匹配规则不会收集它。它保留原生发现、版本租约、真实 SQLite 与加密配置、网络脱敏、配置迟变以及历史归档恢复的全部检查；仅执行官方 CLI 的版本探测，不发送模型请求。

运行条件为 Windows x64 或 arm64、PATH 中可发现的官方 Codex CLI 0.156.1，以及可写的专用隔离数据目录。检查通过生产发现器解析原生安装及物理目录，不读取个人登录文件。使用 npm 包时，可在本次检查的进程环境中把已核验的官方原生 bin 目录置于 PATH 前端；生产发现器仍核验原生路径和版本。缺少条件或安装元数据无法通过生产校验时，显式运行会失败，不使用跳过标记或替代程序。

在仓库根目录运行，先创建专用 work 目录并将 `CHRIPTMAS_APP_ROOT` 设置为该目录的绝对路径：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/memory_app/v2/native_external_codex_api_check.py -q
```

`test_external_codex_api.py` 保留无需安装 Codex 的协议转换与 Core 契约检查。以上两个入口分别报告结果；版本探测通过不代表真实模型执行成功。公开任务的模型、进度、用量及草稿验收由独立授权的实际任务完成。
