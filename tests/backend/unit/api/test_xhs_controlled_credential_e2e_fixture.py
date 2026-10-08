from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from backend.api.governed_local_ocr import GovernedLocalOcrError
from backend.api.source_resolution_evidence import SourceResolutionEvidenceRepository
from backend.api.xhs_controlled_credential_e2e_fixture import (
    DESKTOP_NONCE_ENV,
    FIXTURE_COOKIE_CANARY,
    FIXTURE_NONCE_ENV,
    REAL_OCR_NONCE_ENV,
    FIXTURE_URL,
    controlled_credential_fixture_binary_transport_call_count,
    controlled_credential_fixture_transport_call_count,
    fixture_binary_marker_paths,
    installed_controlled_binary_network,
    installed_controlled_metadata_network,
    install_xhs_controlled_credential_e2e_fixture,
)
from backend.api.xiaohongshu_controlled_credential_runtime import (
    XiaohongshuControlledCredentialRuntime,
    XiaohongshuControlledCredentialRuntimeError,
)
from backend.api.xiaohongshu_asset_materializer import XiaohongshuAssetMaterializer
from backend.api.xiaohongshu_platform_provider import (
    XiaohongshuControlledMetadataPlatformProvider,
)
from backend.security.network_adapter import NetworkBoundaryError
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.secrets import InMemorySecretStore
from backend.security.xiaohongshu_controlled_credentials import (
    XiaohongshuControlledCredentialAuthority,
)
from core.storage_provider import JsonObjectStore
from core.source_processing import ManifestPermission


def test_fixture_does_not_install_without_matching_nonempty_nonces(tmp_path: Path, monkeypatch) -> None:
    for fixture, desktop in ((None, None), ("fixture-only", None), ("fixture", "desktop")):
        if fixture is None:
            monkeypatch.delenv(FIXTURE_NONCE_ENV, raising=False)
        else:
            monkeypatch.setenv(FIXTURE_NONCE_ENV, fixture)
        if desktop is None:
            monkeypatch.delenv(DESKTOP_NONCE_ENV, raising=False)
        else:
            monkeypatch.setenv(DESKTOP_NONCE_ENV, desktop)
        container = SimpleNamespace(root_dir=tmp_path)
        assert install_xhs_controlled_credential_e2e_fixture(container) is False
        assert not hasattr(container, "_e2e_xhs_controlled_metadata_network")
        assert installed_controlled_metadata_network(container) is None


