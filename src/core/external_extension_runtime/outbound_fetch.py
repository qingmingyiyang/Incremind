"""Extension-acquisition-only, DNS-pinned HTTPS egress transport.

This is not a general HTTP client or a central proxy. It is the only authorized
outbound transport for external-extension acquisition. The production
composition root must create it through ``production_extension_acquisition_fetcher``;
other code cannot inject an arbitrary transport through the public constructor.
"""

from __future__ import annotations

import ctypes
import http.client
import ipaddress
import os
import socket
import ssl
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from ctypes import wintypes
from threading import Lock
from typing import Final, Protocol
from urllib.parse import urljoin, urlsplit


_HARD_MAX_BYTES: Final = 32 * 1024 * 1024
_FETCH_DEADLINE_SECONDS: Final = 20.0
_DNS_TIMEOUT_SECONDS: Final = 5.0
_MAX_LOCATION_CHARS: Final = 2048
_REJECTED: Final = "governed outbound fetch rejected"
_CONSTRUCTION_TOKEN: Final = object()
_FIXED_USER_AGENT: Final = "Chriptmas-OS-extension-acquisition/1"
_MAX_PENDING_DNS_CLEANUPS: Final = 4
_PENDING_DNS_LOCK: Final = Lock()


class OutboundFetchError(ValueError):
    """A stable, sanitized failure from the governed acquisition boundary."""


@dataclass(frozen=True, slots=True)
class FetchedBytes:
    """Verified bytes from one allowlisted HTTPS endpoint."""

    final_url: str
    body: bytes
    content_type: str
    peer_ip: str


@dataclass(frozen=True, slots=True)
class _TransportResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    peer_ip: str


class DnsResolver(Protocol):
    def resolve(self, host: str, *, deadline_monotonic: float) -> Sequence[str]:
        """Return the complete current DNS answer for a canonical DNS host."""


class HttpsTransport(Protocol):
    def get(
        self,
        *,
        url: str,
        connect_ip: str,
        server_name: str,
        max_bytes: int,
        deadline_monotonic: float,
    ) -> _TransportResponse:
        """GET through *connect_ip*, preserving TLS SNI and verification host."""


class GovernedOutboundFetcher:
    """Read-only, bounded HTTPS fetcher; use the module factory in production."""

    def __init__(
        self,
        dns: DnsResolver,
        transport: HttpsTransport,
        *,
        max_redirects: int,
        _construction_token: object,
    ) -> None:
        if _construction_token is not _CONSTRUCTION_TOKEN:
            raise TypeError("use the extension acquisition fetcher factory")
        if isinstance(max_redirects, bool) or not isinstance(max_redirects, int) or not 0 <= max_redirects <= 2:
            raise ValueError("max_redirects must be between zero and two")
        self._dns = dns
        self._transport = transport
        self._max_redirects = max_redirects

    def fetch(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        max_bytes: int,
        accepted_content_types: frozenset[str],
    ) -> FetchedBytes:
        byte_limit = _max_bytes(max_bytes)
        allowed = _allowed_hosts(allowed_hosts)
        accepted = _content_types(accepted_content_types)
        deadline = time.monotonic() + _FETCH_DEADLINE_SECONDS
        current = url
        redirects = 0
        visited: set[str] = set()

        while True:
            _before_deadline(deadline)
            parsed, host = _validated_url(current, allowed)
            canonical_url = parsed.geturl()
            if canonical_url in visited:
                _reject()
            visited.add(canonical_url)
            selected_ip = self._resolve_public(host, deadline)
            response = self._request(
                canonical_url,
                connect_ip=selected_ip,
                server_name=host,
                max_bytes=byte_limit,
                deadline=deadline,
            )
            _before_deadline(deadline)
            if _canonical_ip(response.peer_ip) != selected_ip:
                _reject()
            _content_length_within(response.headers, byte_limit)
            if len(response.body) > byte_limit:
                _reject()

            if response.status in {301, 302, 303, 307, 308}:
                if redirects >= self._max_redirects:
                    _reject()
                location = _header(response.headers, "location")
                if not location or len(location) > _MAX_LOCATION_CHARS:
                    _reject()
                current = urljoin(canonical_url, location)
                redirects += 1
                _before_deadline(deadline)
                continue

            if response.status != 200:
                _reject()
            content_type = _media_type(_header(response.headers, "content-type"))
            if content_type not in accepted:
                _reject()
            return FetchedBytes(
                final_url=canonical_url,
                body=response.body,
                content_type=content_type,
                peer_ip=selected_ip,
            )

    def _resolve_public(self, host: str, deadline: float) -> str:
        _before_deadline(deadline)
        try:
            raw_answers = tuple(self._dns.resolve(host, deadline_monotonic=deadline))
        except Exception:
            _reject()
        _before_deadline(deadline)
        if not raw_answers:
            _reject()
        answers = tuple(_canonical_ip(value) for value in raw_answers)
        if any(not _is_public_ip(value) for value in answers):
            _reject()
        return answers[0]

    def _request(
        self,
        url: str,
        *,
        connect_ip: str,
        server_name: str,
        max_bytes: int,
        deadline: float,
    ) -> _TransportResponse:
        _before_deadline(deadline)
        try:
            response = self._transport.get(
                url=url,
                connect_ip=connect_ip,
                server_name=server_name,
                max_bytes=max_bytes,
                deadline_monotonic=deadline,
            )
        except OutboundFetchError:
            _reject()
        except Exception:
            _reject()
        _before_deadline(deadline)
        if not isinstance(response, _TransportResponse):
            _reject()
        if not isinstance(response.status, int) or isinstance(response.status, bool):
            _reject()
        if not isinstance(response.body, bytes) or not isinstance(response.headers, Mapping):
            _reject()
        return response


