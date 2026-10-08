"""Recognition lifecycle on the existing structured SQLite authority.

Records are deliberately small JSON payloads in ``SQLiteStructuredRecordStore``.
The store supplies WAL, one short write transaction, and mandatory CAS; this
module supplies the product rules that a database cannot infer: work scope,
manual review, provenance validity, and recognition restructuring.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import UUID, uuid4

from core.storage_provider import (
    SQLiteStructuredRecord,
    SQLiteStructuredRecordStore,
    SQLiteStructuredRecordUnitOfWork,
    SQLiteUnitOfWorkConflict,
)
from .provenance import ExperienceProvenance, ExperienceProvenanceError
from .document_filings import read_filed_edit_dependencies, DocumentFilingError
from .artifact_dependencies import ArtifactDependencyError, read_artifact_dependencies
from .import_evidence import unresolved_import_evidence
from .experience_origins import COPY_ID, ExperienceOriginError, read_experience_origin
from .product_draft_dependencies import read_product_draft_dependencies, ProductDraftDependencyError
from .external_input_dependencies import read_external_input_dependencies, ExternalInputDependencyError


_EXPERIENCES = "recognition_experiences"
_CANDIDATES = "recognition_candidates"
_RECOGNITIONS = "recognitions"
_VERSIONS = "recognition_versions"
_RELATIONS = "recognition_relations"
_QUESTIONS = "recognition_questions"
MAX_CONTENT_CHARS = 100_000
# A consumption projection has the same node-scale ceiling as evidence traversal,
# plus a byte ceiling because occurrence timestamps are not length-bounded in storage.
MAX_SOURCE_EVIDENCE_ITEMS = 256
MAX_SOURCE_EVIDENCE_BYTES = 64 * 1024
CANDIDATE_GENERATION_STEP_VERSION = "candidate-from-experiences-v1"
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_MARKDOWN_META = re.compile(r"\A<!-- recognition:(\{.*\}) -->\n\n# Recognition\n\n", re.DOTALL)
_MARKDOWN_SECTIONS = re.compile(
    r"\A## Project\n(?P<project>[^\n]+)\n\n"
    r"## Recognition ID\n(?P<recognition_id>[^\n]+)\n\n"
    r"## Base revision\n(?P<revision>[^\n]+)\n\n"
    r"## Source references\n(?P<sources>(?:- [^\n]+\n)*)\n"
    r"## Conditions\n(?P<conditions>(?:- [^\n]+\n)*)\n?"
    r"## Content\n\n",
)
_MARKDOWN_SOURCE = re.compile(r"- `(experience|recognition):([^`]+)` \(revision: ([0-9]+)\)\n")


class RecognitionError(ValueError):
    """The request violates the recognition lifecycle contract."""


class RecognitionConflict(RecognitionError):
    """The requested write is based on stale state or invalid provenance."""


@dataclass(frozen=True, slots=True)
class WorkScope:
    user_id: str
    project_id: str | None = None

    def __post_init__(self) -> None:
        _segment("user_id", self.user_id)
        if self.project_id is not None:
            _segment("project_id", self.project_id)

    @property
    def key(self) -> str:
        return f"{self.user_id}~{self.project_id or 'personal'}"


@dataclass(frozen=True, slots=True)
class RecognitionCandidate:
    candidate_id: str
    revision: int
    scope: WorkScope
    content: str
    source_experience_ids: tuple[str, ...]
    source_recognition_ids: tuple[str, ...]
    source_experience_revisions: Mapping[str, int]
    source_recognition_revisions: Mapping[str, int]
    state: str
    conditions: tuple[str, ...] = ()
    generation: Mapping[str, object] | None = None

    @property
    def id(self) -> str:
        return self.candidate_id

    @property
    def project_id(self) -> str | None:
        return self.scope.project_id


@dataclass(frozen=True, slots=True)
class Recognition:
    recognition_id: str
    revision: int
    scope: WorkScope
    content: str
    state: str
    source_experience_ids: tuple[str, ...]
    source_recognition_ids: tuple[str, ...]
    parent_ids: tuple[str, ...]
    source_experience_revisions: Mapping[str, int]
    source_recognition_revisions: Mapping[str, int]
    conditions: tuple[str, ...]
    evidence_eligible: bool = False
    evidence_reason: str | None = "source evidence has not been checked"
    source_evidence: tuple[Mapping[str, object], ...] = ()
    source_evidence_complete: bool = False
    source_evidence_reason: str | None = "source_evidence_unavailable"

    @property
    def id(self) -> str:
        return self.recognition_id

    @property
    def project_id(self) -> str | None:
        return self.scope.project_id

    @property
    def status(self) -> str:
        return self.effective_state

    @property
    def effective_state(self) -> str:
        return "stale" if self.state == "active" and not self.evidence_eligible else self.state

    @property
    def current_revision(self) -> int:
        return self.revision

    @property
    def authorized(self) -> bool:
        return self.state == "active" and self.evidence_eligible

    @property
    def source_refs(self) -> tuple[dict[str, object], ...]:
        return tuple(
            [
                _source_ref("experience", item, self.source_experience_revisions.get(item))
                for item in self.source_experience_ids
            ]
            + [
                _source_ref("recognition", item, self.source_recognition_revisions.get(item))
                for item in self.source_recognition_ids
            ]
        )

    def retrieval_projection(self) -> dict[str, object]:
        """Stable, database-independent input for the retrieval adapter."""
        return {
            "id": self.id,
            "revision": self.revision,
            "current_revision": self.current_revision,
            "project_id": self.project_id,
            "content": self.content,
            "status": self.status,
            "recorded_state": self.state,
            "evidence_eligible": self.evidence_eligible,
            "evidence_reason": self.evidence_reason,
            "authorized": self.authorized,
            "source_refs": list(self.source_refs),
            "source_experience_ids": list(self.source_experience_ids),
            "source_recognition_ids": list(self.source_recognition_ids),
            "conditions": list(self.conditions),
            "source_evidence": [dict(item) for item in self.source_evidence],
            "source_evidence_complete": self.source_evidence_complete,
            "source_evidence_reason": self.source_evidence_reason,
        }


@dataclass(frozen=True, slots=True)
class Experience:
    experience_id: str
    revision: int
    scope: WorkScope
    content: str
    state: str
    provenance: ExperienceProvenance

    @property
    def id(self) -> str:
        return self.experience_id

    @property
    def project_id(self) -> str | None:
        return self.scope.project_id


@dataclass(frozen=True, slots=True)
class FixedQuestion:
    question_id: str
    revision: int
    scope: WorkScope
    question: str
    content: str
    recognition_ids: tuple[str, ...]
    source_revisions: Mapping[str, int]
    state: str
    evidence_eligible: bool = False
    evidence_reason: str | None = "source evidence has not been checked"
    updated_at: str = ""

    @property
    def effective_state(self) -> str:
        return "stale" if self.state == "current" and not self.evidence_eligible else self.state

    @property
    def status(self) -> str:
        return self.effective_state

    @property
    def id(self) -> str:
        return self.question_id

    @property
    def project_id(self) -> str | None:
        return self.scope.project_id


@dataclass(frozen=True, slots=True)
class MarkdownPreview:
    recognition_id: str
    expected_revision: int
    content: str
    conditions: tuple[str, ...]
    changed: bool


class RecognitionService:
    """The recognition write boundary for a FastAPI thin route.

    The caller supplies selected experience and candidate wording. No method
    invokes a model or auto-promotes a candidate.
    """

    def __init__(self, records: SQLiteStructuredRecordStore, *, cache_invalidation=None,
                 product_draft_validator=None, _external_input_validator=None) -> None:
        if product_draft_validator is not None and not callable(product_draft_validator):
            raise ValueError('product draft validator must be callable')
        self._records = records
        self.cache_invalidation = cache_invalidation
        self.product_draft_validator = product_draft_validator
        self._external_input_validator = _external_input_validator

    def _prepare_cache_invalidation(self, uow, scope, identities):
        if self.cache_invalidation is not None:
            return self.cache_invalidation(uow, scope, identities)
        return self._cascade_invalidate(uow, scope,
            changed_experience_ids={identity for kind, identity in identities if kind == 'experience'},
            changed_recognition_ids={identity for kind, identity in identities if kind == 'recognition'},
            _cache_plan=True)

    @property
    def records(self) -> SQLiteStructuredRecordStore:
        return self._records

    def stage_experience(
        self,
        *,
        scope: WorkScope,
        content: str,
        experience_id: str | None = None,
        provenance: Mapping[str, object] | None = None,
        copy_from: Mapping[str, object] | None = None,
    ) -> str:
        experience_id = experience_id or _new_id('experience-copy-v2' if copy_from is not None else 'experience')
        _segment("experience_id", experience_id)
        if bool(COPY_ID.fullmatch(experience_id)) != (copy_from is not None):
            raise RecognitionError('retained experience copy identity is invalid')
        recorded_at = _now()
        original = None
        if copy_from is not None:
            if (not isinstance(copy_from, Mapping) or set(copy_from) != {'project_id', 'experience_id', 'revision'}
                    or copy_from['project_id'] == scope.project_id):
                raise RecognitionError('experience copy identity is invalid')
            own = WorkScope(scope.user_id, copy_from['project_id'])
            original = self._require_scope_read(_EXPERIENCES, copy_from['experience_id'], own)
            if original.revision != _revision(copy_from['revision']) or original.payload['content'] != _content(content):
                raise RecognitionConflict('experience copy source changed')
            with self._records.begin() as reader:
                self._assert_sources_active(reader, own, [original.object_id], (),
                    experience_revisions={original.object_id: original.revision})
            provenance = original.payload['provenance']
        try:
            source = (
                ExperienceProvenance.legacy(recorded_at=recorded_at)
                if provenance is None
                else ExperienceProvenance.from_payload(provenance,
                    recorded_at=provenance['recorded_at'] if original else recorded_at)
            )
        except ExperienceProvenanceError as exc:
            raise RecognitionError(str(exc)) from exc
        payload = {
            "id": experience_id, "scope": _scope(scope), "project_id": scope.project_id, "content": _content(content),
            "state": "active", "created_at": recorded_at, "provenance": source.to_payload(), "revoked_at": None,
        }
        if original is None:
            self._put_new(_EXPERIENCES, experience_id, payload)
        else:
            with self._records.begin() as uow:
                current = self._require_scope(uow, _EXPERIENCES, original.object_id, own)
                if current != original:
                    raise RecognitionConflict('experience copy source changed')
                self._assert_sources_active(uow, own, [original.object_id], ())
                self._ensure_not_erased(uow, experience_id)
                uow.put(_EXPERIENCES, experience_id, payload, expected_revision=0)
                uow.put('v2_experience_origins', experience_id, {
                    'source_user_id': scope.user_id, 'source_project_id': own.project_id,
                    'source_experience_id': original.object_id, 'source_revision': original.revision,
                    'target_project_id': scope.project_id, 'target_experience_id': experience_id}, expected_revision=0)
                uow.commit()
        return experience_id

    def list_experiences(self, *, scope: WorkScope, include_revoked: bool = False) -> tuple[Experience, ...]:
        return tuple(
            _experience(record) for record in self._records.list(_EXPERIENCES)
            if _as_scope(record.payload) == scope and (include_revoked or record.payload.get("state") == "active")
        )

    def read_candidate_experiences(self, *, scope: WorkScope, experience_ids: Sequence[str]) -> tuple[Experience, ...]:
        """Read eligible candidate inputs together before any model side effect."""
        ids = _ids(experience_ids, "experience_ids")
        with self._evidence_reader() as reader:
            self._assert_sources_active(reader, scope, ids, ())
            return tuple(_experience(self._require_scope(reader, _EXPERIENCES, item_id, scope)) for item_id in ids)

    def assert_frozen_sources_in_uow(self, reader, scope, experience_ids, recognition_ids,
            experience_revisions, recognition_revisions):
        """Reuse publication's strict snapshots before a caller expands evidence."""
        experiences = _ids(experience_ids, 'source_experience_ids')
        recognitions = _ids(recognition_ids, 'source_recognition_ids')
        self._assert_sources_active(reader, scope, experiences, recognitions,
            experience_revisions=_revision_map(experience_revisions, experiences),
            recognition_revisions=_revision_map(recognition_revisions, recognitions))

    def revoke_experience(self, *, scope: WorkScope, experience_id: str, expected_revision: int) -> None:
        """Revoke a source and invalidate every pending proposal that cites it."""
        with self._records.begin() as uow:
            record = self._require_scope(uow, _EXPERIENCES, experience_id, scope)
            self._expect(record, expected_revision)
            if record.payload["state"] == "revoked":
                raise RecognitionConflict("experience is already revoked")
            updated = {**dict(record.payload), "state": "revoked", "revoked_at": _now()}
            apply_cache = self._prepare_cache_invalidation(uow, scope, (("experience", experience_id),))
            uow.put(_EXPERIENCES, experience_id, updated, expected_revision=record.revision)
            self._cascade_invalidate(uow, scope, changed_experience_ids={experience_id})
            apply_cache()
            uow.commit()

    def propose(
        self,
        *,
        scope: WorkScope,
        content: str,
        source_experience_ids: Sequence[str],
        source_recognition_ids: Sequence[str] = (),
        conditions: Sequence[str] = (),
        candidate_id: str | None = None,
        generation: Mapping[str, object] | None = None,
    ) -> RecognitionCandidate:
        """Create a reviewable candidate; this intentionally does not publish."""
        with self._records.begin() as uow:
            candidate = self.propose_in_uow(uow, scope=scope, content=content,
                source_experience_ids=source_experience_ids, source_recognition_ids=source_recognition_ids,
                conditions=conditions, candidate_id=candidate_id, generation=generation)
            uow.commit()
        return candidate

    def propose_in_uow(
        self, uow, *, scope: WorkScope, content: str, source_experience_ids: Sequence[str],
        source_recognition_ids: Sequence[str] = (), conditions: Sequence[str] = (),
        candidate_id: str | None = None, generation: Mapping[str, object] | None = None,
    ) -> RecognitionCandidate:
        """Enlist candidate creation in a caller-owned transaction; never publish."""
        candidate_id = candidate_id or _new_id("candidate")
        _segment("candidate_id", candidate_id)
        experiences = _ids(source_experience_ids, "source_experience_ids")
        recognitions = _ids(source_recognition_ids, "source_recognition_ids")
        candidate_conditions = normalize_conditions(conditions)
        candidate_generation = _candidate_generation(generation)
        experience_revisions, recognition_revisions = self._source_snapshots(
            uow, scope, experiences, recognitions
        )
        payload = {
            "id": candidate_id, "scope": _scope(scope), "project_id": scope.project_id, "content": _content(content),
            "source_experience_ids": list(experiences), "source_recognition_ids": list(recognitions),
            "source_experience_revisions": experience_revisions,
            "source_recognition_revisions": recognition_revisions,
            "conditions": list(candidate_conditions),
            "generation": candidate_generation,
            "state": "pending", "created_at": _now(), "reviewed_at": None,
        }
        try:
            record = uow.put(_CANDIDATES, candidate_id, payload, expected_revision=0)
        except SQLiteUnitOfWorkConflict as exc:
            raise RecognitionConflict("candidate id already exists") from exc
        return _candidate(record)

    def edit_candidate(
        self,
        *,
        scope: WorkScope,
        candidate_id: str,
        expected_revision: int,
        content: str,
        conditions: Sequence[str] | None = None,
        editor: str = "local-user",
    ) -> RecognitionCandidate:
        """Amend a pending proposal without replacing its captured sources.

        Candidate wording is deliberately mutable before review, while its
        provenance snapshot is not.  This prevents editing from making an
        out-of-date proposal appear newly grounded.
        """
        _segment("editor", editor)
        updated_content = _content(content)
        with self._records.begin() as uow:
            candidate = self._require_scope(uow, _CANDIDATES, candidate_id, scope)
            self._expect(candidate, expected_revision)
            if uow.read("v2_candidate_merges", candidate_id):
                raise RecognitionConflict("candidate has already been merged")
            if candidate.payload.get("state") != "pending":
                raise RecognitionConflict("only a pending candidate can be edited")
            experiences = _ids(candidate.payload["source_experience_ids"], "source_experience_ids")
            recognitions = _ids(candidate.payload["source_recognition_ids"], "source_recognition_ids")
            self._assert_sources_active(
                uow,
                scope,
                experiences,
                recognitions,
                experience_revisions=_revision_map(candidate.payload.get("source_experience_revisions"), experiences),
                recognition_revisions=_revision_map(candidate.payload.get("source_recognition_revisions"), recognitions),
            )
            current_conditions = normalize_conditions(candidate.payload.get("conditions", ()))
            updated_conditions = current_conditions if conditions is None else normalize_conditions(conditions)
            if updated_content == candidate.payload["content"] and updated_conditions == current_conditions:
                return _candidate(candidate)
            payload = {
                **dict(candidate.payload),
                "content": updated_content,
                "conditions": list(updated_conditions),
                "updated_at": _now(),
                "updated_by": editor,
            }
            if "initial_content" not in payload:
                payload["initial_content"] = candidate.payload["content"]
                payload["initial_conditions"] = list(current_conditions)
            record = uow.put(_CANDIDATES, candidate_id, payload, expected_revision=candidate.revision)
            from .correction_events import record_correction
            record_correction(uow, candidate, record, object_kind='candidate')
            uow.commit()
        return _candidate(record)

    def list_candidates(self, *, scope: WorkScope, include_inactive: bool = False) -> tuple[RecognitionCandidate, ...]:
        return tuple(
            _candidate(record) for record in self._records.list(_CANDIDATES)
            if _as_scope(record.payload) == scope and (include_inactive or (record.payload.get("state") == "pending"
                and self._records.read("v2_candidate_merges", record.object_id) is None))
        )

    def reject_candidate(self, *, scope: WorkScope, candidate_id: str, expected_revision: int, reviewer: str) -> RecognitionCandidate:
        _segment("reviewer", reviewer)
        with self._records.begin() as uow:
            candidate = self._require_scope(uow, _CANDIDATES, candidate_id, scope)
            self._expect(candidate, expected_revision)
            if uow.read("v2_candidate_merges", candidate_id):
                raise RecognitionConflict("candidate has already been merged")
            if candidate.payload.get("state") != "pending":
                raise RecognitionConflict("only a pending candidate can be rejected")
            record = uow.put(
                _CANDIDATES, candidate_id,
                {**dict(candidate.payload), "state": "rejected", "reviewed_at": _now(), "reviewed_by": reviewer},
                expected_revision=candidate.revision,
            )
            from .correction_events import record_correction
            record_correction(uow, candidate, record, object_kind='candidate', event_type='reject', after='')
            uow.commit()
        return _candidate(record)

    def publish(
        self,
        *,
        scope: WorkScope,
        candidate_id: str,
        expected_revision: int,
        reviewer: str,
        recognition_id: str | None = None,
    ) -> Recognition:
        """Publish one pending candidate after a named user review."""
        _segment("reviewer", reviewer)
        recognition_id = recognition_id or _new_id("recognition")
        _segment("recognition_id", recognition_id)
        with self._records.begin() as uow:
            candidate = self._require_scope(uow, _CANDIDATES, candidate_id, scope)
            self._expect(candidate, expected_revision)
            if uow.read("v2_candidate_merges", candidate_id):
                raise RecognitionConflict("candidate has already been merged")
            if candidate.payload["state"] != "pending":
                raise RecognitionConflict("only a pending candidate can be published")
            experiences = _ids(candidate.payload["source_experience_ids"], "source_experience_ids")
            sources = _ids(candidate.payload["source_recognition_ids"], "source_recognition_ids")
            self._assert_sources_active(
                uow, scope, experiences, sources,
                experience_revisions=_revision_map(candidate.payload.get("source_experience_revisions"), experiences),
                recognition_revisions=_revision_map(candidate.payload.get("source_recognition_revisions"), sources),
            )
            current = {
                "id": recognition_id, "scope": _scope(scope), "project_id": scope.project_id, "content": candidate.payload["content"],
                "state": "active", "version": 1, "source_experience_ids": list(experiences),
                "source_recognition_ids": list(sources),
                "source_experience_revisions": _revision_map(candidate.payload.get("source_experience_revisions"), experiences),
                "source_recognition_revisions": _revision_map(candidate.payload.get("source_recognition_revisions"), sources),
                "parent_ids": [], "conditions": list(normalize_conditions(candidate.payload.get("conditions", ()))), "published_by": reviewer,
                "created_at": _now(), "updated_at": _now(), "revoked_at": None,
            }
            try:
                self._ensure_not_erased(uow, recognition_id)
                recognition = uow.put(_RECOGNITIONS, recognition_id, current, expected_revision=0)
            except SQLiteUnitOfWorkConflict as exc:
                raise RecognitionConflict("recognition id already exists") from exc
            self._append_version(uow, recognition, action="publish")
            uow.put("v2_insight_validity", recognition_id,
                {"valid_from": current["created_at"], "valid_until": None, "superseded_by": None},
                expected_revision=0)
            uow.put(
                _CANDIDATES, candidate_id,
                {**dict(candidate.payload), "state": "published", "reviewed_at": _now(), "recognition_id": recognition_id},
                expected_revision=candidate.revision,
            )
            result = self._qualified_recognition(uow, recognition)
            uow.commit()
        return result

    def list_recognitions(self, *, scope: WorkScope, include_inactive: bool = False) -> tuple[Recognition, ...]:
        with self._evidence_reader() as reader:
            return tuple(
                self._qualified_recognition(reader, record) for record in reader.list(_RECOGNITIONS)
                if _as_scope(record.payload) == scope and (include_inactive or record.payload.get("state") == "active")
            )

    def retrieval_entries(self, *, scope: WorkScope) -> tuple[dict[str, object], ...]:
        """Return only current, project-scoped recognitions for context retrieval."""
        return tuple(item.retrieval_projection() for item in self.list_recognitions(scope=scope) if item.authorized)

    def revise(
        self, *, scope: WorkScope, recognition_id: str, expected_revision: int, content: str,
        source_experience_ids: Sequence[str] | None = None, source_recognition_ids: Sequence[str] | None = None,
        conditions: Sequence[str] | None = None,
    ) -> Recognition:
        with self._records.begin() as uow:
            current = self._active_recognition(uow, recognition_id, scope, expected_revision)
            experiences = _ids(source_experience_ids, "source_experience_ids") if source_experience_ids is not None else _ids(current.payload["source_experience_ids"], "source_experience_ids")
            sources = _ids(source_recognition_ids, "source_recognition_ids") if source_recognition_ids is not None else _ids(current.payload["source_recognition_ids"], "source_recognition_ids")
            updated_conditions = normalize_conditions(conditions) if conditions is not None else normalize_conditions(current.payload.get("conditions", ()))
            result = self._apply_restructure_in_uow(
                uow, scope=scope, operation="revise", target_ids=[recognition_id],
                expected_revisions={recognition_id: expected_revision},
                outputs=[{"content": content, "conditions": updated_conditions,
                          "source_experience_ids": experiences, "source_recognition_ids": sources}], reason="manual revision",
                validate_parent_sources=source_experience_ids is None and source_recognition_ids is None,
            )
            uow.commit()
        return result[0]

    def revoke(self, *, scope: WorkScope, recognition_id: str, expected_revision: int, reason: str) -> Recognition:
        if not isinstance(reason, str) or not reason.strip():
            raise RecognitionError("revocation reason is required")
        with self._records.begin() as uow:
            result = self._apply_restructure_in_uow(
                uow, scope=scope, operation="revoke", target_ids=[recognition_id],
                expected_revisions={recognition_id: expected_revision}, outputs=[], reason=reason.strip(),
                validate_parent_sources=False,
            )
            uow.commit()
        return result[0]

    def split(
        self, *, scope: WorkScope, recognition_id: str, expected_revision: int,
        parts: Sequence[str | Mapping[str, object]], new_ids: Sequence[str] | None = None,
    ) -> tuple[Recognition, ...]:
        parts = tuple(parts)
        if len(parts) < 2:
            raise RecognitionError("split requires at least two parts")
        new_ids = tuple(new_ids) if new_ids is not None else tuple(_new_id("recognition") for _ in parts)
        if len(new_ids) != len(parts) or len(set(new_ids)) != len(new_ids):
            raise RecognitionError("split ids are invalid")
        for item in new_ids:
            _segment("recognition_id", item)
        with self._records.begin() as uow:
            parent = self._active_recognition(uow, recognition_id, scope, expected_revision)
            outputs: list[dict[str, object]] = []
            for child_id, part in zip(new_ids, parts, strict=True):
                content, experiences, sources, conditions = _split_part(part, parent.payload)
                outputs.append({"recognition_id": child_id, "content": content, "conditions": conditions,
                                "source_experience_ids": experiences, "source_recognition_ids": sources})
            children = self._apply_restructure_in_uow(
                uow, scope=scope, operation="split", target_ids=[recognition_id],
                expected_revisions={recognition_id: expected_revision}, outputs=outputs, reason="manual split",
            )
            uow.commit()
        return children

    def merge(
        self, *, scope: WorkScope, recognition_ids: Sequence[str], expected_revisions: Mapping[str, int],
        content: str, recognition_id: str | None = None, conditions: Sequence[str] = (),
        source_experience_ids: Sequence[str] | None = None,
        source_recognition_ids: Sequence[str] | None = None,
        replacement_conditions: Sequence[str] | None = None,
    ) -> Recognition:
        ids = _ids(recognition_ids, "recognition_ids")
        if len(ids) < 2 or set(ids) != set(expected_revisions):
            raise RecognitionError("merge ids and expected revisions are invalid")
        recognition_id = recognition_id or _new_id("recognition")
        _segment("recognition_id", recognition_id)
        with self._records.begin() as uow:
            parents = [self._active_recognition(uow, item, scope, expected_revisions[item]) for item in ids]
            source_experiences = (
                _dedupe(item for parent in parents for item in parent.payload["source_experience_ids"])
                if source_experience_ids is None
                else _ids(source_experience_ids, "source_experience_ids")
            )
            source_recognitions = (
                _dedupe(item for parent in parents for item in parent.payload["source_recognition_ids"] if item not in ids)
                if source_recognition_ids is None
                else _ids(source_recognition_ids, "source_recognition_ids")
            )
            if set(source_recognitions).intersection(ids):
                raise RecognitionError("merged parent recognitions cannot be new sources")
            if replacement_conditions is None:
                merged_conditions = _dedupe(
                    condition
                    for parent in parents
                    for condition in normalize_conditions(parent.payload.get("conditions", ()))
                )
                merged_conditions = _dedupe((*merged_conditions, *normalize_conditions(conditions)))
            else:
                merged_conditions = normalize_conditions(replacement_conditions)
            result = self._apply_restructure_in_uow(
                uow, scope=scope, operation="merge", target_ids=ids, expected_revisions=expected_revisions,
                outputs=[{"recognition_id": recognition_id, "content": content, "conditions": merged_conditions,
                          "source_experience_ids": source_experiences, "source_recognition_ids": source_recognitions}],
                reason="manual merge",
            )
            uow.commit()
        return result[0]

    def _apply_restructure_in_uow(
        self,
        uow: SQLiteStructuredRecordUnitOfWork,
        *,
        scope: WorkScope,
        operation: str,
        target_ids: Sequence[str],
        expected_revisions: Mapping[str, int],
        outputs: Sequence[Mapping[str, object]],
        reason: str,
        validate_parent_sources: bool = True,
    ) -> tuple[Recognition, ...]:
        """Apply a reviewed restructuring without opening or committing a UoW.

        ``RestructureProposalService`` owns the surrounding transaction so the
        proposal terminal state and the lifecycle mutation cannot diverge.
        The public methods above keep their stable standalone contracts.
        """
        ids = _ids(target_ids, "recognition_ids")
        if set(ids) != set(expected_revisions):
            raise RecognitionError("restructure expected revisions are invalid")
        parents = [self._active_recognition(uow, item, scope, expected_revisions[item]) for item in ids]
        if validate_parent_sources:
            for parent in parents:
                experiences = _ids(parent.payload["source_experience_ids"], "source_experience_ids")
                sources = _ids(parent.payload["source_recognition_ids"], "source_recognition_ids")
                self._assert_sources_active(
                    uow, scope, experiences, sources,
                    experience_revisions=_revision_map(parent.payload.get("source_experience_revisions"), experiences),
                    recognition_revisions=_revision_map(parent.payload.get("source_recognition_revisions"), sources),
                )
        if operation == "noop":
            return ()
        if operation == "revoke":
            if len(parents) != 1 or outputs:
                raise RecognitionError("revoke restructuring is invalid")
            parent = parents[0]
            updated = {**dict(parent.payload), "state": "revoked", "version": parent.payload["version"] + 1,
                       "revoked_at": _now(), "revocation_reason": reason, "updated_at": _now()}
            apply_cache = self._prepare_cache_invalidation(uow, scope, (("recognition", parent.object_id),))
            record = uow.put(_RECOGNITIONS, parent.object_id, updated, expected_revision=parent.revision)
            self._append_version(uow, record, action="revoke")
            self._cascade_invalidate(uow, scope, changed_recognition_ids={parent.object_id})
            apply_cache()
            return (self._qualified_recognition(uow, record),)
        if operation == "revise":
            if len(parents) != 1 or len(outputs) != 1:
                raise RecognitionError("revise restructuring is invalid")
            parent, output = parents[0], outputs[0]
            experiences = _ids(output["source_experience_ids"], "source_experience_ids")
            sources = _ids(output["source_recognition_ids"], "source_recognition_ids")
            updated_content = _content(output["content"])
            updated_conditions = normalize_conditions(output["conditions"])
            if (updated_content == parent.payload["content"]
                    and experiences == _ids(parent.payload["source_experience_ids"], "source_experience_ids")
                    and sources == _ids(parent.payload["source_recognition_ids"], "source_recognition_ids")
                    and updated_conditions == normalize_conditions(parent.payload.get("conditions", ()))):
                return (self._qualified_recognition(uow, parent),)
            snapshots = self._source_snapshots(uow, scope, experiences, sources, excluded_recognition_id=parent.object_id)
            updated = {**dict(parent.payload), "content": updated_content, "version": parent.payload["version"] + 1,
                       "source_experience_ids": list(experiences), "source_recognition_ids": list(sources),
                       "source_experience_revisions": snapshots[0], "source_recognition_revisions": snapshots[1],
                       "conditions": list(updated_conditions), "updated_at": _now()}
            apply_cache = self._prepare_cache_invalidation(uow, scope, (("recognition", parent.object_id),))
            record = uow.put(_RECOGNITIONS, parent.object_id, updated, expected_revision=parent.revision)
            self._append_version(uow, record, action="revise")
            from .correction_events import record_correction
            record_correction(uow, parent, record, object_kind='recognition')
            self._cascade_invalidate(uow, scope, changed_recognition_ids={parent.object_id})
            apply_cache()
            return (self._qualified_recognition(uow, record),)
        if operation not in {"split", "merge", "supersede"}:
            raise RecognitionError("restructure operation is invalid")
        if operation == "split" and (len(parents) != 1 or len(outputs) < 2):
            raise RecognitionError("split restructuring is invalid")
        if operation == "merge" and (len(parents) < 2 or len(outputs) != 1):
            raise RecognitionError("merge restructuring is invalid")
        if operation == "supersede" and (len(parents) != 1 or len(outputs) != 1):
            raise RecognitionError("supersede restructuring is invalid")
        child_ids = tuple(_id(item["recognition_id"], "recognition_id") for item in outputs)
        if len(child_ids) != len(set(child_ids)):
            raise RecognitionError("restructure child ids are invalid")
        apply_cache = self._prepare_cache_invalidation(uow, scope,
            tuple(("recognition", parent.object_id) for parent in parents))
        for parent in parents:
            update = {**dict(parent.payload), "state": "superseded", "version": parent.payload["version"] + 1,
                      "updated_at": _now(), "successor_ids": list(child_ids)}
            updated = uow.put(_RECOGNITIONS, parent.object_id, update, expected_revision=parent.revision)
            self._append_version(uow, updated, action=operation)
        children: list[Recognition] = []
        relation = "merged_into" if operation == "merge" else "split_into" if operation == "split" else "superseded_by"
        parent_ids = tuple(parent.object_id for parent in parents)
        for output, child_id in zip(outputs, child_ids, strict=True):
            experiences = _ids(output["source_experience_ids"], "source_experience_ids")
            sources = _ids(output["source_recognition_ids"], "source_recognition_ids")
            if set(sources).intersection(parent_ids):
                raise RecognitionError("restructured parent recognitions cannot be new sources")
            experience_revisions, recognition_revisions = self._source_snapshots(uow, scope, experiences, sources)
            payload = self._new_child(
                {"source_experience_ids": list(experiences), "source_recognition_ids": list(sources),
                 "source_experience_revisions": experience_revisions, "source_recognition_revisions": recognition_revisions,
                 "conditions": normalize_conditions(output["conditions"])},
                scope, child_id, _content(output["content"]), parent_ids,
            )
            try:
                self._ensure_not_erased(uow, child_id)
                record = uow.put(_RECOGNITIONS, child_id, payload, expected_revision=0)
            except SQLiteUnitOfWorkConflict as exc:
                raise RecognitionConflict("restructure child id already exists") from exc
            self._append_version(uow, record, action=f"{operation}-child")
            for parent_id in parent_ids:
                self._relation(uow, scope, parent_id, child_id, relation)
            children.append(self._qualified_recognition(uow, record))
        self._cascade_invalidate(uow, scope, changed_recognition_ids=set(parent_ids))
        apply_cache()
        return tuple(children)

    def upsert_question(
        self,
        *,
        scope: WorkScope,
        question_id: str,
        question: str,
        content: str,
        recognition_ids: Sequence[str],
        source_revisions: Mapping[str, int],
        expected_revision: int,
    ) -> FixedQuestion:
        """Publish a fixed-question synthesis after dependency-version checks.

        The caller may generate ``content`` outside this service, but the
        summary only becomes current inside the same SQLite transaction that
        checks every selected recognition and the question CAS revision.
        """
        _segment("question_id", question_id)
        ids = _ids(recognition_ids, "recognition_ids")
        if set(source_revisions) != set(ids):
            raise RecognitionError("question source revisions are invalid")
        snapshots = {item_id: _revision(source_revisions[item_id]) for item_id in ids}
        with self._records.begin() as uow:
            for item_id in ids:
                source = self._require_scope(uow, _RECOGNITIONS, item_id, scope)
                if source.payload.get("state") != "active" or source.revision != snapshots[item_id]:
                    raise RecognitionConflict("question dependencies changed; refresh again")
            self._assert_sources_active(uow, scope, (), ids, recognition_revisions=snapshots)
            existing = uow.read(_QUESTIONS, question_id)
            if existing is not None and _as_scope(existing.payload) != scope:
                raise RecognitionConflict("question belongs to another scope")
            actual_revision = existing.revision if existing is not None else 0
            if actual_revision != expected_revision:
                raise RecognitionConflict("question revision conflicted")
            payload = {
                "id": question_id, "scope": _scope(scope), "project_id": scope.project_id,
                "question": _content(question), "content": _content(content),
                "recognition_ids": list(ids), "source_revisions": snapshots,
                "state": "current", "updated_at": _now(),
            }
            record = uow.put(_QUESTIONS, question_id, payload, expected_revision=expected_revision)
            result = self._qualified_question(uow, record)
            uow.commit()
        return result

    def list_questions(self, *, scope: WorkScope, include_stale: bool = True) -> tuple[FixedQuestion, ...]:
        with self._evidence_reader() as reader:
            questions = (self._qualified_question(reader, record) for record in reader.list(_QUESTIONS)
                         if _as_scope(record.payload) == scope)
            return tuple(item for item in questions if include_stale or item.effective_state == "current")

    def export_markdown(self, *, scope: WorkScope, recognition_id: str) -> str:
        record = self._require_scope_read(_RECOGNITIONS, recognition_id, scope)
        return _markdown(_recognition(record))

    def markdown_preview(self, *, scope: WorkScope, markdown: str) -> MarkdownPreview:
        metadata, content, conditions, visible = _parse_markdown(markdown)
        recognition_id = _id(metadata.get("recognition_id"), "recognition_id")
        expected_revision = _revision(metadata.get("revision"))
        current = self._require_scope_read(_RECOGNITIONS, recognition_id, scope)
        self._expect(current, expected_revision)
        if visible is not None:
            _validate_markdown_identity(visible, current)
        current_conditions = normalize_conditions(current.payload.get("conditions", ()))
        imported_conditions = current_conditions if conditions is None else conditions
        return MarkdownPreview(
            recognition_id,
            expected_revision,
            content,
            imported_conditions,
            content != current.payload["content"] or imported_conditions != current_conditions,
        )

    def markdown_commit(self, *, scope: WorkScope, markdown: str) -> Recognition:
        preview = self.markdown_preview(scope=scope, markdown=markdown)
        if not preview.changed:
            with self._evidence_reader() as reader:
                record = self._require_scope(reader, _RECOGNITIONS, preview.recognition_id, scope)
                self._expect(record, preview.expected_revision)
                return self._qualified_recognition(reader, record)
        return self.revise(
            scope=scope,
            recognition_id=preview.recognition_id,
            expected_revision=preview.expected_revision,
            content=preview.content,
            conditions=preview.conditions,
        )

    def get_recognition(self, *, scope: WorkScope, recognition_id: str) -> Recognition | None:
        with self._evidence_reader() as reader:
            record = reader.read(_RECOGNITIONS, recognition_id)
            if record is None or _as_scope(record.payload) != scope:
                return None
            return self._qualified_recognition(reader, record)

    @contextmanager
    def _evidence_reader(self):
        # The store's begin() deliberately takes a writer lock. Reuse its
        # connection and UoW reader with a deferred, query-only WAL snapshot.
        connection = self._records._connect()
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            with SQLiteStructuredRecordUnitOfWork(connection) as uow:
                yield _EvidenceReader(uow)
        finally:
            connection.close()

    def _evidence_reason(self, reader, scope, recognition_ids, revisions, *, projection=None) -> str | None:
        try:
            self._assert_sources_active(reader, scope, (), recognition_ids, recognition_revisions=revisions,
                _source_evidence=projection)
        except RecognitionError as error:
            return (str(error) or "source evidence is unavailable")[:240]
        return None

    def _qualified_recognition(self, reader, record):
        item = _recognition(record)
        projection = _SourceEvidenceProjection()
        reason = self._evidence_reason(reader, item.scope, (item.id,), {item.id: item.revision}, projection=projection)
        projection_reason = "source_evidence_unavailable" if reason else projection.reason
        return replace(item, evidence_eligible=reason is None, evidence_reason=reason,
            source_evidence=projection.items if projection_reason is None else (),
            source_evidence_complete=projection_reason is None, source_evidence_reason=projection_reason)

    def _qualified_question(self, reader, record):
        item = _question(record)
        reason = self._evidence_reason(reader, item.scope, item.recognition_ids, item.source_revisions)
        return replace(item, evidence_eligible=reason is None, evidence_reason=reason)

    def _put_new(self, collection: str, object_id: str, payload: Mapping[str, object]) -> None:
        try:
            with self._records.begin() as uow:
                self._ensure_not_erased(uow, object_id)
                uow.put(collection, object_id, payload, expected_revision=0)
                uow.commit()
        except SQLiteUnitOfWorkConflict as exc:
            raise RecognitionConflict("record id already exists") from exc

    def _ensure_not_erased(self, uow, object_id):
        if uow.read("recognition_tombstones", object_id) is not None:
            raise RecognitionConflict("erased object id cannot be recreated")

    def _require_scope(self, uow: SQLiteStructuredRecordUnitOfWork, collection: str, object_id: str, scope: WorkScope) -> SQLiteStructuredRecord:
        record = uow.read(collection, object_id)
        if record is None or _as_scope(record.payload) != scope:
            raise RecognitionConflict("record is unavailable in this work scope")
        return record

    def _require_scope_read(self, collection: str, object_id: str, scope: WorkScope) -> SQLiteStructuredRecord:
        record = self._records.read(collection, object_id)
        if record is None or _as_scope(record.payload) != scope:
            raise RecognitionConflict("record is unavailable in this work scope")
        return record

    def _active_recognition(self, uow: SQLiteStructuredRecordUnitOfWork, recognition_id: str, scope: WorkScope, expected_revision: int) -> SQLiteStructuredRecord:
        record = self._require_scope(uow, _RECOGNITIONS, recognition_id, scope)
        self._expect(record, expected_revision)
        if record.payload.get("state") != "active":
            raise RecognitionConflict("recognition is no longer active")
        return record

    def _source_snapshots(
        self,
        uow: SQLiteStructuredRecordUnitOfWork,
        scope: WorkScope,
        experience_ids: Sequence[str],
        recognition_ids: Sequence[str],
        *,
        excluded_recognition_id: str | None = None,
    ) -> tuple[dict[str, int], dict[str, int]]:
        self._assert_sources_active(uow, scope, experience_ids, recognition_ids, excluded_recognition_id=excluded_recognition_id)
        return (
            {item_id: self._require_scope(uow, _EXPERIENCES, item_id, scope).revision for item_id in experience_ids},
            {item_id: self._require_scope(uow, _RECOGNITIONS, item_id, scope).revision for item_id in recognition_ids},
        )

    def _assert_sources_active(
        self,
        uow: SQLiteStructuredRecordUnitOfWork,
        scope: WorkScope,
        experience_ids: Sequence[str],
        recognition_ids: Sequence[str],
        *,
        experience_revisions: Mapping[str, int] | None = None,
        recognition_revisions: Mapping[str, int] | None = None,
        excluded_recognition_id: str | None = None,
        _evidence_path: tuple[tuple[str, str], ...] = (),
        _evidence_checked: set[tuple[str, str]] | None = None,
        _source_evidence: _SourceEvidenceProjection | None = None,
    ) -> None:
        if not isinstance(uow, _EvidenceReader):
            uow = _EvidenceReader(uow)
        checked = set() if _evidence_checked is None else _evidence_checked
        for experience_id in experience_ids:
            record = self._require_scope(uow, _EXPERIENCES, experience_id, scope)
            if record.payload.get("state") != "active":
                raise RecognitionConflict("a source experience has been revoked")
            if experience_revisions is not None and record.revision != experience_revisions.get(experience_id):
                raise RecognitionConflict("a source experience revision changed")
            if uow.has_unresolved_import_evidence(record, scope):
                raise RecognitionConflict("migration source is unavailable, changed, or cyclic")
            if _source_evidence is not None:
                _source_evidence.observe(record)
            key = ("experience", experience_id)
            if key not in checked or key in _evidence_path:
                try:
                    external = read_external_input_dependencies(uow, scope, record,
                        _sql_validator=self._external_input_validator)
                except (ExternalInputDependencyError, ValueError, OSError) as error:
                    raise RecognitionConflict('external input evidence is unavailable') from error
                if external is not None:
                    _evidence_visit_path(_evidence_path, checked, key)
                    checked.add(key)
                    continue
                try:
                    origin = read_experience_origin(uow, record)
                except ExperienceOriginError as exc:
                    raise RecognitionConflict(str(exc)) from exc
                if origin is not None:
                    marker, original = origin
                    path = _evidence_visit_path(_evidence_path, checked, key)
                    self._assert_sources_active(uow,
                        WorkScope(scope.user_id, marker.payload['source_project_id']), [original.object_id], (),
                        experience_revisions={original.object_id: original.revision},
                        _evidence_path=path, _evidence_checked=checked, _source_evidence=_source_evidence)
                    checked.add(key)
                    continue
                try:
                    filing = read_filed_edit_dependencies(uow, scope, record.payload, experience_id=record.object_id)
                except DocumentFilingError as exc:
                    raise RecognitionConflict(str(exc)) from exc
                if filing is not None:
                    original, _ = filing
                    path = _evidence_visit_path(_evidence_path, checked, key)
                    self._assert_sources_active(uow, scope, [original.object_id], (),
                        experience_revisions={original.object_id: original.revision},
                        _evidence_path=path, _evidence_checked=checked, _source_evidence=_source_evidence)
                    checked.add(key)
                    continue
                try:
                    draft = read_product_draft_dependencies(uow, scope, record.payload)
                except ProductDraftDependencyError as exc:
                    raise RecognitionConflict(str(exc)) from exc
                if draft is not None:
                    path = _evidence_visit_path(_evidence_path, checked, key)
                    if self.product_draft_validator is not None:
                        # 原装配核验整条出生闭包；本机确认不取得模型外发授权。
                        self.product_draft_validator(uow._reader, scope, record)
                    for project, kind, identity, revision in draft.roots:
                        if kind in {'original_item', 'original_source'}:
                            if self.product_draft_validator is None:
                                raise RecognitionConflict('product draft original authority is unavailable')
                            continue
                        # 经历与认识保留各自的身份空间及原递归证据约束。
                        self._assert_sources_active(uow, WorkScope(scope.user_id, project),
                            [identity] if kind == 'experience' else (),
                            [identity] if kind == 'recognition' else (),
                            experience_revisions={identity: revision} if kind == 'experience' else None,
                            recognition_revisions={identity: revision} if kind == 'recognition' else None,
                            excluded_recognition_id=excluded_recognition_id, _evidence_path=path,
                            _evidence_checked=checked, _source_evidence=_source_evidence)
                    checked.add(key)
                    continue
                try:
                    artifact = read_artifact_dependencies(uow, scope, record.payload)
                except ArtifactDependencyError as exc:
                    raise RecognitionConflict(str(exc)) from exc
                if artifact is not None:
                    path = _evidence_visit_path(_evidence_path, checked, key)
                    # Retention remains historical fact. Only its eligibility
                    # to ground a new/edited/published conclusion is checked.
                    self._assert_sources_active(uow, scope, (), [item for _, item, _ in artifact.roots],
                        recognition_revisions={item: revision for _, item, revision in artifact.roots},
                        excluded_recognition_id=excluded_recognition_id,
                        _evidence_path=path, _evidence_checked=checked, _source_evidence=_source_evidence)
                    checked.add(key)
        for recognition_id in recognition_ids:
            if recognition_id == excluded_recognition_id:
                raise RecognitionError("a recognition cannot cite itself")
            record = self._require_scope(uow, _RECOGNITIONS, recognition_id, scope)
            if record.payload.get("state") != "active":
                raise RecognitionConflict("a source recognition is not active")
            if recognition_revisions is not None and record.revision != recognition_revisions.get(recognition_id):
                raise RecognitionConflict("a source recognition revision changed")
            key = ("recognition", recognition_id)
            path = _evidence_visit_path(_evidence_path, checked, key)
            if path is not None:
                payload = record.payload
                experiences = _ids(payload["source_experience_ids"], "source_experience_ids")
                recognitions = _ids(payload["source_recognition_ids"], "source_recognition_ids")
                self._assert_sources_active(uow, scope, experiences, recognitions,
                    experience_revisions=_revision_map(payload.get("source_experience_revisions"), experiences),
                    recognition_revisions=_revision_map(payload.get("source_recognition_revisions"), recognitions),
                    excluded_recognition_id=excluded_recognition_id,
                    _evidence_path=path, _evidence_checked=checked, _source_evidence=_source_evidence)
                checked.add(key)

    def _append_version(self, uow: SQLiteStructuredRecordUnitOfWork, record: SQLiteStructuredRecord, *, action: str) -> None:
        version = _revision(record.payload.get("version"))
        object_id = f"{record.object_id}~v{version}"
        uow.put(_VERSIONS, object_id, {"id": object_id, "recognition_id": record.object_id, "recognition_revision": record.revision, "version": version, "action": action, "snapshot": dict(record.payload), "recorded_at": _now()}, expected_revision=0)

    def _new_child(self, parent: Mapping[str, object], scope: WorkScope, recognition_id: str, content: str, parent_ids: Sequence[str]) -> dict[str, object]:
        return {"id": recognition_id, "scope": _scope(scope), "project_id": scope.project_id, "content": content, "state": "active", "version": 1,
                "source_experience_ids": list(_ids(parent["source_experience_ids"], "source_experience_ids")),
                "source_recognition_ids": list(_ids(parent["source_recognition_ids"], "source_recognition_ids")),
                "source_experience_revisions": _revision_map(parent.get("source_experience_revisions"), _ids(parent["source_experience_ids"], "source_experience_ids")),
                "source_recognition_revisions": _revision_map(parent.get("source_recognition_revisions"), _ids(parent["source_recognition_ids"], "source_recognition_ids")),
                "parent_ids": list(_ids(parent_ids, "parent_ids")), "conditions": list(normalize_conditions(parent.get("conditions", ()))), "published_by": "user", "created_at": _now(), "updated_at": _now(), "revoked_at": None}

    def _relation(self, uow: SQLiteStructuredRecordUnitOfWork, scope: WorkScope, from_id: str, to_id: str, relation: str) -> None:
        relation_id = _new_id("relation")
        uow.put(_RELATIONS, relation_id, {"id": relation_id, "scope": _scope(scope), "project_id": scope.project_id, "from_id": from_id, "to_id": to_id, "relation": relation, "created_at": _now()}, expected_revision=0)

    def _invalidate_questions(self, uow: SQLiteStructuredRecordUnitOfWork, scope: WorkScope, changed_ids: set[str]) -> None:
        for question in uow.list(_QUESTIONS):
            if _as_scope(question.payload) == scope and question.payload.get("state") == "current" and changed_ids.intersection(question.payload.get("recognition_ids", [])):
                uow.put(_QUESTIONS, question.object_id, {**dict(question.payload), "state": "stale", "stale_at": _now()}, expected_revision=question.revision)

    def _cascade_invalidate(
        self,
        uow: SQLiteStructuredRecordUnitOfWork,
        scope: WorkScope,
        *,
        changed_experience_ids: set[str] | None = None,
        changed_recognition_ids: set[str] | None = None,
        _cache_plan: bool = False,
    ):
        """Mark derived state stale without deleting any historical evidence."""
        experiences = set(changed_experience_ids or ())
        recognitions = set(changed_recognition_ids or ())
        artifact_roots = {}
        for record in uow.list(_EXPERIENCES):
            if (record.payload.get("scope") != _scope(scope) or record.payload.get("project_id") != scope.project_id
                    or record.payload.get("state") != "active"):
                continue
            try:
                artifact = read_artifact_dependencies(uow, scope, record.payload)
            except ArtifactDependencyError:
                # An unverified link is not evidence of a dependency. It also
                # cannot pass new proposal/publication or egress validation.
                continue
            if artifact is not None:
                artifact_roots[record.object_id] = {item for _, item, _ in artifact.roots}
        while True:
            newly_stale: set[str] = set()
            experiences.update(item for item, roots in artifact_roots.items() if roots & recognitions)
            for candidate in uow.list(_CANDIDATES):
                payload = candidate.payload
                if _as_scope(payload) != scope or payload.get("state") != "pending":
                    continue
                if not _cache_plan and (experiences.intersection(payload.get("source_experience_ids", ())) or recognitions.intersection(payload.get("source_recognition_ids", ()))):
                    uow.put(_CANDIDATES, candidate.object_id, {**dict(payload), "state": "invalidated", "invalidated_at": _now()}, expected_revision=candidate.revision)
            for recognition in uow.list(_RECOGNITIONS):
                payload = recognition.payload
                if _as_scope(payload) != scope or payload.get("state") != "active" or recognition.object_id in recognitions:
                    continue
                if experiences.intersection(payload.get("source_experience_ids", ())) or recognitions.intersection(payload.get("source_recognition_ids", ())):
                    update = {**dict(payload), "state": "stale", "version": payload["version"] + 1, "updated_at": _now(), "stale_reason": "a source changed or was revoked"}
                    if not _cache_plan:
                        updated = uow.put(_RECOGNITIONS, recognition.object_id, update, expected_revision=recognition.revision)
                        self._append_version(uow, updated, action="source-invalidated")
                    newly_stale.add(recognition.object_id)
            if not newly_stale:
                break
            recognitions.update(newly_stale)
        if _cache_plan:
            from core.search_and_recall.vector_cache_invalidation import VectorCacheInvalidator, vector_cache_path
            invalidator = VectorCacheInvalidator(vector_cache_path(uow))
            identities = tuple(sorted(recognitions))
            return lambda: invalidator.recognitions(scope.project_id, identities)
        self._invalidate_questions(uow, scope, recognitions)

    @staticmethod
    def _expect(record: SQLiteStructuredRecord, expected_revision: int) -> None:
        if record.revision != expected_revision:
            raise RecognitionConflict("record revision conflicted")


