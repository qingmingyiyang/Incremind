from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from core.storage_provider import (
    SQLiteStructuredRecord,
    SQLiteStructuredRecordStore,
    SQLiteStructuredRecordUnitOfWork,
)
from core.storage_provider.external_agent_publication_change import (
    ExternalAgentPublicationChangeError,
    ExternalAgentPublicationChangeOutbox,
)

from .manual_publication_contract import (
    ManualPublicationContractError,
    STAGING_PUBLICATION_CONTEXT_COLLECTION,
    build_record,
    build_replacement_record,
    context_id,
    validate_context,
)


_LAYERS = {
    "atom": ("staging_atoms", "memory_atoms", "memory_atom_revisions"),
    "scenario": ("staging_scenarios", "memory_scenarios", "memory_scenario_revisions"),
    "series_memory": (
        "staging_series_memory",
        "memory_series_memory",
        "memory_series_memory_revisions",
    ),
}


class MemoryPublicationTrustAuditUnitOfWorkError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryPublicationTrustAuditCommit:
    records: tuple[SQLiteStructuredRecord, ...]
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class MemoryPublicationTrustAuditResult:
    layer: str
    object_id: str
    publication_id: str
    transition_id: str
    replayed: bool = False


class SQLiteMemoryPublicationTrustAuditUnitOfWork:
    """Fixture-only publication/audit transaction with immutable Memory revisions."""

    def __init__(
        self,
        database_path: Path,
        *,
        namespace_id: str = "default",
    ) -> None:
        self._records = SQLiteStructuredRecordStore(database_path)
        self._namespace_id = namespace_id

    def begin(self) -> MemoryPublicationTrustAuditTransaction:
        return MemoryPublicationTrustAuditTransaction(self._records.begin(), self._namespace_id)


