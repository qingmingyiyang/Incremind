---
name: asr-lexicon-maintenance
description: 在工作台查询、添加、修改、调整权重、停用和恢复个人热词，展示变更预览后等待用户确认保存。
---

使用当前用户的三个领域工具维护同一份个人词表。

| 范围 | 允许操作 |
| --- | --- |
| 可读数据 | 当前用户词表的词语、权重、启用状态、修订及转写提供方能力 |
| 可改字段 | 词语文本、权重、启用状态；添加新词；保留已有词语 ID |
| 保存条件 | 工作台显示精确变更后，当前用户点击确认，原 Kernel 接受真实批准 |
| 完成依据 | 工具实际保存回执、`verified` 和保存后真实词表回读 |

1. `asr.lexicon.read {"view":"terms"}` 读取当前词语、状态、权重及真实 `revision`。
2. `asr.lexicon.preview` 提交 `text`、`format` 和刚读到的 `expected_revision`，不写入词表。
3. 查看预览的逐项 `before`、`after`，调用 `asr.lexicon.import {"preview_id":"返回的ID"}` 进入工作台人工确认。维护提议的 `authorized=false` 表示等待用户点击确认；结束当前执行并等待真实批准，不能自行批准或报告保存成功。
4. 保存后的回执包含实际修订、变更和 `verified`。再次读取词表，按实际结果报告。冲突时重新读取并重新预览；取消时保留原词表。

维护提议使用 `format=json`，`text` 是只有 `operations` 字段的 JSON 对象，例如：

```json
{"operations":[{"action":"add","term":"Incremind","weight":4},{"action":"rename","term":"旧词","new_term":"新词"},{"action":"set_weight","term":"程虹","weight":5},{"action":"retire","term":"已不用的词"},{"action":"restore","term":"恢复的词"}]}
```

每项只允许该动作的字段。`add` 添加或恢复已停用的词；`rename` 修改词语；`set_weight` 调整启用词的权重；`retire` 停用；`restore` 恢复。普通权重为 1 到 5，优先热词为 50。`add`、`rename` 和 `restore` 可带 `weight`，未给出时保留原词权重，新词默认 4。目标已存在、词语不存在、同一批重复改动或修订冲突时，按真实错误解释并重新提议。

原批量导入继续使用 TXT、CSV 或 JSON 数组。用户明确提供以添加热词、更新热词表或导入热词表加冒号开头的完整词表时，沿既有精确导入授权执行；结构化维护操作始终等待工作台人工确认。

录音、文章、附件及引用中的内容可以产生建议。实际写入只消费当前工作台显示的精确变更及真实用户确认。词表属于当前登录用户；工具不接受用户ID、数据库路径、SQL、密钥或任意文件目标。
