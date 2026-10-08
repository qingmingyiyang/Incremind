# 统一组件与公共能力索引

- T16.4 v2 原资源归属：`external_process._Owner` 复用现有有界采集器，统一管理原 Job、Popen HANDLE、流、描述符及读取/写入/watch 线程；资源创建纳入与关闭共用原清理锁，关闭意图阻止后续分配，清理尝试共用截止时间。原 Job 终止后查询活动数，并等待实际捕获的原进程对象退出；原 CloseHandle 成功后才清空身份，失败保留同一 owner 重试。`HostAdmission` / `HostLease` 在原映射中强持版本探测、进行中及待清理资源，关闭后接纳待清理资源只授予清理归属，不恢复执行权限。`execute_external` 仅在原资源确认关闭后，通过原 immutable payload、回执与 ExternalRuns CAS 保存终态和释放槽；物理清理失败保留原槽，领域收口仍由原 caller 回调负责。启动包 EOF 可开闸后，缺少私有 started 回报不能证明无副作用；内部 may_have_started 只校准原效果确定性，不改 started_at、旧 DTO 展示或持久化字段。验证 `test_external_process_cleanup` 与原 process/host/dispatch/execution/runtime 调用方；当前 Windows 原生故障与模拟 CLI 证据不代签真实登录、完整产品/MCP 或服务器隔离。

- T16.4 原生管道与清理归属：`core.plugin_hands.windows_appcontainer.launch_in_appcontainer` 复用原零网络 AppContainer、单进程 Job 和既有限额。默认 `stderr_reader="internal"` 保持原读取方式；可信宿主选择 `host` 后自行读取两条输出流并结束自己的读取线程，进程所有者继续管理原进程、Job、句柄和流。原 `_AnonymousStdio` 在 raw HANDLE、CRT descriptor 与 FileIO 转交期间保留当前资源身份；失败后只通过原 process/profile 活动项保留待回收资源，成功关闭后才清空。原 `WindowsContainedPluginHandsHost` 在清理成功后释放 profile 和活动项，失败允许同一所有者重试。验证 `test_plugin_hands_appcontainer_stdio`、`test_plugin_hands_contained_host` 与原 Windows AppContainer/ACL 调用方；该接点尚未接入 v2 双流采集，不证明真实 CLI 登录、MCP、研究或服务器隔离。

- T16.4 冻结执行接点：`ExternalContext.qualified_delivery` 回读原 Kernel 终态、effect、交付首修订及当前来源资格，返回脱离副本并脱敏。`external_runner.ExternalRunner.bind` 将原保存的任务、真实交付、控制文件及宿主准入绑定到原 Turn 不可变记录；`invoke` 核原持久 intent、精确原生契约和 frozen authorization facts，实际派发前再次复验材料和外发资格。`ProductTaskPlanner.handles` 仅让严格外部 task@2 避开内置模型路由。`external_execution.execute_external` 复用原工具控制、进程 owner、每用户 CAS 槽及原 Turn payload store，保存完整有界消息、终态与 operation receipt；启动证据在 CAS 之前记录，失败回放保留真实 effect certainty，不再次启动。两个原数据库不组成跨库事务，材料缺失时拒绝重跑。可信宿主须在原应用启动前注入；`external_runtime.install_external_runner` 沿原 installer 回调登记，复用同一 Context、records、Turn store、runtime 与冻结授权。默认应用保持未注册；启动后替换宿主会拒绝准备。
- T16.4 原应用授权与记忆准入：`ExternalExecutionAuthority` 回读原归档和交付，通过原扫描器核 TASK 与结构交接正文，以原项目 profile 修订签当轮批准；宿主版本探测前后使用短 `ensure_current`，不持根 profile 锁等待 CLI。`ExternalRunner.prepare` 由原 Kernel 接受与绑定，新的接受若准备失败沿原 `fail_accepted_turn` 收口，已终态回放保持。首次准备在原 records 的旁路集合 `v2_external_task_preparations` 以修订 0 原子占位，SDK 期间释放写事务；同 Turn 第二次准备不会再探测，失败占位保留，重试使用新 Turn。成功回放精确核调用输入并只读原绑定。`external_memory_admission.check_memory_configuration` 先检查真实依赖，缺依赖不占位；`require_memory_service` 以官方 SDK 探测一次目录交付，证明绑定同一原根的完成记录和回执，后续只读复验，不重复扣目录配额。唯一 Python MCP 参数含 `-I`，拒绝当前目录包覆盖；缺模块、路由、SDK或安全进程条件时在任务 CLI 启动前拒绝。完整 facts→CLI→Kernel 成功链仍未验收，T16.2 服务整合、已有登录、指定文件夹、附件和服务器隔离仍待完成。

- T16.4 MCP批准环境接线：`HostAdmission._approved_credentials`复用原可信秘密映射校验，`_memory_environment`只按原记忆准入设备钥匙白名单返回脱离副本，不交模型钥匙或继承环境；`ExternalRunner`的配置预检与实际准入两接点消费该投影，仍由原`external_memory_admission._environment`解析设备别名并屏蔽SDK默认继承项。错误保持固定码。空路由夹具只证明两个caller的环境实参，真实SDK参数和模拟CLI只证明环境消费与脱敏；缺模块/路由/安全进程条件仍原拒绝，不代签正式服务资格、握手、已有登录或完整产品运行。

- T16.4 宿主租约：`external_host.HostAdmission` 只接收应用固定的部署根、原生 CLI、认证根与内存秘密，重建 canonical argv，检查真实版本、许可修订及配置来源；`HostLease.accepted_turn` 是脱离副本，环境使用固定白名单且只留内存，caller 在 finally 关闭。`external_workspace.validate_task_path` 复用原符号链接与 Windows reparse 检查。许可读取集合为 `v2_external_host_permissions`，本接点没有新建许可写入界面。租约是时点复验，不提供 OS 原子路径锁；已有登录配置未知时拒绝，缺真实隔离启动器时 research、server 与 sandboxed commands 在版本探测前拒绝。本机空认证根及模拟 CLI 的通过证据不能证明已有登录、系统级沙箱或服务器隔离。

- T16.4 原内核执行契约：`project.task@2` 只选择 `external.task.execute@1`，绑定当前 Turn 的 `external-task-run-v1` 不可变引用，关闭三类自动上下文；原 `project.task@1` 保持。`external_execute` 仅接受精确原生工具契约，可信宿主通过 `external_execution_authority` 返回当轮 `BoundaryGrant`，原边界引擎保留硬拒绝、原 profile 修订与扫描要求，授权不写回 profile。`ai_tooling.contracts.is_external_task_execution_contract` 共用于原生构造与 manifest，只让此契约并行调用，原不可逆工具的独占规则保持；宿主仍以每用户运行槽和目录边界控制外部任务。元数据和引用格式不证明材料、用户、目录或启动权限；真实 immutable 绑定、宿主准入、外发复验及产品装配由后续运行器负责。

- T16.4 调用控制：`v2.external_dispatch.dispatch_external` 将原 `ToolExecutionContext` 的取消令牌和剩余期限传给既有进程 owner，CLI 默认最多 20 分钟；`ExternalEventParser.finished` 提供只读协议终态。结束事件在进程树清理后投影，未知用量保持空值，事件和输出保留容量上限与脱敏。调用方须先完成宿主准入，并依据最终返回结果写回执和释放运行槽，进度回调失败须避免发布未提交事实；此函数不授予外发或文件权限，也未安装产品执行器。验证采用原 Kernel 控制对象和真实模拟 CLI，真实登录与服务器隔离留集中验收。

- T16.4 版本适配器：`v2.external_adapters.build_launch_plan` 生成 `codex@1`、`claude-code@1` 的固定 argv 和文本 stdin，区分三个档位与命令请求，关闭扩权功能和会话保留；Claude 只预批准七个指定记忆工具。`v2.external_workspace.validate_memory_mcp_config` 与目录写入共用唯一 MCP 来源白名单，凭据保持环境引用，别名交给宿主解析。`LaunchPlan` 不授予外发、文件或命令权限；宿主仍须验证实际版本、托管配置、冻结 Turn、指定文件夹授权及 OS 隔离。本叶尚未启用产品运行器。验证 external_adapters、external_mcp_config、external_adapter_process 和原目录测试；模拟 CLI 覆盖真实进程、三档位、中文 cwd/stdin 与脱敏，集中 CLI 配置诊断绑定本机版本和合成 provider，不能推及已有登录或服务器环境。

- T16.4 运行底座：`v2.external_workspace.create_task_workspace` 保留原 `handoff@1` 的完整编号正文、任务及中文附件，MCP 使用固定绝对 Python 入口或宿主给定 HTTP 端点，凭据仅写环境引用；Windows 复用 `WindowsHandleTreeIo.write_new_tree`，POSIX 使用目录描述符和 `O_NOFOLLOW`，不覆盖旧任务。`v2.external_events.ExternalEventParser` 将 Codex/Claude JSONL 投影为有限事件和实际已报告用量；文字到终态统一脱敏后切片，开始、步骤及工具摘要即时输出。`v2.external_process.run_process` 以受控启动闸先绑定进程 owner，Windows Job 关闭全部后代，POSIX 使用进程组；双管道计原始字节、限额、超时及取消，返回有界脱敏尾部。进程 owner 不提供文件沙箱。这些底座尚未接入产品运行器；原交接资格、服务器 OS 隔离、版本适配器、并发与记录的产品接线由 T16.4 后续接点负责。验证 external_workspace、external_events、external_process 及 external_cli_pipeline；当前实测环境是 Windows 和模拟 CLI，POSIX 与真实 CLI 留集中验收。

- T16.4 运行记录：`v2.external_runs.ExternalRuns` 在原 `records.begin()` 事务内维护 `v2_external_runs` 与每用户运行槽，默认上限 1，由宿主传入可配置上限。`reserve` 仅首次占位返回 `reservation_created=True`，开始时间保持空；可信私有启动管道触发 `run_process(on_started=...)` 后才调用 `mark_started`。调用方在进程 owner 清理完毕并返回以后调用 `finish`，终态 CAS 与匹配槽释放同事务完成。崩溃后不按时间自动释放；同一终态回放只读返回，不释放后继任务的槽。用量只保存已报告的整数计数，未知为 `null`，不接受正文、环境和输出尾部。这是运行事实底座，不授予外发或执行权限，不改变原 Kernel 的事实责任；宿主需绑定真实用户、冻结 Turn 和 CLI 准入。验证 external_runs、external_process_started、external_run_process，以及受影响的原进程和模拟 CLI 集成测试。
- T16.8 桌面代理：`v2.external_proxy` 装配 `/api/v2/external-agent/proxy/{client}/v1/{chat/completions,responses,messages}`，原 socket 地址须为 loopback；服务器独立用户领域未就绪时拒绝。`external_proxy_protocol.last_user_text/insert_context` 处理最后用户消息，以独立 user 上下文保留原协议字段。`external_proxy_handoff.ProxyHandoff` 沿原范围解析、Query、画像、2000 token 阶梯和 Kernel 交接写回执，发送紧前复验原绑定；外发关闭、停用或失败保留客户端原 HTTP 字节。`external_proxy_transport.ProxyTransport` 用已有 HTTPX 向可信固定上游透传，凭据只用于当次内存请求，不继承默认认证或 cookies，不跟随重定向，SSE、压缩和错误字节保持，取消时关闭上游。客户端配置基址为 `/proxy/claude`（Messages）或 `/proxy/codex/v1`（OpenAI）；实际客户端验收另登记。
- T16.8 录制与保护：`external_proxy_settings` 独立旁路 CAS 保存两客户端的录制开关，默认关；设置沿原 Row/Switch 和 mono。`external_proxy_recording.ConversationRecorder` 旁观完整响应，显式 `proxy_record@1` 判断三协议终态、整段长度和短会话；整段脱敏后由原 WorkspaceIntake 创建原件，不发布认识。`proxy_context@1` 只管预算和资料标注，不改变 ACTIVE。原 `contextual_chunk_vectors.embedding_input_validation` 在可选向量缓存和 MemoryTurn 输入冻结前校验；原 `ExternalContext.prepare(validate_archive=...)` 在同事务归档前校验真实拟复制内容，回调仅收到副本。两接点默认关闭，原使用方保持原流程。

- T14.12 日志与请求编号：`shared.runtime_logging.install_runtime_logging` 在原应用工厂安装惰性 ASGI 外层，按部署根复用应用日志处理器，ContextVar 与引用租约覆盖请求、线程和后台任务；`bind_turn_id` 绑定实际 ASK/干活及幂等重放身份。`redact_log_text` 复用共享密钥规则并移除 URL 查询串，不采集正文；UTF-8 分段不超过 10 MiB，保留最近 14 天且拒绝符号链接与 Windows reparse point。原 `access_log` 使用同一脱敏函数；打包仅排除数据目录并保留 Python runtime。验证 runtime_logging、package_data_exclusion、原访问日志及实际 Turn/计时。

- T14.12 备份自检（第一阶段）：`shared/deployment.runtime_backup_roots` 统一原设置的数据库、快照与恢复操作目录；`memory_app.backup.backup_runtime` 复用原 SQLite 在线快照并启用临时恢复验证。`core/storage_provider/vault_backup_restore.verify_runtime_backup` 逐表比对记录数与最大修订，`verification_metadata` 读取旁路结果，`read_backup_catalog/write_backup_catalog` 复用原目录契约，`prune_automatic_backups` 只清理归属与结构可证的旧自动快照并保护恢复操作。`v2.daily.DailyBackup` 使用原进程间锁和事务 CAS，每日期一次，稳定 job_id/current snapshot 保持失败投影与并发重试幂等；`v2.jobs` 和 AppRouter 复用原托盘失败样式与设备传输。`tools/backup.py --verify` 独立验证已有快照；SettingsData 复用 Row、--ink2 ✓ 与 --red ✕，原因码仅 title。验证 backup_verification、jobs、daily、vault_transactional_restore_api、settingsData 与 appRouter；日志落盘和请求编号属于第二阶段。

- T12.8 纯下钻选材（第一叶）：`v2.ladder.start_ladder` 返回请求内 `LadderSelection`，在 L2 暂停并沿用同一预算、去重与上层 trace 续行；`WorkspaceQuery.prepare_drilldown/resume_drilldown/collect_lower_candidates` 复用原 collector、来源权限和时间资格，`v2.multi_query.expand_drilldown` 只融合已给定缺口问句的真实 L1/L0 结果，不发送 aux。策略 `retrieve@4` 经 get() 提供数值缺口判断、提示词、严格问句解码与下层 RRF 排序，ACTIVE 仍为 @3。新17控制分批及原42调用方通过；正式材料 aux、来源证明与质量验收仍待后叶。

- T16.1 交接能力：`v2.external_context.ExternalContext` 通过原 Kernel 的 `external.context.execute` 交付已选择材料，冻结 handoff@1、对象修订和来源权限，`report_use` 将已交付编号与原 UsageService 事实在同一事务去重写入；`delivery_receipts` 只投影实际内核完成结果。最新合入175f025664，344后端、32前端、构建和12导入契约通过。当前本机owner与显式selection，MCP和自动召回由T16.2复用；共享入库脱敏复用shared.secret_detection，设置由external_agent_settings唯一CAS owner负责。
- T15.8 评论能力：`v2.source_sections` 在实际 WorkspaceItems 创建和原文写入事务内维护区段、owner birth/run 与等值证明，供整理稿字面评论区、`insight_generation` 和可选显示投影复用；它只证明 WorkspaceItems 代际，不代替 SQL experience/recognition/document 身份。B站保持原网络边界并对原提取器返回集取点赞前30；小红书只合并同次链接截图。默认 `extract@3` 沿原 MemoryTurn/SourceEgress 提待确认候选，显式 @1/@2 保留；三编辑入口与 MarkdownBody 复用 CommentSection 的只读计数，InsightChip/CandidateHint 显示评。双版本78题对照评论2/6→6/6、原66检索与6比较语义保持；原普通稿历史修订兼容与绑定评论漂移拒绝均已测试。浏览器集中验收另登记。
- `WorkspaceItems.with_records(records)` 复用现有 processing lease 和 keyed lock，供 favorites 在原事务内切换 reader，避免重新构造 owner 形成反向导入；原事务和 revision 校验保持。
- T14.5 治理模型续读数据接点：`ProviderStoreActivation.resume_source` 冻结无正文 source 及 typed cursor，限定 primary；`ModelConfiguration.complete_governed` 使用原 `ProviderResumeOnlyOptions`，在原 Handle 执行帧内、创建 HTTP client 前绑定来源，仅 GET 读取原响应。绑定拒绝沿原 failed_transport/UNKNOWN、observer 和外发 lease 收尾，默认 OFF 与辅助用途保持。验证：六个新治理结构化流节点、三个原 background configuration 节点及三个精确 no-cache 导入契约通过；外部 HTTP 用 MockTransport 隔离。产品调用方尚未注入数据，闭合胶囊、动作 CAS、完整正文/frames/引用及用量接入未验，此数据选项不授予继续权限。

