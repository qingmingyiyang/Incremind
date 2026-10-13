"""官方 runtime 0.1.5-rc.1 的有界 JSONRPC 会话；不使用 Python SDK 启动器。"""
import json

from .external_process import ProcessInputChannel


class HarnessProtocol:
    def __init__(self, *, session_id, task, cwd, model='deepseek-v4-flash', max_tokens=8192, secret_values=()):
        self.secret_values = tuple(secret_values)
        self.session_id, self.task, self.model = session_id, task, model
        self.channel = ProcessInputChannel()
        self.initial_input = self._request(1, 'initialize', {'cwd': str(cwd),
            'provider': 'deepseek-official', 'model': model, 'maxTokens': max_tokens})
        self.initialized = self.acknowledged = self.idle = self.shutdown = False
        self.finished = self.failed = False
        self.message_id = None
        self.inserted = set()
        self.final_text = ''
        self.turn = None
        self.sequence = -1
        self.reason = None
        self.usage = None
        self._observed_usage = {}
        self._usage_unknown = False

    @staticmethod
    def _request(identity, method, params=None):
        value = {'jsonrpc': '2.0', 'id': identity, 'method': method}
        if params is not None:
            value['params'] = params
        return json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n'

    def _reject(self):
        self.failed = True
        self.channel.close()
        raise ValueError('external_harness_protocol_invalid')

    def feed_line(self, line):
        try:
            frame = json.loads(line)
        except (ValueError, TypeError, RecursionError):
            return self._reject()
        if not isinstance(frame, dict) or frame.get('jsonrpc') != '2.0' or 'error' in frame:
            return self._reject()
        if 'id' in frame:
            identity = frame['id']
            if type(identity) is not int or not isinstance(frame.get('result'), dict):
                return self._reject()
            if identity == 1 and not self.initialized:
                if frame['result'].get('serverInfo') != {'name': 'deepseek-harness-sdk-runtime', 'version': '0.0.1'}:
                    return self._reject()
                self.initialized = True
                self.channel.send(self._request(2, 'session/prompt', {'sessionId': self.session_id,
                    'contentBlocks': [{'type': 'text', 'text': self.task}]}))
                return ({'kind': 'started'},)
            if identity == 2 and self.initialized and not self.acknowledged:
                identity = frame['result'].get('messageId')
                if not isinstance(identity, str) or not identity or len(identity) > 256:
                    return self._reject()
                self.acknowledged, self.message_id = True, identity
                self._try_shutdown()
                return ()
            if identity == 3 and self.shutdown and frame['result'] == {}:
                self.finished = True
                self.channel.close()
                from backend.shared.secret_detection import REDACTED_SECRET, redact_secrets
                from .external_events import _PATH
                text = self.final_text
                for secret in self.secret_values:
                    if secret:
                        text = text.replace(secret, REDACTED_SECRET)
                text = _PATH.sub('[REDACTED_PATH]', redact_secrets(text))
                return ({'kind': 'message', 'text': text},
                    {'kind': 'finished', 'status': 'completed'})
            return self._reject()
        params = frame.get('params')
        if not self.initialized or not isinstance(params, dict) or params.get('sessionId') != self.session_id:
            return self._reject()
        if frame.get('method') == 'session.status':
            if params.get('status') not in {'idle', 'running'}:
                return self._reject()
            self.idle = params['status'] == 'idle'
            self._try_shutdown()
            return ()
        if frame.get('method') != 'session.event' or not isinstance(params.get('event'), dict):
            return self._reject()
        event = params['event']
        sequence, data = event.get('seq'), event.get('data')
        if type(sequence) is not int or sequence != self.sequence + 1 or not isinstance(data, dict):
            return self._reject()
        self.sequence = sequence
        kind = event.get('type')
        if kind == 'agent/inbox/spliced':
            for message in data.get('inserted', []):
                if (isinstance(message, dict) and message.get('role') == 'user'
                        and message.get('content') == [{'type': 'text', 'text': self.task}]
                        and isinstance(message.get('id'), str)):
                    self.inserted.add(message['id'])
            if len(self.inserted) > 16:
                return self._reject()
        elif kind == 'turn/start':
            if self.turn is not None or type(data.get('turn')) is not int or data['turn'] < 1:
                return self._reject()
            self.turn = data['turn']
        elif kind == 'assistant/message':
            message = data.get('message')
            if (data.get('turn') != self.turn or not isinstance(message, dict)
                    or message.get('role') != 'assistant' or not isinstance(message.get('content'), list)):
                return self._reject()
            source = message.get('source', {})
            if source.get('kind') != 'model' or source.get('provider') != 'deepseek-official' or source.get('model') != self.model:
                return self._reject()
            self.final_text = ''.join(block['text'] for block in message['content']
                if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str))
            usage = data.get('usage')
            selected = {target: usage.get(key) for key, target in
                [('inputTokens', 'input_tokens'), ('outputTokens', 'output_tokens')]} if isinstance(usage, dict) else {}
            identity = (self.turn, data.get('step'), message.get('id'))
            if (type(data.get('step')) is not int or not isinstance(message.get('id'), str)
                    or not selected or any(type(value) is not int or value < 0 for value in selected.values())):
                self._usage_unknown = True
            elif identity in self._observed_usage and self._observed_usage[identity] != selected:
                return self._reject()
            else:
                self._observed_usage[identity] = selected
            self.usage = None if self._usage_unknown else {key: sum(row[key] for row in self._observed_usage.values())
                for key in ('input_tokens', 'output_tokens')}
        elif kind == 'turn/end':
            if data.get('turn') != self.turn or data.get('reason', {}).get('kind') != 'completed':
                return self._reject()
            self.reason = 'completed'
        self._try_shutdown()
        if kind == 'tool/call':
            return ({'kind': 'tool', 'name': 'mcp_tool_call'},)
        if kind in {'turn/start', 'step/start'}:
            return ({'kind': 'step', 'stage': 'turn' if kind == 'turn/start' else 'tool_loop'},)
        return ()

    def _try_shutdown(self):
        if (not self.shutdown and self.acknowledged and self.message_id in self.inserted
                and self.reason == 'completed' and self.final_text.strip() and self.idle):
            self.shutdown = True
            self.channel.send(self._request(3, 'shutdown'))
