from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from backend.api.external_project_skill_apply_startup import (
    plan_external_project_skill_apply,
    register_external_project_skill_apply_handler,
)
from core.aggregate_repository_factory import STRUCTURED_DATABASE_NAME
from core.effect_log import EFFECT_V2, EffectLog, EffectState
from core.storage_provider import (
    ExternalProjectSkillApplyEvidence,
    SQLiteExternalProjectSkillApplySagaStore,
    SQLiteStructuredRecordStore,
)


class _PreparedService:
    def __init__(self, evidence) -> None:
        self._operation = SimpleNamespace(operation_id="draft-skill-v2", evidence=evidence)

    def prepare(self, draft_id: str, *, expected_revision: int):
        assert draft_id == "draft-skill-v2"
        assert expected_revision == 1
        return self._operation


def test_v2_probe_refuses_root_revision_and_namespace_drift_for_finalized_saga(tmp_path: Path):
    database = tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    operations = SQLiteExternalProjectSkillApplySagaStore(SQLiteStructuredRecordStore(database))
    evidence = ExternalProjectSkillApplyEvidence(
        "default", "project-alpha", "skill-project-alpha", 1,
        "a" * 64, "json:object-store-v1",
    )
    operations.prepare(operation_id="draft-skill-v2", evidence=evidence)
    prepared = operations.get("draft-skill-v2")
    assert prepared is not None
    applied = operations.mark_skill_applied(
        "draft-skill-v2", expected_revision=prepared.revision, applied_skill_revision=2,
    )
    operations.finalize("draft-skill-v2", expected_revision=applied.revision)
    effects = EffectLog(database)
    effect = effects.get(plan_external_project_skill_apply(
        _PreparedService(evidence), effects, "draft-skill-v2", expected_revision=1,
    ))
    registrations = []

    class _Handlers:
        def register(self, registration):
            registrations.append(registration)

    register_external_project_skill_apply_handler(tmp_path, SimpleNamespace(handlers=_Handlers()))
    probe = next(item.probe for item in registrations if item.contract_version == EFFECT_V2)
    assert probe is not None
    state, receipt_ref = probe(effect)
    assert state is EffectState.SETTLED_OK
    assert receipt_ref == "crp://default/external-project-skill-apply-receipts/draft-skill-v2:r3"
    for drifted in (
        replace(effect, root_id="project-other"),
        replace(effect, rev_set={"policy": "c" * 64}),
        replace(effect, intent_ref="crp://other/external-project-skill-apply-intents/draft-skill-v2"),
    ):
        assert probe(drifted) == (EffectState.PLANNED, None)
