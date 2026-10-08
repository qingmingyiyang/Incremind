from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass


class OpenAICompatibleFourLayerProviderError(ValueError):
    """Raised when an OpenAI-compatible JSON Provider cannot return safe JSON."""


@dataclass(frozen=True, slots=True)
class OpenAICompatibleFourLayerProviderSettings:
    provider_name: str = "deepseek"
    endpoint_url: str = "https://api.deepseek.com/chat/completions"
    model: str = "deepseek-chat"
    timeout_seconds: float = 60.0
    max_tokens: int = 2048
    temperature: float = 0.0
    egress_purpose: str = "memory_candidate"


HttpPostJson = Callable[
    [str, Mapping[str, str], Mapping[str, object], float],
    Mapping[str, object],
]
EgressCompletion = Callable[[str, str | None], None]
EgressGuard = Callable[[str, tuple[str, ...], int], EgressCompletion]
AuthorizationHeaderProvider = Callable[[str], Mapping[str, str]]


class OpenAICompatibleFourLayerJsonProvider:
    """OpenAI-compatible JSON Provider for four-layer Memory Candidate generation.

    Authorization headers are materialized by the platform wire-boundary callback.
    This adapter cannot read Secret storage or process environment values.
    """

    def __init__(
        self,
        settings: OpenAICompatibleFourLayerProviderSettings | None = None,
        *,
        authorization_header_provider: AuthorizationHeaderProvider | None = None,
        http_post_json: HttpPostJson | None = None,
        egress_guard: EgressGuard | None = None,
    ) -> None:
        self._settings = settings or OpenAICompatibleFourLayerProviderSettings()
        self._authorization_header_provider = authorization_header_provider
        self._http_post_json = http_post_json or _urllib_post_json
        self._egress_guard = egress_guard

    @property
    def provider_name(self) -> str:
        return _required_text(self._settings.provider_name, "provider_name")

    def complete_json(self, *, system_prompt: str, user_payload: Mapping[str, object]) -> Mapping[str, object]:
        request = {
            "model": _required_text(self._settings.model, "model"),
            "messages": [
                {"role": "system", "content": _required_text(system_prompt, "system_prompt")},
                {
                    "role": "user",
                    "content": json.dumps(dict(user_payload), ensure_ascii=False, sort_keys=True),
                },
            ],
            "temperature": self._settings.temperature,
            "max_tokens": self._settings.max_tokens,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        if self._egress_guard is None:
            raise OpenAICompatibleFourLayerProviderError("provider egress policy is not configured")
        payload_bytes = len(json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        finish_egress = self._egress_guard(
            _required_text(self._settings.egress_purpose, "egress_purpose"),
            ("instructions", "source_excerpt"),
            payload_bytes,
        )
        endpoint_url = _required_text(self._settings.endpoint_url, "endpoint_url")
        headers = {
            **self._authorization_headers(endpoint_url),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            response = self._http_post_json(
                endpoint_url,
                headers,
                request,
                _positive_timeout(self._settings.timeout_seconds),
            )
        except Exception:
            finish_egress("failed", "provider_request_failed")
            raise
        finish_egress("succeeded", None)
        content = _strip_json_fence(_extract_message_content(response))
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise OpenAICompatibleFourLayerProviderError("provider returned invalid JSON content") from exc
        if not isinstance(parsed, Mapping):
            raise OpenAICompatibleFourLayerProviderError("provider JSON content must be an object")
        return dict(parsed)

    def _authorization_headers(self, endpoint_url: str) -> Mapping[str, str]:
        if self._authorization_header_provider is None:
            raise OpenAICompatibleFourLayerProviderError(
                "provider authorization injection is not configured"
            )
        headers = dict(self._authorization_header_provider(endpoint_url))
        authorization = headers.get("Authorization")
        if (
            not isinstance(authorization, str)
            or not authorization.strip()
            or any(char in authorization for char in "\r\n\x00")
        ):
            raise OpenAICompatibleFourLayerProviderError(
                "provider authorization injection is invalid"
            )
        return headers


def _urllib_post_json(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, object],
    timeout_seconds: float,
) -> Mapping[str, object]:
    request = urllib.request.Request(
        url,
        data=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
        headers=dict(headers),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            decoded = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise OpenAICompatibleFourLayerProviderError(f"provider request failed with status {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise OpenAICompatibleFourLayerProviderError("provider request failed") from exc
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise OpenAICompatibleFourLayerProviderError("provider response was not JSON") from exc
    if not isinstance(parsed, Mapping):
        raise OpenAICompatibleFourLayerProviderError("provider response must be an object")
    return dict(parsed)


def _extract_message_content(response: Mapping[str, object]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise OpenAICompatibleFourLayerProviderError("provider response missing choices")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise OpenAICompatibleFourLayerProviderError("provider response choice must be an object")
    message = first.get("message")
    if not isinstance(message, Mapping):
        raise OpenAICompatibleFourLayerProviderError("provider response missing message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        message_keys = ",".join(sorted(str(key) for key in message))[:240] or "none"
        finish_reason = first.get("finish_reason")
        finish_label = finish_reason if isinstance(finish_reason, str) and finish_reason else "missing"
        content_type = type(content).__name__
        raise OpenAICompatibleFourLayerProviderError(
            "provider response missing content "
            f"(content_type={content_type}; finish_reason={finish_label}; message_keys={message_keys})"
        )
    return content.strip()


def _strip_json_fence(content: str) -> str:
    stripped = content.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3 or lines[-1].strip() != "```":
        return stripped
    opening = lines[0].strip().lower()
    if opening not in {"```", "```json"}:
        return stripped
    return "\n".join(lines[1:-1]).strip()


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OpenAICompatibleFourLayerProviderError(f"{field_name} is required")
    return value.strip()


def _positive_timeout(value: float) -> float:
    if value <= 0:
        raise OpenAICompatibleFourLayerProviderError("provider timeout must be positive")
    return value
