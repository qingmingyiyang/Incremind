from __future__ import annotations

import pytest

from backend.security.network_adapter import (
    BoundedHttpResponse,
    LoopbackHttpConnectProxy,
    NetworkBoundaryError,
    PinnedHttpRequest,
    PinnedBinaryDownloadRequest,
    SafeBinaryDownloadAdapter,
    SafeJsonHttpAdapter,
    SafeTextNetworkAdapter,
    StreamedBinaryDownloadResponse,
    _perform_pinned_request,
    _open_loopback_connect_tunnel,
    _proxy_bounded_content_length,
)


PUBLIC_A = "93.184.216.34"
PUBLIC_B = "2606:2800:220:1:248:1893:25c8:1946"


@pytest.mark.parametrize("address", ["localhost", "example.com", "192.168.1.1", "8.8.8.8"])
def test_loopback_proxy_requires_literal_loopback_address(address: str) -> None:
    with pytest.raises(ValueError, match="loopback proxy address"):
        LoopbackHttpConnectProxy(address, 7890)


@pytest.mark.parametrize("port", [0, 65536, True])
def test_loopback_proxy_rejects_invalid_port(port) -> None:
    with pytest.raises(ValueError, match="proxy port"):
        LoopbackHttpConnectProxy("127.0.0.1", port)


def test_loopback_proxy_accepts_only_declared_length_or_chunked_bounded_framing() -> None:
    assert _proxy_bounded_content_length({"Content-Length": "5"}, max_response_bytes=10) == 5
    assert _proxy_bounded_content_length({"Transfer-Encoding": "chunked"}, max_response_bytes=10) is None
    with pytest.raises(NetworkBoundaryError, match="bounded framing"):
        _proxy_bounded_content_length({}, max_response_bytes=10)
    with pytest.raises(NetworkBoundaryError, match="bounded framing"):
        _proxy_bounded_content_length({"Transfer-Encoding": "gzip"}, max_response_bytes=10)


@pytest.mark.parametrize("content_type", ["application/zip", "application/x-zip-compressed"])
def test_binary_adapter_accepts_governed_archive_content_types(tmp_path, content_type) -> None:
    def transport(request):
        request.destination_part.write_bytes(b"PK fixture")
        return StreamedBinaryDownloadResponse(200, {"Content-Type": content_type}, 10)

    result = SafeBinaryDownloadAdapter(
        tmp_path, resolver=lambda _host, _port: [PUBLIC_A], transport=transport,
        allowed_hosts=("example.com",),
    ).download("https://example.com/archive.zip", relative_path="archive.zip", max_response_bytes=20)
    assert result.media_type == content_type


def test_loopback_connect_uses_numeric_destination_and_exact_proxy_peer(monkeypatch) -> None:
    class ProxySocket:
        def __init__(self, peer: str) -> None:
            self.peer = peer
            self.response = bytearray(b"HTTP/1.1 200 Connection established\r\nContent-Length: 0\r\n\r\n")
            self.sent = b""
            self.closed = False

        def getpeername(self):
            return self.peer, 7890

        def sendall(self, payload: bytes) -> None:
            self.sent += payload

        def recv(self, amount: int) -> bytes:
            assert amount == 1
            return bytes([self.response.pop(0)]) if self.response else b""

        def close(self) -> None:
            self.closed = True

    connection = ProxySocket("127.0.0.1")
    monkeypatch.setattr(
        "backend.security.network_adapter.socket.create_connection",
        lambda target, timeout: connection,
    )

    returned = _open_loopback_connect_tunnel(
        LoopbackHttpConnectProxy("127.0.0.1", 7890),
        destination_address=PUBLIC_A,
        destination_port=443,
        timeout_seconds=1,
    )

    assert returned is connection
    assert connection.sent.startswith(
        f"CONNECT {PUBLIC_A}:443 HTTP/1.1\r\nHost: {PUBLIC_A}:443\r\n".encode("ascii")
    )
    assert b"example.com" not in connection.sent


