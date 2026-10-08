from __future__ import annotations

from dataclasses import dataclass


SCHEMA_VERSION = "1.0.0"
PROJECTION_VERSION = "progressive-memory-r0-r1-v1"
GENERATOR_POLICY_ID = "deterministic-r0-r1-builder-v1"


@dataclass(frozen=True, slots=True)
class AuthorityObjectRef:
    object_type: str
    object_id: str
    revision: int
    content_hash: str

    def to_payload(self) -> dict[str, object]:
        return {
            "object_type": self.object_type,
            "object_id": self.object_id,
            "revision": self.revision,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class SafeSourceRef:
    source_id: str
    locator: str

    def to_payload(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "locator": self.locator,
        }


@dataclass(frozen=True, slots=True)
class ScenarioProjectionRef:
    scenario_id: str
    revision: int
    title: str
    summary_preview: str

    def to_payload(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "revision": self.revision,
            "title": self.title,
            "summary_preview": self.summary_preview,
        }


@dataclass(frozen=True, slots=True)
class AtomProjectionRef:
    atom_id: str
    revision: int
    atom_type: str
    content_preview: str

    def to_payload(self) -> dict[str, object]:
        return {
            "atom_id": self.atom_id,
            "revision": self.revision,
            "atom_type": self.atom_type,
            "content_preview": self.content_preview,
        }


@dataclass(frozen=True, slots=True)
class ProjectSkillProjectionRef:
    skill_id: str
    revision: int
    name: str
    purpose_preview: str

    def to_payload(self) -> dict[str, object]:
        return {
            "skill_id": self.skill_id,
            "revision": self.revision,
            "name": self.name,
            "purpose_preview": self.purpose_preview,
        }


@dataclass(frozen=True, slots=True)
class R0SeriesRouterItem:
    projection_id: str
    project_id: str
    series_id: str
    series_memory_id: str
    authority_identity: str
    authority_fingerprint: str
    generated_at: str
    title: str
    description: str
    keywords: tuple[str, ...]
    source_refs: tuple[SafeSourceRef, ...]
    derived_from: tuple[AuthorityObjectRef, ...]
    content_length: int
    status: str = "ready"
    failure_code: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "projection_id": self.projection_id,
            "projection_type": "r0_series_router",
            "project_id": self.project_id,
            "series_id": self.series_id,
            "series_memory_id": self.series_memory_id,
            "authority_identity": self.authority_identity,
            "authority_fingerprint": self.authority_fingerprint,
            "generator_policy_id": GENERATOR_POLICY_ID,
            "projection_version": PROJECTION_VERSION,
            "generated_at": self.generated_at,
            "status": self.status,
            "failure_code": self.failure_code,
            "title": self.title,
            "description": self.description,
            "keywords": list(self.keywords),
            "source_refs": [ref.to_payload() for ref in self.source_refs],
            "derived_from": [ref.to_payload() for ref in self.derived_from],
            "content_length": self.content_length,
        }


@dataclass(frozen=True, slots=True)
class R1SeriesDigestItem:
    projection_id: str
    project_id: str
    series_id: str
    series_memory_id: str
    authority_identity: str
    authority_fingerprint: str
    generated_at: str
    summary: str
    scenario_refs: tuple[ScenarioProjectionRef, ...]
    atom_refs: tuple[AtomProjectionRef, ...]
    skill_refs: tuple[ProjectSkillProjectionRef, ...]
    source_refs: tuple[SafeSourceRef, ...]
    derived_from: tuple[AuthorityObjectRef, ...]
    content_length: int
    status: str = "ready"
    failure_code: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "projection_id": self.projection_id,
            "projection_type": "r1_series_digest",
            "project_id": self.project_id,
            "series_id": self.series_id,
            "series_memory_id": self.series_memory_id,
            "authority_identity": self.authority_identity,
            "authority_fingerprint": self.authority_fingerprint,
            "generator_policy_id": GENERATOR_POLICY_ID,
            "projection_version": PROJECTION_VERSION,
            "generated_at": self.generated_at,
            "status": self.status,
            "failure_code": self.failure_code,
            "summary": self.summary,
            "scenario_refs": [ref.to_payload() for ref in self.scenario_refs],
            "atom_refs": [ref.to_payload() for ref in self.atom_refs],
            "skill_refs": [ref.to_payload() for ref in self.skill_refs],
            "source_refs": [ref.to_payload() for ref in self.source_refs],
            "derived_from": [ref.to_payload() for ref in self.derived_from],
            "content_length": self.content_length,
        }


@dataclass(frozen=True, slots=True)
class MemoryRetrievalProjection:
    project_id: str
    authority_identity: str
    authority_fingerprint: str
    generated_at: str
    status: str
    derived_from: tuple[AuthorityObjectRef, ...]
    project_skill_refs: tuple[ProjectSkillProjectionRef, ...]
    r0_items: tuple[R0SeriesRouterItem, ...]
    r1_items: tuple[R1SeriesDigestItem, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "projection_version": PROJECTION_VERSION,
            "project_id": self.project_id,
            "authority_identity": self.authority_identity,
            "authority_fingerprint": self.authority_fingerprint,
            "generated_at": self.generated_at,
            "status": self.status,
            "derived_from": [ref.to_payload() for ref in self.derived_from],
            "project_skill_refs": [
                ref.to_payload() for ref in self.project_skill_refs
            ],
            "r0_items": [item.to_payload() for item in self.r0_items],
            "r1_items": [item.to_payload() for item in self.r1_items],
            "safety": {
                "derived": True,
                "business_authority": False,
                "rebuildable": True,
                "source_body_included": False,
                "project_skill_body_included": False,
                "business_writes_allowed": False,
            },
        }


def serialize_memory_retrieval_projection(
    projection: MemoryRetrievalProjection,
) -> dict[str, object]:
    return projection.to_payload()
