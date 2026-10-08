from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from backend.api import memory_publication_effect_runtime as subject
from core.effect_log import EffectState, build_effect_runtime
from core.storage_provider import SQLiteStructuredRecordStore


class _MemoryTransaction:
    def __init__(self, database: Path, calls: list[str]) -> None:
        self._database = database
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def _write(self, collection: str, object_id: str, payload: dict[str, object]) -> None:
        records = SQLiteStructuredRecordStore(self._database)
        with records.begin() as transaction:
            transaction.put(collection, object_id, payload, expected_revision=0)
            transaction.commit()

    def publish_user_confirmed(self, *, layer: str, staged_id: str, published_at: str):
        self._calls.append("memory_publish")
        self._write("memory_publications", "publication-1", {
            "id": "publication-1", "status": "published", "layer": layer,
            "published_object_id": staged_id, "published_at": published_at,
        })
        self._write("memory_transitions", "transition-1", {"id": "transition-1", "object_id": staged_id})
        return SimpleNamespace(layer="atom", object_id="object-1", publication_id="publication-1", transition_id="transition-1")

    def rollback_user_confirmed(self, *, publication_id: str, reason: str, rolled_back_at: str):
        self._calls.append("memory_rollback")
        self._write("memory_publications", publication_id, {
            "id": publication_id, "status": "rolled_back", "layer": "atom",
            "published_object_id": "object-1", "rollback_reason": reason, "rolled_back_at": rolled_back_at,
        })
        self._write("memory_transitions", "transition-2", {
            "id": "transition-2", "object_id": "object-1", "reason": reason, "created_at": rolled_back_at,
        })
        return SimpleNamespace(layer="atom", object_id="object-1", publication_id=publication_id, transition_id="transition-2")

    def commit(self) -> None:
        return None


class _MemoryUnitOfWork:
    def __init__(self, _database: Path, *, namespace_id: str, calls: list[str]) -> None:
        self._transaction = _MemoryTransaction(_database, calls)

    def begin(self):
        return self._transaction


class _SkillTransaction(_MemoryTransaction):
    def staged_draft(self, _object_id: str):
        return {"id": "object-1"}

    def publish(self, **_kwargs):
        self._calls.append("project_skill_publish")
        self._write("memory_publications", "memory-publication-project-skill-object-1", {
            "id": "memory-publication-project-skill-object-1", "status": "published", "layer": "project_skill",
            "published_object_id": "skill-1",
        })
        self._write("memory_transitions", "transition-project-skill-publication-object-1", {
            "id": "transition-project-skill-publication-object-1", "object_id": "skill-1",
        })
        self._write("project_skills", "skill-1", {"id": "skill-1", "revision": 3})
        return SimpleNamespace(publication_id="memory-publication-project-skill-object-1", publication_revision=1, transition_id="transition-project-skill-publication-object-1", project_skill_revision=3)

    def rollback(self, *, publication_id: str, expected_publication_revision: int, expected_project_skill_revision: int, reason: str):
        self._calls.append("project_skill_rollback")
        self._write("memory_publications", publication_id, {
            "id": publication_id, "status": "rolled_back", "layer": "project_skill",
            "published_object_id": "skill-1", "rollback_reason": reason,
        })
        self._write("memory_transitions", f"transition-project-skill-rollback-{publication_id}", {
            "id": f"transition-project-skill-rollback-{publication_id}", "object_id": "skill-1", "reason": reason,
        })
        self._write("project_skills", "skill-1", {"id": "skill-1", "revision": 4})
        return SimpleNamespace(publication_id=publication_id, publication_revision=1, transition_id=f"transition-project-skill-rollback-{publication_id}", project_skill_revision=4)


class _SkillUnitOfWork:
    def __init__(self, _database: Path, *, namespace_id: str, calls: list[str]) -> None:
        self._transaction = _SkillTransaction(_database, calls)

    def begin(self):
        return self._transaction


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(subject, "SQLiteMemoryPublicationTrustAuditUnitOfWork", lambda database, namespace_id: _MemoryUnitOfWork(database, namespace_id=namespace_id, calls=calls))
    monkeypatch.setattr(subject, "SQLiteProjectSkillPublicationCompositeUnitOfWork", lambda database, namespace_id: _SkillUnitOfWork(database, namespace_id=namespace_id, calls=calls))
    result = build_effect_runtime(tmp_path / ".rebuild-data" / "jobs.sqlite3", owner_id="memory-publication-test", lease_seconds=1)
    subject.register_memory_publication_handler(tmp_path, result)
    return result, calls


