from __future__ import annotations

import pytest

from core.memory_core import (
    build_manual_publication_context,
    build_manual_publication_record,
    build_manual_publication_replacement_record,
    manual_publication_context_id,
)
from core.memory_core.publication_trust_audit_uow import (
    MemoryPublicationTrustAuditUnitOfWorkError,
    SQLiteMemoryPublicationTrustAuditUnitOfWork,
)
from core.storage_provider import SQLiteStructuredRecordStore
from core.storage_provider.external_agent_publication_change import (
    publication_outbox_collection,
)


_LAYERS = (
    ("atom", "staging_atoms", "memory_atoms", "memory_atom_revisions"),
    ("scenario", "staging_scenarios", "memory_scenarios", "memory_scenario_revisions"),
    ("series_memory", "staging_series_memory", "memory_series_memory", "memory_series_memory_revisions"),
)
_PUBLISHED_AT = "2026-07-12T12:05:00+08:00"
_ROLLED_BACK_AT = "2026-07-12T12:10:00+08:00"


def _staged(layer: str, object_id: str, revision: int = 1) -> dict[str, object]:
    payload = {
        "id": object_id,
        "revision": revision,
        "trust_status": "system_generated",
        "source_refs": [{"source_id": "source-uow", "locator": "text:0"}],
        "layer": layer,
        "created_at": "2026-07-12T12:00:00+08:00",
        "updated_at": "2026-07-12T12:00:00+08:00",
    }
    if layer == "scenario":
        payload["project_id"] = "project-a"
    elif layer == "series_memory":
        payload["project_ids"] = ["project-a"]
    return payload


def _context(layer: str, object_id: str) -> dict[str, object]:
    refs = [{"source_id": "source-uow", "locator": "text:0"}]
    return build_manual_publication_context(
        namespace_id="default",
        layer=layer,
        draft_id=object_id,
        candidate_id=f"candidate-{layer}-001",
        reviewed_at="2026-07-12T12:01:00+08:00",
        review_reason="用户确认候选进入长期记忆。",
        source_refs=refs,
        evidence_refs=refs,
    )


def _publication(layer: str, object_id: str, revision: int = 1) -> dict[str, object]:
    return build_manual_publication_record(
        context=_context(layer, object_id),
        namespace_id="default",
        layer=layer,
        draft_id=object_id,
        revision=revision,
        published_at=_PUBLISHED_AT,
    )


def _publish_transition(layer: str, object_id: str, publication: dict[str, object]) -> dict[str, object]:
    transition_id = str(publication["transition_ref"]).rsplit("/", 1)[-1].removesuffix(".json")
    return {
        "schema_version": "1.0.0",
        "id": transition_id,
        "object_type": layer,
        "object_id": object_id,
        "transition_type": "confirm",
        "from_trust_status": "system_generated",
        "to_trust_status": "user_confirmed",
        "from_revision": 0,
        "to_revision": publication["published_revision"],
        "actor": "user",
        "reason": publication["reason"],
        "evidence_refs": [
            {
                "object_type": layer,
                "object_id": object_id,
                "source_refs": publication["source_refs"],
            }
        ],
        "created_at": publication["published_at"],
    }


def _replacement_transition(layer: str, object_id: str, publication: dict[str, object]) -> dict[str, object]:
    transition_id = str(publication["transition_ref"]).rsplit("/", 1)[-1].removesuffix(".json")
    return {
        "schema_version": "1.0.0",
        "id": transition_id,
        "object_type": layer,
        "object_id": object_id,
        "transition_type": "confirm",
        "from_trust_status": "user_confirmed",
        "to_trust_status": "user_confirmed",
        "from_revision": publication["published_revision"] - 1,
        "to_revision": publication["published_revision"],
        "actor": "user",
        "reason": publication["reason"],
        "evidence_refs": [{"object_type": layer, "object_id": object_id, "source_refs": publication["source_refs"]}],
        "created_at": publication["published_at"],
    }


