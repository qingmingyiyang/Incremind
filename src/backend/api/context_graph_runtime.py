"""Production composition for Core-owned LineMap graph and binding authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from backend.api.context_binding_composition import (
    ContextBindingCompositionService,
    SQLiteCompilationFactRepository,
)
from backend.api.context_binding_runtime import ContextBindingRegistry
from backend.api.capability_package_runtime import compile_context_graph_adapter_registry
from backend.api.capability_package_runtime import CapabilityPackageContributionCatalog
from backend.api.context_graph_import_composition import (
    ContextGraphImportService,
    SQLiteContextGraphImportEvidenceRepository,
)
from backend.api.context_graph_import_selection_authority import (
    ContextGraphImportSelectionAuthority,
)
from backend.api.context_graph_snapshot_runtime import ContextGraphSnapshotRepository
from backend.api.context_graph_replay_composition import (
    ContextGraphReplayCompositionService,
)
from backend.api.context_revision_resolver import ProjectContextRevisionResolver
from backend.api.workbench_original_asset_runtime import original_assets_root
from core.context_graph import (
    CapabilityPackageLoader,
    ContextCompiler,
    ContextGraphAdapterRegistry,
    ImportLimits,
)
from core.product_core.ports import ObjectStorePort
from core.storage_provider import SQLiteStructuredRecordStore


Clock = Callable[[], str]


@dataclass(frozen=True, slots=True)
class ContextGraphRuntime:
    """One shared runtime whose components contain no execution state machine."""

    snapshots: ContextGraphSnapshotRepository
    import_registry: ContextGraphAdapterRegistry
    import_selections: ContextGraphImportSelectionAuthority
    import_evidence: SQLiteContextGraphImportEvidenceRepository
    imports: ContextGraphImportService
    compilation_facts: SQLiteCompilationFactRepository
    bindings: ContextBindingRegistry
    composition: ContextBindingCompositionService
    replay: ContextGraphReplayCompositionService


def build_context_graph_runtime(
    container: object,
    packages: CapabilityPackageLoader,
    object_store: ObjectStorePort,
    *,
    namespace_id: str,
    contributions: CapabilityPackageContributionCatalog | None = None,
    clock: Clock | None = None,
) -> ContextGraphRuntime:
    root = Path(getattr(container, "root_dir")).resolve(strict=False)
    active_clock = clock or (
        lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    records = SQLiteStructuredRecordStore(
        root / ".rebuild-data" / "context-graphs.sqlite3"
    )
    snapshots = ContextGraphSnapshotRepository(records)
    import_registry = compile_context_graph_adapter_registry(
        packages, contributions,
    )
    import_selections = ContextGraphImportSelectionAuthority(
        records=records,
        object_store=object_store,
        managed_assets_root=original_assets_root(root),
    )
    import_evidence = SQLiteContextGraphImportEvidenceRepository(records)
    imports = ContextGraphImportService(
        snapshots,
        import_registry,
        import_evidence,
        import_selections,
        limits=ImportLimits(),
        clock=active_clock,
    )
    facts = SQLiteCompilationFactRepository(records, active_clock)
    bindings = ContextBindingRegistry(root, namespace_id=namespace_id)

    def active_capability_revision(capability_id: str) -> str | None:
        matches = [
            item for item in packages.active()
            if item.capability_id == capability_id
        ]
        return matches[0].capability_revision if len(matches) == 1 else None

    revisions = ProjectContextRevisionResolver(
        container, active_capability_revision,
    )
    composition = ContextBindingCompositionService(
        snapshots,
        bindings,
        packages,
        ContextCompiler(),
        revisions,
        active_clock,
        facts=facts,
    )
    replay = ContextGraphReplayCompositionService(
        records, snapshots, revisions, active_clock,
    )
    return ContextGraphRuntime(
        snapshots,
        import_registry,
        import_selections,
        import_evidence,
        imports,
        facts,
        bindings,
        composition,
        replay,
    )
