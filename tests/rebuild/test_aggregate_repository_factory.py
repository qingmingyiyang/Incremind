from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.aggregate_repository_factory import (
    AUTHORITY_DATABASE_NAME,
    JSON_MEMORY_PUBLICATION_AUTHORITY_IDENTITY,
    JSON_SOURCE_ASSET_AUTHORITY_IDENTITY,
    SOURCE_ASSET_AUTHORITY_MEMBERS,
    STRUCTURED_DATABASE_NAME,
    TARGET_IDENTITY,
    AggregateRepositoryFactory,
    AggregateRepositoryFactoryError,
)
from core.composition import build_document_repository, build_project_skill_repository
from core.document_engine import DocumentDraft, ObjectStoreDocumentRepository, SQLiteDocumentRepository
from core.memory_core import (
    shared_trust_audit_activation_id,
    shared_trust_audit_activation_payload,
)
from core.project_skill_core import (
    ObjectStoreProjectSkillRepository,
    ProjectSkillUpdate,
    SQLiteProjectSkillRepository,
)
from core.storage_provider import (
    AggregateAuthorityEvidence,
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteStructuredRecordStore,
)


ROOT = Path(__file__).resolve().parents[2]
SKILL_FIXTURE = ROOT / "core-contracts" / "rebuild" / "fixtures" / "project_skill" / "valid-active-skill.json"
MEMORY_PUBLICATION_MEMBERS = (
    "memory_atoms",
    "memory_publications",
    "memory_scenarios",
    "memory_series_memory",
    "memory_transitions",
    "project_skills",
)


def _evidence() -> AggregateAuthorityEvidence:
    return AggregateAuthorityEvidence(
        migration_id="aggregate-fixture-v1",
        source_fingerprint="a" * 64,
        target_fingerprint="b" * 64,
        target_identity=TARGET_IDENTITY,
    )


def _exact_evidence() -> AggregateAuthorityEvidence:
    return AggregateAuthorityEvidence(
        migration_id="documents-exact-v1",
        source_fingerprint=None,
        target_fingerprint=None,
        target_identity=TARGET_IDENTITY,
        verification_method="exact_records",
        verified_record_count=3,
    )


def _exact_document_authority(tmp_path: Path, records: SQLiteStructuredRecordStore,
                              *, marker_count: int = 3) -> None:
    with records.begin() as uow:
        uow.put("aggregate_authority_targets", "default~documents", {
            "namespace_id": "default", "aggregate": "documents",
            "target_identity": TARGET_IDENTITY, "migration_id": "documents-exact-v1",
            "verification_method": "exact_records", "verified_record_count": marker_count,
        }, expected_revision=0)
        uow.commit()
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    initial = authority.create_json_active(namespace_id="default", aggregate="documents", reason="initial")
    staged = authority.transition(namespace_id="default", aggregate="documents",
                                  expected_revision=initial.revision, to_state="sqlite_staged",
                                  evidence=_exact_evidence(), reason="exact copy checked")
    authority.transition(namespace_id="default", aggregate="documents",
                         expected_revision=staged.revision, to_state="sqlite_active",
                         evidence=_exact_evidence(), reason="activate exact copy")


def _authority(tmp_path: Path, aggregate: str, *, active: bool) -> None:
    store = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    initial = store.create_json_active(namespace_id="default", aggregate=aggregate, reason="initial")
    staged = store.transition(
        namespace_id="default",
        aggregate=aggregate,
        expected_revision=initial.revision,
        to_state="sqlite_staged",
        evidence=_evidence(),
        reason="fixture staged",
    )
    if active:
        store.transition(
            namespace_id="default",
            aggregate=aggregate,
            expected_revision=staged.revision,
            to_state="sqlite_active",
            evidence=_evidence(),
            reason="fixture active",
        )


def _target_records(tmp_path: Path) -> SQLiteStructuredRecordStore:
    return SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)


def _marker(records: SQLiteStructuredRecordStore, aggregate: str, *, fingerprint: str = "b" * 64) -> None:
    marker_id = f"default~{aggregate}"
    with records.begin() as uow:
        uow.put(
            "aggregate_authority_targets",
            marker_id,
            {
                "namespace_id": "default",
                "aggregate": aggregate,
                "target_identity": TARGET_IDENTITY,
                "source_fingerprint": "a" * 64,
                "target_fingerprint": fingerprint,
                "migration_id": "aggregate-fixture-v1",
            },
            expected_revision=0,
        )
        uow.commit()


def _memory_factory(tmp_path: Path) -> AggregateRepositoryFactory:
    return AggregateRepositoryFactory(
        runtime_root=tmp_path,
        namespace_id="default",
        json_store=JsonObjectStore(tmp_path / ".rebuild-data"),
    )