def _rollback_evidence(
    layer: str,
    object_id: str,
    publication: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    transition_id = f"transition-memory-rollback-{publication['publication_id']}"
    transition = {
        "schema_version": "1.0.0",
        "id": transition_id,
        "object_type": layer,
        "object_id": object_id,
        "transition_type": "demote",
        "from_trust_status": "user_confirmed",
        "to_trust_status": "system_generated",
        "from_revision": publication["published_revision"],
        "to_revision": publication["published_revision"] + 1,
        "actor": "user",
        "reason": "用户撤回该长期记忆。",
        "evidence_refs": [
            {
                "object_type": layer,
                "object_id": object_id,
                "source_refs": publication["source_refs"],
            }
        ],
        "created_at": _ROLLED_BACK_AT,
    }
    return transition, {
        **publication,
        "status": "rolled_back",
        "rollback_reason": transition["reason"],
        "rolled_back_by": "user",
        "rolled_back_at": _ROLLED_BACK_AT,
        "rollback_transition_ref": f"crp://default/memory-transitions/{transition_id}.json",
        "rollback_revision": publication["published_revision"] + 1,
    }


def _stage(records: SQLiteStructuredRecordStore, layer: str, staging: str, object_id: str) -> None:
    with records.begin() as transaction:
        transaction.put(staging, object_id, _staged(layer, object_id), expected_revision=0)
        transaction.put(
            "staging_memory_publication_contexts",
            manual_publication_context_id(layer, object_id),
            _context(layer, object_id),
            expected_revision=0,
        )
        transaction.commit()


@pytest.mark.parametrize("layer,staging,published,revisions", _LAYERS)
def test_publish_moves_staging_and_records_initial_immutable_revision(
    tmp_path, layer, staging, published, revisions
):
    db = tmp_path / "structured-records.sqlite3"
    records = SQLiteStructuredRecordStore(db)
    object_id = f"{layer}-uow-001"
    _stage(records, layer, staging, object_id)
    publication = _publication(layer, object_id)
    transition = _publish_transition(layer, object_id, publication)

    with SQLiteMemoryPublicationTrustAuditUnitOfWork(db).begin() as transaction:
        transaction.publish(layer=layer, staged_id=object_id, transition=transition, publication=publication)
        result = transaction.commit()

    assert result.replayed is False
    assert records.read(staging, object_id) is None
    assert records.read("staging_memory_publication_contexts", manual_publication_context_id(layer, object_id)) is None
    current = records.read(published, object_id)
    assert current is not None
    assert current.payload["trust_status"] == "user_confirmed"
    assert current.payload["updated_at"] == _PUBLISHED_AT
    revision = records.read(revisions, f"{object_id}~r1")
    assert revision is not None
    assert revision.payload["state"] == "published"
    assert revision.payload["source_candidate_id"] == f"candidate-{layer}-001"
    assert revision.payload["payload"] == current.payload
    assert records.read("memory_transitions", transition["id"]).payload == transition
    assert records.read("memory_publications", publication["id"]).payload == publication


@pytest.mark.parametrize("layer,staging", (("scenario", "staging_scenarios"), ("series_memory", "staging_series_memory")))
def test_publish_enqueues_project_scoped_external_agent_change_in_same_uow(
    tmp_path, layer, staging
):
    db = tmp_path / "structured-records.sqlite3"
    records = SQLiteStructuredRecordStore(db)
    object_id = f"{layer}-outbox-001"
    _stage(records, layer, staging, object_id)
    publication = _publication(layer, object_id)

    with SQLiteMemoryPublicationTrustAuditUnitOfWork(db).begin() as transaction:
        transaction.publish(
            layer=layer,
            staged_id=object_id,
            transition=_publish_transition(layer, object_id, publication),
            publication=publication,
        )
        transaction.commit()

    persisted = records.read(
        publication_outbox_collection("project-a"), str(publication["id"])
    )
    assert persisted is not None
    assert persisted.payload["state"] == "pending"
    assert persisted.payload["event"] == {
        "publication_identity": publication["id"],
        "project_id": "project-a",
        "change_type": "memory.published",
        "object_ref": f"crp://memory/project-a/{object_id}",
        "object_revision": "r1",
        "occurred_at": _PUBLISHED_AT,
    }


def test_publication_rollback_discards_pending_external_agent_change(tmp_path):
    db = tmp_path / "structured-records.sqlite3"
    records = SQLiteStructuredRecordStore(db)
    object_id = "scenario-outbox-rollback"
    _stage(records, "scenario", "staging_scenarios", object_id)
    publication = _publication("scenario", object_id)

    with SQLiteMemoryPublicationTrustAuditUnitOfWork(db).begin() as transaction:
        transaction.publish(
            layer="scenario",
            staged_id=object_id,
            transition=_publish_transition("scenario", object_id, publication),
            publication=publication,
        )
        # Exit without commit: the Memory transition and its outbox event are
        # one structured SQLite transaction.

    assert records.read("memory_scenarios", object_id) is None
    assert records.read(
        publication_outbox_collection("project-a"), str(publication["id"])
    ) is None


def test_confirmed_memory_rollback_enqueues_stable_invalidation_in_same_uow(tmp_path):
    db = tmp_path / "structured-records.sqlite3"
    records = SQLiteStructuredRecordStore(db)
    object_id = "scenario-outbox-demote"
    _stage(records, "scenario", "staging_scenarios", object_id)
    publication = _publication("scenario", object_id)
    aggregate = SQLiteMemoryPublicationTrustAuditUnitOfWork(db)
    with aggregate.begin() as transaction:
        transaction.publish(
            layer="scenario",
            staged_id=object_id,
            transition=_publish_transition("scenario", object_id, publication),
            publication=publication,
        )
        transaction.commit()

    rollback_transition, rolled_back = _rollback_evidence("scenario", object_id, publication)
    with aggregate.begin() as transaction:
        transaction.rollback(
            layer="scenario",
            publication_id=str(publication["id"]),
            transition=rollback_transition,
            publication=rolled_back,
        )
        transaction.commit()

    outbox_records = records.list(publication_outbox_collection("project-a"))
    assert [record.object_id for record in outbox_records] == [
        publication["id"], rollback_transition["id"],
    ]
    assert outbox_records[0].payload["event"]["change_type"] == "memory.published"
    assert outbox_records[1].payload["event"] == {
        "publication_identity": rollback_transition["id"],
        "project_id": "project-a",
        "change_type": "memory.invalidated",
        "object_ref": f"crp://memory/project-a/{object_id}",
        "object_revision": "r2",
        "occurred_at": _ROLLED_BACK_AT,
    }

    with aggregate.begin() as transaction:
        transaction.rollback(
            layer="scenario",
            publication_id=str(publication["id"]),
            transition=rollback_transition,
            publication=rolled_back,
        )
        assert transaction.commit().replayed is True
    assert len(records.list(publication_outbox_collection("project-a"))) == 2


def test_publish_conflict_rolls_back_current_revision_and_staging_mutations(tmp_path):
    db = tmp_path / "structured-records.sqlite3"
    records = SQLiteStructuredRecordStore(db)
    object_id = "atom-uow-conflict"
    _stage(records, "atom", "staging_atoms", object_id)
    publication = _publication("atom", object_id)
    transition = _publish_transition("atom", object_id, publication)
    with records.begin() as transaction:
        transaction.put(
            "memory_publications",
            str(publication["id"]),
            {"id": publication["id"], "publication_id": publication["id"], "status": "other"},
            expected_revision=0,
        )
        transaction.commit()

    with pytest.raises(MemoryPublicationTrustAuditUnitOfWorkError, match="append-only"):
        with SQLiteMemoryPublicationTrustAuditUnitOfWork(db).begin() as transaction:
            transaction.publish(layer="atom", staged_id=object_id, transition=transition, publication=publication)
            transaction.commit()

    assert records.read("staging_atoms", object_id) is not None
    assert records.read("staging_memory_publication_contexts", manual_publication_context_id("atom", object_id)) is not None
    assert records.read("memory_atoms", object_id) is None
    assert records.read("memory_atom_revisions", f"{object_id}~r1") is None
    assert records.read("memory_transitions", transition["id"]) is None


def test_publish_rejects_context_drift_without_partial_records(tmp_path):
    db = tmp_path / "structured-records.sqlite3"
    records = SQLiteStructuredRecordStore(db)
    object_id = "atom-uow-context-drift"
    _stage(records, "atom", "staging_atoms", object_id)
    context_id = manual_publication_context_id("atom", object_id)
    context = records.read("staging_memory_publication_contexts", context_id)
    assert context is not None
    drifted = dict(context.payload)
    drifted["policy_id"] = "other-policy"
    with records.begin() as transaction:
        transaction.put("staging_memory_publication_contexts", context_id, drifted, expected_revision=context.revision)
        transaction.commit()
    publication = _publication("atom", object_id)
    transition = _publish_transition("atom", object_id, publication)

    with pytest.raises(MemoryPublicationTrustAuditUnitOfWorkError, match="context is invalid"):
        with SQLiteMemoryPublicationTrustAuditUnitOfWork(db).begin() as transaction:
            transaction.publish(layer="atom", staged_id=object_id, transition=transition, publication=publication)

    assert records.read("staging_atoms", object_id) is not None
    assert records.read("memory_atoms", object_id) is None
    assert records.read("memory_atom_revisions", f"{object_id}~r1") is None


def test_publish_strict_replay_does_not_append_another_revision(tmp_path):
    db = tmp_path / "structured-records.sqlite3"
    records = SQLiteStructuredRecordStore(db)
    object_id = "atom-uow-replay"
    _stage(records, "atom", "staging_atoms", object_id)
    publication = _publication("atom", object_id)
    transition = _publish_transition("atom", object_id, publication)
    aggregate = SQLiteMemoryPublicationTrustAuditUnitOfWork(db)
    with aggregate.begin() as transaction:
        transaction.publish(layer="atom", staged_id=object_id, transition=transition, publication=publication)
        transaction.commit()
    with aggregate.begin() as transaction:
        transaction.publish(layer="atom", staged_id=object_id, transition=transition, publication=publication)
        assert transaction.commit().replayed is True
    assert len(records.list("memory_atom_revisions")) == 1

    changed = {**publication, "published_at": "2026-07-12T12:06:00+08:00"}
    with pytest.raises(MemoryPublicationTrustAuditUnitOfWorkError, match="replay"):
        with aggregate.begin() as transaction:
            transaction.publish(
                layer="atom",
                staged_id=object_id,
                transition=_publish_transition("atom", object_id, changed),
                publication=changed,
            )


@pytest.mark.parametrize("layer,staging,published,revisions", _LAYERS)
def test_rollback_appends_history_and_removes_only_current_projection(
    tmp_path, layer, staging, published, revisions
):
    db = tmp_path / "structured-records.sqlite3"
    records = SQLiteStructuredRecordStore(db)
    object_id = f"{layer}-uow-rollback"
    _stage(records, layer, staging, object_id)
    original = _publication(layer, object_id)
    publish_transition = _publish_transition(layer, object_id, original)
    aggregate = SQLiteMemoryPublicationTrustAuditUnitOfWork(db)
    with aggregate.begin() as transaction:
        transaction.publish(layer=layer, staged_id=object_id, transition=publish_transition, publication=original)
        transaction.commit()
    rollback_transition, updated = _rollback_evidence(layer, object_id, original)

    with aggregate.begin() as transaction:
        transaction.rollback(
            layer=layer,
            publication_id=str(original["id"]),
            transition=rollback_transition,
            publication=updated,
        )
        committed = transaction.commit()

    assert committed.replayed is False
    assert records.read(published, object_id) is None
    first = records.read(revisions, f"{object_id}~r1")
    second = records.read(revisions, f"{object_id}~r2")
    assert first is not None
    assert second is not None
    assert first.payload["state"] == "published"
    assert second.payload["state"] == "rolled_back"
    assert second.payload["previous_revision_id"] == f"{object_id}~r1"
    assert second.payload["payload"]["trust_status"] == "system_generated"
    assert records.read("memory_transitions", rollback_transition["id"]).payload == rollback_transition
    assert records.read("memory_publications", original["id"]).payload == updated

    with aggregate.begin() as transaction:
        transaction.rollback(
            layer=layer,
            publication_id=str(original["id"]),
            transition=rollback_transition,
            publication=updated,
        )
        assert transaction.commit().replayed is True
    assert len(records.list(revisions)) == 2


def test_rollback_missing_initial_revision_fails_closed_without_deleting_current(tmp_path):
    db = tmp_path / "structured-records.sqlite3"
    records = SQLiteStructuredRecordStore(db)
    object_id = "atom-uow-missing-history"
    _stage(records, "atom", "staging_atoms", object_id)
    original = _publication("atom", object_id)
    publish_transition = _publish_transition("atom", object_id, original)
    aggregate = SQLiteMemoryPublicationTrustAuditUnitOfWork(db)
    with aggregate.begin() as transaction:
        transaction.publish(layer="atom", staged_id=object_id, transition=publish_transition, publication=original)
        transaction.commit()
    revision = records.read("memory_atom_revisions", f"{object_id}~r1")
    assert revision is not None
    with records.begin() as transaction:
        transaction.delete("memory_atom_revisions", revision.object_id, expected_revision=revision.revision)
        transaction.commit()
    rollback_transition, updated = _rollback_evidence("atom", object_id, original)

    with pytest.raises(MemoryPublicationTrustAuditUnitOfWorkError, match="immutable revision"):
        with aggregate.begin() as transaction:
            transaction.rollback(
                layer="atom",
                publication_id=str(original["id"]),
                transition=rollback_transition,
                publication=updated,
            )

    assert records.read("memory_atoms", object_id) is not None
    assert records.read("memory_publications", original["id"]).payload == original
    assert records.read("memory_transitions", rollback_transition["id"]) is None


def test_series_replacement_appends_revision_and_supersedes_prior_publication_with_strict_replay(tmp_path):
    db = tmp_path / "structured-records.sqlite3"
    records = SQLiteStructuredRecordStore(db)
    object_id = "series-memory-uow-replacement"
    _stage(records, "series_memory", "staging_series_memory", object_id)
    initial = _publication("series_memory", object_id)
    aggregate = SQLiteMemoryPublicationTrustAuditUnitOfWork(db)
    with aggregate.begin() as transaction:
        transaction.publish(
            layer="series_memory", staged_id=object_id,
            transition=_publish_transition("series_memory", object_id, initial), publication=initial,
        )
        transaction.commit()

    replacement_context = _context("series_memory", object_id)
    replacement_context["source_candidate_id"] = "candidate-series-memory-002"
    replacement_context["review_ref"] = "crp://default/memory-candidates/candidate-series-memory-002.json"
    replacement_context["reviewed_at"] = "2026-07-12T12:06:00+08:00"
    replacement_context["review_reason"] = "用户确认替换系列记忆。"
    with records.begin() as transaction:
        staged = _staged("series_memory", object_id, revision=2)
        staged["overview"] = "replacement"
        transaction.put("staging_series_memory", object_id, staged, expected_revision=0)
        transaction.put(
            "staging_memory_publication_contexts", manual_publication_context_id("series_memory", object_id),
            replacement_context, expected_revision=0,
        )
        transaction.commit()
    replacement = build_manual_publication_replacement_record(
        context=replacement_context, namespace_id="default", layer="series_memory", draft_id=object_id,
        revision=2, supersedes_publication=initial, published_at="2026-07-12T12:07:00+08:00",
    )
    transition = _replacement_transition("series_memory", object_id, replacement)
    with aggregate.begin() as transaction:
        transaction.replace(
            layer="series_memory", staged_id=object_id, supersedes_publication_id=str(initial["id"]),
            transition=transition, publication=replacement,
        )
        assert transaction.commit().replayed is False

    current = records.read("memory_series_memory", object_id)
    assert current is not None and current.payload["revision"] == 2
    assert current.payload["overview"] == "replacement"
    assert records.read("memory_series_memory_revisions", f"{object_id}~r2").payload["previous_revision_id"] == f"{object_id}~r1"
    old = records.read("memory_publications", str(initial["id"]))
    assert old is not None and old.payload["status"] == "superseded"
    assert old.payload["superseded_by_publication_id"] == replacement["id"]
    assert records.read("memory_publications", str(replacement["id"])).payload == replacement
    replacement_change = records.read(
        publication_outbox_collection("project-a"), str(replacement["id"])
    )
    assert replacement_change is not None
    assert replacement_change.payload["event"]["object_ref"] == (
        f"crp://memory/project-a/{object_id}"
    )
    assert replacement_change.payload["event"]["object_revision"] == "r2"
    with aggregate.begin() as transaction:
        transaction.replace(
            layer="series_memory", staged_id=object_id, supersedes_publication_id=str(initial["id"]),
            transition=transition, publication=replacement,
        )
        assert transaction.commit().replayed is True
    assert len(records.list("memory_series_memory_revisions")) == 2


def test_project_skill_layer_is_rejected_without_mutating_records(tmp_path):
    publication = _publication("atom", "atom-uow-for-project-skill")
    with pytest.raises(MemoryPublicationTrustAuditUnitOfWorkError, match="adapter-owned"):
        with SQLiteMemoryPublicationTrustAuditUnitOfWork(tmp_path / "structured-records.sqlite3").begin() as transaction:
            transaction.publish(
                layer="project_skill",
                staged_id="skill-uow-001",
                transition=_publish_transition("atom", "atom-uow-for-project-skill", publication),
                publication=publication,
            )
