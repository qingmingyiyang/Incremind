"""Production composition for recursive-evolution authorities.

This root keeps the World stream as lifecycle authority.  SQLite records only
hold immutable receipts, registered target material and the policy catalogue;
they are never consulted as a second rollout state machine.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import re

from core.ai_kernel import AgentProfileRegistry, SQLiteAITurnStore, SQLiteAgentStore
from core.ai_kernel.agent_profiles import builtin_agent_profiles
from core.project_skill_core.sqlite_runtime import SQLiteProjectSkillRepository
from core.recursive_evolution.agent_policy import (
    AgentEvolutionPolicy,
    AgentPolicyCatalog,
    AgentPolicyContext,
    AgentPolicyEvaluation,
    AgentPolicyRouting,
    AgentPolicyScheduler,
    VerifiedRolloutEvidence,
)
from core.long_horizon_runtime import TraceSubject, ValidationFact, VersionBinding
from core.recursive_evolution import (
    EvaluationVerdict,
    EvolutionEvaluation,
    EvolutionEvent,
    EvolutionProposal,
    evolution_event_world_identity,
    project_evolution_events,
)
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict

from .agent_runtime_composition import AgentRuntimeComposition
from .personal_world_model_runtime import PersonalWorldModelRuntime
from .project_provenance_runtime import ProjectProvenanceRuntime
from .recursive_evolution_authorities import (
    LocalHumanConfirmationRequest,
    RecursiveEvolutionAuthority,
    RecursiveEvolutionAuthorityError,
)
from .recursive_evolution_runtime import (
    RecordedEvolutionCommand,
    RecursiveEvolutionRuntime,
    RecursiveEvolutionRuntimeError,
)
from .recursive_evolution_targets import PromptStrategyCatalog, RecursiveEvolutionTargetAuthority


class RecursiveEvolutionCompositionError(RuntimeError):
    """Production recursive-evolution dependencies are unavailable or drifted."""


_SOURCE_REF = re.compile(r"^crp://session/([A-Za-z0-9._~-]{1,160})/[A-Za-z0-9._~:/-]+$")
_EVIDENCE_COLLECTION = "recursive_evolution_policy_rollout_evidence"
_CANDIDATES = "recursive_evolution_candidates"
_EVOLUTION_CAPABILITIES = frozenset({
    "recursive_evolution.candidate",
    "recursive_evolution.evaluation",
    "recursive_evolution.evaluate",
    "recursive_evolution.canary",
})


class SQLiteAITurnVerifiedSource:
    """Read evolution evidence only from a recorded, successful Tool outcome."""

    def __init__(self, store: SQLiteAITurnStore, *, capabilities: Sequence[str] = tuple(_EVOLUTION_CAPABILITIES)) -> None:
        if not isinstance(store, SQLiteAITurnStore):
            raise RecursiveEvolutionCompositionError("AI Turn source store is invalid")
        allowed = frozenset(capabilities)
        if not allowed or not allowed.issubset(_EVOLUTION_CAPABILITIES):
            raise RecursiveEvolutionCompositionError("evolution capability allowlist is invalid")
        self._store = store
        self._capabilities = allowed

    def read_verified_outcome(self, *, source_ref: str) -> Mapping[str, object]:
        match = _SOURCE_REF.fullmatch(source_ref) if isinstance(source_ref, str) else None
        if match is None:
            raise RecursiveEvolutionAuthorityError("verified external outcome is invalid")
        turn_id = match.group(1)
        try:
            payload = self._store.get(source_ref)
            events = tuple(self._store.events_after(turn_id))
        except Exception as error:
            raise RecursiveEvolutionAuthorityError("verified external outcome is unavailable") from error
        if not isinstance(payload, Mapping) or "source_ref" in payload:
            raise RecursiveEvolutionAuthorityError("verified external outcome is invalid")
        outcomes = [event for event in events if self._matches_outcome(event, source_ref)]
        if len(outcomes) != 1:
            raise RecursiveEvolutionAuthorityError("verified external outcome is not recorded")
        outcome = outcomes[0]
        data, correlation = outcome.get("data"), outcome.get("correlation")
        assert isinstance(data, Mapping) and isinstance(correlation, Mapping)
        capability = data.get("capability_id")
        call_id = correlation.get("tool_call_id")
        outcome_sequence = outcome.get("sequence")
        if (
            capability not in self._capabilities
            or not isinstance(call_id, str)
            or not isinstance(outcome_sequence, int)
        ):
            raise RecursiveEvolutionAuthorityError("verified external outcome capability is invalid")
        kind = payload.get("kind")
        expected_capabilities = {
            "recursive_evolution.candidate.v1": {"recursive_evolution.candidate"},
            "recursive_evolution.evaluation.v1": {
                "recursive_evolution.evaluation", "recursive_evolution.evaluate",
            },
            "recursive_evolution.canary.v1": {"recursive_evolution.canary"},
        }.get(kind)
        if expected_capabilities is None or capability not in expected_capabilities:
            raise RecursiveEvolutionAuthorityError(
                "verified external outcome capability is invalid"
            )
        terminals = [
            event for event in events
            if event.get("type") == "tool.completed"
            and isinstance(event.get("data"), Mapping)
            and isinstance(event.get("correlation"), Mapping)
            and event["data"].get("capability_id") == capability
            and event["data"].get("status") == "completed"
            and event["correlation"].get("tool_call_id") == call_id
            and isinstance(event.get("sequence"), int)
            and event["sequence"] > outcome_sequence
        ]
        turn_successes = [
            event for event in events
            if event.get("type") == "turn.completed"
            and isinstance(event.get("data"), Mapping)
            and event["data"].get("status") == "completed"
            and isinstance(event.get("sequence"), int)
            and terminals
            and event["sequence"] > terminals[0].get("sequence", -1)
        ]
        if len(terminals) != 1 or len(turn_successes) != 1:
            raise RecursiveEvolutionAuthorityError("verified external outcome is not terminal success")
        # SQLite allocates this payload reference while writing it, so a Tool
        # payload cannot safely self-reference it. Bind the verified ref only
        # after the exact persisted event chain has been checked. Evidence
        # lists receive the same authority-owned binding for exact verification.
        bound = {**dict(payload), "source_ref": source_ref}
        if kind == "recursive_evolution.evaluation.v1":
            evaluation = bound.get("evaluation")
            if not isinstance(evaluation, Mapping):
                raise RecursiveEvolutionAuthorityError(
                    "verified external outcome is invalid"
                )
            refs = evaluation.get("evidence_refs")
            if not isinstance(refs, list):
                raise RecursiveEvolutionAuthorityError(
                    "verified external outcome is invalid"
                )
            bound["evaluation"] = {
                **dict(evaluation),
                "evidence_refs": list(dict.fromkeys((*refs, source_ref))),
            }
        elif kind == "recursive_evolution.canary.v1":
            refs = bound.get("evidence_refs")
            if not isinstance(refs, list):
                raise RecursiveEvolutionAuthorityError(
                    "verified external outcome is invalid"
                )
            bound["evidence_refs"] = list(dict.fromkeys((*refs, source_ref)))
        return bound

    @staticmethod
    def _matches_outcome(event: Mapping[str, object], source_ref: str) -> bool:
        data = event.get("data")
        return (
            event.get("type") == "tool.outcome.recorded"
            and isinstance(data, Mapping)
            and data.get("payload_ref") == source_ref
        )


class WorldPolicyRolloutEvidenceBridge:
    """Persist and resolve policy evidence derived exclusively from World facts."""

    def __init__(self, *, world: PersonalWorldModelRuntime, records: SQLiteStructuredRecordStore, confirmations: RecursiveEvolutionAuthority) -> None:
        self._world = world
        self._records = records
        self._confirmations = confirmations

    def __call__(self, *, action: str, proposal, authorization_ref: str) -> str:
        if action not in {"start_canary", "promote"} or not isinstance(authorization_ref, str):
            raise RecursiveEvolutionCompositionError("policy rollout evidence request is invalid")
        project_id, candidate = self._candidate(proposal)
        self._verify_confirmation(project_id, authorization_ref, action, proposal.proposal_id)
        policy = AgentEvolutionPolicy.from_payload(candidate["policy"])
        events = tuple(
            EvolutionEvent.from_payload(event.payload)
            for event in self._world.events(project_id)
            if event.kind.value == "evolution.event.recorded"
        )
        projection = project_evolution_events(events, project_id=project_id)
        if projection is None:
            raise RecursiveEvolutionCompositionError("World evolution evidence is unavailable")
        status = projection.proposal_statuses.get(proposal.proposal_id)
        if action == "start_canary":
            qualified, reviewed, samples = status == "qualified", status == "qualified", 0
        else:
            qualified, reviewed = status == "canary_passed", status == "canary_passed"
            samples = self._passed_samples(project_id, proposal.episode_id, proposal.proposal_id)
        if not qualified or not reviewed:
            raise RecursiveEvolutionCompositionError("World rollout evidence is not qualified")
        ref = f"crp://recursive-evolution/policy-rollouts/{project_id}/{proposal.proposal_id}/{action}"
        evidence = VerifiedRolloutEvidence(ref, policy.policy_id, policy.revision, qualified, reviewed, samples)
        payload = {
            "evidence_ref": evidence.evidence_ref, "policy_id": evidence.policy_id,
            "revision": evidence.revision, "qualified": evidence.qualified,
            "human_reviewed": evidence.human_reviewed, "completed_turns": evidence.completed_turns,
        }
        try:
            with self._records.begin() as unit:
                existing = unit.read(_EVIDENCE_COLLECTION, ref)
                if existing is not None:
                    if dict(existing.payload) != payload:
                        raise RecursiveEvolutionCompositionError("policy rollout evidence drifted")
                    unit.rollback()
                else:
                    unit.put(_EVIDENCE_COLLECTION, ref, payload, expected_revision=0)
                    unit.commit()
        except SQLiteUnitOfWorkConflict as error:
            raise RecursiveEvolutionCompositionError("policy rollout evidence persistence conflicted") from error
        return ref

    def resolve(self, evidence_ref: str) -> VerifiedRolloutEvidence | None:
        record = self._records.read(_EVIDENCE_COLLECTION, evidence_ref)
        if record is None or not isinstance(record.payload, Mapping):
            return None
        payload = record.payload
        try:
            if set(payload) != {"evidence_ref", "policy_id", "revision", "qualified", "human_reviewed", "completed_turns"}:
                return None
            evidence = VerifiedRolloutEvidence(**dict(payload))
        except (TypeError, ValueError):
            return None
        return evidence if evidence.evidence_ref == evidence_ref else None

    def _candidate(self, proposal) -> tuple[str, Mapping[str, object]]:
        record = self._records.read(_CANDIDATES, proposal.proposal_id)
        if record is None or not isinstance(record.payload, Mapping):
            raise RecursiveEvolutionCompositionError("registered policy candidate is unavailable")
        payload = record.payload
        project_id = payload.get("project_id")
        if not isinstance(project_id, str) or not project_id or payload.get("proposal") != proposal.to_payload():
            raise RecursiveEvolutionCompositionError("registered policy candidate drifted")
        candidate = payload.get("candidate")
        if not isinstance(candidate, Mapping) or set(candidate) != {"policy", "baseline_ref", "baseline_revision", "candidate_ref", "candidate_revision"}:
            raise RecursiveEvolutionCompositionError("registered policy candidate is invalid")
        return project_id, candidate

    def _passed_samples(self, project_id: str, episode_id: str, proposal_id: str) -> int:
        samples = []
        for world_event in self._world.events(project_id):
            if world_event.kind.value != "evolution.event.recorded":
                continue
            event = EvolutionEvent.from_payload(world_event.payload)
            payload = event.payload
            if event.kind.value == "canary.observed" and event.episode_id == episode_id and payload.get("proposal_id") == proposal_id and payload.get("passed") is True:
                value = payload.get("samples")
                if type(value) is int and value > 0:
                    samples.append(value)
        if not samples:
            raise RecursiveEvolutionCompositionError("World canary success evidence is unavailable")
        return sum(samples)

    def _verify_confirmation(self, project_id: str, confirmation_ref: str, action: str, proposal_id: str) -> None:
        prefix = f"crp://recursive-evolution/confirmations/{project_id}/"
        if not isinstance(confirmation_ref, str) or not confirmation_ref.startswith(prefix):
            raise RecursiveEvolutionCompositionError("local human confirmation is unavailable")
        command_id = confirmation_ref.removeprefix(prefix)
        record = self._records.read("recursive_evolution_local_human_confirmations", command_id)
        if record is None or not isinstance(record.payload, Mapping):
            raise RecursiveEvolutionCompositionError("local human confirmation is unavailable")
        user_id = record.payload.get("user_id")
        try:
            self._confirmations.verify_local_human(
                project_id=project_id, user_id=user_id, confirmation_ref=confirmation_ref,
                command_id=command_id, action=action, proposal_id=proposal_id,
            )
        except Exception as error:
            raise RecursiveEvolutionCompositionError("local human confirmation is invalid") from error


class RecursiveEvolutionLocalActionService:
    """Translate one explicit local UI action into governed runtime inputs.

    The renderer supplies only an idempotency command.  This adapter owns the
    local-human fact and derives supporting evidence from the immutable World
    lifecycle stream, so an untrusted UI cannot manufacture confirmation or
    evidence references.
    """

    _ACTIONS = frozenset({"start_canary", "promote", "rollback", "reject", "stop"})
    _LOCAL_USER = "local-user"

    def __init__(
        self,
        *,
        runtime: RecursiveEvolutionRuntime,
        confirmations: RecursiveEvolutionAuthority,
        world: PersonalWorldModelRuntime,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._runtime = runtime
        self._confirmations = confirmations
        self._world = world
        self._now = now or (lambda: datetime.now(UTC))

    def execute(
        self,
        *,
        project_id: str,
        episode_id: str,
        proposal_id: str | None,
        action: str,
        command_id: str,
    ) -> RecordedEvolutionCommand:
        if action not in self._ACTIONS:
            raise RecursiveEvolutionRuntimeError("local evolution action is invalid")
        if (action == "stop") != (proposal_id is None):
            raise RecursiveEvolutionRuntimeError("local evolution action scope is invalid")
        projection = self._runtime.get_projection(
            project_id=project_id, episode_id=episode_id,
        )
        if projection is None:
            raise RecursiveEvolutionRuntimeError("evolution episode is unavailable")
        evidence_refs = self._evidence_refs(
            project_id=project_id,
            episode_id=episode_id,
            proposal_id=proposal_id,
            action=action,
            command_id=command_id,
        )
        confirmation_ref = self._confirmations.create_or_replay(
            LocalHumanConfirmationRequest(
                project_id=project_id,
                user_id=self._LOCAL_USER,
                command_id=command_id,
                action=action,
                proposal_id=proposal_id,
                confirmed_at=self._timestamp(),
            )
        )
        common = {
            "project_id": project_id,
            "episode_id": episode_id,
            "user_id": self._LOCAL_USER,
            "confirmation_ref": confirmation_ref,
            "evidence_refs": evidence_refs,
            "command_id": command_id,
        }
        if action == "start_canary":
            return self._runtime.approve_canary(proposal_id=proposal_id, **common)
        if action == "stop":
            return self._runtime.stop(reason="user", **common)
        return getattr(self._runtime, action)(proposal_id=proposal_id, **common)

    def _evidence_refs(
        self,
        *,
        project_id: str,
        episode_id: str,
        proposal_id: str | None,
        action: str,
        command_id: str,
    ) -> tuple[str, ...]:
        events = self._episode_events(project_id, episode_id)
        if action == "stop":
            refs = tuple(
                evolution_event_world_identity(event)[1]
                for event in events
                if event.event_id != f"{episode_id}.{command_id}.stopped"
            )
            return self._required_refs(refs)
        assert proposal_id is not None
        reviews = tuple(
            ref
            for event in events
            if event.kind.value == "review.recorded"
            and event.payload.get("proposal_id") == proposal_id
            for ref in self._safe_refs(event.payload.get("evidence_refs"))
        )
        if action in {"start_canary", "reject"}:
            return self._required_refs(reviews)
        canaries = tuple(
            ref
            for event in events
            if event.kind.value == "canary.observed"
            and event.payload.get("proposal_id") == proposal_id
            and event.payload.get("passed") is True
            for ref in self._safe_refs(event.payload.get("evidence_refs"))
        )
        if action == "promote":
            return self._required_refs((*reviews, *canaries))
        prior_decisions = tuple(
            ref
            for event in events
            if event.kind.value in {"canary.started", "rollout.promoted"}
            and event.payload.get("proposal_id") == proposal_id
            for ref in self._safe_refs(event.payload.get("evidence_refs"))
        )
        return self._required_refs((*reviews, *canaries, *prior_decisions))

    def _episode_events(self, project_id: str, episode_id: str) -> tuple[EvolutionEvent, ...]:
        values: list[EvolutionEvent] = []
        try:
            world_events = self._world.events(project_id)
            for world_event in world_events:
                if world_event.kind.value != "evolution.event.recorded":
                    continue
                event = EvolutionEvent.from_payload(world_event.payload)
                if event.episode_id == episode_id:
                    values.append(event)
        except Exception as error:
            raise RecursiveEvolutionRuntimeError("evolution lifecycle evidence is unavailable") from error
        if not values:
            raise RecursiveEvolutionRuntimeError("evolution lifecycle evidence is unavailable")
        return tuple(values)

    @staticmethod
    def _safe_refs(value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(
            ref for ref in value
            if isinstance(ref, str)
            and ref.startswith("crp://")
            and not ref.startswith("crp://recursive-evolution/target-operations/")
        )

    @staticmethod
    def _required_refs(values: Sequence[str]) -> tuple[str, ...]:
        refs = tuple(dict.fromkeys(values))
        if not refs:
            raise RecursiveEvolutionRuntimeError("evolution lifecycle evidence is unavailable")
        return refs

    def _timestamp(self) -> str:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise RecursiveEvolutionRuntimeError("trusted local confirmation clock is invalid")
        return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class VerifiedRecursiveEvolutionWorkflow:
    """Non-renderer workflow from verified terminal Tool payloads only."""

    source: SQLiteAITurnVerifiedSource
    authority: RecursiveEvolutionAuthority
    targets: RecursiveEvolutionTargetAuthority
    runtime: RecursiveEvolutionRuntime
    world: PersonalWorldModelRuntime

    def record_candidate(self, *, source_ref: str) -> RecordedEvolutionCommand:
        outcome = self.source.read_verified_outcome(source_ref=source_ref)
        _workflow_envelope(outcome, {"kind", "source_ref", "project_id", "proposal", "candidate"}, "recursive_evolution.candidate.v1")
        proposal = EvolutionProposal.from_payload(outcome["proposal"])
        project = _workflow_project(outcome)
        if not isinstance(outcome["candidate"], Mapping):
            raise RecursiveEvolutionCompositionError("verified candidate identity is invalid")
        self.targets.register_candidate(project_id=project, proposal=proposal, candidate=outcome["candidate"])
        return self.runtime.record_candidate(project_id=project, proposal=proposal, command_id=proposal.proposal_id)

    def record_evaluation(self, *, source_ref: str) -> RecordedEvolutionCommand:
        outcome = self.source.read_verified_outcome(source_ref=source_ref)
        _workflow_envelope(outcome, {"kind", "source_ref", "project_id", "baseline_ref", "baseline_revision", "candidate_ref", "candidate_revision", "evaluation"}, "recursive_evolution.evaluation.v1")
        project = _workflow_project(outcome)
        evaluation = EvolutionEvaluation.from_payload(outcome["evaluation"])
        proposal = _workflow_proposal(self.world, project, evaluation.episode_id, evaluation.proposal_id)
        receipt = self.authority.record_evaluation_receipt(project_id=project, proposal=proposal, source_ref=source_ref)
        return self.runtime.record_evaluation_from_receipt(project_id=project, episode_id=proposal.episode_id, proposal_id=proposal.proposal_id, receipt_ref=receipt, command_id=evaluation.evaluation_id)

    def observe_canary(self, *, source_ref: str) -> RecordedEvolutionCommand:
        outcome = self.source.read_verified_outcome(source_ref=source_ref)
        _workflow_envelope(outcome, {"kind", "source_ref", "project_id", "observation_id", "episode_id", "proposal_id", "candidate_ref", "candidate_revision", "passed", "samples", "evidence_refs"}, "recursive_evolution.canary.v1")
        project = _workflow_project(outcome)
        proposal = _workflow_proposal(self.world, project, outcome["episode_id"], outcome["proposal_id"])
        command = outcome["observation_id"]
        if not isinstance(command, str) or not command:
            raise RecursiveEvolutionCompositionError("verified canary identity is invalid")
        evidence = self.authority.record_canary_observation(project_id=project, proposal=proposal, source_ref=source_ref)
        return self.runtime.observe_canary(project_id=project, episode_id=proposal.episode_id, proposal_id=proposal.proposal_id, evidence_ref=evidence, command_id=command)


def _workflow_envelope(value: Mapping[str, object], fields: set[str], kind: str) -> None:
    if set(value) != fields or value.get("kind") != kind:
        raise RecursiveEvolutionCompositionError("verified evolution workflow envelope is invalid")


def _workflow_project(value: Mapping[str, object]) -> str:
    project = value.get("project_id")
    if not isinstance(project, str) or not project:
        raise RecursiveEvolutionCompositionError("verified evolution workflow identity is invalid")
    return project


def _workflow_proposal(
    world: PersonalWorldModelRuntime,
    project_id: str,
    episode_id: object,
    proposal_id: object,
) -> EvolutionProposal:
    if not isinstance(episode_id, str) or not isinstance(proposal_id, str):
        raise RecursiveEvolutionCompositionError("verified evolution workflow scope is invalid")
    matches = []
    for world_event in world.events(project_id):
        if world_event.kind.value != "evolution.event.recorded":
            continue
        event = EvolutionEvent.from_payload(world_event.payload)
        if event.kind.value != "proposal.recorded":
            continue
        proposal = EvolutionProposal.from_payload(event.payload)
        if proposal.episode_id == episode_id and proposal.proposal_id == proposal_id:
            matches.append(proposal)
    if len(matches) != 1:
        raise RecursiveEvolutionCompositionError("verified evolution workflow proposal is unavailable")
    return matches[0]


@dataclass(slots=True)
class RecursiveEvolutionComposition:
    runtime: RecursiveEvolutionRuntime
    authority: RecursiveEvolutionAuthority
    targets: RecursiveEvolutionTargetAuthority
    policy: AgentPolicyCatalog
    provenance: ProjectProvenanceRuntime
    world: PersonalWorldModelRuntime
    records: SQLiteStructuredRecordStore
    turns: SQLiteAITurnStore
    local_actions: RecursiveEvolutionLocalActionService

    @property
    def verified_workflow(self) -> "VerifiedRecursiveEvolutionWorkflow":
        """Internal adapter; deliberately not registered as an API route."""
        return VerifiedRecursiveEvolutionWorkflow(
            SQLiteAITurnVerifiedSource(self.turns), self.authority,
            self.targets, self.runtime, self.world,
        )

    def recover(self, project_ids: Sequence[str]) -> None:
        for project_id in dict.fromkeys(project_ids):
            self.runtime.recover_pending_rollbacks(project_id=project_id)
            self.reconcile_provenance(project_id)

    def reconcile_provenance(self, project_id: str) -> None:
        """Project verified candidate outcomes into the shared World provenance.

        The evolution stream and target Receipts remain the restart checkpoint.
        This method is an idempotent read-model repair, so a crash between the
        lifecycle append and provenance projection cannot create a second
        lifecycle authority or lose an already verified result.
        """

        proposals: dict[str, EvolutionProposal] = {}
        for world_event in self.world.events(project_id):
            if world_event.kind.value != "evolution.event.recorded":
                continue
            event = EvolutionEvent.from_payload(world_event.payload)
            if event.kind.value == "proposal.recorded":
                proposal = EvolutionProposal.from_payload(event.payload)
                operation_id = (
                    f"evolution.{project_id}.{proposal.episode_id}."
                    f"{proposal.proposal_id}.record_candidate"
                )
                if self.targets.probe(operation_id=operation_id) is None:
                    continue
                proposals[proposal.proposal_id] = proposal
                subject, version = (
                    _proposal_subject(project_id, proposal),
                    _proposal_version(proposal),
                )
                trace = self.provenance.project(project_id)
                prior = next(
                    (
                        item for item in trace.subjects
                        if item.subject == subject
                        and item.version.revision == version.revision
                    ),
                    None,
                )
                if prior is None:
                    self.provenance.record_subject(
                        subject=subject,
                        version=version,
                        recorded_at=self._next_world_recorded_at(project_id),
                    )
                elif prior.version != version:
                    raise RecursiveEvolutionCompositionError(
                        "evolution provenance subject drifted"
                    )
                continue
            if event.kind.value == "evaluation.recorded":
                evaluation = EvolutionEvaluation.from_payload(event.payload)
                proposal = proposals.get(evaluation.proposal_id)
                if proposal is None:
                    # A structurally valid World event without the target
                    # operation Receipt is historical/unverified input, not a
                    # provenance validation source.
                    continue
                receipt_ref = (
                    f"crp://recursive-evolution/evaluations/{project_id}/"
                    f"{evaluation.evaluation_id}"
                )
                if self.records.read(
                    "recursive_evolution_evaluation_receipts",
                    evaluation.evaluation_id,
                ) is None:
                    continue
                try:
                    verified = self.authority.verify_evaluation(
                        project_id=project_id,
                        proposal=proposal,
                        receipt_ref=receipt_ref,
                    )
                except Exception as error:
                    raise RecursiveEvolutionCompositionError(
                        "evolution provenance evaluation receipt is invalid"
                    ) from error
                if verified != evaluation:
                    raise RecursiveEvolutionCompositionError(
                        "evolution provenance evaluation drifted"
                    )
                verdict = {
                    EvaluationVerdict.QUALIFIED: "verified",
                    EvaluationVerdict.UNQUALIFIED: "rejected",
                    EvaluationVerdict.INCONCLUSIVE: "inconclusive",
                    EvaluationVerdict.INVALIDATED: "rejected",
                }[evaluation.verdict]
                validation = ValidationFact(
                        project_id=project_id,
                        validation_id=evaluation.evaluation_id,
                        subject=_proposal_subject(project_id, proposal),
                        subject_version=_proposal_version(proposal),
                        verdict=verdict,
                        validator_kind="recursive_evolution_evaluator",
                        validator_revision=evaluation.metric_set_revision,
                        evidence_refs=(receipt_ref,),
                    )
                self._record_validation_if_missing(validation)
                continue
            if event.kind.value == "canary.observed":
                proposal_id = event.payload.get("proposal_id")
                proposal = proposals.get(str(proposal_id))
                refs = tuple(event.payload.get("evidence_refs", ()))
                authority_prefix = (
                    f"crp://recursive-evolution/canaries/{project_id}/"
                )
                authority_refs = tuple(
                    ref for ref in refs
                    if isinstance(ref, str) and ref.startswith(authority_prefix)
                )
                # Older facts without the authority Receipt remain visible in
                # the lifecycle stream, but cannot be upgraded into verified
                # provenance merely by this projector.
                if proposal is None or len(authority_refs) != 1:
                    continue
                observation_id = authority_refs[0].rsplit("/", 1)[-1]
                if self.records.read(
                    "recursive_evolution_canary_observations", observation_id,
                ) is None:
                    continue
                try:
                    verified_canary = self.authority.verify_canary(
                        project_id=project_id,
                        proposal=proposal,
                        evidence_ref=authority_refs[0],
                    )
                except Exception as error:
                    raise RecursiveEvolutionCompositionError(
                        "evolution provenance canary receipt is invalid"
                    ) from error
                if (
                    verified_canary.passed is not event.payload.get("passed")
                    or verified_canary.samples != event.payload.get("samples")
                    or tuple(verified_canary.evidence_refs) != refs
                ):
                    raise RecursiveEvolutionCompositionError(
                        "evolution provenance canary drifted"
                    )
                evidence = (
                    authority_refs[0],
                    *tuple(ref for ref in refs if ref != authority_refs[0])[:7],
                )
                validation = ValidationFact(
                        project_id=project_id,
                        validation_id=event.event_id,
                        subject=_proposal_subject(project_id, proposal),
                        subject_version=_proposal_version(proposal),
                        verdict="verified" if event.payload.get("passed") is True else "rejected",
                        validator_kind="recursive_evolution_canary",
                        validator_revision="v1",
                        evidence_refs=evidence,
                    )
                self._record_validation_if_missing(validation)

    def _record_validation_if_missing(self, validation: ValidationFact) -> None:
        trace = self.provenance.project(validation.project_id)
        prior = next(
            (
                item for item in trace.validations
                if item.validation_id == validation.validation_id
            ),
            None,
        )
        if prior is None:
            self.provenance.record_validation(
                validation=validation,
                recorded_at=self._next_world_recorded_at(validation.project_id),
            )
        elif prior != validation:
            raise RecursiveEvolutionCompositionError(
                "evolution provenance validation drifted"
            )

    def _next_world_recorded_at(self, project_id: str) -> str:
        events = self.world.events(project_id)
        if not events:
            raise RecursiveEvolutionCompositionError(
                "evolution provenance World stream is unavailable"
            )
        latest = datetime.fromisoformat(
            events[-1].recorded_at.replace("Z", "+00:00")
        ).astimezone(UTC)
        return (latest + timedelta(microseconds=1)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class RecursiveEvolutionPolicyAuthorityComposition:
    records: SQLiteStructuredRecordStore
    authority: RecursiveEvolutionAuthority
    bridge: WorldPolicyRolloutEvidenceBridge
    policy: AgentPolicyCatalog
    world: PersonalWorldModelRuntime
    turns: SQLiteAITurnStore
    profiles: AgentProfileRegistry


def build_recursive_evolution_policy_authority(*, runtime_root: Path, world: PersonalWorldModelRuntime, turns: SQLiteAITurnStore, profiles: AgentProfileRegistry) -> RecursiveEvolutionPolicyAuthorityComposition:
    """First stage: migrate the existing Workbench policy ceiling into SQLite."""
    root = Path(runtime_root).expanduser().resolve(strict=False)
    if not isinstance(turns, SQLiteAITurnStore) or not isinstance(profiles, AgentProfileRegistry):
        raise RecursiveEvolutionCompositionError("recursive evolution production dependencies are invalid")
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "recursive-evolution.sqlite3")
    authority = RecursiveEvolutionAuthority(records, SQLiteAITurnVerifiedSource(turns))
    bridge = WorldPolicyRolloutEvidenceBridge(world=world, records=records, confirmations=authority)
    profile_ids = tuple(profile.profile_id for profile in builtin_agent_profiles())
    baseline = AgentEvolutionPolicy(
        # Existing Workbench upper bounds, frozen as the migration baseline;
        # this adds no profiles, experts, skills or execution authority.
        policy_id="workbench.default", revision=1, status="candidate", parent_revision=None,
        target_roles=("main", "steward", "subagent"), routing=AgentPolicyRouting(profile_ids),
        scheduler=AgentPolicyScheduler(
            "expert_cluster", 3, 3, False,
            ("video-research-expert",),
            ("media-comprehension", "knowledge-intake"),
        ),
        context=AgentPolicyContext(True, True, True, 262_144),
        evaluation=AgentPolicyEvaluation(100, 1, True),
    )
    return RecursiveEvolutionPolicyAuthorityComposition(
        records, authority, bridge,
        AgentPolicyCatalog(
            records,
            profile_ids=profile_ids,
            trusted_baseline=baseline,
            rollout_evidence=bridge,
            expert_ids=("video-research-expert",),
            skill_ids=("media-comprehension", "knowledge-intake"),
        ),
        world,
        turns,
        profiles,
    )


def build_recursive_evolution_composition(
    *, runtime_root: Path, world: PersonalWorldModelRuntime | None = None,
    turns: SQLiteAITurnStore | None = None, profiles: AgentProfileRegistry | None = None,
    project_skills: object | None = None, agent_runtime: AgentRuntimeComposition | None = None,
    policy_authority: RecursiveEvolutionPolicyAuthorityComposition | None = None,
    testing: bool = False,
) -> RecursiveEvolutionComposition:
    """Construct the production recursive-evolution graph on one runtime root."""
    root = Path(runtime_root).expanduser().resolve(strict=False)
    supplied_turns = turns or (SQLiteAITurnStore(root / ".rebuild-data" / "ai-turns.sqlite3") if testing else None)
    supplied_world = world or (PersonalWorldModelRuntime.for_root(root) if testing else None)
    supplied_profiles = profiles or (
        agent_runtime.profiles
        if agent_runtime is not None
        else (AgentProfileRegistry(SQLiteAgentStore(root / ".rebuild-data" / "ai-turns.sqlite3")) if testing else None)
    )
    if supplied_turns is None or supplied_world is None or supplied_profiles is None:
        raise RecursiveEvolutionCompositionError("production composition requires injected World, Turn and Profile authorities")
    staged = policy_authority or build_recursive_evolution_policy_authority(
        runtime_root=root,
        world=supplied_world,
        turns=supplied_turns,
        profiles=supplied_profiles,
    )
    if (
        not isinstance(staged, RecursiveEvolutionPolicyAuthorityComposition)
        or staged.world is not supplied_world
        or staged.turns is not supplied_turns
        or staged.profiles is not supplied_profiles
    ):
        raise RecursiveEvolutionCompositionError(
            "recursive evolution policy authority crossed production authority identity"
        )
    skills = project_skills if project_skills is not None else (SQLiteProjectSkillRepository(staged.records) if testing else None)
    if skills is None:
        raise RecursiveEvolutionCompositionError("production composition requires the active Project Skill repository")
    prompts = PromptStrategyCatalog(staged.records)
    targets = RecursiveEvolutionTargetAuthority(
        records=staged.records, scheduler_policies=staged.policy, agent_profiles=supplied_profiles,
        project_skills=skills, prompt_strategies=prompts, policy_rollout_evidence=staged.bridge,
    )
    runtime = RecursiveEvolutionRuntime(world=supplied_world, evidence=staged.authority, approvals=staged.authority, targets=targets)
    return RecursiveEvolutionComposition(
        runtime, staged.authority, targets, staged.policy,
        ProjectProvenanceRuntime(world=supplied_world), supplied_world,
        staged.records, supplied_turns,
        RecursiveEvolutionLocalActionService(
            runtime=runtime, confirmations=staged.authority, world=supplied_world,
        ),
    )


def _proposal_subject(project_id: str, proposal: EvolutionProposal) -> TraceSubject:
    return TraceSubject(project_id, "config", proposal.proposal_id)


def _proposal_version(proposal: EvolutionProposal) -> VersionBinding:
    return VersionBinding(
        authority_ref=proposal.candidate_ref,
        revision=proposal.candidate_revision,
        content_fingerprint=None,
    )
