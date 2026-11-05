本机生成的默认检查覆盖资源缺失、真实格式损坏、分片路径、选择修订、认证和来源边界。

完整生成使用显式入口，要求真实 Qwen2.5-1.5B-Instruct 资源及项目声明的 Python 依赖。`--root` 指定明确分配的绝对隔离目录，资源位于其 `models/qwen2.5-1.5b-instruct`；同一独立目录需有项目支持的 `config/settings.toml`，ASR 可设为 CPU。Windows 使用短目录，保留真实能力包的文件路径。不得使用正式 runtime。入口不下载模型，不安装依赖，缺少条件直接报错。

```powershell
.\.venv\Scripts\python.exe tools/native_local_generation_check.py --root D:/path/to/ChriptmasAgent/work/local-native --asset-root D:/path/to/ChriptmasAgent/work/local-native/models
```

可追加 `--probe-only` 只读取文件、依赖和状态，完整权重不会加载。默认 `--case generation` 保留九个原本机成功节点：配置恢复、公开模式接口、两个服务器端口的原网关、两成员共用资源与串行输入、桌面密钥、不同端口的密钥范围、快速模型、正常服务器工厂。实际生成走原 `_generate`、Transformers 和资源池。全部检查共用正常服务器工厂持有的同一资源池，避免为每个节点重复加载权重。

第十个原订阅恢复节点通过 `--case subscription` 独立执行，需要同一隔离根的真实加密订阅账号。事先在正常界面选择本机、选择订阅、退出并重新登录，保留尚未重选模型的实际状态。检查读取真实账号及选择修订，验证重选要求，再沿原选择接口恢复本机。缺少账号或状态前提直接报错；入口不登录、不读取 CLI 认证文件、不构造凭据、不发送订阅模型请求。

设置读取使用正规 JSON、tokenizers、safetensors 和 Transformers meta 参数形状检查，实际权重加载只在发送生成请求时发生。资源或依赖具备条件显示待加载；实际加载、生成失败以及已加载资源发生变化时，状态按同一目录和文件身份读取。
