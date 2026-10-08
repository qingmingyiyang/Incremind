"""将外部 CLI JSONL 投影为不含运行环境的有限进度事件。"""
from __future__ import annotations

import json
import re

from backend.shared.secret_detection import REDACTED_SECRET, redact_secrets


_TOKENS = frozenset({'input_tokens', 'output_tokens', 'cached_input_tokens',
    'cache_write_input_tokens', 'reasoning_output_tokens', 'cache_read_input_tokens',
    'cache_creation_input_tokens'})
_PATH = re.compile(r"(?<![\w:])(?:[A-Za-z]:[\\/]|/)[^\s\"'<>]+")
_CLAUDE_TOOLS = {'Bash': 'command_execution', 'WebSearch': 'web_search',
    'WebFetch': 'web_search', 'Read': 'file_read', 'Write': 'file_write',
    'Edit': 'file_write', 'Glob': 'file_read', 'Grep': 'file_read',
    'Agent': 'agent', 'Task': 'agent'}


class ExternalEventParser:
    """只解析事件；总输入容量、进程退出与取消由运行器管理。"""

    def __init__(self, executor: str, *, secret_values=()):
        if executor not in {'codex', 'claude-code'}:
            raise ValueError('external_executor_unknown')
        secret_values = tuple(secret_values)
        if any(not isinstance(value, str) for value in secret_values):
            raise TypeError('external_secret_values_invalid')
        self.executor = executor
        self._secrets = tuple(sorted({value for value in secret_values if value}, key=len, reverse=True))
        self._usage = None
        self.failed = False
        self._finished = False
        self._texts = []
        self._streamed_ids = set()
        self._stream_id = None
        self._has_stream_text = False

    @property
    def usage(self) -> dict | None:
        return dict(self._usage) if self._usage is not None else None

    @property
    def finished(self) -> bool:
        return self._finished

    def feed_line(self, line: str) -> tuple[dict, ...]:
        if self._finished:
            return ()
        if not isinstance(line, str):
            raise TypeError('external_event_line_invalid')
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            return ()
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            return ()
        return self._codex(event) if self.executor == 'codex' else self._claude(event)

    def _finish(self, event, failed=False):
        self._finished = True
        self.failed = failed
        usage = event.get('usage')
        if isinstance(usage, dict) and {'input_tokens', 'output_tokens'} <= usage.keys():
            selected = {key: value for key, value in usage.items() if key in _TOKENS}
            if all(type(value) is int and value >= 0 for value in selected.values()):
                self._usage = selected
        # 未发布的所有文字一起脱敏，避免秘密跨 delta、消息或截断边界逃逸。
        text = ''.join(self._texts)
        for value in self._secrets:
            text = text.replace(value, REDACTED_SECRET)
        text = _PATH.sub('[REDACTED_PATH]', redact_secrets(text))
        self._texts.clear()
        self._streamed_ids.clear()
        self._stream_id = None
        messages = tuple({'kind': 'message', 'text': text[start:start + 4096]}
            for start in range(0, len(text), 4096))
        return (*messages, {'kind': 'finished', 'status': 'failed' if failed else 'completed'})

    def _codex(self, event):
        kind = event.get('type')
        if kind == 'thread.started':
            return ({'kind': 'started'},)
        if kind == 'turn.started':
            return ({'kind': 'step', 'stage': 'turn'},)
        if kind in {'turn.failed', 'error'}:
            return self._finish(event, True)
        if kind == 'turn.completed':
            return self._finish(event)
        item = event.get('item')
        if kind not in {'item.started', 'item.updated', 'item.completed'} or not isinstance(item, dict):
            return ()
        name = item.get('type')
        if not isinstance(name, str):
            return ()
        if name == 'agent_message' and kind == 'item.completed':
            if isinstance(item.get('text'), str):
                self._texts.append(item['text'])
        elif name in {'command_execution', 'web_search', 'mcp_tool_call'}:
            return ({'kind': 'tool', 'name': name},) if kind != 'item.updated' else ()
        elif name == 'error':
            # item.error 是非终态提示，不保留可能含路径或密钥的异常正文。
            return ({'kind': 'step', 'stage': 'error'},)
        elif name in {'reasoning', 'todo_list', 'file_change'}:
            return ({'kind': 'step', 'stage': name},)
        return ()

    def _tool(self, name):
        if not isinstance(name, str):
            return None
        summary = 'mcp_tool_call' if name.startswith('mcp__') else _CLAUDE_TOOLS.get(name, 'tool_use')
        return {'kind': 'tool', 'name': summary}

    def _claude(self, event):
        kind = event.get('type')
        if kind == 'system' and event.get('subtype') == 'init':
            return ({'kind': 'started'},)
        if kind == 'result':
            if 'is_error' in event and type(event['is_error']) is not bool:
                return ()
            if 'subtype' in event and (not isinstance(event['subtype'], str)
                    or not (event['subtype'] == 'success' or event['subtype'].startswith('error_'))):
                return ()
            failed = event.get('is_error') is True or (isinstance(event.get('subtype'), str)
                and event['subtype'].startswith('error'))
            if not self._texts and not failed and isinstance(event.get('result'), str):
                self._texts.append(event['result'])
            return self._finish(event, failed)
        if kind == 'assistant':
            message = event.get('message')
            if not isinstance(message, dict) or not isinstance(message.get('content'), list):
                return ()
            identity = message.get('id')
            streamed = (isinstance(identity, str) and identity in self._streamed_ids
                or identity is None and self._has_stream_text)
            output = []
            for block in message['content']:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'text' and isinstance(block.get('text'), str) and not streamed:
                    self._texts.append(block['text'])
                elif block.get('type') == 'tool_use':
                    tool = self._tool(block.get('name'))
                    if tool:
                        output.append(tool)
            return tuple(output)
        if kind != 'stream_event' or not isinstance(event.get('event'), dict):
            return ()
        stream = event['event']
        if stream.get('type') == 'message_start':
            message = stream.get('message')
            self._stream_id = message.get('id') if isinstance(message, dict) else None
            return ({'kind': 'step', 'stage': 'message'},)
        if stream.get('type') == 'content_block_delta':
            delta = stream.get('delta')
            if isinstance(delta, dict) and delta.get('type') == 'text_delta' and isinstance(delta.get('text'), str):
                self._texts.append(delta['text'])
                self._has_stream_text = True
                if isinstance(self._stream_id, str):
                    self._streamed_ids.add(self._stream_id)
        if stream.get('type') == 'content_block_start':
            block = stream.get('content_block')
            if isinstance(block, dict) and block.get('type') == 'tool_use':
                tool = self._tool(block.get('name'))
                return (tool,) if tool else ()
        return ()