class MemoryPublicationTrustAuditTransaction:
    def __init__(self, records: SQLiteStructuredRecordUnitOfWork, namespace_id: str) -> None:
        self._records = records
        self._namespace_id = namespace_id
        self._replayed = False

    def __enter__(self) -> MemoryPublicationTrustAuditTransaction:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if not self._records.closed:
            self._records.rollback()
        return False

    def publish(
        self,
        *,
        layer: str,
        staged_id: str,
        transition: Mapping[str, object],
        publication: Mapping[str, object],
    ) -> None:
        staging, published, revisions = _collections(layer)
        staged = self._records.read(staging, staged_id)
        if staged is None:
            self._assert_publish_replay(layer, staged_id, published, revisions, transition, publication)
            self._replayed = True
            return

        context = self._records.read(STAGING_PUBLICATION_CONTEXT_COLLECTION, context_id(layer, staged_id))
        if context is None:
            raise MemoryPublicationTrustAuditUnitOfWorkError(
                "staging memory requires canonical manual publication context"
            )
        normalized_context = _context(context.payload, self._namespace_id, layer, staged_id)
        domain_revision = _domain_revision(staged.payload)
        expected_publication = _publication(
            normalized_context,
            namespace_id=self._namespace_id,
            layer=layer,
            object_id=staged_id,
            domain_revision=domain_revision,
            publication=publication,
        )
        expected_transition = _publish_transition(
            layer=layer,
            object_id=staged_id,
            domain_revision=domain_revision,
            publication=expected_publication,
        )
        if dict(transition) != expected_transition:
            raise MemoryPublicationTrustAuditUnitOfWorkError("publication transition evidence conflicts")
        published_payload = _published_payload(staged.payload, expected_publication)
        revision = _published_revision(
            layer=layer,
            object_id=staged_id,
            domain_revision=domain_revision,
            publication=expected_publication,
            payload=published_payload,
        )
        self._records.put(published, staged_id, published_payload, expected_revision=0)
        _put_append_only(self._records, revisions, _revision_id(staged_id, domain_revision), revision)
        _put_append_only(
            self._records,
            "memory_transitions",
            _required_id(expected_transition, "id"),
            expected_transition,
        )
        _put_append_only(
            self._records,
            "memory_publications",
            _required_id(expected_publication, "id"),
            expected_publication,
        )
        _enqueue_external_agent_publication_changes(
            self._records,
            layer=layer,
            payload=published_payload,
            publication=expected_publication,
        )
        self._records.delete(staging, staged_id, expected_revision=staged.revision)
        self._records.delete(
            STAGING_PUBLICATION_CONTEXT_COLLECTION,
            context_id(layer, staged_id),
            expected_revision=context.revision,
        )

    def replace(
        self,
        *,
        layer: str,
        staged_id: str,
        supersedes_publication_id: str,
        transition: Mapping[str, object],
        publication: Mapping[str, object],
    ) -> None:
        """Atomically replace one current projection with its next revision."""

        staging, published, revisions = _collections(layer)
        staged = self._records.read(staging, staged_id)
        previous_publication = self._records.read("memory_publications", supersedes_publication_id)
        current = self._records.read(published, staged_id)
        if staged is None:
            self._assert_replace_replay(
                layer, staged_id, published, revisions, supersedes_publication_id, transition, publication
            )
            self._replayed = True
            return
        if previous_publication is None or current is None:
            raise MemoryPublicationTrustAuditUnitOfWorkError("replacement baseline is missing")
        previous_revision = _domain_revision(current.payload)
        if _domain_revision(staged.payload) != previous_revision + 1:
            raise MemoryPublicationTrustAuditUnitOfWorkError("replacement revision is not consecutive")
        if (
            previous_publication.payload.get("status") != "published"
            or _required_str(previous_publication.payload, "layer") != layer
            or _required_id(previous_publication.payload, "published_object_id") != staged_id
            or _required_revision(previous_publication.payload, "published_revision") != previous_revision
        ):
            raise MemoryPublicationTrustAuditUnitOfWorkError("replacement publication baseline conflicts")
        previous_immutable = self._records.read(revisions, _revision_id(staged_id, previous_revision))
        if previous_immutable is None or previous_immutable.payload.get("payload") != current.payload:
            raise MemoryPublicationTrustAuditUnitOfWorkError("replacement immutable baseline conflicts")
        context = self._records.read(STAGING_PUBLICATION_CONTEXT_COLLECTION, context_id(layer, staged_id))
        if context is None:
            raise MemoryPublicationTrustAuditUnitOfWorkError("staging memory requires canonical manual publication context")
        normalized_context = _context(context.payload, self._namespace_id, layer, staged_id)
        expected_publication = _replacement_publication(
            normalized_context,
            namespace_id=self._namespace_id,
            layer=layer,
            object_id=staged_id,
            domain_revision=previous_revision + 1,
            previous=previous_publication.payload,
            publication=publication,
        )
        expected_transition = _replacement_transition(
            layer=layer,
            object_id=staged_id,
            previous_revision=previous_revision,
            publication=expected_publication,
        )
        if dict(transition) != expected_transition:
            raise MemoryPublicationTrustAuditUnitOfWorkError("replacement transition evidence conflicts")
        replacement_payload = _published_payload(staged.payload, expected_publication)
        replacement_revision = _published_revision(
            layer=layer,
            object_id=staged_id,
            domain_revision=previous_revision + 1,
            publication=expected_publication,
            payload=replacement_payload,
            previous_revision_id=_revision_id(staged_id, previous_revision),
        )
        superseded = _superseded_publication(previous_publication.payload, expected_publication)
        self._records.put(published, staged_id, replacement_payload, expected_revision=current.revision)
        _put_append_only(self._records, revisions, _revision_id(staged_id, previous_revision + 1), replacement_revision)
        _put_append_only(self._records, "memory_transitions", _required_id(expected_transition, "id"), expected_transition)
        _put_append_only(self._records, "memory_publications", _required_id(expected_publication, "id"), expected_publication)
        self._records.put("memory_publications", supersedes_publication_id, superseded, expected_revision=previous_publication.revision)
        _enqueue_external_agent_publication_changes(
            self._records,
            layer=layer,
            payload=replacement_payload,
            publication=expected_publication,
        )
        self._records.delete(staging, staged_id, expected_revision=staged.revision)
        self._records.delete(STAGING_PUBLICATION_CONTEXT_COLLECTION, context_id(layer, staged_id), expected_revision=context.revision)

    def rollback(
        self,
        *,
        layer: str,
        publication_id: str,
        transition: Mapping[str, object],
        publication: Mapping[str, object],
    ) -> None:
        _staging, published, revisions = _collections(layer)
        current_publication = self._records.read("memory_publications", publication_id)
        object_id = _required_id(publication, "published_object_id")
        transition_id = _required_id(transition, "id")
        if current_publication is None:
            raise MemoryPublicationTrustAuditUnitOfWorkError("memory publication not found")
        if current_publication.payload.get("status") == "rolled_back":
            self._assert_rollback_replay(
                layer,
                published,
                revisions,
                object_id,
                transition_id,
                publication_id,
                transition,
                publication,
            )
            self._replayed = True
            return
        if current_publication.payload.get("status") != "published":
            raise MemoryPublicationTrustAuditUnitOfWorkError("memory publication is not published")
        if _required_id(current_publication.payload, "id") != publication_id:
            raise MemoryPublicationTrustAuditUnitOfWorkError("memory publication identity conflicts")
        if _required_str(current_publication.payload, "layer") != layer:
            raise MemoryPublicationTrustAuditUnitOfWorkError("memory publication layer conflicts")
        if _required_id(current_publication.payload, "published_object_id") != object_id:
            raise MemoryPublicationTrustAuditUnitOfWorkError("memory publication object conflicts")

        current_object = self._records.read(published, object_id)
        if current_object is None:
            raise MemoryPublicationTrustAuditUnitOfWorkError("published memory is missing")
        domain_revision = _domain_revision(current_object.payload)
        if domain_revision != _required_revision(current_publication.payload, "published_revision"):
            raise MemoryPublicationTrustAuditUnitOfWorkError("published memory revision conflicts")
        initial_revision = self._records.read(revisions, _revision_id(object_id, domain_revision))
        expected_initial_revision = _published_revision(
            layer=layer,
            object_id=object_id,
            domain_revision=domain_revision,
            publication=current_publication.payload,
            payload=current_object.payload,
            previous_revision_id=(
                _revision_id(object_id, domain_revision - 1)
                if domain_revision > 1
                else None
            ),
        )
        if initial_revision is None or dict(initial_revision.payload) != expected_initial_revision:
            raise MemoryPublicationTrustAuditUnitOfWorkError("published memory immutable revision is missing or drifted")

        expected_publication = _rollback_publication(
            original=current_publication.payload,
            layer=layer,
            object_id=object_id,
            domain_revision=domain_revision,
            transition=transition,
            namespace_id=self._namespace_id,
        )
        if dict(publication) != expected_publication:
            raise MemoryPublicationTrustAuditUnitOfWorkError("rollback publication evidence conflicts")
        expected_transition = _rollback_transition(
            layer=layer,
            object_id=object_id,
            domain_revision=domain_revision,
            publication=expected_publication,
            transition_id=transition_id,
        )
        if dict(transition) != expected_transition:
            raise MemoryPublicationTrustAuditUnitOfWorkError("rollback transition evidence conflicts")
        rollback_revision = _rolled_back_revision(
            layer=layer,
            object_id=object_id,
            domain_revision=domain_revision + 1,
            previous_revision_id=_revision_id(object_id, domain_revision),
            publication=expected_publication,
            payload=current_object.payload,
        )
        _put_append_only(
            self._records,
            revisions,
            _revision_id(object_id, domain_revision + 1),
            rollback_revision,
        )
        _put_append_only(self._records, "memory_transitions", transition_id, expected_transition)
        self._records.put(
            "memory_publications",
            publication_id,
            expected_publication,
            expected_revision=current_publication.revision,
        )
        _enqueue_external_agent_memory_invalidation(
            self._records,
            layer=layer,
            payload=current_object.payload,
            publication=expected_publication,
            transition=expected_transition,
        )
        self._records.delete(published, object_id, expected_revision=current_object.revision)

    def publish_user_confirmed(self, *, layer: str, staged_id: str, published_at: str) -> MemoryPublicationTrustAuditResult:
        """Build canonical evidence inside the authority-owning transaction."""
        _staging, published, _revisions = _collections(layer)
        staged = self._records.read(_staging, staged_id)
        if staged is None:
            publication_id = f"memory-publication-{layer.replace('_', '-')}-{staged_id}"
            publication = self._records.read("memory_publications", publication_id)
            if publication is None:
                raise MemoryPublicationTrustAuditUnitOfWorkError("staging memory not found")
        else:
            context = self._records.read(STAGING_PUBLICATION_CONTEXT_COLLECTION, context_id(layer, staged_id))
            if context is None:
                raise MemoryPublicationTrustAuditUnitOfWorkError("staging memory requires canonical manual publication context")
            replacement = (
                staged.payload.get("hierarchy_update")
                or staged.payload.get("external_series_update")
            )
            current = self._records.read(published, staged_id)
            if isinstance(replacement, Mapping) and replacement.get("authority_identity") == "sqlite:structured-records-v1":
                if layer not in {"scenario", "series_memory"} or current is None:
                    raise MemoryPublicationTrustAuditUnitOfWorkError("Memory replacement baseline is missing")
                expected_object_revision = replacement.get("expected_object_revision")
                base_domain_revision = (
                    replacement.get("base_domain_revision")
                    if "base_domain_revision" in replacement
                    else replacement.get("base_series_revision")
                )
                if (
                    expected_object_revision != current.revision
                    or base_domain_revision != current.payload.get("revision")
                    or replacement.get("object_id", staged_id) != staged_id
                ):
                    raise MemoryPublicationTrustAuditUnitOfWorkError(
                        "Memory replacement baseline conflicted"
                    )
                previous = next(
                    (
                        dict(record.payload)
                        for record in self._records.list("memory_publications")
                        if record.payload.get("layer") == layer
                        and record.payload.get("published_object_id") == staged_id
                        and record.payload.get("status") == "published"
                        and record.payload.get("published_revision") == current.payload.get("revision")
                    ),
                    None,
                )
                if previous is None:
                    raise MemoryPublicationTrustAuditUnitOfWorkError("Memory replacement publication baseline is missing")
                value = build_replacement_record(
                    context=_context(context.payload, self._namespace_id, layer, staged_id), namespace_id=self._namespace_id,
                    layer=layer, draft_id=staged_id, revision=_domain_revision(staged.payload),
                    supersedes_publication=previous, published_at=published_at,
                )
                transition = _replacement_transition(layer=layer, object_id=staged_id, previous_revision=_domain_revision(current.payload), publication=value)
                self.replace(layer=layer, staged_id=staged_id, supersedes_publication_id=_required_id(previous, "id"), transition=transition, publication=value)
                if layer == "series_memory":
                    self._record_series_freshness(
                        payload=staged.payload,
                        publication=value,
                    )
                return MemoryPublicationTrustAuditResult(layer, staged_id, _required_id(value, "id"), _required_id(transition, "id"), self._replayed)
            publication = build_record(
                context=_context(context.payload, self._namespace_id, layer, staged_id), namespace_id=self._namespace_id,
                layer=layer, draft_id=staged_id, revision=_domain_revision(staged.payload), published_at=published_at,
            )
        transition = _publish_transition(layer=layer, object_id=staged_id, domain_revision=_required_revision(publication.payload, "published_revision") if hasattr(publication, "payload") else _required_revision(publication, "published_revision"), publication=publication.payload if hasattr(publication, "payload") else publication)
        value = publication.payload if hasattr(publication, "payload") else publication
        self.publish(layer=layer, staged_id=staged_id, transition=transition, publication=value)
        if layer == "series_memory":
            payload = (
                staged.payload
                if staged is not None
                else self._records.read(published, staged_id).payload
            )
            self._record_series_freshness(
                payload=payload,
                publication=value,
            )
        return MemoryPublicationTrustAuditResult(layer, staged_id, _required_id(value, "id"), _required_id(transition, "id"), self._replayed)

    def _record_series_freshness(
        self,
        *,
        payload: Mapping[str, object],
        publication: Mapping[str, object],
    ) -> None:
        series_id = _required_id(payload, "id")
        series_revision = _domain_revision(payload)
        scenario_revisions: list[dict[str, object]] = []
        for scenario_id in payload.get("scenario_ids", ()):
            if not isinstance(scenario_id, str) or not scenario_id:
                raise MemoryPublicationTrustAuditUnitOfWorkError(
                    "Series freshness Scenario identity is invalid"
                )
            scenario = self._records.read("memory_scenarios", scenario_id)
            if scenario is None:
                raise MemoryPublicationTrustAuditUnitOfWorkError(
                    "Series freshness Scenario is missing"
                )
            scenario_revisions.append(
                {
                    "scenario_id": scenario_id,
                    "revision": _domain_revision(scenario.payload),
                    "object_revision": scenario.revision,
                }
            )
        receipt_id = _revision_id(series_id, series_revision)
        _put_append_only(
            self._records,
            "memory_series_freshness_receipts",
            receipt_id,
            {
                "schema_version": "1.0.0",
                "id": receipt_id,
                "series_object_id": series_id,
                "series_revision": series_revision,
                "publication_id": _required_id(publication, "id"),
                "scenario_revisions": scenario_revisions,
                "created_at": _required_str(publication, "published_at"),
            },
        )

    def rollback_user_confirmed(self, *, publication_id: str, reason: str, rolled_back_at: str) -> MemoryPublicationTrustAuditResult:
        publication_record = self._records.read("memory_publications", publication_id)
        if publication_record is None:
            raise MemoryPublicationTrustAuditUnitOfWorkError("memory publication not found")
        publication = dict(publication_record.payload)
        layer = _required_str(publication, "layer")
        object_id = _required_id(publication, "published_object_id")
        transition_id = f"transition-memory-rollback-{publication_id}"
        if publication.get("status") == "rolled_back":
            transition = self._records.read("memory_transitions", transition_id)
            if transition is None:
                raise MemoryPublicationTrustAuditUnitOfWorkError("rollback replay transition is missing")
            self.rollback(layer=layer, publication_id=publication_id, transition=transition.payload, publication=publication)
            return MemoryPublicationTrustAuditResult(layer, object_id, publication_id, transition_id, self._replayed)
        current = self._records.read(_collections(layer)[1], object_id)
        if current is None:
            raise MemoryPublicationTrustAuditUnitOfWorkError("published memory is missing")
        revision = _domain_revision(current.payload)
        transition = {
            "schema_version": "1.0.0", "id": transition_id, "object_type": layer, "object_id": object_id,
            "transition_type": "demote", "from_trust_status": "user_confirmed", "to_trust_status": "system_generated",
            "from_revision": revision, "to_revision": revision + 1, "actor": "user", "reason": reason,
            "evidence_refs": [{"object_type": layer, "object_id": object_id, "source_refs": _refs(publication, "source_refs")}],
            "created_at": rolled_back_at,
        }
        updated = _rollback_publication(original=publication, layer=layer, object_id=object_id, domain_revision=revision, transition=transition, namespace_id=self._namespace_id)
        self.rollback(layer=layer, publication_id=publication_id, transition=transition, publication=updated)
        return MemoryPublicationTrustAuditResult(layer, object_id, publication_id, transition_id, self._replayed)

    def commit(self) -> MemoryPublicationTrustAuditCommit:
        return MemoryPublicationTrustAuditCommit(self._records.commit(), replayed=self._replayed)

    def _assert_publish_replay(
        self,
        layer: str,
        staged_id: str,
        published: str,
        revisions: str,
        transition: Mapping[str, object],
        publication: Mapping[str, object],
    ) -> None:
        target = self._records.read(published, staged_id)
        publication_id = _required_id(publication, "id")
        transition_id = _required_id(transition, "id")
        existing_transition = self._records.read("memory_transitions", transition_id)
        existing_publication = self._records.read("memory_publications", publication_id)
        if (
            target is None
            or existing_transition is None
            or existing_publication is None
            or self._records.read(STAGING_PUBLICATION_CONTEXT_COLLECTION, context_id(layer, staged_id)) is not None
            or dict(existing_transition.payload) != dict(transition)
            or dict(existing_publication.payload) != dict(publication)
        ):
            raise MemoryPublicationTrustAuditUnitOfWorkError("publication replay evidence conflicts")
        domain_revision = _domain_revision(target.payload)
        expected_revision = _published_revision(
            layer=layer,
            object_id=staged_id,
            domain_revision=domain_revision,
            publication=publication,
            payload=target.payload,
        )
        existing_revision = self._records.read(revisions, _revision_id(staged_id, domain_revision))
        if existing_revision is None or dict(existing_revision.payload) != expected_revision:
            raise MemoryPublicationTrustAuditUnitOfWorkError("publication replay revision conflicts")

    def _assert_replace_replay(
        self,
        layer: str,
        staged_id: str,
        published: str,
        revisions: str,
        supersedes_publication_id: str,
        transition: Mapping[str, object],
        publication: Mapping[str, object],
    ) -> None:
        current = self._records.read(published, staged_id)
        replacement = self._records.read("memory_publications", _required_id(publication, "id"))
        previous = self._records.read("memory_publications", supersedes_publication_id)
        existing_transition = self._records.read("memory_transitions", _required_id(transition, "id"))
        revision = _required_revision(publication, "published_revision")
        immutable = self._records.read(revisions, _revision_id(staged_id, revision))
        if (
            current is None
            or replacement is None
            or previous is None
            or existing_transition is None
            or immutable is None
            or self._records.read(STAGING_PUBLICATION_CONTEXT_COLLECTION, context_id(layer, staged_id)) is not None
            or dict(replacement.payload) != dict(publication)
            or dict(existing_transition.payload) != dict(transition)
            or current.payload.get("revision") != revision
            or immutable.payload.get("payload") != current.payload
            or previous.payload.get("status") != "superseded"
            or previous.payload.get("superseded_by_publication_id") != publication.get("id")
        ):
            raise MemoryPublicationTrustAuditUnitOfWorkError("replacement replay evidence conflicts")

    def _assert_rollback_replay(
        self,
        layer: str,
        published: str,
        revisions: str,
        object_id: str,
        transition_id: str,
        publication_id: str,
        transition: Mapping[str, object],
        publication: Mapping[str, object],
    ) -> None:
        if self._records.read(published, object_id) is not None:
            raise MemoryPublicationTrustAuditUnitOfWorkError("rollback replay object state conflicts")
        existing_transition = self._records.read("memory_transitions", transition_id)
        existing_publication = self._records.read("memory_publications", publication_id)
        if (
            existing_transition is None
            or existing_publication is None
            or dict(existing_transition.payload) != dict(transition)
            or dict(existing_publication.payload) != dict(publication)
        ):
            raise MemoryPublicationTrustAuditUnitOfWorkError("rollback replay evidence conflicts")
        rollback_revision = _required_revision(publication, "rollback_revision")
        expected_revision = _rolled_back_revision(
            layer=layer,
            object_id=object_id,
            domain_revision=rollback_revision,
            previous_revision_id=_revision_id(object_id, rollback_revision - 1),
            publication=publication,
            payload=_snapshot_from_rollback_revision(self._records, revisions, object_id, rollback_revision),
        )
        existing_revision = self._records.read(revisions, _revision_id(object_id, rollback_revision))
        if existing_revision is None or dict(existing_revision.payload) != expected_revision:
            raise MemoryPublicationTrustAuditUnitOfWorkError("rollback replay revision conflicts")
        _enqueue_external_agent_memory_invalidation(
            self._records,
            layer=layer,
            payload=_snapshot_from_rollback_revision(
                self._records, revisions, object_id, rollback_revision,
            ),
            publication=publication,
            transition=transition,
        )


