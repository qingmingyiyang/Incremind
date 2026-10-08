from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect

from backend.security.secret_egress import SecretEgressBroker
from core.product_core.realtime_asr_provider_settings import QWEN_REALTIME_ASR_SECRET_REF


_PROJECT_ID = "realtime-asr"
_PURPOSE = "realtime_transcription"


class QwenRealtimeSecureConnector:
    """Materializes the ASR key only while opening its approved WebSocket."""

    def __init__(self, secret_store: object, *, boundary_revision: str, wire_factory: Callable[[str, dict[str, str]], AbstractAsyncContextManager[object]] | None = None) -> None:
        self._boundary_revision = boundary_revision
        self._broker = SecretEgressBroker(
            secret_store, boundary_revision_reader=lambda project: (
                boundary_revision if project == _PROJECT_ID else "denied"
            )
        )
        self._wire_factory = wire_factory or _wire_connect

    def connect(self, endpoint: str) -> "_SecureConnection":
        return _SecureConnection(self._broker, self._boundary_revision, endpoint, self._wire_factory)


class _SecureConnection:
    def __init__(self, broker: SecretEgressBroker, boundary_revision: str, endpoint: str, wire_factory: Callable[[str, dict[str, str]], AbstractAsyncContextManager[object]]) -> None:
        self._broker, self._boundary_revision, self._endpoint, self._wire_factory = broker, boundary_revision, endpoint, wire_factory
        self._lease = None
        self._wire: AbstractAsyncContextManager[object] | None = None
        self.secret_generation = 0

    async def __aenter__(self) -> object:
        host = urlsplit(self._endpoint).hostname
        if not host:
            raise ValueError("realtime_asr_endpoint_invalid")
        self._lease = self._broker.grant(
            project_id=_PROJECT_ID,
            secret_ref=QWEN_REALTIME_ASR_SECRET_REF,
            purpose=_PURPOSE,
            allowed_hosts=(host,),
            boundary_revision=self._boundary_revision,
            ttl_seconds=30,
        )
        try:
            headers = self._broker.inject_header(
                self._lease,
                project_id=_PROJECT_ID,
                purpose=_PURPOSE,
                boundary_revision=self._boundary_revision,
                url=_http_wire_url(self._endpoint),
                header_name="Authorization",
                prefix="Bearer ",
            )
            self.secret_generation = self._lease.secret_revision
            self._wire = self._wire_factory(self._endpoint, headers)
            return await self._wire.__aenter__()
        except Exception:
            self._broker.revoke(self._lease.lease_id)
            raise

    async def __aexit__(self, *args: object) -> None:
        try:
            if self._wire is not None:
                await self._wire.__aexit__(*args)
        finally:
            if self._lease is not None:
                self._broker.revoke(self._lease.lease_id)


def _wire_connect(endpoint: str, headers: dict[str, str]) -> AbstractAsyncContextManager[object]:
    return connect(endpoint, additional_headers=headers, open_timeout=15, close_timeout=5, max_size=2 * 1024 * 1024)


def _http_wire_url(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment))


__all__ = ("QwenRealtimeSecureConnector",)