def test_loopback_connect_rejects_proxy_peer_drift(monkeypatch) -> None:
    connection = _FakeSocket("127.0.0.2")
    monkeypatch.setattr(
        "backend.security.network_adapter.socket.create_connection",
        lambda target, timeout: connection,
    )
    with pytest.raises(NetworkBoundaryError, match="peer does not match"):
        _open_loopback_connect_tunnel(
            LoopbackHttpConnectProxy("127.0.0.1", 7890),
            destination_address=PUBLIC_A,
            destination_port=443,
            timeout_seconds=1,
        )
    assert connection.closed is True


def _adapter(
    responses: list[BoundedHttpResponse],
    *,
    addresses: tuple[str, ...] = (PUBLIC_A,),
    requests: list[PinnedHttpRequest] | None = None,
    max_bytes: int = 32,
) -> SafeTextNetworkAdapter:
    recorded = requests if requests is not None else []

    def transport(request: PinnedHttpRequest) -> BoundedHttpResponse:
        recorded.append(request)
        return responses.pop(0)

    return SafeTextNetworkAdapter(
        resolver=lambda host, port: addresses,
        transport=transport,
        max_response_bytes=max_bytes,
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "https://user:secret@example.com/",
        "http://example.com:99999/",
        "",
    ],
)
def test_rejects_unsupported_or_credentialed_authority_before_transport(url: str) -> None:
    with pytest.raises(NetworkBoundaryError):
        _adapter([]).fetch_text(url)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "192.168.1.2",
        "::1",
        "fc00::1",
        "fe80::1",
        "0.0.0.0",
    ],
)
def test_rejects_every_non_public_ipv4_and_ipv6_address(address: str) -> None:
    with pytest.raises(NetworkBoundaryError, match="non-public"):
        _adapter([], addresses=(address,)).fetch_text("https://example.com/")


def test_rejects_mixed_public_and_private_dns_answers() -> None:
    with pytest.raises(NetworkBoundaryError, match="non-public"):
        _adapter([], addresses=(PUBLIC_A, "127.0.0.1")).fetch_text("https://example.com/")


def test_safe_json_http_posts_to_pinned_public_endpoint_without_redirect() -> None:
    requests: list[PinnedHttpRequest] = []

    def transport(request: PinnedHttpRequest) -> BoundedHttpResponse:
        requests.append(request)
        return BoundedHttpResponse(200, {"Content-Type": "application/json"}, b"{}")

    adapter = SafeJsonHttpAdapter(
        resolver=lambda _host, _port: (PUBLIC_A,), transport=transport,
    )
    response = adapter.request(
        "POST", "https://mcp.example.com/rpc",
        headers={"Content-Type": "application/json"}, body=b"{}",
        timeout_seconds=2, max_response_bytes=1024,
    )
    assert response.status == 200
    assert requests[0].method == "POST" and requests[0].body == b"{}"
    assert requests[0].addresses == (PUBLIC_A,)


def test_safe_json_http_rejects_private_dns_redirect_and_header_injection() -> None:
    with pytest.raises(NetworkBoundaryError, match="non-public"):
        SafeJsonHttpAdapter(resolver=lambda _host, _port: ("127.0.0.1",)).request(
            "POST", "https://mcp.example.com/rpc", headers={}, body=b"{}",
            timeout_seconds=2, max_response_bytes=1024,
        )
    adapter = SafeJsonHttpAdapter(
        resolver=lambda _host, _port: (PUBLIC_A,),
        transport=lambda _request: BoundedHttpResponse(302, {"Location": "https://other.example/rpc"}, b""),
    )
    with pytest.raises(NetworkBoundaryError, match="redirects"):
        adapter.request(
            "POST", "https://mcp.example.com/rpc", headers={}, body=b"{}",
            timeout_seconds=2, max_response_bytes=1024,
        )
    with pytest.raises(NetworkBoundaryError, match="header"):
        adapter.request(
            "POST", "https://mcp.example.com/rpc", headers={"X-Test": "bad\r\nvalue"}, body=b"{}",
            timeout_seconds=2, max_response_bytes=1024,
        )