def _collections(layer: str) -> tuple[str, str, str]:
    try:
        return _LAYERS[layer]
    except KeyError as exc:
        raise MemoryPublicationTrustAuditUnitOfWorkError("memory publication layer is not adapter-owned") from exc


def _context(value: Mapping[str, object], namespace_id: str, layer: str, object_id: str) -> dict[str, object]:
    try:
        return validate_context(value, namespace_id=namespace_id, layer=layer, draft_id=object_id)
    except ManualPublicationContractError as error:
        raise MemoryPublicationTrustAuditUnitOfWorkError(str(error)) from error


def _publication(
    context: Mapping[str, object],
    *,
    namespace_id: str,
    layer: str,
    object_id: str,
    domain_revision: int,
    publication: Mapping[str, object],
) -> dict[str, object]:
    published_at = _required_str(publication, "published_at")
    try:
        expected = build_record(
            context=context,
            namespace_id=namespace_id,
            layer=layer,
            draft_id=object_id,
            revision=domain_revision,
            published_at=published_at,
        )
    except ManualPublicationContractError as error:
        raise MemoryPublicationTrustAuditUnitOfWorkError(str(error)) from error
    if dict(publication) != expected:
        raise MemoryPublicationTrustAuditUnitOfWorkError("publication evidence conflicts")
    return expected


