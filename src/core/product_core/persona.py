"""L4 Persona — cross-project user preferences, constraints and long-term facts.

Persona is a special long-term memory layer that captures stable user
preferences (language style, format preferences, common projects, avoidances).
Unlike atoms/scenarios which are extracted from individual sources, Persona is
distilled only from confirmed memory candidates (``trust_status=user_confirmed``)
so Provider outputs can never directly publish Persona statements.

Storage layout
--------------

Persona lives in a dedicated ``memory_persona`` collection (one record per
scope). Each record conforms to ``core-contracts/rebuild/persona.schema.json``::

    {
      "schema_version": "1.0.0",
      "id": "persona-global",
      "scope": "global",
      "statements": [
        {"id": "...", "content": "...", "category": "preference", "confidence": 0.9}
      ],
      "evidence_refs": [
        {"object_type": "atom", "object_id": "...", "source_refs": [...]}
      ],
      "confirmation": {"required": true, "status": "confirmed", "actor": "user", "reason": "..."},
      "revision": 1,
      "trust_status": "user_confirmed",
      "created_at": "...",
      "updated_at": "..."
    }

Field mapping (spec §3.5 Prompt F):

- ``language_style`` → ``statements`` with ``category=style``
- ``format_preferences`` → ``statements`` with ``category=preference``
- ``common_projects`` → ``statements`` with ``category=workflow``
- ``avoidances`` → ``statements`` with ``category=constraint``
- ``evidence_refs`` → cross-layer refs into published memory entries
- ``revision`` → bumped on each revision
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from .ports import ObjectStorePort


class PersonaError(ValueError):
    """Raised when a Persona record would bypass review or provenance rules."""


class PersonaConflictError(PersonaError):
    """Raised when a caller operates on stale draft/current revisions."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PersonaStatement:
    """One user-facing Persona statement (preference, constraint, etc.)."""

    id: str
    content: str
    category: str  # preference | constraint | identity | workflow | style | other
    confidence: float


@dataclass(frozen=True, slots=True)
class PersonaEvidenceRef:
    """Traceable evidence backing a Persona statement."""

    object_type: str  # source | atom | scenario | document
    object_id: str
    source_refs: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class PersonaRecord:
    """One Persona record (per scope)."""

    id: str
    scope: str  # global | series | project
    statements: tuple[PersonaStatement, ...]
    evidence_refs: tuple[PersonaEvidenceRef, ...]
    confirmation: Mapping[str, object]
    revision: int
    trust_status: str
    created_at: str
    updated_at: str

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "id": self.id,
            "scope": self.scope,
            "statements": [
                {
                    "id": stmt.id,
                    "content": stmt.content,
                    "category": stmt.category,
                    "confidence": stmt.confidence,
                }
                for stmt in self.statements
            ],
            "evidence_refs": [
                {
                    "object_type": ref.object_type,
                    "object_id": ref.object_id,
                    "source_refs": [dict(src) for src in ref.source_refs],
                }
                for ref in self.evidence_refs
            ],
            "confirmation": dict(self.confirmation),
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "trust_status": self.trust_status,
        }


