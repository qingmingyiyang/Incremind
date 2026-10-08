from __future__ import annotations

import sqlite3

import pytest

from core.storage_provider import (
    AggregateAuthorityConflict,
    AggregateAuthorityError,
    AggregateAuthorityEvidence,
    AggregateAuthorityTransition,
    SQLiteAggregateAuthorityStore,
)


def _store(tmp_path):
    return SQLiteAggregateAuthorityStore(tmp_path / "authority" / "aggregate-authority.sqlite3")


def _evidence(suffix: str = "a") -> AggregateAuthorityEvidence:
    return AggregateAuthorityEvidence(
        migration_id="documents-v1",
        source_fingerprint=suffix * 64,
        target_fingerprint="b" * 64,
        target_identity="sqlite:structured-records-v1",
    )


def _exact_evidence(count: int = 3) -> AggregateAuthorityEvidence:
    return AggregateAuthorityEvidence(
        migration_id="documents-exact-v1",
        source_fingerprint=None,
        target_fingerprint=None,
        target_identity="sqlite:structured-records-v1",
        verification_method="exact_records",
        verified_record_count=count,
    )


def test_exact_record_document_evidence_persists_without_fingerprints(tmp_path) -> None:
    store = _store(tmp_path)
    initial = store.create_json_active(namespace_id="default", aggregate="documents", reason="initial")
    staged = store.transition(
        namespace_id="default", aggregate="documents", expected_revision=initial.revision,
        to_state="sqlite_staged", reason="compared exact records", evidence=_exact_evidence(),
    )
    active = store.transition(
        namespace_id="default", aggregate="documents", expected_revision=staged.revision,
        to_state="sqlite_active", reason="activate exact copy", evidence=_exact_evidence(),
    )
    assert active.evidence == _exact_evidence()
    assert _store(tmp_path).get("default", "documents") == active
    with sqlite3.connect(tmp_path / "authority" / "aggregate-authority.sqlite3") as connection:
        row = connection.execute(
            "SELECT source_fingerprint, target_fingerprint, verification_method, verified_record_count "
            "FROM aggregate_authority WHERE aggregate = 'documents'"
        ).fetchone()
    assert row == (None, None, "exact_records", 3)


@pytest.mark.parametrize("evidence", (
    _exact_evidence(-1),
    AggregateAuthorityEvidence("documents-exact-v1", "a" * 64, None,
                               "sqlite:structured-records-v1", "exact_records", 3),
))
def test_exact_record_evidence_rejects_invalid_metadata(tmp_path, evidence) -> None:
    store = _store(tmp_path)
    store.create_json_active(namespace_id="default", aggregate="documents", reason="initial")
    with pytest.raises(AggregateAuthorityError):
        store.transition(namespace_id="default", aggregate="documents", expected_revision=1,
                         to_state="sqlite_staged", reason="invalid", evidence=evidence)


def test_exact_record_evidence_cannot_change_other_aggregate_contract(tmp_path) -> None:
    store = _store(tmp_path)
    store.create_json_active(namespace_id="default", aggregate="project_skills", reason="initial")
    with pytest.raises(AggregateAuthorityError, match="only supported for documents"):
        store.transition(namespace_id="default", aggregate="project_skills", expected_revision=1,
                         to_state="sqlite_staged", reason="invalid", evidence=_exact_evidence())


def test_existing_fingerprint_authority_database_upgrades_in_place(tmp_path) -> None:
    path = tmp_path / "authority" / "aggregate-authority.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE aggregate_authority (namespace_id TEXT, aggregate TEXT, state TEXT, "
            "revision INTEGER, migration_id TEXT, source_fingerprint TEXT, "
            "target_fingerprint TEXT, target_identity TEXT, reason TEXT, "
            "created_at TEXT, updated_at TEXT, PRIMARY KEY(namespace_id, aggregate))"
        )
        connection.execute(
            "INSERT INTO aggregate_authority VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("default", "project_skills", "sqlite_active", 3, "legacy-v1", "a" * 64,
             "b" * 64, "sqlite:structured-records-v1", "legacy", "2026-01-01", "2026-01-01"),
        )
    record = _store(tmp_path).get("default", "project_skills")
    assert record is not None
    assert record.evidence == AggregateAuthorityEvidence(
        "legacy-v1", "a" * 64, "b" * 64, "sqlite:structured-records-v1"
    )


