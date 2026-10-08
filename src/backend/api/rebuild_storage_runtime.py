from __future__ import annotations

from pathlib import Path
from backend.security.user_context import json_attribution
from backend.security.audited_records import audit_records

from core.aggregate_repository_factory import (
    AggregateRepositoryFactory,
    AggregateRepositoryFactoryError,
)
from core.storage_provider import (
    JsonObjectStore,
    RebuildStorageSettings,
    SourceAssetRuntimeStore,
)


def resolve_rebuild_repository_root(module_path: Path) -> Path:
    """Locate the fixed rebuild configuration root in source and staged layouts."""

    resolved_path = Path(module_path).resolve()
    for candidate in resolved_path.parents:
        if (candidate / "config" / "rebuild.toml.example").is_file():
            return candidate
    raise RuntimeError("rebuild_config_root_not_found")


def build_rebuild_object_store(
    runtime_root: Path,
    *,
    repository_root: Path | None = None,
) -> tuple[SourceAssetRuntimeStore, RebuildStorageSettings]:
    """Compose the Source Asset authority used by rebuild API adapters.

    Authority discovery failures remain attached to the runtime store. This keeps
    unrelated API routes available while every Source Asset access still fails
    closed through the original resolver error.
    """

    resolved_repository_root = (
        Path(repository_root)
        if repository_root is not None
        else resolve_rebuild_repository_root(Path(__file__))
    )
    settings = RebuildStorageSettings.from_toml(
        resolved_repository_root / "config" / "rebuild.toml.example",
        repository_root=resolved_repository_root,
    )
    runtime_root = Path(runtime_root)
    json_store = JsonObjectStore(
        runtime_root / ".rebuild-data",
        legacy_root=runtime_root / "library",
        namespace_id=settings.namespace_id,
        mutation_attribution=json_attribution(runtime_root, settings.namespace_id),
    )
    authority_error: AggregateRepositoryFactoryError | None = None
    try:
        resolution = AggregateRepositoryFactory(
            runtime_root=runtime_root,
            namespace_id=settings.namespace_id,
            json_store=json_store,
        ).source_asset_authority_resolution()
        records = audit_records(resolution.records, runtime_root, settings.namespace_id)
        authority_identity = resolution.authority_identity
    except AggregateRepositoryFactoryError as error:
        authority_error = error
        records = None
        authority_identity = "unresolved:source-asset-authority"
    return (
        SourceAssetRuntimeStore(
            json_store=json_store,
            sqlite_records=records,
            library_root=runtime_root / "library",
            authority_identity=authority_identity,
            authority_error=authority_error,
        ),
        settings,
    )