@dataclass(frozen=True, slots=True)
class PersonaDigest:
    """Read-only digest consumed by output templates and the frontend card.

    The digest groups statements by the four user-facing facets required by
    spec §3.5 Prompt F (language_style, format_preferences, common_projects,
    avoidances) and exposes ``evidence_refs`` for traceability. ``ready=False``
    signals that no Persona has been published yet; consumers must render an
    empty state instead of fabricating defaults.
    """

    ready: bool
    scope: str
    revision: int
    language_style: tuple[str, ...]
    format_preferences: tuple[str, ...]
    common_projects: tuple[str, ...]
    avoidances: tuple[str, ...]
    evidence_refs: tuple[PersonaEvidenceRef, ...]
    confirmation: Mapping[str, object] | None
    trust_status: str | None
    updated_at: str | None

    def to_payload(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "scope": self.scope,
            "revision": self.revision,
            "language_style": list(self.language_style),
            "format_preferences": list(self.format_preferences),
            "common_projects": list(self.common_projects),
            "avoidances": list(self.avoidances),
            "evidence_refs": [
                {
                    "object_type": ref.object_type,
                    "object_id": ref.object_id,
                    "source_refs": [dict(src) for src in ref.source_refs],
                }
                for ref in self.evidence_refs
            ],
            "confirmation": dict(self.confirmation) if self.confirmation else None,
            "trust_status": self.trust_status,
            "updated_at": self.updated_at,
        }


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObjectStorePersonaRepository:
    """Persists Persona records in the rebuild ObjectStore.

    The repository never accepts Provider output directly. Persona records
    must originate from confirmed memory candidates (``trust_status``
    ``user_confirmed``) and are written through ``save()``, which validates
    that the caller already curated the evidence_refs.

    Draft/current authority
    -----------------------
    Pending extraction output lives only in ``memory_persona_drafts``.
    ``memory_persona`` contains confirmed current records only, and
    ``memory_persona_revisions`` contains confirmed history only. Confirmation
    and rollback use separate draft/current CAS tokens; rejected drafts are
    archived outside recall. All transitions remain auditable.
    """

    object_store: ObjectStorePort
    collection: str = "memory_persona"
    drafts_collection: str = "memory_persona_drafts"
    draft_revisions_collection: str = "memory_persona_draft_revisions"
    revisions_collection: str = "memory_persona_revisions"
    transitions_collection: str = "memory_persona_transitions"
    default_scope: str = "global"

    def get(self, scope: str | None = None) -> Mapping[str, object] | None:
        clean_scope = scope or self.default_scope
        record_id = _persona_id(clean_scope)
        record = self.object_store.read(self.collection, record_id)
        if record is None:
            return None
        return dict(record)

    def get_draft(self, scope: str | None = None) -> Mapping[str, object] | None:
        clean_scope = scope or self.default_scope
        record = self.object_store.read(self.drafts_collection, _persona_id(clean_scope))
        return dict(record) if record is not None else None

    def list_scopes(self) -> tuple[str, ...]:
        records = self.object_store.list(self.collection)
        scopes: list[str] = []
        for record in records:
            scope = record.get("scope")
            if isinstance(scope, str) and scope:
                scopes.append(scope)
        return tuple(sorted(set(scopes)))

    def save(
        self,
        record: PersonaRecord,
        *,
        actor: str = "user",
        reason: str | None = None,
        now: str | None = None,
        expected_draft_revision: int | None = None,
        expected_current_revision: int | None = None,
    ) -> Mapping[str, object]:
        """Persist a draft or an explicitly confirmed current Persona."""
        clean_now = now or _utc_now()
        payload = record.to_payload()
        _validate_persona_payload(payload)
        confirmation = payload.get("confirmation")
        if not isinstance(confirmation, Mapping):
            raise PersonaError("persona confirmation is required")
        if confirmation.get("status") == "pending":
            return self._save_draft(
                record,
                actor=actor,
                reason=reason,
                now=clean_now,
                expected_draft_revision=expected_draft_revision,
                expected_current_revision=expected_current_revision,
            )
        if (
            confirmation.get("status") != "confirmed"
            or record.trust_status != "user_confirmed"
        ):
            raise PersonaError("only a confirmed Persona can enter current")
        existing = self.get(record.scope)
        from_revision: int | None = None
        if existing is not None:
            from_revision = existing.get("revision")
            if not isinstance(from_revision, int) or isinstance(from_revision, bool):
                from_revision = 0
            # Push existing to revisions collection with a unique id
            history_id = _revision_id(record.scope, from_revision)
            self.object_store.write(
                self.revisions_collection,
                history_id,
                dict(existing),
                expected_revision=self.object_store.revision(
                    self.revisions_collection,
                    history_id,
                ),
            )
            new_revision = from_revision + 1
            record = dataclasses.replace(
                record,
                revision=new_revision,
                updated_at=clean_now,
            )
        else:
            new_revision = record.revision
        payload = record.to_payload()
        _validate_persona_payload(payload)
        current_cas = self.object_store.revision(self.collection, str(payload["id"]))
        if (
            expected_current_revision is not None
            and current_cas != expected_current_revision
        ):
            raise PersonaConflictError("Persona current revision conflicted")
        self.object_store.write(
            self.collection,
            str(payload["id"]),
            payload,
            expected_revision=current_cas,
        )
        self._write_transition(
            scope=record.scope,
            transition_type="save",
            from_revision=from_revision,
            to_revision=new_revision,
            actor=actor,
            reason=reason or "Persona 蒸馏或更新",
            now=clean_now,
        )
        return payload

    def _save_draft(
        self,
        record: PersonaRecord,
        *,
        actor: str,
        reason: str | None,
        now: str,
        expected_draft_revision: int | None,
        expected_current_revision: int | None,
    ) -> Mapping[str, object]:
        record_id = _persona_id(record.scope)
        current = self.get(record.scope)
        physical_current_cas = self.object_store.revision(self.collection, record_id)
        legacy_pending = (
            current is not None and not _is_confirmed_current(current)
        )
        current_cas = 0 if legacy_pending else physical_current_cas
        if (
            expected_current_revision is not None
            and current_cas != expected_current_revision
        ):
            raise PersonaConflictError("Persona current revision conflicted")
        if legacy_pending:
            self.object_store.delete(self.collection, record_id)
            current = None
        draft_cas = self.object_store.revision(self.drafts_collection, record_id)
        if (
            expected_draft_revision is not None
            and draft_cas != expected_draft_revision
        ):
            raise PersonaConflictError("Persona draft revision conflicted")
        existing_draft = self.get_draft(record.scope)
        if existing_draft is not None:
            self.object_store.write(
                self.draft_revisions_collection,
                f"{record_id}~draft-r{draft_cas}",
                existing_draft,
                expected_revision=self.object_store.revision(
                    self.draft_revisions_collection,
                    f"{record_id}~draft-r{draft_cas}",
                ),
            )
        current_domain_revision = (
            current.get("revision") if isinstance(current, Mapping) else 0
        )
        if (
            not isinstance(current_domain_revision, int)
            or isinstance(current_domain_revision, bool)
        ):
            current_domain_revision = 0
        draft = dataclasses.replace(
            record,
            revision=current_domain_revision + 1,
            updated_at=now,
        ).to_payload()
        _validate_persona_payload(draft)
        self.object_store.write(
            self.drafts_collection,
            record_id,
            draft,
            expected_revision=draft_cas,
        )
        self._write_transition(
            scope=record.scope,
            transition_type="draft_saved",
            from_revision=(
                current_domain_revision if current_domain_revision > 0 else None
            ),
            to_revision=current_domain_revision + 1,
            actor=actor,
            reason=reason or "L4 Persona 草稿等待用户确认",
            now=now,
        )
        return draft

    def digest(self, scope: str | None = None) -> PersonaDigest:
        record = self.get(scope)
        if record is None:
            return _empty_digest(scope or self.default_scope)
        if not _is_confirmed_current(record):
            return _empty_digest(scope or self.default_scope)
        return _digest_from_record(record)

    def review_digest(self, scope: str | None = None) -> PersonaDigest:
        draft = self.get_draft(scope)
        if draft is not None:
            return _digest_from_record(draft)
        legacy_pending = self.get(scope)
        if legacy_pending is not None and not _is_confirmed_current(legacy_pending):
            return _digest_from_record(legacy_pending)
        return self.digest(scope)

    # ------------------------------------------------------------------
    # Revision history
    # ------------------------------------------------------------------

    def list_revisions(self, scope: str | None = None) -> tuple[Mapping[str, object], ...]:
        """Return historical revisions for a scope, newest first."""
        clean_scope = scope or self.default_scope
        records = self.object_store.list(self.revisions_collection)
        matching: list[Mapping[str, object]] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            if record.get("scope") != clean_scope:
                continue
            matching.append(dict(record))
        matching.sort(key=lambda r: r.get("revision", 0), reverse=True)
        return tuple(matching)

    def get_revision(self, scope: str, revision: int) -> Mapping[str, object] | None:
        """Return a specific historical revision, or None if not found."""
        history_id = _revision_id(scope, revision)
        record = self.object_store.read(self.revisions_collection, history_id)
        if record is None:
            return None
        return dict(record)

    # ------------------------------------------------------------------
    # Confirmation / rollback (called by use cases)
    # ------------------------------------------------------------------

    def update_confirmation(
        self,
        scope: str,
        *,
        status: str,
        actor: str = "user",
        reason: str,
        expected_draft_revision: int | None = None,
        expected_current_revision: int | None = None,
        now: str | None = None,
    ) -> Mapping[str, object]:
        """Confirm or reject the active draft without exposing it to recall."""
        if status not in {"confirmed", "rejected"}:
            raise PersonaError("confirmation status must be confirmed or rejected")
        clean_now = now or _utc_now()
        draft = self.get_draft(scope)
        legacy_pending = False
        if draft is None:
            possible_legacy = self.get(scope)
            if possible_legacy is not None and not _is_confirmed_current(possible_legacy):
                draft = possible_legacy
                legacy_pending = True
        if draft is None:
            raise PersonaError("cannot confirm a Persona that has not been distilled yet")
        confirmation = draft.get("confirmation")
        if not isinstance(confirmation, Mapping):
            raise PersonaError("Persona record missing confirmation block")
        current_status = confirmation.get("status")
        if current_status != "pending":
            raise PersonaError(
                f"Persona confirmation is not pending (current={current_status})"
            )
        record_id = _persona_id(scope)
        physical_current_cas = self.object_store.revision(self.collection, record_id)
        draft_cas = (
            physical_current_cas
            if legacy_pending
            else self.object_store.revision(self.drafts_collection, record_id)
        )
        current_cas = 0 if legacy_pending else physical_current_cas
        if expected_draft_revision is not None and draft_cas != expected_draft_revision:
            raise PersonaConflictError("Persona draft revision conflicted")
        if expected_current_revision is not None and current_cas != expected_current_revision:
            raise PersonaConflictError("Persona current revision conflicted")
        current = None if legacy_pending else self.get(scope)
        current_domain_revision = current.get("revision") if current is not None else 0
        if (
            not isinstance(current_domain_revision, int)
            or isinstance(current_domain_revision, bool)
        ):
            current_domain_revision = 0
        new_confirmation = {
            **dict(confirmation),
            "status": status,
            "actor": actor,
            "reason": reason,
        }
        next_domain_revision = current_domain_revision + 1
        updated = {
            **dict(draft),
            "confirmation": new_confirmation,
            "trust_status": (
                "user_confirmed" if status == "confirmed" else "system_generated"
            ),
            "revision": next_domain_revision,
            "updated_at": clean_now,
        }
        _validate_persona_payload(updated)
        if status == "confirmed":
            if current is not None:
                history_id = _revision_id(scope, current_domain_revision)
                self.object_store.write(
                    self.revisions_collection,
                    history_id,
                    current,
                    expected_revision=self.object_store.revision(
                        self.revisions_collection,
                        history_id,
                    ),
                )
            self.object_store.write(
                self.collection,
                record_id,
                updated,
                expected_revision=physical_current_cas,
            )
        else:
            rejected_id = f"{record_id}~rejected-draft-r{draft_cas}"
            self.object_store.write(
                self.draft_revisions_collection,
                rejected_id,
                updated,
                expected_revision=self.object_store.revision(
                    self.draft_revisions_collection,
                    rejected_id,
                ),
            )
        if legacy_pending:
            if status == "rejected":
                self.object_store.delete(self.collection, record_id)
        else:
            self.object_store.delete(self.drafts_collection, record_id)
        self._write_transition(
            scope=scope,
            transition_type="confirm" if status == "confirmed" else "reject",
            from_revision=(
                current_domain_revision if current_domain_revision > 0 else None
            ),
            to_revision=next_domain_revision,
            actor=actor,
            reason=reason,
            now=clean_now,
        )
        return updated

    def rollback(
        self,
        scope: str,
        *,
        to_revision: int | None = None,
        actor: str = "user",
        reason: str,
        expected_draft_revision: int | None = None,
        expected_current_revision: int | None = None,
        now: str | None = None,
    ) -> Mapping[str, object]:
        """Restore a previous revision as the new current record.

        If ``to_revision`` is None, restores the most recent historical
        revision. The current record is pushed to history, and the restored
        record is written as current with a bumped revision.
        """
        clean_now = now or _utc_now()
        existing = self.get(scope)
        if existing is None:
            raise PersonaError("cannot rollback a Persona that has not been distilled yet")
        if not _is_confirmed_current(existing):
            raise PersonaError("cannot rollback an unconfirmed Persona")
        from_revision = existing.get("revision")
        if not isinstance(from_revision, int) or isinstance(from_revision, bool):
            from_revision = 0
        # Find the target historical revision
        target: Mapping[str, object] | None = None
        if to_revision is not None:
            target = self.get_revision(scope, to_revision)
            if target is None:
                raise PersonaError(f"Persona revision {to_revision} not found for scope {scope}")
        else:
            revisions = self.list_revisions(scope)
            if not revisions:
                raise PersonaError(f"no historical revisions to rollback to for scope {scope}")
            target = next(
                (value for value in revisions if _is_confirmed_current(value)),
                None,
            )
            if target is None:
                raise PersonaError(
                    f"no confirmed historical revisions to rollback to for scope {scope}"
                )
        if not _is_confirmed_current(target):
            raise PersonaError("cannot rollback to an unconfirmed Persona revision")
        record_id = _persona_id(scope)
        current_cas = self.object_store.revision(self.collection, record_id)
        draft_cas = self.object_store.revision(self.drafts_collection, record_id)
        if expected_current_revision is not None and current_cas != expected_current_revision:
            raise PersonaConflictError("Persona current revision conflicted")
        if expected_draft_revision is not None and draft_cas != expected_draft_revision:
            raise PersonaConflictError("Persona draft revision conflicted")
        # Push current record to history
        history_id = _revision_id(scope, from_revision)
        self.object_store.write(
            self.revisions_collection,
            history_id,
            dict(existing),
            expected_revision=None,
        )
        # Write target as new current with bumped revision
        restored = {
            **dict(target),
            "revision": from_revision + 1,
            "updated_at": clean_now,
        }
        _validate_persona_payload(restored)
        self.object_store.write(
            self.collection,
            record_id,
            restored,
            expected_revision=current_cas,
        )
        self._write_transition(
            scope=scope,
            transition_type="rollback",
            from_revision=from_revision,
            to_revision=from_revision + 1,
            actor=actor,
            reason=reason,
            now=clean_now,
        )
        return restored

    # ------------------------------------------------------------------
    # Transition audit log
    # ------------------------------------------------------------------

    def _write_transition(
        self,
        *,
        scope: str,
        transition_type: str,
        from_revision: int | None,
        to_revision: int,
        actor: str,
        reason: str,
        now: str,
    ) -> None:
        transition_id = _transition_id(scope, transition_type, to_revision, now)
        transition = {
            "schema_version": "1.0.0",
            "id": transition_id,
            "scope": scope,
            "transition_type": transition_type,
            "from_revision": from_revision,
            "to_revision": to_revision,
            "actor": actor,
            "reason": reason,
            "created_at": now,
        }
        self.object_store.write(
            self.transitions_collection,
            transition_id,
            transition,
            expected_revision=None,
        )

    def list_transitions(self, scope: str | None = None) -> tuple[Mapping[str, object], ...]:
        """Return transition records for a scope, newest first."""
        clean_scope = scope or self.default_scope
        records = self.object_store.list(self.transitions_collection)
        matching: list[Mapping[str, object]] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            if record.get("scope") != clean_scope:
                continue
            matching.append(dict(record))
        matching.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return tuple(matching)


