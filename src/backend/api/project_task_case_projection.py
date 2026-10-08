"""Derived, renderer-safe task case projection for a project world loop.

The task case is deliberately a composition of existing World State, AI Turn,
and Agent-organization read models.  It persists nothing and does not create
another task, run, receipt, or agent authority.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence


def build_project_task_case_projection(
    *,
    project_id: str,
    workflow: object,
    agent_organization_for_turn: Callable[[str], Mapping[str, object] | None] | None = None,
    task_graph_for_project: Callable[[str], object | None] | None = None,
) -> dict[str, object]:
    """Compose one safe project task case from established read authorities.

    An organization can only be read for a pending action's already-verified
    Turn.  This prevents a client-supplied historical or cross-project Turn
    from being spliced into the task case.
    """
    overview = _overview(workflow, project_id)
    state = _mapping(overview, "state")
    if state is None or state.get("project_id") != project_id:
        raise ValueError("task case project scope drifted")
    action = _current_action(state, _mappings(overview, "recent_actions"))
    organization = None
    if action is not None and agent_organization_for_turn is not None:
        turn_id = action["turn_id"]
        loaded = agent_organization_for_turn(turn_id)
        if loaded is not None:
            organization = _organization(loaded, project_id)
    result = {
        "schema_version": "1.0.0",
        "project_id": project_id,
        "derived_at": _string(state, "derived_at") or None,
        "authority": {
            "task_case": "derived_only",
            "state": "world_state_projection",
            "action": "ai_turn_gate_effect_receipt",
            "organization": "agent_organization_projection",
            "feedback": "world_feedback_projection",
        },
        "lifecycle": _lifecycle(state, action),
        # Keep the compact stage for callers that adopted the early contract.
        "stage": _lifecycle(state, action)["stage"],
        "state": _state(state),
        "dynamics": _dynamics(state),
        "action": action,
        "supervision": _supervision(state),
        "feedback": {
            "recorded": isinstance(state.get("latest_feedback_id"), str),
            "ready_for_feedback": bool(action and action["ready_for_feedback"]),
            "latest_feedback_id": _string(state, "latest_feedback_id") or None,
            "feedback_count": len(_mappings(state, "feedback_facts")),
        },
        "organization": organization,
        "deliverables": _deliverables(project_id, action),
        "trace": _trace(
            project_id, state, action, organization,
            task_graph_for_project=task_graph_for_project,
        ),
    }
    result["projection_revision"] = _revision(result, state)
    _assert_public(result)
    return result


def _overview(workflow: object, project_id: str) -> Mapping[str, object]:
    method = getattr(workflow, "overview", None)
    if not callable(method):
        raise TypeError("task case workflow is unavailable")
    value = method(project_id=project_id)
    if not isinstance(value, Mapping):
        raise ValueError("task case workflow overview is invalid")
    return value


def _current_action(
    state: Mapping[str, object], recent_actions: tuple[Mapping[str, object], ...],
) -> dict[str, object] | None:
    planned_actions = _mappings(state, "planned_actions")
    by_action_id = {_string(item, "action_id"): item for item in recent_actions}
    pending_ids = _string_tuple(state.get("pending_action_ids", ()))
    # First use the unresolved durable action.  Once feedback resolves it,
    # retain the newest planned action with an exact status instead of making
    # the completed work and its organization disappear from the task case.
    action_id = next((item for item in reversed(pending_ids) if item in by_action_id), "")
    if not action_id:
        action_id = next(
            (
                _string(item, "action_id")
                for item in reversed(planned_actions)
                if _string(item, "action_id") in by_action_id
            ),
            "",
        )
    if not action_id:
        return None
    planned = next((item for item in reversed(planned_actions) if _string(item, "action_id") == action_id), None)
    status = by_action_id.get(action_id)
    if planned is None or status is None:
        return None
    turn_id = _string(status, "turn_id")
    if not turn_id:
        return None
    lifecycle = _string(status, "status") or "not_admitted"
    return {
        "action_id": action_id,
        "turn_id": turn_id,
        "title": _string(planned, "title"),
        "expected_outcome": _string(planned, "expected_outcome"),
        "status": lifecycle,
        "terminal": bool(status.get("terminal", False)),
        "ready_for_feedback": bool(status.get("ready_for_feedback", False)),
        "governance": _governance(_mapping(status, "governance")),
        "compaction": _compaction(_mapping(status, "context")),
    }


def _lifecycle(state: Mapping[str, object], action: Mapping[str, object] | None) -> dict[str, str]:
    if _mapping(state, "goal") is None:
        return {"stage": "state", "reason": "goal_required"}
    if action is None:
        return (
            {"stage": "feedback", "reason": "feedback_recorded"}
            if isinstance(state.get("latest_feedback_id"), str)
            else {"stage": "dynamics", "reason": "action_required"}
        )
    if action["status"] == "not_admitted":
        return {"stage": "dynamics", "reason": "turn_not_admitted"}
    if action["ready_for_feedback"]:
        return {"stage": "feedback", "reason": "feedback_required"}
    if isinstance(state.get("latest_feedback_id"), str):
        return {"stage": "feedback", "reason": "feedback_recorded"}
    if action["terminal"]:
        return {"stage": "deliverable", "reason": "turn_terminal"}
    return {"stage": "action", "reason": "turn_active"}


def _state(state: Mapping[str, object]) -> dict[str, object]:
    goal = _mapping(state, "goal")
    return {
        "phase": _string(state, "phase"),
        "goal": None if goal is None else {
            "goal_id": _string(goal, "goal_id"),
            "title": _string(goal, "title"),
            "success_criteria": list(_string_tuple(goal.get("success_criteria", ()))),
        },
        "tasks": [
            {"task_id": _string(item, "task_id"), "title": _string(item, "title"), "state": _string(item, "state")}
            for item in _mappings(state, "tasks")
        ],
        "blockers": [
            {"blocker_id": _string(item, "blocker_id"), "summary": _string(item, "summary"), "severity": _string(item, "severity")}
            for item in _mappings(state, "blockers")
        ],
        "observations": [
            {"observation_id": _string(item, "observation_id"), "category": _string(item, "category"), "summary": _string(item, "summary")}
            for item in _mappings(state, "observations")
        ],
        "confidence": state.get("confidence") if isinstance(state.get("confidence"), (int, float)) and not isinstance(state.get("confidence"), bool) else 0,
        "risk_codes": list(_string_tuple(state.get("risk_codes", ()))),
    }


def _dynamics(state: Mapping[str, object]) -> dict[str, object]:
    return {
        "predictions": [
            {
                "predicted_state": _string(item, "predicted_state"),
                "confidence": _number(item, "confidence"),
                "horizon": _string(item, "horizon"),
                "assumptions": list(_string_tuple(item.get("assumptions", ()))),
            }
            for item in _mappings(state, "predictions")
        ],
        "counterfactuals": [
            {
                "condition": _string(item, "condition"),
                "predicted_state": _string(item, "predicted_state"),
                "confidence": _number(item, "confidence"),
                "rationale": _string(item, "rationale"),
            }
            for item in _mappings(state, "counterfactuals")
        ],
    }


def _supervision(state: Mapping[str, object]) -> dict[str, object]:
    supervision = _mapping(state, "supervision")
    statuses = _mappings(supervision, "claim_statuses")
    active = _mappings(supervision, "active_claims")
    latest_verification = _mapping(supervision, "latest_verification")
    latest_decision = _mapping(supervision, "latest_decision")
    return {
        "state": _string(supervision, "state") or "no_claim",
        "active_count": len(active),
        "claims": [
            _supervision_claim(
                status,
                latest_verification=latest_verification,
                latest_decision=latest_decision,
            )
            for status in statuses
        ],
    }


def _supervision_claim(
    status: Mapping[str, object],
    *,
    latest_verification: Mapping[str, object] | None,
    latest_decision: Mapping[str, object] | None,
) -> dict[str, object]:
    claim = _mapping(status, "claim")
    verification = _mapping(status, "latest_verification")
    decision = _mapping(status, "latest_decision")
    action_id = _string(claim, "action_id")
    matched_verification = latest_verification if _string(latest_verification, "action_id") == action_id else None
    matched_decision = latest_decision if _string(latest_decision, "action_id") == action_id else None
    return {
        "action_id": action_id,
        "replaced_prior_direction": bool(_string(claim, "supersedes_claim_id")),
        "hypothesis": _string(claim, "hypothesis"),
        "expected": list(_string_tuple(claim.get("expected_signals", ()) if claim else ())),
        "falsification": list(_string_tuple(claim.get("falsification_signals", ()) if claim else ())),
        "pivot": list(_string_tuple(claim.get("pivot_conditions", ()) if claim else ())),
        "stop": list(_string_tuple(claim.get("stop_conditions", ()) if claim else ())),
        "state": _string(status, "state"),
        "verdict": _string(verification, "verdict") or None,
        "finding": _string(matched_verification, "finding") or None,
        "disposition": _string(decision, "disposition") or None,
        "rationale": _string(matched_decision, "rationale") or None,
    }


def _compaction(context: Mapping[str, object] | None) -> dict[str, object]:
    value = _mapping(context, "compaction")
    input_bytes = _nonnegative(value, "input_bytes")
    output_bytes = _nonnegative(value, "output_bytes")
    saved_bytes = _nonnegative(value, "saved_bytes")
    if output_bytes > input_bytes or saved_bytes != input_bytes - output_bytes:
        return {"applied": False, "count": 0, "input_bytes": 0, "output_bytes": 0, "saved_bytes": 0, "strategy_label": "未使用压缩"}
    return {
        "applied": bool(value and value.get("applied") is True),
        "count": _nonnegative(value, "count"),
        "input_bytes": input_bytes,
        "output_bytes": output_bytes,
        "saved_bytes": saved_bytes,
        "strategy_label": _string(value, "strategy_label") or "未使用压缩",
    }


def _deliverables(project_id: str, action: Mapping[str, object] | None) -> dict[str, object]:
    return {
        "library_href": f"#view=rebuild-library-overview&project_id={project_id}",
        "action_detail": None if action is None else {
            "action_id": action["action_id"],
            "turn_id": action["turn_id"],
            "status": action["status"],
            "terminal": action["terminal"],
        },
    }


def _trace(
    project_id: str,
    state: Mapping[str, object],
    action: Mapping[str, object] | None,
    organization: Mapping[str, object] | None,
    *,
    task_graph_for_project: Callable[[str], object | None] | None,
) -> dict[str, object]:
    """Compose bounded, renderer-safe operational trace summaries.

    The trace deliberately names only product concepts. It contains no record
    identities, references, revisions, source content, or runtime authority.
    """
    graph = task_graph_for_project(project_id) if task_graph_for_project is not None else None
    graph_trace = _trace_task_graph(graph, project_id)
    return {
        "dependencies": graph_trace["dependencies"] if graph_trace is not None else _trace_dependencies(state, action),
        "verification": graph_trace["verification"] if graph_trace is not None else _trace_verification(state, action),
        "freshness": graph_trace["freshness"] if graph_trace is not None else _trace_freshness(project_id, state),
        "agent_load": _trace_agent_load(organization),
        "direction_history": _trace_direction_history(state),
    }


def _trace_task_graph(graph: object | None, project_id: str) -> dict[str, object] | None:
    """Reduce one projected task graph to labels and bounded aggregate facts.

    Node IDs, provenance bindings, budgets, references, and event revisions
    remain inside the graph runtime.  The renderer receives only local ordinal
    labels and lifecycle counts.
    """
    spec = getattr(graph, "spec", None)
    nodes = getattr(graph, "nodes", None)
    if getattr(spec, "project_id", None) != project_id or not isinstance(nodes, Sequence):
        return None
    spec_nodes = getattr(spec, "nodes", None)
    if not isinstance(spec_nodes, Sequence) or len(spec_nodes) != len(nodes):
        return None
    ordered = tuple(spec_nodes[:12])
    node_numbers = {
        node_id: index + 1
        for index, item in enumerate(ordered)
        if isinstance((node_id := getattr(item, "node_id", None)), str)
    }
    if len(node_numbers) != len(ordered):
        return None
    states = {
        node_id: item
        for item in nodes
        if isinstance((node_id := getattr(item, "node_id", None)), str)
    }
    if set(node_numbers) - set(states):
        return None
    edges: list[dict[str, str]] = []
    for node_id, number in node_numbers.items():
        dependency_ids = getattr(next(item for item in ordered if getattr(item, "node_id", None) == node_id), "dependency_ids", ())
        if not isinstance(dependency_ids, Sequence) or isinstance(dependency_ids, (str, bytes)):
            return None
        for dependency_id in dependency_ids:
            if dependency_id in node_numbers:
                edges.append({
                    "from_label": f"任务节点 {number}",
                    "relation_label": "依赖于",
                    "to_label": f"任务节点 {node_numbers[dependency_id]}",
                    "state_label": _graph_state_label(getattr(states[node_id], "status", "")),
                })
    validation_values = [getattr(states[node_id], "validation_status", "") for node_id in node_numbers]
    statuses = [getattr(states[node_id], "status", "") for node_id in node_numbers]
    state_counts = _graph_state_counts(statuses)
    verified = sum(value == "verified" for value in validation_values)
    stale = sum(value in {"stale", "invalidated"} or status == "stale" for value, status in zip(validation_values, statuses))
    invalidated = sum(value == "invalidated" or status == "invalidated" for value, status in zip(validation_values, statuses))
    return {
        "dependencies": {
            "total": len(edges),
            "node_count": len(node_numbers),
            "waiting": sum(status in {"waiting_dependency", "blocked", "ready"} for status in statuses),
            "items": edges[:24],
            "state_counts": state_counts,
        },
        "verification": {
            "state": "verified" if verified == len(node_numbers) else "pending",
            "verified_count": verified,
            "pending_count": len(node_numbers) - verified,
            "latest_kind_label": "任务图节点核验",
        },
        "freshness": {
            "state": "stale" if stale else "fresh",
            "stale_count": stale,
            "invalidated_count": invalidated,
            "affected_kind_labels": ["任务图节点"] if stale else [],
            "review_library_link": f"#view=rebuild-library-overview&project_id={project_id}" if stale else None,
        },
    }


def _graph_state_counts(statuses: Sequence[object]) -> list[dict[str, object]]:
    counts: dict[str, int] = {}
    for status in statuses:
        label = _graph_state_label(status)
        counts[label] = counts.get(label, 0) + 1
    return [{"label": label, "count": count} for label, count in counts.items()]


def _graph_state_label(value: object) -> str:
    return {
        "ready": "可执行", "waiting_dependency": "等待依赖", "blocked": "依赖受阻",
        "dispatching": "正在派发", "leased": "正在执行", "awaiting_validation": "等待核验",
        "settled": "已完成", "rejected": "核验未通过", "stale": "需要复核",
        "invalidated": "前提已失效", "pruned": "已收敛", "cancel_requested": "正在中止",
        "cancelled": "已中止",
    }.get(value, "状态待确认")


def _trace_dependencies(state: Mapping[str, object], action: Mapping[str, object] | None) -> dict[str, object]:
    items: list[dict[str, str]] = []
    if _mapping(state, "goal") is not None:
        items.append({"from_label": "项目目标", "relation_label": "决定", "to_label": "当前计划", "state_label": "已建立"})
    if action is not None:
        items.append({"from_label": "当前计划", "relation_label": "进入", "to_label": "受治理执行", "state_label": _action_state_label(_string(action, "status"))})
    if bool(action and action.get("ready_for_feedback")):
        items.append({"from_label": "执行结果", "relation_label": "等待", "to_label": "结果反馈", "state_label": "等待确认"})
    waiting = sum(1 for item in items if item["state_label"] in {"等待确认", "等待执行"})
    return {"total": len(items), "waiting": waiting, "items": items[:12]}


def _trace_verification(state: Mapping[str, object], action: Mapping[str, object] | None) -> dict[str, object]:
    governance = _mapping(action, "governance")
    checks = (
        ("gate", {"passed"}),
        ("effect", {"settled"}),
        ("receipt", {"terminal"}),
        ("feedback", {"recorded"}),
    )
    verified = sum(1 for key, accepted in checks if _string(governance, key) in accepted)
    pending = len(checks) - verified
    if action is None:
        return {"state": "not_available", "verified_count": 0, "pending_count": 0, "latest_kind_label": "尚无行动核验"}
    if pending == 0:
        return {"state": "verified", "verified_count": verified, "pending_count": 0, "latest_kind_label": "反馈核验"}
    latest = "结果反馈" if isinstance(state.get("latest_feedback_id"), str) else "执行回执"
    return {"state": "pending", "verified_count": verified, "pending_count": pending, "latest_kind_label": latest}


def _trace_freshness(project_id: str, state: Mapping[str, object]) -> dict[str, object]:
    risk_codes = set(_string_tuple(state.get("risk_codes", ())))
    affected: list[str] = []
    if "observations_stale" in risk_codes:
        affected.append("项目观察")
    if not affected:
        return {"state": "fresh", "stale_count": 0, "affected_kind_labels": [], "review_library_link": None}
    return {
        "state": "stale",
        "stale_count": len(affected),
        "affected_kind_labels": affected,
        "review_library_link": f"#view=rebuild-library-overview&project_id={project_id}",
    }


def _trace_agent_load(organization: Mapping[str, object] | None) -> dict[str, int]:
    agents = [item for item in (_mapping(organization, "main"), _mapping(organization, "steward")) if item]
    agents.extend(
        assignment
        for cluster in _mappings(organization, "expert_clusters")
        for assignment in _mappings(cluster, "assignments")
    )
    statuses = [_string(item, "status") for item in agents]
    return {
        "running": sum(status in {"starting", "running", "working"} for status in statuses),
        "waiting": sum(status in {"created", "queued", "waiting", "waiting_approval", "recovery_required"} for status in statuses),
        "cancelling": sum(status in {"cancelling"} for status in statuses),
        "attention": sum(status in {"failed", "timed_out", "quarantined"} for status in statuses),
    }


def _trace_direction_history(state: Mapping[str, object]) -> list[dict[str, object]]:
    supervision = _mapping(state, "supervision")
    result: list[dict[str, object]] = []
    for status in _mappings(supervision, "claim_statuses")[-5:]:
        claim, decision = _mapping(status, "claim"), _mapping(status, "latest_decision")
        result.append({
            "stage_label": "已从上一方向切换" if bool(_string(claim, "supersedes_claim_id")) else "当前方向",
            "state_label": _supervision_state_label(_string(status, "state")),
            "disposition_label": _disposition_label(_string(decision, "disposition")),
            "replaced": bool(_string(claim, "supersedes_claim_id")),
        })
    return result


def _action_state_label(value: str) -> str:
    return {
        "created": "等待执行", "queued": "等待执行", "starting": "正在执行", "running": "正在执行",
        "waiting": "等待确认", "completed": "已完成", "failed": "需要关注", "cancelled": "已取消",
    }.get(value, "状态待确认")


def _supervision_state_label(value: str) -> str:
    return {
        "on_track": "运行正常", "unverified": "等待核验", "at_risk": "需要纠偏",
        "invalidated": "前提已失效", "stop_required": "需要停止",
    }.get(value, "状态待确认")


def _disposition_label(value: str) -> str:
    return {
        "continue": "继续推进", "replan_required": "需要重新规划",
        "stop_required": "需要停止", "escalate_user": "需要你决策",
    }.get(value, "尚无决策")


def _governance(value: Mapping[str, object] | None) -> dict[str, str]:
    return {
        key: _string(value, key)
        for key in ("gate", "effect", "handler", "receipt", "feedback")
    }


def _organization(value: Mapping[str, object], project_id: str) -> dict[str, object]:
    if value.get("project_id") != project_id:
        raise ValueError("task case organization scope drifted")
    return {
        "has_active_run": bool(value.get("has_active_run", False)),
        "projection_revision": _string(value, "projection_revision"),
        "overview": _organization_overview(_mapping(value, "overview")),
        "main": _agent(_mapping(value, "main")),
        "steward": _agent(_mapping(value, "steward")),
        "expert_clusters": [
            {
                "cluster_id": _string(cluster, "cluster_id"),
                "status": _string(cluster, "status"),
                "mode": _string(cluster, "mode"),
                "assignments": [_agent(item) for item in _mappings(cluster, "assignments")],
            }
            for cluster in _mappings(value, "expert_clusters")
        ],
    }


def _organization_overview(value: Mapping[str, object] | None) -> dict[str, object]:
    progress, load = _mapping(value, "progress"), _mapping(value, "load")
    return {
        "status": _string(value, "status"),
        "progress": {"completed": _nonnegative(progress, "completed"), "total": _nonnegative(progress, "total")},
        "load": {"active": _nonnegative(load, "active"), "queued": _nonnegative(load, "queued"), "capacity": _nonnegative(load, "capacity")},
    }


def _agent(value: Mapping[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    task = _mapping(value, "task")
    return {
        "profile_id": _string(value, "profile_id"),
        "display_name": _string(value, "display_name"),
        "organization_role": _string(value, "organization_role"),
        "role": _string(value, "role"),
        "model_tier": _string(value, "model_tier"),
        "work_description": _string(value, "work_description"),
        "status": _string(value, "status"),
        "expert_identity": _string(value, "expert_identity") or None,
        "skill_ids": list(_string_tuple(value.get("skill_ids", ()))),
        "task": None if task is None else {
            "assignment_id": _string(task, "assignment_id"),
            "label": _string(task, "label"),
            "expert_id": _string(task, "expert_id"),
            "skill_ids": list(_string_tuple(task.get("skill_ids", ()))),
            "status": _string(task, "status"),
        },
    }


def _assert_public(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or _forbidden(key):
                raise ValueError("task case public contract rejected")
            _assert_public(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _assert_public(item)


def _forbidden(key: str) -> bool:
    compact = "".join(character for character in key.lower() if character.isalnum())
    if compact in {"modeltier", "modelcalls", "libraryhref"}:
        return False
    return compact.startswith(("prompt", "context", "ref", "provider", "model", "endpoint", "secret", "credential", "token", "apikey", "path")) or compact.endswith(("prompt", "context", "ref", "provider", "model", "endpoint", "secret", "credential", "token", "apikey", "path"))


def _mapping(value: Mapping[str, object] | None, key: str) -> Mapping[str, object] | None:
    item = value.get(key) if value is not None else None
    return item if isinstance(item, Mapping) else None


def _mappings(value: Mapping[str, object] | None, key: str) -> tuple[Mapping[str, object], ...]:
    item = value.get(key) if value is not None else ()
    return tuple(candidate for candidate in item if isinstance(candidate, Mapping)) if isinstance(item, Sequence) and not isinstance(item, (str, bytes)) else ()


def _string(value: Mapping[str, object] | None, key: str) -> str:
    item = value.get(key) if value is not None else None
    return item if isinstance(item, str) else ""


def _string_tuple(value: object) -> tuple[str, ...]:
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def _nonnegative(value: Mapping[str, object] | None, key: str) -> int:
    item = value.get(key) if value is not None else 0
    return item if isinstance(item, int) and not isinstance(item, bool) and item >= 0 else 0


def _number(value: Mapping[str, object] | None, key: str) -> int | float:
    item = value.get(key) if value is not None else 0
    return item if isinstance(item, (int, float)) and not isinstance(item, bool) else 0


def _revision(result: Mapping[str, object], state: Mapping[str, object]) -> str:
    action = _mapping(result, "action")
    lifecycle = _mapping(result, "lifecycle")
    organization = _mapping(result, "organization")
    overview = _mapping(organization, "overview")
    return "|".join((
        f"world:{_nonnegative(state, 'through_sequence')}",
        f"stage:{_string(lifecycle, 'stage')}",
        f"action:{_string(action, 'action_id')}:{_string(action, 'status')}",
        f"feedback:{_string(state, 'latest_feedback_id')}",
        f"organization:{_string(organization, 'projection_revision')}:{_string(overview, 'status')}",
    ))
