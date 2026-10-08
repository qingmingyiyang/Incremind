"""Build detached product Turn requests using the existing kernel contract."""
from copy import deepcopy
from .contracts import AIKernelContractError
from .turn_templates import TURN_KINDS, _template, template_purpose, turn_purpose, is_user_turn, planner_limits


def freeze_turn_request(kind, *, turn_id, session_id, operation_id, idempotency_key,
                        project_id, created_at, text, privacy, refs=(), capabilities=None, budget=None,
                        template_version=1, capability_request=None, situation=None):
    """Construct a detached deterministic request; IDs/time come from the caller.

    Product callers must obtain privacy and filtered material from v2/privacy.
    The kernel reads this frozen policy and never recalculates authorization.
    """
    from .contracts import validate_turn_request
    try:
        allowed, steps, timeout = _template(kind, template_version)
    except ValueError as error:
        raise AIKernelContractError(str(error)) from error
    payload = {
        "schema_version": "1.0.0", "turn_id": turn_id, "session_id": session_id,
        "operation_id": operation_id, "idempotency_key": idempotency_key,
        "scope": {"kind": "project", "project_id": project_id, "series_id": None},
        "input": {"kind": "text", "text": text, "refs": list(refs)},
        "desired_outcome": kind, "privacy": dict(privacy),
        "capability_policy": {"allowed": sorted(set(allowed if capabilities is None else capabilities)),
                              "denied": [], "require_approval": []},
        "context_policy": {"include_project_skill": kind == "project.task",
                           "include_memory": kind.startswith("project."),
                           "include_session_history": kind == "project.task",
                           "max_context_bytes": 262144},
        "approval_policy": {"mode": "risk_based", "auto_approve_read_only": True},
        "execution_policy": {"template_version": template_version,
                             "purpose": template_purpose(kind),
                             "budget": dict(budget or {"max_steps": steps, "planner_timeout_ms": timeout})},
        "created_at": created_at,
    }
    if capability_request is not None:
        payload["capability_request"] = deepcopy(capability_request)
    if situation is not None:
        payload["input"]["situation"] = situation
    if kind == "project.task" and template_version == 2:
        for name in ("include_project_skill", "include_memory", "include_session_history"):
            payload["context_policy"][name] = False
    return deepcopy(validate_turn_request(payload))