def test_authority_store_persists_full_staged_active_and_verified_rollback_flow(tmp_path) -> None:
    store = _store(tmp_path)
    initial = store.create_json_active(
        namespace_id="default",
        aggregate="documents",
        reason="legacy JSON is current authority",
        now="2026-07-11T10:00:00+00:00",
    )
    staged = store.transition(
        namespace_id="default",
        aggregate="documents",
        expected_revision=1,
        to_state="sqlite_staged",
        evidence=_evidence(),
        reason="verified copy staged",
        now="2026-07-11T10:01:00+00:00",
    )
    active = store.transition(
        namespace_id="default",
        aggregate="documents",
        expected_revision=2,
        to_state="sqlite_active",
        evidence=_evidence(),
        reason="explicit cutover verified",
        now="2026-07-11T10:02:00+00:00",
    )
    required = store.transition(
        namespace_id="default",
        aggregate="documents",
        expected_revision=3,
        to_state="rollback_required",
        reason="active target validation failed",
        now="2026-07-11T10:03:00+00:00",
    )
    rolled_back = store.transition(
        namespace_id="default",
        aggregate="documents",
        expected_revision=4,
        to_state="json_active",
        reason="rollback copy and JSON authority verified",
        rollback_verified=True,
        now="2026-07-11T10:04:00+00:00",
    )

    assert initial.state == "json_active" and initial.revision == 1 and initial.evidence is None
    assert staged.state == "sqlite_staged" and staged.evidence == _evidence()
    assert active.state == "sqlite_active" and active.revision == 3
    assert required.state == "rollback_required" and required.evidence == _evidence()
    assert rolled_back.state == "json_active" and rolled_back.revision == 5
    assert rolled_back.evidence == _evidence()
    assert _store(tmp_path).get("default", "documents") == rolled_back
    assert _store(tmp_path).journal_mode() == "wal"
    assert _store(tmp_path).synchronous_mode() == "full"


def test_authority_store_rejects_duplicate_stale_and_illegal_transitions(tmp_path) -> None:
    store = _store(tmp_path)
    store.create_json_active(
        namespace_id="default",
        aggregate="documents",
        reason="initial",
    )
    with pytest.raises(AggregateAuthorityConflict, match="already exists"):
        store.create_json_active(
            namespace_id="default",
            aggregate="documents",
            reason="duplicate",
        )
    with pytest.raises(AggregateAuthorityError, match="illegal.*json_active -> sqlite_active"):
        store.transition(
            namespace_id="default",
            aggregate="documents",
            expected_revision=1,
            to_state="sqlite_active",
            evidence=_evidence(),
            reason="skip staging",
        )
    store.transition(
        namespace_id="default",
        aggregate="documents",
        expected_revision=1,
        to_state="sqlite_staged",
        evidence=_evidence(),
        reason="stage",
    )
    with pytest.raises(AggregateAuthorityConflict, match="expected revision 1, found 2"):
        store.transition(
            namespace_id="default",
            aggregate="documents",
            expected_revision=1,
            to_state="sqlite_active",
            evidence=_evidence(),
            reason="stale",
        )
    with pytest.raises(AggregateAuthorityError, match="evidence cannot drift"):
        store.transition(
            namespace_id="default",
            aggregate="documents",
            expected_revision=2,
            to_state="sqlite_active",
            evidence=_evidence("c"),
            reason="drift",
        )