def _compound_activation(records: SQLiteStructuredRecordStore, *, source_fingerprint: str = "a" * 64) -> None:
    payload = shared_trust_audit_activation_payload(
        namespace_id="default",
        target_identity=TARGET_IDENTITY,
        activation_id="memory-publication-compound-v1",
        member_migrations={member: "aggregate-fixture-v1" for member in MEMORY_PUBLICATION_MEMBERS},
        source_fingerprint=source_fingerprint,
        target_fingerprint="b" * 64,
        activated_at="2026-07-12T12:00:00+08:00",
    )
    with records.begin() as uow:
        uow.put(
            "aggregate_authority_compound_activations",
            shared_trust_audit_activation_id("default"),
            payload,
            expected_revision=0,
        )
        uow.commit()


def _activate_memory_publication_compound(tmp_path: Path, records: SQLiteStructuredRecordStore) -> None:
    for member in MEMORY_PUBLICATION_MEMBERS:
        _marker(records, member)
        _authority(tmp_path, member, active=True)
    _compound_activation(records)


def _activate_source_asset_compound(
    tmp_path: Path,
    records: SQLiteStructuredRecordStore,
) -> None:
    for member in SOURCE_ASSET_AUTHORITY_MEMBERS:
        _marker(records, member)
        _authority(tmp_path, member, active=True)


def _draft() -> DocumentDraft:
    return DocumentDraft(
        title="Factory SQLite document",
        document_type="project_doc",
        markdown="Factory selects verified SQLite authority.",
        source_refs=(
            {
                "source_id": "source-factory-001",
                "locator": "char:0-42",
                "quote": "Factory selects verified SQLite authority.",
            },
        ),
    )


def test_default_builders_remain_json_and_do_not_create_authority_database(tmp_path: Path) -> None:
    documents = build_document_repository(ROOT, runtime_root=tmp_path)
    skills = build_project_skill_repository(ROOT, runtime_root=tmp_path)

    assert type(documents) is ObjectStoreDocumentRepository
    assert type(skills) is ObjectStoreProjectSkillRepository
    assert not (tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME).exists()


def test_staged_authority_keeps_json_even_when_verified_target_exists(tmp_path: Path) -> None:
    records = _target_records(tmp_path)
    _marker(records, "documents")
    _authority(tmp_path, "documents", active=False)

    repository = build_document_repository(ROOT, runtime_root=tmp_path)

    assert type(repository) is ObjectStoreDocumentRepository


def test_active_authority_selects_verified_sqlite_document_and_skill_repositories(tmp_path: Path) -> None:
    records = _target_records(tmp_path)
    sqlite_documents = SQLiteDocumentRepository(records)
    document = sqlite_documents.create(_draft())
    structured = json.loads(SKILL_FIXTURE.read_text(encoding="utf-8"))
    sqlite_skills = SQLiteProjectSkillRepository(records)
    skill = sqlite_skills.save(
        ProjectSkillUpdate(
            project_id=str(structured["project_id"]),
            markdown="# Factory Skill",
            structured=structured,
            expected_revision=0,
            reason="user confirmed factory fixture",
        )
    )
    _marker(records, "documents")
    _marker(records, "project_skills")
    _authority(tmp_path, "documents", active=True)
    _authority(tmp_path, "project_skills", active=True)

    documents = build_document_repository(ROOT, runtime_root=tmp_path)
    skills = build_project_skill_repository(ROOT, runtime_root=tmp_path)

    assert type(documents) is SQLiteDocumentRepository
    assert type(skills) is SQLiteProjectSkillRepository
    assert documents.read(str(document["id"])) == document
    assert skills.load(str(skill["project_id"])) == skill


def test_exact_record_document_authority_selects_existing_sqlite_records(tmp_path: Path) -> None:
    records = _target_records(tmp_path)
    document = SQLiteDocumentRepository(records).create(_draft())
    _exact_document_authority(tmp_path, records)

    repository = build_document_repository(ROOT, runtime_root=tmp_path)

    assert type(repository) is SQLiteDocumentRepository
    assert repository.read(str(document["id"])) == document


def test_exact_record_document_authority_fails_closed_on_count_drift(tmp_path: Path) -> None:
    records = _target_records(tmp_path)
    _exact_document_authority(tmp_path, records, marker_count=2)

    with pytest.raises(AggregateRepositoryFactoryError, match="marker does not match"):
        build_document_repository(ROOT, runtime_root=tmp_path)


