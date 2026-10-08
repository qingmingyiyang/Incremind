# 模型与外发

## 配置模型

在 **设置 · 模型** 里按用途配置：

| 用途 | 做什么 | 可选档位 |
|---|---|---|
| 生成 | 整理、判断意图、自动归属、提炼认识、回答、干活 | 外接（任意 OpenAI 兼容接口，例如 DeepSeek）；本机小模型（可选） |
| 转写 | 录音、视频的语音转文字 | 外接（Hy-ASR）；本机 faster-whisper |
| 向量 | 语义召回 | 本机 EmbeddingGemma（在设置里一键安装，约 1.5 GB）；外接 |
| 识图 | 截图、照片识字和画面说明 | 本机（Windows 自带识别，Linux 可用 RapidOCR）；外接（支持图片输入的模型） |

- 推荐先接 DeepSeek：接口地址 `https://api.deepseek.com`，模型名按 DeepSeek 控制台填写。没有设置推理强度时，系统会关闭 DeepSeek 默认的"思考"，避免后台短任务被截断。
- 密钥在 Windows 上用 DPAPI 加密，存在数据根的 `secrets.json`，绑定当前 Windows 用户；保存后界面上读不回来。
- 本机小模型 `qwen2.5-1.5b-instruct` 需要先在设置里启用并选择，模型文件放在数据根的 `data/models/`。
- **ChatGPT 订阅**：在设置的模型组里登录，浏览器完成授权后选择可用模型，再按需打开外发开关，不需要 API Key。账号变化后需要重选模型；退出或认证失效时不会自动切换到 API。依据 OpenAI 官方的 [Sign in with ChatGPT](https://developers.openai.com/cookbook/articles/sign-in-with-chatgpt)。

## 外发规则

"外发"指把你的内容发给外部模型服务。

- 外发只受 **设置 · 模型** 里各用途的外发开关和**私密**标记控制，没有逐次弹窗。
- 私密项目和私密资料永远不外发；"私密"只表示不发给模型。
- 每次外发都写一条回执：发了哪些内容、哪个版本、给了哪个模型。
- 抓取你贴进来的链接（网页、公众号、视频页）不算外发：只是按你的要求下载内容。抓取会拒绝内网和本机地址、限制重定向和响应大小、不带 Cookie。抓回的内容交给模型整理时，才按外发规则走。
- 交给外部 agent（Claude Code、Codex）的内容也算外发，有自己单独的开关，默认关闭。

## 接入实现与参考

模型接入的入口是 `src/backend/memory_app/model_config.py` 和 `src/backend/shared/llm/litellm_gateway.py`（基于 [LiteLLM](https://github.com/BerriAI/litellm)）。

新增模型或做兼容性修改时，先对照 [earendil-works/pi 的 packages/ai](https://github.com/earendil-works/pi/tree/main/packages/ai)。重点看：

- 模型能力声明和 OpenAI 协议兼容参数；
- 流式响应与取消、工具调用与结构化输出；
- 推理选项、错误分类，以及 token、缓存和费用统计。

pi-ai 是 TypeScript 实现，本项目只借鉴它的协议适配和测试方法，不引入它作为运行依赖。

- 首次核验基线：提交 [7a11fe1](https://github.com/earendil-works/pi/commit/7a11fe1c723b942fa093edb8b00f051ddb9b6627)，发布版 [v0.99.2](https://github.com/earendil-works/pi/commit/005af57d88ee23b33778f343a9595b32e67ff788)。
- 2026-10-06 的订阅兼容更新对照了三处：
  - [取消期间保存轮换令牌](https://github.com/earendil-works/pi/commit/bde882c7471c47dc31af3c622496df66c4fae010)
  - [回调端口失败](https://github.com/earendil-works/pi/commit/eeac84ca92498ac18b6832754d01aef1d3c5f654)
  - [容量不足识别](https://github.com/earendil-works/pi/commit/3874b3e98983c70fa05fa193b675d42cfcb8b9f8)

订阅请求固定 `store=false`、`stream=true`。依据是 OpenAI 的[订阅推理协议](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)和[错误恢复规则](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery)。