def production_extension_acquisition_fetcher() -> GovernedOutboundFetcher:
    """Build the fixed production egress instance for extension acquisition only."""
    return GovernedOutboundFetcher(
        SystemDnsResolver(),
        PinnedStdlibHttpsTransport(),
        max_redirects=2,
        _construction_token=_CONSTRUCTION_TOKEN,
    )


def _fetcher_for_test(
    dns: DnsResolver,
    transport: HttpsTransport,
    *,
    max_redirects: int = 2,
) -> GovernedOutboundFetcher:
    """Private test seam; production callers must use the fixed factory."""
    return GovernedOutboundFetcher(
        dns,
        transport,
        max_redirects=max_redirects,
        _construction_token=_CONSTRUCTION_TOKEN,
    )


class SystemDnsResolver:
    """Bounded Windows system resolver; unsupported platforms fail closed."""

    def resolve(self, host: str, *, deadline_monotonic: float) -> Sequence[str]:
        if os.name != "nt":
            _reject()
        timeout_seconds = min(_DNS_TIMEOUT_SECONDS, _remaining(deadline_monotonic))
        return _windows_timed_getaddrinfo(host, timeout_seconds=timeout_seconds)


class PinnedStdlibHttpsTransport:
    """Stdlib GET transport that dials the supplied IP and preserves TLS host checks."""

    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = float(timeout_seconds)

    def get(
        self,
        *,
        url: str,
        connect_ip: str,
        server_name: str,
        max_bytes: int,
        deadline_monotonic: float,
    ) -> _TransportResponse:
        try:
            parsed = urlsplit(url)
            target = parsed.path or "/"
            if (
                parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
                or parsed.hostname != server_name
                or parsed.scheme != "https"
                or parsed.port not in {None, 443}
            ):
                _reject()
            selected_ip = _canonical_ip(connect_ip)
            connection = _PinnedHTTPSConnection(
                host=server_name,
                peer_ip=selected_ip,
                port=443,
                timeout=min(self._timeout_seconds, _remaining(deadline_monotonic)),
                context=ssl.create_default_context(),
            )
            try:
                connection.request(
                    "GET",
                    target,
                    headers={
                        "Accept-Encoding": "identity",
                        "User-Agent": _FIXED_USER_AGENT,
                    },
                )
                _before_deadline(deadline_monotonic)
                if connection.sock is not None:
                    connection.sock.settimeout(
                        min(self._timeout_seconds, _remaining(deadline_monotonic))
                    )
                response = connection.getresponse()
                _before_deadline(deadline_monotonic)
                peer = connection.sock.getpeername()[0] if connection.sock is not None else ""
                peer_ip = _canonical_ip(peer)
                headers = _unique_headers(response.getheaders())
                content_encoding = _header(headers, "content-encoding")
                if content_encoding not in {None, "", "identity"}:
                    _reject()
                _content_length_within(headers, max_bytes)
                body = _read_limited(response, max_bytes, deadline_monotonic, connection)
                return _TransportResponse(response.status, headers, body, peer_ip)
            finally:
                connection.close()
        except OutboundFetchError:
            _reject()
        except (OSError, http.client.HTTPException, ssl.SSLError, ValueError):
            _reject()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *, peer_ip: str, **kwargs: object) -> None:
        self._peer_ip = peer_ip
        super().__init__(**kwargs)

    def connect(self) -> None:
        raw = socket.create_connection((self._peer_ip, self.port), self.timeout, self.source_address)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


