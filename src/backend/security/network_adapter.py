from __future__ import annotations

import http.client
import ipaddress
import os
import socket
import ssl
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit


class NetworkBoundaryError(ValueError):
    """Raised when a URL fetch crosses the local application's network boundary."""


@dataclass(frozen=True, slots=True)
class LoopbackHttpConnectProxy:
    """Explicit non-secret HTTPS CONNECT hop on this machine only."""

    address: str
    port: int

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.address)
        except ValueError as error:
            raise ValueError("loopback proxy address must be an IP literal") from error
        if not address.is_loopback:
            raise ValueError("loopback proxy address must be loopback")
        if not isinstance(self.port, int) or isinstance(self.port, bool) or not 1 <= self.port <= 65535:
            raise ValueError("loopback proxy port is invalid")
        object.__setattr__(self, "address", str(address))


@dataclass(frozen=True, slots=True)
class PinnedHttpRequest:
    scheme: str
    host: str
    port: int
    target: str
    addresses: tuple[str, ...]
    headers: Mapping[str, str]
    timeout_seconds: float
    max_response_bytes: int
    control_check: Callable[[], None] | None = None
    method: str = "GET"
    body: bytes | None = None


@dataclass(frozen=True, slots=True)
class BoundedHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    complete: bool = True


Resolver = Callable[[str, int], Sequence[str]]
Transport = Callable[[PinnedHttpRequest], BoundedHttpResponse]


@dataclass(frozen=True, slots=True)
class PinnedBinaryDownloadRequest:
    scheme: str
    host: str
    port: int
    target: str
    addresses: tuple[str, ...]
    headers: Mapping[str, str]
    timeout_seconds: float
    max_response_bytes: int
    destination_part: Path
    control_check: Callable[[], None] | None = None


@dataclass(frozen=True, slots=True)
class StreamedBinaryDownloadResponse:
    status: int
    headers: Mapping[str, str]
    bytes_written: int


@dataclass(frozen=True, slots=True)
class DownloadedBinary:
    path: Path
    byte_count: int
    media_type: str


BinaryDownloadTransport = Callable[[PinnedBinaryDownloadRequest], StreamedBinaryDownloadResponse]


class SafeTextNetworkAdapter:
    """Fetch public HTTP(S) text through a redirect- and byte-bounded boundary."""

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        transport: Transport | None = None,
        max_redirects: int = 4,
        max_response_bytes: int = 4 * 1024 * 1024,
        timeout_seconds: float = 20.0,
        allowed_hosts: Sequence[str] | None = None,
        control_check: Callable[[], None] | None = None,
        connect_proxy: LoopbackHttpConnectProxy | None = None,
        _request_headers: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        if max_redirects < 0 or max_response_bytes < 1 or timeout_seconds <= 0:
            raise ValueError("network adapter limits must be positive")
        self._resolver = resolver or _resolve_addresses
        if transport is not None and connect_proxy is not None:
            raise ValueError("custom transport and loopback proxy are mutually exclusive")
        self._transport = transport or (
            (lambda request: _perform_pinned_request_via_loopback_proxy(request, connect_proxy))
            if connect_proxy is not None else _perform_pinned_request
        )
        self._max_redirects = max_redirects
        self._max_response_bytes = max_response_bytes
        self._timeout_seconds = timeout_seconds
        self._allowed_hosts = _normalize_allowed_hosts(allowed_hosts)
        self._control_check = control_check
        self._request_headers = _request_headers

    def fetch_text(self, url: str) -> str:
        self._checkpoint()
        current = _normalize_url(url)
        for redirect_count in range(self._max_redirects + 1):
            self._checkpoint()
            parsed = urlsplit(current)
            host = parsed.hostname or ""
            if self._allowed_hosts and host not in self._allowed_hosts:
                raise NetworkBoundaryError("URL hostname is outside the allowed network scope")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            addresses = _validated_public_addresses(self._resolver(host, port))
            headers = {
                "Accept": "text/html, text/plain;q=0.9, application/json;q=0.7, application/xml;q=0.6",
                "User-Agent": "Chriptmas-OS-Local-WebContentReader/1.0",
            }
            if self._request_headers is not None:
                # This hook is deliberately constructor-private.  Public text
                # consumers never supply headers, particularly not cookies.
                for name, value in self._request_headers().items():
                    if (
                        not isinstance(name, str) or not isinstance(value, str)
                        or name.casefold() in {"host", "connection", "content-length"}
                        or any(char in name or char in value for char in "\r\n\x00")
                    ):
                        raise NetworkBoundaryError("text request header is invalid")
                    headers[name] = value
            request = PinnedHttpRequest(
                scheme=parsed.scheme,
                host=host,
                port=port,
                target=urlunsplit(("", "", parsed.path or "/", parsed.query, "")),
                addresses=addresses,
                headers=headers,
                timeout_seconds=self._timeout_seconds,
                max_response_bytes=self._max_response_bytes,
                control_check=self._control_check,
            )
            response = self._transport(request)
            self._checkpoint()
            if response.status in {301, 302, 303, 307, 308}:
                if redirect_count >= self._max_redirects:
                    raise NetworkBoundaryError("URL redirect limit exceeded")
                location = _header(response.headers, "location")
                if not location:
                    raise NetworkBoundaryError("URL redirect has no location")
                current = _normalize_url(urljoin(current, location))
                continue
            if response.status < 200 or response.status >= 300:
                raise NetworkBoundaryError(f"URL response status is not successful: {response.status}")
            media_type, charset = _validated_text_content_type(_header(response.headers, "content-type"))
            if len(response.body) > self._max_response_bytes:
                raise NetworkBoundaryError("URL response exceeds byte limit")
            try:
                return response.body.decode(charset, errors="strict")
            except (LookupError, UnicodeDecodeError) as error:
                raise NetworkBoundaryError(f"URL {media_type} response has invalid text encoding") from error
        raise NetworkBoundaryError("URL redirect limit exceeded")

    def _checkpoint(self) -> None:
        if self._control_check is not None:
            self._control_check()