def test_fixture_ocr_sentinel_is_ready_but_cannot_fabricate_output(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(FIXTURE_NONCE_ENV, "shared-supervisor-nonce")
    monkeypatch.setenv(DESKTOP_NONCE_ENV, "shared-supervisor-nonce")
    container = SimpleNamespace(root_dir=tmp_path)

    assert install_xhs_controlled_credential_e2e_fixture(container) is True
    container.media_xiaohongshu_ocr_runner.assert_ready()
    assert (
        container.media_xiaohongshu_ocr_runner.provider_revision
        == "xhs-controlled-binary-fence-no-ocr-v1"
    )
    with pytest.raises(
        GovernedLocalOcrError,
        match="^xhs_controlled_binary_fixture_ocr_forbidden$",
    ):
        container.media_xiaohongshu_ocr_runner.extract_text(
            tmp_path / "must-not-exist.jpg",
            media_type="image/jpeg",
            remaining_wall_ms=1,
        )


def test_fixture_real_ocr_mode_leaves_production_runner_unmodified(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(FIXTURE_NONCE_ENV, "shared-supervisor-nonce")
    monkeypatch.setenv(DESKTOP_NONCE_ENV, "shared-supervisor-nonce")
    monkeypatch.setenv(REAL_OCR_NONCE_ENV, "shared-supervisor-nonce")
    existing_runner = object()
    container = SimpleNamespace(
        root_dir=tmp_path, media_xiaohongshu_ocr_runner=existing_runner
    )

    assert install_xhs_controlled_credential_e2e_fixture(container) is True
    assert container.media_xiaohongshu_ocr_runner is existing_runner


def test_fixture_produces_production_controlled_image_set_without_secret_output(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(FIXTURE_NONCE_ENV, "shared-supervisor-nonce")
    monkeypatch.setenv(DESKTOP_NONCE_ENV, "shared-supervisor-nonce")
    container = SimpleNamespace(root_dir=tmp_path)
    assert install_xhs_controlled_credential_e2e_fixture(container) is True
    authority = XiaohongshuControlledCredentialAuthority(
        tmp_path, secret_store=InMemorySecretStore()
    )
    authority.grant(
        project_id="project-1",
        credential_subject_id="fixture-account-1",
        boundary_profile_id="project-boundary-project-1",
        boundary_revision=1,
        expires_at="2030-01-02T03:04:05Z",
        cookie_value=FIXTURE_COOKIE_CANARY,
        expected_authorization_revision=0,
        command_id="fixture-grant-1",
    )
    store = JsonObjectStore(tmp_path / "objects", namespace_id="default")
    provider = XiaohongshuControlledMetadataPlatformProvider(
        runtime=XiaohongshuControlledCredentialRuntime(
            authority=authority,
            boundary_profiles=ProjectBoundaryProfileStore(tmp_path),
        ),
        network=container._e2e_xhs_controlled_metadata_network,
        evidence=SourceResolutionEvidenceRepository(store, namespace_id="default"),
        namespace_id="default",
    )

    manifest = provider.provide_controlled(
        FIXTURE_URL, project_id="project-1", credential_subject_id="fixture-account-1"
    )

    assert manifest.content_kind == "image_set"
    assert [asset.kind for asset in manifest.assets] == ["image", "image"]
    assert manifest.credential_binding is not None
    assert manifest.credential_binding.credential_subject_id == "fixture-account-1"
    assert controlled_credential_fixture_transport_call_count(container) == 1
    persisted = json.dumps(
        store.list(SourceResolutionEvidenceRepository.collection), ensure_ascii=False
    )
    rendered = repr(manifest)
    assert FIXTURE_COOKIE_CANARY not in persisted
    assert FIXTURE_COOKIE_CANARY not in rendered
    assert "Cookie" not in persisted


def test_fixture_transport_rejects_missing_cookie_before_count(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(FIXTURE_NONCE_ENV, "shared-supervisor-nonce")
    monkeypatch.setenv(DESKTOP_NONCE_ENV, "shared-supervisor-nonce")
    container = SimpleNamespace(root_dir=tmp_path)
    assert install_xhs_controlled_credential_e2e_fixture(container) is True

    class MissingCookieLease:
        def cookie_header_value(self) -> str:
            return ""

    with pytest.raises(NetworkBoundaryError, match="fixture_request_denied"):
        container._e2e_xhs_controlled_metadata_network.fetch_text(
            FIXTURE_URL,
            wire_lease=MissingCookieLease(),
            control_check=lambda _post_wire: None,
        )
    assert controlled_credential_fixture_transport_call_count(container) == 0


def test_fixture_binary_adapter_waits_for_non_secret_release_marker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(FIXTURE_NONCE_ENV, "shared-supervisor-nonce")
    monkeypatch.setenv(DESKTOP_NONCE_ENV, "shared-supervisor-nonce")
    container = SimpleNamespace(root_dir=tmp_path)
    assert install_xhs_controlled_credential_e2e_fixture(container) is True
    authority = XiaohongshuControlledCredentialAuthority(
        tmp_path, secret_store=InMemorySecretStore()
    )
    authority.grant(
        project_id="project-1",
        credential_subject_id="fixture-account-1",
        boundary_profile_id="project-boundary-project-1",
        boundary_revision=1,
        expires_at="2030-01-02T03:04:05Z",
        cookie_value=FIXTURE_COOKIE_CANARY,
        expected_authorization_revision=0,
        command_id="fixture-grant-binary-1",
    )
    runtime = XiaohongshuControlledCredentialRuntime(
        authority=authority,
        boundary_profiles=ProjectBoundaryProfileStore(tmp_path),
        controlled_text_network=installed_controlled_metadata_network(container),
    )
    store = JsonObjectStore(tmp_path / "objects", namespace_id="default")
    provider = XiaohongshuControlledMetadataPlatformProvider(
        runtime=runtime,
        network=container._e2e_xhs_controlled_metadata_network,
        evidence=SourceResolutionEvidenceRepository(store, namespace_id="default"),
        namespace_id="default",
    )
    manifest = provider.provide_controlled(
        FIXTURE_URL, project_id="project-1", credential_subject_id="fixture-account-1"
    )
    manifest = replace(
        manifest,
        permission=ManifestPermission("granted", manifest.permission.evidence_refs),
    )
    materializer = XiaohongshuAssetMaterializer(
        object(),
        installed_controlled_binary_network(container),
        controlled_credential_runtime=runtime,
    )
    result: list[object] = []
    failure: list[BaseException] = []

    def materialize() -> None:
        try:
            result.append(materializer.materialize(
                manifest,
                job_id="fixture-binary-job",
                max_download_bytes=100_000,
                timeout_seconds=5,
                project_id="project-1",
            ))
        except BaseException as error:  # surfaced in the parent test thread
            failure.append(error)

    worker = Thread(target=materialize)
    worker.start()
    arrived, release = fixture_binary_marker_paths(container)
    deadline = monotonic() + 3
    while not arrived.is_file() and monotonic() < deadline:
        sleep(0.01)
    if not arrived.is_file():
        worker.join(timeout=1)
        if failure:
            raise failure[0]
    assert arrived.is_file()
    marker = json.loads(arrived.read_text(encoding="utf-8"))
    assert marker == {
        "schema_version": "1.0.0",
        "count": 1,
        "target_basename": "asset-1.jpg",
    }
    assert FIXTURE_COOKIE_CANARY not in arrived.read_text(encoding="utf-8")
    release.touch()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert failure == []
    assert len(result) == 1
    assert controlled_credential_fixture_binary_transport_call_count(container) == 2

    class UntrustedBinaryPort:
        calls = 0

        def download(self, *args, **kwargs):
            self.calls += 1
            pytest.fail("untrusted binary port must not receive a controlled request")

    untrusted = UntrustedBinaryPort()
    wire = runtime.issue_wire_lease(manifest, project_id="project-1", operation="binary")
    with pytest.raises(
        XiaohongshuControlledCredentialRuntimeError,
        match="controlled_credential_pre_wire_drift",
    ):
        wire.download(
            untrusted,
            "https://img.xhscdn.com/xhs-e2e/asset-1.jpg",
            relative_path="job/asset.jpg",
            max_response_bytes=100,
            headers={},
            control_check=lambda: None,
            timeout_seconds=1,
        )
    assert untrusted.calls == 0
