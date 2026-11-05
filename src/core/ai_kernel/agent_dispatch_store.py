"""SQLite persistence for dispatch planning, separate from Turn execution.

The store shares the AI Turn database solely for lifecycle colocation.  It
requires the existing ``ai_turns`` table but never creates, claims, or mutates
Turns, runners, effects, providers, or model configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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


@dataclass(frozen=True)
class EffectiveDispatchSnapshot:
    plan: AgentDispatchPlan
    revision: int
    assignment_ids: tuple[str, ...]
    permit_ids: tuple[str, ...]
    steer_input_refs: tuple[str, ...]
    generation: int = 0
    phase: str | None = None


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

    def effective_snapshot(self, plan_id: str, *, project_id: str) -> EffectiveDispatchSnapshot:
        connection = self._connect()
        try:
            connection.execute('BEGIN')
            return self._effective_snapshot(connection,plan_id,project_id)
        finally:
            connection.close()

    def _effective_snapshot(self, connection, plan_id, project_id):
        row = connection.execute('SELECT payload_json FROM ai_agent_dispatch_plans WHERE plan_id=? AND project_id=?',
            (plan_id,project_id)).fetchone()
        if row is None:
            raise AgentDispatchStoreNotFound('dispatch plan was not found')
        plan = agent_dispatch_plan_from_payload(_load(row[0]))
        head = connection.execute('SELECT revision,payload_json FROM ai_agent_dispatch_effective WHERE plan_id=? AND project_id=?',
            (plan_id,project_id)).fetchone()
        if head is None:
            permits = connection.execute('SELECT permit_id FROM ai_agent_dispatch_permits WHERE plan_id=? AND project_id=? ORDER BY permit_id',
                (plan_id,project_id)).fetchall()
            return EffectiveDispatchSnapshot(plan,0,plan.assignment_ids,tuple(row[0] for row in permits),(),0,plan.status)
        data = _load(head[1])
        return EffectiveDispatchSnapshot(plan,head[0],tuple(data['assignment_ids']),tuple(data['permit_ids']),
            tuple(data['steer_input_refs']),data['generation'],data['phase'] if data['steer_input_refs'] else plan.status)

    def transition_effective(self, snapshot, *, phase):
        if not snapshot.steer_input_refs or phase not in _PLAN_TRANSITIONS.get(snapshot.phase,set()):
            raise AgentDispatchStoreInvalid('effective dispatch phase transition is invalid')
        connection = self._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            current = self._effective_snapshot(connection,snapshot.plan.plan_id,snapshot.plan.project_id)
            if current != snapshot:
                raise AgentDispatchStoreConflict('effective dispatch changed')
            result = {'revision':snapshot.revision+1,'assignment_ids':list(snapshot.assignment_ids),
                'permit_ids':list(snapshot.permit_ids),'steer_input_refs':list(snapshot.steer_input_refs),
                'generation':snapshot.generation,'phase':phase}
            self._write_effective(connection,snapshot,result)
            connection.execute('COMMIT')
            return replace(snapshot,revision=result['revision'],phase=phase)
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def amend_unbound(self, *, project_id, plan_id, main_run_id, expected_plan_revision,
                      expected_effective_revision, expected_main_revision, expected_cancel_epoch,
                      replace_permit_ids, new_assignments, new_permits, steer_input_ref,
                      operation_id, agent_store):
        if agent_store._path != self._path:
            raise AgentDispatchStoreInvalid('dispatch amendment owner database differs')
        request = _json({'project_id':project_id,'plan_id':plan_id,'main_run_id':main_run_id,
            'plan_revision':expected_plan_revision,'effective_revision':expected_effective_revision,
            'main_revision':expected_main_revision,'cancel_epoch':expected_cancel_epoch,
            'replace_permit_ids':list(replace_permit_ids),'steer_input_ref':steer_input_ref,
            'assignments':[expert_assignment_to_payload(item) for item in new_assignments],
            'permits':[dispatch_permit_to_payload(item) for item in new_permits]})
        connection = self._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            replay = self._operation(connection,operation_id,'plan.amend_unbound',request)
            if replay is not None:
                result = _load(replay)
                connection.execute('COMMIT')
                return EffectiveDispatchSnapshot(agent_dispatch_plan_from_payload(result['plan']),
                    result['revision'],tuple(result['assignment_ids']),tuple(result['permit_ids']),
                    tuple(result['steer_input_refs']),result['generation'],result['phase'])
            old = self._effective_snapshot(connection,plan_id,project_id)
            plan = old.plan
            main,revision = agent_store._run_row(connection,main_run_id)
            if (plan.main_run_id != main_run_id or main.project_id != project_id or main.is_terminal
                    or main.status in {'cancelling','quarantined'} or main.cancel_epoch != expected_cancel_epoch
                    or revision != expected_main_revision or plan.revision != expected_plan_revision
                    or old.revision != expected_effective_revision
                    or plan.status not in {'ready','dispatching','dispatched','completed'}):
                raise AgentDispatchStoreConflict('dispatch amendment authority changed')
            agent_store._assert_profile_snapshot(connection,main)
            if plan.max_concurrent_assignments > main.max_concurrent_children:
                raise AgentDispatchStoreInvalid('amendment exceeds main concurrency')
            self._steer_source(connection,main.turn_id,project_id,steer_input_ref)
            if steer_input_ref in old.steer_input_refs:
                raise AgentDispatchStoreConflict('steer input already amended this plan')
            replaced = tuple(replace_permit_ids)
            if len(replaced) != len(set(replaced)) or not set(replaced).issubset(old.permit_ids):
                raise AgentDispatchStoreInvalid('amendment withdrawal is outside effective plan')
            for identity in replaced:
                permit = self._permit(connection,identity,project_id)
                bound = connection.execute('SELECT 1 FROM ai_agent_dispatch_permit_bindings WHERE permit_id=?',(identity,)).fetchone()
                if permit is None or permit.status != 'issued' or bound is not None:
                    raise AgentDispatchStoreConflict('dispatched assignment cannot be amended')
            kept = tuple(identity for identity in old.permit_ids if identity not in replaced)
            kept_assignments = tuple(self._permit(connection,identity,project_id).assignment_id for identity in kept)
            if (len(new_assignments) != len(new_permits)
                    or len({item.assignment_id for item in new_assignments}) != len(new_assignments)
                    or len({item.permit_id for item in new_permits}) != len(new_permits)
                    or {item.assignment_id for item in new_permits} != {item.assignment_id for item in new_assignments}
                    or set(kept_assignments) & {item.assignment_id for item in new_assignments}):
                raise AgentDispatchStoreInvalid('amendment assignments and permits differ')
            allowed = [expert_assignment_from_payload(_load(row[0])) for row in connection.execute(
                'SELECT payload_json FROM ai_agent_dispatch_assignments WHERE assignment_id IN (%s)' % ','.join('?' * len(plan.assignment_ids)),
                plan.assignment_ids)]
            for item in new_assignments:
                if connection.execute('SELECT 1 FROM ai_agent_dispatch_assignments WHERE assignment_id=?',(item.assignment_id,)).fetchone():
                    raise AgentDispatchStoreConflict('amendment assignment identity already exists')
                original = next((value for value in allowed if value.profile_ref == item.profile_ref
                    and value.expert_id == item.expert_id and value.skill_ids == item.skill_ids),None)
                if original is None or any(getattr(original,key) != getattr(item,key) for key in
                        ('project_id','cluster_id','cluster_revision','profile_revision','context_policy_ref',
                         'context_policy_revision','expert_snapshot_ref','expert_revision','skill_snapshot_ref','skill_revision')):
                    raise AgentDispatchStoreInvalid('amendment changed frozen assignment authority')
                if not set(item.capability_ids).issubset(original.capability_ids):
                    raise AgentDispatchStoreInvalid('amendment expanded capabilities')
                prefix = 'crp://agent/profiles/'
                if not item.profile_ref.startswith(prefix):
                    raise AgentDispatchStoreInvalid('amendment profile reference is invalid')
                profile = agent_store.profile_in_transaction(connection,item.profile_ref[len(prefix):])
                if (not profile.enabled or profile.role != 'subagent' or profile.revision != item.profile_revision
                        or not item.delegated_budget.is_subset_of(profile.budget_limit)
                        or not set(item.capability_ids).issubset(profile.capability_ids)):
                    raise AgentDispatchStoreConflict('amendment current profile changed')
                self._assignment_task(connection,main.turn_id,item)
            for permit in new_permits:
                if (permit.project_id != project_id or permit.plan_id != plan_id or permit.status != 'issued'
                        or permit.plan_revision != expected_plan_revision):
                    raise AgentDispatchStoreInvalid('amendment permit authority differs')
                if connection.execute('SELECT 1 FROM ai_agent_dispatch_permits WHERE permit_id=?',(permit.permit_id,)).fetchone():
                    raise AgentDispatchStoreConflict('amendment permit identity already exists')
            effective_ids = (*kept_assignments,*(item.assignment_id for item in new_assignments))
            assignments = [self._assignment(connection,identity,project_id) for identity in kept_assignments] + list(new_assignments)
            total = _sum_budget(item.delegated_budget for item in assignments)
            if not total.is_subset_of(plan.budget_limit):
                raise AgentDispatchStoreInvalid('amendment exceeds frozen plan budget')
            pending = [self._assignment(connection,self._permit(connection,identity,project_id).assignment_id,project_id)
                for identity in kept if not connection.execute('SELECT 1 FROM ai_agent_dispatch_permit_bindings binding '
                    'JOIN ai_agent_budget_reservations reservation ON reservation.child_run_id=binding.child_run_id '
                    'WHERE binding.permit_id=? AND reservation.parent_run_id=?',(identity,main.run_id)).fetchone()] + list(new_assignments)
            agent_store._assert_budget_available(connection,main,_sum_budget(item.delegated_budget for item in pending))
            self._amendment_dependencies(connection,main.turn_id,assignments,set(effective_ids))
            for item in new_assignments:
                self._insert_atomic_immutable(connection,'ai_agent_dispatch_assignments','assignment_id',
                    item.assignment_id,project_id,expert_assignment_to_payload(item),expert_assignment_from_payload,item)
            for permit in new_permits:
                connection.execute('INSERT INTO ai_agent_dispatch_permits(permit_id,project_id,plan_id,assignment_id,status,payload_json) VALUES(?,?,?,?,?,?)',
                    (permit.permit_id,project_id,plan_id,permit.assignment_id,permit.status,_json(dispatch_permit_to_payload(permit))))
                connection.execute('INSERT INTO ai_agent_dispatch_admissions(permit_id,plan_id,operation_id) VALUES(?,?,?)',
                    (permit.permit_id,plan_id,operation_id))
            for identity in replaced:
                connection.execute('INSERT INTO ai_agent_dispatch_withdrawals(permit_id,plan_id,operation_id) VALUES(?,?,?)',
                    (identity,plan_id,operation_id))
            result = {'plan':agent_dispatch_plan_to_payload(plan),'revision':old.revision+1,
                'assignment_ids':list(effective_ids),'permit_ids':[*kept,*(item.permit_id for item in new_permits)],
                'steer_input_refs':[*old.steer_input_refs,steer_input_ref],
                'generation':old.revision+1,'phase':'ready'}
            self._write_effective(connection,old,result)
            self._record_operation(connection,operation_id,'plan.amend_unbound',request,_json(result))
            connection.execute('COMMIT')
            return EffectiveDispatchSnapshot(plan,result['revision'],tuple(result['assignment_ids']),
                tuple(result['permit_ids']),tuple(result['steer_input_refs']),result['generation'],result['phase'])
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

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
        """读取原待执行计划和已提交的有效修订，保留历史计划的终态。"""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 256:
            raise ValueError("dispatch recovery plan limit must be between 1 and 256")
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT plan.payload_json FROM ai_agent_dispatch_plans plan "
                "LEFT JOIN ai_agent_dispatch_effective effective ON effective.plan_id=plan.plan_id "
                "WHERE plan.status IN ('ready','dispatching','dispatched') "
                "OR (json_array_length(json_extract(effective.payload_json,'$.steer_input_refs'))>0 "
                "AND json_extract(effective.payload_json,'$.phase') IN ('ready','dispatching','dispatched')) "
                "ORDER BY plan.project_id,plan.plan_id LIMIT ?",
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
            self._authorized_assignment(connection,prior)
            consumed = replace(prior, status="consumed")
            changed = connection.execute("UPDATE ai_agent_dispatch_permits SET status=?,payload_json=? WHERE permit_id=? AND project_id=? AND status='issued'", (consumed.status, _json(dispatch_permit_to_payload(consumed)), permit_id, project_id)).rowcount
            if changed != 1: raise AgentDispatchStoreConflict("dispatch permit is not available")
            self._touch_effective(connection,prior.plan_id,project_id)
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

    def binding_snapshot(self, permit_id: str, *, project_id: str):
        row = self._read_one('SELECT scope.payload_json FROM ai_agent_dispatch_binding_scopes scope '
            'JOIN ai_agent_dispatch_permit_bindings binding ON binding.permit_id=scope.permit_id '
            'WHERE scope.permit_id=? AND binding.project_id=?',(permit_id,project_id))
        return _load(row[0]) if row is not None else None

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
            snapshot = self._effective_snapshot(connection,permit.plan_id,project_id)
            if snapshot.steer_input_refs:
                main_row = connection.execute('SELECT turn_id FROM ai_agent_runs WHERE run_id=? AND project_id=?',
                    (snapshot.plan.main_run_id,project_id)).fetchone()
                assignment = self._assignment(connection,permit.assignment_id,project_id)
                task = self._assignment_task(connection,main_row[0],assignment)
                connection.execute('INSERT INTO ai_agent_dispatch_binding_scopes(permit_id,payload_json) VALUES(?,?)',
                    (permit_id,_json({'child_run_id':child_run_id,'assignment_ids':list(snapshot.assignment_ids),
                        'task_payload_ref':assignment.task_payload_ref,'depends_on':task.get('division',{}).get('depends_on',[]),
                        'effective_generation':snapshot.generation})))
            self._touch_effective(connection,permit.plan_id,project_id)
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
            self._touch_effective(connection,prior.plan_id,project_id)
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
    def _assignment(connection, identity, project):
        row = connection.execute('SELECT payload_json FROM ai_agent_dispatch_assignments WHERE assignment_id=? AND project_id=?',
            (identity,project)).fetchone()
        if row is None:
            raise AgentDispatchStoreInvalid('effective assignment is unavailable')
        return expert_assignment_from_payload(_load(row[0]))

    @staticmethod
    def _assignment_task(connection, turn_id, assignment):
        row = connection.execute('SELECT payload_json FROM ai_turn_immutable_payloads WHERE payload_ref=? AND turn_id=? AND kind=?',
            (assignment.task_payload_ref,turn_id,f'agent.dispatch.task.{assignment.assignment_id}.v1')).fetchone()
        if row is None:
            raise AgentDispatchStoreInvalid('amendment task does not belong to main Turn')
        value = _load(row[0])
        if (not isinstance(value,dict) or value.get('assignment_id') != assignment.assignment_id
                or value.get('schema_version') != '1.0.0' or not isinstance(value.get('task'),str)
                or not value['task'].strip() or len(value['task']) > 16000):
            raise AgentDispatchStoreInvalid('amendment task input is invalid')
        return value

    @staticmethod
    def _steer_source(connection, turn_id, project, reference):
        row = connection.execute('SELECT payload_json FROM ai_turn_immutable_payloads WHERE payload_ref=? AND turn_id=?',
            (reference,turn_id)).fetchone()
        if row is None:
            raise AgentDispatchStoreInvalid('amendment steer input is unavailable')
        value = _load(row[0])
        if not isinstance(value,dict) or value.get('turn_id') != turn_id or value.get('project_id') != project:
            raise AgentDispatchStoreInvalid('amendment steer input scope differs')
        events = connection.execute('SELECT event_json FROM ai_turn_events WHERE turn_id=?',(turn_id,)).fetchall()
        if not any((event := _load(row[0])).get('type') in {'turn.steer.received','turn.steer.applied'}
                and reference in event.get('data',{}).get('evidence_refs',[]) for row in events):
            raise AgentDispatchStoreInvalid('amendment steer input has no accepted event')

    @classmethod
    def _amendment_dependencies(cls, connection, turn_id, assignments, identities):
        graph = {}
        for item in assignments:
            value = cls._assignment_task(connection,turn_id,item)
            division = value.get('division')
            dependencies = division.get('depends_on',[]) if isinstance(division,dict) else []
            if (not isinstance(dependencies,list) or any(not isinstance(identity,str) for identity in dependencies)
                    or not set(dependencies).issubset(identities) or item.assignment_id in dependencies):
                raise AgentDispatchStoreInvalid('amendment dependency is outside effective plan')
            graph[item.assignment_id] = dependencies
        visiting,finished = set(),set()
        def visit(identity):
            if identity in visiting:
                raise AgentDispatchStoreInvalid('amendment dependency cycle')
            if identity in finished:
                return
            visiting.add(identity)
            for dependency in graph[identity]:
                visit(dependency)
            visiting.remove(identity)
            finished.add(identity)
        for identity in graph:
            visit(identity)

    @staticmethod
    def _write_effective(connection, old, result):
        payload = {key:result[key] for key in ('assignment_ids','permit_ids','steer_input_refs','generation','phase')}
        if old.revision == 0:
            connection.execute('INSERT INTO ai_agent_dispatch_effective(plan_id,project_id,revision,payload_json) VALUES(?,?,?,?)',
                (old.plan.plan_id,old.plan.project_id,result['revision'],_json(payload)))
        else:
            changed = connection.execute('UPDATE ai_agent_dispatch_effective SET revision=?,payload_json=? '
                'WHERE plan_id=? AND project_id=? AND revision=?',
                (result['revision'],_json(payload),old.plan.plan_id,old.plan.project_id,old.revision)).rowcount
            if changed != 1:
                raise AgentDispatchStoreConflict('effective dispatch revision changed')

    def _touch_effective(self, connection, plan_id, project_id):
        old = self._effective_snapshot(connection,plan_id,project_id)
        self._write_effective(connection,old,{'revision':old.revision+1,
            'assignment_ids':list(old.assignment_ids),'permit_ids':list(old.permit_ids),
            'steer_input_refs':list(old.steer_input_refs),'generation':old.generation,'phase':old.phase})

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
        head = connection.execute('SELECT payload_json FROM ai_agent_dispatch_effective WHERE plan_id=? AND project_id=?',
            (permit.plan_id,permit.project_id)).fetchone()
        effective = _load(head[0]) if head is not None else None
        if connection.execute('SELECT 1 FROM ai_agent_dispatch_withdrawals WHERE permit_id=?',(permit.permit_id,)).fetchone():
            raise AgentDispatchStoreConflict('dispatch permit was withdrawn')
        admitted = False
        if effective is not None:
            if permit.permit_id not in effective['permit_ids'] or permit.assignment_id not in effective['assignment_ids']:
                raise AgentDispatchStoreConflict('dispatch permit is outside effective plan')
            if permit.assignment_id not in plan.assignment_ids:
                operations = connection.execute("SELECT op.request_json FROM ai_agent_dispatch_admissions admission "
                    "JOIN ai_agent_dispatch_operations op ON op.operation_id=admission.operation_id "
                    "WHERE admission.permit_id=? AND admission.plan_id=? AND op.kind='plan.amend_unbound'",
                    (permit.permit_id,permit.plan_id)).fetchall()
                admitted = any(dispatch_permit_to_payload(replace(permit,status='issued')) == item
                    for row in operations for item in _load(row[0]).get('permits',[]))
                if not admitted:
                    raise AgentDispatchStoreInvalid('dispatch permit has no amendment authority')
            if effective['steer_input_refs']:
                if effective['phase'] not in {'ready','dispatching'}:
                    raise AgentDispatchStoreConflict('effective dispatch is not accepting children')
                main_row = connection.execute('SELECT payload_json FROM ai_agent_runs WHERE run_id=? AND project_id=?',
                    (plan.main_run_id,plan.project_id)).fetchone()
                if main_row is None or _load(main_row[0])['status'] in {'completed','failed','cancelled','timed_out','quarantined','cancelling'}:
                    raise AgentDispatchStoreConflict('effective dispatch main is not running')
        if not admitted and published_revision != permit.plan_revision:
            raise AgentDispatchStoreConflict("permit plan revision conflict")
        if (not admitted and (plan.status not in {"ready", "dispatching"} or plan.effect_state != "none"
                or permit.assignment_id not in plan.assignment_ids)):
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
      CREATE TABLE IF NOT EXISTS ai_agent_dispatch_effective(plan_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1), payload_json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS ai_agent_dispatch_withdrawals(permit_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, operation_id TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS ai_agent_dispatch_admissions(permit_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, operation_id TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS ai_agent_dispatch_binding_scopes(permit_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
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
