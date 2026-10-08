# 界面原型（视觉权威参考）

用户已于 2026-09-30 确认这套原型。在线可点击版本：https://claude.ai/artifact/PWijd1ZJ4GzuPJuVchPP3H （仅用户本人可打开）。本目录保存同一份源码，供执行者逐项对照。

- 这些文件**不是运行代码**，不要导入到 `src/frontend`。它们使用画布格式（`.dc.html`、`{{hole}}` 模板、`<sc-if>` / `<sc-for>`），只作为布局、颜色、间距、文案和交互的参考。
- 实现时，颜色、字体、圆角、间距以 [DESIGN.md](../../DESIGN.md) 的 token 表为准；DESIGN.md 与原型冲突时，以 DESIGN.md 为准。
- 文案以原型为准。不要新增解释性句子（见 DESIGN.md "少字预算"）。

| 文件 | 画面 |
| --- | --- |
| `Main.dc.html` | 工作台：空状态 → 发送后的线程、三种回执、聚焦面板、进度托盘（全部交互都在这一份里） |
| `WorkbenchInUse.dc.html` / `WorkbenchDark.dc.html` | 以"使用中"状态和暖黑主题引用 Main |
| `WorkbenchContext.dc.html` | 以"上下文"状态引用 Main：聚焦面板里的本次上下文（用量条、分类、阶梯 trace、条目、外发）。数据形状对应 `v2/ladder.py` 的 trace、问答回执的 `model_usage`/`excluded_sources`，以及干活的 `agent/context/budget.py` |
| `Library.dc.html` / `LibraryDark.dc.html` | 资料库：四层标签、场景栏、收件箱、"我"、筛选（认识：待确认/生效/已遗忘；整理稿：未核对/已核对/已遗忘）、"⋯"菜单（约束跳转、只读备忘）、下钻面板 |
| `Settings.dc.html` | 设置：模型、隐私、项目、数据 |
| `SettingsModel / SettingsPrivacy / SettingsProject / SettingsData.dc.html` | 以展开态引用 Settings：模型（方式、地址、密钥、测试、按用途外发开关）、隐私（私密资料、外发记录）、项目（场景、私密、约束）、数据（备份、导出、导入、迁移） |
| `Companion.dc.html` | 伙伴页：聊聊、回顾、专注、安排 |
| `Mobile.dc.html` / `MobileLibrary.dc.html` | 390×844 手机版工作台与资料库 |

### 阶段 12–16 增补画板（设计方 2026-10-03，待用户确认）

静态画板，不可点，每张有"暗色"开关。2026-10-06 增补 S12Signals、S12Review、S12Gaps、S12Data 四张（使用信号与备份自检），接在第一行末尾。2026-10-07 增补 S15Vectors、S15FindImage 两张（本机向量与以文找图），接在第二行末尾。画面内容按 DESIGN.md 各阶段的"界面增量"画出；两者冲突时以 DESIGN.md 为准。画布上分三行：阶段 12–14、阶段 15、阶段 16。

