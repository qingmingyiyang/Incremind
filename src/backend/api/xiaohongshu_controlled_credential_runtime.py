"""OS-owned wire fence for Xiaohongshu controlled credentials.

The runtime turns durable, non-secret credential bindings into one-shot wire
leases.  Provider code receives neither SecretStore keys nor Cookie values;
only the dedicated network adapter can materialize the Cookie header.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from backend.security.network_adapter import (
    DownloadedBinary,
    NetworkBoundaryError,
    SafeBinaryDownloadAdapter,
    SafeTextNetworkAdapter,
)
from backend.security.project_boundary_profiles import (
    ProjectBoundaryProfileSnapshot,
    ProjectBoundaryProfileStore,
)
from backend.security.xiaohongshu_controlled_credentials import (
    XiaohongshuControlledCredentialAuthority,
    XiaohongshuControlledCredentialError,
    XiaohongshuControlledCredentialWireLease,
)
from core.source_processing import ControlledCredentialBinding, SourceManifest


class XiaohongshuControlledCredentialRuntimeError(ValueError):
    """Stable, non-secret resolve/download control-plane outcome."""


@dataclass(frozen=True, slots=True)
class SafeControlledCookieTextNetworkAdapter:
    """One-wire text reader whose Cookie injection is adapter-owned.

    Redirects are intentionally disabled: each platform wire receives a fresh
    lease, and a redirect must therefore be re-resolved by a later governed
    operation rather than silently reusing a Cookie on another request.
    """

    resolver: Callable[[str, int], tuple[str, ...]] | None = None
    transport: Callable | None = None
    max_response_bytes: int = 1024 * 1024
    timeout_seconds: float = 20.0
    allowed_hosts: tuple[str, ...] = ("xiaohongshu.com", "www.xiaohongshu.com")

    def fetch_text(
        self,
        url: str,
        *,
        wire_lease: XiaohongshuControlledCredentialWireLease,
        control_check: Callable[[bool], None],
    ) -> str:
        wire_started = False
        injected_cookie: list[str | None] = [None]

        def checkpoint() -> None:
            control_check(wire_started)

        def transport(request):
            nonlocal wire_started
            checkpoint()
            wire_started = True
            if self.transport is None:
                # Let SafeTextNetworkAdapter select its own pinned transport.
                from backend.security.network_adapter import _perform_pinned_request
                return _perform_pinned_request(request)
            return self.transport(request)

        def request_headers() -> dict[str, str]:
            value = wire_lease.cookie_header_value()
            injected_cookie[0] = value
            return {"Cookie": value}

        adapter = SafeTextNetworkAdapter(
            resolver=self.resolver,
            transport=transport,
            max_redirects=0,
            max_response_bytes=self.max_response_bytes,
            timeout_seconds=self.timeout_seconds,
            allowed_hosts=self.allowed_hosts,
            control_check=checkpoint,
            _request_headers=request_headers,
        )
        response = adapter.fetch_text(url)
        # Authenticated response content becomes durable evidence and Manifest
        # facts. Refuse persistence if the remote endpoint reflects the secret.
        if injected_cookie[0] and injected_cookie[0] in response:
            raise XiaohongshuControlledCredentialRuntimeError(
                "controlled_credential_post_wire_unknown"
            )
        return response


@dataclass(frozen=True, slots=True)
class XiaohongshuControlledCredentialRuntime:
    """Rechecks authorization, Boundary and secret generation for every wire."""

    authority: XiaohongshuControlledCredentialAuthority
    boundary_profiles: ProjectBoundaryProfileStore
    # This is a composition-only seam.  It remains an OS-owned adapter and is
    # never derived from a SourceManifest, model input, or a generic network
    # port.  It lets the packaged Gate exercise the same controlled page
    # revalidation path that the materializer uses in production.
    controlled_text_network: SafeControlledCookieTextNetworkAdapter | None = None

    def issue_wire_lease(
        self, manifest: SourceManifest, *, project_id: str, operation: str
    ) -> "_ControlledWireOperation":
        binding = manifest.credential_binding
        if binding is None or operation not in {"page", "binary"}:
            raise XiaohongshuControlledCredentialRuntimeError(
                "controlled_credential_pre_wire_drift"
            )
        # This object contains only frozen non-secret facts. Every request below
        # obtains its own authority lease while holding the live Boundary lock.
        return _ControlledWireOperation(self, project_id, binding)

    def current_binding(
        self, *, project_id: str, credential_subject_id: str
    ) -> ControlledCredentialBinding:
        authorization = self.authority.current(
            project_id=project_id, credential_subject_id=credential_subject_id
        )
        if authorization is None or authorization.state != "active":
            # A missing or revoked live authorization is a confirmed pre-wire
            # drift from the caller's requested controlled mode.  Keep the
            # stable terminal reason explicit so callers never collapse a
            # revoked credential into a generic unsupported-source result.
            raise XiaohongshuControlledCredentialRuntimeError(
                "controlled_credential_pre_wire_drift"
            )
        snapshot = self.boundary_profiles.get(project_id)
        if (
            snapshot.profile.profile_id != authorization.boundary_profile_id
            or snapshot.profile.revision != authorization.boundary_revision
        ):
            raise XiaohongshuControlledCredentialRuntimeError("controlled_credential_pre_wire_drift")
        return ControlledCredentialBinding(
            mode="controlled_credential",
            provider="xiaohongshu",
            credential_subject_id=authorization.credential_subject_id,
            authorization_ref=f"crp://controlled-credentials/xiaohongshu/{authorization.authorization_id}",
            authorization_revision=authorization.authorization_revision,
            secret_generation=authorization.secret_generation,
            boundary_profile_id=authorization.boundary_profile_id,
            boundary_profile_revision=authorization.boundary_revision,
        )

    def fetch_text(
        self,
        url: str,
        *,
        project_id: str,
        binding: ControlledCredentialBinding,
        network: SafeControlledCookieTextNetworkAdapter,
    ) -> str:
        # Keep the Boundary authority snapshot locked through lease issuance
        # and the pre-wire request fence.  It avoids a TOCTOU gap in which a
        # project policy changes between its inspection and Cookie injection.
        with self.boundary_profiles.locked_snapshot(project_id) as boundary:
            self._assert_binding_current(
                project_id=project_id, binding=binding, boundary=boundary, post_wire=False,
            )
            try:
                lease = self.authority.issue_wire_lease(
                    project_id=project_id,
                    provider=binding.provider,
                    credential_subject_id=binding.credential_subject_id,
                    boundary_profile_id=binding.boundary_profile_id,
                    boundary_revision=binding.boundary_profile_revision,
                    authorization_revision=binding.authorization_revision,
                    secret_generation=binding.secret_generation,
                )
            except XiaohongshuControlledCredentialError as error:
                raise XiaohongshuControlledCredentialRuntimeError("controlled_credential_pre_wire_drift") from error

            def control_check(post_wire: bool) -> None:
                self._assert_binding_current(
                    project_id=project_id, binding=binding, boundary=boundary, post_wire=post_wire,
                )
                if not lease.generation_current():
                    self._raise_drift(post_wire)

            try:
                return network.fetch_text(url, wire_lease=lease, control_check=control_check)
            except XiaohongshuControlledCredentialRuntimeError:
                raise
            except XiaohongshuControlledCredentialError as error:
                # Cookie materialization occurs before the wrapped transport,
                # so a failure here is always confirmed-none.
                raise XiaohongshuControlledCredentialRuntimeError(
                    "controlled_credential_pre_wire_drift"
                ) from error
            except NetworkBoundaryError:
                raise

    def _assert_binding_current(
        self,
        *,
        project_id: str,
        binding: ControlledCredentialBinding,
        boundary: ProjectBoundaryProfileSnapshot,
        post_wire: bool,
    ) -> None:
        if binding.mode != "controlled_credential" or binding.provider != "xiaohongshu":
            self._raise_drift(post_wire)
        try:
            authorization = self.authority.current(
                project_id=project_id, credential_subject_id=binding.credential_subject_id,
            )
        except (XiaohongshuControlledCredentialError, XiaohongshuControlledCredentialRuntimeError):
            self._raise_drift(post_wire)
        if authorization is None or authorization.state != "active":
            self._raise_drift(post_wire)
        current = ControlledCredentialBinding(
            mode="controlled_credential",
            provider="xiaohongshu",
            credential_subject_id=authorization.credential_subject_id,
            authorization_ref=f"crp://controlled-credentials/xiaohongshu/{authorization.authorization_id}",
            authorization_revision=authorization.authorization_revision,
            secret_generation=authorization.secret_generation,
            boundary_profile_id=boundary.profile.profile_id,
            boundary_profile_revision=boundary.profile.revision,
        )
        if (
            authorization.boundary_profile_id != boundary.profile.profile_id
            or authorization.boundary_revision != boundary.profile.revision
            or current != binding
        ):
            self._raise_drift(post_wire)

    @staticmethod
    def _raise_drift(post_wire: bool) -> None:
        raise XiaohongshuControlledCredentialRuntimeError(
            "controlled_credential_post_wire_unknown" if post_wire
            else "controlled_credential_pre_wire_drift"
        )


@dataclass(frozen=True, slots=True)
class _ControlledWireOperation:
    runtime: XiaohongshuControlledCredentialRuntime
    project_id: str
    binding: ControlledCredentialBinding

    def fetch_text(
        self, network: object, url: str, *, control_check: Callable[[], None]
    ) -> str:
        # The caller-supplied anonymous adapter is intentionally ignored. It
        # cannot receive a Cookie. The controlled adapter is constructed here.
        controlled = self.runtime.controlled_text_network
        if controlled is None:
            controlled = SafeControlledCookieTextNetworkAdapter(
                max_response_bytes=int(getattr(network, "max_response_bytes", 1024 * 1024)),
                timeout_seconds=float(getattr(network, "timeout_seconds", 20.0)),
            )
        return self.runtime.fetch_text(
            url, project_id=self.project_id, binding=self.binding, network=controlled
        )

    def download(
        self,
        network: object,
        url: str,
        *,
        relative_path: str,
        max_response_bytes: int,
        headers: Mapping[str, str],
        control_check: Callable[[], None],
        timeout_seconds: float,
    ) -> DownloadedBinary:
        wire_started = False
        downloaded: DownloadedBinary | None = None

        def mark_wire_started() -> None:
            nonlocal wire_started
            wire_started = True

        with self.runtime.boundary_profiles.locked_snapshot(self.project_id) as boundary:
            self.runtime._assert_binding_current(
                project_id=self.project_id, binding=self.binding,
                boundary=boundary, post_wire=False,
            )
            try:
                # A controlled binary must use the dedicated SafeBinary
                # internal entry point.  Generic ports cannot receive a
                # Cookie-bearing public header map.
                controlled_download = getattr(network, "_download_controlled", None)
                if (
                    not isinstance(network, SafeBinaryDownloadAdapter)
                    or not callable(controlled_download)
                ):
                    raise XiaohongshuControlledCredentialRuntimeError(
                        "controlled_credential_pre_wire_drift"
                    )
                lease = self.runtime.authority.issue_wire_lease(
                    project_id=self.project_id,
                    provider=self.binding.provider,
                    credential_subject_id=self.binding.credential_subject_id,
                    boundary_profile_id=self.binding.boundary_profile_id,
                    boundary_revision=self.binding.boundary_profile_revision,
                    authorization_revision=self.binding.authorization_revision,
                    secret_generation=self.binding.secret_generation,
                )
                control_check()
                downloaded = controlled_download(
                    url, relative_path=relative_path,
                    max_response_bytes=max_response_bytes,
                    headers=headers,
                    cookie_header_value=lease.cookie_header_value,
                    control_check=control_check,
                    timeout_seconds=timeout_seconds,
                    wire_started=mark_wire_started,
                )
                self.runtime._assert_binding_current(
                    project_id=self.project_id, binding=self.binding,
                    boundary=boundary, post_wire=True,
                )
                if not lease.generation_current():
                    self.runtime._raise_drift(True)
                return downloaded
            except XiaohongshuControlledCredentialRuntimeError:
                if downloaded is not None:
                    downloaded.path.unlink(missing_ok=True)
                raise
            except Exception as error:
                if downloaded is not None:
                    downloaded.path.unlink(missing_ok=True)
                raise XiaohongshuControlledCredentialRuntimeError(
                    "controlled_credential_post_wire_unknown" if wire_started
                    else "controlled_credential_pre_wire_drift"
                ) from error

    def generation_current(self) -> bool:
        try:
            with self.runtime.boundary_profiles.locked_snapshot(self.project_id) as boundary:
                self.runtime._assert_binding_current(
                    project_id=self.project_id, binding=self.binding,
                    boundary=boundary, post_wire=True,
                )
            return True
        except Exception:
            return False
