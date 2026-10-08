"""Durable coordination primitives for governed internal agent runs.

This store deliberately does not create or mutate ``ai_turns``.  The existing
AI Turn store remains the sole Turn authority; this module only records the
parent/child topology and the coordinator decisions around it.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from core.storage_provider.observability import observe_connection
from core.storage_provider.connection_scope import reusable_connection

from .agent_contracts import (
    AgentBudget,
    AgentBudgetReservation,
    AgentChildLink,
    AgentContractError,
    AgentFanIn,
    AgentFanInResult,
    AgentMessage,
    AgentProfile,
    AgentRun,
    agent_budget_reservation_from_payload,
    agent_budget_reservation_to_payload,
    agent_child_link_from_payload,
    agent_child_link_to_payload,
    agent_fan_in_from_payload,
    agent_fan_in_result_from_payload,
    agent_fan_in_result_to_payload,
    agent_fan_in_to_payload,
    agent_message_from_payload,
    agent_message_to_payload,
    agent_profile_from_payload,
    agent_profile_to_payload,
    agent_run_from_payload,
    agent_run_to_payload,
    validate_child_delegation,
    validate_fan_in_result,
)
from .agent_profiles import builtin_agent_profiles


class AgentStoreError(RuntimeError):
    """Base class for durable internal-agent coordination failures."""


class AgentStoreNotFound(AgentStoreError):
    pass


class AgentStoreConflict(AgentStoreError):
    pass


class AgentStoreLimitExceeded(AgentStoreError):
    pass


class AgentStoreStaleEpoch(AgentStoreError):
    pass


class AgentStoreInvalidTransition(AgentStoreError):
    pass


_TERMINAL = frozenset({"completed", "failed", "cancelled", "timed_out"})
_ZERO = AgentBudget(0, 0, 0, 0, 0)
_RUN_TRANSITIONS = {
    "created": frozenset({"queued", "starting", "cancelled", "failed"}),
    "queued": frozenset({"starting", "cancelled", "failed"}),
    "starting": frozenset({"running", "waiting", "waiting_approval", "cancelling", "failed", "timed_out"}),
    "running": frozenset({"waiting", "waiting_approval", "cancelling", "completed", "failed", "cancelled", "timed_out", "recovery_required", "quarantined"}),
    "waiting": frozenset({"running", "cancelling", "cancelled", "failed", "timed_out", "recovery_required"}),
    "waiting_approval": frozenset({"running", "cancelling", "cancelled", "failed", "timed_out"}),
    "cancelling": frozenset({"cancelled", "failed", "timed_out", "quarantined"}),
    "recovery_required": frozenset({"starting", "quarantined", "failed", "cancelled"}),
    "quarantined": frozenset({"failed", "cancelled"}),
}


class SQLiteAgentStore:
    """A separate coordination schema sharing the existing AI Turn database."""

    def __init__(self, database_path: Path) -> None:
        self._path = database_path.expanduser().resolve(strict=False)
        if not self._path.exists():
            raise AgentStoreNotFound("AI Turn database does not exist")
        connection = self._connect()
        try:
            if not _table_exists(connection, "ai_turns"):
                raise AgentStoreNotFound("AI Turn authority table is required")
            _initialize_schema(connection)
        finally:
            connection.close()

    def put_profile(self, profile: AgentProfile, *, expected_revision: int | None = None) -> AgentProfile:
        payload = _json(agent_profile_to_payload(profile))
        return self._write_profile(profile, payload, expected_revision)

    # AgentProfileStorePort compatibility.  The registry owns policy on which
    # profile identities/revisions are allowed; this adapter supplies CAS.
    def get(self, profile_id: str) -> AgentProfile | None:
        return self.get_profile(profile_id)

    def list(self) -> tuple[AgentProfile, ...]:
        connection = self._connect()
        try:
            return tuple(_profile(_load(row[0])) for row in connection.execute("SELECT payload_json FROM ai_agent_profiles ORDER BY profile_id"))
        finally:
            connection.close()

    def create(self, profile: AgentProfile) -> None:
        self._write_profile(profile, _json(agent_profile_to_payload(profile)), None)

    def replace(self, profile: AgentProfile, *, expected_revision: int) -> None:
        self._write_profile(profile, _json(agent_profile_to_payload(profile)), expected_revision)

    def delete(self, profile_id: str, *, expected_revision: int) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute("DELETE FROM ai_agent_profiles WHERE profile_id=? AND revision=?", (profile_id, expected_revision)).rowcount
            if changed != 1:
                raise AgentStoreConflict("agent profile revision conflict")
            connection.execute("COMMIT")
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def get_profile(self, profile_id: str) -> AgentProfile | None:
        row = self._read_one("SELECT payload_json FROM ai_agent_profiles WHERE profile_id=?", (profile_id,))
        return agent_profile_from_payload(_load(row[0])) if row else None

    def register_run(self, run: AgentRun, *, operation_id: str) -> AgentRun:
        """Record a main run only after the Turn authority accepted its Turn."""
        if run.role != "main":
            raise AgentStoreInvalidTransition("child runs must be reserved through spawn")
        self._validate_run_profile(run)
        return self._write_run(run, operation_id=operation_id, expected_revision=None, require_turn=True)

    def get_run(self, run_id: str) -> AgentRun | None:
        row = self._read_one("SELECT payload_json FROM ai_agent_runs WHERE run_id=?", (run_id,))
        return agent_run_from_payload(_load(row[0])) if row else None

    def get_run_with_revision(self, run_id: str, *, project_id: str) -> tuple[AgentRun, int] | None:
        row = self._read_one("SELECT revision,payload_json FROM ai_agent_runs WHERE run_id=? AND project_id=?", (run_id, project_id))
        return (_run(_load(row[1])), int(row[0])) if row else None

    def get_run_by_turn_id(self, turn_id: str, *, project_id: str) -> tuple[AgentRun, int] | None:
        """Resolve a governed Agent run from a trusted Turn/project boundary.

        Native Agent capabilities receive the current ``turn_id`` from the
        existing Tool provider request.  Requiring the project alongside it
        prevents that runtime identity from becoming an unscoped cross-project
        lookup authority.
        """
        row = self._read_one(
            "SELECT revision,payload_json FROM ai_agent_runs WHERE turn_id=? AND project_id=?",
            (turn_id, project_id),
        )
        return (_run(_load(row[1])), int(row[0])) if row else None

    def list_runs(self, *, project_id: str, parent_run_id: str | None = None) -> tuple[AgentRun, ...]:
        where, params = ("project_id=?", (project_id,)) if parent_run_id is None else ("project_id=? AND parent_run_id=?", (project_id, parent_run_id))
        return self._list_payloads(f"SELECT payload_json FROM ai_agent_runs WHERE {where} ORDER BY run_id", params, _run)

    def list_recovery_candidates(self, *, limit: int = 64) -> tuple[AgentRun, ...]:
        """Return bounded, stable child topology candidates without touching Turns."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 256:
            raise ValueError("agent recovery candidate limit must be between 1 and 256")
        return self._list_payloads(
            "SELECT payload_json FROM ai_agent_runs WHERE parent_run_id IS NOT NULL AND json_extract(payload_json, '$.status') NOT IN ('completed','failed','cancelled','timed_out') ORDER BY project_id,run_id LIMIT ?",
            (limit,), _run,
        )

    def list_organization_start_pairs(
        self, *, limit: int = 64,
    ) -> tuple[tuple[AgentRun, AgentRun], ...]:
        """Locate bounded non-terminal main → steward start pairs.

        This is a topology-only recovery locator.  It intentionally does not
        inspect or alter Turn execution state: callers must validate and replay
        the already registered frozen request through the existing runner.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 256:
            raise ValueError("organization start pair limit must be between 1 and 256")
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT main.payload_json, steward.payload_json "
                "FROM ai_agent_runs AS main "
                "JOIN ai_agent_runs AS steward "
                "ON steward.parent_run_id=main.run_id AND steward.project_id=main.project_id "
                "WHERE main.parent_run_id IS NULL "
                "AND json_extract(main.payload_json, '$.role')='main' "
                "AND json_extract(main.payload_json, '$.profile_id')='main.orchestrator' "
                "AND json_extract(main.payload_json, '$.status') NOT IN ('completed','failed','cancelled','timed_out','quarantined') "
                "AND json_extract(steward.payload_json, '$.role')='subagent' "
                "AND json_extract(steward.payload_json, '$.profile_id')='steward.scheduler' "
                "ORDER BY main.project_id, main.run_id, steward.run_id LIMIT ?",
                (limit,),
            ).fetchall()
            return tuple((_run(_load(row[0])), _run(_load(row[1]))) for row in rows)
        finally:
            connection.close()

    def list_supervision_candidates(self, *, limit: int = 64) -> tuple[AgentRun, ...]:
        """Return bounded main Runs that may need receipt-only World review.

        The store deliberately does not read Turn requests, so session
        membership remains the observer's verified request-boundary check.
        A main Run is eligible only when a durable fan-in result is terminal.
        Terminal mains without fan-in evidence cannot satisfy the observer and
        would otherwise permanently occupy the bounded recovery window.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 256:
            raise ValueError("supervision candidate limit must be between 1 and 256")
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT main.payload_json "
                "FROM ai_agent_runs AS main "
                "WHERE main.parent_run_id IS NULL "
                "AND json_extract(main.payload_json, '$.role')='main' "
                "AND EXISTS ("
                "SELECT 1 FROM ai_agent_fan_ins AS fan "
                "JOIN ai_agent_fan_in_results AS result ON result.fan_in_id=fan.fan_in_id "
                "WHERE fan.parent_run_id=main.run_id "
                "AND fan.project_id=main.project_id "
                "AND json_extract(result.payload_json, '$.status') IN ('completed','failed','cancelled','timed_out')"
                ") "
                # Prefer the newest persisted main runs.  Historical noops must
                # not occupy the bounded window ahead of a fresh crash gap.
                "ORDER BY main.rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return tuple(_run(_load(row[0])) for row in rows)
        finally:
            connection.close()

    def get_child_link(self, link_id: str, *, project_id: str) -> AgentChildLink | None:
        row = self._read_one("SELECT payload_json FROM ai_agent_child_links WHERE link_id=? AND project_id=?", (link_id, project_id))
        return _link(_load(row[0])) if row else None

    def list_child_links(self, *, project_id: str, parent_run_id: str | None = None) -> tuple[AgentChildLink, ...]:
        where, params = ("project_id=?", (project_id,)) if parent_run_id is None else ("project_id=? AND parent_run_id=?", (project_id, parent_run_id))
        return self._list_payloads(f"SELECT payload_json FROM ai_agent_child_links WHERE {where} ORDER BY link_id", params, _link)

    def get_message(self, message_id: str, *, project_id: str) -> AgentMessage | None:
        row = self._read_one("SELECT payload_json FROM ai_agent_messages WHERE message_id=? AND project_id=?", (message_id, project_id))
        return _message(_load(row[0])) if row else None

    def list_messages(self, *, project_id: str, run_id: str, status: str | None = None) -> tuple[AgentMessage, ...]:
        clause = "project_id=? AND (sender_run_id=? OR recipient_run_id=?)"
        params: tuple[object, ...] = (project_id, run_id, run_id)
        if status is not None:
            clause += " AND status=?"; params += (status,)
        return self._list_payloads(f"SELECT payload_json FROM ai_agent_messages WHERE {clause} ORDER BY sequence,message_id", params, _message)

    def get_reservation(self, reservation_id: str, *, project_id: str) -> AgentBudgetReservation | None:
        row = self._read_one("SELECT payload_json FROM ai_agent_budget_reservations WHERE reservation_id=? AND project_id=?", (reservation_id, project_id))
        return _reservation(_load(row[0])) if row else None

    def list_reservations(self, *, project_id: str, parent_run_id: str | None = None) -> tuple[AgentBudgetReservation, ...]:
        where, params = ("project_id=?", (project_id,)) if parent_run_id is None else ("project_id=? AND parent_run_id=?", (project_id, parent_run_id))
        return self._list_payloads(f"SELECT payload_json FROM ai_agent_budget_reservations WHERE {where} ORDER BY reservation_id", params, _reservation)

    def get_fan_in(self, fan_in_id: str, *, project_id: str) -> AgentFanIn | None:
        row = self._read_one("SELECT payload_json FROM ai_agent_fan_ins WHERE fan_in_id=? AND project_id=?", (fan_in_id, project_id))
        return _fan_in(_load(row[0])) if row else None

    def list_fan_ins(self, *, project_id: str, parent_run_id: str | None = None) -> tuple[AgentFanIn, ...]:
        where, params = ("project_id=?", (project_id,)) if parent_run_id is None else ("project_id=? AND parent_run_id=?", (project_id, parent_run_id))
        return self._list_payloads(f"SELECT payload_json FROM ai_agent_fan_ins WHERE {where} ORDER BY fan_in_id", params, _fan_in)

    def get_fan_in_result(self, fan_in_id: str, *, project_id: str) -> AgentFanInResult | None:
        row = self._read_one("SELECT r.payload_json FROM ai_agent_fan_in_results r JOIN ai_agent_fan_ins f ON f.fan_in_id=r.fan_in_id WHERE r.fan_in_id=? AND f.project_id=?", (fan_in_id, project_id))
        return _fan_result(_load(row[0])) if row else None

    def transition_run(self, run: AgentRun, *, expected_revision: int, operation_id: str) -> AgentRun:
        return self._write_run(run, operation_id=operation_id, expected_revision=expected_revision, require_turn=True)

    def converge_terminal_main(
        self, run: AgentRun, *, operation_id: str,
    ) -> tuple[AgentRun, bool]:
        """Project one authoritative terminal main Turn into Agent state.

        Main Runs have no parent reservation to settle.  They may converge only
        after every direct child and fan-in has already left its active state,
        which keeps a prematurely terminal Turn visible for bounded recovery
        instead of silently abandoning organization work.
        """

        if (
            not run.is_terminal or run.role != "main"
            or run.parent_run_id is not None or run.terminal_receipt_ref is None
        ):
            raise AgentStoreInvalidTransition(
                "terminal convergence requires a terminal main receipt"
            )
        request = _json(agent_run_to_payload(run))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._operation(
                connection, operation_id, "converge_terminal_main", request,
            )
            if replay is not None:
                connection.execute("COMMIT")
                return _run(replay), False
            stored, revision = self._run_row(connection, run.run_id)
            if (
                stored.turn_id != run.turn_id
                or stored.project_id != run.project_id
                or stored.role != "main"
                or stored.parent_run_id is not None
                or not _same_run_snapshot(stored, run)
            ):
                raise AgentStoreInvalidTransition(
                    "terminal main snapshot drifted"
                )
            if stored.is_terminal:
                if stored != run:
                    raise AgentStoreInvalidTransition(
                        "terminal main convergence drifted"
                    )
                self._save_operation(
                    connection, operation_id, "converge_terminal_main",
                    request, agent_run_to_payload(stored),
                )
                connection.execute("COMMIT")
                return stored, False
            active_children = connection.execute(
                "SELECT COUNT(*) FROM ai_agent_child_links "
                "WHERE parent_run_id=? AND status IN "
                "('reserved','spawned','started','cancelling')",
                (run.run_id,),
            ).fetchone()
            active_fan_ins = connection.execute(
                "SELECT COUNT(*) FROM ai_agent_fan_ins "
                "WHERE parent_run_id=? AND status IN ('open','collecting')",
                (run.run_id,),
            ).fetchone()
            if int(active_children[0]) or int(active_fan_ins[0]):
                raise AgentStoreInvalidTransition(
                    "terminal main still owns active organization work"
                )
            self._update_run(connection, run, revision)
            self._save_operation(
                connection, operation_id, "converge_terminal_main",
                request, agent_run_to_payload(run),
            )
            connection.execute("COMMIT")
            return run, True
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def reserve_spawn(
        self, *, parent: AgentRun, child: AgentRun, link: AgentChildLink,
        reservation: AgentBudgetReservation,
    ) -> tuple[AgentRun, AgentChildLink, AgentBudgetReservation, bool]:
        """Atomically reserve a child identity, delegated budget and topology edge.

        The child Turn is intentionally not inserted here.  The Turn authority
        accepts it later, then ``finalize_spawn`` binds the durable reservation.
        """
        try:
            validate_child_delegation(parent, child, link)
        except AgentContractError as error:
            raise AgentStoreInvalidTransition(str(error)) from error
        if link.status != "reserved" or reservation.status != "reserved":
            raise AgentStoreInvalidTransition("spawn must begin as a reservation")
        if child.status not in {"created", "queued"}:
            raise AgentStoreInvalidTransition("reserved child must be created or queued")
        if reservation.operation_id != link.spawn_operation_id:
            raise AgentStoreInvalidTransition("spawn operation identity drifted")
        if (reservation.parent_run_id, reservation.child_run_id, reservation.project_id,
            reservation.parent_cancel_epoch, reservation.reserved_budget) != (
                parent.run_id, child.run_id, parent.project_id, parent.cancel_epoch, child.budget_limit):
            raise AgentStoreInvalidTransition("spawn reservation identity drifted")
        request = _json({"parent": agent_run_to_payload(parent), "child": agent_run_to_payload(child),
                         "link": agent_child_link_to_payload(link), "reservation": agent_budget_reservation_to_payload(reservation)})
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._operation(connection, link.spawn_operation_id, "reserve_spawn", request)
            if replay is not None:
                connection.execute("COMMIT")
                return (_run(replay["run"]), _link(replay["link"]), _reservation(replay["reservation"]), False)
            stored_parent, _ = self._run_row(connection, parent.run_id)
            self._assert_parent_current(stored_parent, parent)
            # The parent is an existing frozen Run.  A later Profile edit must
            # not revoke its already accepted delegation authority; only the
            # newly reserved child is checked against current configuration.
            self._assert_profile_snapshot(connection, child)
            if stored_parent.is_terminal or not stored_parent.allow_child_spawn:
                raise AgentStoreInvalidTransition("parent cannot spawn children")
            if not _turn_exists(connection, stored_parent.turn_id):
                raise AgentStoreNotFound("parent Turn authority record is missing")
            active = connection.execute("SELECT COUNT(*) FROM ai_agent_child_links WHERE parent_run_id=? AND status IN ('reserved','spawned','started','cancelling')", (parent.run_id,)).fetchone()
            if int(active[0]) >= stored_parent.max_concurrent_children:
                raise AgentStoreLimitExceeded("parent child concurrency limit reached")
            self._assert_budget_available(connection, stored_parent, reservation.reserved_budget)
            if connection.execute("SELECT 1 FROM ai_agent_runs WHERE run_id=?", (child.run_id,)).fetchone():
                raise AgentStoreConflict("child run identity already exists")
            if connection.execute("SELECT 1 FROM ai_agent_child_links WHERE link_id=?", (link.link_id,)).fetchone():
                raise AgentStoreConflict("child link identity already exists")
            connection.execute("INSERT INTO ai_agent_runs(run_id,turn_id,project_id,parent_run_id,revision,payload_json) VALUES(?,?,?,?,1,?)", (child.run_id, child.turn_id, child.project_id, child.parent_run_id, _json(agent_run_to_payload(child))))
            connection.execute("INSERT INTO ai_agent_child_links(link_id,parent_run_id,child_run_id,project_id,status,payload_json) VALUES(?,?,?,?,?,?)", (link.link_id, link.parent_run_id, link.child_run_id, link.parent_project_id, link.status, _json(agent_child_link_to_payload(link))))
            connection.execute("INSERT INTO ai_agent_budget_reservations(reservation_id,parent_run_id,child_run_id,project_id,status,payload_json) VALUES(?,?,?,?,?,?)", (reservation.reservation_id, reservation.parent_run_id, reservation.child_run_id, reservation.project_id, reservation.status, _json(agent_budget_reservation_to_payload(reservation))))
            result = {"run": agent_run_to_payload(child), "link": agent_child_link_to_payload(link), "reservation": agent_budget_reservation_to_payload(reservation)}
            self._save_operation(connection, link.spawn_operation_id, "reserve_spawn", request, result)
            connection.execute("COMMIT")
            return child, link, reservation, True
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def finalize_spawn(self, link_id: str, *, operation_id: str, expected_cancel_epoch: int) -> AgentChildLink:
        connection = self._connect()
        request = _json({"link_id": link_id, "cancel_epoch": expected_cancel_epoch})
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._operation(connection, operation_id, "finalize_spawn", request)
            if replay is not None:
                connection.execute("COMMIT")
                return _link(replay)
            link = self._link_row(connection, link_id)
            parent, _ = self._run_row(connection, link.parent_run_id)
            self._assert_epoch(parent, expected_cancel_epoch)
            if link.status == "spawned":
                self._save_operation(connection, operation_id, "finalize_spawn", request, agent_child_link_to_payload(link))
                connection.execute("COMMIT")
                return link
            if link.status != "reserved" or not _turn_exists(connection, self._run_row(connection, link.child_run_id)[0].turn_id):
                raise AgentStoreInvalidTransition("reserved child Turn has not been accepted")
            updated = _replace_link(link, status="spawned")
            self._update_link(connection, updated)
            self._save_operation(connection, operation_id, "finalize_spawn", request, agent_child_link_to_payload(updated))
            connection.execute("COMMIT")
            return updated
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def transition_child_link(self, link_id: str, *, status: str, operation_id: str, expected_cancel_epoch: int) -> AgentChildLink:
        request = _json({"link_id": link_id, "status": status, "epoch": expected_cancel_epoch})
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._operation(connection, operation_id, "transition_child_link", request)
            if replay is not None:
                connection.execute("COMMIT")
                return _link(replay)
            link = self._link_row(connection, link_id)
            parent, _ = self._run_row(connection, link.parent_run_id)
            self._assert_epoch(parent, expected_cancel_epoch)
            child, _ = self._run_row(connection, link.child_run_id)
            allowed = {"reserved": {"spawned", "cancelled"}, "spawned": {"started", "cancelling", "cancelled"}, "started": {"cancelling", "completed", "failed", "cancelled", "timed_out", "quarantined"}, "cancelling": {"cancelled", "failed", "timed_out", "quarantined"}}
            if status == link.status:
                self._save_operation(connection, operation_id, "transition_child_link", request, agent_child_link_to_payload(link))
                connection.execute("COMMIT")
                return link
            if status not in allowed.get(link.status, set()):
                raise AgentStoreInvalidTransition("child link transition is invalid")
            if status == "started" and not _turn_exists(connection, child.turn_id):
                raise AgentStoreInvalidTransition("child Turn has not been accepted")
            if status in _TERMINAL and (not child.is_terminal or child.terminal_receipt_ref is None or child.status != status):
                raise AgentStoreInvalidTransition("terminal link requires matching terminal child receipt")
            updated = _replace_link(link, status=status)
            self._update_link(connection, updated)
            self._save_operation(connection, operation_id, "transition_child_link", request, agent_child_link_to_payload(updated))
            connection.execute("COMMIT")
            return updated
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def abort_reserved_spawn(self, link_id: str, reservation_id: str, *, operation_id: str, expected_cancel_epoch: int) -> tuple[AgentChildLink, AgentBudgetReservation]:
        request = _json({"link_id": link_id, "reservation_id": reservation_id, "epoch": expected_cancel_epoch})
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._operation(connection, operation_id, "abort_reserved_spawn", request)
            if replay is not None:
                connection.execute("COMMIT")
                return _link(replay["link"]), _reservation(replay["reservation"])
            link = self._link_row(connection, link_id)
            reservation = self._reservation_row(connection, reservation_id)
            if (link.parent_run_id, link.child_run_id, link.parent_project_id) != (reservation.parent_run_id, reservation.child_run_id, reservation.project_id):
                raise AgentStoreInvalidTransition("spawn abort identity drifted")
            parent, _ = self._run_row(connection, link.parent_run_id)
            child, _ = self._run_row(connection, link.child_run_id)
            self._assert_epoch(parent, expected_cancel_epoch)
            if link.status != "reserved" or reservation.status != "reserved" or _turn_exists(connection, child.turn_id):
                raise AgentStoreInvalidTransition("only an unaccepted reserved spawn can be aborted")
            cancelled = _replace_link(link, status="cancelled")
            released = _replace_reservation(reservation, settled_budget=_ZERO, status="released")
            self._update_link(connection, cancelled)
            self._update_reservation(connection, released)
            result = {"link": agent_child_link_to_payload(cancelled), "reservation": agent_budget_reservation_to_payload(released)}
            self._save_operation(connection, operation_id, "abort_reserved_spawn", request, result)
            connection.execute("COMMIT")
            return cancelled, released
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def cancel_parent(self, parent_run_id: str, *, expected_revision: int, operation_id: str) -> AgentRun:
        connection = self._connect()
        request = _json({"parent_run_id": parent_run_id, "expected_revision": expected_revision})
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._operation(connection, operation_id, "cancel_parent", request)
            if replay is not None:
                connection.execute("COMMIT")
                return _run(replay)
            parent, revision = self._run_row(connection, parent_run_id)
            if revision != expected_revision:
                raise AgentStoreConflict("parent run revision conflict")
            if parent.is_terminal:
                raise AgentStoreInvalidTransition("terminal parent cannot be cancelled")
            if parent.status == "cancelling":
                self._save_operation(connection, operation_id, "cancel_parent", request, agent_run_to_payload(parent))
                connection.execute("COMMIT")
                return parent
            updated = _replace_run(parent, status="cancelling", cancel_epoch=parent.cancel_epoch + 1)
            self._update_run(connection, updated, expected_revision)
            self._cancel_parent_descendants(connection, parent_run_id)
            self._save_operation(connection, operation_id, "cancel_parent", request, agent_run_to_payload(updated))
            connection.execute("COMMIT")
            return updated
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def send_message(self, message: AgentMessage) -> tuple[AgentMessage, bool]:
        request = _json(agent_message_to_payload(message))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._operation(connection, message.operation_id, "send_message", request)
            if replay is not None:
                connection.execute("COMMIT")
                return _message(replay), False
            if message.status != "pending":
                raise AgentStoreInvalidTransition("new messages must be pending")
            self._assert_related_message(connection, message)
            existing = connection.execute("SELECT 1 FROM ai_agent_messages WHERE message_id=?", (message.message_id,)).fetchone()
            if existing:
                raise AgentStoreConflict("message identity already exists")
            prior = connection.execute("SELECT MAX(sequence) FROM ai_agent_messages WHERE sender_run_id=? AND recipient_run_id=?", (message.sender_run_id, message.recipient_run_id)).fetchone()
            if int(prior[0] or 0) + 1 != message.sequence:
                raise AgentStoreConflict("message sequence is not contiguous")
            connection.execute("INSERT INTO ai_agent_messages(message_id,project_id,sender_run_id,recipient_run_id,operation_id,sequence,status,payload_json) VALUES(?,?,?,?,?,?,?,?)", (message.message_id, message.project_id, message.sender_run_id, message.recipient_run_id, message.operation_id, message.sequence, message.status, _json(agent_message_to_payload(message))))
            self._save_operation(connection, message.operation_id, "send_message", request, agent_message_to_payload(message))
            connection.execute("COMMIT")
            return message, True
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def send_message_next(self, message: AgentMessage) -> tuple[AgentMessage, bool]:
        """Allocate a direction-local sequence and persist a message atomically.

        ``message.sequence`` must be the coordinator sentinel ``1``. It is
        excluded from the operation identity because the durable sequence is
        assigned inside the transaction and returned in the stored result.
        """
        if message.sequence != 1:
            raise AgentStoreInvalidTransition("automatic message sequence must use sentinel one")
        request_payload = agent_message_to_payload(message)
        request_payload.pop("sequence")
        request = _json(request_payload)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._operation(
                connection, message.operation_id, "send_message_next", request,
            )
            if replay is not None:
                connection.execute("COMMIT")
                return _message(replay), False
            if message.status != "pending":
                raise AgentStoreInvalidTransition("new messages must be pending")
            self._assert_related_message(connection, message)
            existing = connection.execute(
                "SELECT 1 FROM ai_agent_messages WHERE message_id=?",
                (message.message_id,),
            ).fetchone()
            if existing:
                raise AgentStoreConflict("message identity already exists")
            prior = connection.execute(
                "SELECT MAX(sequence) FROM ai_agent_messages WHERE sender_run_id=? AND recipient_run_id=?",
                (message.sender_run_id, message.recipient_run_id),
            ).fetchone()
            stored = _replace_message(message, sequence=int(prior[0] or 0) + 1)
            connection.execute(
                "INSERT INTO ai_agent_messages(message_id,project_id,sender_run_id,recipient_run_id,operation_id,sequence,status,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                (
                    stored.message_id, stored.project_id, stored.sender_run_id,
                    stored.recipient_run_id, stored.operation_id, stored.sequence,
                    stored.status, _json(agent_message_to_payload(stored)),
                ),
            )
            self._save_operation(
                connection, message.operation_id, "send_message_next", request,
                agent_message_to_payload(stored),
            )
            connection.execute("COMMIT")
            return stored, True
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def deliver_message(self, message_id: str, *, operation_id: str, expected_cancel_epoch: int) -> AgentMessage:
        return self._message_transition(message_id, operation_id, expected_cancel_epoch, "pending", "delivered")

    def acknowledge_message(self, message_id: str, *, operation_id: str, expected_cancel_epoch: int) -> AgentMessage:
        return self._message_transition(message_id, operation_id, expected_cancel_epoch, "delivered", "acknowledged")

    def cancel_message(self, message_id: str, *, operation_id: str, expected_cancel_epoch: int) -> AgentMessage:
        return self._message_transition(message_id, operation_id, expected_cancel_epoch, ("pending", "delivered"), "cancelled")

    def create_fan_in(self, fan_in: AgentFanIn) -> tuple[AgentFanIn, bool]:
        request = _json(agent_fan_in_to_payload(fan_in))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._operation(connection, fan_in.operation_id, "create_fan_in", request)
            if replay is not None:
                connection.execute("COMMIT")
                return _fan_in(replay), False
            if fan_in.status != "open":
                raise AgentStoreInvalidTransition("new fan-in must be open")
            parent, _ = self._run_row(connection, fan_in.parent_run_id)
            self._assert_epoch(parent, fan_in.cancel_epoch)
            rows = connection.execute("SELECT child_run_id,status FROM ai_agent_child_links WHERE parent_run_id=? AND project_id=?", (fan_in.parent_run_id, fan_in.project_id)).fetchall()
            accepted: set[str] = set()
            for child_run_id, status in rows:
                child_id = str(child_run_id)
                if status == "reserved":
                    continue
                if status == "cancelled":
                    child, _ = self._run_row(connection, child_id)
                    if (
                        not _turn_exists(connection, child.turn_id)
                        or not child.is_terminal
                        or child.terminal_receipt_ref is None
                    ):
                        # A pre-Turn spawn abort is a coordination tombstone,
                        # not a terminal Child result that a parent may fan in.
                        continue
                accepted.add(child_id)
            if not set(fan_in.child_run_ids).issubset(accepted):
                raise AgentStoreInvalidTransition("fan-in children must be bound direct children")
            connection.execute("INSERT INTO ai_agent_fan_ins(fan_in_id,parent_run_id,project_id,status,payload_json) VALUES(?,?,?,?,?)", (fan_in.fan_in_id, fan_in.parent_run_id, fan_in.project_id, fan_in.status, _json(agent_fan_in_to_payload(fan_in))))
            self._save_operation(connection, fan_in.operation_id, "create_fan_in", request, agent_fan_in_to_payload(fan_in))
            connection.execute("COMMIT")
            return fan_in, True
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def complete_fan_in(self, result: AgentFanInResult, *, operation_id: str, expected_cancel_epoch: int) -> tuple[AgentFanInResult, bool]:
        request = _json(agent_fan_in_result_to_payload(result))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._operation(connection, operation_id, "complete_fan_in", request)
            if replay is not None:
                connection.execute("COMMIT")
                return _fan_result(replay), False
            fan_in = self._fan_in_row(connection, result.fan_in_id)
            parent, _ = self._run_row(connection, fan_in.parent_run_id)
            self._assert_epoch(parent, expected_cancel_epoch)
            if fan_in.status in {"completed", "failed", "cancelled"}:
                raise AgentStoreInvalidTransition("fan-in is already terminal")
            try:
                validate_fan_in_result(fan_in, result)
            except AgentContractError as error:
                raise AgentStoreInvalidTransition(str(error)) from error
            for summary in result.child_summaries:
                child, _ = self._run_row(connection, summary.child_run_id)
                if child.project_id != result.project_id or not child.is_terminal or child.status != summary.status or child.terminal_receipt_ref != summary.receipt_ref:
                    raise AgentStoreInvalidTransition("fan-in child summary is not a verified terminal receipt")
                if not summary.usage.is_subset_of(child.budget_limit):
                    raise AgentStoreInvalidTransition("fan-in child usage exceeds child budget")
            connection.execute("INSERT INTO ai_agent_fan_in_results(result_id,fan_in_id,payload_json) VALUES(?,?,?)", (result.result_id, result.fan_in_id, _json(agent_fan_in_result_to_payload(result))))
            fan_status = "completed" if result.status == "completed" else ("failed" if result.status in {"failed", "timed_out"} else "cancelled")
            updated = _replace_fan_in(fan_in, status=fan_status)
            self._update_fan_in(connection, updated)
            self._save_operation(connection, operation_id, "complete_fan_in", request, agent_fan_in_result_to_payload(result))
            connection.execute("COMMIT")
            return result, True
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def settle_reservation(self, reservation_id: str, *, usage: AgentBudget, operation_id: str, expected_cancel_epoch: int) -> AgentBudgetReservation:
        return self._finish_reservation(reservation_id, usage, operation_id, expected_cancel_epoch, "settled")

    def release_reservation(self, reservation_id: str, *, operation_id: str, expected_cancel_epoch: int) -> AgentBudgetReservation:
        return self._finish_reservation(reservation_id, _ZERO, operation_id, expected_cancel_epoch, "released")

    def converge_terminal_child(
        self, run: AgentRun, *, usage: AgentBudget, operation_id: str,
    ) -> tuple[AgentRun, AgentChildLink, AgentBudgetReservation, bool]:
        """Atomically project one authoritative terminal Turn into Agent state.

        The caller supplies only refs extracted from the durable Turn stream.
        This store validates the frozen identity, then settles the linked
        reservation and releases the parent's active-child slot in the same
        transaction.  Replays return the original projection.
        """
        if not run.is_terminal or run.role != "subagent" or run.terminal_receipt_ref is None:
            raise AgentStoreInvalidTransition("terminal convergence requires a terminal child receipt")
        if not usage.is_subset_of(run.budget_limit):
            raise AgentStoreInvalidTransition("terminal child usage exceeds delegated budget")
        request = _json({"run": agent_run_to_payload(run), "usage": _budget_payload(usage)})
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._operation(connection, operation_id, "converge_terminal_child", request)
            if replay is not None:
                connection.execute("COMMIT")
                return _run(replay["run"]), _link(replay["link"]), _reservation(replay["reservation"]), False
            stored, revision = self._run_row(connection, run.run_id)
            if stored.turn_id != run.turn_id or stored.project_id != run.project_id or not _same_run_snapshot(stored, run):
                raise AgentStoreInvalidTransition("terminal child snapshot drifted")
            link = next((_link(_load(row[0])) for row in connection.execute("SELECT payload_json FROM ai_agent_child_links WHERE child_run_id=?", (run.run_id,))), None)
            reservation = next((_reservation(_load(row[0])) for row in connection.execute("SELECT payload_json FROM ai_agent_budget_reservations WHERE child_run_id=?", (run.run_id,))), None)
            if link is None or reservation is None or link.parent_run_id != run.parent_run_id or reservation.parent_run_id != run.parent_run_id:
                raise AgentStoreInvalidTransition("terminal child topology is unavailable")
            parent, _ = self._run_row(connection, link.parent_run_id)
            if parent.cancel_epoch != run.cancel_epoch or link.parent_cancel_epoch != run.cancel_epoch or reservation.parent_cancel_epoch != run.cancel_epoch:
                raise AgentStoreStaleEpoch("terminal child cancellation epoch is stale")
            if stored.is_terminal:
                if stored != run or link.status != run.status or reservation.status != "settled":
                    raise AgentStoreInvalidTransition("terminal child convergence drifted")
                result = {"run": agent_run_to_payload(stored), "link": agent_child_link_to_payload(link), "reservation": agent_budget_reservation_to_payload(reservation)}
                self._save_operation(connection, operation_id, "converge_terminal_child", request, result)
                connection.execute("COMMIT")
                return stored, link, reservation, False
            if link.status not in {"spawned", "started", "cancelling"} or reservation.status != "reserved":
                raise AgentStoreInvalidTransition("terminal child is not active")
            self._update_run(connection, run, revision)
            terminal_link = _replace_link(link, status=run.status)
            settled = _replace_reservation(reservation, settled_budget=usage, status="settled")
            self._update_link(connection, terminal_link)
            self._update_reservation(connection, settled)
            result = {"run": agent_run_to_payload(run), "link": agent_child_link_to_payload(terminal_link), "reservation": agent_budget_reservation_to_payload(settled)}
            self._save_operation(connection, operation_id, "converge_terminal_child", request, result)
            connection.execute("COMMIT")
            return run, terminal_link, settled, True
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def _write_profile(self, profile: AgentProfile, payload: str, expected_revision: int | None) -> AgentProfile:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT revision,payload_json FROM ai_agent_profiles WHERE profile_id=?", (profile.profile_id,)).fetchone()
            if row is None:
                # A registry's first persisted built-in override advances its
                # visible revision-one default to revision two.
                if expected_revision is not None or profile.revision not in {1, 2}:
                    raise AgentStoreConflict("new profile revision is invalid")
                connection.execute("INSERT INTO ai_agent_profiles(profile_id,revision,payload_json) VALUES(?,?,?)", (profile.profile_id, profile.revision, payload))
            else:
                if expected_revision is None or int(row[0]) != expected_revision or profile.revision != expected_revision + 1:
                    raise AgentStoreConflict("agent profile revision conflict")
                if connection.execute("UPDATE ai_agent_profiles SET revision=?,payload_json=? WHERE profile_id=? AND revision=?", (profile.revision, payload, profile.profile_id, expected_revision)).rowcount != 1:
                    raise AgentStoreConflict("agent profile revision conflict")
            connection.execute("COMMIT")
            return profile
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def _write_run(self, run: AgentRun, *, operation_id: str, expected_revision: int | None, require_turn: bool) -> AgentRun:
        request = _json(agent_run_to_payload(run))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._operation(connection, operation_id, "write_run", request)
            if replay is not None:
                connection.execute("COMMIT")
                return _run(replay)
            if require_turn and not _turn_exists(connection, run.turn_id):
                raise AgentStoreNotFound("Turn authority record is missing")
            row = connection.execute("SELECT revision FROM ai_agent_runs WHERE run_id=?", (run.run_id,)).fetchone()
            if row is None:
                if expected_revision is not None:
                    raise AgentStoreNotFound("agent run was not found")
                # Profile configuration governs admission of new Runs.  Once
                # persisted, the Run itself is the immutable authority
                # snapshot for lifecycle and terminal convergence.
                self._assert_profile_snapshot(connection, run)
                connection.execute("INSERT INTO ai_agent_runs(run_id,turn_id,project_id,parent_run_id,revision,payload_json) VALUES(?,?,?,?,1,?)", (run.run_id, run.turn_id, run.project_id, run.parent_run_id, request))
            else:
                if expected_revision is None or int(row[0]) != expected_revision:
                    raise AgentStoreConflict("agent run revision conflict")
                previous, _ = self._run_row(connection, run.run_id)
                if previous.is_terminal or not _same_run_snapshot(previous, run):
                    raise AgentStoreInvalidTransition("agent run snapshot transition is invalid")
                if run.status != previous.status and run.status not in _RUN_TRANSITIONS.get(previous.status, frozenset()):
                    raise AgentStoreInvalidTransition("agent run status transition is invalid")
                self._update_run(connection, run, expected_revision)
            self._save_operation(connection, operation_id, "write_run", request, agent_run_to_payload(run))
            connection.execute("COMMIT")
            return run
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def _finish_reservation(self, reservation_id: str, usage: AgentBudget, operation_id: str, expected_cancel_epoch: int, status: str) -> AgentBudgetReservation:
        request = _json({"reservation_id": reservation_id, "usage": _budget_payload(usage), "epoch": expected_cancel_epoch, "status": status})
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._operation(connection, operation_id, "finish_reservation", request)
            if replay is not None:
                connection.execute("COMMIT")
                return _reservation(replay)
            reservation = self._reservation_row(connection, reservation_id)
            parent, _ = self._run_row(connection, reservation.parent_run_id)
            self._assert_epoch(parent, expected_cancel_epoch)
            if reservation.status != "reserved" or not usage.is_subset_of(reservation.reserved_budget):
                raise AgentStoreInvalidTransition("budget reservation cannot be finished")
            child, _ = self._run_row(connection, reservation.child_run_id)
            if status == "settled" and (not child.is_terminal or child.terminal_receipt_ref is None or not usage.is_subset_of(child.budget_limit)):
                raise AgentStoreInvalidTransition("settlement requires a terminal child receipt within its budget")
            updated = _replace_reservation(reservation, settled_budget=usage, status=status)
            self._update_reservation(connection, updated)
            self._save_operation(connection, operation_id, "finish_reservation", request, agent_budget_reservation_to_payload(updated))
            connection.execute("COMMIT")
            return updated
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def _message_transition(self, message_id: str, operation_id: str, epoch: int, current: str | tuple[str, ...], target: str) -> AgentMessage:
        request = _json({"message_id": message_id, "epoch": epoch, "target": target})
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._operation(connection, operation_id, "message_transition", request)
            if replay is not None:
                connection.execute("COMMIT")
                return _message(replay)
            message = self._message_row(connection, message_id)
            self._assert_message_epoch(connection, message, epoch)
            if message.status == target:
                self._save_operation(connection, operation_id, "message_transition", request, agent_message_to_payload(message))
                connection.execute("COMMIT")
                return message
            allowed = (current,) if isinstance(current, str) else current
            if message.status not in allowed:
                raise AgentStoreInvalidTransition("message transition is invalid")
            updated = _replace_message(message, status=target)
            placeholders = ",".join("?" for _ in allowed)
            changed = connection.execute(f"UPDATE ai_agent_messages SET status=?,payload_json=? WHERE message_id=? AND status IN ({placeholders})", (target, _json(agent_message_to_payload(updated)), message_id, *allowed)).rowcount
            if changed != 1:
                raise AgentStoreConflict("message transition conflict")
            self._save_operation(connection, operation_id, "message_transition", request, agent_message_to_payload(updated))
            connection.execute("COMMIT")
            return updated
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def _assert_parent_current(self, stored: AgentRun, supplied: AgentRun) -> None:
        if stored != supplied:
            raise AgentStoreConflict("parent run snapshot is stale")
        self._assert_epoch(stored, supplied.cancel_epoch)

    @staticmethod
    def _assert_epoch(run: AgentRun, expected: int) -> None:
        if run.cancel_epoch != expected:
            raise AgentStoreStaleEpoch("agent cancellation epoch is stale")

    def _assert_budget_available(self, connection: sqlite3.Connection, parent: AgentRun, requested: AgentBudget) -> None:
        rows = connection.execute("SELECT payload_json FROM ai_agent_budget_reservations WHERE parent_run_id=?", (parent.run_id,)).fetchall()
        used = _ZERO
        for row in rows:
            reservation = _reservation(_load(row[0]))
            try:
                used = used.plus(reservation.reserved_budget if reservation.status == "reserved" else reservation.settled_budget or _ZERO)
            except AgentContractError as error:
                raise AgentStoreLimitExceeded("parent budget accounting overflowed") from error
        try:
            total = used.plus(requested)
        except AgentContractError as error:
            raise AgentStoreLimitExceeded("parent budget reservation overflowed") from error
        if not total.is_subset_of(parent.budget_limit):
            raise AgentStoreLimitExceeded("parent budget reservation limit reached")

    @staticmethod
    def _assert_profile_snapshot(connection: sqlite3.Connection, run: AgentRun) -> None:
        row = connection.execute("SELECT payload_json FROM ai_agent_profiles WHERE profile_id=?", (run.profile_id,)).fetchone()
        if row is None:
            profile = next((item for item in builtin_agent_profiles() if item.profile_id == run.profile_id), None)
            if profile is None:
                raise AgentStoreInvalidTransition("custom agent profile must be persisted")
        else:
            profile = _profile(_load(row[0]))
        if (profile.revision, profile.role, profile.model_tier) != (run.profile_revision, run.role, run.model_tier):
            raise AgentStoreInvalidTransition("agent run profile snapshot drifted")
        if not profile.enabled or not run.budget_limit.is_subset_of(profile.budget_limit) or not set(run.capability_ids).issubset(profile.capability_ids):
            raise AgentStoreInvalidTransition("agent run profile authority expanded")
        if run.max_concurrent_children > profile.max_concurrent_children or run.max_depth > profile.max_depth or run.max_steps > profile.max_steps or run.timeout_ms > profile.timeout_ms or (run.allow_child_spawn and not profile.allow_child_spawn):
            raise AgentStoreInvalidTransition("agent run profile limits expanded")

    def _validate_run_profile(self, run: AgentRun) -> None:
        connection = self._connect()
        try:
            self._assert_profile_snapshot(connection, run)
        finally:
            connection.close()

    def _assert_related_message(self, connection: sqlite3.Connection, message: AgentMessage) -> None:
        sender, _ = self._run_row(connection, message.sender_run_id)
        recipient, _ = self._run_row(connection, message.recipient_run_id)
        if sender.project_id != message.project_id or recipient.project_id != message.project_id:
            raise AgentStoreInvalidTransition("message cannot cross project scope")
        row = connection.execute("SELECT parent_run_id FROM ai_agent_child_links WHERE (parent_run_id=? AND child_run_id=?) OR (parent_run_id=? AND child_run_id=?)", (sender.run_id, recipient.run_id, recipient.run_id, sender.run_id)).fetchone()
        if row is None:
            raise AgentStoreInvalidTransition("message endpoints are not direct relatives")
        parent, _ = self._run_row(connection, str(row[0]))
        self._assert_epoch(parent, message.cancel_epoch)

    def _cancel_parent_descendants(self, connection: sqlite3.Connection, parent_run_id: str) -> None:
        message_rows = connection.execute(
            "SELECT message_id,payload_json FROM ai_agent_messages WHERE status IN ('pending','delivered') AND (sender_run_id=? OR recipient_run_id=? OR recipient_run_id IN (SELECT child_run_id FROM ai_agent_child_links WHERE parent_run_id=?))",
            (parent_run_id, parent_run_id, parent_run_id),
        ).fetchall()
        for message_id, payload in message_rows:
            cancelled = _replace_message(_message(_load(payload)), status="cancelled")
            if connection.execute("UPDATE ai_agent_messages SET status='cancelled',payload_json=? WHERE message_id=?", (_json(agent_message_to_payload(cancelled)), message_id)).rowcount != 1:
                raise AgentStoreConflict("message cancellation conflict")
        for link_id, payload in connection.execute("SELECT link_id,payload_json FROM ai_agent_child_links WHERE parent_run_id=? AND status IN ('reserved','spawned','started')", (parent_run_id,)).fetchall():
            cancelling = _replace_link(_link(_load(payload)), status="cancelling")
            if connection.execute("UPDATE ai_agent_child_links SET status='cancelling',payload_json=? WHERE link_id=?", (_json(agent_child_link_to_payload(cancelling)), link_id)).rowcount != 1:
                raise AgentStoreConflict("child cancellation conflict")
        for fan_in_id, payload in connection.execute("SELECT fan_in_id,payload_json FROM ai_agent_fan_ins WHERE parent_run_id=? AND status IN ('open','collecting')", (parent_run_id,)).fetchall():
            cancelled = _replace_fan_in(_fan_in(_load(payload)), status="cancelled")
            if connection.execute("UPDATE ai_agent_fan_ins SET status='cancelled',payload_json=? WHERE fan_in_id=?", (_json(agent_fan_in_to_payload(cancelled)), fan_in_id)).rowcount != 1:
                raise AgentStoreConflict("fan-in cancellation conflict")

    def _assert_message_epoch(self, connection: sqlite3.Connection, message: AgentMessage, epoch: int) -> None:
        row = connection.execute("SELECT parent_run_id FROM ai_agent_child_links WHERE (parent_run_id=? AND child_run_id=?) OR (parent_run_id=? AND child_run_id=?)", (message.sender_run_id, message.recipient_run_id, message.recipient_run_id, message.sender_run_id)).fetchone()
        if row is None:
            raise AgentStoreInvalidTransition("message endpoints are not direct relatives")
        parent, _ = self._run_row(connection, str(row[0]))
        self._assert_epoch(parent, epoch)
        if message.cancel_epoch != epoch:
            raise AgentStoreStaleEpoch("message cancellation epoch is stale")

    def _operation(self, connection: sqlite3.Connection, operation_id: str, kind: str, request: str) -> object | None:
        row = connection.execute("SELECT kind,request_json,result_json FROM ai_agent_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            return None
        if str(row[0]) != kind or str(row[1]) != request:
            raise AgentStoreConflict("agent operation idempotency identity conflict")
        return _load(row[2])

    @staticmethod
    def _save_operation(connection: sqlite3.Connection, operation_id: str, kind: str, request: str, result: object) -> None:
        connection.execute("INSERT INTO ai_agent_operations(operation_id,kind,request_json,result_json) VALUES(?,?,?,?)", (operation_id, kind, request, _json(result)))

    @staticmethod
    def _run_row(connection: sqlite3.Connection, run_id: str) -> tuple[AgentRun, int]:
        row = connection.execute("SELECT revision,payload_json FROM ai_agent_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise AgentStoreNotFound("agent run was not found")
        return _run(_load(row[1])), int(row[0])

    @staticmethod
    def _link_row(connection: sqlite3.Connection, link_id: str) -> AgentChildLink:
        row = connection.execute("SELECT payload_json FROM ai_agent_child_links WHERE link_id=?", (link_id,)).fetchone()
        if row is None:
            raise AgentStoreNotFound("agent child link was not found")
        return _link(_load(row[0]))

    @staticmethod
    def _reservation_row(connection: sqlite3.Connection, reservation_id: str) -> AgentBudgetReservation:
        row = connection.execute("SELECT payload_json FROM ai_agent_budget_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
        if row is None:
            raise AgentStoreNotFound("agent budget reservation was not found")
        return _reservation(_load(row[0]))

    @staticmethod
    def _message_row(connection: sqlite3.Connection, message_id: str) -> AgentMessage:
        row = connection.execute("SELECT payload_json FROM ai_agent_messages WHERE message_id=?", (message_id,)).fetchone()
        if row is None:
            raise AgentStoreNotFound("agent message was not found")
        return _message(_load(row[0]))

    @staticmethod
    def _fan_in_row(connection: sqlite3.Connection, fan_in_id: str) -> AgentFanIn:
        row = connection.execute("SELECT payload_json FROM ai_agent_fan_ins WHERE fan_in_id=?", (fan_in_id,)).fetchone()
        if row is None:
            raise AgentStoreNotFound("agent fan-in was not found")
        return _fan_in(_load(row[0]))

    @staticmethod
    def _update_run(connection: sqlite3.Connection, run: AgentRun, expected_revision: int) -> None:
        changed = connection.execute("UPDATE ai_agent_runs SET turn_id=?,project_id=?,parent_run_id=?,revision=revision+1,payload_json=? WHERE run_id=? AND revision=?", (run.turn_id, run.project_id, run.parent_run_id, _json(agent_run_to_payload(run)), run.run_id, expected_revision)).rowcount
        if changed != 1:
            raise AgentStoreConflict("agent run revision conflict")

    @staticmethod
    def _update_link(connection: sqlite3.Connection, link: AgentChildLink) -> None:
        if connection.execute("UPDATE ai_agent_child_links SET status=?,payload_json=? WHERE link_id=?", (link.status, _json(agent_child_link_to_payload(link)), link.link_id)).rowcount != 1:
            raise AgentStoreConflict("agent child link update conflict")

    @staticmethod
    def _update_reservation(connection: sqlite3.Connection, reservation: AgentBudgetReservation) -> None:
        if connection.execute("UPDATE ai_agent_budget_reservations SET status=?,payload_json=? WHERE reservation_id=?", (reservation.status, _json(agent_budget_reservation_to_payload(reservation)), reservation.reservation_id)).rowcount != 1:
            raise AgentStoreConflict("agent budget reservation update conflict")

    @staticmethod
    def _update_fan_in(connection: sqlite3.Connection, fan_in: AgentFanIn) -> None:
        if connection.execute("UPDATE ai_agent_fan_ins SET status=?,payload_json=? WHERE fan_in_id=?", (fan_in.status, _json(agent_fan_in_to_payload(fan_in)), fan_in.fan_in_id)).rowcount != 1:
            raise AgentStoreConflict("agent fan-in update conflict")

    def _read_one(self, query: str, params: tuple[object, ...]) -> sqlite3.Row | tuple[object, ...] | None:
        connection = self._connect()
        try:
            return connection.execute(query, params).fetchone()
        finally:
            connection.close()

    def _list_payloads(self, query: str, params: tuple[object, ...], decoder: object) -> tuple[object, ...]:
        connection = self._connect()
        try:
            return tuple(decoder(_load(row[0])) for row in connection.execute(query, params))  # type: ignore[operator]
        finally:
            connection.close()

    @reusable_connection
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5, check_same_thread=False)
        observe_connection(connection)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection


def _initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS ai_agent_profiles(profile_id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>=1), payload_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_agent_runs(run_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, project_id TEXT NOT NULL, parent_run_id TEXT NULL REFERENCES ai_agent_runs(run_id), revision INTEGER NOT NULL CHECK(revision>=1), payload_json TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS ai_agent_runs_turn ON ai_agent_runs(turn_id);
        CREATE TABLE IF NOT EXISTS ai_agent_child_links(link_id TEXT PRIMARY KEY, parent_run_id TEXT NOT NULL REFERENCES ai_agent_runs(run_id), child_run_id TEXT NOT NULL UNIQUE REFERENCES ai_agent_runs(run_id), project_id TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ai_agent_child_links_parent_status ON ai_agent_child_links(parent_run_id,status);
        CREATE TABLE IF NOT EXISTS ai_agent_budget_reservations(reservation_id TEXT PRIMARY KEY, parent_run_id TEXT NOT NULL REFERENCES ai_agent_runs(run_id), child_run_id TEXT NOT NULL UNIQUE REFERENCES ai_agent_runs(run_id), project_id TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ai_agent_budget_parent ON ai_agent_budget_reservations(parent_run_id);
        CREATE TABLE IF NOT EXISTS ai_agent_messages(message_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, sender_run_id TEXT NOT NULL REFERENCES ai_agent_runs(run_id), recipient_run_id TEXT NOT NULL REFERENCES ai_agent_runs(run_id), operation_id TEXT NOT NULL UNIQUE, sequence INTEGER NOT NULL CHECK(sequence>=1), status TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE(sender_run_id,recipient_run_id,sequence));
        CREATE TABLE IF NOT EXISTS ai_agent_fan_ins(fan_in_id TEXT PRIMARY KEY, parent_run_id TEXT NOT NULL REFERENCES ai_agent_runs(run_id), project_id TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_agent_fan_in_results(result_id TEXT PRIMARY KEY, fan_in_id TEXT NOT NULL UNIQUE REFERENCES ai_agent_fan_ins(fan_in_id), payload_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_agent_operations(operation_id TEXT PRIMARY KEY, kind TEXT NOT NULL, request_json TEXT NOT NULL, result_json TEXT NOT NULL);
    """)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _turn_exists(connection: sqlite3.Connection, turn_id: str) -> bool:
    return connection.execute("SELECT 1 FROM ai_turns WHERE turn_id=?", (turn_id,)).fetchone() is not None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: object) -> object:
    return json.loads(str(value))


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.execute("ROLLBACK")


def _budget_payload(value: AgentBudget) -> dict[str, object]:
    return {"model_calls": value.model_calls, "tool_calls": value.tool_calls, "input_tokens": value.input_tokens, "output_tokens": value.output_tokens, "wall_time_ms": value.wall_time_ms}


def _run(value: object) -> AgentRun:
    return agent_run_from_payload(value)


def _profile(value: object) -> AgentProfile:
    return agent_profile_from_payload(value)


def _link(value: object) -> AgentChildLink:
    return agent_child_link_from_payload(value)


def _reservation(value: object) -> AgentBudgetReservation:
    return agent_budget_reservation_from_payload(value)


def _message(value: object) -> AgentMessage:
    return agent_message_from_payload(value)


def _fan_in(value: object) -> AgentFanIn:
    return agent_fan_in_from_payload(value)


def _fan_result(value: object) -> AgentFanInResult:
    return agent_fan_in_result_from_payload(value)


def _replace_run(value: AgentRun, **changes: object) -> AgentRun:
    payload = agent_run_to_payload(value)
    payload.update(changes)
    return agent_run_from_payload(payload)


def _replace_link(value: AgentChildLink, **changes: object) -> AgentChildLink:
    payload = agent_child_link_to_payload(value)
    payload.update(changes)
    return agent_child_link_from_payload(payload)


def _replace_message(value: AgentMessage, **changes: object) -> AgentMessage:
    payload = agent_message_to_payload(value)
    payload.update(changes)
    return agent_message_from_payload(payload)


def _replace_reservation(value: AgentBudgetReservation, **changes: object) -> AgentBudgetReservation:
    payload = agent_budget_reservation_to_payload(value)
    settled = changes.get("settled_budget")
    if isinstance(settled, AgentBudget):
        changes["settled_budget"] = _budget_payload(settled)
    payload.update(changes)
    return agent_budget_reservation_from_payload(payload)


def _replace_fan_in(value: AgentFanIn, **changes: object) -> AgentFanIn:
    payload = agent_fan_in_to_payload(value)
    payload.update(changes)
    return agent_fan_in_from_payload(payload)


def _same_run_snapshot(previous: AgentRun, current: AgentRun) -> bool:
    """Lifecycle can attach frozen authority refs exactly once, never replace them."""
    old = agent_run_to_payload(previous)
    new = agent_run_to_payload(current)
    for mutable in (
        "status", "terminal_receipt_ref", "model_routing_snapshot_ref",
        "capability_manifest_ref", "context_manifest_ref", "budget_snapshot_ref",
    ):
        old.pop(mutable)
        new.pop(mutable)
    if old != new:
        return False
    for field in (
        "model_routing_snapshot_ref", "capability_manifest_ref",
        "context_manifest_ref", "budget_snapshot_ref", "terminal_receipt_ref",
    ):
        prior = getattr(previous, field)
        next_value = getattr(current, field)
        if prior is not None and prior != next_value:
            return False
    return True
