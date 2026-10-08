"""以原 Markdown 坐标应用成果补丁，保留标题和用户修改。"""
from difflib import SequenceMatcher
import re

from core.document_engine.markdown_sections import _HEADING, _lines
from .outcome_corrections import _paragraphs


class OutcomePatchError(ValueError):
    """交给主智能体重试或回退的纯结构错误。"""

    def __init__(self, code, operation_index=None):
        self.code, self.operation_index = code, operation_index
        super().__init__(code)


def _outline(markdown, *, heading_error='outline_heading_unsupported'):
    # 原扫描器处理围栏、缩进和精确字符坐标，追加空行只探测未闭围栏。
    if not list(_lines(markdown + '\n\n'))[-1][3]:
        raise OutcomePatchError('fence_unclosed')
    rows, stack, previous = [], [], None
    for start, end, line, visible in _lines(markdown):
        if visible:
            if (re.search(r'<\s*/?h[1-6](?:\s|>)', line, re.IGNORECASE)
                    or (re.fullmatch(r' {0,3}(?:=+|-+)[ \t]*', line)
                        and previous is not None and previous.strip()
                        and not previous.startswith(('    ', '\t'))
                        and not _HEADING.match(previous))):
                raise OutcomePatchError(heading_error)
            heading = _HEADING.match(line)
            if heading:
                level, title = len(heading[1]), heading[2]
                while stack and stack[-1]['level'] >= level:
                    stack.pop()
                path = (*stack[-1]['path'], title) if stack else (title,)
                row = {'start': start, 'end': end, 'level': level, 'path': path,
                       'raw': markdown[start:end]}
                rows.append(row)
                stack.append(row)
        previous = line if visible else None
    paths = [row['path'] for row in rows]
    if len(paths) != len(set(paths)):
        raise OutcomePatchError('path_ambiguous')
    for index, row in enumerate(rows):
        row['body_end'] = rows[index + 1]['start'] if index + 1 < len(rows) else len(markdown)
        row['subtree_end'] = next((later['start'] for later in rows[index + 1:]
                                  if later['level'] <= row['level']), len(markdown))
    return rows


def _tokens(markdown, rows):
    """按整稿顺序比较，标题路径区分不同小节中的同一句话。"""
    result = [('body', None, block) for block in _paragraphs(markdown[:rows[0]['start']]
              if rows else markdown)]
    for row in rows:
        result.append(('heading', row['path'], row['raw']))
        result.extend(('body', row['path'], block)
                      for block in _paragraphs(markdown[row['end']:row['body_end']]))
    return result


def _protected(current, birth, rows):
    old = _tokens(birth, _outline(birth))
    new = _tokens(current, rows)
    unchanged = set()
    for tag, _, _, start, end in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == 'equal':
            unchanged.update(range(start, end))
    changed = {(path, content) for index, (kind, path, content) in enumerate(new)
               if kind == 'body' and index not in unchanged}
    protected = {}
    for index, (kind, path, content) in enumerate(new):
        # 相同段落的新增副本无法与原副本区分，完整保留该小节中的所有副本。
        if kind == 'body' and (path, content) in changed:
            protected.setdefault(path, []).append(content)
    return protected


def _check_user_paragraphs(markdown, rows, protected):
    by_path = {row['path']: row for row in rows}
    for path, blocks in protected.items():
        if path is None:
            current = markdown[:rows[0]['start']] if rows else markdown
        else:
            row = by_path.get(path)
            if row is None:
                raise OutcomePatchError('user_edit_protected')
            current = markdown[row['end']:row['body_end']]
        proposed, position = _paragraphs(current), 0
        for block in blocks:
            found = next((index for index in range(position, len(proposed))
                          if proposed[index] == block or proposed[index].startswith(block + '\n')
                          or proposed[index].startswith(block + '\r\n')), None)
            if found is None:
                raise OutcomePatchError('user_edit_protected')
            position = found + 1


def _body(value):
    if not isinstance(value, str):
        raise OutcomePatchError('body_invalid')
    if _outline(value, heading_error='body_heading'):
        raise OutcomePatchError('body_heading')
    return value.strip('\r\n')


def _path(value):
    if (not isinstance(value, list) or not value
            or any(not isinstance(part, str) or not part or part != part.strip()
                   or '\n' in part or '\r' in part for part in value)):
        raise OutcomePatchError('path_invalid')
    return tuple(value)


