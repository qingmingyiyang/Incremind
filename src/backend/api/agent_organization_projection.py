"""Safe, provider-free read model for the Agent organization screen.

This module is intentionally an adapter over the established profile,
coordinator, and dispatch authorities.  It never dereferences immutable task
payloads and does not expose opaque authority references to the renderer.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from core.ai_kernel.agent_dispatch_contracts import parse_canonical_dispatch_ref


_TERMINAL = frozenset({"completed", "failed", "cancelled", "timed_out", "quarantined", "stopped"})
_ACTIVE = frozenset({"created", "queued", "starting", "running", "waiting", "waiting_approval", "cancelling", "recovery_required"})
_EXECUTING = frozenset({"starting", "running", "waiting", "waiting_approval", "cancelling", "recovery_required"})
_TERMINAL_PLAN = frozenset({"completed", "failed", "cancelled", "superseded"})


def build_agent_organization_projection(
    *, project_id: str, profiles: object, topology: Mapping[str, object] | None = None,
    dispatch_store: object | None = None,
) -> dict[str, object]:
    """Return the renderer-safe organization projection for one project.

    ``topology`` is the coordinator's already-authorized view for an optional
    main Turn.  Its absence deliberately produces a useful profile skeleton
    without inventing a run or selecting historical work.
    """
    profile_items = _profile_items(profiles)
    run = _mapping(topology, "run") if topology is not None else None
    children = _mappings(topology, "children") if topology is not None else ()
    plans = _projection_plans(project_id, run, _mappings(topology, "plans") if topology is not None else (), dispatch_store)
    child_by_run_id = _runs_by_id(children)
    assignments = _assignments(project_id, plans, dispatch_store, child_by_run_id)
    main = _agent_node(_profile_by_id(profile_items, "main.orchestrator"), run, assignment=None)
    steward_run = _find_profile_run(children, "steward.scheduler")
    steward = _agent_node(_profile_by_id(profile_items, "steward.scheduler"), steward_run, assignment=None)
    clusters = _cluster_items(profile_items, assignments, child_by_run_id, plans)
    all_tasks = [item for cluster in clusters for item in cluster["assignments"]]
    completed = sum(1 for item in all_tasks if item["status"] == "completed")
    active = sum(1 for item in all_tasks if item["status"] in _EXECUTING)
    total = len(all_tasks)
    overview = {
        "status": _organization_status(run, all_tasks),
        "progress": {"completed": completed, "total": total},
        "load": {"active": active, "queued": sum(1 for item in all_tasks if item["status"] in {"created", "queued"}), "capacity": _capacity(plans)},
    }
    result: dict[str, object] = {
        "schema_version": "1.0.0",
        "project_id": project_id,
        "has_active_run": run is not None,
        "main": main,
        "steward": steward,
        "expert_clusters": clusters,
        "profiles": profile_items,
        "overview": overview,
    }
    result["projection_revision"] = _revision(result)
    return result


def _profile_items(profiles: object) -> list[dict[str, object]]:
    listing = getattr(profiles, "list_profiles", None)
    if not callable(listing):
        raise RuntimeError("agent profiles are unavailable")
    values = listing()
    if not isinstance(values, Sequence):
        raise RuntimeError("agent profiles are unavailable")
    return [_profile_item(value) for value in values]


def _profile_item(profile: object) -> dict[str, object]:
    profile_id = _string_attr(profile, "profile_id")
    return {
        "profile_id": profile_id,
        "revision": _int_attr(profile, "revision", 1),
        "display_name": _string_attr(profile, "display_name") or profile_id,
        "organization_role": _string_attr(profile, "organization_role") or "受管 Agent",
        "role": _string_attr(profile, "role") or "subagent",
        "model_tier": _string_attr(profile, "model_tier") or "standard",
        "model_route_key": _string_attr(profile, "model_route_key") or None,
        "model_route_revision": _optional_positive_attr(profile, "model_route_revision"),
        "enabled": bool(getattr(profile, "enabled", False)),
        "work_description": _string_attr(profile, "work_description"),
        # Expert and Skill identities are attached by the frozen assignment,
        # never copied into the mutable profile authority.
        "expert_identity": None,
        "skill_ids": (),
    }


def _agent_node(profile: Mapping[str, object] | None, run: Mapping[str, object] | None, *, assignment: Mapping[str, object] | None) -> dict[str, object] | None:
    if profile is None:
        return None
    task = _safe_task_status(assignment)
    status = _string(run, "status") or _string(assignment, "status") or "idle"
    frozen_route_key = _string(run, "model_route_key") or None
    frozen_route_revision = _optional_positive(run, "model_route_revision")
    return {
        **profile,
        "run_id": _string(run, "run_id") if run else None,
        "status": status,
        "expert_identity": _string(assignment, "expert_id") or None,
        "skill_ids": _string_tuple(assignment.get("skill_ids", ())) if assignment else (),
        "task": task,
        # A running Turn keeps the route that was bound at admission.  The
        # mutable Profile value remains available only when no run exists.
        "model_route_key": frozen_route_key or profile["model_route_key"],
        "model_route_revision": frozen_route_revision if frozen_route_key else profile["model_route_revision"],
        "route_binding_source": "frozen_run" if frozen_route_key else "profile",
    }


def _cluster_items(profiles: list[dict[str, object]], assignments: list[dict[str, object]], child_by_run_id: Mapping[str, Mapping[str, object]], plans: tuple[Mapping[str, object], ...]) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for assignment in assignments:
        grouped.setdefault(str(assignment["cluster_id"]), []).append(assignment)
    plan_by_cluster = {str(item.get("cluster_id")): item for item in plans if isinstance(item.get("cluster_id"), str)}
    result: list[dict[str, object]] = []
    for cluster_id in sorted(grouped):
        members: list[dict[str, object]] = []
        for assignment in grouped[cluster_id]:
            profile = _profile_by_id(profiles, str(assignment["profile_id"]))
            child_run_id = assignment.get("child_run_id")
            run = child_by_run_id.get(child_run_id) if isinstance(child_run_id, str) else None
            node = _agent_node(profile, run, assignment=assignment)
            if node is not None:
                members.append(node)
        plan = plan_by_cluster.get(cluster_id, {})
        plan_status = _string(plan, "status")
        result.append({
            "cluster_id": cluster_id,
            "status": plan_status if plan_status in _TERMINAL_PLAN else _cluster_status(members),
            "mode": _string(plan, "mode") or "cluster",
            "assignments": members,
        })
    return result


def _assignments(
    project_id: str,
    plans: tuple[Mapping[str, object], ...],
    dispatch_store: object | None,
    child_by_run_id: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    if dispatch_store is None:
        return []
    get_assignment = getattr(dispatch_store, "get_assignment", None)
    list_permits = getattr(dispatch_store, "list_permits", None)
    get_child_binding = getattr(dispatch_store, "get_permit_child_run_binding", None)
    if not callable(get_assignment) or not callable(list_permits):
        return []
    result: list[dict[str, object]] = []
    for plan in plans:
        plan_id = _string(plan, "plan_id")
        if not plan_id:
            continue
        permits = {str(getattr(item, "assignment_id", "")): item for item in list_permits(project_id=project_id, plan_id=plan_id)}
        for assignment_id in _string_tuple(plan.get("assignment_ids", ())):
            assignment = get_assignment(assignment_id, project_id=project_id)
            if assignment is None:
                continue
            permit = permits.get(assignment_id)
            permit_id = _string_attr(permit, "permit_id")
            child_run_id = (
                get_child_binding(permit_id, project_id=project_id)
                if permit_id and callable(get_child_binding)
                else None
            )
            child_run = child_by_run_id.get(child_run_id) if isinstance(child_run_id, str) else None
            result.append({
                "assignment_id": assignment_id,
                "cluster_id": _string_attr(assignment, "cluster_id") or "unassigned",
                "profile_id": _profile_id_from_ref(_string_attr(assignment, "profile_ref")),
                "expert_id": _string_attr(assignment, "expert_id"),
                "skill_ids": _string_tuple(getattr(assignment, "skill_ids", ())),
                "status": _assignment_status(plan, permit, child_run),
                "plan_id": plan_id,
                # This is a safe identity used only to join the already
                # authorized topology.  It is not copied into the task card.
                "child_run_id": child_run_id,
            })
    return result


def _projection_plans(project_id: str, run: Mapping[str, object] | None, fallback: tuple[Mapping[str, object], ...], dispatch_store: object | None) -> tuple[Mapping[str, object], ...]:
    """Hydrate only plan identities/state needed for the public read model."""
    list_for_main = getattr(dispatch_store, "list_plans_for_main", None) if dispatch_store is not None else None
    main_run_id = _string(run, "run_id")
    if not main_run_id or not callable(list_for_main):
        return fallback
    values = list_for_main(project_id=project_id, main_run_id=main_run_id)
    result: list[Mapping[str, object]] = []
    for plan in values:
        cluster_ref = getattr(plan, "expert_cluster_ref", None)
        result.append({
            "plan_id": _string_attr(plan, "plan_id"),
            "status": _string_attr(plan, "status"),
            "mode": _string_attr(plan, "mode"),
            "revision": _int_attr(plan, "revision", 1),
            "assignment_ids": _string_tuple(getattr(plan, "assignment_ids", ())),
            "max_concurrent_assignments": _int_attr(plan, "max_concurrent_assignments", 0),
            "cluster_id": parse_canonical_dispatch_ref(cluster_ref, "cluster") if cluster_ref else None,
        })
    return tuple(result)


def _assignment_status(
    plan: Mapping[str, object],
    permit: object | None,
    child_run: Mapping[str, object] | None,
) -> str:
    run_status = _string(child_run, "status")
    if run_status:
        return run_status
    status = _string_attr(permit, "status")
    if status == "issued":
        return "queued"
    if status == "consumed":
        return "running" if _string(plan, "status") == "dispatched" else "starting"
    if status == "revoked":
        return "cancelled"
    return _string(plan, "status") or "queued"


def _safe_task_status(assignment: Mapping[str, object] | None) -> dict[str, object] | None:
    if assignment is None:
        return None
    return {
        "assignment_id": assignment["assignment_id"],
        "label": "已分配专家任务",
        "expert_id": assignment["expert_id"],
        "skill_ids": assignment["skill_ids"],
        "status": assignment["status"],
    }


def _capacity(plans: tuple[Mapping[str, object], ...]) -> int:
    values = [item.get("max_concurrent_assignments") for item in plans]
    return max((item for item in values if isinstance(item, int) and not isinstance(item, bool)), default=0)


def _organization_status(run: Mapping[str, object] | None, tasks: list[dict[str, object]]) -> str:
    if run is None:
        return "idle"
    status = _string(run, "status") or "unknown"
    if status in _TERMINAL:
        return status
    if any(item["status"] in _ACTIVE for item in tasks):
        return "working"
    return status


def _cluster_status(members: list[dict[str, object]]) -> str:
    statuses = {str(item.get("status")) for item in members}
    if "running" in statuses or "starting" in statuses:
        return "working"
    if statuses and statuses <= {"completed"}:
        return "completed"
    return "queued"


def _revision(value: Mapping[str, object]) -> str:
    """A deterministic read-model version, deliberately independent of SSE IDs."""
    profiles = value.get("profiles", [])
    profile_bits = ",".join(
        f"{item['profile_id']}:{item['revision']}:{item.get('model_route_key') or '-'}:{item.get('model_route_revision') or '-'}"
        for item in profiles if isinstance(item, Mapping)
    )
    nodes = [value.get("main"), value.get("steward")]
    cluster_bits: list[str] = []
    for cluster in value.get("expert_clusters", []):
        if not isinstance(cluster, Mapping):
            continue
        members = cluster.get("assignments", [])
        nodes.extend(members)
        member_bits = ",".join(
            ":".join((
                str(item.get("task", {}).get("assignment_id", "")) if isinstance(item.get("task"), Mapping) else "",
                str(item.get("run_id") or ""),
                str(item.get("status") or ""),
                str(item.get("expert_identity") or ""),
                "+".join(_string_tuple(item.get("skill_ids", ()))),
            ))
            for item in members
            if isinstance(item, Mapping)
        )
        cluster_bits.append(
            f"{cluster.get('cluster_id')}:{cluster.get('mode')}:{cluster.get('status')}[{member_bits}]"
        )
    state_bits = ",".join(
        f"{item.get('run_id') or item.get('profile_id')}:{item.get('status')}:{item.get('model_route_key') or '-'}:{item.get('model_route_revision') or '-'}"
        for item in nodes if isinstance(item, Mapping)
    )
    overview = value.get("overview", {}) if isinstance(value.get("overview"), Mapping) else {}
    progress = overview.get("progress", {}) if isinstance(overview.get("progress"), Mapping) else {}
    load = overview.get("load", {}) if isinstance(overview.get("load"), Mapping) else {}
    return (
        f"profiles[{profile_bits}]|state[{state_bits}]|clusters[{';'.join(cluster_bits)}]"
        f"|progress[{progress.get('completed', 0)}/{progress.get('total', 0)}]"
        f"|load[{load.get('active', 0)}/{load.get('queued', 0)}/{load.get('capacity', 0)}]"
    )


def _runs_by_id(children: tuple[Mapping[str, object], ...]) -> dict[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    for child in children:
        run_id = _string(child, "run_id")
        if run_id:
            result[run_id] = child
    return result


def _find_profile_run(children: tuple[Mapping[str, object], ...], profile_id: str) -> Mapping[str, object] | None:
    return next((item for item in children if _string(item, "profile_id") == profile_id), None)


def _profile_by_id(profiles: list[dict[str, object]], profile_id: str) -> dict[str, object] | None:
    return next((item for item in profiles if item["profile_id"] == profile_id), None)


def _profile_id_from_ref(value: str) -> str:
    marker = "/profiles/"
    return value.rsplit(marker, 1)[-1].split("/", 1)[0] if marker in value else ""


def _mapping(value: Mapping[str, object] | None, key: str) -> Mapping[str, object] | None:
    item = value.get(key) if value is not None else None
    return item if isinstance(item, Mapping) else None


def _mappings(value: Mapping[str, object] | None, key: str) -> tuple[Mapping[str, object], ...]:
    item = value.get(key) if value is not None else ()
    return tuple(entry for entry in item if isinstance(entry, Mapping)) if isinstance(item, Sequence) and not isinstance(item, str) else ()


def _string(value: Mapping[str, object] | None, key: str) -> str:
    item = value.get(key) if value is not None else None
    return item if isinstance(item, str) else ""


def _string_attr(value: object, key: str) -> str:
    item = getattr(value, key, "")
    return item if isinstance(item, str) else ""


def _int_attr(value: object, key: str, default: int) -> int:
    item = getattr(value, key, default)
    return item if isinstance(item, int) and not isinstance(item, bool) else default


def _optional_positive(value: Mapping[str, object] | None, key: str) -> int | None:
    item = value.get(key) if value is not None else None
    return item if isinstance(item, int) and not isinstance(item, bool) and item > 0 else None


def _optional_positive_attr(value: object, key: str) -> int | None:
    item = getattr(value, key, None)
    return item if isinstance(item, int) and not isinstance(item, bool) and item > 0 else None


def _string_tuple(value: object) -> tuple[str, ...]:
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, Sequence) and not isinstance(value, str) else ()
