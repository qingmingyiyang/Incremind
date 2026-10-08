"""Plain text of the current, editable Document blocks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def document_block_text(document: Mapping[str, object]) -> str:
    """Read current block content, including edits, without loading old revisions."""
    blocks = document.get("blocks")
    if not isinstance(blocks, Sequence) or isinstance(blocks, (str, bytes)):
        return ""
    return "\n".join(
        content
        for block in blocks
        if isinstance(block, Mapping)
        if isinstance(content := block.get("content"), str) and content.strip()
    )
