import { Row, StatusDot } from '../../shared/ui';
import { EXTERNAL_SOURCES, readExternalProgress, receiptExternalUsage } from './externalProgress';
import { ExternalTaskCitations } from './ExternalTaskCitations';

const labels = { turn: '开始处理', error: '收到执行异常事件', reasoning: '模型处理', todo_list: '收到安排',
  file_change: '收到文件变动', message: '收到消息事件', tool_loop: '进入工具步骤',
  command_execution: '执行命令', web_search: '检索网页', mcp_tool_call: '调用 MCP 工具',
  file_read: '读取文件', file_write: '写入文件', agent: '调用执行者', tool_use: '调用工具' };
const statuses = { reported: '已报告', partial: '部分已知', unknown: '未报告' };
const shown = value => Number.isSafeInteger(value) && value >= 0 ? String(value) : '未知';
const label = event => event.kind === 'started' ? '执行进程已启动' : event.kind === 'finished'
  ? ({ completed: '执行进程已结束', failed: '执行进程失败', cancelled: '执行进程已取消', timed_out: '执行进程超时', output_limit: '输出达到上限' })[event.status]
  : labels[event.name ?? event.stage];

export function ExternalUsage({ receipt }) {
  const usage = receiptExternalUsage(receipt);
  return <div className="workbench-external-usage" aria-label="外部用量">外部 {EXTERNAL_SOURCES[usage.source]} · {statuses[usage.status]} · 输入 {shown(usage.tokens.input_tokens)} · 输出 {shown(usage.tokens.output_tokens)} · 金额未知</div>;
}

export function ExternalTaskProgress({ receipt, readOnly = false }) {
  const progress = receipt.external_progress ? readExternalProgress(receipt.external_progress, receipt.executor) : null;
  const events = progress?.events || [], current = events.at(-1);
  const files = receipt.external_files;
  const fileLabel = files && ({ pending: '等待产出附件', captured: `附件 ${files.count}`,
    partial: `附件 ${files.count} · ${files.rejected} 个文件未保存`, blocked: '附件未保存', import_failed: '附件未保存',
    quota_exceeded: '附件超出空间额度',
    empty: '约定目录没有附件', unavailable: '附件记录未完成', not_agreed: '文件目录未约定',
    unsupported: '执行器未提供文件工具', not_completed: '执行未完成 · 附件未接收' })[files.state];
  return <div className="workbench-external-progress">
    <details><summary>{current ? label(current) : receipt.state && receipt.state !== 'running' ? '未报告执行步骤' : '等待执行事件'}<span>总步骤未知</span></summary>
      {progress?.truncated > 0 && <p>较早 {progress.truncated} 条事件未保留</p>}
      {events.map(event => <Row key={event.sequence} readOnly dot={<StatusDot aria-hidden="true" state={event.kind === 'finished' ? event.status === 'completed' ? 'done' : 'failed' : event === current && !progress.terminal ? 'processing' : 'observed'}/>} title={label(event)} meta={`事件 ${event.sequence}`}/>)}
    </details>
    {receipt.external_progress_incomplete && <p>步骤未完整保存</p>}
    {fileLabel && <p role="status">{fileLabel}</p>}
    {receipt.state === 'done' && (receipt.external_citations || receipt.external_citations_binding) &&
      <ExternalTaskCitations value={receipt.external_citations || receipt.external_citations_binding} readOnly={readOnly}/>}
    <ExternalUsage receipt={receipt}/>
  </div>;
}