# ---------------------------------------------------------------------------
# Extraction — distill Persona from confirmed memory candidates
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PersonaExtractor:
    """Distills a Persona draft from confirmed memory candidates.

    The extractor reads published memory entries (atoms / scenarios /
    series_memory) with ``trust_status=user_confirmed`` and projects them onto
    Persona statements. The output is a *draft* ``PersonaRecord`` — the caller
    must still call ``ObjectStorePersonaRepository.save()`` to persist it, and
    that persistence is the only path through which Persona reaches the
    long-term store. Provider outputs never call the extractor directly.
    """

    now: str = "2026-07-04T09:00:00+08:00"

    def extract(
        self,
        *,
        scope: str,
        confirmed_entries: Sequence[Mapping[str, object]],
    ) -> PersonaRecord:
        clean_scope = _required_scope(scope)
        if not confirmed_entries:
            raise PersonaError("persona extractor requires confirmed entries")
        language_style: list[str] = []
        format_preferences: list[str] = []
        common_projects: list[str] = []
        avoidances: list[str] = []
        evidence_refs: list[PersonaEvidenceRef] = []
        seen_projects: set[str] = set()
        for entry in confirmed_entries:
            _ensure_confirmed(entry)
            object_id = _required_str(entry, "id")
            object_type = _entry_object_type(entry)
            source_refs = _entry_source_refs(entry)
            if not source_refs:
                continue
            evidence_refs.append(
                PersonaEvidenceRef(
                    object_type=object_type,
                    object_id=object_id,
                    source_refs=source_refs,
                )
            )
            for project_id in _entry_project_ids(entry):
                if project_id and project_id not in seen_projects:
                    seen_projects.add(project_id)
                    common_projects.append(project_id)
            style = _entry_language_style(entry)
            if style and style not in language_style:
                language_style.append(style)
            for pref in _entry_format_preferences(entry):
                if pref and pref not in format_preferences:
                    format_preferences.append(pref)
            for avoid in _entry_avoidances(entry):
                if avoid and avoid not in avoidances:
                    avoidances.append(avoid)
        statements: list[PersonaStatement] = []
        statement_index = 0
        for style in language_style:
            statement_index += 1
            statements.append(
                PersonaStatement(
                    id=f"persona-statement-style-{statement_index:03d}",
                    content=style,
                    category="style",
                    confidence=0.85,
                )
            )
        for pref in format_preferences:
            statement_index += 1
            statements.append(
                PersonaStatement(
                    id=f"persona-statement-pref-{statement_index:03d}",
                    content=pref,
                    category="preference",
                    confidence=0.8,
                )
            )
        for project in common_projects:
            statement_index += 1
            statements.append(
                PersonaStatement(
                    id=f"persona-statement-project-{statement_index:03d}",
                    content=f"常用项目：{project}",
                    category="workflow",
                    confidence=0.75,
                )
            )
        for avoid in avoidances:
            statement_index += 1
            statements.append(
                PersonaStatement(
                    id=f"persona-statement-avoid-{statement_index:03d}",
                    content=avoid,
                    category="constraint",
                    confidence=0.7,
                )
            )
        if not statements:
            raise PersonaError("persona extractor could not derive any statements")
        return PersonaRecord(
            id=_persona_id(clean_scope),
            scope=clean_scope,
            statements=tuple(statements),
            evidence_refs=tuple(evidence_refs),
            confirmation={
                "required": True,
                "status": "pending",
                "actor": None,
                "reason": "Persona 草稿等待用户确认。",
            },
            revision=1,
            trust_status="system_generated",
            created_at=self.now,
            updated_at=self.now,
        )