class _SourceEvidenceProjection:
    """Bounded metadata collected during the existing source qualification walk.

    This is not stored and never changes publication or source eligibility. A
    failed projection stays empty while qualification continues independently.
    """

    def __init__(self):
        self._items: dict[tuple[str, int], dict[str, object]] = {}
        self._bytes = 2  # JSON array brackets
        self.reason: str | None = None

    @property
    def items(self) -> tuple[Mapping[str, object], ...]:
        return tuple(self._items[key] for key in sorted(self._items))

    def observe(self, record: SQLiteStructuredRecord) -> None:
        key = (record.object_id, record.revision)
        if self.reason is not None or key in self._items:
            return
        if len(self._items) >= MAX_SOURCE_EVIDENCE_ITEMS:
            self._fail("source_evidence_item_limit")
            return
        try:
            provenance = _experience(record).provenance
            item = {"type": "experience", "id": record.object_id, "revision": record.revision,
                "kind": provenance.kind, "epistemic_status": provenance.epistemic_status,
                "recorded_at": provenance.recorded_at, "occurred_at": provenance.occurred_at,
                "artifact_status": provenance.artifact_status, "outcome_status": provenance.outcome_status}
            size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except (RecognitionError, TypeError, ValueError, UnicodeError):
            self._fail("source_evidence_invalid_metadata")
            return
        total = self._bytes + size + bool(self._items)
        if total > MAX_SOURCE_EVIDENCE_BYTES:
            self._fail("source_evidence_byte_limit")
            return
        self._items[key] = item
        self._bytes = total

    def _fail(self, reason: str) -> None:
        self._items.clear()
        self.reason = reason


