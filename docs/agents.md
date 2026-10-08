# 接入 Claude Code、Codex

> 🧪 实验性：有完整的自动化测试，真实客户端的接入还在验收中。

Incremind 和通用 agent 分工：Incremind 决定"带什么上下文去干活、干完沉淀什么"，agent 负责"怎么动手"。接法有两种。

## 它们来问你的记忆：MCP 记忆服务

在 **设置 · 模型 · 外部 agent** 里打开外发开关，页面会给出一行添加命令，形如：

```bash
claude mcp add --transport stdio chriptmas-memory --env PYTHONPATH=<仓库>/src --env CHRIPTMAS_MCP_BACKEND_URL=http://127.0.0.1:8001 -- <python> -m backend.memory_app.mcp
```

Codex 用 `codex mcp add`，参数相同。这个小进程只通过本机接口访问后端，不直接打开数据库；后端需要先启动（`start.bat`）。

| 工具 | 做什么 | 会写入吗 |
|---|---|---|
| `projects` | 列出可用的项目和一句概览（不含私密项目） | 否 |
| `recall` | 按记忆阶梯召回，返回带编号的条目 | 否 |
| `methods` | 按情境找该用的方法 | 否 |
| `read` | 下钻某个编号的原文证据 | 否 |
| `remember` | 作为原件入库，标"未核对"，记下来源客户端 | 原件 |
| `propose_insight` | 提一条待确认的认识 | 候选 |
| `report_use` | 回报用到了哪些编号 | 使用记录 |

外部 agent 只能**提议**：可以记住原件、提待确认认识，不能确认、修改或遗忘认识。交给它们的内容算外发，私密内容不交，每次都写回执。

## 你派活给它们：外部执行者

"干活"可以交给 Claude Code 或 Codex 执行。每个任务一个独立工作目录，里面只有交接包、任务说明、你附带的文件和一份指向上面 MCP 服务的配置。产出只新建草稿，你的修改和重做会作为纠正沉淀回记忆。

## 方法导出为 skill

资料库里已生效、带适用条件的方法认识可以导出成 [Agent Skills](https://agentskills.io/specification) 格式（`SKILL.md`）。草稿里核对触发条件、步骤和来源编号，审阅后下载 zip，或写入你选定的已有文件夹。导出不会改动认识；来源认识变了，skill 会标"需更新"。

解压后把整个 `<name>` 文件夹放到客户端的位置：

| 客户端 | 当前项目 | 自己的全部项目 |
|---|---|---|
| Claude Code | `.claude/skills/<name>/` | `~/.claude/skills/<name>/` |
| Codex | `.agents/skills/<name>/` | `~/.agents/skills/<name>/` |

位置依据 [Claude Code](https://code.claude.com/docs/en/skills) 和 [Codex](https://learn.chatgpt.com/docs/build-skills) 的官方说明。导出不会安装客户端，也不会自动写入这些目录。