# ---------------------------------------------------------------------------
# Output template adapter — exposes Persona as optional high-level context
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PersonaTemplateContext:
    """Read-only adapter that output templates and QA recall consume.

    Templates call ``persona_context_for()`` to fetch a digest. When no
    Persona is published (``ready=False``), templates must proceed without
    Persona — they never fail because Persona is missing.
    """

    repository: ObjectStorePersonaRepository

    def persona_context_for(self, scope: str | None = None) -> PersonaDigest:
        return self.repository.digest(scope)

    def render_template_prefix(self, scope: str | None = None) -> str:
        """Render an optional prefix string for document/answer templates.

        Returns an empty string when no Persona is published, so templates can
        unconditionally prepend this without branching.
        """
        digest = self.persona_context_for(scope)
        if not digest.ready:
            return ""
        lines: list[str] = []
        if digest.language_style:
            lines.append("语言风格：" + "；".join(digest.language_style))
        if digest.format_preferences:
            lines.append("格式偏好：" + "；".join(digest.format_preferences))
        if digest.avoidances:
            lines.append("需要避免：" + "；".join(digest.avoidances))
        if not lines:
            return ""
        return "Persona 提示（可选参考，无证据时不要使用）：\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _persona_id(scope: str) -> str:
    return f"persona-{scope}"


