"""Pure message framing and numeric metadata; no gateway or network dependency."""
from __future__ import annotations
import json
from math import ceil
from collections.abc import Mapping, Sequence
from typing import Any
from pydantic import BaseModel


def _dump_messages(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(message) for message in messages]


def _estimate_input_tokens(messages: Sequence[dict[str, Any]]) -> int:
    encoded = json.dumps(
        list(messages), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return max(1, ceil(len(encoded) / 3)) if encoded else 0


def _build_json_mode_messages(
    *,
    messages: Sequence[dict[str, Any]],
    validation_error: str | None,
) -> list[dict[str, Any]]:
    structured_messages = [
        {
            "role": "user",
            "content": "这是 JSON mode 请求。只输出一个 JSON 对象，不要输出 Markdown、解释、代码块或额外文本。",
        },
        *_dump_messages(messages),
    ]
    if validation_error:
        structured_messages.append(_build_validation_retry_message(validation_error))
    return structured_messages


def _build_prompt_fallback_messages(
    *,
    messages: Sequence[dict[str, Any]],
    response_model: type[BaseModel],
    validation_error: str | None,
) -> list[dict[str, Any]]:
    schema = response_model.model_json_schema()
    instruction = (
        "这是结构化输出请求。只输出一个 JSON 对象，不要输出 Markdown、解释、代码块、引用清单或额外文本。\n"
        f"JSON 对象必须匹配 {response_model.__name__} 的 JSON Schema：\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
    )
    structured_messages = [
        {
            "role": "user",
            "content": instruction,
        },
        *_dump_messages(messages),
    ]
    if validation_error:
        structured_messages.append(_build_validation_retry_message(validation_error))
    return structured_messages


def _build_validation_retry_message(validation_error: str) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            "上一轮结构化输出没有通过本地校验，请修正后重新输出。\n"
            f"校验错误：{validation_error}\n"
            "仍然只输出一个 JSON 对象，不要输出任何额外文本。"
        ),
    }


def _extract_normalized_usage(response: Any) -> dict[str, int]:
    """Normalize provider usage without retaining any provider response body."""
    usage = _lookup(response, "usage")
    if usage is None:
        return {}
    input_tokens = _first_nonnegative_int(usage, "input_tokens", "prompt_tokens")
    output_tokens = _first_nonnegative_int(usage, "output_tokens", "completion_tokens")
    total_tokens = _nonnegative_int(_lookup(usage, "total_tokens"))
    normalized: dict[str, int] = {}
    if input_tokens is not None:
        normalized["input_tokens"] = input_tokens
    if output_tokens is not None:
        normalized["output_tokens"] = output_tokens
    if total_tokens is not None:
        normalized["total_tokens"] = total_tokens
    elif input_tokens is not None and output_tokens is not None:
        normalized["total_tokens"] = input_tokens + output_tokens
    return normalized


def _first_nonnegative_int(source: Any, *keys: str) -> int | None:
    for key in keys:
        value = _nonnegative_int(_lookup(source, key))
        if value is not None:
            return value
    return None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _lookup(source: Any, key: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(key)
    return getattr(source, key, None)