| 文件 | 画面（对应任务） |
| --- | --- |
| `S12Pending.dc.html` | 资料库 · 待确认：关系图标（＋ ≠ ◇ ≈ →）、确认到我、确认到 #项目、"评"标记、聚焦面板里的"不同于"与评论区来源（T12.5、T15.8） |
| `S12Place.dc.html` | 工作台 · 记住的归属：自动写场景、跨项目建议、"已移到"与撤销、"新建 #X"（T12.13） |
| `S12Inbox.dc.html` | 资料库 · 收件箱：同主题 3 条时的新建项目、收到的分享（"来自"）（T12.13、T15.15） |
| `S13Route.dc.html` | 工作台 · 自动拆分：拆分行、⋯ 改路由（切换意图、合并、删除）、等待依赖的空心点、"规则"标记、意图"自动"（T13.2–T13.4） |
| `S12Context.dc.html` | 工作台 · 本次上下文：画像一行、"补"与划掉、搜索引用的域名、"过时"、花费（T12.4、T12.14、T14.7、T15.9） |
| `S13Steer.dc.html` | 工作台 · 中途插话：进行中的干活回执里的插话气泡（已读取为实心点、排队为空心点）、问的历史回执里插话留在回答上方、意图切换最左边的"插话"（T13.6） |
| `S12Signals.dc.html` | 工作台 · 复制与停止：问的回执行尾的复制图标、"已停止 · 继续"、进行中干活回执的停止按钮（T12.18、T14.11） |
| `S12Review.dc.html` | 资料库 · 纠偏：左栏勾选列表（重问、没用上、停下，效果标签 +1 / 降权），右栏证据，底部"确认 n""丢弃 n"（T12.20） |
| `S12Gaps.dc.html` | 资料库 · 待补：⋯ 菜单里的"纠偏 n""待补 n"，聚焦面板里的待补列表与丢弃（T12.21） |
| `S12Data.dc.html` | 设置 · 数据：备份行的校验 ✓、"使用记录"的条数、清除与开关，托盘里的"备份失败 · 重试"（T12.18、T14.12） |
| `S15Devices.dc.html` | 设置 · 设备：设备与钥匙、配对二维码和倒计时、管理员的用户列表与配额、访问记录（T15.3、T15.4） |
| `S15Shared.dc.html` | 资料库 · 共享项目：成员、只看、操作记录与筛选、确认人头像、⋯ 里的"提取到 #项目"、备份、转让、删除（T15.12–T15.14） |
| `S15Share.dc.html` | 工作台 · 分享一轮对话：去处、预览、去掉引用片段（T15.15） |
| `S15Companion.dc.html` | 伙伴 · 小熊提醒：头像红点、提醒卡片与关掉（T15.10） |
| `S15MobileCapture.dc.html` | 手机 · 一键记：按住说话、一行输入、"已记住"、待上传数字（T15.6） |
| `S15MobileOffline.dc.html` | 手机 · 离线包：已下载标记与日期、离线搜索、空心状态点（T15.6） |
| `S15Vectors.dc.html` | 设置 · 模型 · 向量：本机 / 外接、索引进度、展开后的模型与"图片"开关；虚线框里是这一行的其他状态：未安装、安装中、可用、失败、外接（用户追加 V1–V3） |
| `S15FindImage.dc.html` | 工作台 · 以文找图：因图片命中而引用的原件，在角标后跟 28px 缩略图，点开看原图（用户追加 V2） |
| `S16Settings.dc.html` | 设置 · 模型：转写和识图的手机 / 本机 / 外接三档、搜索、外部 agent、连接说明与快照、安全告知、默认执行者、代理接入（T15.1、T15.9、T16.1–T16.8） |
| `S16Executor.dc.html` | 工作台 · 外部执行者干活：执行者与安全档位、步骤进度、取消、草稿与"来自"、外部用量（T16.5） |
| `S16Skill.dc.html` | 资料库 · 方法导出为 skill：⋯ 里的"导出为 skill"、草稿审阅（步骤带来源、验证）、导出、"需更新"（T16.9） |
| `S16ChatGPT.dc.html` | ChatGPT 里的卡片（示意）：召回结果卡、待确认认识卡。宿主界面只是中性示意，不仿照 ChatGPT 的真实界面（T16.7） |

## 图片资源对照

原型里的 `/_blob/<id>` 是画布上传的图片，对应本目录 `assets/` 下的文件。实现时把需要的文件复制到 `src/frontend/public/mascots/`。

| blob id | 文件 | 用途 |
| --- | --- | --- |
| `a1342611c9d3b97bd9c8b4358f376583` | `assets/bear-head-ready.webp` | 小熊头像：平常 |
| `f1441c1f5e91a0cd30d3657a198e9b31` | `assets/bear-head-working.webp` | 小熊头像：处理中 |
| `534ec8071ddfdf6c7d1ed4414f26015e` | `assets/bear-head-attention.webp` | 小熊头像：有待确认 |
| `a4b07d014ea0f58b654900350c1acf51` | `assets/bear-ready.webp` | 伙伴页大图 |
| `1790b7672614ac5bd39ce99b2b0033dc` | `assets/bear-attention.webp` | 伙伴页大图（聊聊） |

这些图片由 `src/frontend/public/mascots/bear_companion_sprite-v1.png` 裁切而来：每帧 384×468，第 0/1/2 行分别是 ready/working/attention，取每行第 1 帧。