- T14.5 跨尝试检查点绑定：`SQLiteAITurnStore.resolve_model_provider_resume_source` 只读核对原失败尝试、UNKNOWN Effect、不可变路由和完整游标链；原 physical Handle 的 `bind_provider_resume` 在新实际 Handler、当前 strict run lease 与 INFLIGHT Effect 内复验并追加无正文血缘。复用原不可变集合及 checkpoint 所有权检查，不重建旧租约、不改旧终态，也不授予产品继续权限。验证：`test_ai_model_provider_resume` 的22个新节点与原 checkpoint 35个节点共57项通过，三个精确 no-cache 导入契约通过；本阶段未接 native HTTP、闭合产品胶囊或完整响应消费者。

- T14.5 Route 后台恢复（独立叶）：`kernel.provider_store_binding.route_provider_store_activation` 复验原独立choice与immutable路由、canonical双身份、实际快模型和typed原适配器；`v2.route` 仅有效ON新accepted调用取得自己的原strict run lease，并交原Runtime记录实际checkpoint，终态原接口释放。原4秒预算与400token、OFF/历史、忙租约不接管、权限撤回和UNKNOWN费用保持。新26按4/12/9/1分批、原bindings25/caller5及3精确契约通过；完整跨attempt/回执与取消接线仍待。

- T16.2 来源发送接点：`research_sources.ReadControl` 复用原来源快照、读取证明与完整来源闭包，在 SQL→JSON 资格作用域内创建原 durable wire handle；返回的委托接点在原 executor claim 与价格落盘后再次复验，退出作用域后调用原模型 handler。拒发时在原 claimed frame 保存原失败回执和 Effect 终态；`shared.llm.litellm_gateway` 的原幂等终态完成 observer 和 lease，不新建执行账本。发送中来源变化仍由事后复验拒绝逻辑结果，保留真实成功 wire 回执；最后复验退出到网络调用之间有短间隙，不提供整个传输期间的来源不变保证。验证 `test_source_wire_boundary` 的六个真实 Source/Runtime/SQLite 场景及原模型费用、网关调用方；仅模型传输为替身，完整产品 planner、派生证明和 MCP 工厂集成仍另验。

- T16.2 本机 MCP 接点：`python -m backend.memory_app.mcp` 使用 SDK stdio 和数字 loopback HTTP，不直接打开数据库；握手客户端固定为 Claude Code 或 Codex。`v2.mcp_memory` 编排原 `ExternalCatalog` / `ExternalRecall` / `ExternalContext` 与编号使用回报；`v2.mcp_intake` 将原 WorkspaceIntake、RecognitionService 和 `ExternalAgentGuard.reserve_write` 纳入同一真实事务。`v2.external_snapshot.generate_snapshot` 复用完整资格冻结与原 Turn 交付，生成带版本起止标记、时间和回执的复制快照；连接命令由 `mcp_connection.connection_metadata` 生成。当前叶已验证目录、召回、无证据投入与快照，非空证据需原域消费者闭环；完整 T16.2 尚未合入，不能用客户端编号或游标代替权限。

- T15.2部署与备份：`shared/deployment.resolve_deployment`只解析新部署形态与目录，`memory_app.serve.create_application`复用既有应用、根解析器和静态挂载，默认desktop、server同源前端及单用户根，CLI仅监听loopback。`memory_app.backup.backup_runtime/restore_runtime`及`tools/backup.py`复用现有完整快照owner与在线SQLite备份，只恢复新目录；`deploy/`提供Caddy/systemd/每日备份模板。验证test_server_deployment、test_server_backup及旧根/静态/备份测试。
- T15.2平台密钥：`security/secrets.build_model_secret_store/build_secret_store`保留Windows原DPAPI位置，非Windows选择`ServerFileSecretStore`，复用既有generation/tombstone、KeyedLockManager与interprocess_file_lock。`restrict_server_secret_file`以真实fd检查已知密钥文件并收紧权限，供读密钥和新目录恢复复用；不处理普通原件。缺主密钥/解密/锁/权限错误为固定安全码，应用沿既有detail响应返回503。验证test_server_secrets及原密钥/模型/设置/识图消费者，Linux实机Q44待验。

- T13.2 多部分工作台：`v2.multipart` 复用 `TurnExecutionService` 与既有记住、问、干活路径，按依赖并发，旁路父子 Turn 聚合回执；每个逻辑幂等键为请求键加部分序号。`v2.part_context` 将已入库原件和已完成问题的首次外发来源闭包绑定到不可变 Turn refs，主执行者、管家、专家及恢复均复验。`SourceEgressService.snapshot_original_content/validate_original_content` 只为该依赖保存完整原件身份与内容坐标，允许整理状态修订，旧严格 snapshot 接口保持。非文本原件采用冻结父子输入坐标；附件绑定第一个记住部分。`WorkspaceIntake.add_text(preserve_text=True)` 仅保存已校验片段的首尾空白，默认行为保持。`overviews/bookshelf` 暴露既有守卫的冻结事实及恢复检查，完整来源权限经私有 multipart answer RESULT 继承。可选 situation 只进问和干活冻结请求。验证：test_workbench_multipart、test_multipart_sources、test_part_context 与现有工作台、来源和契约测试。

- M4 订阅接入：`memory_app.chatgpt_subscription.ChatGPTSubscriptions` 管理 OIDC/PKCE、本机回调、DPAPI 令牌、跨实例刷新租约与待验证轮换、退出及官方模型目录；`shared.llm.openai_responses.ResponsesCompletion` 将原网关请求交给原生 Responses，始终消费完整流并统计用量，无自动重发；`ModelConfiguration.select_subscription` 使用新旁路选择及账号修订，保留 API/local 配置并复用冻结外发守卫。`v2.subscriptions` 只提供交付适配，`shared/ui/ChatGPTSubscriptionSettings` 与 `subscriptionApi` 复用 Row/Switch、桌面后台 URL 与少字规范。验证：test_chatgpt_subscription.py、chatgptSubscription.test.jsx 及直接相关回归。
- M4.1 订阅兼容：`ChatGPTSubscriptions.close` 与刷新共用生命周期锁，等待在途轮换落盘后关闭 HTTP；明确临时刷新失败保留授权，终态失效只保留 client_id，回复丢失不重发。`shared.llm.openai_responses.ResponsesError` 仅提供安全 `status_code/code/category/output_started/retryable`，适用于订阅 Responses 的 HTTP 与失败流，诊断正文有界读取且不保留。`model_config._model_request_failure` 为普通、流式与受管生成保留这些内部事实和原公开错误文案；它们供 Turn 重试判断，不执行重试。验证：`test_chatgpt_subscription.py`、`test_subscription_responses_errors.py`、`test_model_config.py`、`test_governed_generation.py`。

- T14.5 背景响应启用：`ModelConfiguration` 的内部 `ProviderStoreActivation` 绑定原 typed Responses adapter、当前复验及严格 primary/aux 用途；`kernel.provider_store_binding` 分别从原 Main binding 与 ASK 的同轮冻结辅助选择生成 activation。ASK 保留 Main 与 fast 各自模型、配置和 canonical 路由身份；每次 GET 复用原 physical Handle/Effect/lease、授权与价格观察，不新增外发许可。历史缺绑定保持 OFF，不晚捕获；Do 管家与 worker 在此叶保持原路由。验证：test_main_provider_background、test_aux_provider_background 及原 fast 调用方；跨尝试恢复与完整 T14.5 尚未完成。

> 2026-09-30 重构说明：目标形态见 [ARCHITECTURE.md](ARCHITECTURE.md)。
> - 新的产品接口只放在 `src/backend/memory_app/v2/`，新的前端组件只放在 `src/frontend/src/shared/ui/`，都登记在文末的"v2 与 shared/ui"一节。
> - 下面各节描述的是现存能力。阶段 7 删除某个模块时，要同时删掉本索引中对应的条目。

编写实现前先查本索引，再读取对应源码及调用方。已有能力满足需求时直接调用；需要扩展时保持原契约。新的公共能力应有明确复用对象，统一登记用途、入口、边界和验证，不为单次代码片段创建通用框架。

组件保留在对应运行层的现有目录。本索引连接前端组件、共享工具、核心服务与桌面运行组件，不复制它们的实现。

## 已有基础

| 能力 | 权威入口 | 调用及边界 |
| --- | --- | --- |
| 产品壳、导航、项目与主题 | `src/frontend/src/shared/ui/Shell.jsx`、`AppRouter.jsx` | 工作台、资料库、设置共用唯一壳；伙伴页只从头像进入；旧链接由 AppRouter 映射到当前页面并保留项目 |
| 视觉变量与组件规范 | `src/frontend/src/styles.css`、`DESIGN.md` | DESIGN §2 的暖纸配色、字体与圆角 token 是唯一来源；旧 `--cr-*` 映射到新 token，Tailwind 引用同一来源；全局键盘焦点环为朱红 2px |
| 正式Document与修订 | `src/core/document_engine/` | 经repository读取/修改；正文为blocks，禁止另写假定顶层content的正式文档解析 |
| 来源和发布可见性 | `src/backend/shared/document_visibility.py` | 共享文档检索遵循确认、发布、待审核来源资格，不以存在文件代替可见性；memory_app 原路径只显式重新导出 |
| 证据窗口 | `src/core/search_and_recall/evidence_windows.py` | 搜索与问答复用同一原文偏移和预算；query_terms 公开既有加权词项，_terms 保留同一函数别名；不可用无坐标摘要替代证据 |
| SQLite事务及乐观修订 | `src/core/storage_provider/sqlite_uow.py` | 修改经短事务与expected_revision；不得并行绕过统一存储写同一对象 |
| 视频分段 | `src/backend/video_summary/generation/prompts/summary.py` 的`chunk_segments` | 工作台继续复用继承实现；证据、授权和阶段预算由调用方负责 |
| 模型网关 | `src/backend/shared/llm/litellm_gateway.py` | 工作台配置通过model_config适配；保持实际出网前授权校验 |
| 纯消息与用量辅助 | `src/backend/shared/llm/message_metadata.py` | 消息包装、输入估算、用量归一化由编排与网关复用；无模型网关或网络依赖；原网关重导出同一函数以保持调用兼容。T11.7工作树实现，尚未合入。 |
| 模型协议能力 | `src/backend/shared/llm/model_capabilities.py` | `ModelCapabilities` 与 `resolve_model_capabilities` 为既有网关声明结构化模式、角色、token 字段、流式用量和推理等级；DeepSeek 精确主机按 Chat Completions 协议适配，未知端点保留原有有界探测，不从模型名推断代理能力。无新持久化字段；验证：`tests/backend/unit/llm/test_model_capabilities.py` |
| 带用量的结构化生成 | `LiteLLMCompletionGateway.complete_structured_with_usage`、`ModelConfiguration.complete_structured`、`memory_app.structured_generation.generate_structured` | 问答和短认识复用 AskOutput/InsightOutput，原引用上界与认识长度规则留在领域；生产配置保留实际出网前后守卫及原用量元数据，格式降级有界、不重试无效业务输出。旧文本适配器输出也经同一校验；验证：`tests/memory_app/v2/test_structured_generation.py` |
| 流式结构化生成 | `LiteLLMCompletionGateway.stream_structured_with_usage`、`ModelConfiguration.complete_stream`、`shared.llm.json_mode.PartialJSONField` | 复用能力声明、token 预算和完整输出契约，部分 JSON 只解码回答字段，持续复验来源/配置，正常 stop 与最终校验通过才返回完整对象及用量；关闭提供方流，不自动重发。验证：`tests/memory_app/v2/test_workbench_stream.py` |
| 问答执行与交付 | `memory_app.v2.turn_execution.TurnExecutionService`、`memory_app.v2.workbench_transport` | 同一业务编排由 JSON/SSE 共用；旁路 CAS 幂等、断流分离、完成原子落盘与重放。交付层仅负责 RFC Accept 协商、started/delta/done/error、有界合并队列与心跳。前端 workbenchApi 保留请求键、解码 UTF-8 SSE、明确手动重试，Workbench 只临时显示未完成增量。验证：后端 test_workbench_stream、前端 workbenchStream/workbenchStreamView |

## 公共能力调用入口

候选与认识的适用条件统一调用 `backend.recognition.normalize_conditions`：复用领域既有校验和首尾空白规范化，拒绝无效类型、空项、超长项与规范化后重复值。模型请求在副作用前校验，领域保存仍复验；不复制一套 HTTP 专属条件规则。