def _required_scope(scope: str) -> str:
    if scope not in {"global", "series", "project"}:
        raise PersonaError("persona scope must be one of global|series|project")
    return scope


def _validate_persona_payload(payload: Mapping[str, object]) -> None:
    if payload.get("schema_version") != "1.0.0":
        raise PersonaError("persona schema_version must be 1.0.0")
    _required_str(payload, "id")
    _required_scope(_required_str(payload, "scope"))
    statements = payload.get("statements")
    if not isinstance(statements, list) or not statements:
        raise PersonaError("persona requires at least one statement")
    for stmt in statements:
        if not isinstance(stmt, Mapping):
            raise PersonaError("persona statement must be an object")
        _required_str(stmt, "id")
        _required_str(stmt, "content")
        category = stmt.get("category")
        if category not in {"preference", "constraint", "identity", "workflow", "style", "other"}:
            raise PersonaError("persona statement category is not supported")
        confidence = stmt.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            raise PersonaError("persona statement confidence must be a number")
        if confidence < 0 or confidence > 1:
            raise PersonaError("persona statement confidence must be in [0, 1]")
    evidence_refs = payload.get("evidence_refs")
    if not isinstance(evidence_refs, list) or not evidence_refs:
        raise PersonaError("persona requires at least one evidence_ref")
    for ref in evidence_refs:
        if not isinstance(ref, Mapping):
            raise PersonaError("persona evidence_ref must be an object")
        object_type = ref.get("object_type")
        if object_type not in {"source", "atom", "scenario", "document"}:
            raise PersonaError("persona evidence_ref object_type is not supported")
        _required_str(ref, "object_id")
        src_refs = ref.get("source_refs")
        if not isinstance(src_refs, list) or not src_refs:
            raise PersonaError("persona evidence_ref requires source_refs")
    confirmation = payload.get("confirmation")
    if not isinstance(confirmation, Mapping):
        raise PersonaError("persona confirmation is required")
    if confirmation.get("status") not in {"pending", "confirmed", "rejected", "rule_allowed"}:
        raise PersonaError("persona confirmation status is not supported")
    revision = payload.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise PersonaError("persona revision must be a positive integer")
    trust_status = payload.get("trust_status")
    if trust_status not in {"trusted", "imported_unverified", "user_confirmed", "system_generated", "failed"}:
        raise PersonaError("persona trust_status is not supported")


