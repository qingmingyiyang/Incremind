from __future__ import annotations

from core.product_core import (
    CreateThinUiBackendContractView,
    Phase7RegressionClosureCheck,
    Phase7RegressionClosureReport,
    ServeThinUiBackendContractEndpoint,
    ThinUiBackendAdapterError,
    ThinUiValidationPath,
    ThinUiValidationStep,
    serialize_thin_ui_backend_contract,
)


def _validation_path() -> ThinUiValidationPath:
    steps = []
    for name in ("input", "library", "memory", "document", "qa", "provenance"):
        evidence_refs = [f"R059:{name}"]
        if name in {"library", "provenance"}:
            evidence_refs.append("R094:legacy-migration-final-writer-implementation-regression-consolidation")
        steps.append(
            ThinUiValidationStep(
                name=name,
                status="ready",
                entry_label=f"{name} label",
                entry_action=f"open_{name}",
                evidence_refs=tuple(evidence_refs),
                required_capabilities=(name,),
                blockers=(),
            )
        )
    return ThinUiValidationPath(
        status="ready",
        steps=tuple(steps),
        next_entry_action=None,
    )


def _closure() -> Phase7RegressionClosureReport:
    checks = tuple(
        Phase7RegressionClosureCheck(
            name=name,
            status="ready",
            evidence_refs=(f"R062:{name}",),
            blockers=(),
        )
        for name in (
            "entry_gate",
            "health_regression",
            "recall_contract",
            "ui_validation_contract",
            "traceability_contract",
        )
    )
    return Phase7RegressionClosureReport(
        closure_id="phase7_first_regression_slice",
        status="closed",
        checks=checks,
        blocking_check_names=(),
        next_package="thin_ui_backend_endpoint_adapter_smoke",
    )


def _contract():
    return CreateThinUiBackendContractView().execute(
        validation_path=_validation_path(),
        regression_closure=_closure(),
    )


def test_thin_ui_backend_contract_serializer_returns_json_ready_payload() -> None:
    payload = serialize_thin_ui_backend_contract(_contract())

    assert payload["version"] == "thin-ui-backend-contract.v2"
    assert payload["status"] == "ready"
    assert payload["primary_action"] == "open_input_intake"
    assert len(payload["steps"]) == 6
    assert payload["steps"][0] == {
        "name": "input",
        "status": "ready",
        "label": "input label",
        "action": "open_input",
        "enabled": True,
        "evidence_refs": ["R059:input"],
        "blockers": [],
    }
    assert payload["regression"]["closure_id"] == "phase7_first_regression_slice"
    assert "R095:phase9-thin-ui-validation-path-refresh" in payload["evidence_refs"]
    assert isinstance(payload["steps"][0]["evidence_refs"], list)


def test_thin_ui_backend_contract_endpoint_serves_live_contract_payload() -> None:
    response = ServeThinUiBackendContractEndpoint().execute(
        method="GET",
        path="/api/rebuild/thin-ui/backend-contract",
        contract_provider=_contract,
    )

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.body["version"] == "thin-ui-backend-contract.v2"
    assert response.body["status"] == "ready"
    assert response.body["steps"][1]["evidence_refs"] == [
        "R059:library",
        "R094:legacy-migration-final-writer-implementation-regression-consolidation",
    ]


def test_thin_ui_backend_contract_endpoint_rejects_wrong_method_and_path() -> None:
    endpoint = ServeThinUiBackendContractEndpoint()

    wrong_method = endpoint.execute(
        method="POST",
        path="/api/rebuild/thin-ui/backend-contract",
        contract_provider=_contract,
    )
    wrong_path = endpoint.execute(
        method="GET",
        path="/api/rebuild/thin-ui/other",
        contract_provider=_contract,
    )

    assert wrong_method.status_code == 405
    assert wrong_method.headers["Allow"] == "GET"
    assert wrong_method.body["detail"] == "thin UI backend contract endpoint only supports GET"
    assert wrong_path.status_code == 404
    assert wrong_path.body["detail"] == "thin UI backend contract endpoint not found"


def test_thin_ui_backend_contract_endpoint_keeps_adapter_errors_non_actionable() -> None:
    def broken_provider():
        raise ThinUiBackendAdapterError("missing Phase 8 regression evidence")

    response = ServeThinUiBackendContractEndpoint().execute(
        method="GET",
        path="/api/rebuild/thin-ui/backend-contract",
        contract_provider=broken_provider,
    )

    assert response.status_code == 503
    assert response.body == {
        "detail": "thin UI backend contract unavailable",
        "reason": "missing Phase 8 regression evidence",
        "actionable": False,
    }
