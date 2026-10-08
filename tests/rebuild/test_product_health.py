from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.composition import build_product_health, build_product_runtime_readiness
from core.product_core import GetProductHealth, IndexHealth, PlatformHealth, StorageBoundary
from core.product_core.health import REQUIRED_CONTRACTS
from core.search_and_recall import ObjectStoreRecallIndex, RecallIndexEntry
from core.storage_provider import JsonObjectStore


@dataclass
class ContractCatalogStub:
    names: tuple[str, ...]

    def contract_names(self) -> tuple[str, ...]:
        return self.names


@dataclass
class StorageBoundaryStub:
    boundary: StorageBoundary

    def storage_boundary(self) -> StorageBoundary:
        return self.boundary


@dataclass
class IndexHealthStub:
    health: IndexHealth

    def index_health(self) -> IndexHealth:
        return self.health


@dataclass
class PlatformHealthStub:
    health: PlatformHealth

    def platform_health(self) -> PlatformHealth:
        return self.health


def _storage_boundary(
    *,
    legacy_access: str = "disabled",
    isolated: bool = True,
    namespace_id: str = "default",
    storage_version: int = 1,
    root_uri: str = "crp://default/",
    reference_root_uri: str = "crp-ref://default/",
    app_root_uri: str = "platform-app-data://chriptmas-replay",
    backup_ready: bool = True,
) -> StorageBoundary:
    return StorageBoundary(
        rebuild_root="/repo/.rebuild-data",
        legacy_root="/repo/library",
        legacy_access=legacy_access,  # type: ignore[arg-type]
        isolated=isolated,
        namespace_id=namespace_id,
        storage_version=storage_version,
        app_root_uri=app_root_uri,
        root_uri=root_uri,
        reference_root_uri=reference_root_uri,
        backup_ready=backup_ready,
    )


def _ready_index_health() -> IndexHealth:
    return IndexHealth(
        status="ready",
        manifest_present=True,
        backend_kind="object_store_lexical",
        entry_count=2,
        traceable=True,
        vector_enabled=False,
    )


def _ready_platform_health() -> PlatformHealth:
    return PlatformHealth(
        status="ready",
        capability_count=5,
        ready_capabilities=(
            "app_data_dir",
            "backup_destination",
            "file_picker",
            "system_info",
            "worker_lifecycle",
        ),
        degraded_capabilities=(),
        missing_capabilities=(),
        os_path_leaks=(),
    )


def test_product_health_is_ready_for_complete_contracts_and_isolated_storage() -> None:
    use_case = GetProductHealth(
        ContractCatalogStub(REQUIRED_CONTRACTS),
        StorageBoundaryStub(_storage_boundary()),
    )

    health = use_case.execute()

    assert health.status == "ready"
    assert health.contract_count >= len(REQUIRED_CONTRACTS)
    assert health.missing_contracts == ()
    assert health.storage_isolated is True
    assert health.legacy_access == "disabled"
    assert health.namespace_id == "default"
    assert health.storage_version == 1
    assert health.root_uri == "crp://default/"
    assert health.reference_root_uri == "crp-ref://default/"
    assert health.backup_ready is True


def test_product_health_degrades_when_a_contract_is_missing() -> None:
    without_source = tuple(name for name in REQUIRED_CONTRACTS if name != "source.schema.json")
    use_case = GetProductHealth(
        ContractCatalogStub(without_source),
        StorageBoundaryStub(_storage_boundary(legacy_access="read_only")),
    )

    health = use_case.execute()

    assert health.status == "degraded"
    assert health.missing_contracts == ("source.schema.json",)


def test_product_health_requires_asset_contract() -> None:
    without_asset = tuple(name for name in REQUIRED_CONTRACTS if name != "asset.schema.json")
    use_case = GetProductHealth(
        ContractCatalogStub(without_asset),
        StorageBoundaryStub(_storage_boundary()),
    )

    health = use_case.execute()

    assert health.status == "degraded"
    assert health.missing_contracts == ("asset.schema.json",)


