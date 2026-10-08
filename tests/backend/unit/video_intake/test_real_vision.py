from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from backend.video_intake.vision import OpenAICompatibleVisionProvider, VisionSettings


class _Lease:
    def __init__(self) -> None:
        self.results: list[tuple[str, str | None]] = []

    def finish(self, status: str, *, error_code: str | None = None) -> None:
        self.results.append((status, error_code))


def _allow_egress(payload_bytes: int) -> _Lease:
    assert payload_bytes > 0
    return _Lease()


def _authorization_headers(endpoint: str) -> dict[str, str]:
    assert endpoint.startswith("https://vision.example")
    return {"Authorization": "Bearer secret"}


def test_real_provider_sends_only_keyframes_and_normalizes_result(tmp_path: Path) -> None:
    (tmp_path / "visual").mkdir()
    (tmp_path / "visual" / "frame.jpg").write_bytes(b"jpeg-bytes")

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8")
        assert "data:image/jpeg;base64" in body
        assert "附近转写" in body
        assert "完整视频" not in body
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"tables":[],"charts":[],"visual_claims":[{"claim":"界面显示流程图","evidence_text":"流程图","frame_id":"frame-0001","timestamp":"00:10","confidence":"high"}],"uncertainties":[]}'
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = VisionSettings(
        mode="real",
        provider="openai_compatible",
        base_url="https://vision.example/v1",
        model="vision-test",
        has_api_key=True,
    )
    provider = OpenAICompatibleVisionProvider(
        settings, client=client, egress_guard=_allow_egress,
        authorization_header_provider=_authorization_headers,
    )

    result = asyncio.run(
        provider.analyze(
            record_dir=tmp_path,
            frames=[{"frame_id": "frame-0001", "timestamp_text": "00:10", "file": "visual/frame.jpg"}],
            nearby_context={"frame-0001": "附近转写"},
        )
    )
    asyncio.run(client.aclose())

    assert result["mode"] == "real"
    assert result["is_mock"] is False
    assert result["analyzed_frame_count"] == 1
    assert result["visual_claims"][0]["frame_id"] == "frame-0001"
    assert "不发送完整视频" in result["privacy_notice"]


def test_real_provider_retries_once_then_returns_nonblocking_failure(tmp_path: Path) -> None:
    (tmp_path / "frame.jpg").write_bytes(b"jpeg-bytes")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="unavailable")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = VisionSettings(
        mode="real",
        base_url="https://vision.example",
        model="vision-test",
        has_api_key=True,
    )
    result = asyncio.run(
        OpenAICompatibleVisionProvider(
            settings, client=client, egress_guard=_allow_egress,
            authorization_header_provider=_authorization_headers,
        ).analyze(
            record_dir=tmp_path,
            frames=[{"frame_id": "frame-0001", "timestamp_text": "00:10", "file": "frame.jpg"}],
            nearby_context={},
        )
    )
    asyncio.run(client.aclose())

    assert calls == 2
    assert result["analyzed_frame_count"] == 0
    assert result["visual_claims"] == []
    assert any("未阻塞" in item for item in result["uncertainties"])


def test_real_provider_without_egress_policy_does_not_open_network(tmp_path: Path) -> None:
    (tmp_path / "frame.jpg").write_bytes(b"jpeg-bytes")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(
        OpenAICompatibleVisionProvider(
            VisionSettings(mode="real", base_url="https://vision.example", model="vision-test", has_api_key=True),
            client=client,
        ).analyze(
            record_dir=tmp_path,
            frames=[{"frame_id": "frame-0001", "file": "frame.jpg"}],
            nearby_context={},
        )
    )
    asyncio.run(client.aclose())

    assert calls == 0
    assert result["analyzed_frame_count"] == 0
    assert any("外发尚未授权" in item for item in result["uncertainties"])
