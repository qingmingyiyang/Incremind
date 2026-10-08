from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.external_project_skill_apply_saga import ExternalProjectSkillApplySagaService
from backend.api.external_project_skill_apply_startup import (
    _v2_intent,
    backfill_external_project_skill_apply_effects,
    dispatch_external_project_skill_apply_effects,
)
from core.effect_log import EffectLog, EffectReaper, EffectRunner, EffectState
from core.aggregate_repository_factory import (
    AUTHORITY_DATABASE_NAME,
    JSON_PROJECT_SKILL_AUTHORITY_IDENTITY,
    STRUCTURED_DATABASE_NAME,
    TARGET_IDENTITY,
    AggregateRepositoryFactory,
)
from core.project_skill_core import ObjectStoreProjectSkillRepository, ProjectSkillUpdate
from core.storage_provider import (
    AggregateAuthorityEvidence,
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteExternalProjectSkillApplySagaStore,
    SQLiteStructuredRecordStore,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "core-contracts" / "rebuild" / "fixtures" / "project_skill" / "valid-active-skill.json"


def _store(root):
    return JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library")


def _records(root):
    return SQLiteStructuredRecordStore(root / ".rebuild-data" / STRUCTURED_DATABASE_NAME)


def _operations(root):
    return SQLiteExternalProjectSkillApplySagaStore(_records(root))


def _v2_effect(log, operation):
    _gate_id, _gate_fact, intent = _v2_intent(operation.operation_id, operation.evidence)
    return log.get(intent.operation_id)


def recover_external_project_skill_apply_sagas(application, root, *, max_operations=100):
    effects = EffectLog(root / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    backfill_external_project_skill_apply_effects(
        root, effects, max_operations=max_operations,
    )
    EffectReaper(effects).recover_expired(now=2**31)
    return dispatch_external_project_skill_apply_effects(
        application, root, EffectRunner(effects, owner_id="test-project-skill-apply"),
        max_operations=max_operations,
    )


def _structured():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for key in ("revision", "markdown_revision", "json_revision", "markdown_uri", "json_uri", "created_at", "updated_at"):
        payload.pop(key, None)
    return payload


def _seed(root, skills, draft_id):
    structured = _structured()
    skills.save(ProjectSkillUpdate("project-alpha", "# Initial", structured, 0, "initial"))
    proposed = dict(structured)
    proposed["style_preferences"] = {"voice": f"recovered {draft_id}", "format_defaults": ["Markdown"]}
    drafts = _store(root)
    drafts.write("external_agent_review_drafts", draft_id, {
        "id": draft_id, "draft_type": "project_skill_update", "status": "pending_review",
        "project_id": "project-alpha", "target_id": "skill-project-alpha",
        "suggested_changes": {"structured": proposed, "markdown": f"# {draft_id}"},
        "review": {"state": "pending_review"}, "application": {"state": "blocked"},
    }, expected_revision=0)
    return drafts


class _FailAfterSkillCommit:
    def __init__(self, delegate): self.delegate = delegate
    def prepare(self, **kwargs): return self.delegate.prepare(**kwargs)
    def mark_skill_applied(self, *args, **kwargs): raise OSError("injected interruption after skill commit")
    def finalize(self, *args, **kwargs): return self.delegate.finalize(*args, **kwargs)


class _FailDraftFinalize:
    def __init__(self, delegate): self.delegate = delegate
    def read(self, collection, object_id): return self.delegate.read(collection, object_id)
    def list(self, collection): return self.delegate.list(collection)
    def delete(self, collection, object_id): return self.delegate.delete(collection, object_id)
    def write(self, collection, object_id, payload, expected_revision):
        if collection == "external_agent_review_drafts" and payload.get("status") == "applied":
            raise OSError("injected draft finalize failure")
        return self.delegate.write(collection, object_id, payload, expected_revision)


def _interrupt_prepared(root, draft_id="draft-skill-startup"):
    drafts = _store(root)
    skills = ObjectStoreProjectSkillRepository(drafts)
    drafts = _seed(root, skills, draft_id)
    operations = _operations(root)
    with pytest.raises(OSError, match="interruption"):
        ExternalProjectSkillApplySagaService(
            skills=skills, drafts=drafts, operations=_FailAfterSkillCommit(operations),
            project_skill_authority_identity=JSON_PROJECT_SKILL_AUTHORITY_IDENTITY,
        ).apply(draft_id, expected_revision=1)
    return drafts, skills, operations


def test_prepared_recovers_once_and_repeated_startup_does_not_add_revision(tmp_path: Path):
    drafts, skills, operations = _interrupt_prepared(tmp_path)
    first = recover_external_project_skill_apply_sagas(FastAPI(), tmp_path)
    second = recover_external_project_skill_apply_sagas(FastAPI(), tmp_path)
    assert (first.recovered, first.failed, second.attempted) == (1, 0, 0)
    assert operations.get("draft-skill-startup").state == "finalized"
    assert skills.load("project-alpha")["revision"] == 2
    assert len(skills.revisions("project-alpha")) == 2
    assert drafts.read("external_agent_review_drafts", "draft-skill-startup")["status"] == "applied"


def test_skill_applied_state_finishes_draft_finalize_on_startup(tmp_path: Path):
    drafts = _store(tmp_path)
    skills = ObjectStoreProjectSkillRepository(drafts)
    _seed(tmp_path, skills, "draft-skill-applied")
    operations = _operations(tmp_path)
    with pytest.raises(OSError, match="draft finalize failure"):
        ExternalProjectSkillApplySagaService(
            skills=skills, drafts=_FailDraftFinalize(drafts), operations=operations,
            project_skill_authority_identity=JSON_PROJECT_SKILL_AUTHORITY_IDENTITY,
        ).apply("draft-skill-applied", expected_revision=1)
    assert operations.get("draft-skill-applied").state == "skill_applied"

    report = recover_external_project_skill_apply_sagas(FastAPI(), tmp_path)

    assert report.recovered == 1
    assert operations.get("draft-skill-applied").state == "finalized"
    assert skills.load("project-alpha")["revision"] == 2
    assert len(skills.revisions("project-alpha")) == 2


def test_bad_operation_does_not_block_good_and_batch_is_bounded(tmp_path: Path):
    drafts, skills, operations = _interrupt_prepared(tmp_path, "draft-b-good")
    from core.storage_provider import ExternalProjectSkillApplyEvidence
    operations.prepare(operation_id="draft-a-missing", evidence=ExternalProjectSkillApplyEvidence(
        "default", "project-missing", "skill-missing", 1, "a" * 64, JSON_PROJECT_SKILL_AUTHORITY_IDENTITY
    ))
    report = recover_external_project_skill_apply_sagas(FastAPI(), tmp_path)
    assert (report.scanned, report.recovered, report.failed) == (2, 1, 1)
    assert report.items[0].error_code == "operation_invalid"
    assert skills.load("project-alpha")["revision"] == 2

    for index in range(3):
        operations.prepare(operation_id=f"draft-z-{index}", evidence=ExternalProjectSkillApplyEvidence(
            "default", f"project-z-{index}", f"skill-z-{index}", 1, f"{index + 1:064x}", JSON_PROJECT_SKILL_AUTHORITY_IDENTITY
        ))
    bounded = recover_external_project_skill_apply_sagas(FastAPI(), tmp_path, max_operations=2)
    # The previous failed operation remains in a live INFLIGHT lease and is
    # deliberately deferred.  The bounded pass dispatches only one newly
    # planned Saga, so it cannot duplicate a potentially completed write.
    assert (bounded.attempted, bounded.deferred) == (1, 3)
    assert any(item.operation_id.startswith("draft-z-") for item in bounded.items)
    assert all(item.operation_id != "draft-a-missing" for item in bounded.items)


def _activate_skills(root):
    records = _records(root)
    authority = SQLiteAggregateAuthorityStore(root / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    evidence = AggregateAuthorityEvidence("skill-startup-v1", "a" * 64, "b" * 64, TARGET_IDENTITY)
    with records.begin() as uow:
        uow.put("aggregate_authority_targets", "default~project_skills", {
            "namespace_id": "default", "aggregate": "project_skills", "migration_id": evidence.migration_id,
            "source_fingerprint": evidence.source_fingerprint, "target_fingerprint": evidence.target_fingerprint,
            "target_identity": TARGET_IDENTITY,
        }, expected_revision=0); uow.commit()
    initial = authority.create_json_active(namespace_id="default", aggregate="project_skills", reason="initial")
    staged = authority.transition(namespace_id="default", aggregate="project_skills", expected_revision=initial.revision,
                                  to_state="sqlite_staged", evidence=evidence, reason="staged")
    authority.transition(namespace_id="default", aggregate="project_skills", expected_revision=staged.revision,
                         to_state="sqlite_active", evidence=evidence, reason="active")


def test_active_sqlite_recovery_uses_single_authority_and_lifespan(tmp_path: Path):
    _activate_skills(tmp_path)
    drafts = _store(tmp_path)
    resolution = AggregateRepositoryFactory(tmp_path, "default", drafts).project_skill_repository_resolution()
    assert resolution.authority_identity == TARGET_IDENTITY
    _seed(tmp_path, resolution.repository, "draft-sqlite-startup")
    operations = _operations(tmp_path)
    with pytest.raises(OSError):
        ExternalProjectSkillApplySagaService(
            skills=resolution.repository, drafts=drafts, operations=_FailAfterSkillCommit(operations),
            project_skill_authority_identity=TARGET_IDENTITY,
        ).apply("draft-sqlite-startup", expected_revision=1)
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        assert "external_project_skill_apply" in client.app.state.effect_runtime.handlers.kinds()
        operation = operations.get("draft-sqlite-startup")
        assert operation is not None
        effect = _v2_effect(client.app.state.effect_runtime.log, operation)
    assert effect.state is EffectState.SETTLED_OK
    assert resolution.repository.load("project-alpha")["revision"] == 2
    assert ObjectStoreProjectSkillRepository(drafts).load("project-alpha") is None


def test_independent_process_exit_after_skill_commit_recovers_on_startup(tmp_path: Path):
    script = textwrap.dedent("""
        import json, os, sys
        from pathlib import Path
        from backend.api.external_project_skill_apply_saga import ExternalProjectSkillApplySagaService
        from core.aggregate_repository_factory import JSON_PROJECT_SKILL_AUTHORITY_IDENTITY, STRUCTURED_DATABASE_NAME
        from core.project_skill_core import ObjectStoreProjectSkillRepository, ProjectSkillUpdate
        from core.storage_provider import JsonObjectStore, SQLiteExternalProjectSkillApplySagaStore, SQLiteStructuredRecordStore
        root, fixture = Path(sys.argv[1]), Path(sys.argv[2])
        store = JsonObjectStore(root / '.rebuild-data', legacy_root=root / 'library')
        structured = json.loads(fixture.read_text(encoding='utf-8'))
        for key in ('revision','markdown_revision','json_revision','markdown_uri','json_uri','created_at','updated_at'): structured.pop(key, None)
        skills = ObjectStoreProjectSkillRepository(store)
        skills.save(ProjectSkillUpdate('project-alpha','# Initial',structured,0,'initial'))
        draft_id='draft-skill-process-crash'; proposed=dict(structured)
        store.write('external_agent_review_drafts',draft_id,{'id':draft_id,'draft_type':'project_skill_update','status':'pending_review','project_id':'project-alpha','target_id':'skill-project-alpha','suggested_changes':{'structured':proposed,'markdown':'# Recovered'},'review':{'state':'pending_review'},'application':{'state':'blocked'}},expected_revision=0)
        delegate=SQLiteExternalProjectSkillApplySagaStore(SQLiteStructuredRecordStore(root/'.rebuild-data'/STRUCTURED_DATABASE_NAME))
        class Exit:
            def prepare(self,**kwargs): return delegate.prepare(**kwargs)
            def mark_skill_applied(self,*args,**kwargs): os._exit(74)
            def finalize(self,*args,**kwargs): return delegate.finalize(*args,**kwargs)
        ExternalProjectSkillApplySagaService(skills=skills,drafts=store,operations=Exit(),project_skill_authority_identity=JSON_PROJECT_SKILL_AUTHORITY_IDENTITY).apply(draft_id,expected_revision=1)
    """)
    env = dict(os.environ); env["PYTHONPATH"] = str(ROOT / "src")
    crashed = subprocess.run([sys.executable, "-c", script, str(tmp_path), str(FIXTURE)], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=30, check=False)
    assert crashed.returncode == 74, (crashed.stdout, crashed.stderr)
    skills = ObjectStoreProjectSkillRepository(_store(tmp_path))
    assert skills.load("project-alpha")["revision"] == 2
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        operation = _operations(tmp_path).get("draft-skill-process-crash")
        assert operation is not None
        effect = _v2_effect(client.app.state.effect_runtime.log, operation)
        assert effect.state is EffectState.SETTLED_OK
    assert skills.load("project-alpha")["revision"] == 2
    assert len(skills.revisions("project-alpha")) == 2
