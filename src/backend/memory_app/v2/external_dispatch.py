"""连接原工具调用期限和 CLI 进程 owner；不授予执行或外发权限。"""
from collections import deque
from dataclasses import dataclass, field

from core.ai_kernel.dispatcher import ToolCancellationToken, ToolExecutionContext
from .external_adapters import LaunchPlan
from .external_events import ExternalEventParser
from .external_process import ProcessCleanupError, run_process


@dataclass(frozen=True)
class DispatchResult:
    status: str
    exit_code: int | None
    usage: dict | None
    tail: tuple[str, ...]
    events: tuple[dict, ...]
    output_bytes: int
    may_have_started: bool = field(default=False, compare=False, repr=False)


class _CancellationView:
    """原进程 owner 只需 is_set；读取同一个 Kernel 令牌，不创建第二令牌。"""
    def __init__(self, context):
        self.context = context

    def is_set(self):
        return self.context.cancel_requested


def dispatch_external(plan: LaunchPlan, context: ToolExecutionContext, *, environment,
        secret_values=(), on_started=None, event_sink=None, output_limit=1048576, on_owner=None):
    """合格宿主调用；返回前原 owner 已关闭整个进程树。

    event_sink 仅投影进度。调用方须依据最终返回值持久化运行与回执，
    并保证回调失败时不发布未提交的终态，避免把回调输入当作持久事实。
    """
    if (not isinstance(context, ToolExecutionContext)
            or not isinstance(context.cancellation, ToolCancellationToken)
            or type(context.timeout_ms) is not int or context.timeout_ms <= 0
            or type(context.attempt) is not int or context.attempt < 1
            or not isinstance(context.invocation_id, str) or not context.invocation_id):
        return DispatchResult('failed', None, None, ('external_dispatch_context_invalid',), (), 0)
    if context.cancel_requested:
        return DispatchResult('cancelled', None, None, (), (), 0)
    remaining = context.remaining_timeout_ms
    if remaining <= 0:
        return DispatchResult('timed_out', None, None, (), (), 0)
    if (not isinstance(plan, LaunchPlan) or plan.executor not in {'codex','claude-code'}
            or (event_sink is not None and not callable(event_sink))
            or (on_started is not None and not callable(on_started))):
        return DispatchResult('failed', None, None, ('external_dispatch_request_invalid',), (), 0)
    try:
        # 解析器和进程尾部共用同一份值，避免一次性迭代器被先消费。
        secret_values = tuple(secret_values)
        parser = ExternalEventParser(plan.executor, secret_values=secret_values)
    except (TypeError, ValueError):
        return DispatchResult('failed', None, None, ('external_dispatch_request_invalid',), (), 0)
    events = deque(maxlen=128)
    sink_failed = False

    def receive(line):
        nonlocal sink_failed
        for event in parser.feed_line(line):
            # CLI 成功只证明协议结束；最终状态须等待原进程 owner 清理返回。
            if event['kind'] == 'finished':
                continue
            events.append(dict(event))
            if event_sink is not None:
                try:
                    event_sink(dict(event))
                except Exception:
                    sink_failed = True
                    raise

    def started():
        context.mark_provider_started()
        if on_started is not None:
            on_started()

    # 在调用进程 owner 前重读剩余期限，准备工作也不能延长原 invocation。
    remaining = context.remaining_timeout_ms
    if remaining <= 0:
        return DispatchResult('timed_out', None, None, (), (), 0)
    try:
        process = run_process(plan.command, cwd=plan.cwd, environment=environment,
            input_text=plan.stdin_text or plan.input_text, timeout=min(1200, remaining / 1000),
            output_limit=output_limit, cancel=_CancellationView(context),
            on_line=receive, on_started=started, secret_values=secret_values, on_owner=on_owner)
    except ProcessCleanupError as error:
        process = error._cleanup_result
        error._dispatch_result = DispatchResult('failed', process.exit_code, parser.usage,
            process.tail, tuple(events), process.output_bytes, process.may_have_started)
        raise
    status, tail = process.status, process.tail
    if status == 'completed':
        if context.cancel_requested:
            status = 'cancelled'
        elif context.remaining_timeout_ms <= 0:
            status = 'timed_out'
        elif parser.failed:
            status, tail = 'failed', ('external_dispatch_protocol_failed',)
        elif not parser.finished:
            status, tail = 'failed', ('external_dispatch_terminal_missing',)
    terminal = {'kind':'finished','status':status}
    if event_sink is not None and not sink_failed:
        try:
            event_sink(dict(terminal))
        except Exception:
            # 此时 owner 已关闭；失败回调不重试，不将异常详情留在结果中。
            status, tail = 'failed', ('external_dispatch_callback_failed',)
            terminal = {'kind':'finished','status':status}
    events.append(terminal)
    return DispatchResult(status, process.exit_code, parser.usage, tail, tuple(events), process.output_bytes,
        process.may_have_started)