class SafeJsonHttpAdapter:
    """Pinned, no-redirect JSON/SSE HTTP exchange for reviewed remote endpoints."""

    def __init__(self, *, resolver: Resolver | None = None, transport: Transport | None = None) -> None:
        self._resolver = resolver or _resolve_addresses
        self._transport = transport or _perform_pinned_request

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
        max_response_bytes: int,
        control_check: Callable[[], None] | None = None,
    ) -> BoundedHttpResponse:
        if method not in {"POST", "GET", "DELETE"} or timeout_seconds <= 0 or max_response_bytes < 1:
            raise NetworkBoundaryError("JSON HTTP request limits are invalid")
        normalized = _normalize_url(url)
        parsed = urlsplit(normalized)
        if parsed.scheme != "https":
            raise NetworkBoundaryError("JSON HTTP endpoint must use HTTPS")
        clean_headers: dict[str, str] = {}
        for name, value in headers.items():
            if (
                not isinstance(name, str) or not isinstance(value, str) or not name
                or name.casefold() in {"host", "connection", "content-length"}
                or any(char in name or char in value for char in "\r\n\x00")
            ):
                raise NetworkBoundaryError("JSON HTTP header is invalid")
            clean_headers[name] = value
        if body is not None and len(body) > 4 * 1024 * 1024:
            raise NetworkBoundaryError("JSON HTTP request exceeds byte limit")
        host = parsed.hostname or ""
        port = parsed.port or 443
        request = PinnedHttpRequest(
            scheme=parsed.scheme,
            host=host,
            port=port,
            target=urlunsplit(("", "", parsed.path or "/", parsed.query, "")),
            addresses=_validated_public_addresses(self._resolver(host, port)),
            headers=clean_headers,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            control_check=control_check,
            method=method,
            body=body,
        )
        response = self._transport(request)
        if response.status in {301, 302, 303, 307, 308}:
            raise NetworkBoundaryError("JSON HTTP redirects are forbidden")
        return response