@pytest.mark.parametrize(
    ("action", "kwargs", "expected"),
    (
        ("memory_publish", {"layer": "atom"}, "memory_publish"),
        ("memory_rollback", {}, "memory_rollback"),
        ("project_skill_publish", {}, "project_skill_publish"),
        ("project_skill_rollback", {"expected_publication_revision": 2, "expected_project_skill_revision": 3}, "project_skill_rollback"),
    ),
)
def test_formal_publication_uses_durable_v2_gate_intent_and_immutable_receipt(runtime, action, kwargs, expected):
    effects, calls = runtime
    operation_id = subject.plan_memory_publication(
        effects, namespace_id="default", action=action, object_id="object-1", reason="user-confirmed", **kwargs,
    )

    planned = effects.log.get(operation_id)
    assert planned.contract_version == "effect-v2"
    assert planned.effect_class.value == "QUERYABLE"
    assert planned.operation_id == operation_id
    assert planned.operation_id.startswith("eff2_")
    assert planned.operation_id != "formal-memory-publication-object-1"
    assert planned.expected_receipt_kind == subject.RECEIPT_KIND
    assert planned.expected_receipt_schema_version == subject.RECEIPT_SCHEMA
    with sqlite3.connect(effects.log.database) as connection:
        gate = connection.execute(
            "SELECT policy_revision FROM effect_gate_fact WHERE decision_id=?", (planned.gate_decision_id,),
        ).fetchone()
        intent = connection.execute(
            "SELECT intent_ref,payload_json FROM effect_intent_fact WHERE operation_id=?", (operation_id,),
        ).fetchone()
    assert gate == (planned.rev_set["policy"],)
    assert intent[0] == planned.intent_ref
    assert '"publication_intent_ref"' in intent[1]
    assert Path(effects.log.database).name == "jobs.sqlite3"
    assert (Path(effects.log.database).parent / "structured-records.sqlite3").is_file()
    assert Path(effects.log.database) != Path(effects.log.database).parent / "structured-records.sqlite3"

    settled = effects.dispatch_operation(operation_id, now=1)
    assert settled.state is EffectState.SETTLED_OK
    assert settled.result_ref == subject._receipt_ref(operation_id)
    assert calls == [expected]
    receipt = subject.read_memory_publication_receipt(Path(effects.log.database).parent.parent, operation_id)
    assert receipt["receipt_kind"] == subject.RECEIPT_KIND
    assert receipt["intent_schema_version"] == subject.INTENT_SCHEMA

    # A second dispatch is a Core replay and never repeats the formal write.
    assert effects.dispatch_operation(operation_id, now=2).state is EffectState.SETTLED_OK
    assert calls == [expected]


def test_inflight_probe_recovers_existing_receipt_without_repeating_memory_write(runtime):
    effects, calls = runtime
    operation_id = subject.plan_memory_publication(
        effects, namespace_id="default", action="memory_publish", object_id="object-1", reason="user-confirmed", layer="atom",
    )
    inflight, claimed = effects.runner.claim_planned(operation_id, now=1)
    assert claimed is True
    handler = effects.handlers.resolve(inflight).handler
    handler(inflight)
    assert calls == ["memory_publish"]

    recovered = effects.recover_expired(now=3)
    assert [(item.operation_id, item.state) for item in recovered] == [(operation_id, EffectState.SETTLED_OK)]
    assert calls == ["memory_publish"]


def test_gate_fact_is_stable_for_replay_and_scoped_to_exact_publication(runtime):
    effects, _calls = runtime
    first = subject.plan_memory_publication(
        effects,
        namespace_id="default",
        action="memory_publish",
        object_id="object-1",
        reason="user-confirmed",
        layer="atom",
    )
    replay = subject.plan_memory_publication(
        effects,
        namespace_id="default",
        action="memory_publish",
        object_id="object-1",
        reason="user-confirmed",
        layer="atom",
    )
    second = subject.plan_memory_publication(
        effects,
        namespace_id="default",
        action="memory_publish",
        object_id="object-2",
        reason="user-confirmed",
        layer="scenario",
    )

    assert replay == first
    assert second != first
    with sqlite3.connect(effects.log.database) as connection:
        facts = connection.execute(
            "SELECT decision_id, scope_ref, decision_digest FROM effect_gate_fact ORDER BY decision_id"
        ).fetchall()
    assert len(facts) == 2
    assert len({row[2] for row in facts}) == 2
    assert all(row[1].startswith("scope:formal-memory-publication/default/") for row in facts)

