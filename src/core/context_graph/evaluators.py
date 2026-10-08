from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from .models import ContextGraphSnapshot
from .staleness import (
    StalenessConfirmation,
    StalenessImpactPreview,
    evaluate_staleness,
    stale_replay_order,
    staleness_impact_preview,
    validate_staleness_confirmation,
)


class ContextPermissionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ContextPermissionGrant:
    project_id: str
    permission_revision: str
    allowed_content_refs: frozenset[str]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.project_id, str)
            or not self.project_id.strip()
            or not isinstance(self.permission_revision, str)
            or not self.permission_revision.strip()
            or type(self.allowed_content_refs) is not frozenset
            or any(
                not isinstance(content_ref, str) or not content_ref.strip()
                for content_ref in self.allowed_content_refs
            )
        ):
            raise ContextPermissionError("invalid_permission_grant")

    def validate(self, snapshot: ContextGraphSnapshot, node_ids: Sequence[str]) -> None:
        if snapshot.project_id != self.project_id:
            raise ContextPermissionError("permission_project_scope_violation")
        by_id = {node.node_id: node for node in snapshot.nodes}
        unknown = sorted(node_id for node_id in node_ids if node_id not in by_id)
        if unknown:
            raise ContextPermissionError("permission_node_scope_violation:" + ",".join(unknown))
        denied = sorted(node_id for node_id in node_ids if by_id[node_id].content_ref not in self.allowed_content_refs)
        if denied:
            raise ContextPermissionError("content_permission_denied:" + ",".join(denied))


@dataclass(frozen=True, slots=True)
class ContextBudgetResult:
    kept_node_ids: tuple[str, ...]
    rendered: Mapping[str, str]
    trimmed: tuple[Mapping[str, object], ...]
    original_token_estimate: int
    final_token_estimate: int
    selected_outputs_affected: bool
    selected_output_ids_affected: tuple[str, ...] = ()


class ContextBudgetEvaluator:
    estimator_revision = "canonical-model-entry-v2"

    @staticmethod
    def estimate(text: str) -> int:
        return (len(text) + 3) // 4

    def evaluate(
        self,
        *,
        ordered_node_ids: Sequence[str],
        selected_outputs: Sequence[str],
        rendered: Mapping[str, str],
        token_budget: int,
        projection_for: Callable[[Sequence[str], Mapping[str, str]], Mapping[str, object]],
    ) -> ContextBudgetResult:
        if token_budget < 1:
            raise ValueError("invalid_token_budget")
        mutable = dict(rendered)
        original = self.estimate_projection(projection_for(ordered_node_ids, mutable))
        kept = list(ordered_node_ids)
        trimmed: list[Mapping[str, object]] = []
        cost = original
        for node_id in ordered_node_ids:
            if cost <= token_budget:
                break
            if node_id in selected_outputs:
                continue
            kept.remove(node_id)
            next_cost = self.estimate_projection(projection_for(kept, mutable))
            trimmed.append({"node_id": node_id, "reason": "token_budget", "token_estimate": cost - next_cost, "selected_output": False})
            cost = next_cost
        # Selected outputs are preserved where the approved budget permits it.
        # Unlike ordinary trimming, their final rendering may have to be empty
        # when several selected outputs compete for an extremely small budget.
        # That trade-off is recorded below rather than letting the binding exceed
        # its hard budget.
        for node_id in selected_outputs:
            if cost <= token_budget or node_id not in kept:
                continue
            original_content = mutable[node_id]
            current = cost
            # The envelope itself can consume the entire approved budget.  In
            # that case the platform fails closed instead of silently sending
            # an over-budget selected conclusion.
            low, high, best = 0, len(original_content), -1
            while low <= high:
                middle = (low + high) // 2
                mutable[node_id] = original_content[:middle]
                candidate = self.estimate_projection(projection_for(kept, mutable))
                if candidate <= token_budget:
                    best = middle
                    low = middle + 1
                else:
                    high = middle - 1
            if best < 0:
                mutable[node_id] = original_content
                raise ValueError("hard_token_budget_unenforceable")
            mutable[node_id] = original_content[:best]
            cost = self.estimate_projection(projection_for(kept, mutable))
            final = cost
            trimmed.append({
                "node_id": node_id,
                "reason": "selected_output_elided" if best == 0 else "selected_output_truncated",
                "token_estimate": current - final,
                "selected_output": True,
            })
        if cost > token_budget:
            # This is deliberately not a best-effort result.  A compiler must
            # never receive a binding that is over its approved token budget.
            raise ValueError("hard_token_budget_unenforceable")
        return ContextBudgetResult(
            tuple(kept), mutable, tuple(trimmed), original, cost,
            any(bool(item["selected_output"]) for item in trimmed),
            tuple(item["node_id"] for item in trimmed if bool(item["selected_output"])),
        )

    @staticmethod
    def estimate_projection(projection: Mapping[str, object]) -> int:
        # Imported lazily to keep permission/staleness contracts independent
        # from the model projection module at import time.
        from .model_projection import estimate_model_projection_tokens

        return estimate_model_projection_tokens(projection)


class ContextStalenessEvaluator:
    evaluator_revision = "1.0.0"

    def evaluate(self, previous: ContextGraphSnapshot, current: ContextGraphSnapshot, **revision_sets: Mapping[str, str]) -> ContextGraphSnapshot:
        return evaluate_staleness(previous, current, **revision_sets)

    def replay_order(self, snapshot: ContextGraphSnapshot) -> tuple[str, ...]:
        return stale_replay_order(snapshot)

    def preview(self, snapshot: ContextGraphSnapshot) -> StalenessImpactPreview:
        return staleness_impact_preview(snapshot)

    def require_confirmation(
        self,
        preview: StalenessImpactPreview,
        confirmation: StalenessConfirmation | None,
    ) -> None:
        validate_staleness_confirmation(preview, confirmation)