def test_pins_validated_dns_answers_and_decodes_allowed_text() -> None:
    requests: list[PinnedHttpRequest] = []
    result = _adapter(
        [BoundedHttpResponse(200, {"Content-Type": "text/plain; charset=utf-8"}, "中文".encode())],
        addresses=(PUBLIC_A, PUBLIC_B),
        requests=requests,
    ).fetch_text("HTTPS://Example.COM:8443/path?q=1#fragment")

    assert result == "中文"
    assert requests[0].scheme == "https"
    assert requests[0].host == "example.com"
    assert requests[0].port == 8443
    assert requests[0].target == "/path?q=1"
    assert requests[0].addresses == (PUBLIC_A, PUBLIC_B)


def test_redirect_is_resolved_and_validated_again_per_hop() -> None:
    resolved: list[tuple[str, int]] = []
    requests: list[PinnedHttpRequest] = []
    responses = [
        BoundedHttpResponse(302, {"Location": "https://cdn.example.net/final"}, b""),
        BoundedHttpResponse(200, {"Content-Type": "text/html"}, b"<p>ok</p>"),
    ]

    def resolver(host: str, port: int) -> tuple[str, ...]:
        resolved.append((host, port))
        return (PUBLIC_A,) if host == "example.com" else (PUBLIC_B,)

    def transport(request: PinnedHttpRequest) -> BoundedHttpResponse:
        requests.append(request)
        return responses.pop(0)

    adapter = SafeTextNetworkAdapter(resolver=resolver, transport=transport)
    assert adapter.fetch_text("https://example.com/start") == "<p>ok</p>"
    assert resolved == [("example.com", 443), ("cdn.example.net", 443)]
    assert requests[1].addresses == (PUBLIC_B,)


def test_host_allowlist_applies_to_initial_url_and_every_redirect() -> None:
    calls = 0

    def transport(request: PinnedHttpRequest) -> BoundedHttpResponse:
        nonlocal calls
        calls += 1
        return BoundedHttpResponse(302, {"Location": "https://outside.example/final"}, b"")

    adapter = SafeTextNetworkAdapter(
        resolver=lambda host, port: (PUBLIC_A,),
        transport=transport,
        allowed_hosts=("api.bilibili.com",),
    )
    with pytest.raises(NetworkBoundaryError, match="allowed network scope"):
        adapter.fetch_text("https://api.bilibili.com/start")
    assert calls == 1
    with pytest.raises(NetworkBoundaryError, match="allowed network scope"):
        adapter.fetch_text("https://bilibili.com/start")
    assert calls == 1


@pytest.mark.parametrize("host", ["https://api.bilibili.com", "/bad", ""])
def test_host_allowlist_rejects_non_host_values(host: str) -> None:
    with pytest.raises(ValueError, match="allowed host"):
        SafeTextNetworkAdapter(allowed_hosts=(host,))


def test_redirect_to_private_address_is_rejected_before_second_transport() -> None:
    calls = 0

    def resolver(host: str, port: int) -> tuple[str, ...]:
        return (PUBLIC_A,) if host == "example.com" else ("127.0.0.1",)

    def transport(request: PinnedHttpRequest) -> BoundedHttpResponse:
        nonlocal calls
        calls += 1
        return BoundedHttpResponse(302, {"Location": "http://localhost/private"}, b"")

    with pytest.raises(NetworkBoundaryError, match="non-public"):
        SafeTextNetworkAdapter(resolver=resolver, transport=transport).fetch_text("https://example.com")
    assert calls == 1


def test_redirect_limit_is_bounded() -> None:
    responses = [BoundedHttpResponse(302, {"Location": "/again"}, b"") for _ in range(2)]
    adapter = SafeTextNetworkAdapter(
        resolver=lambda host, port: (PUBLIC_A,),
        transport=lambda request: responses.pop(0),
        max_redirects=1,
    )
    with pytest.raises(NetworkBoundaryError, match="redirect limit"):
        adapter.fetch_text("https://example.com")


@pytest.mark.parametrize("content_type", [None, "application/octet-stream", "image/svg+xml"])
def test_rejects_missing_or_non_text_mime(content_type: str | None) -> None:
    headers = {} if content_type is None else {"Content-Type": content_type}
    with pytest.raises(NetworkBoundaryError, match="content type"):
        _adapter([BoundedHttpResponse(200, headers, b"payload")]).fetch_text("https://example.com")