def test_product_health_requires_storage_namespace_contract() -> None:
    without_storage_namespace = tuple(
        name for name in REQUIRED_CONTRACTS if name != "storage_namespace.schema.json"
    )
    use_case = GetProductHealth(
        ContractCatalogStub(without_storage_namespace),
        StorageBoundaryStub(_storage_boundary()),
    )

    health = use_case.execute()

    assert health.status == "degraded"
    assert health.missing_contracts == ("storage_namespace.schema.json",)


def test_product_health_requires_document_revision_contract() -> None:
    without_document_revision = tuple(
        name for name in REQUIRED_CONTRACTS if name != "document_revision.schema.json"
    )
    use_case = GetProductHealth(
        ContractCatalogStub(without_document_revision),
        StorageBoundaryStub(_storage_boundary()),
    )

    health = use_case.execute()

    assert health.status == "degraded"
    assert health.missing_contracts == ("document_revision.schema.json",)


def test_product_health_requires_platform_capability_contract() -> None:
    without_platform_capability = tuple(
        name for name in REQUIRED_CONTRACTS if name != "platform_capability.schema.json"
    )
    use_case = GetProductHealth(
        ContractCatalogStub(without_platform_capability),
        StorageBoundaryStub(_storage_boundary()),
    )

    health = use_case.execute()

    assert health.status == "degraded"
    assert health.missing_contracts == ("platform_capability.schema.json",)


def test_product_health_requires_memory_transition_contract() -> None:
    without_memory_transition = tuple(
        name for name in REQUIRED_CONTRACTS if name != "memory_transition.schema.json"
    )
    use_case = GetProductHealth(
        ContractCatalogStub(without_memory_transition),
        StorageBoundaryStub(_storage_boundary()),
    )

    health = use_case.execute()

    assert health.status == "degraded"
    assert health.missing_contracts == ("memory_transition.schema.json",)


def test_product_health_requires_persona_contract() -> None:
    without_persona = tuple(name for name in REQUIRED_CONTRACTS if name != "persona.schema.json")
    use_case = GetProductHealth(
        ContractCatalogStub(without_persona),
        StorageBoundaryStub(_storage_boundary()),
    )

    health = use_case.execute()

    assert health.status == "degraded"
    assert health.missing_contracts == ("persona.schema.json",)


def test_product_health_requires_series_memory_contract() -> None:
    without_series_memory = tuple(
        name for name in REQUIRED_CONTRACTS if name != "series_memory.schema.json"
    )
    use_case = GetProductHealth(
        ContractCatalogStub(without_series_memory),
        StorageBoundaryStub(_storage_boundary()),
    )

    health = use_case.execute()

    assert health.status == "degraded"
    assert health.missing_contracts == ("series_memory.schema.json",)


def test_product_health_requires_project_contract() -> None:
    without_project = tuple(name for name in REQUIRED_CONTRACTS if name != "project.schema.json")
    use_case = GetProductHealth(
        ContractCatalogStub(without_project),
        StorageBoundaryStub(_storage_boundary()),
    )

    health = use_case.execute()

    assert health.status == "degraded"
    assert health.missing_contracts == ("project.schema.json",)


def test_product_health_requires_project_skill_contract() -> None:
    without_project_skill = tuple(
        name for name in REQUIRED_CONTRACTS if name != "project_skill.schema.json"
    )
    use_case = GetProductHealth(
        ContractCatalogStub(without_project_skill),
        StorageBoundaryStub(_storage_boundary()),
    )

    health = use_case.execute()

    assert health.status == "degraded"
    assert health.missing_contracts == ("project_skill.schema.json",)


