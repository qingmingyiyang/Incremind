"""Fixture-only atomic Project Skill publication and rollback transaction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from core.storage_provider import SQLiteStructuredRecord, SQLiteStructuredRecordStore, SQLiteStructuredRecordUnitOfWork
from core.storage_provider.external_agent_publication_change import (
    ExternalAgentPublicationChangeError,
    ExternalAgentPublicationChangeOutbox,
)

from .ports import ProjectSkillUpdate
from .publication_draft import ProjectSkillPublicationDraftError, validate_project_skill_publication_draft
from .runtime import ObjectStoreProjectSkillRepository, ProjectSkillExpectedRevisionError, ProjectSkillRepositoryError
from .sqlite_runtime import _SQLiteTransactionObjectStore


_SKILL_COLLECTIONS = ("project_skill_index", "project_skill_json", "project_skill_markdown", "project_skill_revisions", "project_skills")


class ProjectSkillPublicationCompositeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectSkillPublicationCompositeResult:
    publication_id: str
    transition_id: str
    project_skill_revision: int
    replayed: bool = False
    publication_revision: int = 1


@dataclass(frozen=True, slots=True)
class ProjectSkillPublicationCompositeCommit:
    records: tuple[SQLiteStructuredRecord, ...]
    replayed: bool = False


class SQLiteProjectSkillPublicationCompositeUnitOfWork:
    """Atomic SQLite Project Skill publication and rollback transaction."""

    def __init__(self, database_path: Path, *, namespace_id: str = "default", now: str = "2026-07-12T12:00:00+08:00") -> None:
        self._records = SQLiteStructuredRecordStore(database_path)
        self._namespace_id = namespace_id
        self._now = now

    def begin(self) -> ProjectSkillPublicationCompositeTransaction:
        return ProjectSkillPublicationCompositeTransaction(self._records.begin(), self._namespace_id, self._now)


class ProjectSkillPublicationCompositeTransaction:
    def __init__(self, records: SQLiteStructuredRecordUnitOfWork, namespace_id: str, now: str) -> None:
        self._records = records
        snapshot = {
            collection: {record.object_id: dict(record.payload) for record in records.list(collection)}
            for collection in _SKILL_COLLECTIONS
        }
        self._aggregate = ObjectStoreProjectSkillRepository(
            _SQLiteTransactionObjectStore(records, snapshot), namespace_id=namespace_id, now=now
        )
        self._namespace_id = namespace_id
        self._now = now
        self._replayed = False

    def __enter__(self) -> ProjectSkillPublicationCompositeTransaction:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if not self._records.closed:
            self._records.rollback()
        return False

    def staged_draft(self, draft_id: str) -> Mapping[str, object]:
        """Read one publish input through the open composite transaction."""

        staged = self._records.read("staging_project_skills", draft_id)
        if staged is None:
            raise ProjectSkillPublicationCompositeError("staging project_skill not found")
        return dict(staged.payload)

    def publish(self, *, draft: Mapping[str, object]) -> ProjectSkillPublicationCompositeResult:
        normalized = _draft(draft)
        draft_id = _required_str(normalized, "id")
        staged = self._records.read("staging_project_skills", draft_id)
        publication_id = f"memory-publication-project-skill-{draft_id}"
        transition_id = f"transition-project-skill-publication-{draft_id}"
        if staged is None:
            return self._publish_replay(normalized, publication_id, transition_id)
        if dict(staged.payload) != normalized:
            raise ProjectSkillPublicationCompositeError("staged Project Skill draft drifted")
        structured = _mapping(normalized, "structured_payload")
        expected_revision = _required_int(normalized, "expected_project_skill_revision")
        published = dict(structured)
        published["status"] = "active"
        published["trust_status"] = "user_confirmed"
        try:
            skill = self._aggregate.save(
                ProjectSkillUpdate(
                    project_id=_required_str(normalized, "project_id"),
                    markdown=_required_str(normalized, "markdown"),
                    structured=published,
                    expected_revision=expected_revision,
                    reason=_required_str(normalized, "review_reason"),
                    transition_kind="ai_publication",
                    actor="user",
                    confirmation_kind="review_and_second_confirmation",
                )
            )
        except (ProjectSkillExpectedRevisionError, ProjectSkillRepositoryError) as error:
            raise ProjectSkillPublicationCompositeError(f"Project Skill aggregate rejected publish: {error}") from error
        revision = _required_int(skill, "revision")
        transition = _transition(
            transition_id=transition_id,
            skill_id=_required_str(skill, "id"),
            from_trust="system_generated",
            to_trust="user_confirmed",
            from_revision=expected_revision,
            to_revision=revision,
            reason=_required_str(normalized, "review_reason"),
            source_refs=_refs(normalized, "source_refs"),
            now=self._now,
        )
        publication = {
            "schema_version": "1.0.0", "id": publication_id, "layer": "project_skill", "object_type": "project_skill",
            "object_id": draft_id, "project_id": normalized["project_id"], "published_object_id": skill["id"], "published_revision": revision, "status": "published",
            "published_by": "user", "reason": normalized["review_reason"], "published_ref": f"crp://{self._namespace_id}/memory/project-skill/{skill['id']}.json",
            "transition_ref": f"crp://{self._namespace_id}/memory-transitions/{transition_id}.json",
            "rollback_ref": f"crp://{self._namespace_id}/memory-publications/{publication_id}/rollback",
            "source_candidate_id": normalized["source_candidate_id"], "review_ref": normalized["review_ref"],
            "draft_digest": normalized["draft_digest"], "source_refs": _refs(normalized, "source_refs"), "created_at": self._now,
        }
        self._supersede_prior_publications(
            skill_id=_required_str(skill, "id"),
            publication_id=publication_id,
        )
        _append(self._records, "memory_transitions", transition_id, transition)
        _append(self._records, "memory_publications", publication_id, publication)
        self._enqueue_external_agent_publication_change(
            publication_identity=publication_id,
            project_id=_required_str(skill, "project_id"),
            skill_id=_required_str(skill, "id"),
            project_skill_revision=revision,
            occurred_at=_required_str(publication, "created_at"),
        )
        self._records.delete("staging_project_skills", draft_id, expected_revision=staged.revision)
        return ProjectSkillPublicationCompositeResult(publication_id, transition_id, revision)

    def _enqueue_external_agent_publication_change(
        self,
        *,
        publication_identity: str,
        project_id: str,
        skill_id: str,
        project_skill_revision: int,
        occurred_at: str,
    ) -> None:
        """Append one project-scoped visibility event with the publication UoW.

        The outbox identity is paired with ``project_id`` by its collection, so
        the immutable publication id remains replay-safe without becoming a
        global cross-project key.  Its revision comes from the active Project
        Skill aggregate, rather than the incidental SQLite record revision.
        """

        self._enqueue_external_agent_project_skill_change(
            publication_identity=publication_identity,
            project_id=project_id,
            skill_id=skill_id,
            project_skill_revision=project_skill_revision,
            change_type="project_skill.published",
            occurred_at=occurred_at,
        )

    def _enqueue_external_agent_project_skill_change(
        self,
        *,
        publication_identity: str,
        project_id: str,
        skill_id: str,
        project_skill_revision: int,
        change_type: str,
        occurred_at: str,
    ) -> None:
        try:
            ExternalAgentPublicationChangeOutbox.enqueue(
                self._records,
                publication_identity=publication_identity,
                project_id=project_id,
                change_type=change_type,
                object_ref=f"crp://skills/{project_id}/{skill_id}",
                object_revision=f"project-skill-r{project_skill_revision}",
                occurred_at=occurred_at,
            )
        except ExternalAgentPublicationChangeError as error:
            raise ProjectSkillPublicationCompositeError(
                f"external Agent Project Skill change is invalid: {error}"
            ) from error

    def _supersede_prior_publications(
        self,
        *,
        skill_id: str,
        publication_id: str,
    ) -> None:
        for record in self._records.list("memory_publications"):
            payload = dict(record.payload)
            if (
                payload.get("object_type") != "project_skill"
                or payload.get("published_object_id") != skill_id
                or payload.get("status") != "published"
                or payload.get("id") == publication_id
            ):
                continue
            self._records.put(
                "memory_publications",
                record.object_id,
                {
                    **payload,
                    "status": "superseded",
                    "superseded_by": publication_id,
                    "superseded_at": self._now,
                },
                expected_revision=record.revision,
            )

    def rollback(self, *, publication_id: str, expected_publication_revision: int, expected_project_skill_revision: int, reason: str) -> ProjectSkillPublicationCompositeResult:
        publication = self._records.read("memory_publications", publication_id)
        if publication is None:
            raise ProjectSkillPublicationCompositeError("Project Skill publication not found")
        if publication.revision != expected_publication_revision:
            raise ProjectSkillPublicationCompositeError("Project Skill publication revision conflicted")
        payload = dict(publication.payload)
        if payload.get("status") == "rolled_back":
            return self._rollback_replay(
                payload,
                publication.revision,
                expected_project_skill_revision,
                reason,
            )
        if payload.get("status") != "published":
            raise ProjectSkillPublicationCompositeError("Project Skill publication is not published")
        project_id = _required_str(payload, "project_id") if "project_id" in payload else _required_str(payload, "published_project_id")
        current = self._aggregate.load(project_id)
        if current is None or _required_int(current, "revision") != expected_project_skill_revision:
            raise ProjectSkillPublicationCompositeError("Project Skill revision conflicted")
        if current.get("status") != "active" or current.get("id") != payload.get("published_object_id"):
            raise ProjectSkillPublicationCompositeError("Project Skill publication evidence drifted")
        markdown = self._aggregate.markdown(project_id)
        if markdown is None:
            raise ProjectSkillPublicationCompositeError("Project Skill markdown is missing")
        rolled_back = dict(current)
        rolled_back["status"] = "rolled_back"
        rolled_back["trust_status"] = "system_generated"
        try:
            skill = self._aggregate.save(ProjectSkillUpdate(
                project_id,
                markdown,
                rolled_back,
                expected_project_skill_revision,
                reason,
                transition_kind="ai_publication_rollback",
                actor="user",
                confirmation_kind="publication_rollback_confirmation",
            ))
        except (ProjectSkillExpectedRevisionError, ProjectSkillRepositoryError) as error:
            raise ProjectSkillPublicationCompositeError(f"Project Skill aggregate rejected rollback: {error}") from error
        transition_id = f"transition-project-skill-rollback-{publication_id}"
        transition = _transition(transition_id, _required_str(skill, "id"), "user_confirmed", "system_generated", expected_project_skill_revision, _required_int(skill, "revision"), reason, _refs(payload, "source_refs"), self._now)
        updated = {**payload, "status": "rolled_back", "rollback_reason": reason, "rolled_back_by": "user", "rolled_back_at": self._now, "rollback_transition_ref": f"crp://{self._namespace_id}/memory-transitions/{transition_id}.json"}
        _append(self._records, "memory_transitions", transition_id, transition)
        self._records.put("memory_publications", publication_id, updated, expected_revision=publication.revision)
        self._enqueue_external_agent_project_skill_change(
            publication_identity=transition_id,
            project_id=project_id,
            skill_id=_required_str(skill, "id"),
            project_skill_revision=_required_int(skill, "revision"),
            change_type="project_skill.invalidated",
            occurred_at=self._now,
        )
        return ProjectSkillPublicationCompositeResult(
            publication_id,
            transition_id,
            _required_int(skill, "revision"),
            publication_revision=publication.revision + 1,
        )

    def commit(self) -> ProjectSkillPublicationCompositeCommit:
        return ProjectSkillPublicationCompositeCommit(self._records.commit(), self._replayed)

    def _publish_replay(self, draft: dict[str, object], publication_id: str, transition_id: str) -> ProjectSkillPublicationCompositeResult:
        publication = self._records.read("memory_publications", publication_id)
        transition = self._records.read("memory_transitions", transition_id)
        current = self._aggregate.load(_required_str(draft, "project_id"))
        if publication is None or transition is None or current is None or publication.payload.get("draft_digest") != draft.get("draft_digest") or publication.payload.get("status") != "published" or current.get("id") != draft.get("skill_id") or current.get("status") != "active":
            raise ProjectSkillPublicationCompositeError("Project Skill publication replay evidence conflicts")
        revision = _required_int(current, "revision")
        if revision != _required_int(publication.payload, "published_revision"):
            raise ProjectSkillPublicationCompositeError("Project Skill publication replay revision conflicts")
        self._enqueue_external_agent_publication_change(
            publication_identity=publication_id,
            project_id=_required_str(current, "project_id"),
            skill_id=_required_str(current, "id"),
            project_skill_revision=revision,
            occurred_at=_required_str(publication.payload, "created_at"),
        )
        self._replayed = True
        return ProjectSkillPublicationCompositeResult(publication_id, transition_id, revision, True)

    def _rollback_replay(self, publication: Mapping[str, object], publication_revision: int, expected_revision: int, reason: str) -> ProjectSkillPublicationCompositeResult:
        project_id = _required_str(publication, "project_id") if "project_id" in publication else _required_str(publication, "published_project_id")
        current = self._aggregate.load(project_id)
        transition_id = f"transition-project-skill-rollback-{_required_str(publication, 'id')}"
        if current is None or current.get("status") != "rolled_back" or _required_int(current, "revision") != expected_revision + 1 or publication.get("rollback_reason") != reason or self._records.read("memory_transitions", transition_id) is None:
            raise ProjectSkillPublicationCompositeError("Project Skill rollback replay evidence conflicts")
        self._enqueue_external_agent_project_skill_change(
            publication_identity=transition_id,
            project_id=project_id,
            skill_id=_required_str(current, "id"),
            project_skill_revision=_required_int(current, "revision"),
            change_type="project_skill.invalidated",
            occurred_at=_required_str(publication, "rolled_back_at"),
        )
        self._replayed = True
        return ProjectSkillPublicationCompositeResult(
            _required_str(publication, "id"),
            transition_id,
            _required_int(current, "revision"),
            True,
            publication_revision,
        )


def _draft(value: Mapping[str, object]) -> dict[str, object]:
    try:
        return validate_project_skill_publication_draft(value)
    except ProjectSkillPublicationDraftError as error:
        raise ProjectSkillPublicationCompositeError(str(error)) from error


def _append(records: SQLiteStructuredRecordUnitOfWork, collection: str, object_id: str, payload: Mapping[str, object]) -> None:
    if payload.get("id") != object_id:
        raise ProjectSkillPublicationCompositeError("append-only audit id does not match payload")
    existing = records.read(collection, object_id)
    if existing is not None:
        if dict(existing.payload) != dict(payload):
            raise ProjectSkillPublicationCompositeError("append-only audit evidence conflicts")
        return
    records.put(collection, object_id, payload, expected_revision=0)


def _transition(transition_id: str, skill_id: str, from_trust: str, to_trust: str, from_revision: int, to_revision: int, reason: str, source_refs: list[dict[str, object]], now: str) -> dict[str, object]:
    return {"schema_version": "1.0.0", "id": transition_id, "object_type": "project_skill", "object_id": skill_id, "transition_type": "confirm" if to_trust == "user_confirmed" else "demote", "from_trust_status": from_trust, "to_trust_status": to_trust, "from_revision": from_revision, "to_revision": to_revision, "actor": "user", "reason": reason, "evidence_refs": [{"object_type": "project_skill", "object_id": skill_id, "source_refs": source_refs}], "created_at": now}


def _mapping(mapping: Mapping[str, object], key: str) -> dict[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ProjectSkillPublicationCompositeError(f"{key} is required")
    return dict(value)


def _refs(mapping: Mapping[str, object], key: str) -> list[dict[str, object]]:
    value = mapping.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(item, Mapping) for item in value):
        raise ProjectSkillPublicationCompositeError(f"{key} is required")
    return [dict(item) for item in value]


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ProjectSkillPublicationCompositeError(f"{key} is required")
    return value


def _required_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProjectSkillPublicationCompositeError(f"{key} must be a non-negative integer")
    return value