| 能力 | 入口 | 当前调用方与约束 |
| --- | --- | --- |
| 安全浏览器存储 | `src/frontend/src/shared/lib/browserStorage.js` | `browserStorage()`安全获取存储；`readStoredJson`区分不可用/损坏；`writeStoredJson`、`removeStoredItem`返回成功与否。工作台、文档编辑器和撤销入口复用；缓存失败不能使内存编辑失败，不能把读取异常当作可删除的损坏记录 |
| 会话与请求代次 | `src/frontend/src/shared/lib/useRequestScope.js` | 工作台和归档页共用。render捕获`gate.scope`；请求用`issue(channel, scope)`，每个await后以`isCurrent(token)`验证，副作用落地也需验证；`invalidate(channel)`取消旧回包资格。卸载与StrictMode旧token失效。草稿revision、授权和字段编辑版本仍由业务管理 |
| 资料库归档 | `features/library/Library.jsx`、`features/rebuild/libraryOverviewApi.js` | 当前资料库复用归档列表、归档与恢复 API，项目会话隔离由当前页面负责 |
| 正式文档正文 | `core.document_engine.document_block_text(document)` | Overview全文检索读取当前blocks；不读取旧revision，不假定顶层content |
| 资料库单项序列化 | `core.product_core.serialize_library_overview_item(item)` | 总览与分页搜索共享字段契约；不要构造虚拟总览再拆items |
| 修订级认识提取 | `backend.memory_app.document_recognition.extract_document_candidate(...)` | 旧资料审核、工作台确认材料共用；新候选保留完整正文作为可编辑初稿，同修订幂等并保留已有候选修改，新修订保留旧候选和已发布历史。重试比较遵循RecognitionService既有首尾空白规范化；原Document及修订不变。消费预算仍由各上下文入口判断 |
| 模型成果证据依赖 | `backend.recognition.artifact_dependencies.read_artifact_dependencies(reader, scope, payload)` | RecognitionService生命周期与SourceEgress共用；核验成果task/document/context_packet身份并读取冻结根引用，返回根、packet与依赖修订。只读，不授予外发许可、不写失效状态；不把parent_ids谱系、正文提及或命名惯例当证据边。调用方分别核验当前资格或外发权限，删除集合不能直接复用为失效集合 |
| 单条资料私密与来源复验 | `backend.memory_app.source_egress.SourceEgressService`、`SourcePrivacyInputError` | 只接受空列表（私密）或全部三用途；旧记录仅空列表私密，不迁移。派生关系与冻结成果只继承私密，保留来源身份/修订/闭包和发送前后快照复验；输入异常由source-policies PUT映射400，其他来源/CAS错误保持原处理。检索与设置列表复用同一权威 |
| 工作台投入与处理 | `backend.memory_app.workspace_intake.WorkspaceIntake` | HTTP绑定传入解析后的body；负责获取、处理、租约续期和结果落盘，音频/生成/链接分别调用所属模块；远端投入复用 v2/privacy 按实际用途检查全局设置与私密项目，领取租约前拒绝、发送前复查，回执保存 global_setting 与 generation/mode 修订 |
| 整理稿层提取 | `backend.memory_app.v2.layers.summary_of` / `facts_of` | 摘要返回正文与原始 Unicode 半开坐标，兼容旧标题后首段及 CRLF；事实返回关键事实列表正文，原件证据继续取 draft/source_refs，不混用坐标 |
| v2 自动入库与核对 | `v2.auto_confirm.process_and_confirm`、`v2.layers.is_verified/mark_verified` | 复用已装配投入与确认领域；重入不重复模型/文档，场景通过旁路继承；核对用独立 v2_verifications 或历史 user_edit 推导，不改工作项。确认中断按原冻结操作恢复，v1 process 仍只到 ready |
| 工作台审核与入库 | `backend.memory_app.workspace_review.WorkspaceReview` | 草稿乐观修订、确认入库及认识提取；复用WorkspaceConfirmation、LegacyIntakeReview和修订级提取服务 |
| 工作台搜索与问答 | `backend.memory_app.workspace_query.WorkspaceQuery` | 搜索可见性、预览冻结、逐次授权、消费与回执；每应用实例独占有界预览表和锁，不能提升为进程全局 |
| 工作台记录操作 | `backend.memory_app.workspace_items.WorkspaceItems` | 记录创建/查询/状态修改，复用ProcessingLease的所有权检查；其他服务显式传入同实例 |
| 认识 Turn 能力装配 | `backend.memory_app.turn_installation.install_recognition_turn_capability(registry, session_store, application_state)` | memory_app 在应用 state 注入安装器，api 只调用注入入口；注册共用工具的远端与本机能力，保留 authority 的终态投影和 routing 返回值。验证：`tests/memory_app/v2/test_turn_installation.py`、`tests/memory_app/test_turn_integration.py` |
| 认识上下文修订校验与时钟 | `backend.memory_app.packet_verification._verify_packet_current`、`_now` | app 与 Turn 安装器共用；逐条验证认识仍获授权且 revision 一致，保持 app 原名导入兼容；模块导入不初始化 ASGI 应用 |
| 工作台生成与音频能力 | `workspace_generation.py`、`workspace_audio.py`、`workspace_links.py` | 原分段/证据、音频转写检查点和安全链接获取的实际归属；媒体适配器直接调用audio，不能反向导入workspace路由 |
| 公众号正文提取 | `workspace_links._WechatArticleText` | 仅 mp.weixin.qq.com 提取 #js_content，复用 _HTMLText 脚本/样式过滤；缺失或空正文拒绝，原 DNS 固定 IP、内网/重定向等安全规则保持。离线夹具真实 HTML 摘录并标注来源，真实文章验收经用户豁免，实时抓取仍受微信验证阻碍 |
| 产品API领域路由 | `src/backend/api/routes/product/__init__.py` | 只组合领域router并保留重叠路径顺序；新代码直接导入documents、memory_import、memory_export、settings等实际owner。rebuild.py仅保留显式兼容导出，外部/api/rebuild路径保持 |
| 产品HTTP/仓储/文档权限 | `src/backend/api/routes/product/http.py`、`repositories.py`、`document_visibility.py` | 仅放跨领域实际复用的输入输出、构造与权限能力；业务handler归领域，不往通用模块堆积 |
| 精确条件读取 | `SQLiteStructuredRecordStore.list_matching(collection, **fields)` | 工作台查询、发布资格、租约恢复；参数为顶层字符串字段，SQL绑定值，不接受自由SQL |
| 桌面请求会话 | `apps/desktop-electron/src/desktop-session.cjs` 的`currentSessionForRequest` | 文件、PDF、语音、视觉、LineMap、资产导入；每次请求取新secret，校验实例/origin，不能缓存旧凭据 |

## 登记与验证

### 依赖方向

- 前端页面装配业务hook和视图，hook按需调用API与shared；shared不反向导入业务页面。
- 产品路由组合入口依赖领域router，领域按需调用服务、仓储和窄公共模块；领域不回调组合入口，不用动态globals或万能context转发。
- 工作台HTTP绑定显式构造各领域实例，共享同一记录仓储与必要锁；问答预览由Query实例独占，音频/生成/链接能力直接按需引用。

每个完成的公共能力在此登记实际入口、调用者和使用限制；实施进度与验收结果统一记录在`notes/task.md`、`notes/architecture-reviews.md`，不另设并行执行计划。

### 2026-09-29 旧审核可靠性复用

- `LegacyIntakeReview.list` 使用 `list_matching` 读取当前项目；四种确定的来源/文档绑定错误投影为 `projection_error=true` 的白名单故障项，不读取或返回其他项目内容，不修改持久状态。单项读取、保存与确认继续严格校验。
- `LegacyReviewPanel` 复用 `browserStorage`、`removeStoredItem` 与 `useRequestScope`。按项目和材料隔离编辑会话，故障分支不访问本机草稿。组件内Map只保护本次面板生命周期，缓存失败时应及时保存。旧raw缓存的兼容读取与v2迁移规则见下方2026-09-29修订保护记录。

- `workspace_intake._run_lease_operation` 供本领域心跳与取消共用独立的有界线程执行容量，避免模型任务占满默认线程池而无法续租。仅明确SQLite busy/locked最多重试三次；成功发布前停止调度并有界结算在途心跳，继续依赖ProcessingLease的run/instance/到期校验。该辅助能力留在实际owner，不增加全局业务中转。

### 2026-09-29 SQLite只读连接

- `SQLiteStructuredRecordStore._connect`按当前数据库实际schema只读检测；仅缺失schema时通过短初始化事务创建或迁移，并在获得锁后重检。每次连接不再重复写schema或扫描全部记录，不缓存路径就绪状态。首次WAL切换的BUSY有界重试，未知错误原样传播。业务服务仍应在短事务内使用`tx.read/list`，不要持写锁构造跨仓储展示投影。

### 2026-09-29 文档发布资格归属

- `document_visibility.recognition_document_visible(records, scope, document_id)`供认识API读取、编辑与列表准入共用；项目文档须存在且未归档，并由已确认审核或已完成非restructure任务支持。字符串项目按项目/文档/状态下推；WorkScope(None)按文档/状态下推后严格检查个人归属。`app._document_visible`仅为显式兼容alias，测试和新代码直接导入owner以免导入应用入口产生初始化副作用。该规则与历史资料库的`LegacyDocumentVisibility`准入范围不同，不能混用。

### 2026-09-29 旧审核修订保护与列表读取

- `shared/ui/DraftConflictView.jsx`是整理稿修订冲突的纯展示组件，调用方传入原版、服务端、本地字段和选择回调；不承担请求、缓存或领域状态。
- 旧审核HTTP写入要求`expected_revision`和`expected_document_basis`（明确无文档时为null，字段缺失不是null），确认另带逐字`expected_markdown`。复用`WorkspaceReview.expected_revision`与`workspaceApi`错误解析；409使用`draft_revision_conflict/current`。已确认投影使用冻结正文，Document后续修改从成果文档入口查看。
- `LegacyReviewReadIndex`只在一次列表请求内复用document/job/completed-read关联，单项读取创建独立实例共用选择规则；惰性加载、跨项目及归档文档参与歧义检查，capture仍经仓储严格get，不跨请求缓存。它属于旧审核读取领域，不是全局业务中转。

### 2026-09-29 认识适用条件与问答预算

- `context_adapter.format_recognition_content(entry)`是普通任务、固定问题生成/刷新与工作台问答共用的认识格式化入口。正文、完整条件、来源身份和时间限制作为一个单元，工作台问答总长不超过1800字符才纳入，超预算整条排除，不截断否定、因果或跨句例外。重组使用`restructure_generation.build_messages`自己的结构化provenance合同，不调用本格式化函数；原始资料与文档仍沿用证据窗口。
- 问答 `sources.excerpt`为实际发送的冻结正文摘录、完整条件和来源身份/时间说明，`conditions`显式携带认识元数据；`windows/start/end`仍只定位原正文，条件与来源说明不计入正文偏移。预览后认识修订继续触发旧快照拒绝。
- `excluded_sources`最多20条，解释已授权且正文匹配的认识因完整证据预算不足、精确冗余或来源说明不完整而未被采用，不含正文；预览、无匹配反馈和最终结果一致提供。`Workspace`内纯展示`ExcludedSourcesNotice`在预览与回答复用，不引入全局路由或存储状态。

### 2026-09-30 新旧审核的文档来源权威

- `source_egress._workspace_document_authority`在同一所有者内分辨既有工作项确认与旧审核确认；`_review_document_revision`共用精确历史记录读取，`_legacy_document_authority`只验证旧审核的冻结确认和来源身份，不负责检索、任务或文档编辑。
- 旧分支要求固定review identity、同项目、confirmed、准确文档及确认修订、确认前基线、冻结正文和来源引用一致。已有文字/视频定位允许同一来源多个片段，多来源身份歧义拒绝；`workspace://`仍走原路径，不能借旧分支绕过工作项校验。
- 这是来源可核验性，不直接授予外发许可。无策略的来源默认允许，单条私密和项目私密阻止外发；旧非空用途策略保留兼容限制，认识继承全部上游限制。快照冻结确认、证据记录与私密状态修订；文档后续编辑或归档保留历史证据，明确撤权使旧快照失效。
- 原始JSON Source的当前物理状态未被当作这一SQLite历史链的新权威；跨存储物理删除传播需单独定义并验收。

认识长稿的 HTTP 编辑、审核和 Markdown 修改共享 `memory_app.app._recognition_body_limit`，声明长度与流式读取使用同一预算。预算按领域 `MAX_CONTENT_CHARS` 及 JSON 转义上界计算并保留元数据空间，正文仍经原领域校验；其他 API 维持原请求上限。此能力只决定入口载荷大小，不参与业务路由或来源授权。

- 认识当前资格：`RecognitionService.list_recognitions/get_recognition/retrieval_entries/list_questions` 共用已有递归来源核验。DTO `state`保留记录值，`effective_state/status`显示当前资格，`evidence_eligible/evidence_reason`解释证据，`authorized`只表示当前可用认识资格，不代表事实为真或外发许可。历史导出仍读取原记录。当前资格读取使用一次只读快照和请求内缓存，不使用写事务；storage尚无公共只读快照入口，目前该细节集中在服务中，第二个实际调用者出现时再向存储层收敛。
- 候选生成输入：`RecognitionService.read_candidate_experiences`在同一只读快照中调用既有递归证据核验并返回所选经历，供生成前置读取使用。来源外发许可仍走SourceEgress，领域提交继续复验；不在模型调用期间持有数据库事务。
- 未解决的迁移证据：`backend.recognition.import_evidence.unresolved_import_evidence`读取既有导入回执的scope、mapping和issues，供领域资格与导出投影共用。导入回执是持久依赖，不作为普通日志清理。新导入只重映射精确匹配引用；历史错误归一化记录再导出时依据原bundle逐引用保留未解决身份，合法引用和数据库历史不变。
- 候选成功生成出处：`RecognitionService.propose(..., generation=...)` 在同一候选事务中保存严格五字段的初稿出处。HTTP仅从本次模型返回的配置身份及服务端ID/时间生成，create/edit/review都拒绝客户端赋值。编辑沿用原有initial_content/initial_conditions保护，发布通过candidate.recognition_id追溯；历史缺字段返回null。本地成功生成出处不等同失败尝试账本、网络恰好一次、事实核验或跨项目完整恢复。

- 认识来源说明：`RecognitionService._qualified_recognition`在既有只读递归资格核验中收集`source_evidence`，最多256条精确Experience修订、64KiB紧凑UTF8 JSON。只输出类型、ID、修订、provenance种类/核验状态、发生与记录时间、成果及业务结果状态，不复制来源正文或actor。上限与异常通过`source_evidence_complete/reason`表达，不改变`authorized`、持久化状态或历史导出。共享格式化入口拒绝不完整或畸形投影；旧手工适配输入完全缺字段时明确核验状态未知。
- 问答证据预算：`WorkspaceQuery`私有`_recognition_evidence_basis`只比较当前项目中完全一致的正文、条件、直接来源、修订、资格、来源说明和冻结节点/许可；一次选择中相同证据只占一份预算。保留原记录，`excluded_sources`带`duplicate_recognition_evidence`及代表记录ID/修订；整体预算不足统一为`recognition_evidence_budget_insufficient`，来源说明不完整为`recognition_source_evidence_incomplete`。解释列表仍最多20项；前端`ExcludedSourcesNotice`供预览和回答复用，也识别旧条件预算原因。
- 认识编辑会话：候选与Markdown修订冲突复用`DraftConflictView`，各调用方保存自己的服务器基线和本机稿，不把状态职责交给展示组件。认识工作台刷新复用`useRequestScope`，核心失败保留同项目编辑会话并阻止父级保存/执行；辅助失败独立提示。`recognitionMarkdownDraft.rebaseRecognitionMarkdown`只将已识别v2稿的可编辑正文和条件放入当前服务器导出外壳，用于明确保留本机及自己保存后的继续编辑；无法识别或被修改的身份外壳保留原文本供核对。保稿限定同组件、同项目、同一认识身份，切认识、项目或路由不会恢复上一身份的未保存稿。

## v2 与 shared/ui（重构新增，执行者按任务登记）

- T15.3设备：`v2.devices.DeviceRegistry`是配对/设备/presence/初始管理员的单一事务owner；`security.device_identity`叶提供真实request身份及当前资格，`device_auth.DeviceAuthenticationMiddleware/ServerDeviceAuth`覆盖HTTP/SSE/WS、有限bootstrap、Origin和确切loopback模型POST临时身份，设备身份不充当DesktopSession。
- T15.3已有owner接点：`ModelConfiguration.internal_local_key_provider`仅实际wire注入，快照和generation guard保持；`RealtimeAsrTicketAuthority.consume_subject/consume_server_ticket`复用原一次票据仅用于server ASR；`serve`server工厂绑定实际CLI端口，desktop原import-string合同保持。
- T15.3客户端：`shared/api/deviceTransport.productFetch`为16默认消费者复用同源凭据与redirect:error，保留注入fetch/signal/SSE/上传/重试；初次加载读持久槽，配对/清除后当前会话身份优先，即使Storage写入/删除失败。`ProductFileLink/downloadProductFile`认证原件下载并复用scope失效保护，外部/desktop链接保持原能力。
- T15.3设备页：`DeviceGate/SettingsDevices/devicesApi`复用Row/token、fragment一次兑换、设备作废和平铺响应、倒计时与过期变淡，迟到回包隔离。QR复用后端qrcode，开发zxing-cpp真实解码；195后端/257前端/构建/12导入契约通过，真实浏览器/手机Q46。

- T12.5比较提炼：`policies.extract@2.prepare/decide`经get调用，`comparative_insights.public_projects`复用ScopeOverviews.current；`insight_generation`冻结近邻与项目清单并在实际wire/写入复验事实，重复材料只提支持建议。`inbox.file`沿既有事务实现显式跨项目确认，`RecognitionService.stage_experience(copy_from=...)`及`experience_origins`保留来源身份/修订，后者只校验来源事实，不充当外发授权服务。
- T12.5来源复用：`source_evidence_refs`原样承接三种既有引用解析及READS，`research_sources`保留re-export；`SourceGraph`按完整用户/项目/类型/id压平新产物来源，`product_draft_dependencies`读取真实已登记请求、结果与读取证明，`SourceEgress`保留当前领域资格和只读事务局部闭包复用。`ReadControl`复用既有锁与许可复验，在真实发送批准时绑定原件及配置；不缓存跨请求授权，不持锁覆盖整个网络调用。
- T12.5展示：`shared/ui/CandidateHint`复用Icon，Library与InsightLinks/ConsolidationSuggestions复用Row/FocusPanel展示旧认识及只读来源；建议到我用人形图标，到其它项目用#项目标签，确认/合并/取代均由人操作。验证38后端430项、7前端54项及12导入契约，浏览器Q45/真实质量Q10。

