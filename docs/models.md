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

### 2026-10-12 兼容性核对

本次对照 pi-ai 提交 [0f4b146](https://github.com/earendil-works/pi/commit/0f4b1465a444a53274ba02df6d4d14a2f04b1596)，发布版 v1.1.0。同步范围为临时繁忙错误的识别与既有重试链的交接，参考上游 [server_busy 修复](https://github.com/earendil-works/pi/commit/8b5708dbb1b4a819f924b43a2de02c9cc8c7a46d)。重试仍由当前领域策略决定，每次尝试沿用来源复验和回执；认证、额度、拒绝、已开始正文或工具输出的请求不得因繁忙提示重发，原始提供方诊断不进入公开错误。

LiteLLM 包装丢失字段时，只从相同 HTTP 状态的原始官方 SDK 异常读取结构化错误，不扫描异常字符串。后台任务遇到可恢复繁忙时，继续读取同一响应的 GET；额度错误停止续读，不重新发送 POST。

其余变化按当前实现核对：

- [上下文估算](https://github.com/earendil-works/pi/commit/27075fe07597fbb9ada7a0b2e3ea9b9b34505cc0)：本项目按序列化输入的 UTF-8 字节数除以 3 估算，比上游每 token 3.5 字符更保守，保持现有预算。
- [长提示分档价格](https://github.com/earendil-works/pi/commit/943a10e744b87d9a593f3dca7406d4671910f490)：本项目未加载 models.dev、OpenRouter 或 Vercel 价格目录，只内置已核验的 DeepSeek CNY 费率，其余沿用人工配置或未知；本次不引入未经核验的目录价格。
- [Mistral 超时](https://github.com/earendil-works/pi/commit/6b07b4e57269ebc4b3fe2b7cd3caba2d95e26cc0)：本项目没有该原生适配；受管传输已区分等待响应头、读取空闲和总时限，保留总预算及取消保护。
- [登录应用名称](https://github.com/earendil-works/pi/commit/9ad083102aa9afd483dbb5e0da53e69f2902c6a6)：已有 `agent_name_hint=ChriptmasAgent`，不增加登录参数。
- [Decisions 能力边界](https://github.com/earendil-works/pi/commit/ce8972a0e99cab79f2620a594a1c17baef436c67)：现有订阅只读取官方 OAuth 模型目录并调用 Responses，没有混入分类模型目录或 Decisions；本次不新增模型接口或订阅提供方。

### 2026-10-19 兼容性核对

本次核对从 `0f4b146` 到 [482c138](https://github.com/earendil-works/pi/commit/482c1381a00d8d00bc81697e4a980c593db6803e) 的 36 条 `packages/ai` 提交，覆盖 v1.2.0、v1.3.0 及其后的未发布修复。适配继续使用本项目网关和领域服务。

- [临时网关故障](https://github.com/earendil-works/pi/commit/698a2d23b86a5045dd54554354a5e0cb7e466795)和[服务暂不可用](https://github.com/earendil-works/pi/commit/550f1f3ecd75bf756ab3c379ca9503e808a6c6d1)：从结构化错误的代码、类型、消息和详情识别上游请求失败、上游读取中断、HTTP/2 中断及暂不可用。只有既有策略批准后才能重新尝试；认证、额度、拒绝和不支持错误优先，正文或工具输出开始后禁止重新派发。后台恢复继续沿原响应身份和 GET 预算执行。
- [完整工具参数校验](https://github.com/earendil-works/pi/commit/87f9a36419e8164bba77397d15c54ff39fac5e8a)：保留参数分片的全部字符，使用标准 JSON 解析器校验完整对象，拒绝截断、额外文本、非法常量以及多个工具混合形成的内容。工具参数在真实完成标记及最终校验后交付；普通正文继续流式交付，原有 prompt 与 fenced JSON 兼容规则保持。
- 运输完成与内容有效分别记录。完整运输后的参数拒绝保留实际报告的用量和原请求回执，应用结果继续报告校验失败；没有提供方终态的工具流不能取得完成资格。工具来源的 schema 拒绝直接结束当前结果，普通正文保留现有校验流程。公开错误只携带固定解码分类，不包含原始参数或提供方诊断。

其他上游变化按实际调用方式核对：

| 上游变化 | 本项目当前处理 |
|---|---|
| 纯空白工具结果使用占位内容 | 内部工具事实以普通消息传递，没有原生 `toolResult` 到 `function_call_output` 的转换入口，保持现有协议。 |
| 同一片段的推理与正文顺序 | 网关只把正文交给回答消费方；推理字段用于内部阶段观察。 |
| 切换模型时删除旧推理块 | 现有问答重新构造消息，没有跨模型的签名或加密推理块重放。 |
| GPT-6 推理强度切换保持缓存 | 上游明确排除 ChatGPT OAuth；本项目订阅仍使用请求级 `reasoning.effort`。 |
| OpenRouter 账号目录与价格、其他 provider 修复 | 现有模型配置没有导入这些目录或 pi-ai 的原生适配；自动费率继续限于已核验的 DeepSeek，未知费用保持未知。 |

订阅登录、模型选择、外发和私密判断继续复用既有服务与回执。
