"""Immutable, project-scoped provenance contracts with no storage authority."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import re
from uuid import NAMESPACE_URL, uuid5


PROVENANCE_SCHEMA_VERSION = "1.0.0"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FINGERPRINT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_REFERENCE = re.compile(r"^crp://[A-Za-z0-9][A-Za-z0-9._~:/?#%+=@-]{1,511}$")
_SUBJECT_KINDS = frozenset({
    "task", "hypothesis", "data", "code", "config", "artifact", "experiment",
    "world_action", "agent_run", "job", "validation",
})
_LINK_RELATIONS = frozenset({
    "depends_on", "produced_by", "validated_by", "supersedes", "uses", "executes",
})
_VERDICTS = frozenset({"verified", "rejected", "inconclusive"})
_PROVENANCE_EVENT_KINDS = frozenset({
    "provenance.subject.recorded",
    "provenance.link.recorded",
    "provenance.validation.recorded",
})
_PROJECT_SCOPED_REF_ROOTS = frozenset({
    "agents", "artifacts", "code", "config", "data", "experiments", "jobs",
    "plans", "projects", "provenance", "receipts", "sources", "tasks",
    "world-model", "world-provenance", "world-supervision",
})


class ProvenanceContractError(ValueError):
    """Raised when a provenance record cannot remain opaque and scoped."""


@dataclass(frozen=True, slots=True)
class TraceSubject:
    project_id: str
    kind: str
    subject_id: str

    def __post_init__(self) -> None:
        _identifier(self.project_id, "project id")
        if self.kind not in _SUBJECT_KINDS:
            raise ProvenanceContractError("trace subject kind is invalid")
        _identifier(self.subject_id, "trace subject id")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "project_id": self.project_id,
            "kind": self.kind,
            "subject_id": self.subject_id,
        }

    @classmethod
    def from_payload(cls, value: object) -> "TraceSubject":
        item = _shape(value, {"project_id", "kind", "subject_id"}, "trace subject")
        return cls(
            project_id=_string(item["project_id"], "project id"),
            kind=_string(item["kind"], "trace subject kind"),
            subject_id=_string(item["subject_id"], "trace subject id"),
        )


@dataclass(frozen=True, slots=True)
class VersionBinding:
    authority_ref: str
    revision: str | None
    content_fingerprint: str | None

    def __post_init__(self) -> None:
        _reference(self.authority_ref, "authority reference")
        if self.revision is not None:
            _identifier(self.revision, "authority revision")
        if self.content_fingerprint is not None:
            _fingerprint(self.content_fingerprint)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "authority_ref": self.authority_ref,
            "revision": self.revision,
            "content_fingerprint": self.content_fingerprint,
        }

    @classmethod
    def from_payload(cls, value: object) -> "VersionBinding":
        item = _shape(value, {"authority_ref", "revision", "content_fingerprint"}, "version binding")
        return cls(
            authority_ref=_string(item["authority_ref"], "authority reference"),
            revision=_optional_string(item["revision"], "authority revision"),
            content_fingerprint=_optional_string(item["content_fingerprint"], "content fingerprint"),
        )


def validate_project_version_binding(
    binding: VersionBinding, project_id: str,
) -> VersionBinding:
    """Validate a VersionBinding against provenance's canonical project slot."""

    _identifier(project_id, "project id")
    _project_binding(binding, project_id)
    return binding


