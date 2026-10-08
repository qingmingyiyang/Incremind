from __future__ import annotations

from core.context_graph import (
    ContextBinding,
    ContextCompiler,
    ContextGraphSnapshot,
    ContextPermissionGrant,
    FrozenContextRevisions,
    StalenessEvaluationInput,
)


def compile_context_graph(
    snapshot: ContextGraphSnapshot,
    *,
    revisions: FrozenContextRevisions,
    expected_revisions: FrozenContextRevisions,
    permission_grant: ContextPermissionGrant,
    token_budget: int,
    staleness_input: StalenessEvaluationInput,
) -> ContextBinding:
    """Package declaration over the platform compiler; it owns no model runtime."""

    return ContextCompiler().compile(
        snapshot,
        revisions=revisions,
        expected_revisions=expected_revisions,
        permission_grant=permission_grant,
        token_budget=token_budget,
        staleness_input=staleness_input,
    )
