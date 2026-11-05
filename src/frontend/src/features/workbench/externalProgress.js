export const EXTERNAL_SOURCES = { 'claude-code': 'Claude Code', codex: 'Codex', 'deepseek-harness': 'DeepSeek Harness' };
const counters = ['input_tokens', 'output_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'reasoning_output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'];
const stages = ['turn', 'error', 'reasoning', 'todo_list', 'file_change', 'message', 'tool_loop'];
const tools = ['command_execution', 'web_search', 'mcp_tool_call', 'file_read', 'file_write', 'agent', 'tool_use'];
const terminal = ['completed', 'failed', 'cancelled', 'timed_out', 'output_limit'];
const integer = value => Number.isSafeInteger(value) && value >= 0;
const fields = (value, names) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === names.length && names.every(name => Object.hasOwn(value, name));

export function readExternalUsage(value, source) {
  if (!fields(value, ['source', 'status', 'tokens', 'amount']) || !Object.hasOwn(EXTERNAL_SOURCES, value.source)
      || source && value.source !== source || !['reported', 'partial', 'unknown'].includes(value.status)
      || value.amount !== null || !value.tokens || typeof value.tokens !== 'object' || Array.isArray(value.tokens)
      || Object.entries(value.tokens).some(([key, count]) => !counters.includes(key) || !integer(count))
      || value.status === 'reported' && !['input_tokens', 'output_tokens'].every(key => Object.hasOwn(value.tokens, key))
      || (value.status === 'unknown') !== (Object.keys(value.tokens).length === 0)) throw new Error('external_usage_invalid');
  return value;
}

export function readExternalProgress(value, source) {
  if (!fields(value, ['version', 'sequence', 'events', 'total', 'truncated', 'usage', 'terminal'])
      || value.version !== 'external-progress@1' || !integer(value.sequence) || !integer(value.truncated)
      || value.total !== null || value.terminal !== null && !terminal.includes(value.terminal)
      || !Array.isArray(value.events) || value.events.length > 128
      || value.truncated + value.events.length !== value.sequence) throw new Error('external_progress_invalid');
  value.events.forEach((event, index) => {
    if (!integer(event?.sequence) || event.sequence !== value.truncated + index + 1) throw new Error('external_progress_invalid');
    const valid = event.kind === 'started' && fields(event, ['sequence', 'kind'])
      || event.kind === 'step' && fields(event, ['sequence', 'kind', 'stage']) && stages.includes(event.stage)
      || event.kind === 'tool' && fields(event, ['sequence', 'kind', 'name']) && tools.includes(event.name)
      || event.kind === 'finished' && fields(event, ['sequence', 'kind', 'status']) && terminal.includes(event.status);
    if (!valid) throw new Error('external_progress_invalid');
  });
  readExternalUsage(value.usage, source);
  return value;
}

export function readExternalProgressFrame(value, state) {
  if (!fields(value, ['turn_id', 'project_id', 'kernel_turn_id', 'executor', 'progress'])
      || value.turn_id !== state.turnId || value.project_id !== state.project || state.turnIntent !== 'do'
      || typeof value.kernel_turn_id !== 'string' || !/^turn-[a-f0-9]{32}$/.test(value.kernel_turn_id)
      || !Object.hasOwn(EXTERNAL_SOURCES, value.executor)
      || state.externalKernel && state.externalKernel !== value.kernel_turn_id
      || state.externalExecutor && state.externalExecutor !== value.executor) throw new Error('external_progress_binding_invalid');
  readExternalProgress(value.progress, value.executor);
  if (state.externalSequence !== undefined && value.progress.sequence < state.externalSequence) throw new Error('external_progress_sequence_invalid');
  state.externalKernel = value.kernel_turn_id;
  state.externalExecutor = value.executor;
  state.externalSequence = value.progress.sequence;
  return value;
}

export function receiptExternalUsage(receipt) {
  if (receipt.external_usage) return readExternalUsage(receipt.external_usage, receipt.executor);
  const values = receipt.usage ?? receipt.model_usage ?? {};
  const tokens = Object.fromEntries(Object.entries(values).filter(([key, value]) => counters.includes(key) && integer(value)));
  return { source: receipt.executor, tokens, amount: null,
    status: Object.hasOwn(tokens, 'input_tokens') && Object.hasOwn(tokens, 'output_tokens') ? 'reported' : Object.keys(tokens).length ? 'partial' : 'unknown' };
}
