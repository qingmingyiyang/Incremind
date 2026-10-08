"""Native target adapter for governed recursive-evolution rollouts.

The World Event stream remains the evolution lifecycle authority.  This module
only holds opaque candidate material and idempotency receipts in SQLite, then
uses the owning authority to make a guarded change.  It deliberately has no
fallback that treats its SQLite records as an active configuration.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Protocol

from core.ai_kernel.agent_contracts import AgentProfile, agent_profile_from_payload, agent_profile_to_payload
from core.recursive_evolution import EvolutionProposal, EvolutionTargetKind
from core.recursive_evolution.agent_policy import AgentEvolutionPolicy, AgentPolicyCatalog
from core.storage_provider import (
    SQLiteStructuredRecordStore,
    SQLiteUnitOfWorkConflict,
    SQLiteUnitOfWorkError,
)


_CANDIDATES = "recursive_evolution_candidates"
_RECEIPTS = "recursive_evolution_target_receipts"
_PROMPT_REVISIONS = "recursive_evolution_prompt_strategy_revisions"
_PROMPT_HEADS = "recursive_evolution_prompt_strategy_heads"
_PROMPT_COMMANDS = "recursive_evolution_prompt_strategy_commands"
_TARGET_RECEIPT_PREFIX = "crp://recursive-evolution/target-operations/"
_PROMPT_STRATEGY_ID = "default.safe"
_PROMPT_BASELINE_REF = "crp://recursive-evolution/prompt-strategies/default.safe/active"
_PROMPT_TEMPLATES = frozenset({"safe-template", "concise-template", "review-template"})
_PROMPT_PARAMETERS = frozenset({"include_project_skill", "include_memory", "include_session_history"})
_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._~-")
_FORBIDDEN = frozenset({"prompt", "instruction", "system_prompt", "path", "file", "secret", "token", "key", "api_key", "password", "endpoint", "provider", "model"})


class RecursiveEvolutionTargetError(ValueError):
    """A target change cannot be made without widening its authority."""


class CandidateResolverPort(Protocol):
    """Resolve structured candidate material; never resolve files or prompts."""

    def __call__(self, *, project_id: str, proposal: EvolutionProposal) -> Mapping[str, object]: ...


class PolicyRolloutEvidenceRefResolver(Protocol):
    """Resolve trusted rollout evidence; confirmations are not policy evidence."""

    def __call__(self, *, action: str, proposal: EvolutionProposal, authorization_ref: str) -> str: ...


class PromptStrategyAuthorityPort(Protocol):
    """Optional native prompt-strategy authority.

    This is intentionally a narrow port: raw prompt text and free-form
    instructions are not part of this adapter's contract.
    """

    def preflight(self, *, project_id: str, action: str, candidate: Mapping[str, object]) -> None: ...
    def apply(self, *, operation_id: str, action: str, candidate: Mapping[str, object], authorization_ref: str) -> None: ...
    def probe(self, *, operation_id: str) -> str | None: ...


class PromptStrategyCatalog:
    """Small native authority for structured prompt-strategy selection only.

    It stores template identity plus boolean switches.  It cannot store prompt
    text, provider/model routing, locators or arbitrary parameter values.
    """

    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        self._records = records
        self._bootstrap()

    def probe(self, *, operation_id: str) -> str | None:
        record = self._records.read(_PROMPT_COMMANDS, operation_id)
        return None if record is None else f"crp://recursive-evolution/prompt-strategy-operations/{operation_id}"

    def state(self, strategy_id: str) -> Mapping[str, object] | None:
        record = self._records.read(_PROMPT_HEADS, strategy_id)
        return None if record is None else dict(record.payload)

    def preflight(self, *, project_id: str, action: str, candidate: Mapping[str, object]) -> None:
        del project_id
        self._candidate(candidate)
        if action not in {"record_candidate", "start_canary", "rollback_canary", "promote", "rollback_promotion"}:
            raise RecursiveEvolutionTargetError("prompt strategy action is invalid")

    def apply(self, *, operation_id: str, action: str, candidate: Mapping[str, object], authorization_ref: str) -> None:
        if not isinstance(authorization_ref, str) or not authorization_ref.startswith("crp://"):
            raise RecursiveEvolutionTargetError("authorization reference is invalid")
        item = self._candidate(candidate)
        strategy_id, revision = item["strategy_id"], item["revision"]
        with self._records.begin() as unit:
            replay = unit.read(_PROMPT_COMMANDS, operation_id)
            if replay is not None:
                if replay.payload.get("action") != action or replay.payload.get("candidate") != item:
                    raise RecursiveEvolutionTargetError("prompt strategy operation conflicts")
                unit.rollback()
                return
            head_record = unit.read(_PROMPT_HEADS, strategy_id)
            head = {} if head_record is None else dict(head_record.payload)
            if action == "record_candidate":
                existing = unit.read(_PROMPT_REVISIONS, f"{strategy_id}~r{revision}")
                if existing is None:
                    if revision != head.get("latest_revision", 0) + 1 or head.get("canary_revision") is not None:
                        raise RecursiveEvolutionTargetError("prompt strategy candidate revision or baseline drifted")
                    unit.put(_PROMPT_REVISIONS, f"{strategy_id}~r{revision}", item, expected_revision=0)
                elif dict(existing.payload) != item:
                    raise RecursiveEvolutionTargetError("prompt strategy revision conflicts")
                next_head = {"latest_revision": revision, "active_revision": head.get("active_revision"), "canary_revision": head.get("canary_revision"), "prior_active_revision": head.get("prior_active_revision")}
            elif action == "start_canary":
                if unit.read(_PROMPT_REVISIONS, f"{strategy_id}~r{revision}") is None or head.get("canary_revision") is not None or head.get("active_revision") != revision - 1:
                    raise RecursiveEvolutionTargetError("prompt strategy canary baseline drifted")
                next_head = {"latest_revision": head.get("latest_revision"), "active_revision": head.get("active_revision"), "canary_revision": revision, "prior_active_revision": head.get("prior_active_revision")}
            elif action == "promote":
                if head.get("canary_revision") != revision:
                    raise RecursiveEvolutionTargetError("prompt strategy canary drifted")
                next_head = {"latest_revision": head.get("latest_revision"), "active_revision": revision, "canary_revision": None, "prior_active_revision": head.get("active_revision")}
            else:
                if action == "rollback_promotion":
                    if head.get("active_revision") != revision or head.get("prior_active_revision") is None:
                        raise RecursiveEvolutionTargetError("prompt strategy promotion drifted")
                    next_head = {"latest_revision": head.get("latest_revision"), "active_revision": head["prior_active_revision"], "canary_revision": None, "prior_active_revision": None}
                else:
                    next_head = {"latest_revision": head.get("latest_revision"), "active_revision": head.get("active_revision"), "canary_revision": None, "prior_active_revision": head.get("prior_active_revision")}
            unit.put(_PROMPT_HEADS, strategy_id, next_head, expected_revision=0 if head_record is None else head_record.revision)
            unit.put(_PROMPT_COMMANDS, operation_id, {"action": action, "candidate": item}, expected_revision=0)
            unit.commit()

    @staticmethod
    def _candidate(value: Mapping[str, object]) -> dict[str, object]:
        expected = {"strategy_id", "revision", "template_id", "parameters", "baseline_ref", "baseline_revision", "candidate_ref", "candidate_revision"}
        if set(value) != expected or value.get("strategy_id") != _PROMPT_STRATEGY_ID or value.get("template_id") not in _PROMPT_TEMPLATES or not isinstance(value.get("revision"), int) or value["revision"] < 2 or not isinstance(value.get("parameters"), Mapping):
            raise RecursiveEvolutionTargetError("prompt strategy candidate fields are invalid")
        if len(value["parameters"]) > len(_PROMPT_PARAMETERS) or any(k not in _PROMPT_PARAMETERS or not isinstance(v, bool) for k, v in value["parameters"].items()):
            raise RecursiveEvolutionTargetError("prompt strategy parameters are invalid")
        if value["baseline_ref"] != _PROMPT_BASELINE_REF or value["baseline_revision"] != "r1":
            raise RecursiveEvolutionTargetError("prompt strategy baseline drifted")
        return dict(value)

    def _bootstrap(self) -> None:
        baseline = {
            "strategy_id": _PROMPT_STRATEGY_ID, "revision": 1,
            "template_id": "safe-template", "parameters": {},
            "baseline_ref": _PROMPT_BASELINE_REF, "baseline_revision": "r1",
            "candidate_ref": _PROMPT_BASELINE_REF, "candidate_revision": "r1",
        }
        try:
            with self._records.begin() as unit:
                head = unit.read(_PROMPT_HEADS, _PROMPT_STRATEGY_ID)
                revision = unit.read(_PROMPT_REVISIONS, f"{_PROMPT_STRATEGY_ID}~r1")
                if head is None and revision is None:
                    unit.put(_PROMPT_REVISIONS, f"{_PROMPT_STRATEGY_ID}~r1", baseline, expected_revision=0)
                    unit.put(_PROMPT_HEADS, _PROMPT_STRATEGY_ID, {"latest_revision": 1, "active_revision": 1, "canary_revision": None, "prior_active_revision": None}, expected_revision=0)
                    unit.commit()
                    return
                if head is None or revision is None or head.payload.get("active_revision") != 1:
                    raise RecursiveEvolutionTargetError("prompt strategy trusted baseline is invalid")
                unit.rollback()
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as error:
            raise RecursiveEvolutionTargetError("prompt strategy bootstrap conflicted") from error


class RecursiveEvolutionTargetAuthority:
    """CAS/idempotent ``TargetAuthorityPort`` implementation.

    Candidates are registered separately because an EvolutionProposal carries
    only immutable ``crp://`` references.  The registry rejects raw prompts,
    path-like values, secrets and unconstrained instruction fields.
    """

    def __init__(
        self,
        *,
        records: SQLiteStructuredRecordStore,
        candidate_resolver: CandidateResolverPort | None = None,
        scheduler_policies: AgentPolicyCatalog | None = None,
        agent_profiles: object | None = None,
        project_skills: object | None = None,
        prompt_strategies: PromptStrategyAuthorityPort | None = None,
        policy_rollout_evidence: PolicyRolloutEvidenceRefResolver | None = None,
    ) -> None:
        if not isinstance(records, SQLiteStructuredRecordStore):
            raise RecursiveEvolutionTargetError("target receipt store is invalid")
        self._records = records
        self._resolver = candidate_resolver
        self._policies = scheduler_policies
        self._profiles = agent_profiles
        self._skills = project_skills
        self._prompts = prompt_strategies or PromptStrategyCatalog(records)
        self._policy_evidence = policy_rollout_evidence

    def register_candidate(self, *, project_id: str, proposal: EvolutionProposal, candidate: Mapping[str, object]) -> None:
        """Persist validated candidate material once; this never changes active state."""
        self._scope(project_id, proposal)
        normalized = self._validate_candidate(proposal, candidate)
        payload = {"project_id": project_id, "proposal": proposal.to_payload(), "candidate": normalized}
        try:
            with self._records.begin() as unit:
                prior = unit.read(_CANDIDATES, proposal.proposal_id)
                if prior is not None:
                    if dict(prior.payload) != payload:
                        raise RecursiveEvolutionTargetError("candidate identity conflicts")
                    unit.rollback()
                    return
                unit.put(_CANDIDATES, proposal.proposal_id, payload, expected_revision=0)
                unit.commit()
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as error:
            raise RecursiveEvolutionTargetError("candidate record write conflicted") from error

    def probe(self, *, operation_id: str) -> str | None:
        self._identifier(operation_id, "operation id")
        receipt = self._records.read(_RECEIPTS, operation_id)
        if receipt is None:
            return None
        payload = receipt.payload
        required = {
            "operation_id", "action", "project_id", "proposal_id",
            "target_kind", "baseline_ref", "baseline_revision",
            "candidate_ref", "candidate_revision", "active_before",
            "active_after", "target_result_ref", "status",
        }
        ref = payload.get("target_result_ref")
        project_id, proposal_id, action = (
            payload.get("project_id"), payload.get("proposal_id"),
            payload.get("action"),
        )
        candidate_record = (
            self._records.read(_CANDIDATES, proposal_id)
            if isinstance(proposal_id, str) else None
        )
        candidate_payload = (
            candidate_record.payload
            if candidate_record is not None
            and isinstance(candidate_record.payload, Mapping)
            else {}
        )
        try:
            proposal = EvolutionProposal.from_payload(
                candidate_payload.get("proposal")
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RecursiveEvolutionTargetError(
                "target receipt candidate authority is invalid"
            ) from error
        if (
            set(payload) != required
            or payload.get("operation_id") != operation_id
            or payload.get("status") != "applied"
            or not isinstance(ref, str)
            or ref != f"{_TARGET_RECEIPT_PREFIX}{operation_id}"
            or not isinstance(project_id, str)
            or candidate_payload.get("project_id") != project_id
            or candidate_payload.get("proposal") != proposal.to_payload()
            or proposal.proposal_id != proposal_id
            or payload.get("target_kind") != proposal.target_kind.value
            or payload.get("baseline_ref") != proposal.baseline_ref
            or payload.get("baseline_revision") != proposal.baseline_revision
            or payload.get("candidate_ref") != proposal.candidate_ref
            or payload.get("candidate_revision") != proposal.candidate_revision
            or action not in {
                "record_candidate", "start_canary", "rollback_canary",
                "promote", "rollback_promotion",
            }
            or (
                operation_id.startswith("evolution.")
                and (
                    not operation_id.startswith(
                        f"evolution.{project_id}.{proposal.episode_id}."
                    )
                    or not operation_id.endswith(f".{action}")
                )
            )
        ):
            raise RecursiveEvolutionTargetError("target receipt authority drifted")
        return ref

    def preflight(self, *, project_id: str, action: str, proposal: EvolutionProposal) -> None:
        self._scope(project_id, proposal)
        candidate = self._candidate(project_id, proposal, persist_resolved=True)
        self._validate_action(action, proposal.target_kind)
        if self._native_exact(project_id, action, proposal, candidate, self._native_state(project_id, proposal, candidate)):
            return
        self._native_preflight(project_id, action, proposal, candidate)

    def apply(self, *, operation_id: str, action: str, proposal: EvolutionProposal, authorization_ref: str) -> str:
        self._identifier(operation_id, "operation id")
        if not isinstance(authorization_ref, str) or not authorization_ref.startswith("crp://"):
            raise RecursiveEvolutionTargetError("authorization reference is invalid")
        project_id = self._proposal_project(proposal)
        candidate = self._candidate(project_id, proposal)
        self._validate_action(action, proposal.target_kind)
        receipt = self._records.read(_RECEIPTS, operation_id)
        if receipt is not None:
            expected = self._receipt_payload(operation_id, project_id, action, proposal, candidate, before=None, after=None)
            immutable_keys = set(expected) - {"active_before", "active_after"}
            if all(receipt.payload.get(key) == expected[key] for key in immutable_keys):
                return str(receipt.payload["target_result_ref"])
            raise RecursiveEvolutionTargetError("operation id conflicts with prior target receipt")
        before = self._native_state(project_id, proposal, candidate)
        if self._native_exact(project_id, action, proposal, candidate, before):
            after = self._native_state(project_id, proposal, candidate)
        else:
            self._native_preflight(project_id, action, proposal, candidate)
            self._native_apply(operation_id, project_id, action, proposal, candidate, authorization_ref)
            after = self._native_state(project_id, proposal, candidate)
        payload = self._receipt_payload(operation_id, project_id, action, proposal, candidate, before=before, after=after)
        try:
            with self._records.begin() as unit:
                prior = unit.read(_RECEIPTS, operation_id)
                if prior is not None:
                    if dict(prior.payload) != payload:
                        raise RecursiveEvolutionTargetError("operation id conflicts with prior target receipt")
                    unit.rollback()
                    return str(payload["target_result_ref"])
                unit.put(_RECEIPTS, operation_id, payload, expected_revision=0)
                unit.commit()
                return str(payload["target_result_ref"])
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as error:
            # Native authorities are idempotent/CAS; a retry reconciles their
            # already-applied result without making SQLite lifecycle authority.
            raise RecursiveEvolutionTargetError("target receipt write conflicted; retry operation") from error

    def _candidate(self, project_id: str, proposal: EvolutionProposal, *, persist_resolved: bool = False) -> Mapping[str, object]:
        stored = self._records.read(_CANDIDATES, proposal.proposal_id)
        if stored is not None:
            payload = stored.payload
            if payload.get("project_id") != project_id or payload.get("proposal") != proposal.to_payload():
                raise RecursiveEvolutionTargetError("candidate record scope drifted")
            candidate = payload.get("candidate")
            if isinstance(candidate, Mapping):
                return self._validate_candidate(proposal, candidate)
            raise RecursiveEvolutionTargetError("candidate record is invalid")
        if self._resolver is None:
            raise RecursiveEvolutionTargetError("candidate material is unavailable")
        try:
            resolved = self._validate_candidate(proposal, self._resolver(project_id=project_id, proposal=proposal))
            if persist_resolved:
                self.register_candidate(project_id=project_id, proposal=proposal, candidate=resolved)
            return resolved
        except RecursiveEvolutionTargetError:
            raise
        except Exception as error:
            raise RecursiveEvolutionTargetError("candidate material is unavailable") from error

    def _native_preflight(self, project_id: str, action: str, proposal: EvolutionProposal, candidate: Mapping[str, object]) -> None:
        kind = proposal.target_kind
        if kind is EvolutionTargetKind.SCHEDULER_POLICY:
            policy = AgentEvolutionPolicy.from_payload(candidate["policy"])
            if self._policies is None:
                raise RecursiveEvolutionTargetError("scheduler policy authority is unavailable")
            head = self._policies.head(policy.policy_id)
            if action == "record_candidate":
                return
            if head is None:
                raise RecursiveEvolutionTargetError("scheduler policy head is unavailable")
            if action == "start_canary" and (head.active_revision != policy.parent_revision or head.canary_revision is not None):
                raise RecursiveEvolutionTargetError("scheduler policy baseline drifted")
            if action == "promote" and head.canary_revision != policy.revision:
                raise RecursiveEvolutionTargetError("scheduler policy canary drifted")
            if action == "rollback_promotion" and head.active_revision != policy.revision:
                raise RecursiveEvolutionTargetError("scheduler policy promotion drifted")
            return
        if kind is EvolutionTargetKind.AGENT_PROFILE:
            profile = agent_profile_from_payload(candidate["profile"])
            current = self._profile(profile.profile_id)
            if current is None:
                raise RecursiveEvolutionTargetError("agent profile baseline is unavailable")
            baseline = agent_profile_from_payload(candidate["baseline_profile"])
            if current != baseline:
                raise RecursiveEvolutionTargetError("agent profile baseline drifted")
            self._profile_does_not_expand(profile, current)
            return
        if kind is EvolutionTargetKind.PROJECT_SKILL:
            current = self._skill(project_id)
            if current is None:
                raise RecursiveEvolutionTargetError("project skill baseline is unavailable")
            if candidate.get("native_baseline_revision") != current.get("revision"):
                raise RecursiveEvolutionTargetError("project skill baseline drifted")
            baseline = candidate["baseline_structured"]
            if any(current.get(key) != value for key, value in baseline.items()):
                raise RecursiveEvolutionTargetError("project skill baseline content drifted")
            return
        if kind is EvolutionTargetKind.PROMPT_STRATEGY:
            if self._prompts is None:
                raise RecursiveEvolutionTargetError("prompt strategy authority is unavailable")
            self._prompts.preflight(project_id=project_id, action=action, candidate=candidate)
            return
        raise RecursiveEvolutionTargetError("target kind is unsupported")

    def _native_apply(self, operation_id: str, project_id: str, action: str, proposal: EvolutionProposal, candidate: Mapping[str, object], authorization_ref: str) -> None:
        kind = proposal.target_kind
        if kind is EvolutionTargetKind.SCHEDULER_POLICY:
            assert self._policies is not None
            policy = AgentEvolutionPolicy.from_payload(candidate["policy"])
            head = self._policies.head(policy.policy_id)
            if action == "record_candidate":
                self._policies.create_candidate(policy, command_id=operation_id)
            elif action == "start_canary":
                assert head is not None
                self._policies.start_canary(policy.policy_id, policy.revision, percent=policy.evaluation.cohort_percent, evidence_ref=self._policy_evidence_ref(action, proposal, authorization_ref), expected_head_revision=head.revision, command_id=operation_id)
            elif action == "promote":
                assert head is not None
                self._policies.activate(policy.policy_id, policy.revision, evidence_ref=self._policy_evidence_ref(action, proposal, authorization_ref), expected_head_revision=head.revision, command_id=operation_id)
            else:
                assert head is not None
                self._policies.rollback(policy.policy_id, expected_head_revision=head.revision, command_id=operation_id)
            return
        if kind is EvolutionTargetKind.AGENT_PROFILE:
            # Profiles have no native cohort selector.  A canary is only a
            # staged candidate here; full replacement happens at promotion.
            if action in {"record_candidate", "start_canary", "rollback_canary"}:
                return
            profile = agent_profile_from_payload(candidate["profile"])
            current = self._profile(profile.profile_id)
            assert current is not None
            if action == "promote":
                self._profiles.update(profile, expected_revision=current.revision)
            else:
                baseline = agent_profile_from_payload(candidate["baseline_profile"])
                self._profiles.update(replace(baseline, revision=current.revision + 1), expected_revision=current.revision)
            return
        if kind is EvolutionTargetKind.PROJECT_SKILL:
            # ProjectSkillRepository has no cohort route either.  Its candidate
            # remains staged through canary; publish is its native promotion.
            if action in {"record_candidate", "start_canary", "rollback_canary"}:
                return
            from core.project_skill_core.ports import ProjectSkillUpdate
            current = self._skill(project_id)
            assert current is not None
            if action == "promote":
                self._skills.save(ProjectSkillUpdate(project_id=project_id, markdown=candidate["markdown"], structured=candidate["structured"], expected_revision=current["revision"], reason="governed recursive evolution promotion", transition_kind="ai_publication", actor="user", confirmation_kind="review_and_second_confirmation"))
            else:
                self._skills.rollback(project_id, target_revision=candidate["native_baseline_revision"], expected_revision=current["revision"], reason="governed recursive evolution rollback")
            return
        assert kind is EvolutionTargetKind.PROMPT_STRATEGY and self._prompts is not None
        self._prompts.apply(operation_id=operation_id, action=action, candidate=candidate, authorization_ref=authorization_ref)

    def _native_state(self, project_id: str, proposal: EvolutionProposal, candidate: Mapping[str, object]) -> str | int | None:
        if proposal.target_kind is EvolutionTargetKind.SCHEDULER_POLICY:
            policy = AgentEvolutionPolicy.from_payload(candidate["policy"])
            head = None if self._policies is None else self._policies.head(policy.policy_id)
            return None if head is None else head.revision
        if proposal.target_kind is EvolutionTargetKind.AGENT_PROFILE:
            profile = agent_profile_from_payload(candidate["profile"])
            current = self._profile(profile.profile_id)
            return None if current is None else current.revision
        if proposal.target_kind is EvolutionTargetKind.PROJECT_SKILL:
            current = self._skill(project_id)
            return None if current is None else current.get("revision")
        return candidate["revision"]

    def _native_exact(self, project_id: str, action: str, proposal: EvolutionProposal, candidate: Mapping[str, object], before: object) -> bool:
        if proposal.target_kind is EvolutionTargetKind.SCHEDULER_POLICY:
            policy = AgentEvolutionPolicy.from_payload(candidate["policy"])
            head = None if self._policies is None else self._policies.head(policy.policy_id)
            if head is None:
                return False
            if action == "record_candidate":
                return self._policies.get_revision(policy.policy_id, policy.revision) == policy
            if action == "start_canary":
                return head.canary_revision == policy.revision
            if action == "promote":
                return head.active_revision == policy.revision and head.canary_revision is None
            if action == "rollback_canary":
                return head.canary_revision is None and head.active_revision == policy.parent_revision
            return head.active_revision == policy.parent_revision and head.canary_revision is None
        if proposal.target_kind is EvolutionTargetKind.PROMPT_STRATEGY:
            if not isinstance(self._prompts, PromptStrategyCatalog):
                return False
            state = self._prompts.state(str(candidate["strategy_id"]))
            if state is None:
                return False
            revision = candidate["revision"]
            if action == "record_candidate":
                return self._records.read(_PROMPT_REVISIONS, f"{candidate['strategy_id']}~r{revision}") is not None
            if action == "start_canary":
                return state.get("canary_revision") == revision
            if action == "promote":
                return state.get("active_revision") == revision and state.get("canary_revision") is None
            if action == "rollback_canary":
                return state.get("canary_revision") is None and state.get("active_revision") == revision - 1
            return state.get("active_revision") == revision - 1 and state.get("canary_revision") is None
        if action in {"record_candidate", "start_canary", "rollback_canary"}:
            return False
        if proposal.target_kind is EvolutionTargetKind.AGENT_PROFILE:
            profile = agent_profile_from_payload(candidate["profile"])
            current = self._profile(profile.profile_id)
            if current is None:
                return False
            wanted = profile if action == "promote" else agent_profile_from_payload(candidate["baseline_profile"])
            return self._same_profile(current, wanted)
        if proposal.target_kind is EvolutionTargetKind.PROJECT_SKILL:
            current = self._skill(project_id)
            if current is None:
                return False
            if action == "promote":
                structured = candidate["structured"]
                return all(current.get(key) == value for key, value in structured.items() if key not in {"revision", "markdown_revision", "json_revision", "updated_at", "decision_log"})
            baseline = candidate["baseline_structured"]
            return all(current.get(key) == value for key, value in baseline.items())
        return False

    @staticmethod
    def _same_profile(left: AgentProfile, right: AgentProfile) -> bool:
        return agent_profile_to_payload(left) | {"revision": 0} == agent_profile_to_payload(right) | {"revision": 0}

    def _policy_evidence_ref(self, action: str, proposal: EvolutionProposal, authorization_ref: str) -> str:
        if self._policy_evidence is None:
            raise RecursiveEvolutionTargetError("scheduler policy rollout evidence is unavailable")
        try:
            ref = self._policy_evidence(action=action, proposal=proposal, authorization_ref=authorization_ref)
        except Exception as error:
            raise RecursiveEvolutionTargetError("scheduler policy rollout evidence is unavailable") from error
        if not isinstance(ref, str) or not ref.startswith("crp://"):
            raise RecursiveEvolutionTargetError("scheduler policy rollout evidence is invalid")
        return ref

    @staticmethod
    def _receipt_payload(operation_id: str, project_id: str, action: str, proposal: EvolutionProposal, candidate: Mapping[str, object], *, before: object, after: object) -> dict[str, object]:
        return {
            "operation_id": operation_id, "action": action, "project_id": project_id,
            "proposal_id": proposal.proposal_id, "target_kind": proposal.target_kind.value,
            "baseline_ref": proposal.baseline_ref, "baseline_revision": proposal.baseline_revision,
            "candidate_ref": proposal.candidate_ref, "candidate_revision": proposal.candidate_revision,
            "active_before": before, "active_after": after,
            "target_result_ref": f"{_TARGET_RECEIPT_PREFIX}{operation_id}", "status": "applied",
        }

    def _validate_candidate(self, proposal: EvolutionProposal, value: Mapping[str, object]) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            raise RecursiveEvolutionTargetError("candidate material is invalid")
        candidate = dict(value)
        self._reject_unsafe(candidate)
        binding = {"baseline_ref": proposal.baseline_ref, "baseline_revision": proposal.baseline_revision, "candidate_ref": proposal.candidate_ref, "candidate_revision": proposal.candidate_revision}
        if any(candidate.get(key) != item for key, item in binding.items()):
            raise RecursiveEvolutionTargetError("candidate baseline or revision binding drifted")
        kind = proposal.target_kind
        if kind is EvolutionTargetKind.SCHEDULER_POLICY:
            if set(candidate) != {"policy", *binding}:
                raise RecursiveEvolutionTargetError("scheduler policy candidate fields are invalid")
            policy = AgentEvolutionPolicy.from_payload(candidate["policy"])
            return {"policy": policy.to_payload(), **binding}
        if kind is EvolutionTargetKind.AGENT_PROFILE:
            if set(candidate) != {"profile", "baseline_profile", *binding}:
                raise RecursiveEvolutionTargetError("agent profile candidate fields are invalid")
            profile = agent_profile_from_payload(candidate["profile"])
            baseline = agent_profile_from_payload(candidate["baseline_profile"])
            if profile.profile_id != baseline.profile_id:
                raise RecursiveEvolutionTargetError("agent profile candidate identity drifted")
            return {"profile": candidate["profile"], "baseline_profile": candidate["baseline_profile"], **binding}
        if kind is EvolutionTargetKind.PROJECT_SKILL:
            if set(candidate) != {"markdown", "structured", "baseline_structured", "native_baseline_revision", *binding} or not isinstance(candidate["markdown"], str) or not isinstance(candidate["structured"], Mapping) or not isinstance(candidate["baseline_structured"], Mapping) or not isinstance(candidate["native_baseline_revision"], int):
                raise RecursiveEvolutionTargetError("project skill candidate fields are invalid")
            return candidate
        if kind is EvolutionTargetKind.PROMPT_STRATEGY:
            if set(candidate) != {"strategy_id", "revision", "template_id", "parameters", *binding} or not isinstance(candidate["strategy_id"], str) or not isinstance(candidate["revision"], int) or not isinstance(candidate["template_id"], str) or not isinstance(candidate["parameters"], Mapping):
                raise RecursiveEvolutionTargetError("prompt strategy candidate fields are invalid")
            return candidate
        raise RecursiveEvolutionTargetError("target kind is unsupported")

    def _profile(self, profile_id: str) -> AgentProfile | None:
        if self._profiles is None or not callable(getattr(self._profiles, "get", None)):
            raise RecursiveEvolutionTargetError("agent profile authority is unavailable")
        value = self._profiles.get(profile_id)
        if value is not None and not isinstance(value, AgentProfile):
            raise RecursiveEvolutionTargetError("agent profile authority is invalid")
        return value

    def _skill(self, project_id: str) -> Mapping[str, object] | None:
        if self._skills is None or not callable(getattr(self._skills, "load", None)):
            raise RecursiveEvolutionTargetError("project skill authority is unavailable")
        value = self._skills.load(project_id)
        if value is not None and not isinstance(value, Mapping):
            raise RecursiveEvolutionTargetError("project skill authority is invalid")
        return value

    @staticmethod
    def _profile_does_not_expand(candidate: AgentProfile, baseline: AgentProfile) -> None:
        if (candidate.profile_id != baseline.profile_id or candidate.role != baseline.role or candidate.model_tier != baseline.model_tier or candidate.enabled is not baseline.enabled or not set(candidate.capability_ids).issubset(baseline.capability_ids) or candidate.max_concurrent_children > baseline.max_concurrent_children or candidate.max_depth > baseline.max_depth or candidate.max_steps > baseline.max_steps or candidate.timeout_ms > baseline.timeout_ms or candidate.allow_child_spawn and not baseline.allow_child_spawn):
            raise RecursiveEvolutionTargetError("agent profile candidate expands authority")
        for name in ("model_calls", "tool_calls", "input_tokens", "output_tokens", "wall_time_ms"):
            if getattr(candidate.budget_limit, name) > getattr(baseline.budget_limit, name):
                raise RecursiveEvolutionTargetError("agent profile candidate expands budget")

    def _proposal_project(self, proposal: EvolutionProposal) -> str:
        # TargetAuthorityPort.apply has no project argument.  The preceding
        # preflight durably binds the opaque candidate artifact to its project;
        # this is intentionally not a lifecycle state or active configuration.
        record = self._records.read(_CANDIDATES, proposal.proposal_id)
        if record is None or record.payload.get("proposal") != proposal.to_payload():
            raise RecursiveEvolutionTargetError("apply requires a matching project-scoped preflight")
        project_id = record.payload.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            raise RecursiveEvolutionTargetError("candidate record project scope is invalid")
        return project_id

    @staticmethod
    def _scope(project_id: str, proposal: EvolutionProposal) -> None:
        if not isinstance(project_id, str) or not project_id or proposal.target_kind not in EvolutionTargetKind:
            raise RecursiveEvolutionTargetError("target project scope is invalid")

    @staticmethod
    def _validate_action(action: str, kind: EvolutionTargetKind) -> None:
        if action not in {"record_candidate", "start_canary", "rollback_canary", "promote", "rollback_promotion"}:
            raise RecursiveEvolutionTargetError("target action is invalid")

    @staticmethod
    def _identifier(value: str, label: str) -> None:
        if not isinstance(value, str) or not 8 <= len(value) <= 128 or any(char not in _ID_CHARS for char in value):
            raise RecursiveEvolutionTargetError(f"{label} is invalid")

    @classmethod
    def _reject_unsafe(cls, value: object) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if not isinstance(key, str) or key.lower().replace("-", "_") in _FORBIDDEN:
                    raise RecursiveEvolutionTargetError("candidate contains an unsafe field")
                cls._reject_unsafe(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                cls._reject_unsafe(nested)
        elif isinstance(value, str) and ("\\" in value or value.startswith("/") or "://" in value and not value.startswith("crp://")):
            raise RecursiveEvolutionTargetError("candidate contains an unsafe locator")
