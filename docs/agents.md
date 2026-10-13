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

DeepSeek Harness 发布版会过滤父环境中带 `KEY` 的变量。外部客户端连接设备鉴权服务器时，生成的 JSON 片段尚未补充这层传递，需要在 `cordis.patch.yml` 的 `memory-mcp.config.env` 中显式加入下面的固定环境引用，保留原来的地址和四个模型密钥空覆盖。变量值仍由启动进程提供，文件只保存引用。这沿用[官方 MCP 环境配置](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.1.5-rc.1/packages/mcp/mcp-client/README.md)，公网外部客户端的完整连接尚未验收。

```yaml
CHRIPTMAS_DEVICE_KEY: !!js process.env.CHRIPTMAS_DEVICE_KEY
```

只有管理员明确选择其他用户空间时才另设 `CHRIPTMAS_MCP_TARGET_USER`；服务器本机派活的专用 profile 已自动传递当前已鉴权目标。

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

Windows 本机自动发现 Codex `0.156.1`、Claude Code `2.1.257` 及后续 `2.1` 补丁版。运行器须有可发现的原生可执行文件，其路径及上层目录须为本机实体路径，符号链接或 junction 会被拒绝。安装后重启 Incremind，让可信启动装配重新发现版本。

DeepSeek 的可选运行包装在本机数据根下的独立目录。默认数据根为 `runtime` 时，在仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install --target .\runtime\external_executor_runtimes\deepseek-harness-0.1.5rc1 -r requirements-external-agents.txt
```

当前 DeepSeek 适配器核验的是 `deepseek-harness-runtime-bin==0.1.5rc1`，原生版本 `0.1.5-rc.1`，使用该发布版的 SDK JSON-RPC。它复用 **设置 · 模型 · 生成** 中官方 `https://api.deepseek.com` 或 `/v1` 的 OpenAI 兼容配置与密钥。

如果启动时指定了其他数据根，把命令中的 `runtime` 换成该目录；安装到普通虚拟环境不会被这个独立运行包入口发现。完成后重启应用。