def test_active_document_authority_archives_and_restores_only_in_sqlite(tmp_path: Path) -> None:
    records = _target_records(tmp_path)
    sqlite_documents = SQLiteDocumentRepository(records)
    document = sqlite_documents.create(_draft())
    document_id = str(document["id"])
    json_store = JsonObjectStore(tmp_path / ".rebuild-data")
    _marker(records, "documents")
    _authority(tmp_path, "documents", active=True)

    active = build_document_repository(ROOT, runtime_root=tmp_path)
    archived = active.archive(document_id, expected_revision=1)
    reopened = build_document_repository(ROOT, runtime_root=tmp_path)
    restored = reopened.restore(document_id, expected_revision=2)

    assert type(active) is SQLiteDocumentRepository
    assert archived["status"] == "archived"
    assert restored["status"] == "draft"
    assert [item["operation"] for item in reopened.revisions(document_id)] == [
        "create",
        "archive",
        "restore",
    ]
    assert json_store.read("documents", document_id) is None
    assert not json_store.list("document_revisions")
    assert not json_store.list("document_markdown")


@pytest.mark.parametrize("failure", ("missing-target", "marker-mismatch", "rollback-required"))
def test_active_or_rollback_authority_fails_closed_without_json_fallback(
    tmp_path: Path,
    failure: str,
) -> None:
    if failure != "missing-target":
        records = _target_records(tmp_path)
        _marker(records, "documents", fingerprint="c" * 64 if failure == "marker-mismatch" else "b" * 64)
    _authority(tmp_path, "documents", active=True)
    if failure == "rollback-required":
        authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
        current = authority.get("default", "documents")
        authority.transition(
            namespace_id="default",
            aggregate="documents",
            expected_revision=current.revision,
            to_state="rollback_required",
            reason="target failed",
        )

    with pytest.raises(AggregateRepositoryFactoryError):
        build_document_repository(ROOT, runtime_root=tmp_path)


def test_memory_publication_resolution_stays_json_until_all_members_are_active(tmp_path: Path) -> None:
    default = _memory_factory(tmp_path).memory_publication_authority_resolution()

    records = _target_records(tmp_path)
    for member in MEMORY_PUBLICATION_MEMBERS:
        _marker(records, member)
        _authority(tmp_path, member, active=False)
    staged = _memory_factory(tmp_path).memory_publication_authority_resolution()

    assert default.records is None
    assert default.authority_identity == JSON_MEMORY_PUBLICATION_AUTHORITY_IDENTITY
    assert staged.records is None
    assert staged.authority_identity == JSON_MEMORY_PUBLICATION_AUTHORITY_IDENTITY


def test_memory_publication_resolution_returns_sqlite_only_for_complete_compound_proof(tmp_path: Path) -> None:
    records = _target_records(tmp_path)
    _activate_memory_publication_compound(tmp_path, records)
    target = tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    before_mtime = target.stat().st_mtime_ns

    resolution = _memory_factory(tmp_path).memory_publication_authority_resolution()

    assert resolution.authority_identity == TARGET_IDENTITY
    assert resolution.records is not None
    assert target.stat().st_mtime_ns == before_mtime
    assert resolution.records.read("aggregate_authority_compound_activations", shared_trust_audit_activation_id("default")) is not None


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("partial-active", "partially SQLite active"),
        ("marker-drift", "target marker does not match"),
        ("attestation-drift", "compound activation evidence does not match"),
        ("rollback-required", "requires verified rollback"),
    ],
)
def test_memory_publication_resolution_fails_closed_for_partial_or_drifted_compound_proof(
    tmp_path: Path,
    failure: str,
    message: str,
) -> None:
    records = _target_records(tmp_path)
    if failure == "partial-active":
        _marker(records, "memory_atoms")
        _authority(tmp_path, "memory_atoms", active=True)
    else:
        _activate_memory_publication_compound(tmp_path, records)
        if failure == "marker-drift":
            marker = records.read("aggregate_authority_targets", "default~memory_atoms")
            assert marker is not None
            with records.begin() as uow:
                uow.put(
                    "aggregate_authority_targets",
                    "default~memory_atoms",
                    {**marker.payload, "target_fingerprint": "c" * 64},
                    expected_revision=marker.revision,
                )
                uow.commit()
        elif failure == "attestation-drift":
            activation = records.read(
                "aggregate_authority_compound_activations",
                shared_trust_audit_activation_id("default"),
            )
            assert activation is not None
            with records.begin() as uow:
                uow.put(
                    "aggregate_authority_compound_activations",
                    shared_trust_audit_activation_id("default"),
                    {**activation.payload, "source_fingerprint": "c" * 64},
                    expected_revision=activation.revision,
                )
                uow.commit()
        elif failure == "rollback-required":
            authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
            active = authority.get("default", "memory_atoms")
            assert active is not None
            authority.transition(
                namespace_id="default",
                aggregate="memory_atoms",
                expected_revision=active.revision,
                to_state="rollback_required",
                reason="fixture target verification failed",
            )

    with pytest.raises(AggregateRepositoryFactoryError, match=message):
        _memory_factory(tmp_path).memory_publication_authority_resolution()