def test_rejects_response_body_over_adapter_budget_even_for_fake_transport() -> None:
    with pytest.raises(NetworkBoundaryError, match="byte limit"):
        _adapter(
            [BoundedHttpResponse(200, {"Content-Type": "text/plain"}, b"x" * 33)],
            max_bytes=32,
        ).fetch_text("https://example.com")


def test_rejects_invalid_declared_charset_without_replacement() -> None:
    response = BoundedHttpResponse(200, {"Content-Type": "text/plain; charset=ascii"}, "中文".encode())
    with pytest.raises(NetworkBoundaryError, match="invalid text encoding"):
        _adapter([response]).fetch_text("https://example.com")


class _FakeSocket:
    def __init__(self, peer: str) -> None:
        self.peer = peer
        self.closed = False

    def getpeername(self) -> tuple[str, int]:
        return self.peer, 80

    def sendall(self, payload: bytes) -> None:
        assert payload.startswith(b"GET / HTTP/1.1\r\n")

    def close(self) -> None:
        self.closed = True


def _pinned_request(*, max_bytes: int = 8) -> PinnedHttpRequest:
    return PinnedHttpRequest(
        scheme="http",
        host="example.com",
        port=80,
        target="/",
        addresses=(PUBLIC_A,),
        headers={},
        timeout_seconds=1,
        max_response_bytes=max_bytes,
    )


def test_default_transport_rejects_peer_not_in_pinned_dns(monkeypatch) -> None:
    connection = _FakeSocket(PUBLIC_B)
    monkeypatch.setattr("backend.security.network_adapter.socket.create_connection", lambda *args, **kwargs: connection)

    with pytest.raises(NetworkBoundaryError, match="peer does not match"):
        _perform_pinned_request(_pinned_request())

    assert connection.closed is True


@pytest.mark.parametrize("declared_length,body", [("9", b""), (None, b"123456789")])
def test_default_transport_enforces_declared_and_streamed_byte_limits(
    monkeypatch,
    declared_length: str | None,
    body: bytes,
) -> None:
    connection = _FakeSocket(PUBLIC_A)
    monkeypatch.setattr("backend.security.network_adapter.socket.create_connection", lambda *args, **kwargs: connection)

    class FakeHttpResponse:
        status = 200

        def __init__(self, sock) -> None:
            assert sock is connection

        def begin(self) -> None:
            return None

        def getheader(self, name: str) -> str | None:
            return declared_length if name == "Content-Length" else None

        def read(self, amount: int) -> bytes:
            assert amount == 9
            return body

        def getheaders(self):
            return [("Content-Type", "text/plain")]

    monkeypatch.setattr("backend.security.network_adapter.http.client.HTTPResponse", FakeHttpResponse)

    with pytest.raises(NetworkBoundaryError, match="byte limit"):
        _perform_pinned_request(_pinned_request())

    assert connection.closed is True


def test_binary_download_streams_into_owned_staging_and_returns_receipt(tmp_path) -> None:
    requests: list[PinnedBinaryDownloadRequest] = []
    controls = []

    def transport(request: PinnedBinaryDownloadRequest) -> StreamedBinaryDownloadResponse:
        requests.append(request)
        request.destination_part.write_bytes(b"audio")
        return StreamedBinaryDownloadResponse(
            200,
            {"Content-Type": "audio/mp4", "Content-Length": "5"},
            5,
        )

    result = SafeBinaryDownloadAdapter(
        tmp_path / "staging",
        resolver=lambda host, port: (PUBLIC_A,),
        transport=transport,
        allowed_host_suffixes=("bilivideo.com",),
    ).download(
        "https://upos-sz-mirrorcos.bilivideo.com/audio.m4s",
        relative_path="job-1/audio.m4s",
        max_response_bytes=8,
        headers={"Referer": "https://www.bilibili.com/video/BV1xx411c7mD/"},
        control_check=lambda: controls.append("checked"),
    )

    assert result.path == (tmp_path / "staging" / "job-1" / "audio.m4s").resolve()
    assert result.path.read_bytes() == b"audio"
    assert result.byte_count == 5 and result.media_type == "audio/mp4"
    assert requests[0].addresses == (PUBLIC_A,)
    assert requests[0].headers["Referer"].startswith("https://www.bilibili.com/")
    assert len(controls) == 2


