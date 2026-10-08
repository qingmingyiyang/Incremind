"""纯请求体投影；未知协议形状由调用方原样转发。"""
from copy import deepcopy


_FIELDS = {'chat_completions':'messages', 'messages':'messages', 'responses':'input'}


def _selection(protocol, payload):
    if not isinstance(protocol, str) or protocol not in _FIELDS or not isinstance(payload, dict):
        return None
    value = payload.get(_FIELDS[protocol])
    if protocol == 'responses' and isinstance(value, str):
        return (None, value) if value.strip() else None
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        return None
    for position in range(len(value) - 1, -1, -1):
        item = value[position]
        if item.get('role') != 'user':
            continue
        if protocol == 'responses' and item.get('type', 'message') != 'message':
            return None
        content = item.get('content')
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            block_type = 'input_text' if protocol == 'responses' else 'text'
            text = '\n'.join(block['text'] for block in content
                if isinstance(block, dict) and block.get('type') == block_type
                and isinstance(block.get('text'), str))
        else:
            return None
        # 最后用户条目无文本时不借用较早的问题，也不解析工具结果内部正文。
        return (position, text) if text.strip() else None
    return None


def last_user_text(protocol, payload) -> str | None:
    """只读最后用户条目的文本块，原输入不作修改。"""
    selected = _selection(protocol, payload)
    return selected[1] if selected is not None else None


def insert_context(protocol, payload, context) -> dict:
    """已标注上下文作为独立 user 条目，不提升为系统指令。"""
    selected = _selection(protocol, payload)
    if selected is None or not isinstance(context, str) or not context.strip():
        raise ValueError('external_proxy_body_unsupported')
    position, text = selected
    result = deepcopy(payload)
    field = _FIELDS[protocol]
    entry = {'role':'user', 'content':context}
    if position is None:
        result[field] = [entry, {'role':'user', 'content':text}]
    else:
        result[field].insert(position, entry)
    return result