def _target(rows, path):
    row = next((row for row in rows if row['path'] == path), None)
    if row is None:
        raise OutcomePatchError('path_missing')
    return row


def _replace_body(markdown, row, body, newline):
    previous = markdown[row['end']:row['body_end']]
    if not previous.strip():
        if not body:
            return markdown
        prefix = previous or (newline if row['raw'].endswith(('\r', '\n')) else '')
        if prefix and not prefix.endswith(('\r', '\n')):
            prefix += newline
        if not row['raw'].endswith(('\r', '\n')):
            prefix = newline + prefix
        suffix = (newline * 2 if row['body_end'] < len(markdown)
                  else newline if markdown.endswith(('\r', '\n')) else '')
        return markdown[:row['end']] + prefix + body + suffix + markdown[row['body_end']:]
    leading = re.match(r'^(?:[ \t]*\r?\n)*', previous)[0]
    trailing = re.search(r'(?:\r?\n[ \t]*)*$', previous)[0]
    if len(leading) + len(trailing) > len(previous):
        trailing = ''
    replacement = leading + body + trailing
    if body and not row['raw'].endswith(('\r', '\n')):
        replacement = newline + replacement
    return markdown[:row['end']] + replacement + markdown[row['body_end']:]


def _insert_heading(markdown, row, operation, newline):
    level, title = operation['level'], operation['title']
    if type(level) is not int or level not in (row['level'], row['level'] + 1) or level > 6:
        raise OutcomePatchError('level_invalid')
    if (not isinstance(title, str) or not title or title != title.strip()
            or '\r' in title or '\n' in title):
        raise OutcomePatchError('title_invalid')
    body = _body(operation['body'])
    path = (*row['path'][:-1], title) if level == row['level'] else (*row['path'], title)
    start = row['subtree_end']
    before, after = markdown[:start], markdown[start:]
    gap = '' if before.endswith(newline * 2) else newline if before.endswith(newline) else newline * 2
    inserted = '#' * level + ' ' + title + newline * 2 + body + newline
    if after:
        inserted += newline
    result = before + gap + inserted + after
    if path not in {item['path'] for item in _outline(result)}:
        raise OutcomePatchError('title_invalid')
    return result, path


def _check_old_outline(rows, originals):
    old_paths = {row['path'] for row in originals}
    actual = [(row['path'], row['level'], row['raw'].rstrip('\r\n'))
              for row in rows if row['path'] in old_paths]
    expected = [(row['path'], row['level'], row['raw'].rstrip('\r\n')) for row in originals]
    if actual != expected:
        raise OutcomePatchError('outline_changed')


def apply_patch(current_markdown, birth_ai_markdown, operations):
    """只返回新版正文和改动路径，不修改旧稿、不重试、不读写存储。"""
    if not isinstance(current_markdown, str) or not isinstance(birth_ai_markdown, str):
        raise OutcomePatchError('markdown_invalid')
    if not isinstance(operations, list):
        raise OutcomePatchError('operations_invalid')
    original_rows = _outline(current_markdown)
    protected = _protected(current_markdown, birth_ai_markdown, original_rows)
    markdown, changes = current_markdown, {}
    newline = '\r\n' if '\r\n' in current_markdown else '\n'
    for index, operation in enumerate(operations):
        try:
            if not isinstance(operation, dict):
                raise OutcomePatchError('operation_invalid')
            kind = operation.get('kind')
            fields = ({'kind', 'path', 'body'} if kind == 'update'
                      else {'kind', 'after_path', 'level', 'title', 'body'} if kind == 'add' else None)
            if fields is None or set(operation) != fields:
                raise OutcomePatchError('operation_invalid')
            rows = _outline(markdown)
            path = _path(operation['path' if kind == 'update' else 'after_path'])
            row = _target(rows, path)
            if kind == 'update':
                result = _replace_body(markdown, row, _body(operation['body']), newline)
                change = 'updated'
            else:
                result, path = _insert_heading(markdown, row, operation, newline)
                change = 'added'
            result_rows = _outline(result)
            _check_old_outline(result_rows, original_rows)
            _check_user_paragraphs(result, result_rows, protected)
            if result != markdown:
                if changes.get(path) != 'added':
                    changes[path] = change
                markdown = result
        except OutcomePatchError as error:
            error.operation_index = index
            raise
    return {'markdown': markdown, 'changes': [{'path': list(path), 'kind': kind}
            for path, kind in changes.items()]}
