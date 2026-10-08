from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.effect_log import EffectClass, EffectIntent, EffectState


def _unknown(application, step: str):
    runtime = application.state.effect_runtime
    intent = EffectIntent(
        session_id="workflow-session", root_id="workflow-root", step_key=step,
        kind="plugin_hands_execution", effect_class=EffectClass.AT_MOST_ONCE,
        intent_ref=f"crp://workflow/{step}", gate_decision_id="gate-workflow-test",
        rev_set={"workflow": "r1"}, payload={"step": step},
    )
    effect, _created = runtime.log.plan(intent, now=1)
    claimed = runtime.runner.begin_planned(effect.operation_id, now=2)
    return runtime.runner.mark_unknown(claimed, error_ref="receipt-missing", now=3)


def test_unknown_effect_projection_and_approved_recheck_survive_restart(tmp_path):
    application = create_app(SimpleNamespace(root_dir=tmp_path))
    with TestClient(application) as client:
        unknown = _unknown(application, "approve")
        projection = client.get(f"/api/rebuild/workflows/effects/{unknown.operation_id}")
        assert projection.status_code == 200
        assert projection.json()["schema_version"] == "1.0.0"
        assert projection.json()["action"] == "need_user"
        assert projection.json()["reasons"] == ["unknown_effect"]
        assert projection.json()["handler_governance"] == {
            "category": "plugin",
            "boundary": "high_risk",
            "default_mode": "ask",
        }

        approved = client.post(
            f"/api/rebuild/workflows/effects/{unknown.operation_id}/decision",
            json={"choice": "approve", "confirm": True},
        )
        assert approved.status_code == 200
        assert approved.json()["effect_state"] == "PLANNED"
        assert approved.json()["recorded_decision"]["decision_ref"].startswith("decision:")
        decision_ref = approved.json()["recorded_decision"]["decision_ref"]
        assert application.state.effect_runtime.log.get(unknown.operation_id).state is EffectState.PLANNED

    restarted = create_app(SimpleNamespace(root_dir=tmp_path))
    with TestClient(restarted) as client:
        recovered = client.get(f"/api/rebuild/workflows/effects/{unknown.operation_id}")
        assert recovered.status_code == 200
        assert recovered.json()["effect_state"] in {"PLANNED", "INFLIGHT", "SETTLED_OK"}
        assert recovered.json()["effect_state"] != "UNKNOWN"
        assert recovered.json()["decision_ref"] == decision_ref


def test_unknown_effect_abandon_and_confirmation_guards(tmp_path):
    application = create_app(SimpleNamespace(root_dir=tmp_path))
    with TestClient(application) as client:
        unknown = _unknown(application, "abandon")
        rejected = client.post(
            f"/api/rebuild/workflows/effects/{unknown.operation_id}/decision",
            json={"choice": "abandon", "confirm": False},
        )
        assert rejected.status_code == 400
        assert application.state.effect_runtime.log.get(unknown.operation_id).state is EffectState.UNKNOWN
        abandoned = client.post(
            f"/api/rebuild/workflows/effects/{unknown.operation_id}/decision",
            json={"choice": "abandon", "confirm": True},
        )
        assert abandoned.status_code == 200
        assert abandoned.json()["effect_state"] == "ABANDONED"
        replay = client.post(
            f"/api/rebuild/workflows/effects/{unknown.operation_id}/decision",
            json={"choice": "abandon", "confirm": True},
        )
        assert replay.status_code == 409
