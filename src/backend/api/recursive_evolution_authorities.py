"""Durable evidence and local-human authority for recursive evolution.

This module is deliberately not an HTTP writer.  Its producers accept only an
already verified external outcome through :class:`VerificationSourcePort`, then
freeze the minimum scoped evidence needed by ``RecursiveEvolutionRuntime``.
Records are insert-only (revision zero CAS); a repeated identity must resolve
to byte-for-byte equivalent authority or fails closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Protocol

from core.recursive_evolution import EvolutionEvaluation, EvolutionProposal
from core.storage_provider import (
    SQLiteStructuredRecordStore,
    SQLiteUnitOfWorkConflict,
    SQLiteUnitOfWorkError,
)

from .recursive_evolution_runtime import CanaryObservation


class RecursiveEvolutionAuthorityError(ValueError):
    """Evidence or confirmation authority is malformed or unavailable."""


class RecursiveEvolutionAuthorityConflict(RecursiveEvolutionAuthorityError):
    """A stable idempotency identity was replayed with different authority."""


class VerificationSourcePort(Protocol):
    """Read one externally verified immutable tool outcome by its CRP ref.

    Implementations are responsible for authenticating the external tool and
    checking its own receipt chain.  This boundary never accepts a raw score,
    canary result, or caller-authored receipt payload.
    """

    def read_verified_outcome(self, *, source_ref: str) -> Mapping[str, object]: ...


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_EVALUATIONS = "recursive_evolution_evaluation_receipts"
_CANARIES = "recursive_evolution_canary_observations"
_CONFIRMATIONS = "recursive_evolution_local_human_confirmations"
_MAX_REFS = 32
_MAX_SAMPLES = 1_000_000
_ACTIONS = frozenset({"start_canary", "promote", "rollback", "reject", "stop"})


@dataclass(frozen=True, slots=True)
class LocalHumanConfirmationRequest:
    """Server-derived fact emitted only after an explicit local UI click."""

    project_id: str
    user_id: str
    command_id: str
    action: str
    proposal_id: str | None
    confirmed_at: str


class RecursiveEvolutionEvidenceAuthority:
    """Append-only Evolution receipts produced from a verified source port."""

    def __init__(self, records: SQLiteStructuredRecordStore, source: VerificationSourcePort) -> None:
        if not isinstance(records, SQLiteStructuredRecordStore) or not callable(getattr(source, "read_verified_outcome", None)):
            raise RecursiveEvolutionAuthorityError("recursive evolution evidence authority is invalid")
        self._records = records
        self._source = source

    def record_evaluation_receipt(
        self, *, project_id: str, proposal: EvolutionProposal, source_ref: str,
    ) -> str:
        project = _identity(project_id, "project id")
        _proposal(project, proposal)
        source = _ref(source_ref, "evaluation source ref")
        outcome = self._outcome(source)
        fields = {"kind", "source_ref", "project_id", "baseline_ref", "baseline_revision", "candidate_ref", "candidate_revision", "evaluation"}
        _exact_keys(outcome, fields, "evaluation outcome")
        if (outcome["kind"] != "recursive_evolution.evaluation.v1" or outcome["source_ref"] != source or outcome["project_id"] != project
                or outcome["baseline_ref"] != proposal.baseline_ref or outcome["baseline_revision"] != proposal.baseline_revision
                or outcome["candidate_ref"] != proposal.candidate_ref or outcome["candidate_revision"] != proposal.candidate_revision):
            raise RecursiveEvolutionAuthorityError("evaluation source scope drifted")
        try:
            evaluation = EvolutionEvaluation.from_payload(outcome["evaluation"])
        except (TypeError, ValueError) as error:
            raise RecursiveEvolutionAuthorityError("verified evaluation outcome is invalid") from error
        _validate_evaluation(project, proposal, evaluation, source)
        receipt_ref = _evaluation_ref(project, evaluation.evaluation_id)
        payload = {"schema_version": "1", "receipt_ref": receipt_ref, "source_ref": source,
                   "project_id": project, "episode_id": proposal.episode_id, "proposal_id": proposal.proposal_id,
                   "baseline_ref": proposal.baseline_ref, "baseline_revision": proposal.baseline_revision,
                   "candidate_ref": proposal.candidate_ref, "candidate_revision": proposal.candidate_revision,
                   "evaluation": evaluation.to_payload()}
        self._append(_EVALUATIONS, evaluation.evaluation_id, payload)
        return receipt_ref

    def record_canary_observation(
        self, *, project_id: str, proposal: EvolutionProposal, source_ref: str,
    ) -> str:
        project = _identity(project_id, "project id")
        _proposal(project, proposal)
        source = _ref(source_ref, "canary source ref")
        outcome = self._outcome(source)
        fields = {"kind", "source_ref", "project_id", "observation_id", "episode_id", "proposal_id", "candidate_ref", "candidate_revision", "passed", "samples", "evidence_refs"}
        _exact_keys(outcome, fields, "canary outcome")
        if outcome["kind"] != "recursive_evolution.canary.v1" or outcome["source_ref"] != source or outcome["project_id"] != project:
            raise RecursiveEvolutionAuthorityError("canary source scope drifted")
        observation_id = _identity(outcome["observation_id"], "observation id")
        if (outcome["episode_id"] != proposal.episode_id or outcome["proposal_id"] != proposal.proposal_id
                or outcome["candidate_ref"] != proposal.candidate_ref or outcome["candidate_revision"] != proposal.candidate_revision):
            raise RecursiveEvolutionAuthorityError("canary proposal scope drifted")
        passed = outcome["passed"]
        samples = outcome["samples"]
        if not isinstance(passed, bool) or type(samples) is not int or not 1 <= samples <= _MAX_SAMPLES:
            raise RecursiveEvolutionAuthorityError("canary outcome is invalid")
        refs = _refs(outcome["evidence_refs"], "canary evidence refs")
        if source not in refs:
            raise RecursiveEvolutionAuthorityError("canary source ref is not evidenced")
        evidence_ref = _canary_ref(project, observation_id)
        payload = {"schema_version": "1", "evidence_ref": evidence_ref, "source_ref": source,
                   "project_id": project, "observation_id": observation_id, "episode_id": proposal.episode_id,
                   "proposal_id": proposal.proposal_id, "candidate_ref": proposal.candidate_ref,
                   "candidate_revision": proposal.candidate_revision, "passed": passed, "samples": samples,
                   "evidence_refs": list(refs)}
        self._append(_CANARIES, observation_id, payload)
        return evidence_ref

    def verify_evaluation(self, *, project_id: str, proposal: EvolutionProposal, receipt_ref: str) -> EvolutionEvaluation:
        project = _identity(project_id, "project id")
        _proposal(project, proposal)
        evaluation_id = _parse_ref(_ref(receipt_ref, "evaluation receipt ref"), _EVALUATIONS, project)
        record = self._read(_EVALUATIONS, evaluation_id)
        _exact_keys(record, {"schema_version", "receipt_ref", "source_ref", "project_id", "episode_id", "proposal_id", "baseline_ref", "baseline_revision", "candidate_ref", "candidate_revision", "evaluation"}, "evaluation receipt")
        if (record.get("schema_version") != "1" or record.get("receipt_ref") != receipt_ref or record.get("project_id") != project
                or record.get("episode_id") != proposal.episode_id or record.get("proposal_id") != proposal.proposal_id
                or record.get("baseline_ref") != proposal.baseline_ref or record.get("baseline_revision") != proposal.baseline_revision
                or record.get("candidate_ref") != proposal.candidate_ref or record.get("candidate_revision") != proposal.candidate_revision):
            raise RecursiveEvolutionAuthorityError("evaluation receipt identity drifted")
        try:
            evaluation = EvolutionEvaluation.from_payload(record["evaluation"])
        except (KeyError, TypeError, ValueError) as error:
            raise RecursiveEvolutionAuthorityError("evaluation receipt is invalid") from error
        _validate_evaluation(project, proposal, evaluation, _ref(record.get("source_ref"), "evaluation source ref"))
        return evaluation

    def verify_canary(self, *, project_id: str, proposal: EvolutionProposal, evidence_ref: str) -> CanaryObservation:
        project = _identity(project_id, "project id")
        _proposal(project, proposal)
        observation_id = _parse_ref(_ref(evidence_ref, "canary evidence ref"), _CANARIES, project)
        record = self._read(_CANARIES, observation_id)
        required = {"schema_version", "evidence_ref", "source_ref", "project_id", "observation_id", "episode_id", "proposal_id", "candidate_ref", "candidate_revision", "passed", "samples", "evidence_refs"}
        _exact_keys(record, required, "canary record")
        if (record["schema_version"] != "1" or record["evidence_ref"] != evidence_ref or record["project_id"] != project or record["observation_id"] != observation_id
                or record["episode_id"] != proposal.episode_id or record["proposal_id"] != proposal.proposal_id
                or record["candidate_ref"] != proposal.candidate_ref or record["candidate_revision"] != proposal.candidate_revision):
            raise RecursiveEvolutionAuthorityError("canary evidence scope drifted")
        if not isinstance(record["passed"], bool) or type(record["samples"]) is not int or not 1 <= record["samples"] <= _MAX_SAMPLES:
            raise RecursiveEvolutionAuthorityError("canary evidence is invalid")
        refs = _refs(record["evidence_refs"], "canary evidence refs")
        source = _ref(record["source_ref"], "canary source ref")
        if source not in refs:
            raise RecursiveEvolutionAuthorityError("canary source ref is not evidenced")
        # The authority Receipt itself is the durable binding consumed by the
        # lifecycle runtime.  Preserve the external tool refs as supporting
        # evidence, but always include this immutable, project-scoped Receipt
        # so callers cannot substitute a raw tool payload for the authority.
        bound_refs = refs if evidence_ref in refs else (*refs, evidence_ref)
        return CanaryObservation(
            proposal.episode_id,
            proposal.proposal_id,
            record["passed"],
            record["samples"],
            bound_refs,
        )

    def _outcome(self, source_ref: str) -> Mapping[str, object]:
        try:
            outcome = self._source.read_verified_outcome(source_ref=source_ref)
        except Exception as error:
            raise RecursiveEvolutionAuthorityError("verified external outcome is unavailable") from error
        if not isinstance(outcome, Mapping):
            raise RecursiveEvolutionAuthorityError("verified external outcome is invalid")
        return dict(outcome)

    def _append(self, collection: str, object_id: str, payload: Mapping[str, object]) -> None:
        try:
            with self._records.begin() as uow:
                uow.put(collection, object_id, payload, expected_revision=0)
                uow.commit()
        except SQLiteUnitOfWorkConflict as error:
            existing = self._read(collection, object_id)
            if _canonical(existing) != _canonical(payload):
                raise RecursiveEvolutionAuthorityConflict("recursive evolution evidence identity drifted") from error
        except SQLiteUnitOfWorkError as error:
            raise RecursiveEvolutionAuthorityError("recursive evolution evidence persistence is unavailable") from error

    def _read(self, collection: str, object_id: str) -> dict[str, object]:
        try:
            record = self._records.read(collection, object_id)
        except SQLiteUnitOfWorkError as error:
            raise RecursiveEvolutionAuthorityError("recursive evolution authority read is unavailable") from error
        if record is None or not isinstance(record.payload, Mapping):
            raise RecursiveEvolutionAuthorityError("recursive evolution authority record is unavailable")
        return dict(record.payload)


class LocalHumanConfirmationAuthority:
    """Append-only local UI confirmations, bound to one immutable command."""

    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        if not isinstance(records, SQLiteStructuredRecordStore):
            raise RecursiveEvolutionAuthorityError("local human confirmation store is invalid")
        self._records = records

    def create(self, request: LocalHumanConfirmationRequest) -> str:
        if not isinstance(request, LocalHumanConfirmationRequest):
            raise RecursiveEvolutionAuthorityError("local human confirmation request is invalid")
        project, user, command = (_identity(request.project_id, "project id"), _human_user(request.user_id), _identity(request.command_id, "command id"))
        action, proposal = _action(request.action, request.proposal_id)
        timestamp = _timestamp(request.confirmed_at)
        ref = _confirmation_ref(project, command)
        payload = {"schema_version": "1", "confirmation_ref": ref, "project_id": project, "user_id": user,
                   "command_id": command, "action": action, "proposal_id": proposal, "confirmed_at": timestamp}
        try:
            with self._records.begin() as uow:
                uow.put(_CONFIRMATIONS, command, payload, expected_revision=0)
                uow.commit()
        except SQLiteUnitOfWorkConflict as error:
            existing = self._read_confirmation(command)
            if _canonical(existing) != _canonical(payload):
                raise RecursiveEvolutionAuthorityConflict("local human command authority drifted") from error
        except SQLiteUnitOfWorkError as error:
            raise RecursiveEvolutionAuthorityError("local human confirmation persistence is unavailable") from error
        return ref

    def create_or_replay(self, request: LocalHumanConfirmationRequest) -> str:
        """Create one explicit UI confirmation or safely reuse its command.

        A retry must not need to reproduce the original trusted timestamp, but
        it must retain the exact project, human, action and proposal binding.
        """
        if not isinstance(request, LocalHumanConfirmationRequest):
            raise RecursiveEvolutionAuthorityError("local human confirmation request is invalid")
        project, user, command = (
            _identity(request.project_id, "project id"),
            _human_user(request.user_id),
            _identity(request.command_id, "command id"),
        )
        action, proposal = _action(request.action, request.proposal_id)
        try:
            stored = self._records.read(_CONFIRMATIONS, command)
        except SQLiteUnitOfWorkError as error:
            raise RecursiveEvolutionAuthorityError(
                "local human confirmation read is unavailable"
            ) from error
        if stored is None:
            return self.create(request)
        if not isinstance(stored.payload, Mapping):
            raise RecursiveEvolutionAuthorityError(
                "local human confirmation is invalid"
            )
        record = dict(stored.payload)
        required = {
            "schema_version", "confirmation_ref", "project_id", "user_id",
            "command_id", "action", "proposal_id", "confirmed_at",
        }
        _exact_keys(record, required, "local human confirmation")
        ref = _confirmation_ref(project, command)
        if (
            record["schema_version"], record["confirmation_ref"],
            record["project_id"], record["user_id"], record["command_id"],
            record["action"], record["proposal_id"],
        ) != ("1", ref, project, user, command, action, proposal):
            raise RecursiveEvolutionAuthorityConflict(
                "local human command authority drifted"
            )
        _timestamp(record["confirmed_at"])
        return ref

    def verify_local_human(self, *, project_id: str, user_id: str, confirmation_ref: str, command_id: str, action: str, proposal_id: str | None) -> None:
        project, user, command = (_identity(project_id, "project id"), _human_user(user_id), _identity(command_id, "command id"))
        action, proposal = _action(action, proposal_id)
        ref = _confirmation_ref(project, command)
        if _ref(confirmation_ref, "confirmation ref") != ref:
            raise RecursiveEvolutionAuthorityConflict("local human confirmation reference drifted")
        record = self._read_confirmation(command)
        required = {"schema_version", "confirmation_ref", "project_id", "user_id", "command_id", "action", "proposal_id", "confirmed_at"}
        _exact_keys(record, required, "local human confirmation")
        if (record["schema_version"], record["confirmation_ref"], record["project_id"], record["user_id"], record["command_id"], record["action"], record["proposal_id"]) != ("1", ref, project, user, command, action, proposal):
            raise RecursiveEvolutionAuthorityConflict("local human confirmation authority drifted")
        _timestamp(record["confirmed_at"])

    def _read_confirmation(self, command_id: str) -> dict[str, object]:
        try:
            record = self._records.read(_CONFIRMATIONS, command_id)
        except SQLiteUnitOfWorkError as error:
            raise RecursiveEvolutionAuthorityError("local human confirmation read is unavailable") from error
        if record is None:
            raise RecursiveEvolutionAuthorityError("local human confirmation is unavailable")
        return dict(record.payload)


class RecursiveEvolutionAuthority(RecursiveEvolutionEvidenceAuthority, LocalHumanConfirmationAuthority):
    """Convenience composition implementing both Runtime verifier ports."""

    def __init__(self, records: SQLiteStructuredRecordStore, source: VerificationSourcePort) -> None:
        RecursiveEvolutionEvidenceAuthority.__init__(self, records, source)
        LocalHumanConfirmationAuthority.__init__(self, records)


# Explicit aliases keep composition roots narrow when they need only one port.
EvolutionEvidenceAuthority = RecursiveEvolutionEvidenceAuthority
EvolutionApprovalAuthority = LocalHumanConfirmationAuthority
RecursiveEvolutionAuthorities = RecursiveEvolutionAuthority


def _validate_evaluation(project: str, proposal: EvolutionProposal, evaluation: EvolutionEvaluation, source_ref: str) -> None:
    if (evaluation.episode_id != proposal.episode_id or evaluation.proposal_id != proposal.proposal_id
            or evaluation.evaluator_id in {proposal.proposer_id, proposal.executor_id}
            or source_ref not in evaluation.evidence_refs):
        raise RecursiveEvolutionAuthorityError("evaluation receipt scope or independence drifted")


def _proposal(project: str, proposal: object) -> EvolutionProposal:
    if not isinstance(proposal, EvolutionProposal) or proposal.episode_id == "" or project == "":
        raise RecursiveEvolutionAuthorityError("evolution proposal is invalid")
    return proposal


def _exact_keys(value: Mapping[str, object], fields: set[str], label: str) -> None:
    if set(value) != fields:
        raise RecursiveEvolutionAuthorityError(f"{label} shape is invalid")


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise RecursiveEvolutionAuthorityError(f"recursive evolution {label} is invalid")
    return value


def _human_user(value: object) -> str:
    user = _identity(value, "user id")
    if user in {"agent", "system"}:
        raise RecursiveEvolutionAuthorityError("recursive evolution local human user is invalid")
    return user


def _ref(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("crp://") or len(value) > 512 or any(char.isspace() for char in value):
        raise RecursiveEvolutionAuthorityError(f"recursive evolution {label} is invalid")
    return value


def _refs(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_REFS:
        raise RecursiveEvolutionAuthorityError(f"recursive evolution {label} are invalid")
    refs = tuple(_ref(item, label) for item in value)
    if len(set(refs)) != len(refs):
        raise RecursiveEvolutionAuthorityError(f"recursive evolution {label} are invalid")
    return refs


def _action(action: object, proposal_id: object) -> tuple[str, str | None]:
    if action not in _ACTIONS:
        raise RecursiveEvolutionAuthorityError("recursive evolution action is invalid")
    if action == "stop":
        if proposal_id is not None:
            raise RecursiveEvolutionAuthorityError("recursive evolution stop proposal is invalid")
        return action, None
    return action, _identity(proposal_id, "proposal id")


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 40:
        raise RecursiveEvolutionAuthorityError("recursive evolution confirmation time is invalid")
    try:
        if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError
    except ValueError as error:
        raise RecursiveEvolutionAuthorityError("recursive evolution confirmation time is invalid") from error
    return value


def _evaluation_ref(project: str, evaluation_id: str) -> str:
    return f"crp://recursive-evolution/evaluations/{project}/{evaluation_id}"


def _canary_ref(project: str, observation_id: str) -> str:
    return f"crp://recursive-evolution/canaries/{project}/{observation_id}"


def _confirmation_ref(project: str, command: str) -> str:
    return f"crp://recursive-evolution/confirmations/{project}/{command}"


def _parse_ref(reference: str, collection: str, project: str) -> str:
    kind = { _EVALUATIONS: "evaluations", _CANARIES: "canaries" }[collection]
    prefix = f"crp://recursive-evolution/{kind}/{project}/"
    if not reference.startswith(prefix):
        raise RecursiveEvolutionAuthorityConflict("recursive evolution authority reference drifted")
    return _identity(reference.removeprefix(prefix), "authority id")


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
