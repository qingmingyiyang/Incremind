"""Shared evidence framing and source-coordinate-preserving token limits."""

from backend.shared.llm.message_metadata import (
    _estimate_input_tokens,
    _build_prompt_fallback_messages,
    _build_json_mode_messages,
)
from ..structured_generation import AskOutput
from core.search_and_recall.evidence_windows import EvidenceWindow, select_evidence_windows
from .policies import get

WINDOW_TOKENS = 1200
# Scan up to three windows at the estimator's three bytes per token;
# the exact token and coordinate checks below enforce the actual limit.
WINDOW_SCAN_CHARS = WINDOW_TOKENS * 3 * 3

_INSTRUCTION = (
    "只根据用户提供的资料回答。返回 JSON 对象：answer 为简洁中文答案，citations 为实际使用的资料编号整数数组。"
    "没有依据时明确说不知道，不编造来源。不要输出 Markdown 围栏。"
)


def ask_instruction(chosen):
    return get('compose')(_ask_instruction, chosen, operation='instruction')


def _ask_instruction(chosen, *, operation=None):
    search = get('search').instruction(chosen) if any(c.get('kind') == 'search' for c in chosen) else ''
    return _INSTRUCTION + search + getattr(get('rank'), 'instruction', lambda rows: '')(chosen) + ("用到书架资料时，在句中简短说明这是以前记过、已经淡忘的内容。" if any(c.get("bookshelf") for c in chosen) else "") + (
        "对于矛盾的认识，说明两方依据及分歧，不合并成同一个结论。"
        if any(c.get("link_kind") == "refutes" for c in chosen)
        else ""
    )


def source_texts(chosen):
    return get('compose')(_source_texts, chosen, operation='sources')


def _source_texts(chosen, *, operation=None):
    return [
        f"[{index}] {candidate.get('title', candidate['id'])}\n"
        + (candidate['href'] + '\n' if candidate.get('kind') == 'search' else '') + candidate['excerpt']
        for index, candidate in enumerate(chosen, 1)
    ]


def user_text(sources, question, history=""):
    context = "\n\n".join(sources)
    history_section = f"\n\n对话历史（仅帮助理解，不是证据）：\n{history}" if history else ""
    return f"资料：\n{context}{history_section}\n\n问题：{question}"


def input_tokens(chosen, question, *, reserve_refutes=False, history=""):
    return _estimate_input_tokens(
        [
            {"role": "system", "content": ask_instruction([*chosen, {"link_kind": "refutes"}] if reserve_refutes else chosen)},
            {"role": "user", "content": user_text(source_texts(chosen), question, history)},
        ]
    )


def structured_prompt_overhead():
    messages = [{"role": "user", "content": ""}]
    base = _estimate_input_tokens(messages)
    return (
        max(
            _estimate_input_tokens(
                _build_prompt_fallback_messages(messages=messages, response_model=AskOutput, validation_error=None)
            ),
            _estimate_input_tokens(_build_json_mode_messages(messages=messages, validation_error=None)),
        )
        - base
        + 1
    )  # Allow the estimator's rounding at a different message length.


def history_tokens(history):
    return input_tokens([], "", history=history) - input_tokens([], "")


def evidence_tokens(chosen):
    messages = [{"role": "system", "content": ask_instruction(chosen)}, {"role": "user", "content": user_text([], "")}]
    base = _estimate_input_tokens(messages)
    messages[-1]["content"] = user_text(source_texts(chosen), "")
    return _estimate_input_tokens(messages) - base


def text_tokens(text):
    return _estimate_input_tokens([{"role": "user", "content": text}]) - _estimate_input_tokens(
        [{"role": "user", "content": ""}]
    )


def trim_candidate(candidate, question, *, limit=WINDOW_TOKENS):
    """Recognitions are atomic; other windows retain exact original offsets."""
    if candidate["layer"] == "L3" or not candidate.get("windows"):
        return dict(candidate) if text_tokens(candidate["excerpt"]) <= limit else None
    windows = []
    for window in candidate["windows"]:
        if text_tokens(window.text) <= limit:
            windows.append(window)
            continue
        left, right, best = 1, len(window.text), None
        while left <= right:
            size = (left + right) // 2
            selected = select_evidence_windows(
                window.text, question, title=candidate.get("title", ""), max_chars=size, max_windows=1
            )
            if selected.windows and text_tokens(selected.excerpt) <= limit:
                best = selected.windows[0]
                left = size + 1
            else:
                right = size - 1
        if best is None:
            return None
        windows.append(EvidenceWindow(window.start + best.start, window.start + best.end, best.text))
    return {**candidate, "windows": tuple(windows), "excerpt": "\n…\n".join(window.text for window in windows)}