@dataclass(frozen=True, slots=True)
class TraceLink:
    project_id: str
    source: TraceSubject
    source_version: VersionBinding
    target: TraceSubject
    target_version: VersionBinding
    relation: str

    def __post_init__(self) -> None:
        _identifier(self.project_id, "project id")
        if not isinstance(self.source, TraceSubject) or not isinstance(self.target, TraceSubject):
            raise ProvenanceContractError("trace link subjects are invalid")
        if self.source.project_id != self.project_id or self.target.project_id != self.project_id:
            raise ProvenanceContractError("trace link crossed cross-project scope")
        if self.source == self.target:
            raise ProvenanceContractError("trace link cannot be self-referential")
        if (
            not isinstance(self.source_version, VersionBinding)
            or not isinstance(self.target_version, VersionBinding)
            or self.source_version.revision is None
            or self.target_version.revision is None
        ):
            raise ProvenanceContractError("trace link versions must be precise")
        if self.relation not in _LINK_RELATIONS:
            raise ProvenanceContractError("trace link relation is invalid")
        _project_binding(self.source_version, self.project_id)
        _project_binding(self.target_version, self.project_id)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "project_id": self.project_id,
            "source": self.source.to_payload(),
            "source_version": self.source_version.to_payload(),
            "target": self.target.to_payload(),
            "target_version": self.target_version.to_payload(),
            "relation": self.relation,
        }

    @classmethod
    def from_payload(cls, value: object) -> "TraceLink":
        item = _shape(
            value,
            {"project_id", "source", "source_version", "target", "target_version", "relation"},
            "trace link",
        )
        return cls(
            project_id=_string(item["project_id"], "project id"),
            source=TraceSubject.from_payload(item["source"]),
            source_version=VersionBinding.from_payload(item["source_version"]),
            target=TraceSubject.from_payload(item["target"]),
            target_version=VersionBinding.from_payload(item["target_version"]),
            relation=_string(item["relation"], "trace link relation"),
        )


@dataclass(frozen=True, slots=True)
class ValidationFact:
    project_id: str
    validation_id: str
    subject: TraceSubject
    subject_version: VersionBinding
    verdict: str
    validator_kind: str
    validator_revision: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.project_id, "project id")
        _identifier(self.validation_id, "validation id")
        if not isinstance(self.subject, TraceSubject) or self.subject.project_id != self.project_id:
            raise ProvenanceContractError("validation subject crossed project scope")
        if not isinstance(self.subject_version, VersionBinding) or self.subject_version.revision is None:
            raise ProvenanceContractError("validation subject revision is required")
        if self.verdict not in _VERDICTS:
            raise ProvenanceContractError("validation verdict is invalid")
        _project_binding(self.subject_version, self.project_id)
        _identifier(self.validator_kind, "validator kind")
        _identifier(self.validator_revision, "validator revision")
        if not isinstance(self.evidence_refs, tuple) or not 1 <= len(self.evidence_refs) <= 8:
            raise ProvenanceContractError("validation evidence references are invalid")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ProvenanceContractError("validation evidence references are duplicated")
        for reference in self.evidence_refs:
            _reference(reference, "validation evidence reference")
            _project_reference(reference, self.project_id)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "project_id": self.project_id,
            "validation_id": self.validation_id,
            "subject": self.subject.to_payload(),
            "subject_version": self.subject_version.to_payload(),
            "verdict": self.verdict,
            "validator_kind": self.validator_kind,
            "validator_revision": self.validator_revision,
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_payload(cls, value: object) -> "ValidationFact":
        item = _shape(
            value,
            {
                "project_id", "validation_id", "subject", "subject_version", "verdict",
                "validator_kind", "validator_revision", "evidence_refs",
            },
            "validation fact",
        )
        refs = item["evidence_refs"]
        if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)):
            raise ProvenanceContractError("validation evidence references are invalid")
        return cls(
            project_id=_string(item["project_id"], "project id"),
            validation_id=_string(item["validation_id"], "validation id"),
            subject=TraceSubject.from_payload(item["subject"]),
            subject_version=VersionBinding.from_payload(item["subject_version"]),
            verdict=_string(item["verdict"], "validation verdict"),
            validator_kind=_string(item["validator_kind"], "validator kind"),
            validator_revision=_string(item["validator_revision"], "validator revision"),
            evidence_refs=tuple(_string(reference, "validation evidence reference") for reference in refs),
        )


@dataclass(frozen=True, slots=True)
class RecordedTraceSubject:
    subject: TraceSubject
    version: VersionBinding
    declared_sequence: int


