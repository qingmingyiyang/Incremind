from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError
from pydantic_core import from_json


class StructuredResponseDecodeError(ValueError):
    """只携带固定解码分类，完成资格与未知用量分别表示。"""
    def __init__(self, code, message, *, completed_wire=False, usage=None):
        if code not in {'json_empty', 'json_object_required', 'json_invalid', 'schema_invalid'}:
            raise ValueError('invalid_response_decode_code')
        if type(completed_wire) is not bool:
            raise ValueError('invalid_response_decode_completion')
        if completed_wire:
            if (not isinstance(usage, dict) or set(usage) - {'input_tokens', 'output_tokens', 'total_tokens'}
                    or any(type(value) is not int or value < 0 for value in usage.values())):
                raise ValueError('invalid_response_decode_usage')
        elif usage is not None:
            raise ValueError('incomplete_response_decode_has_usage')
        super().__init__(message)
        self.code, self.completed_wire = code, completed_wire
        self.usage = dict(usage) if completed_wire else None


class PartialJSONField:
    """Emit stable decoded string prefixes; only final schema validation accepts output."""
    def __init__(self, field: str, *, observe=None):
        self.field, self.raw, self.emitted = field, "", ""
        self.observe = observe

    def feed(self, delta: str) -> str:
        self.raw += delta
        if self.observe is not None:
            self.observe({'raw': self.raw})
        start = self.raw.find("{")
        if start < 0:
            return ""
        try:
            payload = from_json(self.raw[start:], allow_partial="trailing-strings")
        except ValueError:
            return ""
        text = payload.get(self.field) if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.startswith(self.emitted):
            return ""
        delta, self.emitted = text[len(self.emitted):], text
        return delta

def validate_json_response(
    *,
    raw_text: str,
    response_model: type[BaseModel],
) -> BaseModel:
    payload = extract_json_document(raw_text, require_object=True)
    # Some providers (DeepSeek in JSON mode) echo the requested response_format as a
    # top-level "type": "json_object" key. It is never content; strict models reject it.
    if (isinstance(payload, dict) and payload.get("type") == "json_object"
            and "type" not in response_model.model_fields):
        payload = {key: value for key, value in payload.items() if key != "type"}
    return response_model.model_validate(payload)


def describe_validation_error(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return json.dumps(error.errors(include_url=False), ensure_ascii=False)
    return str(error)


def extract_json_document(raw_text: str, *, require_object: bool = False) -> object:
    text = raw_text.strip()
    if not text:
        raise StructuredResponseDecodeError("json_empty", "模型返回为空，无法解析 JSON。")

    direct_value = _try_json_load(text)
    if direct_value is not None:
        if require_object and not isinstance(direct_value, dict):
            raise StructuredResponseDecodeError("json_object_required", "模型返回的 JSON 顶层必须是对象。")
        return direct_value

    fenced_value = _extract_fenced_json(text, require_object=require_object)
    if fenced_value is not None:
        return fenced_value

    balanced_value = _extract_balanced_json(text, require_object=require_object)
    if balanced_value is not None:
        return balanced_value

    if require_object:
        raise StructuredResponseDecodeError("json_invalid", "模型返回中未找到可解析的 JSON 对象。")
    raise StructuredResponseDecodeError("json_invalid", "模型返回中未找到可解析的 JSON。")


def _try_json_load(candidate: str) -> object | None:
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _extract_fenced_json(text: str, *, require_object: bool = False) -> object | None:
    matches = re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    for match in matches:
        candidate = match.group(1).strip()
        loaded = _try_json_load(candidate)
        if loaded is not None:
            if require_object and not isinstance(loaded, dict):
                continue
            return loaded
    return None


def _extract_balanced_json(text: str, *, require_object: bool = False) -> object | None:
    opening_chars = "{" if require_object else "{["
    start_indexes = [index for index, char in enumerate(text) if char in opening_chars]
    for start_index in start_indexes:
        end_index = _find_balanced_end(text, start_index)
        if end_index is None:
            continue
        candidate = text[start_index : end_index + 1]
        loaded = _try_json_load(candidate)
        if loaded is not None:
            if require_object and not isinstance(loaded, dict):
                continue
            return loaded
    return None


def _find_balanced_end(text: str, start_index: int) -> int | None:
    opening = text[start_index]
    expected_closing = "}" if opening == "{" else "]"
    stack: list[str] = [opening]
    in_string = False
    escaped = False

    for index in range(start_index + 1, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char in "{[":
            stack.append(char)
            continue
        if char not in "}]":
            continue
        if not stack:
            return None
        current_open = stack.pop()
        if (current_open == "{" and char != "}") or (current_open == "[" and char != "]"):
            return None
        if not stack:
            if char != expected_closing:
                return None
            return index
    return None
