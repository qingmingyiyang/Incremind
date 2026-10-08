"""响应副本的有界旁观器；失败只放弃录制，调用方保留原转发字节。"""
import json
import zlib

from backend.shared.secret_detection import REDACTED_SECRET, redact_secrets
from .policies import get


def _json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('ambiguous_json')
            result[key] = value
        return result

    def constant(value):
        raise ValueError('invalid_json_constant')

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def _events(text, maximum):
    normalized = text.replace('\r\n', '\n').replace('\r', '\n')
    frames = normalized.split('\n\n')
    # EOF 后的纯注释没有待派发事件；有 data/event 尾段时仍拒绝不完整帧。
    if any(line and not line.startswith(':') for line in frames[-1].split('\n')):
        raise ValueError('incomplete_sse_frame')
    events = []
    for frame in frames[:-1]:
        name, data = None, []
        for line in frame.split('\n'):
            if not line or line.startswith(':'):
                continue
            field, _, value = line.partition(':')
            value = value.removeprefix(' ')
            if field == 'event':
                if name is not None:
                    raise ValueError('ambiguous_sse_event')
                name = value
            elif field == 'data':
                data.append(value)
        if data:
            joined = '\n'.join(data)
            events.append((name, joined if joined == '[DONE]' else _json(joined)))
            if len(events) > maximum:
                raise ValueError('too_many_sse_events')
    return events


def _decoded_body(data, encoding, maximum):
    """只解码旁观副本；拒绝解压超限、残缺流和成员后的额外字节。"""
    if not isinstance(encoding, str):
        raise ValueError('invalid_content_encoding')
    encoding = encoding.strip().lower()
    if encoding in {'', 'identity'}:
        return data
    window = {'gzip': 16 + zlib.MAX_WBITS, 'deflate': zlib.MAX_WBITS}.get(encoding)
    if window is None:
        raise ValueError('unsupported_content_encoding')
    decoder = zlib.decompressobj(window)
    decoded = decoder.decompress(data, maximum + 1)
    if (len(decoded) > maximum or not decoder.eof
            or decoder.unused_data or decoder.unconsumed_tail):
        raise ValueError('incomplete_or_excess_compressed_body')
    return decoded


class ConversationRecorder:
    def __init__(self, protocol, payload, *, credentials=()):
        self._buffer = bytearray()
        self._history = None
        self._credentials = ()
        self._finished = False
        self._protocol = None
        self._policy = None
        try:
            self._policy = get('proxy_record', version='@1')
            if (not isinstance(credentials, (tuple, list)) or len(credentials) > self._policy.max_credentials
                    or any(not isinstance(value, str) or not value
                        or len(value) > self._policy.max_credential_characters for value in credentials)):
                return
            self._history = self._policy.request_history(protocol, payload)
            if self._history is not None:
                self._protocol = protocol
                # 只留当次字面值，不持有凭据对象、原请求对象或请求头。
                self._credentials = tuple(sorted(set(credentials), key=len, reverse=True))
        except Exception:
            self._discard()

    def _discard(self):
        self._buffer.clear()
        self._history = None
        self._credentials = ()

    def close(self):
        """显式取消或路由清理只丢弃观察内容，永不生成原件正文。"""
        self._finished = True
        self._discard()

    def feed(self, chunk):
        """旁观副本，不返回或改写任何转发字节。"""
        if self._finished or self._history is None:
            return
        try:
            if not isinstance(chunk, bytes) or len(self._buffer) + len(chunk) > self._policy.max_response_bytes:
                self._discard()
                return
            self._buffer.extend(chunk)
        except Exception:
            self._discard()

    def finish(self, *, status_code, content_type, content_encoding=''):
        """调用方确认上游完整结束后调用；取消路径不调用或以失败状态调用。"""
        if self._finished:
            return None
        self._finished = True
        try:
            if (self._history is None or type(status_code) is not int or not 200 <= status_code < 300
                    or not isinstance(content_type, str)):
                return None
            media = content_type.partition(';')[0].strip().lower()
            text = _decoded_body(self._buffer, content_encoding,
                self._policy.max_response_bytes).decode('utf-8-sig')
            if media == 'application/json':
                assistant = self._policy.json_answer(self._protocol, _json(text))
            elif media == 'text/event-stream':
                assistant = self._policy.stream_answer(self._protocol, _events(text, self._policy.max_events))
            else:
                return None
            if assistant is None:
                return None

            def redact(value):
                for credential in self._credentials:
                    value = value.replace(credential, REDACTED_SECRET)
                return redact_secrets(value)

            history = tuple((role, redact(value)) for role, value in self._history)
            result = self._policy.render(history, redact(assistant))
            return redact(result) if result is not None else None
        except Exception:
            return None
        finally:
            self._discard()
