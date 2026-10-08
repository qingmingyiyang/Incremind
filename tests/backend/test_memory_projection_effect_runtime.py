from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

import backend.api.workbench_ai_runtime as workbench_ai_runtime
from backend.api.memory_projection_effect_runtime import (
    admit_memory_projection_rebuild,
    execute_granted_memory_projection_rebuild,
    memory_projection_rebuild_authority,
    memory_projection_rebuild_automation_binding,
    preview_memory_projection_rebuild_automation,
    register_memory_projection_rebuild_effect_runtime,
)
from backend.security.automation_grants import AutomationGrantError, AutomationGrantRepository
from backend.security.secrets import InMemorySecretStore
from core.effect_log import EffectState, build_effect_runtime
from core.product_core.memory_projection_authority_contract import (
    MemoryProjectionAuthoritySnapshot,
)


def _snapshot() -> MemoryProjectionAuthoritySnapshot:
    return MemoryProjectionAuthoritySnapshot(
        project_id="project-1",
        authority_identity="memory-authority-v1",
        series_memories=(),
        scenarios=(),
        atoms=(),
        project_skills=(),
    )


@dataclass
class _Authority:
    snapshot: MemoryProjectionAuthoritySnapshot

    def load(self, project_id: str) -> MemoryProjectionAuthoritySnapshot:
        return self.snapshot


def test_admission_uses_the_caller_owned_core_effect_runtime_transaction(tmp_path) -> None:
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="test")
    authority = _Authority(_snapshot())

    effect, created = admit_memory_projection_rebuild(
        runtime, authority, project_id="project-1", admitted_at=100,
    )
    replay, replay_created = admit_memory_projection_rebuild(
        runtime, authority, project_id="project-1", admitted_at=101,
    )

    assert created is True
    assert replay_created is False
    assert replay.operation_id == effect.operation_id
    assert effect.kind == "memory_projection_rebuild"
    assert runtime.log.get(effect.operation_id).state is EffectState.PLANNED


def test_confirmed_repair_uses_a_second_stable_effect_identity(tmp_path) -> None:
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="test")
    authority = _Authority(_snapshot())

    refresh, _ = admit_memory_projection_rebuild(
        runtime, authority, project_id="project-1", admitted_at=100,
    )
    repair, repair_created = admit_memory_projection_rebuild(
        runtime, authority, project_id="project-1", admitted_at=101, repair=True,
    )
    repair_replay, replay_created = admit_memory_projection_rebuild(
        runtime, authority, project_id="project-1", admitted_at=102, repair=True,
    )

    assert repair_created is True
    assert replay_created is False
    assert repair.operation_id != refresh.operation_id
    assert repair_replay.operation_id == repair.operation_id


def test_admission_rejects_a_snapshot_from_another_project(tmp_path) -> None:
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="test")

    try:
        admit_memory_projection_rebuild(
            runtime, _Authority(_snapshot()), project_id="other", admitted_at=100,
        )
    except ValueError as error:
        assert str(error) == "memory projection authority project identity drifted"
    else:
        raise AssertionError("cross-project authority admission must fail closed")


def test_registration_attaches_handler_probe_and_recovery_to_one_core_runtime(tmp_path) -> None:
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="test")

    register_memory_projection_rebuild_effect_runtime(tmp_path, runtime)

    assert "memory_projection_rebuild" in runtime.handlers.kinds()
    assert ("memory_projection_rebuild", "effect-v2") in runtime.handlers.probes()
    assert ("memory_projection_rebuild", "effect-v2") in runtime.recoveries.probes()


def test_exact_automation_grant_claims_before_effect_and_binds_receipt(tmp_path) -> None:
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="test")
    authority = memory_projection_rebuild_authority(tmp_path)
    register_memory_projection_rebuild_effect_runtime(tmp_path, runtime)
    grants = AutomationGrantRepository(tmp_path, secret_store=InMemorySecretStore())
    binding = memory_projection_rebuild_automation_binding(
        authority, project_id="project-1", admitted_at=100,
    )
    from datetime import datetime, timedelta, timezone
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    created = grants.create(
        binding=binding, expires_at=expires_at, max_uses=1, command_id="memory-projection-1",
    )

    settled, completed = execute_granted_memory_projection_rebuild(
        runtime, authority, grants,
        project_id="project-1", admitted_at=100,
        grant_id=created.grant_id, expected_grant_revision=created.revision,
    )

    assert settled.state is EffectState.SETTLED_OK
    assert completed.uses_consumed == 1
    assert completed.state == "exhausted"
    assert completed.receipt_ref == settled.result_ref


def test_automation_grant_binding_changes_with_projection_authority(tmp_path) -> None:
    first = memory_projection_rebuild_automation_binding(
        _Authority(_snapshot()), project_id="project-1", admitted_at=100,
    )
    changed = MemoryProjectionAuthoritySnapshot(
        project_id="project-1", authority_identity="memory-authority-v2",
        series_memories=(), scenarios=(), atoms=(), project_skills=(),
    )
    second = memory_projection_rebuild_automation_binding(
        _Authority(changed), project_id="project-1", admitted_at=100,
    )

    assert first.operation_id != second.operation_id
    assert first.parameter_digest != second.parameter_digest


