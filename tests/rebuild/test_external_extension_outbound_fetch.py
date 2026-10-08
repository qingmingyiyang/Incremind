from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

import pytest

import core.external_extension_runtime.outbound_fetch as outbound_fetch
from core.external_extension_runtime.outbound_fetch import (
    DnsResolver,
    GovernedOutboundFetcher,
    HttpsTransport,
    OutboundFetchError,
    PinnedStdlibHttpsTransport,
    SystemDnsResolver,
    _fetcher_for_test,
    _TransportResponse,
)


class _Dns(DnsResolver):
    def __init__(self, answers: Mapping[str, Sequence[str]]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def resolve(self, host: str, *, deadline_monotonic: float) -> Sequence[str]:
        assert deadline_monotonic > 0
        self.calls.append(host)
        return self.answers[host]


class _Transport(HttpsTransport):
    def __init__(self, responses: Mapping[str, _TransportResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, str, int]] = []

    def get(
        self,
        *,
        url: str,
        connect_ip: str,
        server_name: str,
        max_bytes: int,
        deadline_monotonic: float,
    ) -> _TransportResponse:
        self.calls.append((url, connect_ip, server_name, max_bytes))
        return self.responses[url]


class _SequentialDns(DnsResolver):
    def __init__(self, answers: Sequence[Sequence[str]]) -> None:
        self.answers = list(answers)
        self.calls: list[str] = []

    def resolve(self, host: str, *, deadline_monotonic: float) -> Sequence[str]:
        assert deadline_monotonic > 0
        self.calls.append(host)
        return self.answers.pop(0)


def _response(
    *,
    status: int = 200,
    body: bytes = b"fixture",
    peer_ip: str = "93.184.216.34",
    headers: Mapping[str, str] | None = None,
) -> _TransportResponse:
    return _TransportResponse(
        status=status,
        headers={"content-type": "application/zip", **(headers or {})},
        body=body,
        peer_ip=peer_ip,
    )


def _fetcher(dns: _Dns, transport: _Transport, *, redirects: int = 2) -> GovernedOutboundFetcher:
    return _fetcher_for_test(dns, transport, max_redirects=redirects)


def _fetch(fetcher: GovernedOutboundFetcher, url: str, *, max_bytes: object = 32):
    return fetcher.fetch(
        url,
        allowed_hosts=frozenset({"example.com", "cdn.example.com"}),
        max_bytes=max_bytes,
        accepted_content_types=frozenset({"application/zip"}),
    )


@pytest.mark.parametrize(
    "url",
    (
        "http://example.com/artifact.zip",
        "https://user@example.com/artifact.zip",
        "https://example.com/artifact.zip?token=secret",
        "https://example.com/artifact.zip#fragment",
        "https://example.com:444/artifact.zip",
        "https://EXAMPLE.com/artifact.zip",
        "https://[::1]/artifact.zip",
        "https://evil.example/artifact.zip",
    ),
)
def test_fetch_rejects_non_governed_urls_before_dns(url: str) -> None:
    dns = _Dns({"example.com": ("93.184.216.34",)})
    transport = _Transport({})
    with pytest.raises(OutboundFetchError):
        _fetch(_fetcher(dns, transport), url)
    assert dns.calls == []
    assert transport.calls == []


@pytest.mark.parametrize(
    "address",
    (
        "127.0.0.1",
        "10.1.2.3",
        "169.254.1.1",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
        "::ffff:10.1.2.3",
        "fe80::1%eth0",
        "100.64.0.1",
    ),
)
def test_fetch_rejects_ssrf_dns_answers(address: str) -> None:
    dns = _Dns({"example.com": (address,)})
    transport = _Transport({})
    with pytest.raises(OutboundFetchError):
        _fetch(_fetcher(dns, transport), "https://example.com/artifact.zip")
    assert transport.calls == []


def test_fetch_rejects_dns_answer_mixed_with_private_address() -> None:
    dns = _Dns({"example.com": ("93.184.216.34", "127.0.0.1")})
    transport = _Transport({})
    with pytest.raises(OutboundFetchError):
        _fetch(_fetcher(dns, transport), "https://example.com/artifact.zip")
    assert transport.calls == []


def test_fetch_pins_response_to_the_resolved_ip() -> None:
    url = "https://example.com/artifact.zip"
    dns = _Dns({"example.com": ("93.184.216.34",)})
    transport = _Transport({url: _response(peer_ip="93.184.216.35")})
    with pytest.raises(OutboundFetchError):
        _fetch(_fetcher(dns, transport), url)
    assert transport.calls == [(url, "93.184.216.34", "example.com", 32)]


def test_fetch_rechecks_dns_and_policy_on_each_redirect() -> None:
    first = "https://example.com/artifact.zip"
    second = "https://cdn.example.com/artifact.zip"
    dns = _Dns({"example.com": ("93.184.216.34",), "cdn.example.com": ("93.184.216.35",)})
    transport = _Transport(
        {
            first: _response(status=302, headers={"location": second}),
            second: _response(peer_ip="93.184.216.35", body=b"payload"),
        }
    )
    result = _fetch(_fetcher(dns, transport), first)
    assert result.final_url == second
    assert result.body == b"payload"
    assert dns.calls == ["example.com", "cdn.example.com"]
    assert [call[1] for call in transport.calls] == ["93.184.216.34", "93.184.216.35"]


@pytest.mark.parametrize(
    ("location", "final_url"),
    (
        ("/artifact-v2.zip", "https://example.com/artifact-v2.zip"),
        ("//cdn.example.com/artifact.zip", "https://cdn.example.com/artifact.zip"),
    ),
)
def test_fetch_revalidates_safe_relative_and_protocol_relative_redirects(location: str, final_url: str) -> None:
    first = "https://example.com/artifact.zip"
    host = "cdn.example.com" if "cdn" in final_url else "example.com"
    dns = _Dns({"example.com": ("93.184.216.34",), "cdn.example.com": ("93.184.216.35",)})
    transport = _Transport(
        {
            first: _response(status=302, headers={"location": location}),
            final_url: _response(peer_ip="93.184.216.35" if host == "cdn.example.com" else "93.184.216.34"),
        }
    )
    assert _fetch(_fetcher(dns, transport), first).final_url == final_url


def test_fetch_pins_each_hop_when_dns_answer_changes_for_the_same_host() -> None:
    first = "https://example.com/first"
    second = "https://example.com/second"
    dns = _SequentialDns((("93.184.216.34",), ("93.184.216.35",)))
    transport = _Transport(
        {
            first: _response(status=302, headers={"location": second}),
            second: _response(peer_ip="93.184.216.35"),
        }
    )
    result = _fetch(_fetcher(dns, transport), first)
    assert result.peer_ip == "93.184.216.35"
    assert dns.calls == ["example.com", "example.com"]
    assert [call[1] for call in transport.calls] == ["93.184.216.34", "93.184.216.35"]


def test_fetch_rejects_redirect_to_unallowlisted_or_query_url() -> None:
    first = "https://example.com/artifact.zip"
    dns = _Dns({"example.com": ("93.184.216.34",)})
    transport = _Transport(
        {first: _response(status=302, headers={"location": "https://evil.example/a?secret=x"})}
    )
    with pytest.raises(OutboundFetchError):
        _fetch(_fetcher(dns, transport), first)
    assert dns.calls == ["example.com"]


@pytest.mark.parametrize("location", ("//evil.example/artifact.zip", "?token=secret", "x" * 2049))
def test_fetch_rejects_unsafe_or_overlong_redirect_locations(location: str) -> None:
    first = "https://example.com/artifact.zip"
    dns = _Dns({"example.com": ("93.184.216.34",)})
    transport = _Transport({first: _response(status=302, headers={"location": location})})
    with pytest.raises(OutboundFetchError):
        _fetch(_fetcher(dns, transport), first)


def test_fetch_enforces_redirect_limit() -> None:
    one = "https://example.com/one"
    two = "https://example.com/two"
    three = "https://example.com/three"
    dns = _Dns({"example.com": ("93.184.216.34",)})
    transport = _Transport(
        {
            one: _response(status=302, headers={"location": two}),
            two: _response(status=302, headers={"location": three}),
            three: _response(status=302, headers={"location": one}),
        }
    )
    with pytest.raises(OutboundFetchError):
        _fetch(_fetcher(dns, transport), one)
    assert len(transport.calls) == 3


def test_fetch_rejects_redirect_cycles_before_reissuing_a_request() -> None:
    one = "https://example.com/one"
    two = "https://example.com/two"
    dns = _Dns({"example.com": ("93.184.216.34",)})
    transport = _Transport(
        {
            one: _response(status=302, headers={"location": two}),
            two: _response(status=302, headers={"location": one}),
        }
    )
    with pytest.raises(OutboundFetchError):
        _fetch(_fetcher(dns, transport), one)
    assert len(transport.calls) == 2


@pytest.mark.parametrize(
    "response",
    (
        _response(status=201),
        _response(headers={"content-type": "text/plain"}),
        _response(headers={"content-length": "33"}),
        _response(body=b"x" * 33),
    ),
)
def test_fetch_rejects_status_content_type_and_size(response: _TransportResponse) -> None:
    url = "https://example.com/artifact.zip"
    dns = _Dns({"example.com": ("93.184.216.34",)})
    transport = _Transport({url: response})
    with pytest.raises(OutboundFetchError):
        _fetch(_fetcher(dns, transport), url)


@pytest.mark.parametrize("value", (" 32", "+32", "032", "32, 32", "-1", "three", "9" * 21))
def test_fetch_rejects_malformed_content_length(value: str) -> None:
    url = "https://example.com/artifact.zip"
    dns = _Dns({"example.com": ("93.184.216.34",)})
    transport = _Transport({url: _response(headers={"content-length": value})})
    with pytest.raises(OutboundFetchError):
        _fetch(_fetcher(dns, transport), url)


def test_fetch_rejects_duplicate_content_length_and_boolean_or_excessive_byte_limits() -> None:
    url = "https://example.com/artifact.zip"
    dns = _Dns({"example.com": ("93.184.216.34",)})
    duplicate = _Transport(
        {url: _response(headers={"content-length": "8", "Content-Length": "8"})}
    )
    with pytest.raises(OutboundFetchError):
        _fetch(_fetcher(dns, duplicate), url)
    safe_transport = _Transport({url: _response()})
    with pytest.raises(ValueError):
        _fetch(_fetcher(dns, safe_transport), url, max_bytes=True)
    with pytest.raises(ValueError):
        _fetch(_fetcher(dns, safe_transport), url, max_bytes=32 * 1024 * 1024 + 1)


def test_public_constructor_rejects_arbitrary_transport_injection() -> None:
    with pytest.raises(TypeError):
        GovernedOutboundFetcher(_Dns({}), _Transport({}), max_redirects=2, _construction_token=object())


def test_fetch_checks_aggregate_deadline_before_issuing_transport_request(monkeypatch) -> None:
    url = "https://example.com/artifact.zip"
    dns = _Dns({"example.com": ("93.184.216.34",)})
    transport = _Transport({url: _response()})
    ticks = iter((0.0, 0.0, 0.0, 21.0))
    monkeypatch.setattr(outbound_fetch.time, "monotonic", lambda: next(ticks))
    with pytest.raises(OutboundFetchError):
        _fetch(_fetcher(dns, transport), url)
    assert transport.calls == []


def test_fetch_sanitizes_transport_errors_without_preserving_the_cause() -> None:
    class _UnsafeTransport(HttpsTransport):
        def get(self, **_kwargs):
            raise OutboundFetchError("credential=value") from RuntimeError("credential=value")

    dns = _Dns({"example.com": ("93.184.216.34",)})
    with pytest.raises(OutboundFetchError) as error:
        _fetch(_fetcher_for_test(dns, _UnsafeTransport()), "https://example.com/artifact.zip")
    assert str(error.value) == "governed outbound fetch rejected"
    assert error.value.__cause__ is None


def test_stdlib_transport_uses_fixed_identity_headers_and_closes_connection(monkeypatch) -> None:
    class _Socket:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def getpeername(self):
            return ("93.184.216.34", 443)

        def settimeout(self, value: float) -> None:
            self.timeouts.append(value)

    class _Response:
        status = 200

        def __init__(self) -> None:
            self._chunks = [b"payload", b""]

        def getheaders(self):
            return [("Content-Type", "application/zip"), ("Content-Length", "7")]

        def read(self, _limit: int) -> bytes:
            return self._chunks.pop(0)

    class _Connection:
        instance = None

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.sock = _Socket()
            self.request_call = None
            self.closed = False
            _Connection.instance = self

        def request(self, method: str, target: str, *, headers: Mapping[str, str]) -> None:
            self.request_call = (method, target, dict(headers))

        def getresponse(self):
            return _Response()

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(outbound_fetch, "_PinnedHTTPSConnection", _Connection)
    response = PinnedStdlibHttpsTransport().get(
        url="https://example.com/artifact.zip",
        connect_ip="93.184.216.34",
        server_name="example.com",
        max_bytes=32,
        deadline_monotonic=outbound_fetch.time.monotonic() + 5,
    )
    connection = _Connection.instance
    assert response.body == b"payload"
    assert connection.request_call == (
        "GET",
        "/artifact.zip",
        {
            "Accept-Encoding": "identity",
            "User-Agent": "Chriptmas-OS-extension-acquisition/1",
        },
    )
    assert connection.closed is True
    assert len(connection.sock.timeouts) >= 3


def test_stdlib_transport_rejects_transparent_content_encoding(monkeypatch) -> None:
    class _Response:
        status = 200

        def getheaders(self):
            return [("Content-Type", "application/zip"), ("Content-Encoding", "gzip")]

    class _Connection:
        def __init__(self, **_kwargs: object) -> None:
            self.sock = type(
                "Socket",
                (),
                {
                    "getpeername": lambda self: ("93.184.216.34", 443),
                    "settimeout": lambda self, _value: None,
                },
            )()

        def request(self, *_args: object, **_kwargs: object) -> None:
            return None

        def getresponse(self):
            return _Response()

        def close(self) -> None:
            return None

    monkeypatch.setattr(outbound_fetch, "_PinnedHTTPSConnection", _Connection)
    with pytest.raises(OutboundFetchError):
        PinnedStdlibHttpsTransport().get(
            url="https://example.com/artifact.zip",
            connect_ip="93.184.216.34",
            server_name="example.com",
            max_bytes=32,
            deadline_monotonic=outbound_fetch.time.monotonic() + 5,
        )


@pytest.mark.skipif(os.name != "nt", reason="production bounded resolver is Windows-specific")
def test_system_dns_resolver_uses_bounded_windows_api_for_localhost() -> None:
    answers = SystemDnsResolver().resolve(
        "localhost",
        deadline_monotonic=outbound_fetch.time.monotonic() + 2,
    )
    assert answers
    assert set(answers) <= {"127.0.0.1", "::1"}


def test_windows_dns_timeout_never_waits_infinitely_and_defers_bounded_cleanup() -> None:
    outbound_fetch._PENDING_DNS_QUERIES.clear()
    state = {"signaled": False, "cancelled": 0, "closed": 0}
    waits: list[int] = []

    def wait_for_single(_event: object, milliseconds: int) -> int:
        waits.append(milliseconds)
        if milliseconds > 0:
            return 258
        return 0 if state["signaled"] else 258

    api = outbound_fetch._WindowsDnsApi(
        get_addr_info=lambda *_args: 997,
        get_overlapped_result=lambda *_args: 10111,
        cancel_query=lambda *_args: state.__setitem__("cancelled", state["cancelled"] + 1) or 0,
        free_addr_info=lambda *_args: None,
        create_event=lambda *_args: 101,
        wait_for_single=wait_for_single,
        close_handle=lambda *_args: state.__setitem__("closed", state["closed"] + 1) or True,
    )
    with pytest.raises(OutboundFetchError):
        outbound_fetch._windows_timed_getaddrinfo(
            "example.com",
            timeout_seconds=0.001,
            _api=api,
        )
    assert state["cancelled"] == 1
    assert waits == [1, 0]
    assert len(outbound_fetch._PENDING_DNS_QUERIES) == 1

    state["signaled"] = True
    outbound_fetch._reap_pending_windows_dns_queries()
    assert outbound_fetch._PENDING_DNS_QUERIES == []
    assert state["closed"] == 1