def test_source_asset_resolution_stays_json_until_every_member_is_active(
    tmp_path: Path,
) -> None:
    default = _memory_factory(tmp_path).source_asset_authority_resolution()
    records = _target_records(tmp_path)
    for member in SOURCE_ASSET_AUTHORITY_MEMBERS:
        _marker(records, member)
        _authority(tmp_path, member, active=False)

    staged = _memory_factory(tmp_path).source_asset_authority_resolution()

    assert default.records is None
    assert default.authority_identity == JSON_SOURCE_ASSET_AUTHORITY_IDENTITY
    assert staged.records is None
    assert staged.authority_identity == JSON_SOURCE_ASSET_AUTHORITY_IDENTITY


def test_source_asset_resolution_returns_one_verified_sqlite_target(
    tmp_path: Path,
) -> None:
    records = _target_records(tmp_path)
    with records.begin() as uow:
        uow.put(
            "asset_blobs",
            "a" * 64,
            {"sha256": "a" * 64},
            expected_revision=0,
        )
        uow.commit()
    _activate_source_asset_compound(tmp_path, records)
    target = tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    before_mtime = target.stat().st_mtime_ns

    resolution = _memory_factory(tmp_path).source_asset_authority_resolution()

    assert resolution.authority_identity == TARGET_IDENTITY
    assert resolution.records is not None
    assert resolution.records.read("asset_blobs", "a" * 64) is not None
    assert target.stat().st_mtime_ns == before_mtime


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("partial-active", "partially SQLite active"),
        ("marker-drift", "target marker does not match"),
        ("evidence-drift", "evidence does not match across members"),
        ("rollback-required", "requires verified rollback"),
    ],
)
def test_source_asset_resolution_fails_closed_for_partial_or_drifted_authority(
    tmp_path: Path,
    failure: str,
    message: str,
) -> None:
    records = _target_records(tmp_path)
    if failure == "partial-active":
        member = SOURCE_ASSET_AUTHORITY_MEMBERS[0]
        _marker(records, member)
        _authority(tmp_path, member, active=True)
    else:
        _activate_source_asset_compound(tmp_path, records)
        if failure == "marker-drift":
            member = SOURCE_ASSET_AUTHORITY_MEMBERS[0]
            marker = records.read("aggregate_authority_targets", f"default~{member}")
            assert marker is not None
            with records.begin() as uow:
                uow.put(
                    "aggregate_authority_targets",
                    f"default~{member}",
                    {**marker.payload, "target_fingerprint": "c" * 64},
                    expected_revision=marker.revision,
                )
                uow.commit()
        elif failure == "evidence-drift":
            member = SOURCE_ASSET_AUTHORITY_MEMBERS[-1]
            authority = SQLiteAggregateAuthorityStore(
                tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME
            )
            current = authority.get("default", member)
            assert current is not None
            authority.transition(
                namespace_id="default",
                aggregate=member,
                expected_revision=current.revision,
                to_state="rollback_required",
                reason="replace divergent fixture",
            )
            initial = authority.transition(
                namespace_id="default",
                aggregate=member,
                expected_revision=current.revision + 1,
                to_state="json_active",
                reason="fixture rollback completed",
                rollback_verified=True,
            )
            divergent = AggregateAuthorityEvidence(
                migration_id="aggregate-fixture-v2",
                source_fingerprint="c" * 64,
                target_fingerprint="d" * 64,
                target_identity=TARGET_IDENTITY,
            )
            staged = authority.transition(
                namespace_id="default",
                aggregate=member,
                expected_revision=initial.revision,
                to_state="sqlite_staged",
                evidence=divergent,
                reason="divergent fixture staged",
            )
            authority.transition(
                namespace_id="default",
                aggregate=member,
                expected_revision=staged.revision,
                to_state="sqlite_active",
                evidence=divergent,
                reason="divergent fixture active",
            )
        elif failure == "rollback-required":
            member = SOURCE_ASSET_AUTHORITY_MEMBERS[0]
            authority = SQLiteAggregateAuthorityStore(
                tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME
            )
            current = authority.get("default", member)
            assert current is not None
            authority.transition(
                namespace_id="default",
                aggregate=member,
                expected_revision=current.revision,
                to_state="rollback_required",
                reason="fixture target verification failed",
            )

    with pytest.raises(AggregateRepositoryFactoryError, match=message):
        _memory_factory(tmp_path).source_asset_authority_resolution()