def _replacement_publication(
    context: Mapping[str, object],
    *,
    namespace_id: str,
    layer: str,
    object_id: str,
    domain_revision: int,
    previous: Mapping[str, object],
    publication: Mapping[str, object],
) -> dict[str, object]:
    try:
        expected = build_replacement_record(
            context=context,
            namespace_id=namespace_id,
            layer=layer,
            draft_id=object_id,
            revision=domain_revision,
            supersedes_publication=previous,
            published_at=_required_str(publication, "published_at"),
        )
    except ManualPublicationContractError as error:
        raise MemoryPublicationTrustAuditUnitOfWorkError(str(error)) from error
    if dict(publication) != expected:
        raise MemoryPublicationTrustAuditUnitOfWorkError("replacement publication evidence conflicts")
    return expected


def _superseded_publication(
    previous: Mapping[str, object], replacement: Mapping[str, object]
) -> dict[str, object]:
    return {
        **dict(previous),
        "status": "superseded",
        "superseded_by_publication_id": _required_id(replacement, "publication_id"),
        "superseded_at": _required_str(replacement, "published_at"),
        "superseded_revision": _required_revision(replacement, "published_revision"),
    }


def _published_payload(staged: Mapping[str, object], publication: Mapping[str, object]) -> dict[str, object]:
    payload = dict(staged)
    payload.pop("external_series_update", None)
    payload.pop("hierarchy_update", None)
    payload.update(
        {
            "trust_status": "user_confirmed",
            "updated_at": _required_str(publication, "published_at"),
        }
    )
    return payload


