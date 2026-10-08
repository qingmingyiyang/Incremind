from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone, tzinfo
from pathlib import Path

from backend.companion_runtime_layout import build_companion_object_store
from core.aggregate_repository_factory import (
    AggregateRepositoryFactory,
    AggregateRepositoryFactoryError,
    STRUCTURED_DATABASE_NAME,
)
from core.companion_core import (
    CompanionMemoryBridge,
    CompanionMemoryBridgeError,
    CompanionRepository,
    build_companion_clock,
)
from core.memory_core import (
    ObjectStoreMemoryCandidateRepository,
    ObjectStoreMemoryStore,
    SQLiteMemoryReader,
)
from core.memory_core.publication_trust_audit_uow import SQLiteMemoryPublicationTrustAuditUnitOfWork
from core.search_and_recall import (
    LibrarySearchService,
    ObjectStoreRecallIndex,
    build_recall_authority_ledger,
    build_recall_entries_from_authorities,
)
from core.storage_provider import JsonObjectStore


def build_companion_memory_bridge(
    container: object,
    *,
    repository: CompanionRepository | None = None,
    project_id: str = "default",
) -> CompanionMemoryBridge:
    root_dir = Path(getattr(container, "root_dir")).resolve()
    companion_repository = repository or CompanionRepository.at_data_root(root_dir)
    clock = build_companion_clock()

    def local_today() -> date:
        return date.fromisoformat(clock.local_day(clock.now_utc()))

    def local_timezone() -> tzinfo:
        return clock.now_utc().astimezone().tzinfo or timezone.utc

    store = build_companion_object_store(root_dir)
    index = ObjectStoreRecallIndex(store)
    factory = AggregateRepositoryFactory(
        runtime_root=root_dir,
        namespace_id=store.namespace_id,
        json_store=store,
    )
    try:
        resolution = factory.memory_publication_authority_resolution()
    except AggregateRepositoryFactoryError:
        # A partially activated or rollback-required publication authority is
        # deliberately ambiguous. History and candidate review remain usable,
        # while recall and published-memory erasure fail closed instead of
        # choosing either JSON or SQLite behind the authority contract.
        return CompanionMemoryBridge(
            companion_repository,
            candidates=ObjectStoreMemoryCandidateRepository(store),
            search=LibrarySearchService(recall_index=index, active_manifest=index.manifest()),
            project_id=project_id,
            published_lookup=lambda _layer, _object_id: None,
            published_list=lambda _layer: (),
            candidate_delete=lambda candidate_id: store.delete("memory_candidates", candidate_id),
            published_dependency_id=lambda _layer, _object_id: None,
            published_withdraw=_unavailable_publication_withdraw,
            now=clock.now_utc,
            today=local_today,
            local_timezone=local_timezone,
        )
    memory = SQLiteMemoryReader(resolution.records) if resolution.records is not None else ObjectStoreMemoryStore(store)
    recall_entries = build_recall_entries_from_authorities(
        store,
        memory=memory,
        project_skills=factory.project_skill_repository(),
    )
    return CompanionMemoryBridge(
        companion_repository,
        candidates=ObjectStoreMemoryCandidateRepository(store),
        search=LibrarySearchService(
            recall_index=index,
            active_manifest=index.manifest(),
            source_ledger=build_recall_authority_ledger(recall_entries),
            current_entries=recall_entries,
        ),
        project_id=project_id,
        published_lookup=lambda layer, object_id: _published_memory(memory, layer, object_id),
        published_list=lambda layer: _published_memory_list(memory, layer),
        candidate_delete=lambda candidate_id: store.delete("memory_candidates", candidate_id),
        published_dependency_id=lambda layer, object_id: _publication_id(
            store,
            resolution.records,
            layer,
            object_id,
        ),
        published_withdraw=(
            (
                lambda publication_id: _withdraw_publication(
                    root_dir,
                    store,
                    publication_id,
                )
            )
            if resolution.records is not None
            else _unavailable_publication_withdraw
        ),
        now=clock.now_utc,
        today=local_today,
        local_timezone=local_timezone,
    )


def _unavailable_publication_withdraw(_publication_id: str) -> None:
    raise CompanionMemoryBridgeError("published memory authority is unavailable")


def _published_memory(memory: object, layer: str, object_id: str) -> Mapping[str, object] | None:
    domain_layer = {
        "l1_atom": "atom",
        "l2_scenario": "scenario",
        "l3_series_memory": "series_memory",
    }.get(layer)
    return memory.get(domain_layer, object_id) if domain_layer else None


def _published_memory_list(memory: object, layer: str) -> tuple[Mapping[str, object], ...]:
    domain_layer = {
        "l1_atom": "atom",
        "l2_scenario": "scenario",
        "l3_series_memory": "series_memory",
    }.get(layer)
    return tuple(memory.list(domain_layer)) if domain_layer else ()


def _publication_id(
    store: JsonObjectStore,
    records: object | None,
    layer: str,
    object_id: str,
) -> str | None:
    domain_layer = {
        "l1_atom": "atom",
        "l2_scenario": "scenario",
        "l3_series_memory": "series_memory",
    }.get(layer)
    items = (
        (record.payload for record in records.list("memory_publications"))
        if records is not None
        else store.list("memory_publications")
    )
    matches = [
        item
        for item in items
        if item.get("layer") == domain_layer
        and item.get("published_object_id") == object_id
        and item.get("status") == "published"
    ]
    return str(matches[-1].get("id") or matches[-1].get("publication_id")) if matches else None


def _withdraw_publication(
    root_dir: Path,
    store: JsonObjectStore,
    publication_id: str,
) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    with SQLiteMemoryPublicationTrustAuditUnitOfWork(
        root_dir / ".rebuild-data" / STRUCTURED_DATABASE_NAME,
        namespace_id=store.namespace_id,
    ).begin() as transaction:
        transaction.rollback_user_confirmed(
            publication_id=publication_id,
            reason="原始陪伴消息已由用户永久删除。",
            rolled_back_at=timestamp,
        )
        transaction.commit()


__all__ = ["build_companion_memory_bridge"]
