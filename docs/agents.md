# 接入 Claude Code、Codex 与 DeepSeek Harness

三种客户端通过版本化插件接入，支持两个方向：它们调用 Incremind 的记忆工具；你在工作台把干活交给它们，结果保存为草稿。设置入口是 **设置 · 模型 · 外部 agent**。

插件固定登记在项目代码中，分别管理启用状态、客户端身份和运行器版本。设置页会显示安装、凭据与可用状态，不接受任意插件代码、路径或启动命令。

## 它们来问你的记忆：MCP 记忆服务

先启动 Incremind 后端，打开外部 agent 外发开关和对应客户端开关。运行客户端的电脑需要本项目的 Python 环境及 MCP 依赖。设置页提供 Claude Code、Codex 的添加命令，使用固定脚本入口，形如：

```bash
claude mcp add --transport stdio chriptmas-memory --env CHRIPTMAS_MCP_BACKEND_URL=http://127.0.0.1:8001 -- <Python绝对路径> -I <仓库绝对路径>/src/backend/memory_app/mcp.py
```

Codex 使用设置页的 `codex mcp add` 命令。路径必须指向运行客户端的电脑；从服务器网页复制时，把服务器上的 Python 和脚本路径替换为本机安装路径。`-I` 保持 Python 隔离模式，不依赖 `PYTHONPATH`。

DeepSeek Harness 使用同一页面提供的配置片段。它是一个 JSON 数组，也是有效的 YAML：把数组中的插入项合并到所用 profile 的 `cordis.patch.yml`，启用官方 `@deepseek-ai/dsh-mcp-client`，再启动该 profile。保留已有配置，避免覆盖整个文件。配置中的 `failOnStartupError: true` 会让连接失败明确报错。

三种客户端都通过 stdio 启动这个小进程，再由它访问后端，不直接打开数据库。DeepSeek 的真实握手身份 `dsh-mcp-client` 会被记为 `deepseek-harness`。

### 连接服务器上的记忆

客户端仍在本机启动 MCP 脚本，把 `CHRIPTMAS_MCP_BACKEND_URL` 设为服务器的 HTTPS 根地址，并在启动客户端的进程环境中提供既有设备钥匙 `CHRIPTMAS_DEVICE_KEY`。地址不能带账号、密码、查询参数或额外路径。钥匙只经 Bearer 请求头发送，不写入复制出来的配置。可在设备设置里单独作废对应设备。

桥接进程验证 TLS，关闭重定向和环境代理。本机 HTTP 仅接受 `127.0.0.1` 或 `::1`。这是 stdio 到已鉴权 HTTPS 接口的连接方式；原生远程 MCP `/mcp` 与 ChatGPT 接入仍按阶段 16 的计划另行验收。

| 工具 | 做什么 | 会写入吗 |
|---|---|---|
| `projects` | 列出可用的项目和一句概览（不含私密项目） | 否 |
| `recall` | 按记忆阶梯召回，返回带编号的条目 | 否 |
| `methods` | 按情境找该用的方法 | 否 |
| `read` | 下钻某个编号的原文证据 | 否 |
| `remember` | 作为原件入库，标"未核对"，记下来源客户端 | 原件 |
| `propose_insight` | 提一条待确认的认识 | 待确认认识 |
| `report_use` | 回报用到了哪些编号 | 使用记录 |

外部 agent 只能**提议**：可以记住原件、提待确认认识，不能确认、修改或遗忘认识。交给它们的内容算外发，私密内容不交，每次都写回执。

## 你派活给它们：外部执行者

工作台输入框旁可临时选择 Claude Code、Codex 或 DeepSeek Harness。只列出已安装、启用且通过当前准入的执行者，默认仍为内置。提交前会重新检查；执行失败会显示失败原因。

当前自动发现面向 Windows 本机：Codex 核验 `0.156.1`，Claude Code 核验 `2.1.257` 及后续 `2.1` 补丁版，运行器须有可发现的原生可执行文件，其路径及上层目录须为本机实体路径，符号链接或 junction 会被拒绝。安装后重启 Incremind，让可信启动装配重新发现版本。