def test_product_health_requires_recall_request_contract() -> None:
    without_recall_request = tuple(
        name for name in REQUIRED_CONTRACTS if name != "recall_request.schema.json"
    )
    use_case = GetProductHealth(
        ContractCatalogStub(without_recall_request),
        StorageBoundaryStub(_storage_boundary()),
    )

    health = use_case.execute()

    assert health.status == "degraded"
    assert health.missing_contracts == ("recall_request.schema.json",)


def test_product_health_requires_recall_result_contract() -> None:
    without_recall_result = tuple(
        name for name in REQUIRED_CONTRACTS if name != "recall_result.schema.json"
    )
    use_case = GetProductHealth(
        ContractCatalogStub(without_recall_result),
        StorageBoundaryStub(_storage_boundary()),
    )

    health = use_case.execute()

    assert health.status == "degraded"
    assert health.missing_contracts == ("recall_result.schema.json",)


def test_product_health_requires_model_request_contract() -> None:
    without_model_request = tuple(
        name for name in REQUIRED_CONTRACTS if name != "model_request.schema.json"
    )
    use_case = GetProductHealth(
        ContractCatalogStub(without_model_request),
        StorageBoundaryStub(_storage_boundary()),
    )

    health = use_case.execute()

    assert health.status == "degraded"
    assert health.missing_contracts == ("model_request.schema.json",)


def test_product_health_requires_model_result_contract() -> None:
    without_model_result = tuple(
        name for name in REQUIRED_CONTRACTS if name != "model_result.schema.json"
    )
    use_case = GetProductHealth(
        ContractCatalogStub(without_model_result),
        StorageBoundaryStub(_storage_boundary()),
    )

    health = use_case.execute()

    assert health.status == "degraded"
    assert health.missing_contracts == ("model_result.schema.json",)


def test_product_health_reports_ready_index_boundary() -> None:
    use_case = GetProductHealth(
        ContractCatalogStub(REQUIRED_CONTRACTS),
        StorageBoundaryStub(_storage_boundary()),
        IndexHealthStub(_ready_index_health()),
    )

    health = use_case.execute()

    assert health.status == "ready"
    assert health.index_status == "ready"
    assert health.index_manifest_present is True
    assert health.index_backend_kind == "object_store_lexical"
    assert health.index_entry_count == 2
    assert health.index_traceable is True
    assert health.index_vector_enabled is False


def test_product_health_reports_ready_platform_capabilities() -> None:
    use_case = GetProductHealth(
        ContractCatalogStub(REQUIRED_CONTRACTS),
        StorageBoundaryStub(_storage_boundary()),
        IndexHealthStub(_ready_index_health()),
        PlatformHealthStub(_ready_platform_health()),
    )

    health = use_case.execute()

    assert health.status == "ready"
    assert health.platform_status == "ready"
    assert health.platform_capability_count == 5
    assert health.platform_ready_capabilities == (
        "app_data_dir",
        "backup_destination",
        "file_picker",
        "system_info",
        "worker_lifecycle",
    )
    assert health.platform_degraded_capabilities == ()
    assert health.platform_missing_capabilities == ()
    assert health.platform_os_path_leaks == ()


def test_product_health_degrades_when_platform_capability_degrades() -> None:
    use_case = GetProductHealth(
        ContractCatalogStub(REQUIRED_CONTRACTS),
        StorageBoundaryStub(_storage_boundary()),
        IndexHealthStub(_ready_index_health()),
        PlatformHealthStub(
            PlatformHealth(
                status="degraded",
                capability_count=5,
                ready_capabilities=("app_data_dir", "backup_destination", "file_picker", "system_info"),
                degraded_capabilities=("worker_lifecycle",),
                missing_capabilities=(),
                os_path_leaks=(),
            )
        ),
    )

    health = use_case.execute()

    assert health.status == "degraded"
    assert health.index_status == "ready"
    assert health.platform_status == "degraded"
    assert health.platform_degraded_capabilities == ("worker_lifecycle",)