def _enqueue_external_agent_publication_changes(
    records: SQLiteStructuredRecordUnitOfWork,
    *,
    layer: str,
    payload: Mapping[str, object],
    publication: Mapping[str, object],
) -> None:
    """Append project-visible Memory changes inside the owning UoW.

    Atoms are intentionally omitted: an atom has no independent project
    membership.  A later Scenario publication that binds the atom to a
    project is the project-visible change and therefore the only safe time to
    notify that project's external-context sessions.
    """

    publication_id = _required_id(publication, "publication_id")
    object_id = _required_id(publication, "published_object_id")
    revision = _required_revision(publication, "published_revision")
    occurred_at = _required_str(publication, "published_at")
    for project_id in _project_ids_for_published_memory(layer, payload):
        try:
            ExternalAgentPublicationChangeOutbox.enqueue(
                records,
                publication_identity=publication_id,
                project_id=project_id,
                change_type="memory.published",
                object_ref=f"crp://memory/{project_id}/{object_id}",
                object_revision=f"r{revision}",
                occurred_at=occurred_at,
            )
        except ExternalAgentPublicationChangeError as error:
            raise MemoryPublicationTrustAuditUnitOfWorkError(
                f"external Agent publication change is invalid: {error}"
            ) from error


def _enqueue_external_agent_memory_invalidation(
    records: SQLiteStructuredRecordUnitOfWork,
    *,
    layer: str,
    payload: Mapping[str, object],
    publication: Mapping[str, object],
    transition: Mapping[str, object],
) -> None:
    """Record the stable rollback transition as an external-context invalidation.

    The transition, publication update, current-projection removal and outbox
    row share one SQLite UoW.  Replaying a completed rollback repeats this
    exact enqueue and therefore verifies, rather than recreates, the event.
    """

    transition_id = _required_id(transition, "id")
    object_id = _required_id(publication, "published_object_id")
    revision = _required_revision(publication, "rollback_revision")
    occurred_at = _required_str(transition, "created_at")
    for project_id in _project_ids_for_published_memory(layer, payload):
        try:
            ExternalAgentPublicationChangeOutbox.enqueue(
                records,
                publication_identity=transition_id,
                project_id=project_id,
                change_type="memory.invalidated",
                object_ref=f"crp://memory/{project_id}/{object_id}",
                object_revision=f"r{revision}",
                occurred_at=occurred_at,
            )
        except ExternalAgentPublicationChangeError as error:
            raise MemoryPublicationTrustAuditUnitOfWorkError(
                f"external Agent memory invalidation is invalid: {error}"
            ) from error