def test_binary_download_accepts_raster_image_media_for_governed_staging(tmp_path) -> None:
    def transport(request: PinnedBinaryDownloadRequest) -> StreamedBinaryDownloadResponse:
        request.destination_part.write_bytes(b"image")
        assert request.headers["Accept"].startswith("image/*")
        return StreamedBinaryDownloadResponse(
            200,
            {"Content-Type": "image/webp", "Content-Length": "5"},
            5,
        )

    result = SafeBinaryDownloadAdapter(
        tmp_path / "staging",
        resolver=lambda host, port: (PUBLIC_A,),
        transport=transport,
        allowed_host_suffixes=("xhscdn.com",),
    ).download(
        "https://img.xhscdn.com/image.webp",
        relative_path="job-1/image.webp",
        max_response_bytes=8,
    )

    assert result.media_type == "image/webp"
    assert result.path.read_bytes() == b"image"


@pytest.mark.parametrize("relative_path", ["../escape.bin", "C:/escape.bin", ""])
def test_binary_download_rejects_destination_outside_staging(tmp_path, relative_path: str) -> None:
    adapter = SafeBinaryDownloadAdapter(
        tmp_path / "staging",
        resolver=lambda host, port: (PUBLIC_A,),
        transport=lambda request: pytest.fail("transport must not run"),
        allowed_hosts=("media.example.com",),
    )
    with pytest.raises(NetworkBoundaryError):
        adapter.download(
            "https://media.example.com/audio.bin",
            relative_path=relative_path,
            max_response_bytes=8,
        )


def test_binary_download_revalidates_redirect_host_and_removes_partial_file(tmp_path) -> None:
    calls = 0

    def transport(request: PinnedBinaryDownloadRequest) -> StreamedBinaryDownloadResponse:
        nonlocal calls
        calls += 1
        request.destination_part.write_bytes(b"partial")
        return StreamedBinaryDownloadResponse(
            302, {"Location": "https://evil.example/audio.bin"}, 0
        )

    staging = tmp_path / "staging"
    adapter = SafeBinaryDownloadAdapter(
        staging,
        resolver=lambda host, port: (PUBLIC_A,),
        transport=transport,
        allowed_host_suffixes=("bilivideo.com",),
    )
    with pytest.raises(NetworkBoundaryError, match="allowed network scope"):
        adapter.download(
            "https://cdn.bilivideo.com/audio.bin",
            relative_path="job/audio.bin",
            max_response_bytes=8,
        )
    assert calls == 1
    assert not any(staging.rglob("*.part"))


def test_binary_download_rejects_transport_receipt_over_budget_and_sensitive_headers(tmp_path) -> None:
    def transport(request: PinnedBinaryDownloadRequest) -> StreamedBinaryDownloadResponse:
        request.destination_part.write_bytes(b"123456789")
        return StreamedBinaryDownloadResponse(
            200, {"Content-Type": "application/octet-stream"}, 9
        )

    adapter = SafeBinaryDownloadAdapter(
        tmp_path / "staging",
        resolver=lambda host, port: (PUBLIC_A,),
        transport=transport,
        allowed_hosts=("media.example.com",),
    )
    with pytest.raises(NetworkBoundaryError, match="byte limit"):
        adapter.download(
            "https://media.example.com/audio.bin",
            relative_path="job/audio.bin",
            max_response_bytes=8,
        )
    with pytest.raises(NetworkBoundaryError, match="header"):
        adapter.download(
            "https://media.example.com/audio.bin",
            relative_path="job/audio.bin",
            max_response_bytes=8,
            headers={"Cookie": "secret"},
        )