def _digest_from_record(record: Mapping[str, object]) -> PersonaDigest:
    scope = _required_str(record, "scope")
    revision = record.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        revision = 0
    statements = record.get("statements")
    if not isinstance(statements, list):
        statements = []
    language_style: list[str] = []
    format_preferences: list[str] = []
    common_projects: list[str] = []
    avoidances: list[str] = []
    for stmt in statements:
        if not isinstance(stmt, Mapping):
            continue
        content = stmt.get("content")
        if not isinstance(content, str) or not content:
            continue
        category = stmt.get("category")
        if category == "style":
            language_style.append(content)
        elif category == "preference":
            format_preferences.append(content)
        elif category == "workflow" and content.startswith("常用项目："):
            common_projects.append(content[len("常用项目："):])
        elif category == "constraint":
            avoidances.append(content)
    evidence_refs = _evidence_refs_from_record(record.get("evidence_refs"))
    confirmation = record.get("confirmation")
    if not isinstance(confirmation, Mapping):
        confirmation = None
    trust_status = record.get("trust_status")
    if not isinstance(trust_status, str):
        trust_status = None
    updated_at = record.get("updated_at")
    if not isinstance(updated_at, str):
        updated_at = None
    return PersonaDigest(
        ready=True,
        scope=scope,
        revision=revision,
        language_style=tuple(language_style),
        format_preferences=tuple(format_preferences),
        common_projects=tuple(common_projects),
        avoidances=tuple(avoidances),
        evidence_refs=tuple(evidence_refs),
        confirmation=confirmation,
        trust_status=trust_status,
        updated_at=updated_at,
    )


