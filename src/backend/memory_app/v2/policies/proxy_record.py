"""代理录制的纯阈值、完整性判断和已脱敏会话渲染。"""
from dataclasses import dataclass
from backend.shared.secret_detection import REDACTED_SECRET


_PROTOCOLS = {'chat_completions', 'messages', 'responses'}
_ROLES = {'system', 'developer', 'user', 'assistant'}
_LABELS = {'system': '系统', 'developer': '开发者', 'user': '用户', 'assistant': '助手'}


def _tool(value):
    return any(value.get(key) not in (None, []) for key in ('tool_calls', 'function_call'))


def _index(value, maximum):
    return type(value) is int and 0 <= value < maximum


def _text(content, kinds, maximum, *, thinking=False):
    if isinstance(content, str):
        return content if len(content) <= maximum else None
    if not isinstance(content, list) or len(content) > 256:
        return None
    pieces = []
    length = 0
    for part in content:
        if not isinstance(part, dict):
            return None
        if thinking and part.get('type') in {'thinking', 'redacted_thinking'}:
            continue
        if part.get('type') not in kinds or not isinstance(part.get('text'), str):
            return None
        length += len(part['text'])
        if length > maximum:
            return None
        pieces.append(part['text'])
    return '\n'.join(pieces)


@dataclass(frozen=True)
class RecordingPolicy:
    max_response_bytes: int = 2 * 1024 * 1024
    max_history_bytes: int = 1024 * 1024
    max_messages: int = 256
    max_events: int = 8192
    max_blocks: int = 256
    max_record_characters: int = 60000
    max_credentials: int = 16
    max_credential_characters: int = 8192
    min_dialogue_characters: int = 80
    min_user_characters: int = 16
    min_assistant_characters: int = 16

    def __call__(self, history, assistant):
        return self.render(history, assistant)

    def request_history(self, protocol, payload):
        if protocol not in _PROTOCOLS or not isinstance(payload, dict):
            return None
        if protocol == 'responses' and any(payload.get(key) is not None
                for key in ('previous_response_id', 'conversation')):
            return None
        entries = payload.get('input' if protocol == 'responses' else 'messages')
        if protocol == 'responses' and isinstance(entries, str):
            entries = [{'role': 'user', 'content': entries}]
        if not isinstance(entries, list) or not entries or len(entries) > self.max_messages:
            return None
        history = []
        instruction = payload.get('system' if protocol == 'messages' else 'instructions')
        if instruction is not None and protocol != 'chat_completions':
            value = _text(instruction, {'text'}, self.max_history_bytes)
            if value is None:
                return None
            history.append(('system', value))
        size = 0
        for entry in entries:
            if (not isinstance(entry, dict) or entry.get('role') not in _ROLES or _tool(entry)
                    or entry.get('type', 'message') != 'message'
                    or entry.get('status', 'completed') != 'completed'):
                return None
            kinds = {'input_text', 'output_text'} if protocol == 'responses' else {'text'}
            value = _text(entry.get('content'), kinds, self.max_history_bytes,
                thinking=protocol == 'messages' and entry['role'] == 'assistant')
            if value is None or not value.strip():
                return None
            size += len(value.encode('utf-8'))
            if size > self.max_history_bytes:
                return None
            history.append((entry['role'], value))
        if len(history) > self.max_messages or history[-1][0] != 'user':
            return None
        if sum(len(value.encode('utf-8')) for _, value in history) > self.max_history_bytes:
            return None
        return tuple(history)

    def json_answer(self, protocol, value):
        if (not isinstance(value, dict) or value.get('error') is not None
                or value.get('status') not in {None, 'completed'}):
            return None
        if protocol == 'chat_completions':
            choices = value.get('choices')
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                return None
            choice = choices[0]
            if choice.get('finish_reason') != 'stop' or type(choice.get('index')) is not int or choice['index'] != 0:
                return None
            message = choice.get('message')
            if (not isinstance(message, dict) or message.get('role') != 'assistant'
                    or _tool(message) or message.get('refusal') is not None):
                return None
            answer = _text(message.get('content'), {'text'}, self.max_response_bytes)
        elif protocol == 'messages':
            if value.get('type') != 'message' or value.get('role') != 'assistant' or value.get('stop_reason') != 'end_turn':
                return None
            answer = _text(value.get('content'), {'text'}, self.max_response_bytes, thinking=True)
        elif protocol == 'responses':
            if value.get('status') != 'completed' or value.get('incomplete_details') is not None:
                return None
            output = value.get('output')
            if not isinstance(output, list) or len(output) > self.max_blocks:
                return None
            pieces = []
            for item in output:
                if not isinstance(item, dict):
                    return None
                if item.get('type') == 'reasoning':
                    continue
                if (item.get('type') != 'message' or item.get('role') != 'assistant'
                        or item.get('status') != 'completed' or _tool(item)):
                    return None
                text = _text(item.get('content'), {'output_text'}, self.max_response_bytes)
                if text is None:
                    return None
                pieces.append(text)
            answer = '\n'.join(pieces)
        else:
            return None
        return answer if answer is not None and answer.strip() else None

    def stream_answer(self, protocol, events):
        if len(events) > self.max_events:
            return None
        if protocol == 'chat_completions':
            return self._chat_stream(events)
        if protocol == 'messages':
            return self._messages_stream(events)
        if protocol == 'responses':
            return self._responses_stream(events)
        return None

    def _chat_stream(self, events):
        pieces, stopped, done, identity = [], False, False, None
        for name, value in events:
            if done:
                return None
            if value == '[DONE]':
                if not stopped:
                    return None
                done = True
                continue
            if not isinstance(value, dict) or value.get('error') is not None or name not in {None, 'message'}:
                return None
            if value.get('id') is not None:
                if not isinstance(value['id'], str) or not value['id'] or (identity is not None and value['id'] != identity):
                    return None
                identity = value['id']
            choices = value.get('choices')
            if choices == [] and stopped and isinstance(value.get('usage'), dict):
                continue
            if stopped or not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                return None
            choice = choices[0]
            delta = choice.get('delta')
            if (type(choice.get('index')) is not int or choice['index'] != 0 or not isinstance(delta, dict)
                    or _tool(delta) or delta.get('role', 'assistant') != 'assistant'
                    or delta.get('refusal') is not None):
                return None
            text = delta.get('content')
            if text is not None:
                if not isinstance(text, str):
                    return None
                pieces.append(text)
            reason = choice.get('finish_reason')
            if reason is not None:
                if reason != 'stop':
                    return None
                stopped = True
        answer = ''.join(pieces)
        return answer if done and answer.strip() else None

    def _messages_stream(self, events):
        started, ended, stopped, blocks = False, False, False, []
        for name, value in events:
            if not isinstance(value, dict) or (name is not None and name != value.get('type')):
                return None
            kind = value.get('type')
            if kind == 'ping':
                continue
            if ended or value.get('error') is not None:
                return None
            if kind == 'message_start':
                message = value.get('message')
                if (started or not isinstance(message, dict) or message.get('role') != 'assistant'
                        or message.get('type') != 'message' or message.get('content') != []
                        or message.get('stop_reason') is not None):
                    return None
                started = True
            elif not started:
                return None
            elif kind == 'content_block_start':
                index, block = value.get('index'), value.get('content_block')
                if (stopped or not _index(index, self.max_blocks) or index != len(blocks)
                        or not isinstance(block, dict) or block.get('type') not in {'text', 'thinking', 'redacted_thinking'}):
                    return None
                text = block.get('text', '')
                if not isinstance(text, str):
                    return None
                blocks.append({'kind': block['type'], 'pieces': [text] if block['type'] == 'text' else [], 'closed': False})
            elif kind == 'content_block_delta':
                index, delta = value.get('index'), value.get('delta')
                if (stopped or not _index(index, len(blocks)) or blocks[index]['closed'] or not isinstance(delta, dict)):
                    return None
                if blocks[index]['kind'] == 'text' and delta.get('type') == 'text_delta' and isinstance(delta.get('text'), str):
                    blocks[index]['pieces'].append(delta['text'])
                elif blocks[index]['kind'] == 'thinking' and delta.get('type') in {'thinking_delta', 'signature_delta'}:
                    continue
                else:
                    return None
            elif kind == 'content_block_stop':
                index = value.get('index')
                if stopped or not _index(index, len(blocks)) or blocks[index]['closed']:
                    return None
                blocks[index]['closed'] = True
            elif kind == 'message_delta':
                delta = value.get('delta')
                if not isinstance(delta, dict) or stopped:
                    return None
                reason = delta.get('stop_reason')
                if reason is not None:
                    if reason != 'end_turn' or any(not block['closed'] for block in blocks):
                        return None
                    stopped = True
            elif kind == 'message_stop':
                if not stopped or any(not block['closed'] for block in blocks):
                    return None
                ended = True
            else:
                return None
        answer = '\n'.join(''.join(block['pieces']) for block in blocks if block['kind'] == 'text')
        return answer if ended and answer.strip() else None

    def _responses_stream(self, events):
        final, identity, deltas, completed_text = None, None, {}, {}
        observed_items, completed_items, observed_parts, completed_parts = {}, {}, set(), {}
        done = False
        for name, value in events:
            if done:
                return None
            if value == '[DONE]':
                if final is None:
                    return None
                done = True
                continue
            if (final is not None or not isinstance(value, dict) or value.get('error') is not None
                    or (name is not None and name != value.get('type'))):
                return None
            kind = value.get('type')
            if kind in {'response.created', 'response.in_progress'}:
                current = value.get('response')
                if (not isinstance(current, dict) or not isinstance(current.get('id'), str)
                        or current.get('status') not in {'queued', 'in_progress'}
                        or (identity is not None and current['id'] != identity)
                        or not isinstance(current.get('output', []), list)
                        or len(current.get('output', [])) > self.max_blocks
                        or any(not isinstance(item, dict) or item.get('type') not in {'message', 'reasoning'}
                            for item in current.get('output', []))):
                    return None
                identity = current['id']
            elif kind in {'response.output_item.added', 'response.output_item.done'}:
                item = value.get('item')
                index = value.get('output_index')
                if (not _index(index, self.max_blocks) or not isinstance(item, dict)
                        or not isinstance(item.get('id'), str) or not item['id']
                        or item.get('type') not in {'message', 'reasoning'}
                        or (item['type'] == 'message' and item.get('role') != 'assistant')):
                    return None
                binding = item['id'], item['type']
                if index in observed_items and observed_items[index] != binding:
                    return None
                if kind.endswith('.added') and index in observed_items:
                    return None
                if index in completed_items:
                    return None
                observed_items[index] = binding
                if kind.endswith('.done'):
                    if item.get('status') != 'completed':
                        return None
                    completed_items[index] = item
            elif kind in {'response.content_part.added', 'response.content_part.done'}:
                part = value.get('part')
                output_index, content_index = value.get('output_index'), value.get('content_index')
                if (not _index(output_index, self.max_blocks) or not _index(content_index, self.max_blocks)
                        or not isinstance(value.get('item_id'), str) or not value['item_id']
                        or not isinstance(part, dict) or part.get('type') != 'output_text'
                        or not isinstance(part.get('text'), str)):
                    return None
                key = output_index, content_index, value['item_id']
                if key in completed_parts or (kind.endswith('.added') and key in observed_parts):
                    return None
                observed_parts.add(key)
                if kind.endswith('.done'):
                    completed_parts[key] = part['text']
            elif kind in {'response.output_text.delta', 'response.output_text.done'}:
                output_index, content_index = value.get('output_index'), value.get('content_index')
                if (not _index(output_index, self.max_blocks) or not _index(content_index, self.max_blocks)
                        or not isinstance(value.get('item_id'), str)):
                    return None
                key = output_index, content_index, value['item_id']
                text = value.get('delta' if kind.endswith('.delta') else 'text')
                if not isinstance(text, str) or key in completed_text:
                    return None
                if kind.endswith('.delta'):
                    deltas.setdefault(key, []).append(text)
                else:
                    if key in deltas and ''.join(deltas[key]) != text:
                        return None
                    completed_text[key] = text
            elif kind in {'response.reasoning_summary_part.added', 'response.reasoning_summary_part.done',
                    'response.reasoning_summary_text.delta', 'response.reasoning_summary_text.done',
                    'response.reasoning_text.delta', 'response.reasoning_text.done'}:
                continue
            elif kind == 'response.completed':
                final = value.get('response')
                answer = self.json_answer('responses', final)
                if answer is None or (identity is not None and final.get('id') != identity):
                    return None
                for index, binding in observed_items.items():
                    if index >= len(final['output']):
                        return None
                    item = final['output'][index]
                    if (item.get('id'), item.get('type')) != binding:
                        return None
                    completed = completed_items.get(index)
                    if completed is not None and completed.get('content') != item.get('content'):
                        return None
                for key in deltas.keys() | completed_text.keys() | observed_parts:
                    output_index, content_index, item_id = key
                    output = final['output']
                    if output_index >= len(output):
                        return None
                    item = output[output_index]
                    parts = item.get('content', [])
                    if item.get('id') != item_id or content_index >= len(parts):
                        return None
                    text = parts[content_index].get('text')
                    if (key in deltas and ''.join(deltas[key]) != text) or (
                            key in completed_text and completed_text[key] != text) or (
                            key in completed_parts and completed_parts[key] != text):
                        return None
            else:
                return None
        return self.json_answer('responses', final) if final is not None else None

    def render(self, history, assistant):
        if not isinstance(assistant, str) or not assistant.strip() or not history:
            return None
        count = lambda text: sum(not character.isspace() for character in text.replace(REDACTED_SECRET, ''))
        user_size = sum(count(text) for role, text in history if role == 'user')
        dialogue_size = sum(count(text) for role, text in history if role in {'user', 'assistant'}) + count(assistant)
        if (user_size < self.min_user_characters or count(assistant) < self.min_assistant_characters
                or dialogue_size < self.min_dialogue_characters):
            return None
        entries = (*history, ('assistant', assistant))
        result = '# 代理对话\n\n' + '\n\n'.join('## ' + _LABELS[role] + '\n' + text for role, text in entries)
        return result if len(result) <= self.max_record_characters else None


v1 = RecordingPolicy()