- T15.1识图公共接点：`shared/llm/image_input.image_content`将已读图bytes转现网关content，`memory_app/local_image_provider`优先已保存OCR命令，Linux可选`core/product_core/rapidocr_provider.RapidOcrImageAdapter`，包与五资产齐备才构造，不下载。`ModelConfiguration.update_vision_mode/vision_binding/complete_vision`共用密钥owner与网关，独立vision许可/CAS；`kernel/image_read.read_images`复用MemoryTurn、structured输出与actual usage。
- T15.1冻结与图读模型：`v2/image_read.freeze_image_request/validate_image_request/frozen_image_messages`绑定上传owner、原图身份/顺序、精确请求和版本map；`group_entries/uploaded_images/effective_image_read/image_inference_for_document`复验scope/run/封印owner修订及确认正文，不公开磁盘路径。`image_markdown/image_only_draft/read_uploaded_images`将OCR与L1看图模型推断分开，纯图不编造摘要事实。
- T15.1上传与展示：`WorkspaceItems.create_upload`同TX创建一组一原件及索引，`WorkspaceIntake.add_images/read_images`执行总预算和失败清理；WorkspaceReview复用看图markdown callback，ProcessingLease同TX封印sidecar修订。工作台images读取/下载、DraftPanel顺序附件和设置vision行复用shared/ui，晚到回包按project/item隔离。验证test_image_read、test_image_groups、test_vision_intake、test_image_intake及visionSettings/visionTray与相关旧模块；实际292后端/80前端通过。

T13.1 路由：`backend.memory_app.v2.route.RouteService` 返回经过逐字跨度、覆盖与依赖校验的 `RoutePlan`。`route` 应用快速通道，`route_model` 复用相同受管模型链供完整模型评测；请求键标识一次提交，重放复验授权并复用冻结输入。`workbench.route` 为无工具的 aux Turn，单步、400 tokens 上限、4 秒剩余预算传给现有传输，迟到结果丢弃。`ModelConfiguration.complete_governed(response_model=...)` 复用原守卫与实际外发回执，结构化调用最多一次 wire，不重试。验证：`test_route.py`、`test_governed_generation.py`、`test_product_turn_kinds.py`；正式工作台接线留 T13.2。

T13.2 调用约束：先由既有执行服务认领整个请求的幂等所有权，再调用 `RouteService`，并冻结首次接纳的完整路由计划，包括规则回退。并发重复路由在原 Turn 未结束时会返回 `prior_result_unavailable`，该回退不能抢先作为第二份可执行计划；内核末尾落盘超时后的缓存也不能改变已接纳计划。原请求终态由执行服务统一保存和重放。

| 能力 | 入口 | 调用方与约束 |
| --- | --- | --- |
| 私密范围与外发开关 | `backend.memory_app.v2.privacy.is_private_project/set_private_project/privacy_revision/egress_allowed` | v2 共用入口；项目状态存于 `v2_private_scopes`，计数存于 `v2_privacy_state/default`，同事务 CAS 写入。取消私密保留 false 记录以维持修订单调增长；全局开关读取模型 public 配置，ASR 使用 enabled。SourceEgress 复用项目判断与计数校验，原来源 payload 不变。验证：`tests/memory_app/v2/test_privacy.py` |
| 私密事务联动 | `backend.memory_app.v2.privacy.set_private_project_in_transaction` | 项目 PATCH 在自己的事务内复用私密写入，项目、私密范围和计数一并提交或回滚；外部单独设置仍使用 set_private_project。验证：`tests/memory_app/v2/test_projects.py` 的过期写入与事务回滚测试 |
| v2 与工作区装配 | `backend.memory_app.workspace.WorkspaceDomains`、`backend.memory_app.v2.install_v2_routes` | install_workspace_routes 返回六个已构造的领域实例，app 保存于 state.workspace_domains 并传给 v2；v2 仅在 default 文档 namespace 安装，其余记录日志跳过，不重复构造工作区服务。验证：`tests/memory_app/v2/test_projects.py`、`tests/memory_app/test_workspace.py`、`tests/memory_app/test_workspace_confirmation.py` |
| 项目与场景旁路 | `backend.memory_app.v2.projects.install_project_routes/assign_scene/scene_of` | 项目 GET 幂等发现旧记录并确保 inbox/me；POST/PATCH 遵循项目契约与 CAS。场景用 `v2_scene_assignments_<object_type>/{object_id}` 兼容安全 ID，scene_of 返回 `{project_id, scene}` 或 None；assign_scene 的 if_absent=True 在同一事务内仅补缺失归属，保留已有人工选择及旁路修订；四类对象隔离，原对象修订不变。验证：`tests/memory_app/v2/test_projects.py` |
| 范围标签与意图 | `backend.memory_app.v2.intent.parse_scope_tag/route_intent` | 行首或行尾范围标签解析为项目名称、场景、清理后的正文；兼容 LF/CRLF。文件/链接、灵感、干活、问依次匹配，无命中返回 remember；前端手动选择的保持规则由 Composer 管理。验证：`tests/memory_app/v2/test_intent.py` |

| 暖纸共享组件 | `src/frontend/src/shared/ui/index.js` | 导出 DESIGN §7 的 18 个组件；Shell/ProjectSwitcher/Tray、Composer/Receipt/InsightChip、LayerBadges/LadderTrace/FocusPanel/SourceDraftView、StatusDot/ProgressDots/LayerTabs/FilterBar/Row/Breadcrumb/Switch/Icon。调用方持有项目、任务、输入和领域状态，组件只展示与回调；统一使用 styles.css token 和 lucide-react。手机全屏面板按用户 A 裁决覆盖托盘并在关闭后恢复，FocusPanel 支持 Escape、焦点约束与返回。验证：`tests/frontend/shared/ui/` |
| 原文选句与高亮 | `shared/ui/sourceEvidence.jsx` 的 `selectedSourceEvidence/SourceHighlight` | SourceDraftView 复用 DOM 范围和逐字正文校验；坐标有效时精确定位重复片段，旧无坐标输入保留首个匹配。只生成证据坐标与展示，不写业务状态或发请求。验证：`tests/frontend/shared/ui/sourceEvidence.test.jsx` |
| 整理稿冲突展示 | `shared/ui/DraftConflictView.jsx` 的 `compact` | SourceDraftView 复用现有冲突组件，opt-in 少字模式；版本基线、选择、保存与确认仍由调用方负责，旧调用默认行为保留。验证：`tests/frontend/shared/ui/memory.test.jsx` |
| 按块排版编辑 | `shared/ui/RichMarkdownEditor.jsx`、`richMarkdown.js` | 一个ProseMirror内核做块内编辑，原字符串及分隔符保留，只序列化实际改块；CRLF/CR/LF坐标对齐。未知语法原样阅读或源码，待办只编辑文字，唯一可见事实才定位。复用Icon/token，三入口共用；验证：22篇样本及richMarkdown/editor测试。 |
| 文档编辑会话 | `shared/ui/DocumentMarkdownEditor.jsx`、`WorkbenchOutcomePanel.jsx` | 复用既有PATCH/expected_revision及DraftConflictView三快照；调用方持有真实document/readonly/scope。SourceDraftView的renderedEditor({locateFact})显式替代右列，默认旧调用不变；验证：documentMarkdownEditor/libraryMarkdownEditor/richDraftPanel及真实后端字节/CAS/重启。 |

| 进度托盘读模型 | `backend.memory_app.v2.jobs.install_job_routes` | `/api/v2/jobs` 按项目汇总 workspace_items、recognition_tasks 和 v2 干活回执；复用待确认来源、get_task_status/task_receipt纯读状态，按保存检查点显示进度，完成任务保留24小时；关联任务去重、线程/项目漂移拒绝。目标为工作台thread/turn或真实document，无文档旧任务落整理稿列表，原记录不修改。验证：`tests/memory_app/v2/test_jobs.py`、`test_workbench_remember.py` |
| 唯一应用路由与项目控制 | `src/frontend/src/AppRouter.jsx`、`App.jsx` 的 `AppContent` | 新 Shell 统一项目、导航、头像与托盘；兼容所有旧视图，项目来自 v2/projects 并安全读写 hash/localStorage，轮询处理中 1.5s/其余 15s、取消过期请求。旧无项目 pending_memory 保留全局入口，临时展示状态不持久化为项目。AppContent 复用旧页面分派与权限门禁，pet 初始窗口归属保持。验证：`tests/frontend/appRouter.test.jsx`、`webEntry.test.jsx` |
| 认识任务深链 | `RecognitionWorkbench` 的 `embedded/initialTaskId` | 新壳隐藏旧项目选择，按项目与完整 hash 重建编辑会话；托盘原始 recognition task ID 复用 loadTask 与 acceptTask 进入结果窗及进度轮询，不冒充旧 TaskDetail 的 task_ref。独立旧页面默认入口保留。验证：`tests/frontend/appRouterPages.test.jsx` |


- T2.5 文档经验抽取：`memory_app.document_recognition.ensure_document_experience` 固定文档修订并复用旧经验身份；`CANDIDATE_SOURCE_CONSTRAINTS` 共用来源安全提示。旧候选提取保持身份及人工审核历史。
- T2.5 来源生成守卫：`memory_app.generation_sources.generation_source_guard` 供原候选生成与 v2 共用，保留来源快照、配置和远端用途复验，不改变模型网关。
- T2.5 短认识与身份读模型：`v2.insight_generation.generate_insights` 只产生 pending 候选；`v2.insights.resolve_insight/insight_view` 通过真实发布关系解析别名，按完整 WorkScope 隔离并复用领域资格。验证：`tests/memory_app/v2/test_insight_generation.py`。

- T2.6 工作台编排：`v2.workbench.install_workbench_routes` 装配线程、轮次、上传与记住/灵感，使用 app 内任务强引用及同材料锁；结果/失败同事务写 turn 与 v2_workbench_item_states，原领域 payload 不改。GET 只刷新状态；retry 复用入库与确定性认识，不自动调用模型。
- T2.6 显式认识审核：`v2.library.install_library_routes` 的 confirm/drop 调用 RecognitionService，校验 WorkScope 与 expected_revision；禁止自动发布。自动确认和短认识函数的可选 on_error 仅传固定码，旧调用默认兼容。jobs 合并精确关联旁路并显示重启中断；前端 turn 映射已由 T2.7 接入。验证：`tests/memory_app/v2/test_workbench_remember.py`。

- T2.7 工作台前端：`features/workbench/Workbench.jsx` 与 `workbenchApi.js` 接入 v2 线程、记住/灵感、认识确认/丢弃及失败重试；useRequestScope 隔离迟到请求，新会话同步 hash 并可刷新恢复。Composer 共用文件/录音入口，MediaRecorder 生命周期清理轨道。共享原文高亮与冲突展示只有 shared/ui 权威实现，旧 workspace 前端已在 T7.2 删除。R2已补齐真实对照面板。WorkbenchDraftPanel复用libraryApi及recognitionApi，持有打开版本、编辑、原件切换及409三方快照；SourceDraftView可选highlightFacts/sourceTitle/sourceControls/editor保留旧默认调用，按Unicode坐标逐字校验。验证：`tests/frontend/workbench.test.jsx` 及既有共享组件测试。

- T3.1 遗忘召回偏好：`memory_app.recall_preferences.is_recall_excluded(records, scope, recognition_id)` 统一读取旁路 forgotten；normal/cooled 不排除，scope 冲突沿用既有校验。认识检索、工作台问答、任务预览和未消费冻结包边界排除遗忘，恢复改回 normal，原认识/原件修订与 authorized 不变。显式选择通过 context_adapter 防御性过滤，旧包通过 packet_verification 复验；不改冻结任务内核。验证：`tests/memory_app/v2/test_forget.py`。

- T3.2 全局问答授权：`WorkspaceQuery.ask/execute_ask/validate_ask_plan` 保留旧预览入口，直接远端问答按 v2/privacy 一次授权。消费前、模型发送与响应后复验全局/项目私密及冻结目标，回执 consent_basis 保存 generation/mode 设置修订；无匹配不调用模型，完成结果保持幂等。冻结模型守卫固定配置错误由调用方按当前权限映射，不修改守卫。验证：`tests/memory_app/v2/test_ask_authorization.py`。

- T3.3 逐层召回：`WorkspaceQuery.collect_candidates` 保留真实来源、条件、偏移与冻结基线，复用 insight_view 关联/场景及 summary_of；`v2.ladder.plan_ladder` 按 L3→L2→L1→L0 的每层上限和总 8 条预算选择，画像最多 2 且不计加权覆盖，达到 0.6 充分才停，细节问题实际达到 L1/L0。摘要优先认识所连文档，正文优先已选摘要文档，原文优先已选文档关联的工作区原件/Source；多文档场景按真实引用读取。候选以自身 scope 复验，三种正文坐标基底通过现有 coordinate_space 区分。验证：`tests/memory_app/v2/test_ladder.py` 及计划指定 ask/query 聚焦回归。

- T3.4 工作台问答：`v2.workbench` 复用 `WorkspaceQuery.prepare_ask/execute_ask` 同步执行，回执投影按原编号映射引用并保留多窗口真实坐标；发送层级计数、画像子集和 trace 持久化于轮次，空匹配不调用模型或创建外发回执。线程读取/重启只读历史回执，不重发问答。验证：`tests/memory_app/v2/test_workbench_ask.py`。

- T3.5 引用回答展示：`shared/ui/CitedAnswer` 接受 answer/citations/onOpenCitation，保留原 n 并识别 [n]/【n】，无正文标记时在末尾附角标；只有合法调用方提供真实定位回调时渲染按钮，当前工作台读取受阻时为只读角标。`LadderTrace.layers` 可提供实际发送画像数，兼容未传 layers 的既有组件使用；无回调显示只读引用条目，不制造禁用的定位按钮。验证：`citedAnswer.test.jsx`、`workbenchAsk.test.jsx`。

- T4.1 离线迁移演练：`tools/migrate_to_ladder.py --app-root <path> --dry-run|--commit` 读取已选定真实仓储权威，复用文档经验、发布、核对与场景旁路；确定性身份和重复运行不新增。干运行通过 SQLite 备份在独立 clone 执行，支持 Windows 长路径。报告只含类型/ID/状态/计数/原因，不输出材料正文；无可靠来源或未明确技能投影的记录跳过并保持原件。完整历史映射仍受阻，正式运行需用户明确批准。验证：`tests/memory_app/v2/test_ladder_migration.py`、`work/qa/T4.1/invariants.json`。

- T4.3 资料库只读投影：`v2.library.LibraryRead` 与 GET insights/summaries/notes/sources/drill 复用现有身份、资格、可见性、场景、文档和已发布来源；项目内列表和反查不写存储。R1支持GET sources/{id}/text原样全文与真实坐标、drill的document_id/source_id选择及候选列表；无冻结证据仍提供原件对象，window为null，唯一匹配事实才构造窗口。FixedQuestion传递持久化updated_at，_question_payload提供更新时间与evidence_count。`v2.layers.todos_of` 共用 facts_of 的围栏/小节/列表解析并读取当前待办。验证：`tests/memory_app/v2/test_library_read.py` 与旧层/认识/审核测试。

- T4.4 资料库动作：`v2.library` PATCH insight、POST forget、POST notes/verify 复用 edit_candidate/revise/set_preference/mark_verified，身份经 resolve_insight，项目与修订冲突按原领域复验；confirm/drop 保持。`mark_verified(..., expected_current_revision=...)` 可在事务内要求当前修订，旧调用不传时保持历史单调标记。links/收件箱项目转移仍缺明确契约。验证：`tests/memory_app/v2/test_library_actions.py`。