class SafeBinaryDownloadAdapter:
    """Stream one public binary into an adapter-owned atomic staging root."""

    def __init__(
        self,
        staging_root: Path,
        *,
        resolver: Resolver | None = None,
        transport: BinaryDownloadTransport | None = None,
        max_redirects: int = 3,
        timeout_seconds: float = 20.0,
        allowed_hosts: Sequence[str] | None = None,
        allowed_host_suffixes: Sequence[str] | None = None,
        connect_proxy: LoopbackHttpConnectProxy | None = None,
    ) -> None:
        if max_redirects < 0 or timeout_seconds <= 0:
            raise ValueError("binary network adapter limits must be positive")
        self._root = Path(staging_root).resolve(strict=False)
        self._resolver = resolver or _resolve_addresses
        if transport is not None and connect_proxy is not None:
            raise ValueError("custom transport and loopback proxy are mutually exclusive")
        self._transport = transport or (
            (lambda request: _perform_pinned_binary_download_via_loopback_proxy(request, connect_proxy))
            if connect_proxy is not None else _perform_pinned_binary_download
        )
        self._max_redirects = max_redirects
        self._timeout_seconds = timeout_seconds
        self._allowed_hosts = _normalize_allowed_hosts(allowed_hosts)
        self._allowed_suffixes = _normalize_allowed_hosts(allowed_host_suffixes)

    def download(
        self,
        url: str,
        *,
        relative_path: str,
        max_response_bytes: int,
        headers: Mapping[str, str] | None = None,
        control_check: Callable[[], None] | None = None,
        timeout_seconds: float | None = None,
    ) -> DownloadedBinary:
        return self._download(
            url,
            relative_path=relative_path,
            max_response_bytes=max_response_bytes,
            headers=headers,
            control_check=control_check,
            timeout_seconds=timeout_seconds,
        )

    def _download_controlled(
        self,
        url: str,
        *,
        relative_path: str,
        max_response_bytes: int,
        headers: Mapping[str, str] | None,
        cookie_header_value: Callable[[], str],
        control_check: Callable[[], None] | None = None,
        timeout_seconds: float | None = None,
        wire_started: Callable[[], None] | None = None,
    ) -> DownloadedBinary:
        """Internal one-wire cookie path for an OS-owned credential lease.

        The public ``download`` API continues to reject caller-provided Cookie
        headers.  This private entry point materializes one Cookie locally
        immediately before constructing the pinned request, never stores it on
        the adapter, and forbids redirects so it cannot silently follow the
        credential to another authority.
        """

        if not callable(cookie_header_value):
            raise NetworkBoundaryError("controlled binary cookie callback is required")
        return self._download(
            url,
            relative_path=relative_path,
            max_response_bytes=max_response_bytes,
            headers=headers,
            control_check=control_check,
            timeout_seconds=timeout_seconds,
            cookie_header_value=cookie_header_value,
            wire_started=wire_started,
            max_redirects=0,
        )

    def _download(
        self,
        url: str,
        *,
        relative_path: str,
        max_response_bytes: int,
        headers: Mapping[str, str] | None,
        control_check: Callable[[], None] | None,
        timeout_seconds: float | None,
        cookie_header_value: Callable[[], str] | None = None,
        wire_started: Callable[[], None] | None = None,
        max_redirects: int | None = None,
    ) -> DownloadedBinary:
        if max_response_bytes < 1:
            raise ValueError("binary download byte limit must be positive")
        effective_timeout = self._timeout_seconds if timeout_seconds is None else min(
            self._timeout_seconds, timeout_seconds
        )
        if effective_timeout <= 0:
            raise ValueError("binary download timeout must be positive")
        destination = self._destination(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_name(f".{destination.name}.download.part")
        part.unlink(missing_ok=True)
        current = _normalize_url(url)
        try:
            effective_max_redirects = self._max_redirects if max_redirects is None else max_redirects
            for redirect_count in range(effective_max_redirects + 1):
                if control_check is not None:
                    control_check()
                parsed = urlsplit(current)
                host = parsed.hostname or ""
                if not self._host_allowed(host):
                    raise NetworkBoundaryError("binary URL hostname is outside the allowed network scope")
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                request_headers = _validated_binary_headers(headers)
                addresses = _validated_public_addresses(self._resolver(host, port))
                if cookie_header_value is not None:
                    # Keep materialization adjacent to the pinned request: a
                    # failing/invalid callback cannot reach the transport.
                    request_headers["Cookie"] = _validated_controlled_cookie_header_value(
                        cookie_header_value()
                    )
                request = PinnedBinaryDownloadRequest(
                    scheme=parsed.scheme,
                    host=host,
                    port=port,
                    target=urlunsplit(("", "", parsed.path or "/", parsed.query, "")),
                    addresses=addresses,
                    headers=request_headers,
                    timeout_seconds=effective_timeout,
                    max_response_bytes=max_response_bytes,
                    destination_part=part,
                    control_check=control_check,
                )
                if wire_started is not None:
                    wire_started()
                response = self._transport(request)
                if control_check is not None:
                    control_check()
                if response.status in {301, 302, 303, 307, 308}:
                    part.unlink(missing_ok=True)
                    if redirect_count >= effective_max_redirects:
                        raise NetworkBoundaryError("binary URL redirect limit exceeded")
                    location = _header(response.headers, "location")
                    if not location:
                        raise NetworkBoundaryError("binary URL redirect has no location")
                    current = _normalize_url(urljoin(current, location))
                    continue
                if response.status < 200 or response.status >= 300:
                    raise NetworkBoundaryError(
                        f"binary URL response status is not successful: {response.status}"
                    )
                if response.bytes_written < 1 or response.bytes_written > max_response_bytes:
                    raise NetworkBoundaryError("binary URL response exceeds byte limit")
                if not part.is_file() or part.stat().st_size != response.bytes_written:
                    raise NetworkBoundaryError("binary URL staged size does not match transport receipt")
                media_type = _validated_binary_content_type(
                    _header(response.headers, "content-type")
                )
                os.replace(part, destination)
                return DownloadedBinary(destination, response.bytes_written, media_type)
            raise NetworkBoundaryError("binary URL redirect limit exceeded")
        except Exception:
            part.unlink(missing_ok=True)
            raise

    def _destination(self, relative_path: str) -> Path:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise NetworkBoundaryError("binary download relative path is required")
        candidate = (self._root / relative_path).resolve(strict=False)
        if candidate == self._root or not candidate.is_relative_to(self._root):
            raise NetworkBoundaryError("binary download destination escaped staging root")
        return candidate

    def staged_path(self, relative_path: str) -> Path:
        """Resolve an adapter-owned staging path without performing network I/O."""

        candidate = self._destination(relative_path)
        if not candidate.is_file() or candidate.is_symlink():
            raise NetworkBoundaryError("binary staged output is unavailable")
        return candidate

    def _host_allowed(self, host: str) -> bool:
        return host in self._allowed_hosts or any(
            host != suffix and host.endswith(f".{suffix}") for suffix in self._allowed_suffixes
        )


def _normalize_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NetworkBoundaryError("URL is required")
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as error:
        raise NetworkBoundaryError("URL authority is invalid") from error
    if parsed.scheme.lower() not in {"http", "https"}:
        raise NetworkBoundaryError("URL scheme must be http or https")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise NetworkBoundaryError("URL authority must not contain credentials")
    if port is not None and not 1 <= port <= 65535:
        raise NetworkBoundaryError("URL port is invalid")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise NetworkBoundaryError("URL hostname is invalid") from error
    display_host = f"[{host}]" if ":" in host else host
    if port is not None:
        display_host = f"{display_host}:{port}"
    return urlunsplit((parsed.scheme.lower(), display_host, parsed.path or "/", parsed.query, ""))


def _normalize_allowed_hosts(values: Sequence[str] | None) -> frozenset[str]:
    if values is None:
        return frozenset()
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip() or "://" in value or "/" in value:
            raise ValueError("network adapter allowed host is invalid")
        try:
            host = value.strip().encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError as error:
            raise ValueError("network adapter allowed host is invalid") from error
        if not host or host.startswith("."):
            raise ValueError("network adapter allowed host is invalid")
        normalized.add(host)
    return frozenset(normalized)


def _resolve_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise NetworkBoundaryError("URL hostname could not be resolved") from error
    return tuple(dict.fromkeys(info[4][0] for info in infos))


def _validated_public_addresses(values: Sequence[str]) -> tuple[str, ...]:
    if not values:
        raise NetworkBoundaryError("URL hostname resolved to no addresses")
    normalized: list[str] = []
    for value in values:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as error:
            raise NetworkBoundaryError("URL hostname resolved to an invalid address") from error
        if not address.is_global:
            raise NetworkBoundaryError("URL hostname resolved to a non-public address")
        canonical = str(address)
        if canonical not in normalized:
            normalized.append(canonical)
    return tuple(normalized)


def _perform_pinned_request(request: PinnedHttpRequest) -> BoundedHttpResponse:
    last_error: OSError | ssl.SSLError | None = None
    for address in request.addresses:
        raw_socket: socket.socket | None = None
        connection: socket.socket | ssl.SSLSocket | None = None
        request_sent = False
        response: http.client.HTTPResponse | None = None
        body_parts: list[bytes] = []
        try:
            if request.control_check is not None:
                request.control_check()
            raw_socket = socket.create_connection((address, request.port), timeout=request.timeout_seconds)
            connection = raw_socket
            if request.scheme == "https":
                connection = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=request.host)
            peer = str(ipaddress.ip_address(connection.getpeername()[0]))
            if peer not in request.addresses or not ipaddress.ip_address(peer).is_global:
                raise NetworkBoundaryError("URL connection peer does not match validated DNS")
            host_header = f"[{request.host}]" if ":" in request.host else request.host
            default_port = 443 if request.scheme == "https" else 80
            if request.port != default_port:
                host_header = f"{host_header}:{request.port}"
            headers = {**request.headers, "Host": host_header, "Connection": "close"}
            if request.method not in {"GET", "POST", "DELETE"}:
                raise NetworkBoundaryError("URL request method is invalid")
            if request.body is not None:
                headers["Content-Length"] = str(len(request.body))
            payload = request.method + " " + request.target + " HTTP/1.1\r\n"
            payload += "".join(f"{name}: {value}\r\n" for name, value in headers.items()) + "\r\n"
            request_sent = True
            connection.sendall(payload.encode("ascii"))
            if request.body is not None:
                connection.sendall(request.body)
            response = http.client.HTTPResponse(connection)
            response.begin()
            if request.control_check is not None:
                request.control_check()
            content_length = response.getheader("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError as error:
                    raise NetworkBoundaryError("URL response has invalid content length") from error
                if declared_length < 0:
                    raise NetworkBoundaryError("URL response has invalid content length")
                if declared_length > request.max_response_bytes:
                    raise NetworkBoundaryError("URL response exceeds byte limit")
            body_size = 0
            while body_size <= request.max_response_bytes:
                if request.control_check is not None:
                    request.control_check()
                chunk = response.read(min(64 * 1024, request.max_response_bytes + 1 - body_size))
                if not chunk:
                    break
                body_parts.append(chunk)
                body_size += len(chunk)
            body = b"".join(body_parts)
            if len(body) > request.max_response_bytes:
                raise NetworkBoundaryError("URL response exceeds byte limit")
            return BoundedHttpResponse(response.status, dict(response.getheaders()), body)
        except NetworkBoundaryError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            partial = b"".join(body_parts)
            if request_sent and response is not None and partial and _is_sse_headers(dict(response.getheaders())):
                return BoundedHttpResponse(response.status, dict(response.getheaders()), partial, complete=False)
            if request_sent:
                raise NetworkBoundaryError("URL response stream failed") from error
            last_error = error
        finally:
            if connection is not None:
                connection.close()
            elif raw_socket is not None:
                raw_socket.close()
    raise NetworkBoundaryError("URL connection failed") from last_error


def _perform_pinned_request_via_loopback_proxy(
    request: PinnedHttpRequest, proxy: LoopbackHttpConnectProxy
) -> BoundedHttpResponse:
    if request.scheme != "https":
        raise NetworkBoundaryError("loopback proxy permits HTTPS destinations only")
    last_error: OSError | ssl.SSLError | http.client.HTTPException | None = None
    for address in request.addresses:
        connection: ssl.SSLSocket | None = None
        raw_socket: socket.socket | None = None
        request_sent = False
        response: http.client.HTTPResponse | None = None
        body_parts: list[bytes] = []
        try:
            if request.control_check is not None:
                request.control_check()
            raw_socket = _open_loopback_connect_tunnel(
                proxy, destination_address=address, destination_port=request.port,
                timeout_seconds=request.timeout_seconds,
            )
            connection = ssl.create_default_context().wrap_socket(
                raw_socket, server_hostname=request.host
            )
            raw_socket = None
            host_header = f"[{request.host}]" if ":" in request.host else request.host
            if request.port != 443:
                host_header = f"{host_header}:{request.port}"
            headers = {**request.headers, "Host": host_header, "Connection": "close"}
            if request.method not in {"GET", "POST", "DELETE"}:
                raise NetworkBoundaryError("URL request method is invalid")
            if request.body is not None:
                headers["Content-Length"] = str(len(request.body))
            payload = request.method + " " + request.target + " HTTP/1.1\r\n"
            payload += "".join(f"{name}: {value}\r\n" for name, value in headers.items()) + "\r\n"
            request_sent = True
            connection.sendall(payload.encode("ascii"))
            if request.body is not None:
                connection.sendall(request.body)
            response = http.client.HTTPResponse(connection)
            response.begin()
            if request.control_check is not None:
                request.control_check()
            content_length = response.getheader("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError as error:
                    raise NetworkBoundaryError("URL response has invalid content length") from error
                if declared_length < 0:
                    raise NetworkBoundaryError("URL response has invalid content length")
                if declared_length > request.max_response_bytes:
                    raise NetworkBoundaryError("URL response exceeds byte limit")
            body_size = 0
            while body_size <= request.max_response_bytes:
                if request.control_check is not None:
                    request.control_check()
                chunk = response.read(min(64 * 1024, request.max_response_bytes + 1 - body_size))
                if not chunk:
                    break
                body_parts.append(chunk)
                body_size += len(chunk)
            body = b"".join(body_parts)
            if len(body) > request.max_response_bytes:
                raise NetworkBoundaryError("URL response exceeds byte limit")
            return BoundedHttpResponse(response.status, dict(response.getheaders()), body)
        except NetworkBoundaryError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            partial = b"".join(body_parts)
            if request_sent and response is not None and partial and _is_sse_headers(dict(response.getheaders())):
                return BoundedHttpResponse(response.status, dict(response.getheaders()), partial, complete=False)
            if request_sent:
                raise NetworkBoundaryError("URL proxy response stream failed") from error
            last_error = error
        finally:
            if connection is not None:
                connection.close()
            elif raw_socket is not None:
                raw_socket.close()
    raise NetworkBoundaryError("URL proxy connection failed") from last_error


def _is_sse_headers(headers: Mapping[str, str]) -> bool:
    content_type = _header(headers, "content-type") or ""
    return content_type.split(";", 1)[0].strip().casefold() == "text/event-stream"


def _open_loopback_connect_tunnel(
    proxy: LoopbackHttpConnectProxy,
    *,
    destination_address: str,
    destination_port: int,
    timeout_seconds: float,
) -> socket.socket:
    connection = socket.create_connection((proxy.address, proxy.port), timeout=timeout_seconds)
    try:
        peer = str(ipaddress.ip_address(connection.getpeername()[0]))
        if peer != proxy.address:
            raise NetworkBoundaryError("loopback proxy peer does not match configured address")
        destination = f"[{destination_address}]" if ":" in destination_address else destination_address
        authority = f"{destination}:{destination_port}"
        request = (
            f"CONNECT {authority} HTTP/1.1\r\n"
            f"Host: {authority}\r\n"
            "Proxy-Connection: close\r\n\r\n"
        ).encode("ascii")
        connection.sendall(request)
        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            chunk = connection.recv(1)
            if not chunk:
                raise NetworkBoundaryError("loopback proxy CONNECT response ended early")
            header.extend(chunk)
            if len(header) > 4096:
                raise NetworkBoundaryError("loopback proxy CONNECT response is too large")
        lines = bytes(header[:-4]).split(b"\r\n")
        if not lines or lines[0] not in {b"HTTP/1.0 200 Connection established", b"HTTP/1.1 200 Connection established"}:
            raise NetworkBoundaryError("loopback proxy CONNECT was rejected")
        for line in lines[1:]:
            name, separator, value = line.partition(b":")
            if not separator or not name.strip() or any(byte < 32 and byte != 9 for byte in value):
                raise NetworkBoundaryError("loopback proxy CONNECT response is malformed")
            if name.strip().lower() in {b"transfer-encoding", b"proxy-authenticate"}:
                raise NetworkBoundaryError("loopback proxy CONNECT response is not allowed")
            if name.strip().lower() == b"content-length" and value.strip() != b"0":
                raise NetworkBoundaryError("loopback proxy CONNECT response body is not allowed")
        return connection
    except Exception:
        connection.close()
        raise


def _perform_pinned_binary_download(
    request: PinnedBinaryDownloadRequest,
) -> StreamedBinaryDownloadResponse:
    last_error: OSError | ssl.SSLError | None = None
    for address in request.addresses:
        raw_socket: socket.socket | None = None
        connection: socket.socket | ssl.SSLSocket | None = None
        try:
            if request.control_check is not None:
                request.control_check()
            raw_socket = socket.create_connection(
                (address, request.port), timeout=request.timeout_seconds
            )
            connection = raw_socket
            if request.scheme == "https":
                connection = ssl.create_default_context().wrap_socket(
                    raw_socket, server_hostname=request.host
                )
            peer = str(ipaddress.ip_address(connection.getpeername()[0]))
            if peer not in request.addresses or not ipaddress.ip_address(peer).is_global:
                raise NetworkBoundaryError("binary URL connection peer does not match validated DNS")
            host_header = f"[{request.host}]" if ":" in request.host else request.host
            default_port = 443 if request.scheme == "https" else 80
            if request.port != default_port:
                host_header = f"{host_header}:{request.port}"
            headers = {**request.headers, "Host": host_header, "Connection": "close"}
            payload = "GET " + request.target + " HTTP/1.1\r\n"
            payload += "".join(f"{name}: {value}\r\n" for name, value in headers.items()) + "\r\n"
            connection.sendall(payload.encode("ascii"))
            response = http.client.HTTPResponse(connection)
            response.begin()
            response_headers = dict(response.getheaders())
            if response.status in {301, 302, 303, 307, 308}:
                return StreamedBinaryDownloadResponse(response.status, response_headers, 0)
            content_length = _required_bounded_content_length(
                response_headers, max_response_bytes=request.max_response_bytes
            )
            written = 0
            with request.destination_part.open("xb") as handle:
                while written < content_length:
                    if request.control_check is not None:
                        request.control_check()
                    chunk = response.read(min(64 * 1024, content_length - written))
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > request.max_response_bytes:
                        raise NetworkBoundaryError("binary URL response exceeds byte limit")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if written != content_length:
                raise NetworkBoundaryError("binary URL response ended before declared size")
            return StreamedBinaryDownloadResponse(response.status, response_headers, written)
        except NetworkBoundaryError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            request.destination_part.unlink(missing_ok=True)
            last_error = error
        finally:
            if connection is not None:
                connection.close()
            elif raw_socket is not None:
                raw_socket.close()
    raise NetworkBoundaryError("binary URL connection failed") from last_error


def _perform_pinned_binary_download_via_loopback_proxy(
    request: PinnedBinaryDownloadRequest, proxy: LoopbackHttpConnectProxy
) -> StreamedBinaryDownloadResponse:
    if request.scheme != "https":
        raise NetworkBoundaryError("loopback proxy permits HTTPS destinations only")
    last_error: OSError | ssl.SSLError | http.client.HTTPException | None = None
    for address in request.addresses:
        raw_socket: socket.socket | None = None
        connection: ssl.SSLSocket | None = None
        try:
            if request.control_check is not None:
                request.control_check()
            raw_socket = _open_loopback_connect_tunnel(
                proxy, destination_address=address, destination_port=request.port,
                timeout_seconds=request.timeout_seconds,
            )
            connection = ssl.create_default_context().wrap_socket(
                raw_socket, server_hostname=request.host
            )
            raw_socket = None
            host_header = f"[{request.host}]" if ":" in request.host else request.host
            if request.port != 443:
                host_header = f"{host_header}:{request.port}"
            headers = {**request.headers, "Host": host_header, "Connection": "close"}
            payload = "GET " + request.target + " HTTP/1.1\r\n"
            payload += "".join(f"{name}: {value}\r\n" for name, value in headers.items()) + "\r\n"
            connection.sendall(payload.encode("ascii"))
            response = http.client.HTTPResponse(connection)
            response.begin()
            response_headers = dict(response.getheaders())
            if response.status in {301, 302, 303, 307, 308}:
                return StreamedBinaryDownloadResponse(response.status, response_headers, 0)
            content_length = _proxy_bounded_content_length(
                response_headers, max_response_bytes=request.max_response_bytes
            )
            written = 0
            with request.destination_part.open("xb") as handle:
                while content_length is None or written < content_length:
                    if request.control_check is not None:
                        request.control_check()
                    remaining = (
                        request.max_response_bytes + 1 - written
                        if content_length is None else content_length - written
                    )
                    chunk = response.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > request.max_response_bytes:
                        raise NetworkBoundaryError("binary URL response exceeds byte limit")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if content_length is not None and written != content_length:
                raise NetworkBoundaryError("binary URL response ended before declared size")
            return StreamedBinaryDownloadResponse(response.status, response_headers, written)
        except NetworkBoundaryError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            request.destination_part.unlink(missing_ok=True)
            last_error = error
        finally:
            if connection is not None:
                connection.close()
            elif raw_socket is not None:
                raw_socket.close()
    raise NetworkBoundaryError("binary URL proxy connection failed") from last_error


def _validated_binary_headers(values: Mapping[str, str] | None) -> dict[str, str]:
    headers = {
        "Accept": "image/*, audio/*, video/*, application/octet-stream;q=0.8",
        "User-Agent": "Chriptmas-OS-GovernedMediaHands/1.0",
    }
    for name, value in dict(values or {}).items():
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or not name
            or name.lower() in {"authorization", "connection", "cookie", "host", "proxy-authorization"}
            or any(character in name or character in value for character in "\r\n")
        ):
            raise NetworkBoundaryError("binary download header is not allowed")
        headers[name] = value
    return headers


def _validated_controlled_cookie_header_value(value: str) -> str:
    """Validate the locally materialized Cookie without widening public headers."""

    if (
        not isinstance(value, str)
        or not value
        or any(character in value for character in "\r\n")
    ):
        raise NetworkBoundaryError("controlled binary cookie header is invalid")
    return value


def _required_bounded_content_length(
    headers: Mapping[str, str], *, max_response_bytes: int
) -> int:
    value = _header(headers, "content-length")
    if not isinstance(value, str) or not value.isdigit():
        raise NetworkBoundaryError("binary URL response requires Content-Length")
    length = int(value)
    if length < 1 or length > max_response_bytes:
        raise NetworkBoundaryError("binary URL response exceeds byte limit")
    return length


def _proxy_bounded_content_length(
    headers: Mapping[str, str], *, max_response_bytes: int
) -> int | None:
    """Permit HTTP/1.1 chunking only on the explicit loopback CONNECT path.

    HTTPResponse removes the chunk framing.  The caller still enforces the
    same hard byte ceiling while streaming and reads one extra byte to detect
    an oversized body.
    """
    value = _header(headers, "content-length")
    if isinstance(value, str) and value.isdigit():
        return _required_bounded_content_length(headers, max_response_bytes=max_response_bytes)
    transfer_encoding = _header(headers, "transfer-encoding")
    if isinstance(transfer_encoding, str) and transfer_encoding.strip().casefold() == "chunked":
        return None
    raise NetworkBoundaryError("binary URL response requires bounded framing")


def _validated_binary_content_type(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NetworkBoundaryError("binary URL response content type is required")
    media_type = value.split(";", 1)[0].strip().lower()
    if not (
        media_type.startswith("image/")
        or media_type.startswith("audio/")
        or media_type.startswith("video/")
        or media_type == "application/octet-stream"
        or media_type in {"application/zip", "application/x-zip-compressed"}
    ):
        raise NetworkBoundaryError("binary URL response content type is not allowed")
    return media_type


def _validated_text_content_type(value: str | None) -> tuple[str, str]:
    if not value:
        raise NetworkBoundaryError("URL response content type is required")
    parts = [part.strip() for part in value.split(";")]
    media_type = parts[0].lower()
    allowed = media_type.startswith("text/") or media_type in {
        "application/json",
        "application/xml",
        "application/xhtml+xml",
    }
    if not allowed:
        raise NetworkBoundaryError("URL response content type is not allowed")
    charset = "utf-8"
    for parameter in parts[1:]:
        name, separator, parameter_value = parameter.partition("=")
        if separator and name.strip().lower() == "charset":
            charset = parameter_value.strip().strip('"') or "utf-8"
    return media_type, charset


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None