Claude Code 与 Codex 的本机派活使用受控 API 密钥：在启动 Incremind 的进程环境中分别设置 `ANTHROPIC_API_KEY`、`CODEX_API_KEY`，然后重启应用。宿主只挑选这两项并按执行者分别传入，不读取已有登录文件。Codex 的非交互 API 覆盖变量须使用 `CODEX_API_KEY`。变量约定见 [Codex 非交互认证](https://learn.chatgpt.com/docs/non-interactive-mode#authenticate-in-automation) 和 [Claude Code 环境变量](https://code.claude.com/docs/en/env-vars)。密钥不写进任务目录、连接配置或回执；MCP 子进程不接收模型密钥。设置页的凭据已配置表示存在受控输入，账号是否有效仍由实际运行结果决定。

DeepSeek 运行包包含原生程序及闭合 Node 依赖，无需另装系统 Node。安装包与独立命令的使用方式见 [官方发布版运行包说明](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.1.5-rc.1/python/sdk-runtime/README.md)。

### Linux 服务器上的 DeepSeek Harness

服务器只开放 DeepSeek Harness 派活。运行包固定安装在 `/opt/chriptmas/external_executor_runtimes/deepseek-harness-0.1.5rc1`，版本同上；还需安装非 SUID 的 `bubblewrap`。2026-10-13 的服务器安装使用 bubblewrap `0.6.1`。

应用服务须以普通用户运行，使用 root 维护的实体 Python 环境；解释器、依赖及上层目录不能由服务用户写入，可信挂载内容不能允许组或其他用户写入。普通 venv 的解释器链接不满足要求，应使用 `venv --copies` 并检查其基础 Python 环境；基础 Python、依赖和任务所需最小源码均以可信只读方式挂载。用户 namespace 必须实际可用；启动装配会运行原生探测并确认进程域关闭，任一检查失败就保持不可用。

通过 systemd override 切换解释器时，只替换原 `ExecStart` 中的解释器路径，保留原 ASGI 模块、监听地址、端口和其他全部启动参数，以及原数据根、环境与权限配置。

每个任务进入独立的用户、挂载与 PID namespace，只挂载可信只读依赖和当前任务的可写目录。正式用户资料、其他用户空间及服务器主密钥不挂入任务；网络用于调用官方 DeepSeek 和本机已鉴权记忆接口。模型和密钥取自操作用户现有的生成配置，不读取其他用户配置。专用 YAML profile 仅用两个固定环境引用在内存中传递设备钥匙与目标用户，文件不保存凭据明文，四个模型密钥不传给 MCP。

原生任务启动前重验同一设备、目标用户、模型密钥修订、外发开关及资料私密状态；失效即停止。取消、超时和正常退出均由原进程 owner 确认整个 PID 域关闭，再结束原运行槽。服务器 Claude Code 与 Codex 派活保持不可用。

每个任务使用独立目录和冻结的交接内容，通过原任务内核、外发回执与运行槽执行。当前工作台提供工作目录档位，关闭命令执行；DeepSeek 的专用 profile 仅装配模型、SDK 与本项目 MCP，不加载文件系统、Shell、子 agent、会话日志上传或遥测插件。它可在执行中调用上面的七个记忆工具，最终文字只新建草稿。

外部选择只用于干活。明确的问、记住以及带附件的输入仍走已有流程；继续编辑已有文档使用内置流程。服务器未通过上述隔离检查时不开放外部派活，服务器上的记忆访问可单独使用。

运行器受原内核取消令牌、超时和关闭管理，限制输出大小，并负责结束整个进程树；当前工作台没有用户取消按钮。应用中断后只核对原任务的真实结果并结束回执，不自动再次执行；清理结果无法确认时保留原运行槽，避免重复副作用。

### 验证范围

Windows 原生运行器、完整应用界面及桌面手机明暗检查使用隔离数据和合成模型响应。2026-10-13 已在 Linux 服务器隔离空间复用现有 `deepseek-flash` 密钥完成真实派活：官方 Harness 成功经鉴权 MCP 取得当前项目，再由原 Kernel 保存可打开的未核对草稿。29 项检查通过，包括工具成功正文及输出 schema、幂等、跨用户隔离、模型密钥和设备钥匙不落任务文件或回执；QA 凭据和设备已清理。

本轮共两次真实任务。首次暴露官方环境过滤导致的设备钥匙缺失，修复后第二次完整通过，累计输入 4017、输出 1226 tokens。原失败与用量保留。适配源码已发布并同步到服务器，正式启用须满足上述运行环境和隔离准入；Claude Code、Codex 真实账号和手机公网派活仍需按各自环境验收。

同日另以普通服务用户、root 维护的新实体 Python 和正式源码，在相同隔离限制下完成官方原生版本 `0.1.5-rc.1` 探测及 `setsid` 派生进程取消复验；原进程 owner、namespace 和进程树全部关闭。该项未使用密钥、模型调用为 0，与上述真实模型派活的 29 项验收分别记录。

## 方法导出为 skill

资料库里已生效、带适用条件的方法认识可以导出成 [Agent Skills](https://agentskills.io/specification) 格式（`SKILL.md`）。草稿里核对触发条件、步骤和来源编号，审阅后下载 zip，或写入你选定的已有文件夹。导出不会改动认识；来源认识变了，skill 会标"需更新"。

解压后把整个 `<name>` 文件夹放到客户端的位置：

| 客户端 | 当前项目 | 自己的全部项目 |
|---|---|---|
| Claude Code | `.claude/skills/<name>/` | `~/.claude/skills/<name>/` |
| Codex | `.agents/skills/<name>/` | `~/.agents/skills/<name>/` |

位置依据 [Claude Code](https://code.claude.com/docs/en/skills) 和 [Codex](https://learn.chatgpt.com/docs/build-skills) 的官方说明。导出不会安装客户端，也不会自动写入这些目录。