class _WindowsTimeval(ctypes.Structure):
    _fields_ = (("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long))


class _WindowsOverlapped(ctypes.Structure):
    _fields_ = (
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    )


class _WindowsAddrInfoExW(ctypes.Structure):
    pass


_WindowsAddrInfoExWPointer = ctypes.POINTER(_WindowsAddrInfoExW)
_WindowsAddrInfoExW._fields_ = (
    ("ai_flags", ctypes.c_int),
    ("ai_family", ctypes.c_int),
    ("ai_socktype", ctypes.c_int),
    ("ai_protocol", ctypes.c_int),
    ("ai_addrlen", ctypes.c_size_t),
    ("ai_canonname", wintypes.LPWSTR),
    ("ai_addr", ctypes.c_void_p),
    ("ai_blob", ctypes.c_void_p),
    ("ai_bloblen", ctypes.c_size_t),
    ("ai_provider", ctypes.c_void_p),
    ("ai_next", _WindowsAddrInfoExWPointer),
)


@dataclass(frozen=True, slots=True)
class _WindowsDnsApi:
    get_addr_info: object
    get_overlapped_result: object
    cancel_query: object
    free_addr_info: object
    create_event: object
    wait_for_single: object
    close_handle: object


@dataclass(slots=True)
class _PendingWindowsDnsQuery:
    api: _WindowsDnsApi
    result: _WindowsAddrInfoExWPointer
    overlapped: _WindowsOverlapped
    cancel_handle: wintypes.HANDLE
    event_handle: object
    deferred: bool = False
    closed: bool = False


_PENDING_DNS_QUERIES: list[_PendingWindowsDnsQuery] = []


def _windows_timed_getaddrinfo(
    host: str,
    *,
    timeout_seconds: float,
    _api: _WindowsDnsApi | None = None,
) -> tuple[str, ...]:
    """Resolve through cancellable ``GetAddrInfoExW`` with a hard caller wait."""

    if _api is None and os.name != "nt":
        _reject()
    _reap_pending_windows_dns_queries()
    timeout_ms = max(1, min(5_000, int(timeout_seconds * 1_000)))
    api = _api or _load_windows_dns_api()
    result = _WindowsAddrInfoExWPointer()
    event_handle = api.create_event(None, True, False, None)
    if not event_handle:
        _reject()
    hints = _WindowsAddrInfoExW()
    hints.ai_flags = 0x00080000  # AI_DISABLE_IDN_ENCODING: host is canonical ASCII.
    hints.ai_family = socket.AF_UNSPEC
    hints.ai_socktype = socket.SOCK_STREAM
    hints.ai_protocol = socket.IPPROTO_TCP
    overlapped = _WindowsOverlapped()
    overlapped.hEvent = event_handle
    cancel_handle = wintypes.HANDLE()
    query = _PendingWindowsDnsQuery(api, result, overlapped, cancel_handle, event_handle)
    with _PENDING_DNS_LOCK:
        if len(_PENDING_DNS_QUERIES) >= _MAX_PENDING_DNS_CLEANUPS:
            _close_windows_dns_query(query)
            _reject()
        _PENDING_DNS_QUERIES.append(query)
    try:
        timeout = _WindowsTimeval(timeout_ms // 1_000, (timeout_ms % 1_000) * 1_000)
        status = api.get_addr_info(
            host,
            "443",
            12,  # NS_DNS
            None,
            ctypes.byref(hints),
            ctypes.byref(result),
            ctypes.byref(timeout),
            ctypes.byref(overlapped),
            None,
            ctypes.byref(cancel_handle),
        )
        if status == 997:  # ERROR_IO_PENDING
            wait_status = api.wait_for_single(event_handle, timeout_ms)
            if wait_status == 258:  # WAIT_TIMEOUT
                with _PENDING_DNS_LOCK:
                    query.deferred = True
                api.cancel_query(ctypes.byref(cancel_handle))
                if api.wait_for_single(event_handle, 0) == 0:
                    api.get_overlapped_result(ctypes.byref(overlapped))
                    with _PENDING_DNS_LOCK:
                        query.deferred = False
                _reject()
            if wait_status != 0:  # WAIT_OBJECT_0
                _reject()
            status = api.get_overlapped_result(ctypes.byref(overlapped))
        if status != 0 or not result:
            _reject()
        answers = _windows_addrinfo_addresses(result)
        if not answers:
            _reject()
        return answers
    except OutboundFetchError:
        raise
    except Exception:
        _reject()
    finally:
        if not query.deferred:
            with _PENDING_DNS_LOCK:
                if query in _PENDING_DNS_QUERIES:
                    _PENDING_DNS_QUERIES.remove(query)
            _close_windows_dns_query(query)


def _load_windows_dns_api() -> _WindowsDnsApi:
    windows_dll = getattr(ctypes, "WinDLL", None)
    if os.name != "nt" or windows_dll is None:
        _reject()
    ws2 = windows_dll("Ws2_32.dll", use_last_error=True)
    kernel = windows_dll("Kernel32.dll", use_last_error=True)
    get_addr_info = ws2.GetAddrInfoExW
    get_addr_info.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        _WindowsAddrInfoExWPointer,
        ctypes.POINTER(_WindowsAddrInfoExWPointer),
        ctypes.POINTER(_WindowsTimeval),
        ctypes.POINTER(_WindowsOverlapped),
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.HANDLE),
    )
    get_addr_info.restype = ctypes.c_int
    get_overlapped_result = ws2.GetAddrInfoExOverlappedResult
    get_overlapped_result.argtypes = (ctypes.POINTER(_WindowsOverlapped),)
    get_overlapped_result.restype = ctypes.c_int
    cancel_query = ws2.GetAddrInfoExCancel
    cancel_query.argtypes = (ctypes.POINTER(wintypes.HANDLE),)
    cancel_query.restype = ctypes.c_int
    free_addr_info = ws2.FreeAddrInfoExW
    free_addr_info.argtypes = (_WindowsAddrInfoExWPointer,)
    free_addr_info.restype = None
    create_event = kernel.CreateEventW
    create_event.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
    create_event.restype = wintypes.HANDLE
    wait_for_single = kernel.WaitForSingleObject
    wait_for_single.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait_for_single.restype = wintypes.DWORD
    close_handle = kernel.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    return _WindowsDnsApi(
        get_addr_info,
        get_overlapped_result,
        cancel_query,
        free_addr_info,
        create_event,
        wait_for_single,
        close_handle,
    )


def _reap_pending_windows_dns_queries() -> None:
    with _PENDING_DNS_LOCK:
        pending = tuple(_PENDING_DNS_QUERIES)
        for query in pending:
            if query.deferred and query.api.wait_for_single(query.event_handle, 0) == 0:
                query.api.get_overlapped_result(ctypes.byref(query.overlapped))
                _close_windows_dns_query(query)
                _PENDING_DNS_QUERIES.remove(query)


def _close_windows_dns_query(query: _PendingWindowsDnsQuery) -> None:
    if query.closed:
        return
    if query.result:
        query.api.free_addr_info(query.result)
    if query.event_handle:
        query.api.close_handle(query.event_handle)
    query.closed = True


def _windows_addrinfo_addresses(head: _WindowsAddrInfoExWPointer) -> tuple[str, ...]:
    answers: list[str] = []
    visited: set[int] = set()
    current = head
    while current:
        address_id = ctypes.addressof(current.contents)
        if address_id in visited or len(visited) >= 64:
            _reject()
        visited.add(address_id)
        entry = current.contents
        if not entry.ai_addr or entry.ai_addrlen > 128:
            _reject()
        raw = ctypes.string_at(entry.ai_addr, entry.ai_addrlen)
        if entry.ai_family == socket.AF_INET and len(raw) >= 8:
            value = socket.inet_ntop(socket.AF_INET, raw[4:8])
        elif entry.ai_family == socket.AF_INET6 and len(raw) >= 24:
            value = socket.inet_ntop(socket.AF_INET6, raw[8:24])
        else:
            current = entry.ai_next
            continue
        if value not in answers:
            answers.append(value)
        current = entry.ai_next
    return tuple(answers)


def _validated_url(url: object, allowed: frozenset[str]):
    if not isinstance(url, str) or not url or len(url) > _MAX_LOCATION_CHARS or any(char.isspace() for char in url):
        _reject()
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        _reject()
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
    ):
        _reject()
    host = _canonical_host(parsed.hostname)
    if parsed.netloc not in {host, f"{host}:443"}:
        _reject()
    if host not in allowed:
        _reject()
    return parsed, host