def test_product_health_reports_platform_os_path_leak() -> None:
    use_case = GetProductHealth(
        ContractCatalogStub(REQUIRED_CONTRACTS),
        StorageBoundaryStub(_storage_boundary()),
        IndexHealthStub(_ready_index_health()),
        PlatformHealthStub(
            PlatformHealth(
                status="degraded",
                capability_count=5,
                ready_capabilities=("app_data_dir", "backup_destination", "file_picker", "system_info"),
                degraded_capabilities=(),
                missing_capabilities=(),
                os_path_leaks=("app_data_dir",),
            )
        ),
    )

    health = use_case.execute()

    assert health.status == "degraded"
    assert health.platform_status == "degraded"
    assert health.platform_os_path_leaks == ("app_data_dir",)


def test_product_health_degrades_when_index_manifest_is_missing() -> None:
    use_case = GetProductHealth(
        ContractCatalogStub(REQUIRED_CONTRACTS),
        StorageBoundaryStub(_storage_boundary()),
        IndexHealthStub(
            IndexHealth(
                status="degraded",
                manifest_present=False,
                backend_kind=None,
                entry_count=0,
                traceable=False,
                vector_enabled=False,
            )
        ),
    )

    health = use_case.execute()

    assert health.status == "degraded"
    assert health.index_status == "degraded"
    assert health.index_manifest_present is False


def test_product_health_degrades_when_index_vector_is_enabled() -> None:
    use_case = GetProductHealth(
        ContractCatalogStub(REQUIRED_CONTRACTS),
        StorageBoundaryStub(_storage_boundary()),
        IndexHealthStub(
            IndexHealth(
                status="degraded",
                manifest_present=True,
                backend_kind="object_store_lexical",
                entry_count=2,
                traceable=True,
                vector_enabled=True,
            )
        ),
    )

    health = use_case.execute()

    assert health.status == "degraded"
    assert health.index_vector_enabled is True


def test_product_health_degrades_when_namespace_uri_does_not_match() -> None:
    use_case = GetProductHealth(
        ContractCatalogStub(REQUIRED_CONTRACTS),
        StorageBoundaryStub(_storage_boundary(root_uri="crp://other/")),
    )

    health = use_case.execute()

    assert health.status == "degraded"
    assert health.missing_contracts == ()
    assert health.storage_isolated is True


