"""利用已验证的原文坐标限制单引号歧义，不猜测后续词语的词性。"""
import re
from bisect import bisect_right


MAX_CONTEXT_CHARS = 64000


def _ambiguous_sentence(boundaries, index):
    # 只有原文起点已知且没有活动引语时，歧义才不回溯污染前面的完整句。
    slot = bisect_right(boundaries, index)
    return boundaries[slot - 1], boundaries[slot]


def _inline_word_boundary(text, index):
    """只认同一行词后空白的局部形状，不把它当作词性或完整语义的证明。"""
    if (not index or text[index - 1] not in ' \t' or index + 1 == len(text)
            or not text[index + 1].isalnum()):
        return False
    left = index - 1
    while left >= 0 and text[left] in ' \t':
        left -= 1
    return left >= 0 and text[left].isalnum()


def _spans(text, *, known_start, quote_version=2):
    if type(quote_version) is not int or quote_version not in (2, 3):
        raise ValueError('unsupported_date_quote_version')
    spans, stack = [], []
    # 边界只扫描一次，避免单引号较多时反复扫描整段前缀。
    boundaries = [0, *(match.end() for match in re.finditer(r'[.!?。！？](?:\s+|$)|\n', text)), len(text)]
    # 新版的整句歧义不能由裸换行解除；旧版的边界与输出保持原样。
    sentences = ([0, *(match.end() for match in re.finditer(r'[.!?](?:\s+|$)|[。！？]\s*', text)), len(text)]
                 if quote_version == 3 else boundaries)
    pairs = {'"': '"', "'": "'", '“': '”', '‘': '’', '「': '」'}
    for index, character in enumerate(text):
        if character not in {*pairs, *pairs.values()}:
            continue
        if character in {"'", '’'} and index and index + 1 < len(text):
            if text[index - 1].isalnum() and text[index + 1].isalnum():
                continue
        if stack and character == stack[-1][1]:
            start, _, _ = stack.pop()
            spans.append((start, index + 1))
        elif known_start and not stack and character in {"'", '’'} and index and text[index - 1] in 'sS':
            # kids' 与 flowers' is 都有歧义；不判为所有格，也不把它当作跨句开引号。
            spans.append(_ambiguous_sentence(boundaries, index))
        elif character in pairs:
            stack.append((index, pairs[character], not stack))
        else:
            # 未见开引号的闭号可能来自截断历史引语，当前连续片段保持保守。
            spans.append((0, index + 1))
    for start, closing, without_outer in stack:
        left = 0 if closing in {'"', "'"} else start
        if (quote_version == 3 and known_start and without_outer and closing == "'"
                and _inline_word_boundary(text, start)):
            # 仅在限定的局部歧义下保留更早完整句；当前整句与后文仍未知。
            # 畸形原件自身遗漏开号时，同样的形状仍有语义歧义，不能称为已识别省略词。
            left, _ = _ambiguous_sentence(sentences, start)
        spans.append((left, len(text)))
    return spans


def _windows(candidate):
    """核对窗口文本和拼接位置，失败时不使用坐标作为可信起点。"""
    excerpt = candidate.get('excerpt', '')
    windows = candidate.get('windows') or ()
    pieces = excerpt.split('\n…\n')
    if not windows or len(windows) != len(pieces):
        return None
    result, position, previous_end = [], 0, -1
    for window, piece in zip(windows, pieces):
        if isinstance(window, dict):
            start, end, text = window.get('start'), window.get('end'), window.get('text', piece)
        else:
            start, end, text = window.start, window.end, window.text
        if (type(start) is not int or type(end) is not int or not 0 <= start < end
                or start < previous_end or end - start != len(piece) or text != piece):
            return None
        result.append((start, end, piece, position))
        position += len(piece) + len('\n…\n')
        previous_end = end
    return result


def quote_spans(candidate, *, quote_version=2):
    """返回摘录内受引语或歧义影响的位置，不改动原文或暴露未选内容。"""
    if type(quote_version) is not int or quote_version not in (2, 3):
        raise ValueError('unsupported_date_quote_version')
    excerpt = candidate.get('excerpt', '')
    windows = _windows(candidate)
    if candidate.get('coordinate_space') != 'source_content_v1':
        return _spans(excerpt, known_start=False, quote_version=quote_version)
    if windows is None:
        return [(0, len(excerpt))] if candidate.get('windows') else _spans(excerpt, known_start=False, quote_version=quote_version)
    entry = candidate.get('entry') or {}
    content = entry.get('content') if candidate.get('coordinate_space') == 'source_content_v1' else None
    if isinstance(content, str) and not all(content[start:end] == text for start, end, text, _ in windows):
        return [(0, len(excerpt))]
    if isinstance(content, str) and windows[-1][1] <= MAX_CONTEXT_CHARS:
        # 只利用未选原文的引号位置恢复状态；未选正文不作为日期、事实或外发材料。
        # 扫描有界；长原文优先用下面已选连续前缀，未知间隙保持拒算。
        spans = _spans(content[:windows[-1][1]], known_start=True, quote_version=quote_version)
        return [(offset + max(left, start) - start, offset + min(right, end) - start)
                for start, end, _, offset in windows for left, right in spans if left < end and right > start]
    # 缺少完整原文时，只能相信从偏移零开始的连续窗口；跳过的内容可能含开引号。
    result, index = [], 0
    while index < len(windows):
        first = index
        while index + 1 < len(windows) and windows[index][1] == windows[index + 1][0]:
            index += 1
        group = windows[first:index + 1]
        text = ''.join(window[2] for window in group)
        # 已知窗口从原文中间开始且不能恢复前缀时，未看到引号也不能证明处于引语外。
        spans = _spans(text, known_start=True, quote_version=quote_version) if group[0][0] == 0 else [(0, len(text))]
        for start, end, _, offset in group:
            left_in_group, right_in_group = start - group[0][0], end - group[0][0]
            result.extend((offset + max(left, left_in_group) - left_in_group,
                           offset + min(right, right_in_group) - left_in_group)
                          for left, right in spans if left < right_in_group and right > left_in_group)
        index += 1
    return result