def _allowed_hosts(value: object) -> frozenset[str]:
    if not isinstance(value, frozenset) or not value:
        raise ValueError("allowed_hosts must be a non-empty frozenset")
    normalized = frozenset(_canonical_host(host) for host in value)
    if normalized != value:
        raise ValueError("allowed_hosts must be canonical lowercase LDH hosts")
    return normalized


def _canonical_host(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        _reject()
    try:
        value.encode("ascii")
        ipaddress.ip_address(value)
    except ValueError:
        pass
    except UnicodeEncodeError:
        _reject()
    else:
        _reject()
    labels = value.split(".")
    if any(
        not label
        or len(label) > 63
        or not label[0].isalnum()
        or not label[-1].isalnum()
        or any(not (character.isascii() and (character.islower() or character.isdigit() or character == "-")) for character in label)
        for label in labels
    ):
        _reject()
    return value


def _content_types(value: object) -> frozenset[str]:
    if not isinstance(value, frozenset) or not value:
        raise ValueError("accepted_content_types must be a non-empty frozenset")
    if any(not isinstance(item, str) or item != item.lower() for item in value):
        raise ValueError("accepted_content_types must be lowercase media types")
    return frozenset(_media_type(item) for item in value)


def _max_bytes(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= _HARD_MAX_BYTES:
        raise ValueError("max_bytes is outside the governed hard limit")
    return value


def _header(headers: Mapping[str, str], name: str) -> str | None:
    matches = [value for key, value in headers.items() if isinstance(key, str) and key.lower() == name]
    if len(matches) > 1 or (matches and not isinstance(matches[0], str)):
        _reject()
    return matches[0] if matches else None


def _unique_headers(headers: Sequence[tuple[str, str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers:
        normalized = str(key).lower()
        if normalized in result:
            _reject()
        result[normalized] = str(value)
    return result


def _media_type(value: object) -> str:
    if not isinstance(value, str) or not value:
        _reject()
    media = value.split(";", 1)[0].strip().lower()
    if not media or "/" not in media or any(character.isspace() for character in media):
        _reject()
    return media


def _canonical_ip(value: object) -> str:
    if not isinstance(value, str) or "%" in value:
        _reject()
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        _reject()
    mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
    return str(mapped or address)


def _is_public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return address.is_global and not any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _content_length_within(headers: Mapping[str, str], max_bytes: int) -> None:
    value = _header(headers, "content-length")
    if value is None:
        return
    if (
        len(value) > 20
        or not value.isascii()
        or (value != "0" and (not value or value[0] == "0"))
        or not value.isdecimal()
    ):
        _reject()
    if int(value) > max_bytes:
        _reject()


def _read_limited(
    response: http.client.HTTPResponse,
    max_bytes: int,
    deadline: float,
    connection: http.client.HTTPSConnection,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        _before_deadline(deadline)
        if connection.sock is not None:
            connection.sock.settimeout(_remaining(deadline))
        chunk = response.read(min(64 * 1024, max_bytes - total + 1))
        _before_deadline(deadline)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            _reject()
        chunks.append(chunk)
    return b"".join(chunks)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _reject()
    return remaining


def _before_deadline(deadline: float) -> None:
    _remaining(deadline)


def _reject() -> None:
    raise OutboundFetchError(_REJECTED) from None