def test_repository_composition_is_read_only_and_reports_missing_index(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    rebuild_root = tmp_path / ".rebuild-data"
    config = tmp_path / "rebuild.toml"
    config.write_text(
        f"""
[storage]
namespace_id = "default"
storage_version = 1
app_root_uri = "platform-app-data://chriptmas-replay"
root_uri = "crp://default/"
reference_root_uri = "crp-ref://default/"
root = "{rebuild_root.as_posix()}"
legacy_root = "{(tmp_path / 'library').as_posix()}"
legacy_access = "disabled"

[storage.backup]
enabled = true
retention_count = 5
pre_restore_required = true
""".strip(),
        encoding="utf-8",
    )
    existed_before = rebuild_root.exists()

    health = build_product_health(repository_root, config_path=config).execute()

    assert health.status == "degraded"
    assert health.contract_count >= len(REQUIRED_CONTRACTS)
    assert health.namespace_id == "default"
    assert health.backup_ready is True
    assert health.index_status == "degraded"
    assert health.index_manifest_present is False
    assert health.platform_status == "degraded"
    assert health.platform_degraded_capabilities == ("worker_lifecycle",)
    assert rebuild_root.exists() is existed_before


def test_repository_composition_reports_ready_persistent_index(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    rebuild_root = tmp_path / ".rebuild-data"
    config = tmp_path / "rebuild.toml"
    config.write_text(
        f"""
[storage]
namespace_id = "default"
storage_version = 1
app_root_uri = "platform-app-data://chriptmas-replay"
root_uri = "crp://default/"
reference_root_uri = "crp-ref://default/"
root = "{rebuild_root.as_posix()}"
legacy_root = "{(tmp_path / 'library').as_posix()}"
legacy_access = "disabled"

[storage.backup]
enabled = true
retention_count = 5
pre_restore_required = true
""".strip(),
        encoding="utf-8",
    )
    ObjectStoreRecallIndex(JsonObjectStore(rebuild_root, legacy_root=tmp_path / "library")).rebuild(
        (
            RecallIndexEntry(
                object_id="health-skill-alpha",
                project_id="project-alpha",
                layer="l3_project_skill",
                content="health recall index evidence",
                source_refs=("source-alpha#char:0-20",),
                trust_status="user_confirmed",
                base_score=0.8,
            ),
            RecallIndexEntry(
                object_id="health-atom-alpha",
                project_id="project-alpha",
                layer="l1_atom",
                content="health recall atom evidence",
                source_refs=("source-alpha#char:20-40",),
                trust_status="system_generated",
                base_score=0.6,
            ),
        ),
        source="product-health-test",
        rebuilt_at="2026-06-30T23:40:00+08:00",
    )

    health = build_product_health(repository_root, config_path=config).execute()

    assert health.status == "degraded"
    assert health.index_status == "ready"
    assert health.index_manifest_present is True
    assert health.index_backend_kind == "object_store_lexical"
    assert health.index_entry_count == 2
    assert health.index_traceable is True
    assert health.index_vector_enabled is False
    assert health.platform_status == "degraded"
    assert health.platform_degraded_capabilities == ("worker_lifecycle",)
    assert health.platform_missing_capabilities == ()
    assert health.platform_os_path_leaks == ()
    assert not (tmp_path / "library").exists()


def test_repository_composition_accepts_verified_sqlite_fts5_active_manifest(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    rebuild_root = tmp_path / ".rebuild-data"
    config = tmp_path / "rebuild.toml"
    config.write_text(
        f"""
[storage]
namespace_id = "default"
storage_version = 1
app_root_uri = "platform-app-data://chriptmas-replay"
root_uri = "crp://default/"
reference_root_uri = "crp-ref://default/"
root = "{rebuild_root.as_posix()}"
legacy_root = "{(tmp_path / 'library').as_posix()}"
legacy_access = "disabled"

[storage.backup]
enabled = true
retention_count = 5
pre_restore_required = true
""".strip(),
        encoding="utf-8",
    )
    object_store = JsonObjectStore(rebuild_root, legacy_root=tmp_path / "library")
    object_store.write(
        "recall_index_manifests",
        "active",
        _sqlite_fts5_active_manifest(),
        expected_revision=None,
    )

    health = build_product_health(repository_root, config_path=config).execute()

    assert health.status == "degraded"
    assert health.index_status == "ready"
    assert health.index_manifest_present is True
    assert health.index_backend_kind == "sqlite_fts5"
    assert health.index_entry_count == 2
    assert health.index_traceable is True
    assert health.index_vector_enabled is False
    assert health.platform_status == "degraded"
    assert not (tmp_path / "library").exists()


def test_repository_composition_rejects_unverified_sqlite_fts5_active_manifest(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    rebuild_root = tmp_path / ".rebuild-data"
    config = tmp_path / "rebuild.toml"
    config.write_text(
        f"""
[storage]
namespace_id = "default"
storage_version = 1
app_root_uri = "platform-app-data://chriptmas-replay"
root_uri = "crp://default/"
reference_root_uri = "crp-ref://default/"
root = "{rebuild_root.as_posix()}"
legacy_root = "{(tmp_path / 'library').as_posix()}"
legacy_access = "disabled"

[storage.backup]
enabled = true
retention_count = 5
pre_restore_required = true
""".strip(),
        encoding="utf-8",
    )
    manifest = _sqlite_fts5_active_manifest()
    manifest["verification_ref"] = None
    object_store = JsonObjectStore(rebuild_root, legacy_root=tmp_path / "library")
    object_store.write("recall_index_manifests", "active", manifest, expected_revision=None)

    health = build_product_health(repository_root, config_path=config).execute()

    assert health.status == "degraded"
    assert health.index_status == "degraded"
    assert health.index_backend_kind == "sqlite_fts5"
    assert health.index_entry_count == 2
    assert health.index_traceable is False
    assert health.index_vector_enabled is False
    assert not (tmp_path / "library").exists()


def test_repository_composition_rejects_sqlite_fts5_active_manifest_with_vector_enabled(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    rebuild_root = tmp_path / ".rebuild-data"
    config = tmp_path / "rebuild.toml"
    config.write_text(
        f"""
[storage]
namespace_id = "default"
storage_version = 1
app_root_uri = "platform-app-data://chriptmas-replay"
root_uri = "crp://default/"
reference_root_uri = "crp-ref://default/"
root = "{rebuild_root.as_posix()}"
legacy_root = "{(tmp_path / 'library').as_posix()}"
legacy_access = "disabled"

[storage.backup]
enabled = true
retention_count = 5
pre_restore_required = true
""".strip(),
        encoding="utf-8",
    )
    manifest = _sqlite_fts5_active_manifest()
    manifest["vector"] = {"enabled": True, "provider": "sqlite-vec", "dimension": 384}
    object_store = JsonObjectStore(rebuild_root, legacy_root=tmp_path / "library")
    object_store.write("recall_index_manifests", "active", manifest, expected_revision=None)

    health = build_product_health(repository_root, config_path=config).execute()

    assert health.status == "degraded"
    assert health.index_status == "degraded"
    assert health.index_backend_kind == "sqlite_fts5"
    assert health.index_traceable is True
    assert health.index_vector_enabled is True
    assert not (tmp_path / "library").exists()


def test_product_runtime_readiness_smoke_uses_temp_storage_only(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    real_library = repository_root / "library"
    real_library_existed_before = real_library.exists()

    readiness = build_product_runtime_readiness(repository_root, runtime_root=tmp_path).execute()

    assert readiness.status == "ready"
    assert {check.name for check in readiness.checks} == {
        "completed_multi_atom_publish",
        "checkpoint_staging_boundary",
        "persistent_recall_index_manifest",
        "persistent_recall_index_traceability",
        "resume_publish_idempotent",
        "series_memory_merge",
    }
    assert all(check.status == "ready" for check in readiness.checks)
    assert readiness.completed_job_id is not None
    assert readiness.resumed_job_id is not None
    assert readiness.series_memory_id == "series-memory-series-runtime-readiness"
    assert (tmp_path / ".rebuild-data").exists()
    assert (tmp_path / ".rebuild-data" / "jobs.sqlite3").is_file()
    runtime_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    assert runtime_store.list("jobs") == ()
    assert not (tmp_path / "library").exists()
    assert real_library.exists() is real_library_existed_before


def _sqlite_fts5_active_manifest() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": "active",
        "status": "active",
        "backend_kind": "sqlite_fts5",
        "source": "verified_index_rebuild_job",
        "source_fingerprint": "f22ab7ec12fc41e3",
        "source_count": 2,
        "source_refs": ["source-alpha#rev:1", "source-beta#rev:2"],
        "index_role": "active_manifest",
        "fts": {
            "engine": "sqlite",
            "module": "fts5",
            "table": "recall_fts",
            "content_columns": ["content", "search_text"],
            "tokenizer": "unicode61",
            "ranker": "bm25",
            "filters": ["project_id", "layer", "trust_status"],
        },
        "vector": {
            "enabled": False,
            "provider": None,
            "dimension": None,
        },
        "verification_ref": "crp://default/recall/index-verifications/job-index-rebuild-default-f22ab7ec12fc41e3",
        "verified_job_id": "job-index-rebuild-default-f22ab7ec12fc41e3",
        "previous_backend_kind": "object_store_lexical",
        "activated_by": "system",
        "activated_at": "2026-07-01T03:37:00+08:00",
    }
