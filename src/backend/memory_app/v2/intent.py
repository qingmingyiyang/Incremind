"""Pure parsing and intent selection for the workbench input."""

import re


_TAG = r"#(?P<project>[^\s/#]+)(?:/(?P<scene>[^\s/#]+))?"
_LEADING_TAG = re.compile(r"^[ \t]*" + _TAG + r"(?=[ \t]|\r?$)[ \t]*", re.MULTILINE)
_TRAILING_TAG = re.compile(r"[ \t]+" + _TAG + r"[ \t]*(?=\r?$)", re.MULTILINE)
_DO_PREFIXES = ("帮我", "请帮", "写", "整理一份", "做一份")
_ASK_PREFIXES = ("什么", "怎么", "为什么", "哪", "上次")


def parse_scope_tag(text: str) -> tuple[str | None, str | None, str]:
    """Extract a scope token at a line boundary, preserving the remaining text."""
    matches = [pattern.search(text) for pattern in (_LEADING_TAG, _TRAILING_TAG)]
    matches = [match for match in matches if match is not None]
    if not matches:
        return None, None, text.strip()
    match = min(matches, key=lambda candidate: candidate.start())
    cleaned = (text[:match.start()] + text[match.end():]).strip()
    return match.group("project"), match.group("scene"), cleaned


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