class _EvidenceReader:
    """Request-local reads; no writes and no cache survives a read snapshot."""

    def __init__(self, reader):
        self._reader = reader
        self._records = {}
        self._collections = {}
        self._unresolved_imports = None

    @property
    def connection(self):
        return self._reader.connection

    @property
    def database_path(self):
        path = getattr(self._reader, 'database_path', None)
        return path if path is not None else next(row[2] for row in
            self._reader.connection.execute('PRAGMA database_list') if row[1] == 'main')

    def read(self, collection, object_id):
        key = (collection, object_id)
        if key not in self._records:
            self._records[key] = self._reader.read(collection, object_id)
        return self._records[key]

    def list(self, collection):
        if collection not in self._collections:
            records = self._reader.list(collection)
            self._collections[collection] = records
            self._records.update(((collection, row.object_id), row) for row in records)
        return self._collections[collection]

    def has_unresolved_import_evidence(self, record, scope):
        provenance = record.payload.get("provenance")
        if not isinstance(provenance, Mapping) or not provenance.get("source_refs"):
            return False
        if self._unresolved_imports is None:
            self._unresolved_imports = set()
            # Existing durable import receipts identify the unresolved input.
            # A descriptive kind or placeholder spelling is not authority.
            for payload, target_id, _ in unresolved_import_evidence(self):
                imported_scope = payload["scope"]
                self._unresolved_imports.add((imported_scope.get("user_id"),
                    imported_scope.get("project_id"), target_id))
        return (scope.user_id, scope.project_id, record.object_id) in self._unresolved_imports