def _project_ids_for_published_memory(
    layer: str,
    payload: Mapping[str, object],
) -> tuple[str, ...]:
    if layer == "atom":
        return ()
    if layer == "scenario":
        # Historical generic fixtures predate project bindings.  They remain
        # publishable, but are deliberately invisible to external project
        # sessions until a project-scoped Scenario revision exists.
        if "project_id" not in payload:
            return ()
        return (_required_id(payload, "project_id"),)
    if layer == "series_memory":
        project_ids = payload.get("project_ids")
        if project_ids is None:
            return ()
        if not isinstance(project_ids, list) or not project_ids:
            raise MemoryPublicationTrustAuditUnitOfWorkError(
                "published series Memory project bindings are required"
            )
        normalized = tuple(_required_id({"project_id": value}, "project_id") for value in project_ids)
        if len(set(normalized)) != len(normalized):
            raise MemoryPublicationTrustAuditUnitOfWorkError(
                "published series Memory project bindings are duplicated"
            )
        return normalized
    raise MemoryPublicationTrustAuditUnitOfWorkError(
        "memory publication layer is not project-visible"
    )


def _published_revision(
    *,
    layer: str,
    object_id: str,
    domain_revision: int,
    publication: Mapping[str, object],
    payload: Mapping[str, object],
    previous_revision_id: str | None = None,
) -> dict[str, object]:
    _require_publication_facts(publication, layer=layer, object_id=object_id, domain_revision=domain_revision)
    snapshot = dict(payload)
    if snapshot.get("trust_status") != "user_confirmed" or _domain_revision(snapshot) != domain_revision:
        raise MemoryPublicationTrustAuditUnitOfWorkError("published memory payload conflicts")
    result = {
        "schema_version": "1.0.0",
        "id": _revision_id(object_id, domain_revision),
        "layer": layer,
        "object_id": object_id,
        "revision": domain_revision,
        "state": "published",
        "trust_status": "user_confirmed",
        "publication_id": _required_id(publication, "publication_id"),
        "source_candidate_id": _required_id(publication, "source_candidate_id"),
        "review_ref": _required_str(publication, "review_ref"),
        "reviewer": _required_str(publication, "reviewer"),
        "reviewed_at": _required_str(publication, "reviewed_at"),
        "policy_id": _required_str(publication, "policy_id"),
        "source_refs": _refs(publication, "source_refs"),
        "evidence_refs": _refs(publication, "evidence_refs"),
        "published_at": _required_str(publication, "published_at"),
        "payload": snapshot,
    }
    if domain_revision > 1:
        if previous_revision_id != _revision_id(object_id, domain_revision - 1):
            raise MemoryPublicationTrustAuditUnitOfWorkError("replacement revision predecessor conflicts")
        result["previous_revision_id"] = previous_revision_id
    elif previous_revision_id is not None:
        raise MemoryPublicationTrustAuditUnitOfWorkError("initial revision cannot have a predecessor")
    return result


