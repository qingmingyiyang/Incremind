from __future__ import annotations

import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from backend.shared.llm.image_generation_gateway import (
    ImageGenerationEffectUnknownError,
    ImageGenerationGatewayError,
    ImageGenerationProviderAdapter,
)
from core.model_gateway import ImageGenerationRequest


PNG = b"\x89PNG\r\n\x1a\nexample"


def _request(**overrides: object) -> ImageGenerationRequest:
    payload: dict[str, object] = {
        "capability": "image_generation", "prompt": "a quiet mountain lake",
        "parameters": {"size": "1024x1024"}, "privacy_scope": "remote_allowed",
    }
    payload.update(overrides)
    return ImageGenerationRequest(**payload)  # type: ignore[arg-type]


def test_provider_b64_output_is_ephemeral_and_validated() -> None:
    calls = []
    adapter = ImageGenerationProviderAdapter(
        provider="image-provider", model="image-model",
        provider_call=lambda request: calls.append(request) or {
            "data": [{"b64_json": base64.b64encode(PNG).decode()}],
        },
    )
    result = adapter.generate(_request())
    assert result.image_bytes == PNG and result.media_type == "image/png"
    assert calls[0].input_image_bytes is None
    assert not hasattr(result, "prompt") and not hasattr(result, "url")


def test_actual_transport_posts_dedicated_images_api_once_without_chat_headers() -> None:
    seen: list[tuple[str, dict[str, str], dict[str, object]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, dict(self.headers), body))
            encoded = json.dumps({"data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        adapter = ImageGenerationProviderAdapter(
            provider="openai", model="image-model", base_url=f"http://127.0.0.1:{server.server_port}",
            anonymous=True,
        )
        assert adapter.generate(_request()).image_bytes == PNG
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    assert len(seen) == 1 and seen[0][0] == "/v1/images/generations"
    assert seen[0][1].get("Authorization") is None
    assert seen[0][2] == {"model": "image-model", "prompt": "a quiet mountain lake", "response_format": "b64_json", "size": "1024x1024"}


def test_remote_transport_adds_secret_only_when_not_anonymous() -> None:
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            seen.append(self.headers["Authorization"])
            encoded = json.dumps({"data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ImageGenerationProviderAdapter(provider="openai", model="image-model", base_url=f"http://127.0.0.1:{server.server_port}", api_key="secret").generate(_request())
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    assert seen == ["Bearer secret"]


def test_transport_does_not_follow_provider_redirects() -> None:
    paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            paths.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        adapter = ImageGenerationProviderAdapter(
            provider="openai", model="image-model", base_url=f"http://127.0.0.1:{server.server_port}", anonymous=True,
        )
        with pytest.raises(ImageGenerationEffectUnknownError):
            adapter.generate(_request())
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    assert paths == ["/v1/images/generations"]


def test_url_output_and_input_edit_fail_closed() -> None:
    adapter = ImageGenerationProviderAdapter(
        provider="image-provider", model="image-model", provider_call=lambda _request: {"data": [{"url": "https://example.invalid/image.png"}]},
    )
    with pytest.raises(ImageGenerationEffectUnknownError, match="URL output"):
        adapter.generate(_request())
    with pytest.raises(ImageGenerationGatewayError, match="edit input"):
        adapter.generate(_request(input_image_b64=base64.b64encode(PNG).decode()))


def test_provider_started_failure_is_explicitly_unknown() -> None:
    calls = 0

    def dropped(_request: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise ConnectionError("dropped")
    adapter = ImageGenerationProviderAdapter(provider="image-provider", model="image-model", provider_call=dropped)
    with pytest.raises(ImageGenerationEffectUnknownError) as raised:
        adapter.generate(_request())
    assert raised.value.provider_started is True and calls == 1


def test_invalid_parameters_and_b64_fail_before_provider() -> None:
    adapter = ImageGenerationProviderAdapter(
        provider="image-provider", model="image-model",
        provider_call=lambda _request: (_ for _ in ()).throw(AssertionError("must not call provider")),
    )
    with pytest.raises(ImageGenerationGatewayError, match="reserved"):
        adapter.generate(_request(parameters={"prompt": "overwrite"}))
    with pytest.raises(ImageGenerationGatewayError, match="not valid base64"):
        adapter.generate(_request(input_image_b64="not base64!"))
