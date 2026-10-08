"""Detached policy inputs and results; no domain, storage, or runtime imports."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class StrengthInput:
    count: int
    project_id: str
    score: float | None = None
    updated_at: datetime | None = None
    now: datetime | None = None


@dataclass(frozen=True)
class StrengthOutput:
    half_life_days: float
    score: float | None


@dataclass(frozen=True)
class ForgetInput:
    score: float
    previous: str
    kind: str
    project_id: str
    can_recover: bool


@dataclass(frozen=True)
class EnoughInput:
    evidence: str
    coverage: float
    detail: bool
    layers: Sequence[str]


@dataclass(frozen=True)
class RankInput:
    score: float
    weight: float
    conditions: Sequence[str] = ()
    reference: str | None = None


@dataclass(frozen=True)
class ScopeInput:
    requested: str | None
    assigned: str | None


@dataclass(frozen=True)
class PlaceInput:
    tagged_project: str | None
    intent: str
    current_project: str


@dataclass(frozen=True)
class PlaceProject:
    project_id: str
    vocabulary: frozenset[str]
    scenes: Mapping[str, frozenset[str]]


@dataclass(frozen=True)
class PlaceExample:
    current_project: str
    terms: frozenset[str]
    project_id: str
    scene: str | None


@dataclass(frozen=True)
class PlaceSuggestionInput:
    current_project: str
    terms: Sequence[tuple[str, float]]
    projects: Sequence[PlaceProject]
    examples: Sequence[PlaceExample] = ()


@dataclass(frozen=True)
class PlaceHint:
    project_id: str
    scene: str | None
    score: float


@dataclass(frozen=True)
class PlaceInboxItem:
    id: str
    terms: Sequence[tuple[str, float]]


@dataclass(frozen=True)
class PlaceInboxInput:
    items: Sequence[PlaceInboxItem]


@dataclass(frozen=True)
class PlaceGroup:
    name: str
    ids: tuple[str, ...]


@dataclass(frozen=True)
class TriggerInput:
    initial: bool
    initial_delay: float = 60
    interval: float = 86400


@dataclass(frozen=True)
class ExtractInput:
    experiences: Sequence[Mapping[str, Any]]
    source_constraints: str
    neighbors: Sequence[Mapping[str, Any]] = ()
    projects: Sequence[Mapping[str, Any]] = ()
    project_id: str | None = None


@dataclass(frozen=True)
class ExtractDecodeInput:
    output: Any
    normalize_conditions: Callable[[Any], tuple[str, ...]]
    neighbors: Sequence[Mapping[str, Any]] = ()
    projects: Sequence[Mapping[str, Any]] = ()
    project_id: str | None = None


@dataclass(frozen=True)
class ExtractOutput:
    rows: tuple[tuple[str, tuple[str, ...]], ...]
    errors: tuple[str, ...] = ()
    valid: bool = True
    hints: tuple[Mapping[str, Any], ...] = ()
    supports: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class ModelPolicy:
    """Prepare messages and decide from the separately persisted model output."""
    prepare: Callable[[ExtractInput], list[dict[str, str]]]
    decide: Callable[[ExtractDecodeInput], ExtractOutput]

    def __call__(self, request: ExtractDecodeInput) -> ExtractOutput:
        return self.decide(request)


def invoke_entry(entrypoint: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Dispatch a real caller-owned entry without importing its infrastructure.

    The five existing service entries stay in their current modules. Their
    caller injects the actual function; registry versions never own services.
    """
    return entrypoint(*args, **kwargs)