def _rollback_publication(
    *,
    original: Mapping[str, object],
    layer: str,
    object_id: str,
    domain_revision: int,
    transition: Mapping[str, object],
    namespace_id: str,
) -> dict[str, object]:
    _require_publication_facts(original, layer=layer, object_id=object_id, domain_revision=domain_revision)
    transition_id = _required_id(transition, "id")
    rolled_back_at = _required_str(transition, "created_at")
    reason = _required_str(transition, "reason")
    return {
        **dict(original),
        "status": "rolled_back",
        "rollback_reason": reason,
        "rolled_back_by": "user",
        "rolled_back_at": rolled_back_at,
        "rollback_transition_ref": f"crp://{namespace_id}/memory-transitions/{transition_id}.json",
        "rollback_revision": domain_revision + 1,
    }


def _rollback_transition(
    *,
    layer: str,
    object_id: str,
    domain_revision: int,
    publication: Mapping[str, object],
    transition_id: str,
) -> dict[str, object]:
    publication_id = _required_id(publication, "publication_id")
    if transition_id != f"transition-memory-rollback-{publication_id}":
        raise MemoryPublicationTrustAuditUnitOfWorkError("rollback transition identity conflicts")
    return {
        "schema_version": "1.0.0",
        "id": transition_id,
        "object_type": layer,
        "object_id": object_id,
        "transition_type": "demote",
        "from_trust_status": "user_confirmed",
        "to_trust_status": "system_generated",
        "from_revision": domain_revision,
        "to_revision": domain_revision + 1,
        "actor": "user",
        "reason": _required_str(publication, "rollback_reason"),
        "evidence_refs": [
            {
                "object_type": layer,
                "object_id": object_id,
                "source_refs": _refs(publication, "source_refs"),
            }
        ],
        "created_at": _required_str(publication, "rolled_back_at"),
    }


def _rolled_back_revision(
    *,
    layer: str,
    object_id: str,
    domain_revision: int,
    previous_revision_id: str,
    publication: Mapping[str, object],
    payload: Mapping[str, object],
) -> dict[str, object]:
    if domain_revision < 2:
        raise MemoryPublicationTrustAuditUnitOfWorkError("rollback revision must advance the published revision")
    snapshot = {
        **dict(payload),
        "revision": domain_revision,
        "trust_status": "system_generated",
        "updated_at": _required_str(publication, "rolled_back_at"),
    }
    return {
        "schema_version": "1.0.0",
        "id": _revision_id(object_id, domain_revision),
        "layer": layer,
        "object_id": object_id,
        "revision": domain_revision,
        "state": "rolled_back",
        "trust_status": "system_generated",
        "previous_revision_id": previous_revision_id,
        "publication_id": _required_id(publication, "publication_id"),
        "source_candidate_id": _required_id(publication, "source_candidate_id"),
        "review_ref": _required_str(publication, "review_ref"),
        "reviewer": _required_str(publication, "reviewer"),
        "reviewed_at": _required_str(publication, "reviewed_at"),
        "policy_id": _required_str(publication, "policy_id"),
        "source_refs": _refs(publication, "source_refs"),
        "evidence_refs": _refs(publication, "evidence_refs"),
        "published_at": _required_str(publication, "published_at"),
        "rolled_back_at": _required_str(publication, "rolled_back_at"),
        "rolled_back_by": _required_str(publication, "rolled_back_by"),
        "rollback_reason": _required_str(publication, "rollback_reason"),
        "payload": snapshot,
    }


