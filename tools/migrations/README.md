# 认识文档并入旧 OS 文档权威

本目录保存第二阶段的离线、可重入迁移工具。迁移不删除原 `recognition.sqlite3`，也不修改工作台原件。默认先只读预检；`--apply` 必须提供三份 SQLite 数据库备份目录。

先停止所有指向目标 vault 的网页、桌面和后台写入进程。以 `F:/ChriptmasAgent/runtime` 为当前源码运行 vault 的示例：

```powershell
$env:PYTHONPATH = 'F:/ChriptmasAgent/src'
$python = 'F:/ChriptmasAgent/.venv/Scripts/python.exe'
$root = 'F:/ChriptmasAgent/runtime'
$backup = Join-Path 'F:/Chriptmas_OS/work/agent-checkpoint-20260924' ('pre-cutover-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
& $python tools/migrations/backup_pre_cutover.py --runtime-root $root --backup-dir $backup
& $python tools/migrations/merge_recognition_records.py --runtime-root $root
& $python tools/migrations/merge_recognition_records.py --runtime-root $root --backup-dir $backup --apply
& $python tools/migrations/activate_unified_documents.py --runtime-root $root --repository-root F:/ChriptmasAgent
& $python tools/migrations/activate_unified_documents.py --runtime-root $root --repository-root F:/ChriptmasAgent --backup-dir $backup --apply
```

备份目录必须在 vault 外且为空；已存在的备份不要覆盖。目标数据库已有不同内容的同键记录、旧 JSON Document 存在、文档修订不一致、权威标记冲突时工具会拒绝切换。只有 `sqlite_active` 的完整证据通过后，应用才把新旧文档读写指向同一结构化 SQLite。重启后应核对旧资料库、工作项、原件和文档修订；保留备份与原认识库以便追溯。

切换后，旧版已确认工作项仍保留原 Document 与 `workspace://` 引用。用 `backfill_workspace_sources.py` 为缺少 `source_id` 的记录幂等补建 Source；先预览，再停写入进程、另取新备份后 `--apply --backup-dir`。重启会恢复已冻结但未提交的回填操作。

## 历史音频工作流项目投影修复

旧音频流程曾把非默认项目的独立 `audio_auto_workflows` 及 Source 内嵌投影写成 `default`。修复工具默认只读，列出工作流与 Source ID、当前及目标项目、修订号、审核意图和 Job 身份、可修复或跳过原因。Source 中只有内嵌投影而缺少独立工作流时也会列为跳过。清单不输出转写正文、Source 正文、原件路径或凭据。

```powershell
$env:PYTHONPATH = 'F:/ChriptmasAgent/src'
$python = 'F:/ChriptmasAgent/.venv/Scripts/python.exe'
& $python tools/migrations/repair_audio_workflow_projects.py --runtime-root F:/ChriptmasAgent/runtime
```

应用前检查清单和备份，停止所有指向该 runtime 的网页、桌面及后台写入进程。JSON 对象存储的 `expected_revision` 只提供进程内修订检查，不提供跨进程原子事务；`--confirm-offline` 表示操作者已确认无并发写入。工具逐项重读 workflow、Source、审核意图和路由 Job，仅对四者身份、非默认项目及当前修订均一致的记录，分别以 `expected_revision` 写入独立工作流和 Source 内嵌投影的 `project_id`；冲突跳过，可再次预览并幂等续跑。它不会重跑 ASR、移动原件或改动审核意图与 Job。

```powershell
& $python tools/migrations/repair_audio_workflow_projects.py --runtime-root F:/ChriptmasAgent/runtime --apply --confirm-offline
```