@dataclass(frozen=True, slots=True)
class CurrentSubjectValidity:
    """Derived validity for one immutable subject revision."""

    subject: TraceSubject
    version: VersionBinding
    status: str
    stable: bool


@dataclass(frozen=True, slots=True)
class ProjectProvenanceValidity:
    """Current-only validity view; historical provenance facts remain intact."""

    subjects: tuple[CurrentSubjectValidity, ...]

    def for_subject(
        self, subject: TraceSubject, version: VersionBinding,
    ) -> CurrentSubjectValidity:
        found = next(
            (item for item in self.subjects if item.subject == subject and item.version == version),
            None,
        )
        if found is None:
            raise ProvenanceContractError("provenance subject version is unavailable")
        return found

    @property
    def stable_subjects(self) -> tuple[CurrentSubjectValidity, ...]:
        return tuple(item for item in self.subjects if item.stable)


@dataclass(frozen=True, slots=True)
class ProjectProvenanceTrace:
    project_id: str
    through_sequence: int
    subjects: tuple[RecordedTraceSubject, ...]
    links: tuple[TraceLink, ...]
    validations: tuple[ValidationFact, ...]

    @property
    def current_subjects(self) -> tuple[RecordedTraceSubject, ...]:
        latest: dict[tuple[str, str], RecordedTraceSubject] = {}
        for item in self.subjects:
            latest[(item.subject.kind, item.subject.subject_id)] = item
        return tuple(latest[key] for key in sorted(latest))

    def summary_payload(self) -> dict[str, object]:
        """Return a renderer-safe aggregate without internal subject identities."""

        kind_counts: dict[str, int] = {}
        for item in self.current_subjects:
            kind = item.subject.kind
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
        return {
            "kind": "project_provenance_summary.v1",
            "counts": {
                "subjects": len(self.subjects),
                "links": len(self.links),
                "validations": len(self.validations),
            },
            "subject_kinds": [
                {"kind": kind, "count": kind_counts[kind]}
                for kind in sorted(kind_counts)
            ],
            "relations": sorted({item.relation for item in self.links}),
            "validation_statuses": sorted({item.verdict for item in self.validations}),
        }

    @property
    def current_validity(self) -> ProjectProvenanceValidity:
        """Project current validity without revising any historic fact or Receipt.

        A link always means ``source`` depends on ``target``.  Stale and
        invalidated targets therefore invalidate their dependants transitively;
        pending targets merely prevent a dependant from being stable.
        """
        keys = tuple((_subject_key(item.subject, item.version), item) for item in self.subjects)
        statuses = {
            key: _validation_status(self.validations, item.subject, item.version)
            for key, item in keys
        }
        latest: dict[tuple[str, str], int] = {}
        for key, item in keys:
            logical = (item.subject.kind, item.subject.subject_id)
            latest[logical] = max(latest.get(logical, item.declared_sequence), item.declared_sequence)
        for key, item in keys:
            logical = (item.subject.kind, item.subject.subject_id)
            if item.declared_sequence < latest[logical] and statuses[key] != "invalidated":
                statuses[key] = "stale"
        reverse_dependencies: dict[tuple[str, str, str, str], set[tuple[str, str, str, str]]] = {}
        for link in self.links:
            source = _subject_key(link.source, link.source_version)
            target = _subject_key(link.target, link.target_version)
            if source in statuses and target in statuses:
                reverse_dependencies.setdefault(target, set()).add(source)
        changed = True
        while changed:
            changed = False
            for target, sources in reverse_dependencies.items():
                target_status = statuses[target]
                if target_status not in {"stale", "invalidated"}:
                    continue
                for source in sources:
                    propagated = "invalidated" if target_status == "invalidated" else "stale"
                    if _status_rank(propagated) > _status_rank(statuses[source]):
                        statuses[source] = propagated
                        changed = True
        dependencies: dict[
            tuple[str, str, str, str], set[tuple[str, str, str, str]],
        ] = {}
        for link in self.links:
            dependencies.setdefault(
                _subject_key(link.source, link.source_version), set(),
            ).add(_subject_key(link.target, link.target_version))
        result = []
        for key, item in keys:
            stable = statuses[key] == "verified" and _dependencies_are_stable(
                key, statuses, dependencies,
            )
            result.append(CurrentSubjectValidity(item.subject, item.version, statuses[key], stable))
        return ProjectProvenanceValidity(tuple(result))