def _publish_transition(
    *,
    layer: str,
    object_id: str,
    domain_revision: int,
    publication: Mapping[str, object],
) -> dict[str, object]:
    transition_id = _transition_id(publication, "transition_ref")
    return {
        "schema_version": "1.0.0",
        "id": transition_id,
        "object_type": layer,
        "object_id": object_id,
        "transition_type": "confirm",
        "from_trust_status": "system_generated",
        "to_trust_status": "user_confirmed",
        "from_revision": 0,
        "to_revision": domain_revision,
        "actor": "user",
        "reason": _required_str(publication, "reason"),
        "evidence_refs": [
            {
                "object_type": layer,
                "object_id": object_id,
                "source_refs": _refs(publication, "source_refs"),
            }
        ],
        "created_at": _required_str(publication, "published_at"),
    }


def _replacement_transition(
    *,
    layer: str,
    object_id: str,
    previous_revision: int,
    publication: Mapping[str, object],
) -> dict[str, object]:
    transition_id = _transition_id(publication, "transition_ref")
    return {
        "schema_version": "1.0.0",
        "id": transition_id,
        "object_type": layer,
        "object_id": object_id,
        "transition_type": "confirm",
        "from_trust_status": "user_confirmed",
        "to_trust_status": "user_confirmed",
        "from_revision": previous_revision,
        "to_revision": _required_revision(publication, "published_revision"),
        "actor": "user",
        "reason": _required_str(publication, "reason"),
        "evidence_refs": [
            {
                "object_type": layer,
                "object_id": object_id,
                "source_refs": _refs(publication, "source_refs"),
            }
        ],
        "created_at": _required_str(publication, "published_at"),
    }


def _snapshot_from_rollback_revision(
    records: SQLiteStructuredRecordUnitOfWork,
    revisions: str,
    object_id: str,
    rollback_revision: int,
) -> Mapping[str, object]:
    previous = records.read(revisions, _revision_id(object_id, rollback_revision - 1))
    if previous is None:
        raise MemoryPublicationTrustAuditUnitOfWorkError("rollback replay prior revision is missing")
    payload = previous.payload.get("payload")
    if not isinstance(payload, Mapping):
        raise MemoryPublicationTrustAuditUnitOfWorkError("rollback replay prior payload is missing")
    return dict(payload)


def _require_publication_facts(
    publication: Mapping[str, object],
    *,
    layer: str,
    object_id: str,
    domain_revision: int,
) -> None:
    if (
        _required_id(publication, "id") != _required_id(publication, "publication_id")
        or _required_str(publication, "layer") != layer
        or _required_id(publication, "published_object_id") != object_id
        or _required_revision(publication, "published_revision") != domain_revision
        or _required_str(publication, "status") not in {"published", "rolled_back"}
    ):
        raise MemoryPublicationTrustAuditUnitOfWorkError("publication facts conflict")
    for key in ("review_ref", "reviewer", "reviewed_at", "policy_id", "published_at"):
        _required_str(publication, key)
    _required_id(publication, "source_candidate_id")
    _refs(publication, "source_refs")
    _refs(publication, "evidence_refs")


def _revision_id(object_id: str, revision: int) -> str:
    if revision < 1:
        raise MemoryPublicationTrustAuditUnitOfWorkError("memory revision must be positive")
    return f"{object_id}~r{revision}"


def _transition_id(publication: Mapping[str, object], key: str) -> str:
    reference = _required_str(publication, key)
    suffix = reference.rsplit("/", 1)[-1].removesuffix(".json")
    return _required_id({"id": suffix}, "id")


def _domain_revision(value: Mapping[str, object]) -> int:
    return _required_revision(value, "revision")


def _required_revision(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise MemoryPublicationTrustAuditUnitOfWorkError(f"{key} must be a positive integer")
    return value


def _required_id(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise MemoryPublicationTrustAuditUnitOfWorkError(f"{key} is required")
    return value


def _required_str(payload: Mapping[str, object], key: str) -> str:
    return _required_id(payload, key)


def _refs(payload: Mapping[str, object], key: str) -> list[dict[str, object]]:
    value = payload.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(item, Mapping) for item in value):
        raise MemoryPublicationTrustAuditUnitOfWorkError(f"{key} is required")
    return [dict(item) for item in value]


def _put_append_only(
    records: SQLiteStructuredRecordUnitOfWork,
    collection: str,
    object_id: str,
    payload: Mapping[str, object],
) -> None:
    if payload.get("id") != object_id:
        raise MemoryPublicationTrustAuditUnitOfWorkError("append-only record id does not match payload")
    existing = records.read(collection, object_id)
    if existing is not None:
        if dict(existing.payload) != dict(payload):
            raise MemoryPublicationTrustAuditUnitOfWorkError("append-only record conflicts")
        return
    records.put(collection, object_id, payload, expected_revision=0)