def test_inflight_probe_rebuilds_missing_receipt_from_committed_publication_without_writer(runtime):
    effects, calls = runtime
    operation_id = subject.plan_memory_publication(
        effects, namespace_id="default", action="memory_publish", object_id="object-1", reason="user-confirmed", layer="atom",
    )
    database = Path(effects.log.database).parent / "structured-records.sqlite3"
    records = SQLiteStructuredRecordStore(database)
    intent = records.list(subject.INTENTS)[0].payload
    with records.begin() as transaction:
        transaction.put("memory_publications", "publication-1", {
            "id": "publication-1", "status": "published", "layer": "atom",
            "published_object_id": "object-1", "published_at": intent["occurred_at"],
            "transition_ref": "crp://default/memory-transitions/transition-1.json",
        }, expected_revision=0)
        transaction.put("memory_transitions", "transition-1", {
            "id": "transition-1", "object_id": "object-1",
        }, expected_revision=0)
        transaction.commit()

    _inflight, claimed = effects.runner.claim_planned(operation_id, now=1)
    assert claimed is True
    recovered = effects.recover_expired(now=3)
    assert [(item.operation_id, item.state) for item in recovered] == [(operation_id, EffectState.SETTLED_OK)]
    assert calls == []
    assert subject.read_memory_publication_receipt(Path(effects.log.database).parent.parent, operation_id)["publication_id"] == "publication-1"

def test_v2_handler_rejects_revision_drift_and_new_writer_has_no_legacy_plan(runtime):
    effects, _calls = runtime
    operation_id = subject.plan_memory_publication(
        effects, namespace_id="default", action="memory_publish", object_id="object-1", reason="user-confirmed", layer="atom",
    )
    planned = effects.log.get(operation_id)
    drifted = replace(planned, rev_set={**planned.rev_set, "policy": "other-policy-v2"})
    with pytest.raises(ValueError, match="policy revision drifted"):
        effects.handlers.resolve(drifted).handler(drifted)
    assert ".plan(" not in Path(subject.__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("action", "kwargs", "receipt", "message"),
    (
        ("memory_publish", {"layer": "atom"}, {
            "status": "published", "layer": "atom", "object_id": "other-object",
            "publication_id": "publication-1", "transition_id": "transition-1",
        }, "content drifted"),
        ("memory_rollback", {}, {
            "status": "published", "layer": "atom", "object_id": "object-1",
            "publication_id": "object-1", "transition_id": "transition-1",
        }, "rollback receipt content drifted"),
        ("project_skill_rollback", {"expected_publication_revision": 2, "expected_project_skill_revision": 3}, {
            "status": "rolled_back", "layer": "project_skill", "publication_id": "object-1",
            "transition_id": "transition-1", "publication_revision": 0, "project_skill_revision": 3,
        }, "receipt revision is invalid"),
    ),
)
def test_v2_receipt_rejects_status_object_and_revision_tamper(runtime, action, kwargs, receipt, message):
    effects, _calls = runtime
    operation_id = subject.plan_memory_publication(
        effects, namespace_id="default", action=action, object_id="object-1", reason="user-confirmed", **kwargs,
    )
    database = Path(effects.log.database).parent / "structured-records.sqlite3"
    subject._write_receipt(database, operation_id, receipt)
    effect = effects.log.get(operation_id)
    with pytest.raises(ValueError, match=message):
        effects.handlers.resolve(effect).handler(effect)


def test_receipt_only_cannot_settle_without_committed_publication_authority(runtime):
    effects, _calls = runtime
    operation_id = subject.plan_memory_publication(
        effects, namespace_id="default", action="memory_publish", object_id="object-1", reason="user-confirmed", layer="atom",
    )
    database = Path(effects.log.database).parent / "structured-records.sqlite3"
    subject._write_receipt(database, operation_id, {
        "status": "published", "layer": "atom", "object_id": "object-1",
        "publication_id": "publication-1", "transition_id": "transition-1",
    })
    effect = effects.log.get(operation_id)
    with pytest.raises(ValueError, match="publication authority is unavailable"):
        effects.handlers.resolve(effect).handler(effect)


def test_memory_rollback_probe_rejects_committed_reason_or_time_drift(runtime):
    effects, _calls = runtime
    operation_id = subject.plan_memory_publication(
        effects, namespace_id="default", action="memory_rollback", object_id="publication-1", reason="user-confirmed",
    )
    database = Path(effects.log.database).parent / "structured-records.sqlite3"
    records = SQLiteStructuredRecordStore(database)
    intent = records.list(subject.INTENTS)[0].payload
    with records.begin() as transaction:
        transaction.put("memory_publications", "publication-1", {
            "id": "publication-1", "status": "rolled_back", "layer": "atom",
            "published_object_id": "object-1", "rollback_reason": "different-reason",
            "rolled_back_at": f"{intent['occurred_at']}-different",
        }, expected_revision=0)
        transaction.put("memory_transitions", "transition-1", {
            "id": "transition-1", "object_id": "object-1", "reason": "different-reason",
            "created_at": f"{intent['occurred_at']}-different",
        }, expected_revision=0)
        transaction.commit()
    subject._write_receipt(database, operation_id, {
        "status": "rolled_back", "layer": "atom", "object_id": "object-1",
        "publication_id": "publication-1", "transition_id": "transition-1",
    })
    effect = effects.log.get(operation_id)
    with pytest.raises(ValueError, match="rollback authority drifted"):
        effects.handlers.resolve(effect).handler(effect)
