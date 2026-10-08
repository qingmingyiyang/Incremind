"""Governed recursive-evolution domain contracts and pure projector."""

from .contracts import (
    EvaluationVerdict,
    EvolutionContractError,
    EvolutionEpisode,
    EvolutionEvaluation,
    EvolutionPolicy,
    EvolutionProposal,
    EvolutionResourceEnvelope,
    EvolutionReview,
    EvolutionRolloutDecision,
    EvolutionTargetKind,
    ReviewVerdict,
)
from .projection import (
    EvolutionEpisodeProjection,
    EvolutionEvent,
    EvolutionEventKind,
    evolution_event_world_identity,
    project_evolution_events,
)

__all__ = [
    "EvaluationVerdict",
    "EvolutionContractError",
    "EvolutionEpisode",
    "EvolutionEpisodeProjection",
    "EvolutionEvaluation",
    "EvolutionEvent",
    "EvolutionEventKind",
    "EvolutionPolicy",
    "EvolutionProposal",
    "EvolutionResourceEnvelope",
    "EvolutionReview",
    "EvolutionRolloutDecision",
    "EvolutionTargetKind",
    "ReviewVerdict",
    "evolution_event_world_identity",
    "project_evolution_events",
]
