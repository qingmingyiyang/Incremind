"""SQLite persistence for dispatch planning, separate from Turn execution.

The store shares the AI Turn database solely for lifecycle colocation.  It
requires the existing ``ai_turns`` table but never creates, claims, or mutates
Turns, runners, effects, providers, or model configuration.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
import sqlite3
from core.storage_provider.observability import observe_connection

from .agent_dispatch_contracts import (
    AgentDispatchPlan, CapacitySnapshot, DispatchPermit, ExpertAssignment,
    ExpertCluster, IntakeRoutingReceipt, WorkloadSnapshot,
    agent_dispatch_plan_from_payload, agent_dispatch_plan_to_payload,
    capacity_snapshot_from_payload, capacity_snapshot_to_payload,
    dispatch_permit_from_payload, dispatch_permit_to_payload,
    expert_assignment_from_payload, expert_assignment_to_payload,
    expert_cluster_from_payload, expert_cluster_to_payload,
    intake_routing_receipt_from_payload, intake_routing_receipt_to_payload,
    workload_snapshot_from_payload, workload_snapshot_to_payload,
    parse_canonical_dispatch_ref,
)


class AgentDispatchStoreError(RuntimeError): pass
class AgentDispatchStoreNotFound(AgentDispatchStoreError): pass
class AgentDispatchStoreConflict(AgentDispatchStoreError): pass
class AgentDispatchStoreInvalid(AgentDispatchStoreError): pass


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class SQLiteAgentDispatchStore:
    """Durable, project-scoped dispatch planning with CAS and idempotency."""

    def __init__(self, database_path: Path) -> None:
        self._path = database_path.expanduser().resolve(strict=False)
        if not self._path.exists(): raise AgentDispatchStoreNotFound("AI Turn database does not exist")
        connection = self._connect()
        try:
            if not _table_exists(connection, "ai_turns"): raise AgentDispatchStoreNotFound("AI Turn authority table is required")
            _initialize_schema(connection)
        finally: connection.close()

    def put_intake_receipt(self, value: IntakeRoutingReceipt, *, operation_id: str) -> tuple[IntakeRoutingReceipt, bool]:
        return self._put_immutable("intake", "ai_agent_dispatch_intakes", "receipt_id", value.receipt_id, value.project_id, intake_routing_receipt_to_payload(value), intake_routing_receipt_from_payload, value, operation_id)

    def put_workload_snapshot(self, value: WorkloadSnapshot, *, operation_id: str) -> tuple[WorkloadSnapshot, bool]:
        return self._put_immutable("workload", "ai_agent_dispatch_workloads", "snapshot_id", value.snapshot_id, value.project_id, workload_snapshot_to_payload(value), workload_snapshot_from_payload, value, operation_id)

    def put_capacity_snapshot(self, value: CapacitySnapshot, *, operation_id: str) -> tuple[CapacitySnapshot, bool]:
        return self._put_immutable("capacity", "ai_agent_dispatch_capacities", "snapshot_id", value.snapshot_id, value.project_id, capacity_snapshot_to_payload(value), capacity_snapshot_from_payload, value, operation_id)

    def put_expert_cluster(self, value: ExpertCluster, *, operation_id: str) -> tuple[ExpertCluster, bool]:
        return self._put_immutable("cluster", "ai_agent_dispatch_clusters", "cluster_id", value.cluster_id, value.project_id, expert_cluster_to_payload(value), expert_cluster_from_payload, value, operation_id)

    def put_assignment(self, value: ExpertAssignment, *, operation_id: str) -> tuple[ExpertAssignment, bool]:
        return self._put_immutable("assignment", "ai_agent_dispatch_assignments", "assignment_id", value.assignment_id, value.project_id, expert_assignment_to_payload(value), expert_assignment_from_payload, value, operation_id)

    def create_plan(self, value: AgentDispatchPlan, *, operation_id: str) -> tuple[AgentDispatchPlan, bool]:
        self._validate_plan_dependencies(value)
        return self._put_immutable("plan.create", "ai_agent_dispatch_plans", "plan_id", value.plan_id, value.project_id, agent_dispatch_plan_to_payload(value), agent_dispatch_plan_from_payload, value, operation_id, extra=(value.revision, value.status, value.effect_state))

    def publish_ready_plan(
        self,
        draft: AgentDispatchPlan,
        *,
        cluster: ExpertCluster | None,
        assignments: tuple[ExpertAssignment, ...],
        permits: tuple[DispatchPermit, ...],
        operation_id: str,
    ) -> tuple[AgentDispatchPlan, tuple[DispatchPermit, ...]]:
        """Atomically publish one frozen draft as ready, with all its permits."""
        if draft.status != "draft" or draft.revision != 1 or draft.effect_state != "none":
            raise AgentDispatchStoreInvalid("published plan must begin as draft revision one")
        if draft.mode == "main_only":
            if cluster is not None or assignments or permits:
                raise AgentDispatchStoreInvalid("main-only publish cannot carry dispatch work")
        elif cluster is None or len(assignments) != len(draft.assignment_ids) or len(permits) != len(assignments):
            raise AgentDispatchStoreInvalid("cluster publish is incomplete")
        ready = replace(draft, revision=draft.revision + 1, status="ready")
        request = _json({
            "draft": agent_dispatch_plan_to_payload(draft),
            "cluster": expert_cluster_to_payload(cluster) if cluster is not None else None,
            "assignments": [expert_assignment_to_payload(item) for item in assignments],
            "permits": [dispatch_permit_to_payload(item) for item in permits],
        })
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._operation(connection, operation_id, "plan.publish_ready", request)
            if replay is not None:
                result = _load(replay); connection.execute("COMMIT")
                return agent_dispatch_plan_from_payload(result["plan"]), tuple(dispatch_permit_from_payload(item) for item in result["permits"])
            if connection.execute("SELECT 1 FROM ai_agent_dispatch_plans WHERE plan_id=?", (draft.plan_id,)).fetchone() is not None:
                raise AgentDispatchStoreConflict("published plan identity already exists")
            if cluster is not None:
                self._insert_atomic_immutable(connection, "ai_agent_dispatch_clusters", "cluster_id", cluster.cluster_id, cluster.project_id, expert_cluster_to_payload(cluster), expert_cluster_from_payload, cluster)
            for assignment in assignments:
                self._insert_atomic_immutable(connection, "ai_agent_dispatch_assignments", "assignment_id", assignment.assignment_id, assignment.project_id, expert_assignment_to_payload(assignment), expert_assignment_from_payload, assignment)
            self._validate_plan_dependencies_in_connection(connection, draft)
            connection.execute("INSERT INTO ai_agent_dispatch_plans(plan_id,project_id,revision,status,effect_state,payload_json) VALUES(?,?,?,?,?,?)", (draft.plan_id, draft.project_id, draft.revision, draft.status, draft.effect_state, _json(agent_dispatch_plan_to_payload(draft))))
            changed = connection.execute("UPDATE ai_agent_dispatch_plans SET revision=?,status=?,payload_json=? WHERE plan_id=? AND project_id=? AND revision=? AND status='draft'", (ready.revision, ready.status, _json(agent_dispatch_plan_to_payload(ready)), draft.plan_id, draft.project_id, draft.revision)).rowcount
            if changed != 1: raise AgentDispatchStoreConflict("published plan revision conflict")
            self._validate_publish_permits(connection, ready, permits)
            for permit in permits:
                connection.execute("INSERT INTO ai_agent_dispatch_permits(permit_id,project_id,plan_id,assignment_id,status,payload_json) VALUES(?,?,?,?,?,?)", (permit.permit_id, permit.project_id, permit.plan_id, permit.assignment_id, permit.status, _json(dispatch_permit_to_payload(permit))))
            result = {"plan": agent_dispatch_plan_to_payload(ready), "permits": [dispatch_permit_to_payload(item) for item in permits]}
            self._record_operation(connection, operation_id, "plan.publish_ready", request, _json(result))
            connection.execute("COMMIT")
            return ready, permits
        except Exception:
            _rollback(connection); raise
        finally: connection.close()

    def get_plan(self, plan_id: str, *, project_id: str) -> AgentDispatchPlan | None:
        row = self._read_one("SELECT payload_json FROM ai_agent_dispatch_plans WHERE plan_id=? AND project_id=?", (plan_id, project_id))
        return agent_dispatch_plan_from_payload(_load(row[0])) if row else None

    def get_plan_with_revision(self, plan_id: str, *, project_id: str) -> tuple[AgentDispatchPlan, int] | None:
        row = self._read_one("SELECT revision,payload_json FROM ai_agent_dispatch_plans WHERE plan_id=? AND project_id=?", (plan_id, project_id))
        return (agent_dispatch_plan_from_payload(_load(row[1])), int(row[0])) if row else None

    def list_active_plans(self, *, project_id: str, main_run_id: str, steward_run_id: str) -> tuple[AgentDispatchPlan, ...]:
        """Read ready/dispatching plans for one durable main/steward pair."""
        _run_id(main_run_id); _run_id(steward_run_id)
        connection = self._connect()
        try:
            rows = connection.execute("SELECT payload_json FROM ai_agent_dispatch_plans WHERE project_id=? AND status IN ('ready','dispatching') ORDER BY plan_id", (project_id,)).fetchall()
            return tuple(
                plan for row in rows
                if (plan := agent_dispatch_plan_from_payload(_load(row[0]))).main_run_id == main_run_id
                and plan.steward_run_id == steward_run_id
            )
        finally: connection.close()

    def list_recovery_plans(self, *, limit: int = 64) -> tuple[AgentDispatchPlan, ...]:
        """Return a bounded cross-project view of plans safe to progress.

        Recovery only receives ready, dispatching, or dispatched plans.  Draft
        plans have not been published by the steward; terminal and superseded
        plans never need execution replay.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 256:
            raise ValueError("dispatch recovery plan limit must be between 1 and 256")
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT payload_json FROM ai_agent_dispatch_plans "
                "WHERE status IN ('ready','dispatching','dispatched') "
                "ORDER BY project_id,plan_id LIMIT ?",
                (limit,),
            ).fetchall()
            return tuple(agent_dispatch_plan_from_payload(_load(row[0])) for row in rows)
        finally:
            connection.close()

    def list_plans_for_main(self, *, project_id: str, main_run_id: str) -> tuple[AgentDispatchPlan, ...]:
        """Read every durable plan for one main Run, including terminal plans.

        Main coordination must observe the steward's published decision before
        it may synthesize.  Restricting that view to active plans would hide a
        completed ``main_only`` plan and reopen the steward-to-main race.
        """
        _run_id(main_run_id)
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT payload_json FROM ai_agent_dispatch_plans WHERE project_id=? ORDER BY plan_id",
                (project_id,),
            ).fetchall()
            return tuple(
                plan for row in rows
                if (plan := agent_dispatch_plan_from_payload(_load(row[0]))).main_run_id == main_run_id
            )
        finally: connection.close()

    def list_permits(self, *, project_id: str, plan_id: str | None = None) -> tuple[DispatchPermit, ...]:
        if plan_id is not None: _run_id(plan_id)
        connection = self._connect()
        try:
            query = "SELECT payload_json FROM ai_agent_dispatch_permits WHERE project_id=?" + (" AND plan_id=?" if plan_id is not None else "") + " ORDER BY permit_id"
            rows = connection.execute(query, (project_id, plan_id) if plan_id is not None else (project_id,)).fetchall()
            return tuple(dispatch_permit_from_payload(_load(row[0])) for row in rows)
        finally: connection.close()

    def transition_plan(self, value: AgentDispatchPlan, *, expected_revision: int, operation_id: str) -> AgentDispatchPlan:
        if expected_revision < 1 or value.revision != expected_revision + 1: raise AgentDispatchStoreConflict("plan version must advance exactly once")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._operation(connection, operation_id, "plan.transition", _json(agent_dispatch_plan_to_payload(value)))
            if replay is not None: connection.execute("COMMIT"); return agent_dispatch_plan_from_payload(_load(replay))
            row = connection.execute("SELECT payload_json FROM ai_agent_dispatch_plans WHERE plan_id=? AND project_id=? AND revision=?", (value.plan_id, value.project_id, expected_revision)).fetchone()
            if row is None: raise AgentDispatchStoreConflict("plan revision conflict")
            prior = agent_dispatch_plan_from_payload(_load(row[0]))
            if not _same_plan_snapshot(prior, value) or value.status not in _PLAN_TRANSITIONS.get(prior.status, frozenset()): raise AgentDispatchStoreInvalid("plan transition is invalid")
            changed = connection.execute("UPDATE ai_agent_dispatch_plans SET revision=?,status=?,effect_state=?,payload_json=? WHERE plan_id=? AND project_id=? AND revision=?", (value.revision, value.status, value.effect_state, _json(agent_dispatch_plan_to_payload(value)), value.plan_id, value.project_id, expected_revision)).rowcount
            if changed != 1: raise AgentDispatchStoreConflict("plan revision conflict")
            self._record_operation(connection, operation_id, "plan.transition", _json(agent_dispatch_plan_to_payload(value)), _json(agent_dispatch_plan_to_payload(value)))
            connection.execute("COMMIT"); return value
        except Exception:
            _rollback(connection); raise
        finally: connection.close()

    def supersede_plan(self, *, project_id: str, prior_plan_id: str, expected_revision: int, replacement: AgentDispatchPlan, operation_id: str) -> tuple[AgentDispatchPlan, AgentDispatchPlan]:
        if replacement.project_id != project_id or replacement.revision != 1: raise AgentDispatchStoreInvalid("replacement plan identity is invalid")
        self._validate_plan_dependencies(replacement)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            request = _json({"project_id": project_id, "prior_plan_id": prior_plan_id, "expected_revision": expected_revision, "replacement": agent_dispatch_plan_to_payload(replacement)})
            replay = self._operation(connection, operation_id, "plan.supersede", request)
            if replay is not None:
                result = _load(replay); connection.execute("COMMIT")
                return (agent_dispatch_plan_from_payload(result["prior"]), agent_dispatch_plan_from_payload(result["replacement"]))
            row = connection.execute("SELECT payload_json FROM ai_agent_dispatch_plans WHERE plan_id=? AND project_id=? AND revision=?", (prior_plan_id, project_id, expected_revision)).fetchone()
            if row is None: raise AgentDispatchStoreConflict("plan revision conflict")
            prior = agent_dispatch_plan_from_payload(_load(row[0]))
            if not prior.may_reschedule: raise AgentDispatchStoreInvalid("plan with known or unknown effects cannot be rescheduled")
            if replacement.plan_id == prior.plan_id: raise AgentDispatchStoreInvalid("replacement plan must have a new identity")
            if connection.execute("SELECT 1 FROM ai_agent_dispatch_plans WHERE plan_id=?", (replacement.plan_id,)).fetchone() is not None: raise AgentDispatchStoreConflict("replacement plan already exists")
            superseded = replace(prior, revision=prior.revision + 1, status="superseded")
            connection.execute("UPDATE ai_agent_dispatch_plans SET revision=?,status=?,payload_json=? WHERE plan_id=? AND project_id=? AND revision=?", (superseded.revision, superseded.status, _json(agent_dispatch_plan_to_payload(superseded)), prior.plan_id, project_id, expected_revision))
            connection.execute("INSERT INTO ai_agent_dispatch_plans(plan_id,project_id,revision,status,effect_state,payload_json) VALUES(?,?,?,?,?,?)", (replacement.plan_id, project_id, replacement.revision, replacement.status, replacement.effect_state, _json(agent_dispatch_plan_to_payload(replacement))))
            result = {"prior": agent_dispatch_plan_to_payload(superseded), "replacement": agent_dispatch_plan_to_payload(replacement)}
            self._record_operation(connection, operation_id, "plan.supersede", request, _json(result))
            connection.execute("COMMIT"); return superseded, replacement
        except Exception:
            _rollback(connection); raise
        finally: connection.close()

    def issue_permit(self, value: DispatchPermit) -> tuple[DispatchPermit, bool]:
        if value.status != "issued": raise AgentDispatchStoreInvalid("only an issued permit may be persisted")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            request = _json(dispatch_permit_to_payload(value))
            replay = self._operation(connection, value.operation_id, "permit.issue", request)
            if replay is not None: connection.execute("COMMIT"); return dispatch_permit_from_payload(_load(replay)), False
            plan_row = connection.execute("SELECT payload_json FROM ai_agent_dispatch_plans WHERE plan_id=? AND project_id=? AND revision=?", (value.plan_id, value.project_id, value.plan_revision)).fetchone()
            if plan_row is None: raise AgentDispatchStoreConflict("permit plan revision conflict")
            plan = agent_dispatch_plan_from_payload(_load(plan_row[0]))
            if plan.status not in {"ready", "dispatching"} or plan.effect_state != "none" or value.assignment_id not in plan.assignment_ids: raise AgentDispatchStoreInvalid("permit is not authorized by plan")
            assignment = connection.execute("SELECT project_id FROM ai_agent_dispatch_assignments WHERE assignment_id=?", (value.assignment_id,)).fetchone()
            if assignment is None or assignment[0] != value.project_id: raise AgentDispatchStoreInvalid("permit assignment is outside project scope")
            existing = connection.execute("SELECT payload_json FROM ai_agent_dispatch_permits WHERE permit_id=?", (value.permit_id,)).fetchone()
            if existing is not None:
                stored = dispatch_permit_from_payload(_load(existing[0]))
                if stored != value: raise AgentDispatchStoreConflict("permit identity collision")
                self._record_operation(connection, value.operation_id, "permit.issue", request, _json(dispatch_permit_to_payload(value)))
                connection.execute("COMMIT"); return value, False
            siblings = connection.execute("SELECT payload_json FROM ai_agent_dispatch_permits WHERE plan_id=? AND project_id=? AND assignment_id=?", (value.plan_id, value.project_id, value.assignment_id)).fetchall()
            if any(dispatch_permit_from_payload(_load(row[0])).plan_revision == value.plan_revision for row in siblings):
                raise AgentDispatchStoreConflict("assignment already has a permit for this plan revision")
            connection.execute("INSERT INTO ai_agent_dispatch_permits(permit_id,project_id,plan_id,assignment_id,status,payload_json) VALUES(?,?,?,?,?,?)", (value.permit_id, value.project_id, value.plan_id, value.assignment_id, value.status, request))
            self._record_operation(connection, value.operation_id, "permit.issue", request, request)
            connection.execute("COMMIT"); return value, True
        except Exception:
            _rollback(connection); raise
        finally: connection.close()

    def consume_permit(self, permit_id: str, *, project_id: str, operation_id: str) -> DispatchPermit:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            request = _json({"permit_id": permit_id, "project_id": project_id})
            replay = self._operation(connection, operation_id, "permit.consume", request)
            if replay is not None: connection.execute("COMMIT"); return dispatch_permit_from_payload(_load(replay))
            row = connection.execute("SELECT payload_json FROM ai_agent_dispatch_permits WHERE permit_id=? AND project_id=?", (permit_id, project_id)).fetchone()
            if row is None: raise AgentDispatchStoreNotFound("dispatch permit was not found")
            prior = dispatch_permit_from_payload(_load(row[0]))
            if prior.status != "issued": raise AgentDispatchStoreConflict("dispatch permit is not available")
            consumed = replace(prior, status="consumed")
            changed = connection.execute("UPDATE ai_agent_dispatch_permits SET status=?,payload_json=? WHERE permit_id=? AND project_id=? AND status='issued'", (consumed.status, _json(dispatch_permit_to_payload(consumed)), permit_id, project_id)).rowcount
            if changed != 1: raise AgentDispatchStoreConflict("dispatch permit is not available")
            self._record_operation(connection, operation_id, "permit.consume", request, _json(dispatch_permit_to_payload(consumed)))
            connection.execute("COMMIT"); return consumed
        except Exception:
            _rollback(connection); raise
        finally: connection.close()

    def get_permit(self, permit_id: str, *, project_id: str) -> DispatchPermit | None:
        row = self._read_one("SELECT payload_json FROM ai_agent_dispatch_permits WHERE permit_id=? AND project_id=?", (permit_id, project_id))
        return dispatch_permit_from_payload(_load(row[0])) if row else None

    def get_permit_child_run_binding(self, permit_id: str, *, project_id: str) -> str | None:
        row = self._read_one("SELECT child_run_id FROM ai_agent_dispatch_permit_bindings WHERE permit_id=? AND project_id=?", (permit_id, project_id))
        return str(row[0]) if row else None

    def bind_permit_to_child_run(self, permit_id: str, *, project_id: str, child_run_id: str, operation_id: str) -> tuple[DispatchPermit, str]:
        """Atomically consume a permit only while recording its durable child Run binding.

        This method does not create or inspect child Turns.  The binding is
        recovery metadata: after a crash, a consumed permit can be joined back
        to the exact child Run instead of being treated as lost work.
        """
        _run_id(child_run_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            request = _json({"permit_id": permit_id, "project_id": project_id, "child_run_id": child_run_id})
            replay = self._operation(connection, operation_id, "permit.bind_child_run", request)
            if replay is not None:
                result = _load(replay); connection.execute("COMMIT")
                return dispatch_permit_from_payload(result["permit"]), str(result["child_run_id"])
            permit = self._permit(connection, permit_id, project_id)
            if permit is None: raise AgentDispatchStoreNotFound("dispatch permit was not found")
            existing = connection.execute("SELECT child_run_id FROM ai_agent_dispatch_permit_bindings WHERE permit_id=? AND project_id=?", (permit_id, project_id)).fetchone()
            if existing is not None:
                if str(existing[0]) != child_run_id: raise AgentDispatchStoreConflict("dispatch permit is already bound to another child Run")
                if permit.status != "consumed": raise AgentDispatchStoreInvalid("bound dispatch permit has invalid state")
                result = {"permit": dispatch_permit_to_payload(permit), "child_run_id": child_run_id}
                self._record_operation(connection, operation_id, "permit.bind_child_run", request, _json(result))
                connection.execute("COMMIT")
                return permit, child_run_id
            if permit.status != "issued": raise AgentDispatchStoreConflict("dispatch permit is not available for child binding")
            self._authorized_assignment(connection, permit)
            consumed = replace(permit, status="consumed")
            changed = connection.execute("UPDATE ai_agent_dispatch_permits SET status=?,payload_json=? WHERE permit_id=? AND project_id=? AND status='issued'", (consumed.status, _json(dispatch_permit_to_payload(consumed)), permit_id, project_id)).rowcount
            if changed != 1: raise AgentDispatchStoreConflict("dispatch permit is not available for child binding")
            connection.execute("INSERT INTO ai_agent_dispatch_permit_bindings(permit_id,project_id,child_run_id) VALUES(?,?,?)", (permit_id, project_id, child_run_id))
            result = {"permit": dispatch_permit_to_payload(consumed), "child_run_id": child_run_id}
            self._record_operation(connection, operation_id, "permit.bind_child_run", request, _json(result))
            connection.execute("COMMIT")
            return consumed, child_run_id
        except Exception:
            _rollback(connection); raise
        finally: connection.close()

    def get_assignment(self, assignment_id: str, *, project_id: str) -> ExpertAssignment | None:
        row = self._read_one("SELECT payload_json FROM ai_agent_dispatch_assignments WHERE assignment_id=? AND project_id=?", (assignment_id, project_id))
        return expert_assignment_from_payload(_load(row[0])) if row else None

    def get_assignment_for_permit(self, permit_id: str, *, project_id: str) -> tuple[DispatchPermit, ExpertAssignment] | None:
        connection = self._connect()
        try:
            permit = self._permit(connection, permit_id, project_id)
            if permit is None: return None
            return permit, self._authorized_assignment(connection, permit)
        finally: connection.close()

    def claim_permit_assignment(self, permit_id: str, *, project_id: str, operation_id: str) -> tuple[DispatchPermit, ExpertAssignment]:
        """Atomically consume one issued permit and return its frozen assignment."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            request = _json({"permit_id": permit_id, "project_id": project_id})
            replay = self._operation(connection, operation_id, "permit.claim_assignment", request)
            if replay is not None:
                result = _load(replay); connection.execute("COMMIT")
                return dispatch_permit_from_payload(result["permit"]), expert_assignment_from_payload(result["assignment"])
            prior = self._permit(connection, permit_id, project_id)
            if prior is None: raise AgentDispatchStoreNotFound("dispatch permit was not found")
            if prior.status != "issued": raise AgentDispatchStoreConflict("dispatch permit is not available")
            assignment = self._authorized_assignment(connection, prior)
            consumed = replace(prior, status="consumed")
            changed = connection.execute("UPDATE ai_agent_dispatch_permits SET status=?,payload_json=? WHERE permit_id=? AND project_id=? AND status='issued'", (consumed.status, _json(dispatch_permit_to_payload(consumed)), permit_id, project_id)).rowcount
            if changed != 1: raise AgentDispatchStoreConflict("dispatch permit is not available")
            result = {"permit": dispatch_permit_to_payload(consumed), "assignment": expert_assignment_to_payload(assignment)}
            self._record_operation(connection, operation_id, "permit.claim_assignment", request, _json(result))
            connection.execute("COMMIT")
            return consumed, assignment
        except Exception:
            _rollback(connection); raise
        finally: connection.close()

    def _put_immutable(self, kind: str, table: str, key_column: str, key: str, project_id: str, payload: dict[str, object], decoder: object, value: object, operation_id: str, extra: tuple[object, ...] = ()) -> tuple[object, bool]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            request = _json(payload); replay = self._operation(connection, operation_id, kind, request)
            if replay is not None: connection.execute("COMMIT"); return decoder(_load(replay)), False  # type: ignore[operator]
            existing = connection.execute(f"SELECT payload_json FROM {table} WHERE {key_column}=?", (key,)).fetchone()
            if existing is not None:
                stored = decoder(_load(existing[0]))  # type: ignore[operator]
                if stored != value: raise AgentDispatchStoreConflict(f"{kind} identity collision")
                self._record_operation(connection, operation_id, kind, request, request); connection.execute("COMMIT"); return value, False
            columns = f"{key_column},project_id" + (",revision,status,effect_state" if table == "ai_agent_dispatch_plans" else "") + ",payload_json"
            values = (key, project_id, *extra, request)
            marks = ",".join("?" for _ in values)
            connection.execute(f"INSERT INTO {table}({columns}) VALUES({marks})", values)
            self._record_operation(connection, operation_id, kind, request, request)
            connection.execute("COMMIT"); return value, True
        except Exception:
            _rollback(connection); raise
        finally: connection.close()

    def _validate_plan_dependencies(self, plan: AgentDispatchPlan) -> None:
        connection = self._connect()
        try:
            self._validate_plan_dependencies_in_connection(connection, plan)
        finally: connection.close()

    def _validate_plan_dependencies_in_connection(self, connection: sqlite3.Connection, plan: AgentDispatchPlan) -> None:
        intake_id = parse_canonical_dispatch_ref(plan.intake_receipt_ref, "intake")
        workload_id = parse_canonical_dispatch_ref(plan.workload_snapshot_ref, "workload")
        capacity_id = parse_canonical_dispatch_ref(plan.capacity_snapshot_ref, "capacity")
        intake = self._dependency(connection, "ai_agent_dispatch_intakes", "receipt_id", intake_id, plan.project_id, intake_routing_receipt_from_payload)
        workload = self._dependency(connection, "ai_agent_dispatch_workloads", "snapshot_id", workload_id, plan.project_id, workload_snapshot_from_payload)
        capacity = self._dependency(connection, "ai_agent_dispatch_capacities", "snapshot_id", capacity_id, plan.project_id, capacity_snapshot_from_payload)
        if intake.revision != plan.intake_revision or workload.revision != plan.workload_revision or capacity.revision != plan.capacity_revision:
            raise AgentDispatchStoreInvalid("plan authority revision does not match persisted snapshot")
        if plan.max_concurrent_assignments > capacity.available_slots:
            raise AgentDispatchStoreInvalid("plan concurrency exceeds capacity")
        if plan.mode == "main_only":
            return
        assignments = connection.execute("SELECT assignment_id,project_id FROM ai_agent_dispatch_assignments WHERE assignment_id IN (%s)" % ",".join("?" * len(plan.assignment_ids)), plan.assignment_ids).fetchall()
        cluster_id = parse_canonical_dispatch_ref(plan.expert_cluster_ref, "cluster")
        cluster = self._dependency(connection, "ai_agent_dispatch_clusters", "cluster_id", cluster_id, plan.project_id, expert_cluster_from_payload)
        if cluster.revision != plan.expert_cluster_revision:
            raise AgentDispatchStoreInvalid("plan cluster revision does not match persisted snapshot")
        if len(assignments) != len(plan.assignment_ids) or any(row[1] != plan.project_id for row in assignments): raise AgentDispatchStoreInvalid("plan assignment scope is invalid")
        assignment_values = [expert_assignment_from_payload(_load(row[0])) for row in connection.execute("SELECT payload_json FROM ai_agent_dispatch_assignments WHERE assignment_id IN (%s)" % ",".join("?" * len(plan.assignment_ids)), plan.assignment_ids)]
        if any(item.cluster_id != cluster.cluster_id or item.cluster_revision != cluster.revision for item in assignment_values): raise AgentDispatchStoreInvalid("plan assignment cluster is invalid")
        total = _sum_budget(item.delegated_budget for item in assignment_values)
        if not total.is_subset_of(plan.budget_limit) or not total.is_subset_of(capacity.remaining_budget): raise AgentDispatchStoreInvalid("plan assignment budget exceeds its limit")

    @staticmethod
    def _insert_atomic_immutable(connection: sqlite3.Connection, table: str, key_column: str, key: str, project_id: str, payload: dict[str, object], decoder: object, value: object) -> None:
        existing = connection.execute(f"SELECT payload_json FROM {table} WHERE {key_column}=?", (key,)).fetchone()
        if existing is not None:
            if decoder(_load(existing[0])) != value: raise AgentDispatchStoreConflict("published dispatch identity collision")  # type: ignore[operator]
            return
        connection.execute(f"INSERT INTO {table}({key_column},project_id,payload_json) VALUES(?,?,?)", (key, project_id, _json(payload)))

    @staticmethod
    def _validate_publish_permits(connection: sqlite3.Connection, ready: AgentDispatchPlan, permits: tuple[DispatchPermit, ...]) -> None:
        if len({item.permit_id for item in permits}) != len(permits) or {item.assignment_id for item in permits} != set(ready.assignment_ids):
            raise AgentDispatchStoreInvalid("published permits do not cover assignments exactly once")
        for permit in permits:
            if permit.status != "issued" or permit.project_id != ready.project_id or permit.plan_id != ready.plan_id or permit.plan_revision != ready.revision:
                raise AgentDispatchStoreInvalid("published permit is not bound to ready plan")
            sibling = connection.execute("SELECT 1 FROM ai_agent_dispatch_permits WHERE plan_id=? AND project_id=? AND assignment_id=?", (permit.plan_id, permit.project_id, permit.assignment_id)).fetchone()
            if sibling is not None: raise AgentDispatchStoreConflict("assignment already has a permit for this plan")

    @staticmethod
    def _dependency(connection: sqlite3.Connection, table: str, column: str, identity: str, project_id: str, decoder: object) -> object:
        row = connection.execute(f"SELECT payload_json FROM {table} WHERE {column}=? AND project_id=?", (identity, project_id)).fetchone()
        if row is None: raise AgentDispatchStoreInvalid("plan authority reference is dangling or outside project scope")
        return decoder(_load(row[0]))  # type: ignore[operator]

    @staticmethod
    def _permit(connection: sqlite3.Connection, permit_id: str, project_id: str) -> DispatchPermit | None:
        row = connection.execute("SELECT payload_json FROM ai_agent_dispatch_permits WHERE permit_id=? AND project_id=?", (permit_id, project_id)).fetchone()
        return dispatch_permit_from_payload(_load(row[0])) if row else None

    @staticmethod
    def _authorized_assignment(connection: sqlite3.Connection, permit: DispatchPermit) -> ExpertAssignment:
        plan_row = connection.execute(
            "SELECT payload_json FROM ai_agent_dispatch_plans WHERE plan_id=? AND project_id=?",
            (permit.plan_id, permit.project_id),
        ).fetchone()
        if plan_row is None: raise AgentDispatchStoreConflict("permit plan revision conflict")
        plan = agent_dispatch_plan_from_payload(_load(plan_row[0]))
        published_revision = (
            plan.revision - 1 if plan.status == "dispatching" else plan.revision
        )
        if published_revision != permit.plan_revision:
            raise AgentDispatchStoreConflict("permit plan revision conflict")
        if plan.status not in {"ready", "dispatching"} or plan.effect_state != "none" or permit.assignment_id not in plan.assignment_ids:
            raise AgentDispatchStoreInvalid("permit is not authorized by plan")
        row = connection.execute("SELECT payload_json FROM ai_agent_dispatch_assignments WHERE assignment_id=? AND project_id=?", (permit.assignment_id, permit.project_id)).fetchone()
        if row is None: raise AgentDispatchStoreInvalid("permit assignment is outside project scope")
        return expert_assignment_from_payload(_load(row[0]))

    def _operation(self, connection: sqlite3.Connection, operation_id: str, kind: str, request: str) -> str | None:
        row = connection.execute("SELECT kind,request_json,result_json FROM ai_agent_dispatch_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None: return None
        if row[0] != kind or row[1] != request: raise AgentDispatchStoreConflict("operation id was reused with a different request")
        return str(row[2])

    @staticmethod
    def _record_operation(connection: sqlite3.Connection, operation_id: str, kind: str, request: str, result: str) -> None:
        connection.execute("INSERT INTO ai_agent_dispatch_operations(operation_id,kind,request_json,result_json) VALUES(?,?,?,?)", (operation_id, kind, request, result))

    def _read_one(self, query: str, params: tuple[object, ...]) -> sqlite3.Row | tuple[object, ...] | None:
        connection = self._connect()
        try: return connection.execute(query, params).fetchone()
        finally: connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        observe_connection(connection)
        connection.execute("PRAGMA foreign_keys=ON"); connection.execute("PRAGMA journal_mode=WAL"); connection.execute("PRAGMA synchronous=FULL"); connection.execute("PRAGMA busy_timeout=5000")
        return connection


_PLAN_TRANSITIONS = {"draft": frozenset({"ready", "cancelled"}), "ready": frozenset({"dispatching", "cancelled", "failed"}), "dispatching": frozenset({"dispatched", "failed", "cancelled"}), "dispatched": frozenset({"completed", "failed"}), "failed": frozenset({"cancelled"}), "cancelled": frozenset(), "completed": frozenset(), "superseded": frozenset()}


def _same_plan_snapshot(prior: AgentDispatchPlan, next_value: AgentDispatchPlan) -> bool:
    old = agent_dispatch_plan_to_payload(prior); new = agent_dispatch_plan_to_payload(next_value)
    for mutable in ("schema_version", "revision", "status", "effect_state"): old.pop(mutable); new.pop(mutable)
    return old == new


def _initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
      CREATE TABLE IF NOT EXISTS ai_agent_dispatch_intakes(receipt_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, payload_json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS ai_agent_dispatch_workloads(snapshot_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, payload_json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS ai_agent_dispatch_capacities(snapshot_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, payload_json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS ai_agent_dispatch_clusters(cluster_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, payload_json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS ai_agent_dispatch_assignments(assignment_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, payload_json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS ai_agent_dispatch_plans(plan_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1), status TEXT NOT NULL, effect_state TEXT NOT NULL, payload_json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS ai_agent_dispatch_permits(permit_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, plan_id TEXT NOT NULL, assignment_id TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS ai_agent_dispatch_permit_bindings(permit_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, child_run_id TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS ai_agent_dispatch_operations(operation_id TEXT PRIMARY KEY, kind TEXT NOT NULL, request_json TEXT NOT NULL, result_json TEXT NOT NULL);
    """)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool: return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None
def _json(value: object) -> str: return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
def _load(value: object) -> object: return json.loads(str(value))
def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction: connection.execute("ROLLBACK")


def _run_id(value: object) -> None:
    if not isinstance(value, str) or not _RUN_ID.fullmatch(value):
        raise AgentDispatchStoreInvalid("Run identity is invalid")


def _sum_budget(values: object) -> object:
    from .agent_contracts import AgentBudget
    result = AgentBudget(0, 0, 0, 0, 0)
    for value in values: result = result.plus(value)
    return result