def _is_confirmed_current(record: Mapping[str, object]) -> bool:
    confirmation = record.get("confirmation")
    return (
        isinstance(confirmation, Mapping)
        and confirmation.get("status") == "confirmed"
        and record.get("trust_status") == "user_confirmed"
    )


def _empty_digest(scope: str) -> PersonaDigest:
    return PersonaDigest(
        ready=False,
        scope=scope,
        revision=0,
        language_style=(),
        format_preferences=(),
        common_projects=(),
        avoidances=(),
        evidence_refs=(),
        confirmation=None,
        trust_status=None,
        updated_at=None,
    )


def _evidence_refs_from_record(value: object) -> tuple[PersonaEvidenceRef, ...]:
    if not isinstance(value, list):
        return ()
    refs: list[PersonaEvidenceRef] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        object_type = item.get("object_type")
        object_id = item.get("object_id")
        if not isinstance(object_type, str) or not isinstance(object_id, str):
            continue
        src_refs_raw = item.get("source_refs")
        if not isinstance(src_refs_raw, list):
            continue
        src_refs: list[Mapping[str, object]] = []
        for src in src_refs_raw:
            if isinstance(src, Mapping):
                src_refs.append(dict(src))
        if not src_refs:
            continue
        refs.append(
            PersonaEvidenceRef(
                object_type=object_type,
                object_id=object_id,
                source_refs=tuple(src_refs),
            )
        )
    return tuple(refs)


def _ensure_confirmed(entry: Mapping[str, object]) -> None:
    trust_status = entry.get("trust_status")
    if trust_status != "user_confirmed":
        raise PersonaError("persona extractor only accepts user_confirmed entries")


def _entry_object_type(entry: Mapping[str, object]) -> str:
    layer = entry.get("layer") or entry.get("object_type")
    if isinstance(layer, str):
        if layer in {"atom", "l1_atom"}:
            return "atom"
        if layer in {"scenario", "l2_scenario"}:
            return "scenario"
        if layer in {"series_memory", "l3_series_memory"}:
            return "scenario"  # series_memory maps to scenario in evidence_ref
        if layer in {"document", "documents"}:
            return "document"
    return "atom"


