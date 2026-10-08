"""Commit-guard secret shapes shared with external-content redaction.

Detection is exactly the original task_guard rule set. Redaction removes each
matched shape; a detected private-key header owns its matching PEM block, or
the rest of the input when that block is unterminated.
"""
from __future__ import annotations

import re


SECRET_PATTERNS = (
    re.compile(r"\bsk-(?!test-)[A-Za-z0-9_\-]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|access[_-]?token)\b\s*[:=]\s*[\"'][A-Za-z0-9_\-]{24,}[\"']"),
)
REDACTED_SECRET = "[REDACTED_SECRET]"


def _text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("secret detection requires text")
    return value


def contains_secret(value: str) -> bool:
    """Apply the same three searches as the commit guard, without exposing values."""
    return any(pattern.search(_text(value)) for pattern in SECRET_PATTERNS)


def redact_secrets(value: str) -> str:
    """Replace detected spans, retaining every byte outside them in the string."""
    text = _text(value)
    spans = []
    for index, pattern in enumerate(SECRET_PATTERNS):
        for match in pattern.finditer(text):
            end = match.end()
            if index == 1:
                closing = match.group().replace("BEGIN ", "END ", 1)
                position = text.find(closing, end)
                end = position + len(closing) if position >= 0 else len(text)
            spans.append((match.start(), end))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    pieces, previous = [], 0
    for start, end in merged:
        pieces.extend((text[previous:start], REDACTED_SECRET))
        previous = end
    pieces.append(text[previous:])
    return "".join(pieces)
