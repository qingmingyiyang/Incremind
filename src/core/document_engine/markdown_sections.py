"""Pure exact-coordinate Markdown sections shared by projections and readers."""
from __future__ import annotations

import re


_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_BULLET = re.compile(r"^[-*+][ \t]+(.+)$")


def _lines(markdown):
    """Retain original string indices and ignore headings inside code fences."""
    offset = 0
    fence = None
    for raw in markdown.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        match = _FENCE.match(line)
        visible = fence is None
        if fence is not None:
            if (match and match[1][0] == fence[0] and len(match[1]) >= len(fence)
                    and not match[2].strip()):
                fence = None
            visible = False
        elif match:
            fence = match[1]
            visible = False
        yield offset, offset + len(raw), line, visible
        offset += len(raw)


def _section(markdown, lines, title):
    start = None
    for offset, end, line, visible in lines:
        heading = _HEADING.match(line) if visible else None
        if heading is None:
            continue
        level = len(heading[1])
        if start is not None and level <= 2:
            return start, offset
        if level == 2 and heading[2] == title:
            start = end
    return (start, len(markdown)) if start is not None else None


def _trimmed_span(markdown, start, end):
    raw = markdown[start:end]
    text = raw.strip()
    if not text:
        return "", 0, 0
    start += len(raw) - len(raw.lstrip())
    return text, start, start + len(text)


def summary_of(markdown: str) -> tuple[str, int, int]:
    """Read explicit summary or the first legacy paragraph after the H1 title.

    Coordinates are half-open Unicode character indices in this exact Markdown
    string, not original-source coordinates. Missing or empty summaries use
    ("", 0, 0); an explicit empty summary never falls back to another section.
    """
    lines = list(_lines(markdown))
    section = _section(markdown, lines, "摘要")
    if section is not None:
        return _trimmed_span(markdown, *section)
    after_title = False
    start = end = None
    for offset, next_offset, line, visible in lines:
        heading = _HEADING.match(line) if visible else None
        if not after_title:
            if heading and len(heading[1]) == 1:
                after_title = True
            continue
        if not line.strip():
            if start is not None:
                break
            continue
        if not visible or heading or _BULLET.match(line):
            break
        if start is None:
            start = offset
        end = next_offset
    return _trimmed_span(markdown, start, end) if start is not None else ("", 0, 0)


