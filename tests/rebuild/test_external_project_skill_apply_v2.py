from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from backend.api.external_project_skill_apply_startup import (
    _domain_operation_id,
    backfill_external_project_skill_apply_effects,
    plan_external_project_skill_apply,
    register_external_project_skill_apply_handler,
)
from core.aggregate_repository_factory import STRUCTURED_DATABASE_NAME
from core.effect_log import EFFECT_V2, EffectLog, EffectState
from core.effect_log import V2_REVISION_KEYS
from core.storage_provider import (
    ExternalProjectSkillApplyEvidence,
    SQLiteExternalProjectSkillApplySagaStore,
    SQLiteStructuredRecordStore,
)


class _PreparedService:
    def __init__(self, operation_id: str, evidence: ExternalProjectSkillApplyEvidence) -> None:
        self._operation = SimpleNamespace(operation_id=operation_id, evidence=evidence)

    def prepare(self, draft_id: str, *, expected_revision: int):
        assert draft_id == self._operation.operation_id
        assert expected_revision == self._operation.evidence.base_revision
        return self._operation


def _service() -> _PreparedService:
    return _PreparedService(
        "draft-skill-v2",
        ExternalProjectSkillApplyEvidence(
            "default", "project-alpha", "skill-project-alpha", 1,
            "a" * 64, "json:object-store-v1",
        ),
    )


def test_user_confirmed_project_skill_apply_persists_v2_gate_and_intent_facts(tmp_path: Path):
    effects = EffectLog(tmp_path / "effects.sqlite3")

    effect_id = plan_external_project_skill_apply(
        _service(), effects, "draft-skill-v2", expected_revision=1,
    )
    effect = effects.get(effect_id)

    assert effect.operation_id.startswith("eff2_")
    assert effect.contract_version == EFFECT_V2
    assert effect.state is EffectState.PLANNED
    assert tuple(sorted(effect.rev_set)) == tuple(sorted(V2_REVISION_KEYS))
    assert effect.rev_set["policy"] == "a" * 64
    assert effect.operation_id != "draft-skill-v2"
    assert _domain_operation_id(effect) == "draft-skill-v2"
    with sqlite3.connect(effects.database) as connection:
        gate = connection.execute(
            "SELECT policy_revision FROM effect_gate_fact WHERE decision_id=?",
            (effect.gate_decision_id,),
        ).fetchone()
        intent = connection.execute(
            "SELECT schema_version,intent_digest FROM effect_intent_fact WHERE operation_id=?",
            (effect.operation_id,),
        ).fetchone()
    assert gate == ("a" * 64,)
    assert intent == ("external-project-skill-apply-intent-v2", effect.intent_digest)


def test_project_skill_v2_writer_has_no_legacy_plan_or_operation_override():
    source = Path(__file__).resolve().parents[2] / "src" / "backend" / "api" / "external_project_skill_apply_startup.py"
    text = source.read_text(encoding="utf-8")

    planned = text[text.index("def plan_external_project_skill_apply"):text.index("class ExternalProjectSkillApplyStartupRecoveryItem")]
    assert ".plan(" not in planned
    assert "operation_id_override" not in planned
    assert ".plan_v2(" in planned


def test_startup_backfill_reconstructs_missing_effect_as_v2_without_legacy_writer(tmp_path: Path):
    database = tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    operations = SQLiteExternalProjectSkillApplySagaStore(SQLiteStructuredRecordStore(database))
    evidence = ExternalProjectSkillApplyEvidence(
        "default", "project-alpha", "skill-project-alpha", 1,
        "b" * 64, "json:object-store-v1",
    )
    operations.prepare(operation_id="draft-recovery-v2", evidence=evidence)
    effects = EffectLog(database)

    backfilled = backfill_external_project_skill_apply_effects(tmp_path, effects)

    assert len(backfilled) == 1
    effect = effects.get(backfilled[0])
    assert effect.contract_version == EFFECT_V2
    assert effect.state is EffectState.PLANNED
    with sqlite3.connect(effects.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect_gate_fact").fetchone() == (1,)


def test_v2_handler_declares_query_probe_and_immutable_receipt_schema(tmp_path: Path):
    registrations = []

    class _Handlers:
        def register(self, registration):
            registrations.append(registration)

    register_external_project_skill_apply_handler(
        tmp_path, SimpleNamespace(handlers=_Handlers()),
    )

    v2 = next(item for item in registrations if item.contract_version == EFFECT_V2)
    assert v2.effect_class.value == "QUERYABLE"
    assert callable(v2.probe)
    assert v2.receipt_kind == "external-project-skill-apply-receipt"
    assert v2.receipt_schema_version == "external-project-skill-apply-receipt-v2"
