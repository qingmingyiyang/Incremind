"""Pure parsing and intent selection for the workbench input."""

import json
import re


_TAG = r"#(?P<project>[^\s/#]+)(?:/(?P<scene>[^\s/#]+))?"
_LEADING_TAG = re.compile(r"^[ \t]*" + _TAG + r"(?=[ \t]|\r?$)[ \t]*", re.MULTILINE)
_TRAILING_TAG = re.compile(r"[ \t]+" + _TAG + r"[ \t]*(?=\r?$)", re.MULTILINE)
_FRAMED_TAG = re.compile(r"^[ \t]*#[ \t]+(?P<value>\[[^\r\n]*\])[ \t]*\r?$", re.MULTILINE)
_BARE_COMPONENT = re.compile(r"[^\s/#]+")
_DO_PREFIXES = ("帮我", "请帮", "写", "整理一份", "做一份")
_ASK_PREFIXES = ("什么", "怎么", "为什么", "哪", "上次")


def scope_tag_match(text: str):
    matches = [pattern.search(text) for pattern in (_LEADING_TAG, _TRAILING_TAG)]
    matches = [(match.start(), match.end(), match.group('project'), match.group('scene'), False)
               for match in matches if match is not None]
    # 扩展范围独占一行，使用标准 JSON 保留名称；旧裸标签不经过解码。
    for match in _FRAMED_TAG.finditer(text):
        try:
            value = json.loads(match.group('value'))
        except ValueError:
            continue
        if (not isinstance(value, list) or len(value) != 2
                or not isinstance(value[0], str) or not value[0].strip()
                or value[1] is not None and (not isinstance(value[1], str) or not value[1].strip())):
            continue
        matches.append((match.start(), match.end(), value[0], value[1], True))
    return min(matches, key=lambda candidate: candidate[0]) if matches else None


def format_scope_tag(project: str, scene: str | None = None) -> str:
    if (not isinstance(project, str) or not project.strip()
            or scene is not None and (not isinstance(scene, str) or not scene.strip())):
        raise ValueError('invalid_scope_name')
    if _BARE_COMPONENT.fullmatch(project) and (scene is None or _BARE_COMPONENT.fullmatch(scene)):
        return '#' + project + ('/' + scene if scene is not None else '')
    return '# ' + json.dumps([project, scene], ensure_ascii=False, separators=(',', ':'))


def parse_scope_tag(text: str) -> tuple[str | None, str | None, str]:
    match = scope_tag_match(text)
    if match is None:
        return None, None, text.strip()
    start, end, project, scene, _ = match
    return project, scene, (text[:start] + text[end:]).strip()


def route_intent(text: str, has_files: bool) -> str:
    """Select an intent using DESIGN's ordered rules after removing the scope."""
    _, _, cleaned = parse_scope_tag(text)
    if has_files or cleaned.lower().startswith(("https://", "http://")):
        return "remember"
    if cleaned.startswith("灵感"):
        return "inspiration"
    if cleaned.startswith(_DO_PREFIXES):
        return "do"
    if cleaned.endswith(("？", "?")) or cleaned.startswith(_ASK_PREFIXES):
        return "ask"
    return "remember"