def _evidence_visit_path(path, checked, key):
    if key in path:
        raise RecognitionConflict("source evidence contains a cycle")
    if key in checked:
        return None
    if len(path) + len(checked) >= 256:
        raise RecognitionConflict("source evidence is too large")
    return (*path, key)


def _recognition(record: SQLiteStructuredRecord) -> Recognition:
    payload = record.payload
    experience_ids = _ids(payload["source_experience_ids"], "source_experience_ids")
    recognition_ids = _ids(payload["source_recognition_ids"], "source_recognition_ids")
    return Recognition(record.object_id, record.revision, _as_scope(payload), str(payload["content"]), str(payload["state"]), experience_ids, recognition_ids, _ids(payload["parent_ids"], "parent_ids"), _revision_map(payload.get("source_experience_revisions"), experience_ids), _revision_map(payload.get("source_recognition_revisions"), recognition_ids), normalize_conditions(payload.get("conditions", ())))


def _source_ref(source_type: str, source_id: str, revision: int | None) -> dict[str, object]:
    payload: dict[str, object] = {"type": source_type, "id": source_id}
    if revision is not None:
        payload["revision"] = revision
    return payload


def _candidate_generation(value: object) -> dict[str, object] | None:
    """Bounded origin of a saved initial draft, never execution or fact proof."""
    if value is None:
        return None
    fields = {"id", "step_version", "model", "configuration_revision", "completed_at"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise RecognitionError("candidate generation provenance is invalid")
    if value["step_version"] != CANDIDATE_GENERATION_STEP_VERSION:
        raise RecognitionError("candidate generation step is invalid")
    model = value["model"]
    if (not isinstance(model, str) or not model.strip() or len(model) > 256
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in model)):
        raise RecognitionError("candidate generation model is invalid")
    _revision(value["configuration_revision"])
    timestamp = value["completed_at"]
    try:
        if not isinstance(value["id"], str) or len(value["id"]) > 36 or UUID(value["id"]).version != 4:
            raise ValueError
        if not isinstance(timestamp, str) or len(timestamp) > 64:
            raise ValueError
        if datetime.fromisoformat(timestamp.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as error:
        raise RecognitionError("candidate generation identity or time is invalid") from error
    return dict(value)


def _candidate(record: SQLiteStructuredRecord) -> RecognitionCandidate:
    payload = record.payload
    experience_ids = _ids(payload["source_experience_ids"], "source_experience_ids")
    recognition_ids = _ids(payload["source_recognition_ids"], "source_recognition_ids")
    return RecognitionCandidate(
        record.object_id, record.revision, _as_scope(payload), str(payload["content"]),
        experience_ids, recognition_ids,
        _revision_map(payload.get("source_experience_revisions"), experience_ids),
        _revision_map(payload.get("source_recognition_revisions"), recognition_ids),
        str(payload["state"]), normalize_conditions(payload.get("conditions", ())),
        _candidate_generation(payload.get("generation")),
    )


def _experience(record: SQLiteStructuredRecord) -> Experience:
    payload = record.payload
    recorded_at = payload.get("created_at")
    if not isinstance(recorded_at, str):
        raise RecognitionError("stored experience created_at is invalid")
    try:
        provenance = (
            ExperienceProvenance.legacy(recorded_at=recorded_at)
            if "provenance" not in payload
            else ExperienceProvenance.from_payload(payload["provenance"], recorded_at=recorded_at)
        )
    except ExperienceProvenanceError as exc:
        raise RecognitionError("stored experience provenance is invalid") from exc
    return Experience(record.object_id, record.revision, _as_scope(payload), str(payload["content"]), str(payload["state"]), provenance)


def _question(record: SQLiteStructuredRecord) -> FixedQuestion:
    payload = record.payload
    raw_revisions = payload.get("source_revisions")
    if not isinstance(raw_revisions, Mapping):
        raise RecognitionError("stored question source revisions are invalid")
    ids = _ids(payload["recognition_ids"], "recognition_ids")
    if set(raw_revisions) != set(ids):
        raise RecognitionError("stored question source revisions are invalid")
    return FixedQuestion(
        record.object_id,
        record.revision,
        _as_scope(payload),
        _content(payload.get("question")),
        _content(payload.get("content")),
        ids,
        {item_id: _revision(raw_revisions[item_id]) for item_id in ids},
        str(payload["state"]),
        updated_at=payload.get("updated_at") if isinstance(payload.get("updated_at"), str) else "",
    )


def _scope(scope: WorkScope) -> dict[str, str | None]:
    return {"user_id": scope.user_id, "project_id": scope.project_id}


def _as_scope(payload: Mapping[str, object]) -> WorkScope:
    value = payload.get("scope")
    if not isinstance(value, Mapping):
        raise RecognitionError("stored scope is invalid")
    scope = WorkScope(value.get("user_id"), value.get("project_id"))
    if payload.get("project_id") != scope.project_id:
        raise RecognitionError("stored project scope is invalid")
    return scope


def _markdown(value: Recognition) -> str:
    metadata = json.dumps(
        {
            "recognition_id": value.recognition_id,
            "revision": value.revision,
            "source_experience_revisions": dict(value.source_experience_revisions),
            "source_recognition_revisions": dict(value.source_recognition_revisions),
            "format_version": 2,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    sources = "".join(
        f"- `experience:{item_id}` (revision: {value.source_experience_revisions[item_id]})\n"
        for item_id in value.source_experience_ids
    ) + "".join(
        f"- `recognition:{item_id}` (revision: {value.source_recognition_revisions[item_id]})\n"
        for item_id in value.source_recognition_ids
    )
    conditions = "".join(f"- {item}\n" for item in value.conditions)
    project = value.project_id or "personal"
    return (
        f"<!-- recognition:{metadata} -->\n\n# Recognition\n\n"
        f"## Project\n{project}\n\n"
        f"## Recognition ID\n{value.id}\n\n"
        f"## Base revision\n{value.revision}\n\n"
        f"## Source references\n{sources}\n"
        f"## Conditions\n{conditions}\n"
        f"## Content\n\n{value.content}\n"
    )


def _parse_markdown(markdown: str) -> tuple[Mapping[str, object], str, tuple[str, ...] | None, Mapping[str, object] | None]:
    if not isinstance(markdown, str):
        raise RecognitionError("markdown is required")
    match = _MARKDOWN_META.match(markdown)
    if match is None:
        raise RecognitionError("markdown does not contain recognition metadata")
    try:
        metadata = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RecognitionError("markdown metadata is invalid") from exc
    if not isinstance(metadata, Mapping):
        raise RecognitionError("markdown metadata is invalid")
    body = markdown[match.end():]
    sections = _MARKDOWN_SECTIONS.match(body)
    if sections is None:
        # Version-one exports placed the editable body directly below the title.
        # Keep accepting them so existing local documents remain usable.
        return metadata, _content(body.rstrip("\n")), None, None
    sources = _parse_markdown_sources(sections.group("sources"))
    conditions = _parse_markdown_conditions(sections.group("conditions"))
    visible = {
        "project_id": sections.group("project").strip(),
        "recognition_id": sections.group("recognition_id").strip(),
        "revision": sections.group("revision").strip(),
        "sources": sources,
    }
    return metadata, _content(body[sections.end():].rstrip("\n")), conditions, visible


def _parse_markdown_sources(value: str) -> tuple[tuple[str, str, int], ...]:
    if not value:
        return ()
    result: list[tuple[str, str, int]] = []
    cursor = 0
    for match in _MARKDOWN_SOURCE.finditer(value):
        if match.start() != cursor:
            raise RecognitionError("markdown source references are invalid")
        source_type, source_id, revision = match.groups()
        result.append((source_type, _id(source_id, "source reference"), _markdown_revision(revision)))
        cursor = match.end()
    if cursor != len(value) or len(result) != len(set(result)):
        raise RecognitionError("markdown source references are invalid")
    return tuple(result)


def _parse_markdown_conditions(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    lines = value.splitlines()
    if any(not line.startswith("- ") for line in lines):
        raise RecognitionError("markdown conditions are invalid")
    return normalize_conditions(tuple(line[2:] for line in lines))


def _validate_markdown_identity(visible: Mapping[str, object], current: SQLiteStructuredRecord) -> None:
    scope = _as_scope(current.payload)
    expected_project = scope.project_id or "personal"
    if visible["project_id"] != expected_project:
        raise RecognitionConflict("markdown project identity conflicted")
    if visible["recognition_id"] != current.object_id:
        raise RecognitionConflict("markdown recognition identity conflicted")
    if _markdown_revision(visible["revision"]) != current.revision:
        raise RecognitionConflict("markdown base revision conflicted")
    experiences = _ids(current.payload["source_experience_ids"], "source_experience_ids")
    recognitions = _ids(current.payload["source_recognition_ids"], "source_recognition_ids")
    expected_sources = tuple(
        [("experience", item_id, _revision_map(current.payload.get("source_experience_revisions"), experiences)[item_id]) for item_id in experiences]
        + [("recognition", item_id, _revision_map(current.payload.get("source_recognition_revisions"), recognitions)[item_id]) for item_id in recognitions]
    )
    if visible["sources"] != expected_sources:
        raise RecognitionConflict("markdown source references conflicted")


def _markdown_revision(value: object) -> int:
    if not isinstance(value, str) or not value.isdecimal():
        raise RecognitionError("markdown revision is invalid")
    return _revision(int(value))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _segment(label: str, value: object) -> None:
    if not isinstance(value, str) or not _SAFE.fullmatch(value):
        raise RecognitionError(f"{label} is invalid")


def _id(value: object, label: str) -> str:
    _segment(label, value)
    return str(value)


def _revision(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RecognitionError("revision is invalid")
    return value


def _revision_map(value: object, ids: Sequence[str]) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(ids):
        raise RecognitionError("source revision snapshot is invalid")
    return {item_id: _revision(value[item_id]) for item_id in ids}


def normalize_conditions(value: object) -> tuple[str, ...]:
    """Validate and normalize conditions before side effects or persistence."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise RecognitionError("conditions are invalid")
    result = tuple(_content(item) for item in value)
    if len(result) != len(set(result)):
        raise RecognitionError("conditions contain duplicates")
    return result


def _split_part(value: str | Mapping[str, object], parent: Mapping[str, object]) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    if isinstance(value, str):
        return (
            _content(value),
            _ids(parent["source_experience_ids"], "source_experience_ids"),
            _ids(parent["source_recognition_ids"], "source_recognition_ids"),
            normalize_conditions(parent.get("conditions", ())),
        )
    if not isinstance(value, Mapping):
        raise RecognitionError("split part is invalid")
    return (
        _content(value.get("content")),
        _ids(value.get("source_experience_ids", parent["source_experience_ids"]), "source_experience_ids"),
        _ids(value.get("source_recognition_ids", parent["source_recognition_ids"]), "source_recognition_ids"),
        normalize_conditions(value.get("conditions", parent.get("conditions", ()))),
    )


def _content(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_CONTENT_CHARS:
        raise RecognitionError("content is invalid")
    return value.strip()


def _ids(values: Iterable[object] | None, label: str) -> tuple[str, ...]:
    if values is None or isinstance(values, (str, bytes)):
        raise RecognitionError(f"{label} is invalid")
    result = tuple(_id(value, label) for value in values)
    if len(result) != len(set(result)):
        raise RecognitionError(f"{label} contains duplicates")
    return result


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
