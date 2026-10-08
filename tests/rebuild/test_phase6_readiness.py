from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from core.composition import build_phase6_readiness
from core.product_core import (
    GetPhase6Readiness,
    IndexHealth,
    StorageBoundary,
)
from core.search_and_recall import ObjectStoreRecallIndex, RecallIndexEntry
from core.storage_provider import JsonObjectStore


@dataclass(frozen=True)
class StorageBoundaryStub:
    boundary: StorageBoundary

    def storage_boundary(self) -> StorageBoundary:
        return self.boundary


@dataclass(frozen=True)
class IndexHealthStub:
    health: IndexHealth

    def index_health(self) -> IndexHealth:
        return self.health


@dataclass(frozen=True)
class PlatformCapabilityStub:
    capabilities: tuple[Mapping[str, object], ...]

    def platform_capabilities(self) -> tuple[Mapping[str, object], ...]:
        return self.capabilities


def _storage_boundary() -> StorageBoundary:
    return StorageBoundary(
        rebuild_root="/repo/.rebuild-data",
        legacy_root="/repo/library",
        legacy_access="disabled",
        isolated=True,
        namespace_id="default",
        storage_version=1,
        app_root_uri="platform-app-data://chriptmas-replay",
        root_uri="crp://default/",
        reference_root_uri="crp-ref://default/",
        backup_ready=True,
    )


def _index_health() -> IndexHealth:
    return IndexHealth(
        status="ready",
        manifest_present=True,
        backend_kind="object_store_lexical",
        entry_count=2,
        traceable=True,
        vector_enabled=False,
    )


def _capability(
    name: str,
    *,
    provided_uri: str | None = None,
    available: bool = True,
    platform: str = "windows",
    error: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    return {
        "schema_version": "1.0.0",
        "name": name,
        "platform": platform,
        "adapter_id": "desktop_windows",
        "available": available,
        "permission_required": False,
        "permission": "not_applicable",
        "provided_uri": provided_uri,
        "error": error,
        "checked_at": "2026-06-30T23:55:00+08:00",
    }


def _platform_capabilities() -> tuple[Mapping[str, object], ...]:
    return (
        _capability("app_data_dir", provided_uri="platform-app-data://chriptmas-replay"),
        _capability("file_picker"),
        _capability("backup_destination", provided_uri="platform-backup://chriptmas-replay/snapshots"),
        _capability(
            "worker_lifecycle",
            available=False,
            error={
                "code": "worker_unavailable",
                "message": "Worker lifecycle is unavailable in this probe.",
                "retryable": True,
                "degradation": "local_only",
            },
        ),
        _capability("system_info"),
    )


def _config(tmp_path: Path) -> Path:
    config = tmp_path / "rebuild.toml"
    config.write_text(
        f"""
[storage]
namespace_id = "default"
storage_version = 1
app_root_uri = "platform-app-data://chriptmas-replay"
root_uri = "crp://default/"
reference_root_uri = "crp-ref://default/"
root = "{(tmp_path / '.rebuild-data').as_posix()}"
legacy_root = "{(tmp_path / 'library').as_posix()}"
legacy_access = "disabled"

[storage.backup]
enabled = true
retention_count = 5
pre_restore_required = true
""".strip(),
        encoding="utf-8",
    )
    return config


def _build_index(tmp_path: Path) -> None:
    ObjectStoreRecallIndex(JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")).rebuild(
        (
            RecallIndexEntry(
                object_id="phase6-skill-alpha",
                project_id="project-alpha",
                layer="l3_project_skill",
                content="phase six readiness evidence",
                source_refs=("source-alpha#char:0-24",),
                trust_status="user_confirmed",
                base_score=0.8,
            ),
            RecallIndexEntry(
                object_id="phase6-atom-alpha",
                project_id="project-alpha",
                layer="l1_atom",
                content="phase six readiness atom evidence",
                source_refs=("source-alpha#char:24-48",),
                trust_status="system_generated",
                base_score=0.6,
            ),
        ),
        source="phase6-readiness-test",
        rebuilt_at="2026-07-01T00:05:00+08:00",
    )


def test_phase6_readiness_reports_ready_for_storage_platform_and_index() -> None:
    readiness = GetPhase6Readiness(
        storage=StorageBoundaryStub(_storage_boundary()),
        index=IndexHealthStub(_index_health()),
        platform=PlatformCapabilityStub(_platform_capabilities()),
    ).execute()

    assert readiness.status == "ready"
    assert [check.name for check in readiness.checks] == [
        "storage_namespace_portable",
        "legacy_library_protected",
        "platform_capability_minimum",
        "platform_uri_portability",
        "persistent_index_ready",
    ]
    assert all(check.status == "ready" for check in readiness.checks)
    assert set(readiness.ready_capabilities) == set(readiness.required_capabilities)


def test_phase6_readiness_degrades_when_index_is_missing() -> None:
    readiness = GetPhase6Readiness(
        storage=StorageBoundaryStub(_storage_boundary()),
        index=IndexHealthStub(
            IndexHealth(
                status="degraded",
                manifest_present=False,
                backend_kind=None,
                entry_count=0,
                traceable=False,
                vector_enabled=False,
            )
        ),
        platform=PlatformCapabilityStub(_platform_capabilities()),
    ).execute()

    checks = {check.name: check.status for check in readiness.checks}
    assert readiness.status == "degraded"
    assert checks["persistent_index_ready"] == "degraded"


def test_phase6_readiness_rejects_os_specific_platform_uri() -> None:
    capabilities = tuple(
        _capability("app_data_dir", provided_uri="C:\\Users\\example\\AppData\\Roaming\\Chriptmas_Replay")
        if item["name"] == "app_data_dir"
        else item
        for item in _platform_capabilities()
    )

    readiness = GetPhase6Readiness(
        storage=StorageBoundaryStub(_storage_boundary()),
        index=IndexHealthStub(_index_health()),
        platform=PlatformCapabilityStub(capabilities),
    ).execute()

    checks = {check.name: check.status for check in readiness.checks}
    assert readiness.status == "degraded"
    assert checks["platform_capability_minimum"] == "degraded"
    assert checks["platform_uri_portability"] == "degraded"


def test_phase6_readiness_composition_uses_temp_storage_only(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    _build_index(tmp_path)

    readiness = build_phase6_readiness(repository_root, config_path=_config(tmp_path)).execute()

    assert readiness.status == "ready"
    assert {check.name for check in readiness.checks} == {
        "storage_namespace_portable",
        "legacy_library_protected",
        "platform_capability_minimum",
        "platform_uri_portability",
        "persistent_index_ready",
    }
    assert all(check.status == "ready" for check in readiness.checks)
    assert not (tmp_path / "library").exists()