- T4.5 资料库前端：`features/library/Library` / `libraryApi` 复用四层列表、真实下钻及现有 v2 动作，请求按项目/场景/搜索隔离迟到响应；共同层级/筛选/行/面板均使用 shared/ui。`shared/ui/MarkdownBody` 复用 react-markdown，保留正文结构、阻止远程图片自动加载，仅真实唯一事实匹配可显示定位回调。R4 的 libraryApi.drill 可选 document_id/source_id 与 sourceText(project,id,{signal})供工作台直接复用；资料库全文请求按选择取消，备忘使用正式 evidence_count，收件箱只加 pending/active。验证：library.test.jsx、libraryDrill.test.jsx、markdownBody.test.jsx、appRouter.test.jsx。

- T4.6 资料库维护：`features/library/LibraryTools` 的 ProjectGroup/InsightMaintenance 复用 recognitionApi 与旧文档生命周期 API，项目键重建编辑会话、卸载后不应用请求结果。人工认识拆并保持来源/条件，删除须真预览/明确勾选，失败清除旧预览；版本复用既有 VersionHistory。共享 Row 的 opt-in readOnly 仅移除标题按钮，原调用契约保持。验证：libraryTools.test.jsx、library.test.jsx、rowReadOnly.test.jsx 与 atoms 回归。

- T6.1 采集分派：`memory_app.workspace_intake.SOURCE_READERS` 为只读类型映射，`WorkspaceIntake.acquire_source` 复用原媒体与音频函数；已有正文直接读取，检查点复用不调用 ASR。验证：`tests/memory_app/v2/test_intake_dispatch.py` 及原 workspace 模块。

- T6.2 有界上传：`WorkspaceIntake.add_file` 使用 1MiB 分块写盘，只有完整复制且解析通过才 CAS 建项；异常与取消删除本次创建的文件。`memory_app.app` 的 v2/files 使用同旧文件端点的 multipart 预算。验证：`tests/memory_app/v2/test_upload_streaming.py`；100MiB 请求真实超限，峰值测试不代表新容量承诺。

- T6.2 本地图片：`v2.intake_media.read_uploaded_image` 复用现有 OCR 配置及 LocalCommandImageOcrAdapter；只读授权投影来自当前项目/租约的真实上传文件，路径限定 workspace，文件身份前后校验。SOURCE_READERS image 仅在未确认处理阶段写正文，失败安全映射，不自动开启或发布认识。验证：`tests/memory_app/v2/test_image_intake.py`。

- T6.2 本地视频：`v2.intake_media` 提供视频扩展名/2GiB预算；SOURCE_READERS video 复用原音频转写链，云端既有PyAV派生、local先提轨，checkpoint继续绑定视频原件identity与audio输出。仅视频扩容，原件下载支持video。验证：`tests/memory_app/v2/test_video_intake.py` 与真实multipart内存验收。

- T5.3 任务接入（T11.9/T7.3更新）：现行任务由 `v2.task_do.TaskDo` 编排冻结Turn与组织执行，详见下方T11.9公共能力；旧RecognitionTaskWorkflow及任务创建链已退役。`task_status/get_job` 仅保留历史任务查询和真实文档namespace兼容。Workbench继续复用ProgressDots/Receipt/FocusPanel/MarkdownBody，迟到响应按scope隔离；清理后浏览器集中验收Q37。

- 用户追加U1：共享Shell日期恢复胶囊样式，日历图标通过现有Icon的calendar键复用lucide CalendarDays；日期time语义、当前短日期文本与移动布局保持，配色仅用现有token。验证：shell.test.jsx 10通过、构建通过；浏览器截图受客户端拦截。

- T4.6 修订：`features/library/LibraryTools.MemoPanel` 只读复用 mental_models，未知日期/依据数显示—，无模型写调用；`features/settings/ProjectConstraints` 提取原约束CAS编辑供T5.1复用，迟到写结果按项目/卸载隔离。`FilterBar`可选filters/label复用整理稿三档，默认认识筛选不变；`LibraryRead.docs(include_archived)`仅内部展示资格，notes/sources保留遗忘稿，summaries及召回仍排除。验证：library.test.jsx、libraryTools.test.jsx、test_library_read.py。

- T5.1：`memory_app/v2/settings.py` 编排安全模型读模型、私密项目 CAS、有效私密资料及真实外发回执，复用 ModelConfiguration、SourceEgressService 和既有 ASR 安全读取；各段独立修订。`features/settings/SettingsPage` 与 `settingsApi` 复用 Row/Switch、订阅组件和原配置/授权接口；`ProjectConstraints` 支持注入既有 API、启用数/总数、回车创建及 IME 保护；`SettingsData` 仅复用真实快照列表/创建。验证：test_settings.py、settings.test.jsx、settingsApi.test.js、settingsData.test.jsx；尚缺的契约和浏览器验收见 task.md T5.1。

- T5.4：`LiteLLMCompletionGateway.input_budget_snapshot()` 只读复制最后成功预算检查的数值窗口、实际输出预留和输入估算；普通/流式 ModelConfiguration metadata 转交 WorkspaceQuery，问答七类 parts 之和匹配 schema 格式适配后的实际输入估算。`features/workbench/ContextPanel` 复用 FocusPanel/LadderTrace/Row，仅消费纯数值及既有引用/回执安全字段；Workbench 条目下钻复用 libraryApi.drill 和 MarkdownBody，按项目及请求代次隔离迟到结果。干活预算、私密排除数未知，完整送入条目列表待契约补齐。验证：contextPanel.test.jsx、workbenchAsk.test.jsx 和网关/模型/问答相关后端测试。

- R3 引用全文：Workbench按层读取drill或sourceText，画像固定me；CitationBody以Python Unicode坐标和完整回执quote校验全部窗口，失配不搜索替代位置。原件drill与全文独立，全文失败保留真实证据并重试。CitedAnswer可选citationHref提供原生导航链接与真实面板目标，旧回调按钮契约保留。验证：tests/frontend/workbenchCitations.test.jsx、workbenchAsk.test.jsx、shared/ui/citedAnswer.test.jsx。

- T4.4 原子收件箱归类：v2/inbox.py复用RecognitionService与场景/偏好服务；v2/transaction_records.py的TransactionRecords仅借用调用者事务给领域写入，不创建连接、不供资格读取，子commit不落盘，外层唯一提交。v2_inbox_filings按规范原条ID及source_revision防重放；场景建议共用query_terms，零分/并列为null，不调模型。验证：tests/memory_app/v2/test_inbox_filing.py（真实临时SQLite事务、回滚、别名/恢复/并发）、test_library_actions.py。

- T4.5 收件箱行：features/library/InboxFiling.jsx复用Row.trailing/libraryApi.fileInbox与inboxSuggestions，useRequestScope隔离归类迟到响应、ref阻止重复提交；Library的收件箱按原型为当前项目局部范围，领域读取/审核用inbox，归类目标保持当前项目，切回层级恢复项目范围。验证：tests/frontend/libraryInbox.test.jsx及原资料库/路由模块。

- T5.4b：`v2/do_context.py`读取冻结任务消息，五类数值归因与模型回执去重合计；`_LazyOrganization.usage()`复用execution_projection读取已初始化运行时的任务/研究树。`LiteLLMCompletionGateway.input_budget_limits()`和`ModelConfiguration.generation_budget_limits()`只读数字窗口，历史配置不一致返回未知；ContextPanel干活只呈现四区。测试：test_do_context.py、test_workbench_do.py、test_workbench_do_agents.py、contextPanel.test.jsx。
- T11.6：`kernel/receipt_projection.py`只读查询两处既有内核数据库，复验回执身份、项目和真实wire，按主Turn汇总子树并去重；用量保留已报告计数、未知不补零。`v2/task_egress.py`只作为历史任务根分组兼容，设置与新问/干活共享内核事实；历史旧回执仅可读，v2外发回执写入器已删除。

- T5.4问补完：WorkspaceQuery._ask_context提供实际全部entries与逐条片段tokens；v2._ask_receipt复制本次外发模型/授权/修订，未知私密排除不推断；ContextPanel.entries优先完整条目，历史回执回退citations，点击沿R3真实下钻。

- T6.2收藏夹：`backend.api.favorites_discovery.discover_bilibili_favorites`复用原发现/分页/过滤和快照校验，内存对象存储不落盘；`v2/favorites.py`识别收藏夹、规范视频域名与分P身份并在借用事务中复用现有投入。Workbench原创建接口逐视频同线程回执、外层幂等/单条重试，前端读完整thread展示。验证test_favorites.py与workbench.test.jsx。

- T5.2 伙伴与待办：v2.todos.TodoService复用LibraryRead可见性与layers.todos_of，/api/v2/todos提供当前待办及事务CAS完成/恢复，状态只在v2_todo_state、不改整理稿；v2.stats.record_activity提供失败不影响主操作的旁路事件，week_stats按北京时间半开周统计；CompanionPage复用companionApi聊聊/专注和shared/ui，安排与回顾用v2接口，旧面板深链保留。验证test_todos.py、test_stats.py、companionPage.test.jsx、companionApi.test.js与appRouter.test.jsx。
- Row展开语义：shared/ui/Row的可选expanded属性映射aria-expanded，设置模型、隐私、项目和备份行复用；未传入时不输出展开状态，原选择语义保留。验证settings.test.jsx、settingsData.test.jsx及shared/ui/rowReadOnly.test.jsx、atoms.test.jsx。

- T10.6周统计收口：v2.stats.week_stats单列forget_auto，读取by:auto的forget并兼容既有created_at；手动forget仍独立，cool/revive不计入。验证test_stats.py与test_auto_forget.py，既有自动事件不迁移。

- T7.8 F5：shared/lib/responseJson.js统一响应边界，readResponseJson在解析前将5xx映射server_unavailable，解析失败映射invalid_response；两者带status及既有中文短句，不暴露服务正文。资料库、工作台、设置、认识、ASR、订阅与伙伴前端复用；业务4xx继续由原调用方处理，认识非JSON错误同样收敛。AppRouter失败轮询有界指数退避、成功重置，维持项目切换取消旧请求。验证requestErrors/appRouter及直接消费者测试。

- T7.8 P3：Shell显式导航名称，active=companion标记头像；LayerTabs的counts[key]=null显示未知—，未传值保持默认0。MarkdownBody新增可选omitEmptyArtifacts，仅隐藏单独内容为：。。的段落，正文中标点与默认调用保持。shared/lib/time.formatLocalShortDate复用补零逻辑，显示本地MM-DD HH:mm，空/无效时间为—。验证parityPolish、appRouter、library、atoms及markdownBody。

- T7.8 F2：create_vault_backup的sqlite_online=True供备份接口使用，对SQLite使用只读Connection.backup并固定读事务；离线迁移默认保留严格复制及源指纹契约，快照沿用既有manifest及验证/恢复；物理文件身份、文件集合及非SQLite内容漂移仍拒绝。VaultBackupRestoreError.reason_code提供backup_source_changed/backup_sqlite_busy/backup_sqlite_failed/backup_failed；备份接口仅在既有detail字段返回安全码，createMemorySnapshot复用responseJson解析，SettingsData复用Row显示。验证tests/rebuild/test_live_vault_backup.py、tests/rebuild/test_vault_backup_restore.py、tests/backend/integration/api/test_vault_transactional_restore_api.py及tests/frontend/settingsData.test.jsx。

- T7.8 F6：OriginalPrivacy复用Row/Switch，libraryApi.sourcePrivacy/setSourcePrivacy按原件源修订与策略修订双CAS并刷新；SourceEgressService.policy及original_sources提供类型化原件身份/精确确认别名/L0继承，与v2/settings私密列表共用。source_snapshot比较物料身份而不抹去私密；research_reads/research_packets为无正文持久证明和终态brief绑定，research_sources的ReadRegistry/ReadPlanner通过app.state注入读工具及发送前边界。memory_app/transaction_records.TransactionRecords可嵌套借用同一事务，v2旧导入兼容；JsonObjectStore.locked串行sources/memory_persona/project_skills读写，incarnation识别来源删后重建，不用于正式数据迁移。验证test_original_privacy、test_workbench_do_agents、source_egress/packet_egress、intake_authorization及originalPrivacy.test.jsx。

- T7.8 F7：v2/task_egress.task_call_groups支持可选task_id/project过滤；同一只读SQLite快照关联任务/研究子树远端尝试与模型调用，附匹配冻结模型配置的安全egress三项。do_context与设置复用同源合计，未知授权/用量保持未知；Workbench圆环按冻结context.parts/window显示百分比。验证test_egress_receipts、test_do_context、test_workbench_do及workbench/contextPanel前端测试。

- T7.2 保留能力迁移：`shared/api/recognitionApi.js` 与 `shared/api/workspaceApi.js` 保持既有参数、错误解析和修订合同，供当前工作台、资料库及旧任务重试复用；`features/library/VersionHistory.jsx` 保留历史读取展示；`shared/lib/projectId.js` 保留桌面导航项目键的裁剪、长度和控制字符校验。伙伴 API 与 DesktopPet 原位保留。验证：recognitionWorkbench、workspaceApi、versionHistory、webEntry、AppSameViewNavigation。

- T11.1 内核组装权威入口：`backend.memory_app.kernel.ai_runtime.get_or_build_ai_runtime`、`kernel.ai_execution_control`、`kernel.agent_coordinator`、`kernel.agent_runtime_composition`、`kernel.agent_organization_runtime`；只搬迁现有装配，core内核逻辑不复制。旧backend.api五个模块转发同一个module对象，保留私有兼容名与monkeypatch身份；turn_*直接使用新路径。运行资产同时识别旧/新源码布局，其他旧API能力适配器暂沿用，交后续退役任务核对。验证：test_kernel_composition.py及原Agent/资产/架构与memory_app测试。

- T11.2 冻结Turn：`core.ai_kernel.turn_kinds.freeze_turn_request`按调用方给定身份/时间返回独立快照，`turn_templates`保存固定version1能力、用途、上下文与实际步数/单次时限；无字段旧请求保留primary和旧运行约束。`v2.turn_requests.freeze_product_turn`接材料`{type,id,revision,project_id}`及权威payload渲染器，经`v2/privacy`复用原件闭包排私密，`validate_frozen_inputs`供真实发送前再次核验撤权、原件和派生修订。aux没有使用记录/线程写入；后续领域调用不能省略派发前复验。`is_user_turn`提供统一用途判断；coordinator对模型回执/可信无回执终态做aux预算排除。验证：test_product_turn_kinds.py、test_turn_requests.py与原内核/SQLite/协调器/私密测试。

- T11.4 整理Turn：`v2.organize_turns.OrganizeTurns`供WorkspaceIntake普通整理与workspace_generation视频分块共用；复用冻结Turn、SynchronousAIRuntime、发送前原件/隐私复验，以workspace_organize_steps旁路保存模型产物，处理租约与CAS保护写入；完成分块重用、未知外发结果隔离、领域校验失败允许显式重试。ProcessingLease续约使用workspace_processing_heartbeats，保持冻结原件revision。ModelConfiguration.complete_governed的purpose默认primary，整理显式aux；真实网关与合成transport同走内核回执。验证：test_organize_kernel.py、test_processing_lease.py、test_governed_generation.py及T2整理测试。
- T11.3 辅助记忆Turn：`memory_app.kernel.memory_turn.MemoryTurn` 复用现有内核，由`v2.memory_turn.MemoryTurn`注入产品隐私绑定，接收领域材料身份、幂等键和发送前复验回调；`generate`只提交aux Turn并保存校验后输出，`propose`以memory_propose effect调用原领域写入；`embedding_request`供连接排序复用同一调用回执。`v2.turn_requests`/`privacy.freeze_turn_materials`可显式选择embedding用途，默认generation保持不变。模型结果未知时禁止重发，已持久结果恢复不再次调用；正式认识发布仍必须由用户确认。认识生成的`select_insight_attempt`用既有执行token选择已知失败后的独立尝试，旧回执和候选ID保留；重试复验原冻结材料，其他执行者不得借用未完成尝试，未知远程派发不得因当前切为本地而重发。验证：`tests/memory_app/v2/test_memory_kernel_turns.py`、`test_insight_retry.py`及既有认识/每日整理/连接测试。
- 文档生命周期旁路：install_stats_routes/install_usage_routes向application.state注册document_record_activity/document_record_usage；旧文档路由调用已装配能力，保持归档统计和恢复权重，避免api反向导入memory_app。验证test_stats.py/test_usage.py。