def test_controlled_binary_download_materializes_cookie_once_only_at_pinned_transport(tmp_path) -> None:
    calls: list[PinnedBinaryDownloadRequest] = []
    cookie_calls = 0

    def transport(request: PinnedBinaryDownloadRequest) -> StreamedBinaryDownloadResponse:
        calls.append(request)
        request.destination_part.write_bytes(b"jpeg")
        return StreamedBinaryDownloadResponse(
            200, {"Content-Type": "image/jpeg"}, 4
        )

    def cookie_header_value() -> str:
        nonlocal cookie_calls
        cookie_calls += 1
        return "controlled-cookie-canary"

    adapter = SafeBinaryDownloadAdapter(
        tmp_path / "staging",
        resolver=lambda host, port: (PUBLIC_A,),
        transport=transport,
        allowed_hosts=("media.example.com",),
    )
    result = adapter._download_controlled(
        "https://media.example.com/image.jpg",
        relative_path="job/image.jpg",
        max_response_bytes=8,
        headers={"Referer": "https://www.xiaohongshu.com"},
        cookie_header_value=cookie_header_value,
    )

    assert result.path.read_bytes() == b"jpeg"
    assert cookie_calls == 1
    assert len(calls) == 1
    assert calls[0].headers["Cookie"] == "controlled-cookie-canary"
    assert "controlled-cookie-canary" not in repr(adapter)


def test_controlled_binary_download_does_not_follow_redirect_or_reuse_cookie(tmp_path) -> None:
    calls = 0
    cookie_calls = 0

    def transport(request: PinnedBinaryDownloadRequest) -> StreamedBinaryDownloadResponse:
        nonlocal calls
        calls += 1
        return StreamedBinaryDownloadResponse(
            302, {"Location": "https://media.example.com/second.jpg"}, 0
        )

    def cookie_header_value() -> str:
        nonlocal cookie_calls
        cookie_calls += 1
        return "controlled-cookie-canary"

    adapter = SafeBinaryDownloadAdapter(
        tmp_path / "staging",
        resolver=lambda host, port: (PUBLIC_A,),
        transport=transport,
        allowed_hosts=("media.example.com",),
    )
    with pytest.raises(NetworkBoundaryError, match="redirect limit"):
        adapter._download_controlled(
            "https://media.example.com/image.jpg",
            relative_path="job/image.jpg",
            max_response_bytes=8,
            headers={},
            cookie_header_value=cookie_header_value,
        )

    assert calls == 1
    assert cookie_calls == 1


def test_controlled_binary_download_validates_dns_before_materializing_cookie(tmp_path) -> None:
    cookie_calls = 0

    def cookie_header_value() -> str:
        nonlocal cookie_calls
        cookie_calls += 1
        return "controlled-cookie-canary"

    adapter = SafeBinaryDownloadAdapter(
        tmp_path / "staging",
        resolver=lambda host, port: ("127.0.0.1",),
        transport=lambda request: pytest.fail("transport must not run"),
        allowed_hosts=("media.example.com",),
    )
    with pytest.raises(NetworkBoundaryError, match="non-public address"):
        adapter._download_controlled(
            "https://media.example.com/image.jpg",
            relative_path="job/image.jpg",
            max_response_bytes=8,
            headers={},
            cookie_header_value=cookie_header_value,
        )

    assert cookie_calls == 0
    assert all("controlled-cookie-canary" not in repr(value) for value in vars(adapter).values())


def test_controlled_binary_download_callback_failure_never_reaches_transport(tmp_path) -> None:
    calls = 0

    def transport(request: PinnedBinaryDownloadRequest) -> StreamedBinaryDownloadResponse:
        nonlocal calls
        calls += 1
        pytest.fail("transport must not run")

    adapter = SafeBinaryDownloadAdapter(
        tmp_path / "staging",
        resolver=lambda host, port: (PUBLIC_A,),
        transport=transport,
        allowed_hosts=("media.example.com",),
    )
    with pytest.raises(RuntimeError, match="secret store unavailable"):
        adapter._download_controlled(
            "https://media.example.com/image.jpg",
            relative_path="job/image.jpg",
            max_response_bytes=8,
            headers=None,
            cookie_header_value=lambda: (_ for _ in ()).throw(RuntimeError("secret store unavailable")),
        )
    assert calls == 0