def provenance_event_identity(
    event_kind: str,
    project_id: str,
    payload: Mapping[str, object],
) -> tuple[str, str, str]:
    """Return the canonical World-event identity for one provenance fact."""

    if event_kind not in _PROVENANCE_EVENT_KINDS:
        raise ProvenanceContractError("provenance event kind is invalid")
    _identifier(project_id, "project id")
    canonical = json.dumps(
        _plain_json(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    event_id = (
        f"provenance-{event_kind.split('.')[1]}-"
        f"{uuid5(NAMESPACE_URL, f'{event_kind}:{project_id}:{canonical}').hex}"
    )
    return (
        event_id,
        f"crp://world-provenance/{project_id}/{event_id}",
        "provenance-v1",
    )


def project_provenance_events(
    events: Sequence[object],
    *,
    project_id: str,
) -> ProjectProvenanceTrace:
    """Validate and rebuild provenance facts from one ordered World stream."""

    _identifier(project_id, "project id")
    subjects: list[RecordedTraceSubject] = []
    links: list[TraceLink] = []
    validations: list[ValidationFact] = []
    validation_ids: dict[str, ValidationFact] = {}
    through_sequence = 0
    for event in events:
        event_project = getattr(event, "project_id", None)
        sequence = getattr(event, "sequence", None)
        if event_project != project_id:
            raise ProvenanceContractError("provenance stream crossed project scope")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise ProvenanceContractError("provenance event sequence is invalid")
        kind_value = getattr(getattr(event, "kind", None), "value", getattr(event, "kind", None))
        if kind_value not in _PROVENANCE_EVENT_KINDS:
            continue
        through_sequence = max(through_sequence, sequence)
        if getattr(event, "actor", None) != "system":
            raise ProvenanceContractError("provenance event is not system-owned")
        payload = getattr(event, "payload", None)
        if not isinstance(payload, Mapping):
            raise ProvenanceContractError("provenance event payload is invalid")
        expected_id, expected_ref, expected_revision = provenance_event_identity(
            str(kind_value), project_id, payload,
        )
        if (
            getattr(event, "event_id", None) != expected_id
            or getattr(event, "source_ref", None) != expected_ref
            or getattr(event, "source_revision", None) != expected_revision
        ):
            raise ProvenanceContractError("provenance event identity drifted")
        if kind_value == "provenance.subject.recorded":
            if set(payload) != {"subject", "version"}:
                raise ProvenanceContractError("provenance subject event shape is invalid")
            subject = TraceSubject.from_payload(payload.get("subject"))
            version = VersionBinding.from_payload(payload.get("version"))
            if subject.project_id != project_id:
                raise ProvenanceContractError("provenance subject crossed project scope")
            if version.revision is None:
                raise ProvenanceContractError("provenance subject revision is required")
            _project_binding(version, project_id)
            prior = _find_subject(subjects, subject, version.revision)
            if prior is not None:
                if prior.version != version:
                    raise ProvenanceContractError("provenance subject version drifted")
                continue
            subjects.append(RecordedTraceSubject(subject, version, sequence))
            continue
        if kind_value == "provenance.link.recorded":
            link = TraceLink.from_payload(payload)
            if link.project_id != project_id:
                raise ProvenanceContractError("provenance link crossed project scope")
            _require_subject(subjects, link.source, link.source_version)
            _require_subject(subjects, link.target, link.target_version)
            if link not in links:
                links.append(link)
            continue
        validation = ValidationFact.from_payload(payload)
        if validation.project_id != project_id:
            raise ProvenanceContractError("provenance validation crossed project scope")
        _require_subject(subjects, validation.subject, validation.subject_version)
        prior_validation = validation_ids.get(validation.validation_id)
        if prior_validation is not None and prior_validation != validation:
            raise ProvenanceContractError("provenance validation id is already bound")
        if prior_validation is None:
            validation_ids[validation.validation_id] = validation
            validations.append(validation)
    return ProjectProvenanceTrace(
        project_id,
        through_sequence,
        tuple(subjects),
        tuple(links),
        tuple(validations),
    )


def _shape(value: object, fields: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields | {"schema_version"}:
        raise ProvenanceContractError(f"{label} shape is invalid")
    if value.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        raise ProvenanceContractError(f"{label} schema version is invalid")
    return value


def _plain_json(value: object) -> object:
    """Copy immutable World payload containers into canonical JSON values."""

    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ProvenanceContractError(f"{label} is invalid")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _string(value, label)


def _identifier(value: object, label: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ProvenanceContractError(f"{label} is invalid")


def _fingerprint(value: object) -> None:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise ProvenanceContractError("content fingerprint is invalid")


def _reference(value: object, label: str) -> None:
    if not isinstance(value, str) or _REFERENCE.fullmatch(value) is None:
        raise ProvenanceContractError(f"{label} is invalid")


def _project_binding(value: VersionBinding, project_id: str) -> None:
    if not isinstance(value, VersionBinding):
        raise ProvenanceContractError("project version binding is invalid")
    _project_reference(value.authority_ref, project_id)


def _project_reference(value: str, project_id: str) -> None:
    body = value.removeprefix("crp://")
    segments = tuple(segment for segment in body.split("/") if segment)
    if not segments or segments[0] not in _PROJECT_SCOPED_REF_ROOTS:
        return
    project_segment = (
        segments[2]
        if len(segments) >= 3 and segments[:2] == ("world-model", "projects")
        else segments[1] if len(segments) >= 2 else None
    )
    if project_segment != project_id:
        raise ProvenanceContractError("provenance reference crossed project scope")


def _find_subject(
    values: Sequence[RecordedTraceSubject],
    subject: TraceSubject,
    revision: str | None,
) -> RecordedTraceSubject | None:
    return next(
        (
            item
            for item in values
            if item.subject == subject and item.version.revision == revision
        ),
        None,
    )


def _require_subject(
    values: Sequence[RecordedTraceSubject],
    subject: TraceSubject,
    version: VersionBinding,
) -> None:
    found = _find_subject(values, subject, version.revision)
    if found is None or found.version != version:
        raise ProvenanceContractError(
            "provenance endpoint version was not declared earlier"
        )


def _subject_key(
    subject: TraceSubject, version: VersionBinding,
) -> tuple[str, str, str, str]:
    revision = version.revision
    if revision is None:
        raise ProvenanceContractError("provenance subject revision is required")
    return subject.kind, subject.subject_id, version.authority_ref, revision


def _validation_status(
    validations: Sequence[ValidationFact], subject: TraceSubject, version: VersionBinding,
) -> str:
    verdicts = {
        item.verdict for item in validations
        if item.subject == subject and item.subject_version == version
    }
    if "rejected" in verdicts:
        return "invalidated"
    if "inconclusive" in verdicts:
        return "pending"
    return "verified" if "verified" in verdicts else "pending"


def _status_rank(value: str) -> int:
    return {"pending": 0, "verified": 1, "stale": 2, "invalidated": 3}[value]


def _dependencies_are_stable(
    key: tuple[str, str, str, str],
    statuses: Mapping[tuple[str, str, str, str], str],
    dependencies: Mapping[
        tuple[str, str, str, str], set[tuple[str, str, str, str]],
    ],
) -> bool:
    pending = list(dependencies.get(key, ()))
    seen: set[tuple[str, str, str, str]] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if statuses.get(current) != "verified":
            return False
        pending.extend(dependencies.get(current, ()))
    return True
