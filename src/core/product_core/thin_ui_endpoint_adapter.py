from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .thin_ui_backend_adapter import ThinUiBackendAdapterError, ThinUiBackendContractView


@dataclass(frozen=True, slots=True)
class ThinUiBackendEndpointResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class ServeThinUiBackendContractEndpoint:
    """Serve the Thin UI backend contract through a narrow endpoint shape."""

    endpoint_path = "/api/rebuild/thin-ui/backend-contract"

    def execute(
        self,
        *,
        method: str,
        path: str,
        contract_provider: Callable[[], ThinUiBackendContractView],
    ) -> ThinUiBackendEndpointResponse:
        request_path = path.split("?", 1)[0]
        if request_path != self.endpoint_path:
            return self._json_response(404, {"detail": "thin UI backend contract endpoint not found"})
        if method.upper() != "GET":
            return self._json_response(
                405,
                {"detail": "thin UI backend contract endpoint only supports GET"},
                extra_headers={"Allow": "GET"},
            )
        try:
            contract = contract_provider()
        except ThinUiBackendAdapterError as error:
            return self._json_response(
                503,
                {
                    "detail": "thin UI backend contract unavailable",
                    "reason": str(error),
                    "actionable": False,
                },
            )
        return self._json_response(200, serialize_thin_ui_backend_contract(contract))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> ThinUiBackendEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return ThinUiBackendEndpointResponse(status_code=status_code, body=body, headers=headers)


def serialize_thin_ui_backend_contract(contract: ThinUiBackendContractView) -> dict[str, Any]:
    return {
        "version": contract.version,
        "status": contract.status,
        "primary_action": contract.primary_action,
        "steps": [
            {
                "name": step.name,
                "status": step.status,
                "label": step.label,
                "action": step.action,
                "enabled": step.enabled,
                "evidence_refs": list(step.evidence_refs),
                "blockers": list(step.blockers),
            }
            for step in contract.steps
        ],
        "regression": {
            "closure_id": contract.regression.closure_id,
            "status": contract.regression.status,
            "blocking_check_names": list(contract.regression.blocking_check_names),
            "next_package": contract.regression.next_package,
        },
        "phase8_regression_evidence_refs": list(contract.phase8_regression_evidence_refs),
        "validation_refresh_evidence_refs": list(contract.validation_refresh_evidence_refs),
        "evidence_refs": list(contract.evidence_refs),
    }