def test_active_authority_cannot_silently_fallback_to_json(tmp_path) -> None:
    store = _store(tmp_path)
    store.create_json_active(namespace_id="default", aggregate="project_skills", reason="initial")
    store.transition(
        namespace_id="default",
        aggregate="project_skills",
        expected_revision=1,
        to_state="sqlite_staged",
        evidence=_evidence(),
        reason="stage",
    )
    store.transition(
        namespace_id="default",
        aggregate="project_skills",
        expected_revision=2,
        to_state="sqlite_active",
        reason="activate",
    )

    with pytest.raises(AggregateAuthorityError, match="illegal.*sqlite_active -> json_active"):
        store.transition(
            namespace_id="default",
            aggregate="project_skills",
            expected_revision=3,
            to_state="json_active",
            reason="silent fallback",
            rollback_verified=True,
        )
    required = store.transition(
        namespace_id="default",
        aggregate="project_skills",
        expected_revision=3,
        to_state="rollback_required",
        reason="target unavailable",
    )
    with pytest.raises(AggregateAuthorityError, match="requires rollback_verified"):
        store.transition(
            namespace_id="default",
            aggregate="project_skills",
            expected_revision=required.revision,
            to_state="json_active",
            reason="unverified rollback",
        )
    assert store.get("default", "project_skills").state == "rollback_required"


def test_staged_authority_can_be_cancelled_without_activating_sqlite(tmp_path) -> None:
    store = _store(tmp_path)
    store.create_json_active(namespace_id="default", aggregate="documents", reason="initial")
    staged = store.transition(
        namespace_id="default",
        aggregate="documents",
        expected_revision=1,
        to_state="sqlite_staged",
        evidence=_evidence(),
        reason="stage",
    )
    cancelled = store.transition(
        namespace_id="default",
        aggregate="documents",
        expected_revision=staged.revision,
        to_state="json_active",
        reason="cancel staged cutover",
    )

    assert cancelled.state == "json_active"
    assert cancelled.evidence == _evidence()
    assert store.list_records() == (cancelled,)


def test_staging_rejects_absolute_or_incomplete_target_evidence(tmp_path) -> None:
    store = _store(tmp_path)
    store.create_json_active(namespace_id="default", aggregate="documents", reason="initial")
    unsafe = AggregateAuthorityEvidence(
        migration_id="documents-v1",
        source_fingerprint="a" * 64,
        target_fingerprint="b" * 64,
        target_identity="sqlite:C:/private/documents.sqlite3",
    )

    with pytest.raises(AggregateAuthorityError, match="opaque sqlite identity"):
        store.transition(
            namespace_id="default",
            aggregate="documents",
            expected_revision=1,
            to_state="sqlite_staged",
            evidence=unsafe,
            reason="unsafe absolute target",
        )

    assert store.get("default", "documents").revision == 1


def test_transition_many_is_atomic_when_one_expected_revision_is_stale(tmp_path) -> None:
    store = _store(tmp_path)
    for aggregate in ("documents", "project_skills"):
        store.create_json_active(namespace_id="default", aggregate=aggregate, reason="initial")
        store.transition(
            namespace_id="default",
            aggregate=aggregate,
            expected_revision=1,
            to_state="sqlite_staged",
            evidence=_evidence(),
            reason="staged",
        )

    with pytest.raises(AggregateAuthorityConflict, match="expected revision 1, found 2"):
        store.transition_many(
            (
                AggregateAuthorityTransition(
                    "default",
                    "documents",
                    2,
                    "sqlite_active",
                    "activate documents",
                    _evidence(),
                ),
                AggregateAuthorityTransition(
                    "default",
                    "project_skills",
                    1,
                    "sqlite_active",
                    "stale activation",
                    _evidence(),
                ),
            )
        )

    assert store.get("default", "documents").state == "sqlite_staged"
    assert store.get("default", "project_skills").state == "sqlite_staged"
