"""Dedicated, one-wire OpenAI-compatible image-generation transport.

This module deliberately does not share the chat-completion parser. A
generated image stays ephemeral until controlled asset ingestion. Provider
URLs are rejected because dereferencing untrusted URLs needs DNS-rebinding
resistant connection pinning that this local-first runtime does not yet have.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from backend.shared.llm.base_url import resolve_openai_compatible_api_base_url
from core.model_gateway import ImageGenerationRequest, ImageGenerationResult


class ImageGenerationGatewayError(RuntimeError):
    """An image request failed before durable asset ingestion."""


class ImageGenerationEffectUnknownError(ImageGenerationGatewayError):
    """The provider may have accepted generation; recovery must reconcile."""

    provider_started = True


class EgressLease(Protocol):
    def finish(self, status: str, *, error_code: str | None = None) -> None: ...


EgressGuard = Callable[[str, tuple[str, ...], int], EgressLease]
_ALLOWED_MEDIA_TYPES: Final = frozenset({"image/png", "image/jpeg", "image/webp"})
_MAX_IMAGE_BYTES: Final = 10 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS: Final = 30.0
_RESERVED_PARAMETERS: Final = frozenset({"model", "prompt", "response_format"})


@dataclass(frozen=True, slots=True)
class ImageProviderRequest:
    prompt: str
    parameters: Mapping[str, object]
    privacy_scope: str
    input_image_bytes: bytes | None


ProviderCall = Callable[[ImageProviderRequest], Mapping[str, object]]


class _RejectRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


class ImageGenerationProviderAdapter:
    """One exact POST /images/generations invocation per generate call.

    ``provider_call`` is a focused test seam. Production supplies ``base_url``
    and uses this module's dedicated HTTP transport, never LiteLLM/chat parse.
    """

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        api_key_provider: Callable[[], str] | None = None,
        anonymous: bool = False,
        provider_call: ProviderCall | None = None,
        egress_guard: EgressGuard | None = None,
        egress_purpose: str = "image_generation",
        egress_categories: tuple[str, ...] = ("instructions",),
        max_image_bytes: int = _MAX_IMAGE_BYTES,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("image provider is required")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("image model is required")
        if provider_call is None:
            if not isinstance(base_url, str) or not base_url.strip():
                raise ValueError("image provider base_url is required")
            if api_key_provider is not None and api_key:
                raise ValueError("image provider API key sources conflict")
            if not anonymous and api_key_provider is None and (not isinstance(api_key, str) or not api_key.strip()):
                raise ValueError("image provider API key is required")
        elif not callable(provider_call):
            raise TypeError("provider_call must be callable")
        if not isinstance(max_image_bytes, int) or isinstance(max_image_bytes, bool) or max_image_bytes < 1:
            raise ValueError("max_image_bytes must be a positive integer")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not isinstance(egress_purpose, str) or not egress_purpose.strip():
            raise ValueError("image egress purpose is required")
        if not egress_categories or not all(isinstance(item, str) and item for item in egress_categories):
            raise ValueError("image egress categories are required")
        self._provider = provider.strip()
        self._model = model.strip()
        self._base_url = resolve_openai_compatible_api_base_url(base_url or "")
        normalized_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
        self._api_key_provider = api_key_provider or ((lambda: normalized_key) if normalized_key else None)
        self._anonymous = bool(anonymous)
        self._provider_call = provider_call
        self._egress_guard = egress_guard
        self._egress_purpose = egress_purpose.strip()
        self._egress_categories = tuple(egress_categories)
        self._max_image_bytes = max_image_bytes
        self._timeout_seconds = float(timeout_seconds)

    def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        _validate_request(request)
        # Edits need a separate multipart endpoint and receipt contract. Do
        # not smuggle arbitrary base64 into a generation POST.
        if request.input_image_b64 is not None:
            _decode_b64(request.input_image_b64, self._max_image_bytes, field="input_image_b64")
            raise ImageGenerationGatewayError("image edit input is not supported by this generation transport")
        normalized = ImageProviderRequest(
            prompt=request.prompt.strip(), parameters=_normalize_parameters(request.parameters),
            privacy_scope=request.privacy_scope, input_image_bytes=None,
        )
        payload_bytes = len(normalized.prompt.encode("utf-8")) + len(
            json.dumps(normalized.parameters, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        lease = self._egress_guard(
            self._egress_purpose, self._egress_categories, payload_bytes,
        ) if self._egress_guard is not None else None
        provider_started = False
        try:
            provider_started = True
            response = self._invoke_provider(normalized)
            image_bytes, media_type = _decode_provider_response(response, self._max_image_bytes)
        except ImageGenerationGatewayError as error:
            if lease is not None:
                lease.finish("unknown_effect" if provider_started else "not_sent", error_code="image_generation_failed")
            if provider_started and not isinstance(error, ImageGenerationEffectUnknownError):
                raise ImageGenerationEffectUnknownError(str(error)) from error
            raise
        except BaseException as error:
            if lease is not None:
                lease.finish("unknown_effect" if provider_started else "not_sent", error_code="image_generation_transport_failed")
            if provider_started:
                raise ImageGenerationEffectUnknownError("image provider effect is unknown") from error
            raise
        if lease is not None:
            lease.finish("succeeded")
        return ImageGenerationResult(
            capability="image_generation", image_bytes=image_bytes,
            media_type=media_type, provider=self._provider, model=self._model,
        )

    def _invoke_provider(self, request: ImageProviderRequest) -> Mapping[str, object]:
        if self._provider_call is not None:
            response = self._provider_call(request)
            if not isinstance(response, Mapping):
                raise ImageGenerationGatewayError("image provider returned an invalid response")
            return response
        return _post_openai_compatible_image_generation(
            base_url=self._base_url, api_key=self._wire_api_key(), anonymous=self._anonymous,
            model=self._model, request=request, timeout_seconds=self._timeout_seconds,
        )

    def _wire_api_key(self) -> str | None:
        if self._anonymous:
            return None
        if self._api_key_provider is None:
            raise ImageGenerationGatewayError("image provider secret injection is unavailable")
        value = self._api_key_provider()
        if not isinstance(value, str) or not value.strip():
            raise ImageGenerationGatewayError("image provider secret injection is unavailable")
        return value.strip()


def _post_openai_compatible_image_generation(
    *, base_url: str, api_key: str | None, anonymous: bool, model: str,
    request: ImageProviderRequest, timeout_seconds: float,
) -> Mapping[str, object]:
    if not base_url:
        raise ImageGenerationGatewayError("image provider endpoint is unavailable")
    body: dict[str, object] = {
        "model": model, "prompt": request.prompt, "response_format": "b64_json",
        **request.parameters,
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if not anonymous:
        if not api_key:
            raise ImageGenerationGatewayError("image provider API key is unavailable")
        headers["Authorization"] = f"Bearer {api_key}"
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        with build_opener(_RejectRedirectHandler()).open(
            Request(base_url.rstrip("/") + "/images/generations", data=encoded, headers=headers, method="POST"),
            timeout=timeout_seconds,
        ) as response:
            status = getattr(response, "status", response.getcode())
            if status != 200:
                raise ImageGenerationGatewayError("image provider returned a non-success status")
            raw = response.read()
    except HTTPError as error:
        raise ImageGenerationGatewayError(f"image provider returned HTTP {error.code}") from error
    except URLError as error:
        raise ImageGenerationGatewayError("image provider connection failed") from error
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ImageGenerationGatewayError("image provider returned invalid JSON") from error
    if not isinstance(decoded, Mapping):
        raise ImageGenerationGatewayError("image provider returned an invalid response")
    return decoded


def _validate_request(request: ImageGenerationRequest) -> None:
    if request.capability != "image_generation":
        raise ImageGenerationGatewayError("image generation capability is required")
    if not isinstance(request.prompt, str) or not request.prompt.strip():
        raise ImageGenerationGatewayError("image generation prompt is required")
    if not isinstance(request.parameters, Mapping):
        raise ImageGenerationGatewayError("image generation parameters are invalid")
    if request.privacy_scope not in {"remote_allowed", "local_only"}:
        raise ImageGenerationGatewayError("image generation privacy scope is invalid")
    if request.input_image_b64 is not None and not isinstance(request.input_image_b64, str):
        raise ImageGenerationGatewayError("input_image_b64 must be a string")


def _normalize_parameters(parameters: Mapping[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, value in parameters.items():
        if not isinstance(key, str) or not key or key in _RESERVED_PARAMETERS:
            raise ImageGenerationGatewayError("image generation parameters contain a reserved key")
        if value is None or isinstance(value, (str, int, float, bool)):
            normalized[key] = value
        else:
            raise ImageGenerationGatewayError("image generation parameters contain an unsupported value")
    return normalized


def _decode_provider_response(response: Mapping[str, object], max_image_bytes: int) -> tuple[bytes, str]:
    data = response.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], Mapping):
        raise ImageGenerationGatewayError("image provider must return exactly one data result")
    item = data[0]
    if "url" in item:
        raise ImageGenerationGatewayError("image provider URL output is unsupported")
    value = item.get("b64_json")
    if not isinstance(value, str) or not value:
        raise ImageGenerationGatewayError("image provider must return b64_json")
    media_type = _normalize_media_type(item.get("media_type", "image/png"))
    return _validate_image_bytes(_decode_b64(value, max_image_bytes, field="provider b64_json"), media_type, max_image_bytes)


def _decode_b64(value: str, max_bytes: int, *, field: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ImageGenerationGatewayError(f"{field} is not valid base64") from exc
    if not decoded or len(decoded) > max_bytes:
        raise ImageGenerationGatewayError(f"{field} exceeds the image byte limit")
    return decoded


def _normalize_media_type(value: object) -> str:
    if not isinstance(value, str):
        raise ImageGenerationGatewayError("image media type is required")
    media_type = value.split(";", 1)[0].strip().lower()
    if media_type not in _ALLOWED_MEDIA_TYPES:
        raise ImageGenerationGatewayError("image media type is not allowed")
    return media_type


def _validate_image_bytes(image_bytes: object, media_type: str, max_image_bytes: int) -> tuple[bytes, str]:
    if not isinstance(image_bytes, bytes) or not image_bytes or len(image_bytes) > max_image_bytes:
        raise ImageGenerationGatewayError("generated image exceeds the image byte limit")
    signatures = {"image/png": (b"\x89PNG\r\n\x1a\n",), "image/jpeg": (b"\xff\xd8\xff",), "image/webp": (b"RIFF",)}
    if not any(image_bytes.startswith(signature) for signature in signatures[media_type]):
        raise ImageGenerationGatewayError("image bytes do not match the declared media type")
    if media_type == "image/webp" and image_bytes[8:12] != b"WEBP":
        raise ImageGenerationGatewayError("image bytes do not match the declared media type")
    return image_bytes, media_type