- IMPORT-FIX：`shared.memory_sidecars.UsageStorage/record_activity`统一旧文档与v2统计、权重旁路写入；v2 UsageService保留认识解析与连接传播。`memory_app.privacy_policy`保存私密/外发开关原语，v2/privacy保留冻结入口；`recall_state`提供只读认识状态，v2/recall_preferences承担带统计的变更，旧路径兼容转发。`uploaded_media`复用原OCR和媒体限制，v2原模块指向同一对象；`workspace_contracts._empty_ask_context`供新旧问答共用。收藏夹命令接纳在`api.bilibili_favorite_batch_admission`，原runtime兼容导出，Effect执行与回执不变。

- IMPORT-FIX重基补充：WorkspaceIntake.with_items复用原构造类、runtime_root和传入models，供收藏夹在TransactionRecords内绑定同一lease/lock；消除favorites到workspace_intake构造反向依赖。文档路由缺少v2装配回调时直接复用shared.memory_sidecars，不漏写旧入口旁路。
- T7.9：storage_provider.vault_backup_restore.fingerprint_vault_restore_source仅用于恢复确认的源库逻辑身份，复用目录安全检查、SQLite只读快照与普通文件校验；不替代物理备份fingerprint_vault_root。prepare_vault_recovery的expected_source_fingerprint绑定确认计划，新operation记录restore-source-logical-v1，旧记录缺省physical-v1；adopt/rollback/recovery/replay复用算法分派。测试test_vault_restore_identity、test_vault_operational_recovery与test_vault_transactional_restore_api。

- T13.0离线路由评测：`tools.route_eval.evaluate(fixture, predictions=None)`复用`v2.intent.route_intent`生成规则基线，或读取`{version:1,cases:[{id,parts}]}`外部预测；CLI支持`--cases/--predictions/--output`。权威合成集为`tests/fixtures/workbench_route/cases.json`，逐项/分类/总分并报，支持T13.1完美模型和后续路由比较；无App初始化或网络。验证`tests/memory_app/v2/test_route_eval.py`。

- 离线记忆评测：`tools/memory_eval.py` 读取 `tests/fixtures/memory_eval/corpus.json`，在临时SQLite与合成资料上调用真实召回阶梯；固定播种事件时钟，不加载正式runtime或模型配置。`score_selection` 分开报告逐类命中、引用对象正确率、证据token、召回条数及拒答误召回；多来源/时间题按 `expected_evidence` 逐对象核对，长文按后半答案片段核对。阶段12前后对比复用此入口；验证 `tests/memory_app/v2/test_memory_eval.py`，基线 `work/qa/T12.0/baseline.json`。
- T11.9 干活分工：`backend.shared.task_division_graph.validate_divisions` 验证有向依赖，`AgentOrganizationRuntime` 按冻结许可并行派发并经父级扇入传递依赖成果；`task_division_authority.frozen_division_capabilities` 验证真实父子Run、已消费许可、冻结请求及能力交集，只有本项draft_create_only可免批准。旧能力适配仍保留legacy资源锁，普通写能力不得进入干活分工。
- T11.9 成果与分工样例：`v2.task_drafts.TaskDrafts` 以事务和operation回执只新建整理稿；`v2.task_divisions.TaskDivisions` 保存/调整/删除样例，复用InsightLinks排序、既有向量缓存和内核embedding回执；`v2.task_do.TaskDo` 编排冻结Turn、进度、最终成果与样例，不调用旧任务创建。历史任务只读；交付稿可见性核验终态产品回执及创建回执。
- T13.7 成果版本事实：`v2.outcomes.record_outcome/select_outcome/validate_selection/qualified_lineages` 沿公开完成轮、真实内核交付操作与保留的出生修订核验身份，在原公开终态事务 CAS 写入不可变成果链；重做冻结同根与原前驱，并发编号按已完成版本分配。`hidden_outcome_ids/versions` 共用于资料库列表、召回候选和版本读取，旧版下钻及历史正文保留，合法当前引用修订不丢出生资格。未知历史交付沿原行为且不赋新链身份；验证 `tests/memory_app/v2/test_outcome_lineage.py` 及原干活、资料库和重做调用方。
- T13.7 续写与结构评测：`v2.outcomes.prepare_continuation/validate_continuation/candidates` 共用于冻结当前正文、来源及可信最新版选择；`v2.outcome_patches.apply_patch` 复用原标题解析和用户段落边界，返回新正文及改动路径，旧稿保持。`continuation@1/@2` 共用原相似度与提示词，`@2` 将同场景优先分纳入原阈值；仅登记候选，启用另行提交。`tools.outcome_eval` 读取真实管线观测，按固定样本与落点分母计分，`--policy` 核对保存版本；失败和缺失计零，不由标注生成交付。验证入口为 `test_outcome_continuation*`、`test_outcome_evaluation.py` 与 `test_outcome_pipeline_evaluation.py`；标注补丁的结构计分不代表真实模型生成质量。
- T13.7 写法块：`v2.style_context.confirmed_style/style_input/freeze_task_style/frozen_task_style` 复用原认识、召回偏好、场景与 `SourceEgressService` 核验，沿保存的 `style` 版本挑选本场景、项目及我中已确认的写法。同一修订集合缓存首个强度排序与字节，权限依据每次复验；模型输入只含选中字节和计量，旁路保存完整依据。`TaskDo` 冻结去重后的原引用及快照，产品装配把写法与续写校验合入原外发事务，Main 首稿及续写注入，非空写法在原最终发布事务再次核验，空块保持原快捷路径。上下文按真实 Main wire 只投影写法和上一版的数量及 token；默认启用 `continuation@2` 与 `style@1`，启用使用独立提交；旧版及离线覆盖保留。验证入口为 `test_outcome_style_context.py`、`test_outcome_style_main.py`、`test_outcome_style_boundaries.py`，固定管线可用 `CHRIPTMAS_OUTCOME_EVAL_STYLE` 核对真实保存版本。
- T13.7 成果方法来源：`RecognitionService` 按类型递归成果的出生经历和认识；`product_draft_validator` 由原 `memory_app.app` 显式装配，委托公共 `source_egress.validate_product_draft_source` 在当前事务内核验原 Root 完整闭包与原件身份。临时认识服务沿用该能力，归类写服务继承原能力及缓存失效回调。缺原件校验能力时拒绝使用该来源。完整性校验允许私密材料在本机提认识并人工确认，外发仍需原用途权限。`TaskDo` 显式接收同一原服务。`test_outcome_style_product_sources.py` 覆盖经历、投入原件和已有原件的公开及私密来源，公开确认的写法进入下一次真实 Main，私密来源不进入写法块；`test_outcome_style_filing_sources.py` 经真实接口验证归到我的来源与人工确认。
- T11.9 模型与读取：`kernel.product_routing.ProductGenerationRouting` 冻结公开模型配置，`ProductTaskPlanner` 通过既有受管网关和真实内核回执执行；`v2.task_receipt_details.task_receipt_details` 投影当前子项工具状态及召回标题/正文，不返回参数、凭据或其他Turn载荷。验证入口tests/memory_app/v2/test_divided_do.py、test_task_process_recovery.py、test_task_divisions.py及tests/frontend/taskDivision.test.jsx。
- T14.6 终稿结束：干活专用 `TaskDraftCapability` 的 `title/markdown` 参数兼容保留，可选 `final_for` 精确声明冻结交付物；中间稿省略，错误声明仍按非终稿处理。`frozen_division_binding(composition, request, payloads)` 从已消费许可与父 Turn 的不可变分工载荷读取交付身份，供草稿能力与 `ProductTaskPlanner` 共用。`TaskDrafts.get_operation(operation)` 从服务自有集合只读成功创建记录，内核不导入产品集合常量。`final_draft_decision` 核验同 Turn 的完成结果、对应工具意图与成功草稿操作，当前权限、画像及取消检查后返回既有完成决定；说明取标题与正文摘要，最多2000字，现有扇入消费。只减少真实网关调用，不改变内核控制步骤计数；无声明与通用草稿能力保持原流程。验证 `tests/memory_app/v2/test_final_task_draft.py`、`tests/memory_app/test_final_task_draft_authority.py`。

- T12.1 上下文分块：`core.search_and_recall.evidence_windows.split_evidence_chunks` 按400–800字保留原文坐标及末句重叠；`v2.contextual_chunks.select_contextual_windows` 共用查询词并保留最多三处互补证据。`v2.contextual_chunk_vectors.chunk_vector_scores` 复用现有向量缓存、retrieve与受管embedding Turn，每父资料独立命名空间，按项目/条目/修订/模型四键读取，检索不清扫，失效由写入口及每日维护承担；外发关闭回退关键词，前缀仅当前资料标题及120字摘要。验证：test_contextual_chunks.py、test_contextual_chunk_vectors.py及现有问答/阶梯测试。
- T14.3 缓存写入口：`core.search_and_recall.vector_cache_invalidation` 提供固定namespace、仅元数据父枚举及单事务精准target删除，不读vector_json、不创建不存在的cache；公开事务main路径或database_path定位缓存，内存库返回None。`backend.recognition_retrieval.cache_invalidation` 为兼容薄转发；原件投影支持当前/旧document_id，旧JSON Source显式注入非持久化vector_cache_path。
- T14.3 来源复验：`memory_app.source_egress.prepare_sources/invalidate_sources/_parent` 共用当前正式SourceGraph与worker证明资格，`v2.cache_sources` 转发同函数对象；`privacy_state` 仅提取既有项目私密事实读能力。`RecognitionService.cache_invalidation` 为可选两阶段产品依赖，应用、收件箱和重构写入实际传递；裸服务限自身/同范围，跨用户数据根分别持有缓存。
- T14.3 维护：`v2.cache_maintenance.CacheMaintenance` 注册既有DailyJobs的唯一embedding_cache回调，首次60秒、此后86400秒；模型0，只删除无资格/损坏来源/过期派生向量。验证：cache_maintenance、cache_product_sources、cache_write_projection、cache_original_write及领域cache_writes真实事务测试。
- T11.5 问答：kernel.answer_turns.ProductAnswerTurns在真实单次只读能力中编排预取领域召回、aux改写及primary回答；ProductGenerationRouting复用公开配置冻结，WorkspaceQuery提供原有冻结和逐wire复验。迟到aux按先持久终态处理，model.result.discarded只记录丢弃，不覆盖结果。
- 请求工作单元：`core.storage_provider.connection_scope.connection_scope/with_connection_scope` 按数据库与兼容仓储组独占租借连接，归还后可交给另一线程；跨线程仅等待 50ms，同线程嵌套及持租借等待子线程时允许独立连接，避免互等。归还回滚未完成事务并清理 `query_only`；`capture_connection_scope/create_scoped_task` 把生命周期延伸到内核执行、心跳、后台任务和最后计时落盘，最后一个所有者退出才关闭连接。问、记住、资料库列表、干活均接入；`begin()` 仍独立写事务，FULL、CAS 和实时授权保持不变。验证：`test_request_unit_of_work.py`、`test_request_unit_routes.py`、`test_answer_connection_scope.py`。
- 请求读集：`SQLiteStructuredRecordStore.read_batch` 按显式集合及 ID 批量读取，`None` 表示全集合，空元组不查询，大集合分批绑定参数；不改变普通 `read/list` 的实时语义。`v2.request_reads.DocumentReadSet` 只预读目标项目文档、当前修订正文和已确认原件，供问与资料库组装共用；记住入口预读项目、线程和选中原件，干活预读样例及选中轮次展示引用。授权、私密、来源完整性和模型发送复验始终使用原仓储，不使用读集。验证：`test_request_read_batch.py`、`test_request_read_paths.py`。

- T12.2 召回去重：`v2.recall_dedup` 提供实际证据二元组Jaccard与已有向量原始余弦，阈值0.8；`ladder.plan_ladder` 记录skipped_duplicate并继续选后续材料，保留refutes和同资料跨层。`cached_candidate_vectors` 只读匹配父修订/模型/单窗块身份，读取前后复验权限和父资料；SQLiteEmbeddingCache可选read_only不建表或修坏行，默认写缓存能力不变。T12.1块cache namespace/id函数共用。验证：test_recall_dedup.py、test_contextual_chunk_vectors.py、独立dedup_interference.json及相关阶梯/问答/缓存测试。

- T12.3 范围概览：`v2.overviews.ScopeOverviews.update/current` 复用MemoryTurn与来源权限，仅将完整当前scope的非私密有效摘要输入aux生成，≤300字概览经领域CAS写旁路；结构化input_revision绑定成员集合、归属/资料/item/私密/来源修订，不用hash。`navigation_candidates` 补真实L2与精确窗口，用概览排序，`WorkspaceQuery.validate_ask_plan`的overview_guard持续复验输入，关闭外发回旧检索。`Consolidation.run`每日更新项目/场景，仅输入变化触发；`memory.overview`模板与JSON schema空工具能力。验证：test_overviews.py、test_consolidation.py、test_product_turn_kinds.py、test_turn_requests.py及现有阶梯/问答/MemoryTurn测试；固定专项overview_navigation.json。
- T14.0 性能观测：`core.storage_provider.observability`提供请求ContextVar、线程安全连接/SQL计数及十段计时；SQLite trace只累计数字，原SQL与正文不落盘。`memory_app.v2.turn_timings.turn_timing`默认开启（CHRIPTMAS_TURN_TIMINGS=0关闭），事务CAS写v2_turn_timings，观测失败只记录安全日志；引用计数覆盖请求/执行任务/记住及干活后台生命周期，收藏夹子Turn独立归属。AITurnRunner执行池、完成回调和心跳通过租约绑定同一观测，清理完成后释放引用。TurnExecutionService从幂等领取到终态检查计数；资料库列表使用独立观测ID，重放不覆盖原Turn。`tools/perf_bench.py`用临时合成库及内存提供方运行真实应用组合，分档独立进程；本地耗时扣除提供方实际等待，首字与generation重叠单列。基准逐请求保存检查点，--resume保留完整档；HTTP/SSE失败与后台计时分别记录，未执行阶段标记缺测，中断中的请求不自动重放。用户2026-10-03取消基线生成要求，基准脚本及已有测量仅作历史工具，不作为验收门槛。验证：test_turn_timings、test_query_timings、test_timing_routes、test_perf_bench。

- T12.4 常驻画像：v2.profile.confirmed_profile 复用认识领域资格、SourceEgressService、实际记忆强度与token估算；完整有效认识修订及来源/隐私资格构成compose@1投影键，首次按强度排序，事务CAS复用稳定字节，≤600tokens并保留条件完整性。WorkspaceQuery与TaskDo共享此块，发送前validate_profile重验；干活通过冻结材料refs/来源闭包、v2_task_profiles和注入的reader将同块传给主执行者/管家/专家。回执保留画像来源身份供追问复验，ContextPanel复用persona分类行。验证：test_profile.py、test_ladder.py、test_ladder_budget.py、test_workbench_ask.py、test_followup.py、test_do_context.py、contextPanel.test.jsx。