def _entry_source_refs(entry: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    refs = entry.get("source_refs")
    if not isinstance(refs, list):
        return ()
    out: list[Mapping[str, object]] = []
    for ref in refs:
        if not isinstance(ref, Mapping):
            continue
        source_id = ref.get("source_id")
        locator = ref.get("locator")
        if isinstance(source_id, str) and isinstance(locator, str) and source_id and locator:
            out.append(dict(ref))
    return tuple(out)


def _entry_project_ids(entry: Mapping[str, object]) -> tuple[str, ...]:
    project_id = entry.get("project_id")
    if isinstance(project_id, str) and project_id:
        return (project_id,)
    project_ids = entry.get("project_ids")
    if not isinstance(project_ids, list):
        return ()
    return tuple(pid for pid in project_ids if isinstance(pid, str) and pid)


def _entry_language_style(entry: Mapping[str, object]) -> str | None:
    style = entry.get("language_style")
    if isinstance(style, str) and style.strip():
        return style.strip()
    style_prefs = entry.get("style_preferences")
    if isinstance(style_prefs, Mapping):
        voice = style_prefs.get("voice")
        if isinstance(voice, str) and voice.strip():
            return f"语气：{voice.strip()}"
    return None


def _entry_format_preferences(entry: Mapping[str, object]) -> tuple[str, ...]:
    prefs = entry.get("format_preferences")
    if isinstance(prefs, list):
        return tuple(str(p).strip() for p in prefs if isinstance(p, str) and p.strip())
    style_prefs = entry.get("style_preferences")
    if isinstance(style_prefs, Mapping):
        defaults = style_prefs.get("format_defaults")
        if isinstance(defaults, list):
            return tuple(str(d).strip() for d in defaults if isinstance(d, str) and d.strip())
    return ()


def _entry_avoidances(entry: Mapping[str, object]) -> tuple[str, ...]:
    avoidances = entry.get("avoidances")
    if not isinstance(avoidances, list):
        return ()
    return tuple(str(a).strip() for a in avoidances if isinstance(a, str) and a.strip())


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise PersonaError(f"{key} is required")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _revision_id(scope: str, revision: int) -> str:
    """Unique id for a historical revision record."""
    return f"{_persona_id(scope)}-r{revision}"


def _transition_id(scope: str, transition_type: str, to_revision: int, now: str) -> str:
    """Unique id for a transition audit log entry."""
    import hashlib
    digest = hashlib.sha256(
        f"{scope}:{transition_type}:{to_revision}:{now}".encode("utf-8")
    ).hexdigest()[:8]
    return f"persona-transition-{scope}-{transition_type}-{digest}"


# ---------------------------------------------------------------------------
# Use cases — confirm / rollback
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UpdatePersonaConfirmation:
    """User confirms or rejects a pending Persona draft.

    Mirrors the ``PublishStagingMemoryToMemory`` pattern from
    ``memory_publication.py`` but for Persona. Only ``pending`` records can
    be confirmed/rejected. On confirm, ``trust_status`` is upgraded to
    ``user_confirmed``; on reject, it stays ``system_generated``.
    """

    repository: ObjectStorePersonaRepository
    now: str = "2026-07-04T09:00:00+08:00"

    def execute(
        self,
        *,
        scope: str,
        status: str,
        actor: str = "user",
        reason: str,
        expected_draft_revision: int | None = None,
        expected_current_revision: int | None = None,
    ) -> PersonaDigest:
        clean_scope = _required_scope(scope)
        if status not in {"confirmed", "rejected"}:
            raise PersonaError("confirmation status must be confirmed or rejected")
        if not reason or not isinstance(reason, str):
            raise PersonaError("confirmation reason is required")
        self.repository.update_confirmation(
            clean_scope,
            status=status,
            actor=actor,
            reason=reason,
            now=self.now,
            expected_draft_revision=expected_draft_revision,
            expected_current_revision=expected_current_revision,
        )
        return self.repository.digest(clean_scope)


@dataclass(frozen=True, slots=True)
class RollbackPersonaRevision:
    """Restore a previous Persona revision as the new current record.

    Mirrors ``RollbackPublishedMemory`` from ``memory_publication.py`` but
    for Persona. If ``to_revision`` is None, restores the most recent
    historical revision.
    """

    repository: ObjectStorePersonaRepository
    now: str = "2026-07-04T09:00:00+08:00"

    def execute(
        self,
        *,
        scope: str,
        to_revision: int | None = None,
        actor: str = "user",
        reason: str,
        expected_draft_revision: int | None = None,
        expected_current_revision: int | None = None,
    ) -> PersonaDigest:
        clean_scope = _required_scope(scope)
        if not reason or not isinstance(reason, str):
            raise PersonaError("rollback reason is required")
        self.repository.rollback(
            clean_scope,
            to_revision=to_revision,
            actor=actor,
            reason=reason,
            now=self.now,
            expected_draft_revision=expected_draft_revision,
            expected_current_revision=expected_current_revision,
        )
        return self.repository.digest(clean_scope)


__all__ = [
    "ObjectStorePersonaRepository",
    "PersonaDigest",
    "PersonaConflictError",
    "PersonaError",
    "PersonaEvidenceRef",
    "PersonaExtractor",
    "PersonaRecord",
    "PersonaStatement",
    "PersonaTemplateContext",
    "RollbackPersonaRevision",
    "UpdatePersonaConfirmation",
]