DeepSeek 的可选运行包装在本机数据根下的独立目录。默认数据根为 `runtime` 时，在仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install --target .\runtime\external_executor_runtimes\deepseek-harness-0.1.5rc1 -r requirements-external-agents.txt
```

当前 DeepSeek 适配器核验的是 `deepseek-harness-runtime-bin==0.1.5rc1`，原生版本 `0.1.5-rc.1`，使用该发布版的 SDK JSON-RPC。它复用 **设置 · 模型 · 生成** 中官方 `https://api.deepseek.com` 或 `/v1` 的 OpenAI 兼容配置与密钥。

如果启动时指定了其他数据根，把命令中的 `runtime` 换成该目录；安装到普通虚拟环境不会被这个独立运行包入口发现。完成后重启应用。

Claude Code 与 Codex 的本机派活使用受控 API 密钥：在启动 Incremind 的进程环境中分别设置 `ANTHROPIC_API_KEY`、`CODEX_API_KEY`，然后重启应用。宿主只挑选这两项并按执行者分别传入，不读取已有登录文件。Codex 的非交互 API 覆盖变量须使用 `CODEX_API_KEY`。变量约定见 [Codex 非交互认证](https://learn.chatgpt.com/docs/non-interactive-mode#authenticate-in-automation) 和 [Claude Code 环境变量](https://code.claude.com/docs/en/env-vars)。密钥不写进任务目录、连接配置或回执；MCP 子进程不接收模型密钥。设置页的凭据已配置表示存在受控输入，账号是否有效仍由实际运行结果决定。

DeepSeek 运行包包含原生程序及闭合 Node 依赖，无需另装系统 Node。安装包与独立命令的使用方式见 [官方发布版运行包说明](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.1.5-rc.1/python/sdk-runtime/README.md)。

每个任务使用独立目录和冻结的交接内容，通过原任务内核、外发回执与运行槽执行。当前工作台提供工作目录档位，关闭命令执行；DeepSeek 的专用 profile 仅装配模型、SDK 与本项目 MCP，不加载文件系统、Shell、子 agent、会话日志上传或遥测插件。它可在执行中调用上面的七个记忆工具，最终文字只新建草稿。

外部选择只用于干活。明确的问、记住以及带附件的输入仍走已有流程；继续编辑已有文档使用内置流程。服务器尚无满足要求的执行隔离时不开放外部派活，服务器上的记忆访问可单独使用。

运行器受原内核取消令牌、超时和关闭管理，限制输出大小，并负责结束整个进程树；当前工作台没有用户取消按钮。应用中断后只核对原任务的真实结果并结束回执，不自动再次执行；清理结果无法确认时保留原运行槽，避免重复副作用。

### 验证范围

已用官方 DeepSeek 发布运行器连接本机合成模型响应，验证受控进程与 MCP 协议；界面以完整应用和隔离数据检查了桌面、手机及明暗主题。合成模型响应不证明真实账号登录或模型回答质量。真实 Claude Code、Codex、DeepSeek 账号派活与公网设备连接仍需按实际安装环境验收。

## 方法导出为 skill

资料库里已生效、带适用条件的方法认识可以导出成 [Agent Skills](https://agentskills.io/specification) 格式（`SKILL.md`）。草稿里核对触发条件、步骤和来源编号，审阅后下载 zip，或写入你选定的已有文件夹。导出不会改动认识；来源认识变了，skill 会标"需更新"。

解压后把整个 `<name>` 文件夹放到客户端的位置：

| 客户端 | 当前项目 | 自己的全部项目 |
|---|---|---|
| Claude Code | `.claude/skills/<name>/` | `~/.claude/skills/<name>/` |
| Codex | `.agents/skills/<name>/` | `~/.agents/skills/<name>/` |

位置依据 [Claude Code](https://code.claude.com/docs/en/skills) 和 [Codex](https://learn.chatgpt.com/docs/build-skills) 的官方说明。导出不会安装客户端，也不会自动写入这些目录。