- T12.11 版本化记忆策略：`v2.policies.register/get/override/parse_overrides` 统一登记13个现有接口的@1实现，纯模块只导入标准库与本包；`types.ModelPolicy.prepare/decide` 分离认识提示与模型输出解析，服务由调用方注入。`pipelines` 保留记住、问/做、主动学习的主步骤，remember显式冻结实际route依赖，ask_do显式冻结strength/place依赖。`kernel.turn_requests`装配可选policy_versions，`ProductPolicyRuntime`经四个薄super执行叶绑定冻结版本，覆盖批准工具、记录意图恢复、重新规划与专家唤醒并恢复ContextVar；已存缺字段产品Turn按历史@1，不改请求形状，通用Turn沿原选择。现有画像构建和画像消息、整理、路由、检索、每日整理均通过登记入口；TaskDo.initial在真实画像准备前绑定新请求版本，恢复仍使用原保存请求。工作台首次规则判断前捕获入口版本并延续到await、后台任务与SSE；TurnExecutionService.completed_intent仅为body一致的completed缓存复用保存意图，实际回放仍经原幂等守卫；每日整理入口绑定learn版本。整理与路由仅在旧请求身份及输入可信匹配后选其冻结入口，保留原缓存和实时校验。两离线评测支持重复`--policy iface=@版本`，退出恢复。验证：test_policies.py、test_policy_turns.py、test_policy_runtime.py、test_policy_entries.py、test_policy_product_replay.py、test_policy_extract_replay.py、test_policy_route_replay.py、test_policy_organize_replay.py、test_policy_task_freeze.py、test_policy_evaluations.py、test_policy_runtime_leaves.py、test_policy_front_freeze.py、test_policy_learn_freeze.py及test_memory_policies.py。

- T12.12 场景继承：`v2.policies.scope.v2` 在指定场景时允许本场景与未分场景的项目层，保留`scope@1`精确场景回退；WorkspaceQuery所有文档层及原件关联、认识与bookshelf使用同一登记判断。`scene_priority/prefer_scene_ties`只交换同有效分数的位置，本场景优先，原分数、其他分数位置和无场景顺序保持。只关联兄弟文档的原件不会因过滤后关联为空被当成项目层；me认识继续仅进入T12.4常驻画像块。离线评测支持文档/认识/题目scene注释并传入真实召回链，新增场景继承六题；验证test_scope_inheritance.py、test_scope_evaluation.py及相关阶梯/书架/评测测试。
- T14.7价格声明与计算：shared/llm/model_capabilities.py的prices与model_prices.ModelPrices/validate_rates/calculate_cost只依赖标准库，精确官方身份及有效时间选CNY费率，Decimal计算完整input/output/cache分账；未知或观测不完整返回None，不兑换外币、不套代理价格。
- T14.7冻结价格旁路：ModelConfiguration.model_prices/update_model_prices/price_wire_sink复用现网关与原WireHandle，memory_app/model_costs.PriceRecordingSink只写白名单公共身份及费率到v2_model_wire_prices，v2_model_prices独立CAS同时校验配置修订，不改外发授权修订。kernel.receipt_projection.aggregate_cost按canonical attempt聚合一次，旧记录不初始化、不迁移、不按现价回算；question_receipt可注入既有records只读投影。
- T14.7价格接口与展示：GET /api/v2/settings的model[purpose].pricing及PATCH /api/v2/settings/model-prices复用设置入口，独立expected_revision/expected_configuration_revision；ChatCompletionStreamChunk.cache_observation默认None，仅透传实际已报告缓存。frontend shared/lib/modelCost.formatModelCost按十进制半入四位，未知为—，设置表与ContextPanel复用Row/mono token。验证test_model_prices、test_model_costs、test_settings_prices、test_model_cost_stream、modelCosts.test.jsx及相关原模块。

- T14.2写时检索投影：`core/document_engine/retrieval_index.project_document` 与资料写入同事务，产中立条目、摘要和精确坐标分块；`core/storage_provider/source_retrieval_index.project_original(tx,id,project,revision,text)` 复用12个原件写接点，只接受显式领域参数。`source_mutation/invalidate_source/project_source/refresh_source` 在既有旁路DB绑定namespace/revision/incarnation，SQLite→file固定锁序，正文修改前失效，成功后只刷新自身token。三个派生集合document_retrieval_index/original_text_retrieval_index/source_retrieval_index不改原payload；许可始终按现态判断。
- T14.2索引消费与定点读取：`v2.retrieval_index.RetrievalIndex.prepared/indexed_entries/vector_material/hydrate` 读取条目及分块，修订失配丢弃并后台补算；向量校验只读对应父投影，最终命中读取真实正文并复验精确片段。`SQLiteStructuredRecordStore.list_projected/read_projected` 只允许固定metadata白名单，后者按绑定ID读取，保留原读lease和嵌套形状；`shared.document_visibility.from_repository(metadata_only=True)` 沿原发布规则投影资格，默认行为保持。`core.document_engine.markdown_sections` 与 `v2.contextual_chunks.select_indexed_contextual_windows` 复用原解析/排序算法。验证test_retrieval_index.py、test_source_retrieval_index.py及现有书架/阶梯/原件/存储测试；完整66题业务对象与原报告一致。

- T14.9快模型：ModelConfiguration.fast_model/update_fast_model/freeze_auxiliary_binding/for_auxiliary复用原治理网关，独立selector与父generation/mode CAS，不改public/snapshot或暴露密钥；kernel/aux_routing在调用者身份事务stage选择、真实接受后投影独立不可变路由，历史缺绑定沿主路由，修订漂移拒外发。现receipt_projection按真实wire模型投影费用；v2/settings与既有设置UI单一配置入口，v2/multi_query保序并发并等待真实worker失败/取消结算。验证fast_generation/fast_generation_modes/fast_aux_calls/fast_model_settings/parallel_multi_query与fastSettings。

- T12.14情境方法：retrieve@2/compose@2经get分派，policies/method_situation按条件匹配与输入覆盖筛选；WorkspaceQuery、multi_query融合、ladder与budget复用原证据资格/去重/预算。method_context.prepare_methods/freeze_task_methods/frozen_task_methods为现有TaskDo与task profile reader提供v2_task_methods冻结旁路，ProductTaskPlanner实际wire单独加入方法块；画像字节与原事实保持。
- T12.14纠正：context_feedback.strike及GET/POST /api/v2/workbench/turns/{turn_id}/context-feedback按原项目、原轮已消费条目与精确修订写v2_context_feedback；do_context复用内核实际wire投影，未发送初始化材料不可划掉。ContextPanel复用Row显示补/×并隔离迟到响应。验证test_situation_methods/test_context_feedback/test_task_methods/test_method_evaluation及contextMethods；离线tools/method_eval.py，浏览器Q48。

- T12.6有效时间：v2/insight_validity.read_validity复用确认历史与取代事实，close在现有review事务CAS闭合区间，backfill只补缺失新旁路。WorkspaceQuery.filter_time_candidates统一原问题冻结日期，供主检索、重写和书架调用；retrieve@3的time/time_candidates/validity与compose@3历史标注/区间去重经get分派，普通输入沿@2。tools/backfill_insight_validity.py须显式已有副本DB，无默认runtime。验证test_insight_validity/test_temporal_consumers及相关阶梯、书架、多问法和认识服务；Q49五轮22布局通过。_query_answer保留选材temporal/validity，仅已结束历史认识投影可选historical:true；CitedAnswer沿--muted显示当时，普通/旧回执不增加字段。验证test_temporal_receipts和citedAnswerTemporal，兼容真实API与原下钻。

- T16.2 编号证据复验：`memory_app.source_egress.recognition_service(records, cache_invalidation=...)` 构造原 RecognitionService，并显式传入同模块固定 `validate_external_number` 能力。SQL 编排复用原材料解析、资格与 SourceEgress、精确选中记录和出生身份；领域消费者在调用前后比较原证明字节，缺能力拒绝 SQL，显式历史 JSON 保留独立提交核验。`original_sources.resolve_turn_material` 是原函数搬移，v2 保留同对象导出；`recognition.external_evidence_json.encoded` 是原 canonical JSON 唯一纯 owner。两个 connection 属性只委托原连接，读事务与外层提交权保持。验证：test_sql_external_input_dependencies、test_external_input_dependencies、test_external_experience_identity、test_mcp_evidence 及实际构造调用方；完整 T16.2 的冻结发送边界仍未通过。
- T12.7纠正事实与使用明细：shared/memory_sidecars.record_correction在RecognitionService及context_feedback原UOW中CAS追加v2_correction_events，对象种类参与键；真实冻结wire/recognition_versions核before，画像不占引用编号，无历史wire不虚构事件。UsageStorage只为实际引用者追加最近64条at/kind与older_count，strength@1不变。
- T12.7积累与手动整理：v2/learning_events.events/checkpoint/complete_in_transaction读取现确认/整理/纠正事实，持久化增量与运行时间并同完成租约清零；DailyJobs复用原scheduler，trigger@2经get决定10分/7200秒/休眠cap，启动不整理，保留24h兜底。Consolidation.status/request及GET/POST /api/v2/library/consolidate复用原日期租约、同实例及jobs投影，GET只读，POST仍project_id→job_id。
- T12.7纠正冻结与依据：consolidation_events.corrections/validate复用SourceEgress当前资格和owner CAS；freeze_adapter只替原冻结材料load_text，v2 MemoryTurn可选caller-owned freeze_request默认保持。selected_source_count经正式snapshot/original_count只计候选所引event_ids的原件闭包；完整输入provenance用于追溯，不宣称全被引用。trigger@2/consolidate@2独立登记并切换ACTIVE，验证test_accumulation_trigger/test_consolidation_events/test_consolidation_manual/test_feedback_corrections/test_usage_events及相关领域、Library测试；tools/learning_eval.py双策略，浏览器Q50。
- T12.18事件底座：v2/signals.SignalService/PreparedSignal复用原SQLite事务与实际Turn/待确认认识资格，只持久化copy/view/stop编号、修订、时间和actor；公开接口禁止stop与正文，响应后safe_record失败仅记异常类型。enabled/cleared_at CAS隔离关闭及迟到写入，每日signals_rollup在同一事务把180天前事件并入按月计数并删除；管理员事实不计用户统计。合法rollup-UUID键通过payload的project_id/month保留归属，原记忆导出与外部选择不包含信号。验证test_signals.py40项及原daily3项；前端接线已完成，浏览器Q57待集中。
- T12.19只读统计：SignalService.report仅从已有修订、冻结请求、原调用回执与使用记录推算七项数字，返回编号、数字与固定枚举，不返回业务正文；关闭记录及清除时间前的事实排除。模型按真实完成调用用途归因，缺版本或来源证明保持未知；未应用的冲突AI块不进入正文比较基线。reask@1本机策略含600秒窗口与独立校准阈值，42对样例按15校准/27留出划分；登记与默认激活分次提交，未来producer接线随对应任务补测。
- T12.19离线工具：tools.signal_report.open_signal_copy拒绝正式根与相交路径，物理复制现有数据及WAL/SHM到临时分析副本，注入原authority/记录/publication读模型，JSON沿原codec；输入只读且前后文件名、大小、时间保持。原WorkspaceQuery/RetrievalIndex只读盘点不修索引，kernel_call_groups可选读取父轮真实不可变输入；默认owner行为保持。signal_report与memory_eval --replay共享输出保护，拒既有文件覆盖，复用原检索比较两版(layer,id)排名，零模型调用、不输出正文；未引用L0沿真实主调用/输入/回执/有序来源链核资格。冷投影、私密无模型基线及未来使用事实保持未知。验证test_signal_tools与原8caller；副本须静止一致，用户真实副本另需授权。
- T12.18界面信号：shared/signalsApi复用productFetch与原URL owner，有限128项会话去重、epoch失效与AbortController，无正文/持久队列/重试；CitedAnswer只移除合法角标并保真实局部Range，Workbench复用Icon/FocusPanel处理复制回显和pending查看，Library复用原scope/generation资格。SettingsData复用Row/Switch与实际devices mode，CAS关闭、三秒同位清除/可点击回读重试；过期关闭失败不覆盖后来epoch。变基后17新usageSignals与原60共77前端、43后台、构建及12契约通过；浏览器Q57待集中。

- T12.19认识打开统计补修：SignalService.report补读原UsageService实际写入的v2_usage_insight，保留旧document/recognition/candidate历史集合；不新增事实writer或改变报告七项结构。原RecognitionService发布到UsageService打开的真实链3项、关闭/清除准确cutoff与pending零写、原统计28项通过；用户模型和正式数据零访问。

- T12.15成果编辑事实：v2.outcome_corrections.record_edit借原文档保存事务和真实历史修订，沿product_draft_source核交付出生修订与public Turn，在v2_outcome_corrections以CAS记录首before/末after。纯outcome_correction@1经get决定600字截取与十分钟窗口；窗口内无正文变化的保存仍推进末修订与成功保存时间，成熟事实保持原样。登记与ACTIVE激活分次提交，新窗口支持离线override，旧窗口保原策略版本；统一消费者见下方登记。验证test_outcome_corrections及原文档/策略直接调用方。
- T12.15分工调整事实：record_division_adjust由原TaskDivisions._change在同一事务调用，读取真实保存前后样例行，CAS追加有序目标列表、public Turn和两版修订。每次成功已调整保存单独记录，原样例结构、快路排序和删除语义保持；验证test_outcome_divisions及原task_divisions两个直接控制，计分与冻结沿下方统一消费者。
- T12.15反馈冻结接点：kernel/turn_requests与v2/turn_requests的freeze_product_turn可选load_verified_feedback仅供memory.consolidate使用，原材料加载后执行一次并附入已核验反馈文本。默认/None请求字节保持，原memory.text禁止、材料资格、privacy终检和Core格式保持；验证test_outcome_freeze17项及原freezer7项。统一消费者已验零材料配置guard与私密实际辅助路由。
- T12.15成果纠正计分：learning_events.events复用原积累流程，只读取完整成果修改、真实完成重做和成功分工调整事实；outcome:前缀隔离旧事件编号，按记录策略判断编辑窗口成熟，未完成、未知版本和净撤回不计分。checkpoint/reset不封存或改写事件，实际消费沿下方统一消费者，计分不替代消费。验证test_outcome_accumulation及原积累四项。
- T12.15成果重做事实：record_redo由原重做入口在新Turn事务内读取旧成果真实出生与当前历史正文，complete_redos由TaskDo原最终发布事务CAS补新成果。public Turn与Kernel映射沿原product_draft_source核验，300字限额沿记录策略；失败旧结果重试和未完成新结果保持零完成事件。验证test_outcome_redos及原分工/干活三个调用方，真实消费沿下方统一消费者。

- T12.15成果反馈资格：consolidation_events.outcomes/validate_outcomes/verified_feedback沿真实product_draft_source出生、文档历史、分工连续修订链和原SourceEgress/read-proof重建三类反馈，outcome:消费编号只认已提交consolidation_inputs。要求目标project并拒跨项目、混合本地模式、重复事件和损坏owner；本地资格不授权模型外发，零refs不造Experience或空snapshot。9新节点分批3/6通过，旧纠正6项与3精确契约通过；统一消费者已验候选私密继承与辅助路由。
- T12.15成果反馈消费：consumer_outcomes/validate_consumer_outcomes/outcome_key与原Consolidation._pattern共用MemoryTurn、辅助路由和候选/消费事务，真实出生稿用ensure_document_experience可选retained_revision保留原来源闭包，默认调用保持。只认显式无稿回执或真实绑定Root；真正零材料反馈不造Experience与snapshot，有父来源却无Root的分工保守延期。缓存绑定事件、全部owner、来源与隐私；普通和私密Me支持沿原关系owner待人工接受，元数据固定出生修订。16新节点分批通过，原9调用方与3精确契约保持；次日收集及完整任务179测试、12导入契约通过，4037d4e128已合入。
- T12.15次日普通收集：Consolidation._collect复用outcome_corrections._root出生证明，排除已编辑成果及依赖它的候选、认识，避免以当前修订重建出生材料。原未编辑成果与普通文档的使用时间窗保持；test_outcome_consolidation_nextday的待确认/已确认两个场景、四原调用方及三精确导入契约通过。
- T12.20纠偏清单与决定：SignalReviews复用原七项事实与实际ASK历史输入，review@1经get选择每项目最多5项，首次展示14天并保留起点。批量决定同SQLite事务复读owner与设置CAS，关闭、清除及旧令牌冲突拒绝；丢弃重问保留原pairs并输出编号反例。完整后端141项分批、前端80项、构建及12契约已通过；T14.11实际停下producer待对应任务。