def test_automation_preview_does_not_create_a_grant_or_effect(tmp_path) -> None:
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="test")

    binding, fingerprint = preview_memory_projection_rebuild_automation(
        _Authority(_snapshot()), project_id="project-1", admitted_at=100,
    )

    assert binding.project_id == "project-1"
    assert binding.operation_id.startswith("eff2_")
    assert len(fingerprint) == 64
    assert not (tmp_path / ".rebuild-data" / "security" / "automation-grants.sqlite3").exists()
    with runtime.log._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 0


def test_authority_drift_rejects_before_grant_claim_or_effect_admission(tmp_path) -> None:
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="test")
    original = _Authority(_snapshot())
    binding = memory_projection_rebuild_automation_binding(
        original, project_id="project-1", admitted_at=100,
    )
    grants = AutomationGrantRepository(tmp_path, secret_store=InMemorySecretStore())
    from datetime import datetime, timedelta, timezone
    created = grants.create(
        binding=binding,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        max_uses=1,
        command_id="memory-projection-drift",
    )
    changed = _Authority(MemoryProjectionAuthoritySnapshot(
        project_id="project-1", authority_identity="memory-authority-v2",
        series_memories=(), scenarios=(), atoms=(), project_skills=(),
    ))

    with pytest.raises(ValueError, match="authority changed before grant binding"):
        execute_granted_memory_projection_rebuild(
            runtime, changed, grants,
            project_id="project-1", admitted_at=101,
            grant_id=created.grant_id, expected_grant_revision=created.revision,
            expected_authority_fingerprint=str(binding.parameter_digest),
        )

    assert grants.get(created.grant_id).state == "active"
    assert grants.get(created.grant_id).uses_consumed == 0
    with runtime.log._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 0


def test_workbench_question_admits_rebuild_without_synchronous_dispatch(monkeypatch) -> None:
    snapshot = _snapshot()
    authority = _Authority(snapshot)
    effect_runtime = SimpleNamespace(
        dispatch_operation=lambda *_args, **_kwargs: (
            _ for _ in ()
        ).throw(AssertionError("ordinary questions must not dispatch rebuild Effects")),
    )
    admissions: list[dict[str, object]] = []

    class _Factory:
        def memory_publication_authority_resolution(self):
            return SimpleNamespace(records=None, authority_identity="memory-authority-v1")

        def project_skill_repository_resolution(self):
            return SimpleNamespace(repository=object(), authority_identity="skill-authority-v1")

    class _Recall:
        def __init__(self, *, schedule_rebuild, **_kwargs) -> None:
            self._schedule_rebuild = schedule_rebuild

        def execute(self, _project_id, *, query, created_at=None):
            operation_id = self._schedule_rebuild(snapshot, "authority-fingerprint")
            assert operation_id == "eff2_workbench_rebuild"
            return object()

    class _Answer:
        def __init__(self, *_args, **_kwargs) -> None:
            self._recall = _kwargs["recall"]

        def execute(self, *, question):
            self._recall.execute("project-1", query=question)
            return object()

    def admit(runtime, observed_authority, **kwargs):
        admissions.append({"runtime": runtime, "authority": observed_authority, **kwargs})
        return SimpleNamespace(operation_id="eff2_workbench_rebuild"), True

    monkeypatch.setattr(workbench_ai_runtime, "AggregateRepositoryFactory", lambda **_kwargs: _Factory())
    monkeypatch.setattr(workbench_ai_runtime, "ObjectStoreMemoryStore", lambda _store: object())
    monkeypatch.setattr(workbench_ai_runtime, "ObjectStoreRecallRepository", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(workbench_ai_runtime, "ObjectStorePersonaRepository", lambda _store: object())
    monkeypatch.setattr(workbench_ai_runtime, "CreateProjectMemoryRecall", lambda **_kwargs: object())
    monkeypatch.setattr(workbench_ai_runtime, "CurrentMemoryProjectionAuthority", lambda **_kwargs: authority)
    monkeypatch.setattr(workbench_ai_runtime, "ObjectStoreMemoryProjectionRepository", lambda _store: object())
    monkeypatch.setattr(workbench_ai_runtime, "ObjectStoreProgressiveRecallAuthorityReader", lambda _store: object())
    monkeypatch.setattr(workbench_ai_runtime, "ProgressiveDirectQuestionRecall", _Recall)
    monkeypatch.setattr(workbench_ai_runtime, "AnswerWorkbenchDirectQuestion", _Answer)
    monkeypatch.setattr(workbench_ai_runtime, "serialize_workbench_direct_question", lambda _result: {"status": "answered"})
    monkeypatch.setattr(workbench_ai_runtime, "admit_memory_projection_rebuild", admit)

    payload = workbench_ai_runtime.WorkbenchQuestionCapability(
        runtime_root=object(), store=object(), namespace_id="default", effect_runtime=effect_runtime,
    )._answer(question="what changed?", scope={"project_id": "project-1"})

    assert payload["project_route"]["selected_project_id"] == "project-1"
    assert len(admissions) == 1
    assert admissions[0]["runtime"] is effect_runtime
    assert admissions[0]["authority"] is authority
    assert admissions[0]["project_id"] == "project-1"
    assert isinstance(admissions[0]["admitted_at"], int)
    assert admissions[0]["expected_authority_fingerprint"] == "authority-fingerprint"
