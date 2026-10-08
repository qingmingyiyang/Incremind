"""Receipt-bound reviewer observer for project World supervision.

The observer is deliberately an adapter, not a scheduler: it may write one
verification/decision pair only after the existing Agent store proves the
reviewer, parent main run, fan-in and recipient-owned summary chain.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import re

from backend.api.workbench_ai_runtime import WORLD_PROJECT_SESSION_ID
from backend.api.world_supervision_runtime import WorldSupervisionRuntime
from core.ai_kernel import validate_turn_request
from core.personal_world_model import validate_world_narrative


_VERDICT = re.compile(
    r"^VERDICT=(supported|weakened|refuted|inconclusive);"
    r"DISPOSITION=(continue|replan_required|stop_required|escalate_user);"
    r"FINDING=([^;\r\n]{1,512})$"
)
_ALLOWED = {
    "supported": frozenset({"continue"}),
    "weakened": frozenset({"replan_required", "escalate_user"}),
    "refuted": frozenset({"replan_required", "stop_required", "escalate_user"}),
    "inconclusive": frozenset({"replan_required", "escalate_user"}),
}


class WorldSupervisionAgentObserver:
    """Consume only a completed reviewer fan-in using durable references."""

    def __init__(
        self,
        *,
        supervision: WorldSupervisionRuntime,
        run_store: object,
        request_loader: Callable[[str], Mapping[str, object]],
        payload_loader: Callable[[str], object],
        now: Callable[[], str],
    ) -> None:
        self._supervision = supervision
        self._runs = run_store
        self._request_loader = request_loader
        self._payload_loader = payload_loader
        self._now = now

    def observe(self, turn_id: str) -> str:
        """Record one result, or safely ignore a non-World terminal Turn.

        Any child terminal callback may be the last one that makes an
        already-created fan-in observable.  Therefore lookup begins at the
        durable parent rather than requiring the callback Turn to be the
        reviewer itself.
        """

        try:
            binding = self._binding(turn_id)
        except Exception:
            return "ignored"
        if binding is None:
            return "ignored"
        project_id, action_id, reviewer, result, summary = binding
        try:
            claim = self._supervision.current_claim(project_id, action_id)
            if getattr(claim, "latest_decision", None) is not None:
                return "noop"
            evidence = _evidence(reviewer, result)
            parsed = (
                self._summary(summary, reviewer)
                if getattr(result, "status", None) == "completed"
                and getattr(reviewer, "status", None) == "completed"
                else None
            )
            verdict, disposition, finding = parsed or (
                "inconclusive",
                "replan_required",
                "Reviewer output was unavailable or did not match the fixed verdict contract.",
            )
            existing = getattr(claim, "latest_verification", None)
            if existing is None:
                # Read the projection at the last possible point before the
                # append; it is the fact the verification actually checked.
                verification = self._supervision.record_verification(
                    project_id=project_id, action_id=action_id, verdict=verdict,
                    finding=finding,
                    checked_world_sequence=self._supervision.current_world_sequence(project_id),
                    evidence_refs=evidence, recorded_at=self._now(),
                )
                verification_id = str(verification.event.payload["verification_id"])
            else:
                verification_id = str(existing.verification_id)
            self._supervision.record_decision(
                project_id=project_id, action_id=action_id,
                verification_id=verification_id, disposition=disposition,
                rationale=finding, evidence_refs=evidence, recorded_at=self._now(),
            )
            return "recorded"
        except Exception:
            # Do not append a second verification after a storage failure. A
            # restart resumes from ``latest_verification`` and retries only
            # the deterministic decision append above.
            return "ignored"

    def recover(self, *, limit: int = 64) -> dict[str, int | str]:
        """Replay bounded durable candidates after callback-chain interruption.

        Candidate selection is topology-only.  Each candidate still flows
        through ``observe`` so the immutable request is revalidated and only
        the World session can produce a supervision append.
        """
        candidates = tuple(self._runs.list_supervision_candidates(limit=limit))
        counts = {"recorded": 0, "noop": 0, "ignored": 0}
        for candidate in candidates:
            turn_id = getattr(candidate, "turn_id", None)
            if not isinstance(turn_id, str):
                counts["ignored"] += 1
                continue
            outcome = self.observe(turn_id)
            if outcome in counts:
                counts[outcome] += 1
            else:
                counts["ignored"] += 1
        return {"status": "completed", "scanned": len(candidates), **counts}

    def _binding(self, turn_id: str):
        observed_request = validate_turn_request(self._request_loader(turn_id))
        project_id = _project_id(observed_request)
        found = self._runs.get_run_by_turn_id(turn_id, project_id=project_id)
        if found is None:
            return None
        observed = found[0]
        if getattr(observed, "role", None) == "main":
            main = observed
        elif getattr(observed, "role", None) == "subagent" and isinstance(
            getattr(observed, "parent_run_id", None), str,
        ):
            main_found = self._runs.get_run_with_revision(observed.parent_run_id, project_id=project_id)
            if main_found is None:
                return None
            main = main_found[0]
        else:
            return None
        if getattr(main, "role", None) != "main" or getattr(main, "parent_run_id", None) is not None:
            return None
        main_request = validate_turn_request(self._request_loader(main.turn_id))
        if (
            main_request.get("session_id") != WORLD_PROJECT_SESSION_ID
            or _project_id(main_request) != project_id
            or not isinstance(main_request.get("operation_id"), str)
        ):
            return None
        action_id = str(main_request["operation_id"])
        children = self._runs.list_runs(project_id=project_id, parent_run_id=main.run_id)
        for fan_in in self._runs.list_fan_ins(project_id=project_id, parent_run_id=main.run_id):
            result = self._runs.get_fan_in_result(fan_in.fan_in_id, project_id=project_id)
            if (
                result is None or getattr(result, "status", None) not in {
                    "completed", "failed", "cancelled", "timed_out",
                }
                or getattr(result, "fan_in_id", None) != fan_in.fan_in_id
                or getattr(result, "project_id", None) != project_id
                or getattr(result, "parent_run_id", None) != main.run_id
                or not isinstance(getattr(result, "receipt_ref", None), str)
            ):
                continue
            for reviewer in children:
                if (
                    getattr(reviewer, "run_id", None) not in getattr(fan_in, "child_run_ids", ())
                    or getattr(reviewer, "profile_id", None) != "subagent.reviewer"
                    or getattr(reviewer, "role", None) != "subagent"
                    or getattr(reviewer, "parent_run_id", None) != main.run_id
                    or not getattr(reviewer, "is_terminal", False)
                    or not isinstance(getattr(reviewer, "terminal_receipt_ref", None), str)
                ):
                    continue
                summary = next(
                    (item for item in getattr(result, "child_summaries", ())
                     if getattr(item, "child_run_id", None) == reviewer.run_id),
                    None,
                )
                if (
                    summary is None or getattr(summary, "project_id", None) != project_id
                    or getattr(summary, "status", None) != getattr(reviewer, "status", None)
                    or getattr(summary, "receipt_ref", None) != reviewer.terminal_receipt_ref
                    or not isinstance(getattr(summary, "summary_ref", None), str)
                    or not summary.summary_ref.startswith(f"crp://session/{main.turn_id}/")
                ):
                    continue
                return project_id, action_id, reviewer, result, summary
        return None

    def _summary(self, summary: object, reviewer: object):
        payload = self._payload_loader(summary.summary_ref)
        if not isinstance(payload, Mapping):
            return None
        if (
            payload.get("kind") != "agent.child-terminal-summary.v1"
            or payload.get("child_run_id") != reviewer.run_id
            or payload.get("profile_id") != "subagent.reviewer"
            or payload.get("status") != "completed"
        ):
            return None
        return parse_reviewer_verdict(payload.get("final_summary"))


def parse_reviewer_verdict(value: object) -> tuple[str, str, str] | None:
    if not isinstance(value, str):
        return None
    match = _VERDICT.fullmatch(value)
    if match is None:
        return None
    verdict, disposition, finding = match.groups()
    if disposition not in _ALLOWED[verdict] or not finding.strip():
        return None
    try:
        # Reviewer text is model-generated.  Apply the same privacy/locator
        # boundary as a World event before it can become a durable finding.
        finding = validate_world_narrative(finding, "reviewer finding", maximum=512)
    except Exception:
        return None
    return verdict, disposition, finding


def _project_id(request: Mapping[str, object]) -> str:
    scope = request.get("scope")
    if not isinstance(scope, Mapping) or not isinstance(scope.get("project_id"), str):
        raise ValueError("project scope is unavailable")
    return str(scope["project_id"])


def _evidence(reviewer: object, result: object) -> list[str]:
    refs = [reviewer.terminal_receipt_ref, result.receipt_ref]
    if isinstance(getattr(result, "result_ref", None), str):
        refs.append(result.result_ref)
    if not all(isinstance(item, str) and item.startswith("crp://") for item in refs):
        raise ValueError("reviewer evidence is unavailable")
    return refs