- T12.20明确纠正计分：learning_events只将有效用户confirm reask/stop沿实际ASK Turn、项目与时间资格计为signal-decision编号，复用原get(trigger)与幂等积累。丢弃、unused、admin、未知字段及跨项目不加分，关闭隐含记录不撤销明确确认事实；冻结与消费复用原Consolidation接点。
- T12.20纠偏界面：SignalReviews/useSignalReviews复用Library API、useRequestScope、Row/FocusPanel/Icon和原下钻，项目与CAS批处理、0.2秒淡出、返回及迟回调失效保持。读回清单仅清除已移除或修订变化的勾选，同ID与revision保持；换项目清除勾选。降权认识复用forget(false)，整理稿restore_document_preference通过原库事务与双修订CAS恢复旁路，归档优先原restoreDocument，只读不显示写操作，恢复不改变核对状态。Q61修复15新后端、54原调用方、8新前端、40原调用方与3精确契约分别通过，源码提交3f0ac8812b；Q61四布局批处理和五对象恢复已通过，T14.11实际停下随任务补验。
- T12.20问答反馈资格：signal_review_feedback.review_corrections/validate_review_corrections/review_key/review_feedback复用原ASK不可变请求、回答、主调用输入和completed回执r2生命周期；按原有序history_turn_ids有界递归祖先，并沿冻结compose及followup formatter整段核对实际wire。真实SourceEgress/SourceGraph、来源偏好、公开Turn CAS和全部祖先证明同时进入复验与缓存身份，before/after按review@1截取回答300字。只使用已有合法Experience和Recognition，不造原件；缺保留证明和跨Me来源延期，真实stop生产者仍待T14.11。交易复验沿原RecognitionService资格reader，无额外连接；22项已纳入最新88项相关测试。
- T12.20原整理消费者：Consolidation与consolidation_events.freeze_adapter把明确问答纠正送入原MemoryTurn冻结反馈接点，并在派发及提交前复验真实来源。候选、pattern和消费事实同一事务；混合旧纠正保留全部真实认识父，支持关系无法承载时产生待确认候选，显式确认后仍保来源闭包。关闭生成阻止额外overview调用，同日回放零新增外发。原消费正例1项与真实SQL回滚、来源撤回、重复目标、混合来源及未确认负例13项通过；前端80项、构建及12导入契约通过，浏览器待集中。

- T12.17 本机项目提示：`v2.elsewhere.suggest_elsewhere` 与注册的 `elsewhere@1` 只比较项目名、场景及经原来源/修订证明核验的概览文字；复用原词法相似度，私密只在本机计算，共享范围须实际读取授权，未知时不推荐。`ScopeOverviews.current_metadata` 复用原概览与只读来源索引资格，不触发概览生成或索引修复。原阶梯的词权重并集与覆盖度提取为 `policies.enough.coverage_terms/weighted_coverage`，阶梯与最终书架证据导航复用原数学，原 enough@1 与 trace 不改。原 Workbench 标签点击在目标范围新发同一句问，冻结结果GET不重算；版本当前仅由原operation闭包捕获。验证24新节点、11阶梯调用方，离线30项目提示样例及原72报告保持；默认激活与最终组合验收由root随后执行。
- T12.21 待补事实与 API：`v2.gaps.Gaps` 从完成 ASK 的原冻结请求、不可变模型输入、最新终态和公开轮次推导本机清单；`gap@1` 经 registry 选择，复用本机文字原语按场景合并、90 天过期。最终覆盖率沿原阶梯公式，校验材料编号、身份和精确引文，排除问题、历史及画像；证明缺失或歧义时保留既有结论。投影只存编号、修订和时间，问题原话按需读取，丢弃单独记录事实。`refresh` 注册到原 `DailyJobs`，通过独立只读 `WorkspaceQuery.local_coverage` 复查新资料，不调用模型或修复索引；验证当前资料后，在投影事务中复核设置、原轮次和 CAS。原 `kernel.receipt_projection.kernel_call_groups` 的默认关闭选项支持合法无模型无命中轮次，不改变既有读取默认行为。自动化和集中验收入口见 notes/task.md T12.21 与 Q64。前端复用 Composer 可选 draft/onDraft 保存本次会话内的项目草稿，handoff/onHandoff 一次切换场景范围、记住意图并聚焦；成功发送通过原回调清空来源草稿，失败保持正文与附件。

- T15.15 本机对话分享：`shared/lib/conversationSnapshot.js` 只冻结问题、回答和普通引用标题/片段，移除已识别角标，画像、用量及内部身份不进快照；`readingMarkdown.js` 复用原 unified/remark AST，仅移除阅读器既有根级系统标记节点，MarkdownBody 共用同一判断，原稿与坐标不改。`ConversationSharePreview` 复用 FocusPanel、Icon、StatusDot 和 useRequestScope，引用可逐条移除，关闭/换范围作废迟到导出并回收 URL；`conversationImage.js` 用原 token/字体、Unicode grapheme 和原换行生成完整 PNG 分页，不读取远程图片。Workbench 的 ASK/灵感及真实 DO/记住整理稿入口共用，后两者沿 recognitionApi.loadDocument 读原修订。新39及原工作台52节点、3相关导入契约通过；平台替身覆盖边界和未完成的服务器/Q66见 task.md。

- T15.9 时效与搜索：rank@2经原registry/get选择，将过期认识排序权重乘0.5且保留召回；冻结时刻/策略及过时标记沿原画像和问答读模型复用。search@1在本地不足且时效问题时触发原web.search辅助Turn，复用ModelConfiguration与LiteLLMNativeWebSearchGateway；v2/search.supplement只追加预算内证据，WorkspaceQuery通过原WorkspaceIntake批事务保存被引用结果。每轮原搜索证明在普通材料读完后完整复验一次，MemoryTurn仍复验冻结请求与当前权限，空材料与直接调用保持。CitedAnswer沿既有标签token显示过时与网址域名。自动化结果及完整工厂未复验边界见task.md/Q65。

- T16.4本机目录与材料（隔离分支codex/T16.4的40942e5c8f，主线尚未合入）：`v2.external_permissions.HostPermissions` 是desktop/local-user目录首次确认唯一CAS owner，通过`folder_reference/confirm_folder`复用许可修订并绑定目录元数据身份；`external_workspace.task_material_payloads/task_path_identity/verify_task_material`复用原名称碰撞与16MiB总量校验、有界读取及实际fd身份，原`WindowsHandleTreeIo.write_new_tree(expected_root_identity=None)`在任何目录写入前核已打开根HANDLE，默认调用不变。`policies.get("external_task_input", version="@1")`仅为指定folder给出冻结资料相对路径，ACTIVE不切换；ExternalRunner共用严格新档案与精确旧五字段分类，旧MCP白名单语义保持。实际实现与275节点证据权威入口为D:/AIworkspace/Codex/.codex/worktrees/t16-4/ChriptmasAgent/及work/T16.4-local-completed-leaf/；原Runtime/facts/intent/invoke/模拟CLI已验，真实MCP、登录、服务器及界面确认不在此阶段结论内。

- T16.4可信本机Host装配（隔离分支codex/T16.4的031bbde336，主线尚未合入）：`v2.external_host_bootstrap.discover_local_executors/install_local_host`仅消费可信启动PATH、系统profile及原认证目录位置，解析Windows原生入口与已核固定版本Codex npm nested/hoisted/bundled布局；wrapper不读不执行，认证目录不创建不读正文。公开package.json经既有Core `_Nt`单文件HANDLE在读前核五字段身份并有界读取，不枚举目录或重开正文；`external_host._local_candidate_path/_local_candidate_metadata`共用全部盘符先核与盘根到叶祖先预检，`ExecutorRegistration.discovery_stamps`为内存元数据资格快照，持续到原prepare/lease，旧显式Host空默认兼容。原v2首次Kernel前用同DeploymentLayout/root/records/context/owner固定Host，缓存不重新发现，server/未知配置仍拒绝。129不同节点及4定向契约证据入口为work/T16.4-host-bootstrap-completed-leaf/，源权威位置仍为D:/AIworkspace/Codex/.codex/worktrees/t16-4/ChriptmasAgent/；整项、既有登录、真实MCP与系统隔离尚未完成。

- T16.4可信内部并发（隔离分支codex/T16.4的3f85b17a9a，主线尚未合入）：原`HostAdmission(concurrency_limit=1)`接受可信严格正整数构造值，固定后经prepare真实准入才Weak登记租约records/owner/limit/原plan身份；`HostLease.execution_limit`校验同源真实签发身份供原execute_external复用ExternalRuns(limit=...) CAS占位。原close按真实签发拥有权清环境和秘密，未签发副本只脱离自己；原派发接点在before_launch后再验，不改事实或持久shape。213不同聚焦节点与4导入契约证据见work/T16.4-concurrency-completed-leaf/，源权威位置为D:/AIworkspace/Codex/.codex/worktrees/t16-4/ChriptmasAgent/。此处只提供可信内部Host配置，设置UI/server/真实MCP和登录不在阶段结论内。
- T12.8材料绑定下钻：WorkspaceQuery.gap_materials复用原Document markdown/RecognitionService与SourceEgress来源闭包重建L3/L2正文和精确窗口，纯策略只经原领域owner构造prompt。kernel.answer_turns.gap_answer_input及原nested handle在网络前不可变绑定input_ref/route_ref/actual model_request_id；短锁/事务在wire前释放，缓存复验身份、失败/超时/缺历史binding不新发。multi_query.expand_gap_plan复用原aux预算槽与lower RRF，充分/空材料零aux，失败按原预算退回。验证新材料17节点分批、原8caller与3精确契约；ACTIVE/质量仍待。
- T12.8工作台接线：`v2.workbench._answer_workbench` 仅在原accepted冻结retrieve@4时成对调用prepare_drilldown/expand_gap_plan，默认@3保持原ladder/rewrite直调用。历史与私密/来源复验、书架恢复、引用及原回执保留，condense/gap/primary最多三个原nested handles；新gap撤回映射既有409，迟到结果丢弃且已发生用量保留。验证新11真实HTTP控制分批、原4caller与3精确契约；零gap原5样本本地中位226.846ms≤250ms通过，原样保留413.023/354.188ms两慢样本；真实质量Q58仍待，ACTIVE保持@3。
- T14.5管家主配置恢复：ProductGenerationRouting私有provider-store资格复用真实parent Planner及原coordinator binding/topology验证，保留_main_owner；ProductTaskPlanner仅为steward.scheduler冻结既有primary/Main选择并接原Responses背景激活，worker仍OFF。原child严格租约、2000token及每GET当前guard/费用owner不变，历史缺失/OFF不接管。验证新steward13节点分批及6原caller、3精确导入契约；跨attempt/full output/回执字段仍待。
- T14.5辅助记忆后台恢复：`kernel.provider_store_binding.memory_provider_store_call` 复验既有冻结 BINDINGS/CHOICES、原五字段 Memory 路由及当前模型，将原引用和缓存身份投影给既有 governed 调用；`MemoryTurn.generate` 只为普通 generation ON 接入原物理回执与自有严格租约。OFF、历史缺失、custom invoke、vision、embedding 保留原路径；UNKNOWN 保留租约，成功已结算才释放。验证新23节点按4/12/1/6分批通过、原5caller与3精确导入契约；原120000ms执行预算不变，跨attempt/full output/服务商暂存回执字段仍待。

- T14.5原Do生产接点复用：`v2.workbench.persist_workbench_turn` 共用原创建事务内的线程、轮次、ITEM_STATES及TASK_EXECUTIONS写入，原create仍负责同事务executions.complete与commit；`v2.TaskRuntimeProjection`直接持有原runtime/store/composition/organization，原Lazy委托后保留懒加载、冻结范围与usage缺owner行为。验证新writer两个节点及原协调器read/privacy/topology三个节点5P4.27秒，两个精确导入契约保持；原HTTP夹具startup缺recognition_service的两个ERROR保留，不代签消费者。完整Do后台GET恢复仍待。
- T12.16灵感只读投影（隔离检查点，未激活）：`v2.inspirations.collect_inspirations/resolve_inspiration/validate_inspiration` 从真实灵感轮次、原 ExperienceProvenance、归类连接及来源修订解析用户原话；候选编辑不改变原话。`scope@3`/`compose@4` 已登记，问的独立灵感层计入预算，充分判断保持原四层；引用沿当前项目原 drill URL 展示只读原话与码点，使用记录复用 UsageService。16 个非干活语义节点分批通过，原调用方 21 项与根补验 8 项通过，前端新增 11 项、原调用方 55 项和构建通过；新增灵感题 0/6→6/6，旧 72 题命中不变。干活冻结证明的 JSON 形状不一致、任务版本/资格及适配器循环依赖仍未通过，三轮修复后冻结，完整 T12.16 未完成、不切默认、不合入。上述辅助函数只作为已验证问答与下钻接点，干活适配器不可作为可用公共能力。
- T16.9 方法导出：v2.skill_exports.SkillExports 复用 RecognitionService 原资格、insight_view、SourceEgressService 与同事务 reader，旁路保存源修订、原来源闭包、审阅和导出事实；saved scope 版本维持既有草稿判断。v2.skill_generation.generate_skill 复用原 MemoryTurn/freezer/辅助路由，生成前后核对原冻结输入，保存端核原 accepted request、model.completed 与最终 turn.completed；同生成键返回原草稿身份并保留用户编辑。纯 skill_export@1/skill_author@1 经 get 调用，ACTIVE 保持原样。
- T16.9 包与目录：v2.skill_package.validate_document/package_files/zip_package 用原纯路径资格和 SafeYAML scalar 序列化输出 Agent Skills frontmatter、逐步骤来源及 references/methods.md；v2.skill_folder.write_reviewed_folder 复用 ApplicationSkillVerifiedContent 的结构限额与 WindowsHandleTreeIo，create-new、句柄相对访问、精确全树字节复验；不同目录内容拒绝覆盖，相同内容支持 SQL 事实失败后的重试。无文件摘要、自动安装或来源正文修改。
- T16.9 前端：SkillExportPanel 与设置 SkillExports 复用 Row/Switch/FocusPanel/Icon/useRequestScope；libraryApi 沿原 productFetch 提供来源、草稿 CAS、审阅、生成、zip POST 与双 CAS 本地目录导出。新 DTO 显示需更新与可用能力，迟回调按 scope 失效。验证 test_skill_exports/test_skill_export_routes/test_skill_generation/test_skill_folder 与 skillExports/libraryApiSkillExports；服务器用户域和 A/B 待确认改进入口继续依赖原阶段 16 接入。
- V1 本机文字向量：`local_vectors.VectorWorker` 共用进程内 CPU 编码器，查询优先、文档按批让出；`LocalVectorTransport` 保留原冻结配置、来源与 MemoryTurn 回执。`v2.embedding_settings.EmbeddingSettings` 只写模式、安装和索引旁路，模式切换保留外接配置；安装权限委托原 ServerUsers。`v2.embedding_index.EmbeddingIndex` 用原认识整段与整理稿 L1 分块四键缓存，复用原 DailyJobs 调度。`local_vector_assets` 是唯一官方文字派生安装 owner，CLI 与设置均委托它；官方 safetensors API 选择 413 个文字参数，配置关闭视觉和音频，manifest 明确派生来源与字节校验，暂存后原子发布。`vector@1` 登记模型、256 维度、前缀与资源限额；SettingsPage 沿用 Row、Switch、token 和原请求 scope。
- T14.5服务商暂存设置：`features/settings/ProviderStoreField` 在生成展开区复用 Row/Switch、useRequestScope 与原用户空间事件，默认关、仅 API 能力 available 时显示，订阅与本机隐藏；`providerStoreApi` 复用原 settingsRequest/productFetch，以原三修订 CAS 保存选择，拒绝保持原值，旧用户迟响应失效。没有新增 CSS、token 或依赖。新设置六节点、原 settings 十三节点（真实 RED 后仅补后端 HTTP unavailable 夹具）、快模型两个页面节点与生成价格三个页面节点共二十四项通过；后两组只验证原页面断言，不代表未隔离的能力 HTTP 边界通过。浏览器待既有 Q52；原 Do 消费者复审冻结，完整 T14.5 未完成。
