from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .package_catalog import ApplicationSkillCatalogSnapshot, ApplicationSkillPackage


class ApplicationSkillBindingError(ValueError):
    """Raised when a project Skill binding violates its configuration contract."""


class ApplicationSkillBindingConflict(ApplicationSkillBindingError):
    """Raised when a binding preview or CAS revision is stale."""


class ApplicationSkillBindingStorePort(Protocol):
    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None: ...

    def revision(self, collection: str, object_id: str) -> int: ...

    def write(
        self,
        collection: str,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int | None,
    ) -> int: ...


_SCHEMA_VERSION = "1.0.0"
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_SKILL_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_CONSUMERS = frozenset({
    "answer.model-request", "document.generate", "turn.agent-child",
    "turn.workbench-question",
})
_SOURCE_KINDS = frozenset({"bundled", "user", "plugin", "external"})
_MAX_TRIGGER_TERMS = 24
_MAX_BINDINGS = 256
_MAX_HISTORY = 100
_SECRET = re.compile(r"(?i)(?:authorization\s*:|cookie\s*:|\bsk-[A-Za-z0-9_-]{16,}\b|api[_-]?key\s*[:=])")


@dataclass(frozen=True, slots=True)
class EffectiveApplicationSkillBinding:
    project_id: str
    consumer: str
    binding_id: str
    binding_revision: int
    priority: int
    trigger_terms: tuple[str, ...]
    package: ApplicationSkillPackage


class ApplicationSkillBindingRegistry:
    """CAS registry binding exact Skill package fingerprints to projects."""

    collection = "application_skill_registries"
    registry_id = "default"

    def __init__(self, store: ApplicationSkillBindingStorePort, *, now: str | None = None) -> None:
        self._store = store
        self._fixed_now = now

    def status(self, catalog: ApplicationSkillCatalogSnapshot | None = None) -> dict[str, object]:
        record, _ = self._read()
        bindings = [self._binding_projection(item, catalog) for item in record["bindings"]]
        return {
            "schema_version": record["schema_version"],
            "registry_revision": record["registry_revision"],
            "bindings": bindings,
            "history": deepcopy(record["history"]),
            "updated_at": record["updated_at"],
        }

    def preview_bind(
        self,
        package: ApplicationSkillPackage,
        *,
        project_id: str,
        allowed_consumers: Sequence[str],
        priority: int = 500,
        trigger_terms: Sequence[str] = (),
    ) -> dict[str, object]:
        record, _ = self._read()
        candidate = _binding_candidate(
            package,
            project_id=project_id,
            allowed_consumers=allowed_consumers,
            priority=priority,
            trigger_terms=trigger_terms,
            current=_find_binding(record["bindings"], project_id, package.skill_id),
            now=self._timestamp(),
        )
        current = _find_binding(record["bindings"], candidate["project_id"], candidate["skill_id"])
        action = "already_active" if current == candidate else ("rebound" if current is not None else "activated")
        return {
            "status": "validated",
            "action": action,
            "registry_revision": record["registry_revision"],
            "binding": deepcopy(candidate),
            "preview_token": _preview_token(record["registry_revision"], candidate, current),
            "write_effect": "none",
        }

    def activate(
        self,
        package: ApplicationSkillPackage,
        *,
        project_id: str,
        allowed_consumers: Sequence[str],
        priority: int,
        trigger_terms: Sequence[str],
        expected_registry_revision: int,
        preview_token: str,
        confirm: bool,
        reason: str,
    ) -> dict[str, object]:
        clean_reason = _confirmation(confirm, reason, "activation")
        preview = self.preview_bind(
            package,
            project_id=project_id,
            allowed_consumers=allowed_consumers,
            priority=priority,
            trigger_terms=trigger_terms,
        )
        if preview["registry_revision"] != expected_registry_revision:
            raise ApplicationSkillBindingConflict("Application Skill registry revision conflict")
        if preview["preview_token"] != preview_token:
            raise ApplicationSkillBindingConflict("Application Skill binding preview drifted")
        record, store_revision = self._read()
        self._expect_revision(record, expected_registry_revision)
        candidate = dict(preview["binding"])
        current = _find_binding(record["bindings"], candidate["project_id"], candidate["skill_id"])
        if current == candidate:
            return {**self.status(), "status": "already_active", "replayed": True}
        bindings = [
            dict(item)
            for item in record["bindings"]
            if item["binding_id"] != candidate["binding_id"]
        ]
        bindings.append(candidate)
        bindings.sort(key=_binding_sort_key)
        action = "rebound" if current is not None else "activated"
        next_record = self._next_record(
            record,
            bindings,
            action=action,
            binding=candidate,
            before=current,
            reason=clean_reason,
        )
        self._write(next_record, store_revision)
        return {**self.status(), "status": action, "replayed": False}

    def preview_bind_batch(self, requests: Sequence[Mapping[str, object]]) -> dict[str, object]:
        """Validate a complete activation batch without changing the registry."""
        record, _ = self._read()
        candidates = self._batch_candidates(record, requests)
        current = [
            _find_binding(record["bindings"], candidate["project_id"], candidate["skill_id"])
            for candidate in candidates
        ]
        actions = [
            "already_active" if existing == candidate else ("rebound" if existing is not None else "activated")
            for candidate, existing in zip(candidates, current, strict=True)
        ]
        return {
            "status": "validated",
            "actions": actions,
            "registry_revision": record["registry_revision"],
            "bindings": deepcopy(candidates),
            "preview_token": _batch_preview_token(record["registry_revision"], candidates, current),
            "write_effect": "none",
        }

    def activate_batch(
        self,
        requests: Sequence[Mapping[str, object]],
        *,
        expected_registry_revision: int,
        preview_token: str,
        confirm: bool,
        reason: str,
    ) -> dict[str, object]:
        """Atomically activate, rebind, or replay several Skill bindings."""
        preview = self.preview_bind_batch(requests)
        clean_reason = _confirmation(confirm, reason, "activation")
        if preview["registry_revision"] != expected_registry_revision:
            raise ApplicationSkillBindingConflict("Application Skill registry revision conflict")
        if preview["preview_token"] != preview_token:
            raise ApplicationSkillBindingConflict("Application Skill binding preview drifted")
        record, store_revision = self._read()
        self._expect_revision(record, expected_registry_revision)
        candidates = self._batch_candidates(record, requests)
        next_record = deepcopy(record)
        actions: list[str] = []
        changed = False
        for candidate in candidates:
            current = _find_binding(
                next_record["bindings"], candidate["project_id"], candidate["skill_id"]
            )
            if current == candidate:
                actions.append("already_active")
                continue
            bindings = [
                dict(item)
                for item in next_record["bindings"]
                if item["binding_id"] != candidate["binding_id"]
            ]
            bindings.append(candidate)
            bindings.sort(key=_binding_sort_key)
            action = "rebound" if current is not None else "activated"
            next_record = self._next_record(
                next_record, bindings, action=action, binding=candidate,
                before=current, reason=clean_reason,
            )
            actions.append(action)
            changed = True
        if not changed:
            return {**self.status(), "status": "already_active", "actions": actions, "replayed": True}
        self._write(next_record, store_revision)
        return {**self.status(), "status": "activated", "actions": actions, "replayed": False}

    def deactivate(
        self,
        *,
        project_id: str,
        skill_id: str,
        expected_registry_revision: int,
        confirm: bool,
        reason: str,
    ) -> dict[str, object]:
        clean_reason = _confirmation(confirm, reason, "deactivation")
        clean_project = _project_id(project_id)
        clean_skill = _skill_id(skill_id)
        record, store_revision = self._read()
        self._expect_revision(record, expected_registry_revision)
        current = _find_binding(record["bindings"], clean_project, clean_skill)
        if current is None or current["status"] == "inactive":
            return {**self.status(), "status": "already_inactive", "replayed": True}
        now = self._timestamp()
        inactive = {
            **current,
            "binding_revision": int(current["binding_revision"]) + 1,
            "status": "inactive",
            "updated_at": now,
            "deactivated_at": now,
            "deactivation_reason": clean_reason,
        }
        bindings = [dict(item) for item in record["bindings"] if item["binding_id"] != current["binding_id"]]
        bindings.append(inactive)
        bindings.sort(key=_binding_sort_key)
        next_record = self._next_record(
            record,
            bindings,
            action="deactivated",
            binding=inactive,
            before=current,
            reason=clean_reason,
        )
        self._write(next_record, store_revision)
        return {**self.status(), "status": "deactivated", "replayed": False}

    def deactivate_batch(
        self,
        requests: Sequence[Mapping[str, object]],
        *,
        expected_registry_revision: int,
        confirm: bool,
        reason: str,
    ) -> dict[str, object]:
        """Atomically deactivate exact package fingerprints from a complete request batch."""
        normalized = _deactivation_requests(requests)
        clean_reason = _confirmation(confirm, reason, "deactivation")
        record, store_revision = self._read()
        self._expect_revision(record, expected_registry_revision)
        # Verify the full batch before deriving any replacement bindings.
        current = [
            _find_binding(record["bindings"], item["project_id"], item["skill_id"])
            for item in normalized
        ]
        for item, binding in zip(normalized, current, strict=True):
            if binding is not None and binding["skill_fingerprint"] != item["skill_fingerprint"]:
                raise ApplicationSkillBindingConflict("Application Skill package fingerprint conflict")
        next_record = deepcopy(record)
        actions: list[str] = []
        changed = False
        for item in normalized:
            binding = _find_binding(next_record["bindings"], item["project_id"], item["skill_id"])
            if binding is None or binding["status"] == "inactive":
                actions.append("already_inactive")
                continue
            now = self._timestamp()
            inactive = {
                **binding,
                "binding_revision": int(binding["binding_revision"]) + 1,
                "status": "inactive",
                "updated_at": now,
                "deactivated_at": now,
                "deactivation_reason": clean_reason,
            }
            bindings = [
                dict(value) for value in next_record["bindings"]
                if value["binding_id"] != binding["binding_id"]
            ]
            bindings.append(inactive)
            bindings.sort(key=_binding_sort_key)
            next_record = self._next_record(
                next_record, bindings, action="deactivated", binding=inactive,
                before=binding, reason=clean_reason,
            )
            actions.append("deactivated")
            changed = True
        if not changed:
            return {**self.status(), "status": "already_inactive", "actions": actions, "replayed": True}
        self._write(next_record, store_revision)
        return {**self.status(), "status": "deactivated", "actions": actions, "replayed": False}

    preview_bind_many = preview_bind_batch
    activate_many = activate_batch
    deactivate_many = deactivate_batch

    def effective_bindings(
        self,
        catalog: ApplicationSkillCatalogSnapshot,
        *,
        project_id: str,
        consumer: str,
    ) -> tuple[EffectiveApplicationSkillBinding, ...]:
        clean_project = _project_id(project_id)
        clean_consumer = _consumer(consumer)
        record, _ = self._read()
        effective: list[EffectiveApplicationSkillBinding] = []
        for binding in record["bindings"]:
            if (
                binding["project_id"] != clean_project
                or binding["status"] != "active"
                or clean_consumer not in binding["allowed_consumers"]
            ):
                continue
            package = catalog.get(str(binding["skill_id"]))
            if (
                package is None
                or not _binding_matches_package(binding, package)
                or package.maturity == "deprecated"
            ):
                continue
            effective.append(
                EffectiveApplicationSkillBinding(
                    project_id=clean_project,
                    consumer=clean_consumer,
                    binding_id=str(binding["binding_id"]),
                    binding_revision=int(binding["binding_revision"]),
                    priority=int(binding["priority"]),
                    trigger_terms=tuple(str(item) for item in binding["trigger_terms"]),
                    package=package,
                )
            )
        return tuple(sorted(effective, key=lambda item: (-item.priority, item.package.skill_id)))

    def _binding_projection(
        self,
        binding: Mapping[str, object],
        catalog: ApplicationSkillCatalogSnapshot | None,
    ) -> dict[str, object]:
        projection = deepcopy(dict(binding))
        if binding["status"] != "active":
            effective_status = "inactive"
        elif catalog is None:
            effective_status = "unverified"
        else:
            package = catalog.get(str(binding["skill_id"]))
            if package is None:
                effective_status = "missing"
            elif package.fingerprint != binding["skill_fingerprint"]:
                effective_status = "drifted"
            elif binding["source_kind"] == "unknown":
                # Schema-1 records did not attest the package origin.  They
                # remain inspectable for migration and support, but cannot
                # authorize any present-day package, even byte-identical
                # bundled/user/plugin content.  Otherwise a source
                # replacement could silently inherit an old active binding.
                effective_status = "legacy_provenance_untrusted"
            elif (
                package.source_id != binding["source_id"]
                or package.source_kind != binding["source_kind"]
            ):
                effective_status = "source_drifted"
            elif package.maturity == "deprecated":
                effective_status = "deprecated"
            else:
                effective_status = "active"
        projection["effective_status"] = effective_status
        return projection

    def _read(self) -> tuple[dict[str, object], int]:
        store_revision = self._store.revision(self.collection, self.registry_id)
        raw = self._store.read(self.collection, self.registry_id)
        if self._store.revision(self.collection, self.registry_id) != store_revision:
            raise ApplicationSkillBindingConflict("Application Skill registry storage revision conflict")
        return (_empty_registry() if raw is None else _validate_registry(raw)), store_revision

    def _write(self, record: Mapping[str, object], store_revision: int) -> None:
        clean = _validate_registry(record)
        try:
            self._store.write(self.collection, self.registry_id, clean, expected_revision=store_revision)
        except ValueError as error:
            if "expected revision" in str(error):
                raise ApplicationSkillBindingConflict("Application Skill registry storage revision conflict") from error
            raise

    @staticmethod
    def _expect_revision(record: Mapping[str, object], expected: int) -> None:
        if record["registry_revision"] != expected:
            raise ApplicationSkillBindingConflict(
                f"Application Skill registry revision conflict: expected {expected}, current {record['registry_revision']}"
            )

    def _next_record(
        self,
        record: Mapping[str, object],
        bindings: Sequence[Mapping[str, object]],
        *,
        action: str,
        binding: Mapping[str, object],
        before: Mapping[str, object] | None,
        reason: str,
    ) -> dict[str, object]:
        next_revision = int(record["registry_revision"]) + 1
        event = {
            "registry_revision": next_revision,
            "action": action,
            "binding_id": binding["binding_id"],
            "project_id": binding["project_id"],
            "skill_id": binding["skill_id"],
            "binding_revision": binding["binding_revision"],
            "before": [deepcopy(dict(before))] if before is not None else [],
            "after": [deepcopy(dict(binding))],
            "reason": reason,
            "recorded_at": binding["updated_at"],
        }
        return {
            **record,
            "registry_revision": next_revision,
            "bindings": [deepcopy(dict(item)) for item in bindings],
            "history": [*[deepcopy(dict(item)) for item in record["history"]], event][-_MAX_HISTORY:],
            "updated_at": binding["updated_at"],
        }

    def _batch_candidates(
        self,
        record: Mapping[str, object],
        requests: Sequence[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        clean_requests = _activation_requests(requests)
        now = self._timestamp()
        return [
            _binding_candidate(
                item["package"], project_id=item["project_id"],
                allowed_consumers=item["allowed_consumers"], priority=item["priority"],
                trigger_terms=item["trigger_terms"],
                current=_find_binding(record["bindings"], item["project_id"], item["package"].skill_id),
                now=now,
            )
            for item in clean_requests
        ]

    def _timestamp(self) -> str:
        return self._fixed_now or datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_registry() -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "registry_revision": 0,
        "bindings": [],
        "history": [],
        "updated_at": "",
    }


def _binding_candidate(
    package: ApplicationSkillPackage,
    *,
    project_id: str,
    allowed_consumers: Sequence[str],
    priority: int,
    trigger_terms: Sequence[str],
    current: Mapping[str, object] | None,
    now: str,
) -> dict[str, object]:
    project = _project_id(project_id)
    skill = _skill_id(package.skill_id)
    if not _SHA256.fullmatch(package.fingerprint):
        raise ApplicationSkillBindingError("Application Skill package fingerprint is invalid")
    consumers = sorted({_consumer(value) for value in allowed_consumers})
    if not consumers:
        raise ApplicationSkillBindingError("at least one Application Skill consumer is required")
    if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 1000:
        raise ApplicationSkillBindingError("Application Skill priority must be an integer from 0 to 1000")
    terms = _trigger_terms(trigger_terms)
    base = {
        "binding_id": _binding_id(project, skill),
        "project_id": project,
        "skill_id": skill,
        "skill_fingerprint": package.fingerprint,
        "source_id": package.source_id,
        "source_kind": package.source_kind,
        "allowed_consumers": consumers,
        "priority": priority,
        "trigger_terms": terms,
        "status": "active",
    }
    if current is not None and all(current.get(key) == value for key, value in base.items()):
        return deepcopy(dict(current))
    revision = int(current["binding_revision"]) + 1 if current is not None else 1
    return {
        **base,
        "binding_revision": revision,
        "activated_at": now,
        "updated_at": now,
    }


def _activation_requests(requests: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    if not isinstance(requests, Sequence) or isinstance(requests, (str, bytes)) or not requests:
        raise ApplicationSkillBindingError("Application Skill activation batch must be a non-empty array")
    clean: list[dict[str, object]] = []
    identities: set[tuple[str, str]] = set()
    expected = {"package", "project_id", "allowed_consumers", "priority", "trigger_terms"}
    for request in requests:
        if not isinstance(request, Mapping) or set(request) != expected:
            raise ApplicationSkillBindingError("invalid Application Skill activation batch request")
        package = request["package"]
        if not isinstance(package, ApplicationSkillPackage):
            raise ApplicationSkillBindingError("invalid Application Skill package")
        project = _project_id(request["project_id"])
        identity = (project, _skill_id(package.skill_id))
        if identity in identities:
            raise ApplicationSkillBindingError("duplicate Application Skill binding in batch")
        identities.add(identity)
        allowed_consumers = request["allowed_consumers"]
        trigger_terms = request["trigger_terms"]
        if not isinstance(allowed_consumers, Sequence) or isinstance(allowed_consumers, (str, bytes)):
            raise ApplicationSkillBindingError("Application Skill consumers must be an array")
        if not isinstance(trigger_terms, Sequence) or isinstance(trigger_terms, (str, bytes)):
            raise ApplicationSkillBindingError("Application Skill trigger terms must be an array")
        priority = request["priority"]
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise ApplicationSkillBindingError("Application Skill priority must be an integer from 0 to 1000")
        clean.append({
            "package": package,
            "project_id": project,
            "allowed_consumers": tuple(allowed_consumers),
            "priority": priority,
            "trigger_terms": tuple(trigger_terms),
        })
    return clean


def _deactivation_requests(requests: Sequence[Mapping[str, object]]) -> list[dict[str, str]]:
    if not isinstance(requests, Sequence) or isinstance(requests, (str, bytes)) or not requests:
        raise ApplicationSkillBindingError("Application Skill deactivation batch must be a non-empty array")
    clean: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    expected = {"project_id", "skill_id", "skill_fingerprint"}
    for request in requests:
        if not isinstance(request, Mapping) or set(request) != expected:
            raise ApplicationSkillBindingError("invalid Application Skill deactivation batch request")
        project, skill = _project_id(request["project_id"]), _skill_id(request["skill_id"])
        fingerprint = request["skill_fingerprint"]
        if not isinstance(fingerprint, str) or not _SHA256.fullmatch(fingerprint):
            raise ApplicationSkillBindingError("invalid Application Skill package fingerprint")
        identity = (project, skill)
        if identity in identities:
            raise ApplicationSkillBindingError("duplicate Application Skill binding in batch")
        identities.add(identity)
        clean.append({"project_id": project, "skill_id": skill, "skill_fingerprint": fingerprint})
    return clean


def _validate_registry(value: Mapping[str, object]) -> dict[str, object]:
    expected = {"schema_version", "registry_revision", "bindings", "history", "updated_at"}
    if set(value) != expected or value.get("schema_version") != _SCHEMA_VERSION:
        raise ApplicationSkillBindingError("invalid Application Skill registry schema")
    revision = value.get("registry_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ApplicationSkillBindingError("invalid Application Skill registry revision")
    bindings_value = value.get("bindings")
    history_value = value.get("history")
    if not isinstance(bindings_value, Sequence) or isinstance(bindings_value, (str, bytes)):
        raise ApplicationSkillBindingError("Application Skill bindings must be an array")
    if len(bindings_value) > _MAX_BINDINGS:
        raise ApplicationSkillBindingError("Application Skill bindings are unbounded")
    if not isinstance(history_value, Sequence) or isinstance(history_value, (str, bytes)) or len(history_value) > _MAX_HISTORY:
        raise ApplicationSkillBindingError("Application Skill history is invalid or unbounded")
    bindings = [_validate_binding(item) for item in bindings_value]
    binding_ids = [item["binding_id"] for item in bindings]
    if len(binding_ids) != len(set(binding_ids)):
        raise ApplicationSkillBindingError("duplicate Application Skill binding identity")
    history = [_validate_history(item) for item in history_value]
    history_revisions = [item["registry_revision"] for item in history]
    if history_revisions != sorted(set(history_revisions)) or any(item > revision for item in history_revisions):
        raise ApplicationSkillBindingError("Application Skill history revision drifted")
    if revision == 0 and (bindings or history):
        raise ApplicationSkillBindingError("empty Application Skill registry contains state")
    if revision > 0 and (not history or history_revisions[-1] != revision):
        raise ApplicationSkillBindingError("Application Skill history does not reach current revision")
    latest: dict[str, Mapping[str, object]] = {}
    for event in history:
        latest[str(event["binding_id"])] = event
    current_by_id = {str(item["binding_id"]): item for item in bindings}
    for binding_id, event in latest.items():
        after = event["after"]
        if len(after) != 1 or current_by_id.get(binding_id) != after[0]:
            raise ApplicationSkillBindingError("Application Skill binding drifted from history")
    return {
        "schema_version": _SCHEMA_VERSION,
        "registry_revision": revision,
        "bindings": sorted(bindings, key=_binding_sort_key),
        "history": history,
        "updated_at": str(value.get("updated_at") or ""),
    }


def _validate_binding(value: object) -> dict[str, object]:
    required = {
        "binding_id", "binding_revision", "project_id", "skill_id", "skill_fingerprint",
        "allowed_consumers", "priority", "trigger_terms", "status", "activated_at", "updated_at",
    }
    if not isinstance(value, Mapping):
        raise ApplicationSkillBindingError("Application Skill binding must be an object")
    expected = set(required)
    if value.get("status") == "inactive":
        expected |= {"deactivated_at", "deactivation_reason"}
    provenance = {"source_id", "source_kind"}
    if set(value) != expected and set(value) != expected | provenance:
        raise ApplicationSkillBindingError("Application Skill binding fields drifted")
    project, skill = _project_id(value.get("project_id")), _skill_id(value.get("skill_id"))
    if value.get("binding_id") != _binding_id(project, skill):
        raise ApplicationSkillBindingError("Application Skill binding identity drifted")
    binding_revision = value.get("binding_revision")
    if not isinstance(binding_revision, int) or isinstance(binding_revision, bool) or binding_revision < 1:
        raise ApplicationSkillBindingError("invalid Application Skill binding revision")
    if not _SHA256.fullmatch(str(value.get("skill_fingerprint") or "")):
        raise ApplicationSkillBindingError("invalid Application Skill binding fingerprint")
    source_id = value.get("source_id", "unknown")
    source_kind = value.get("source_kind", "unknown")
    if source_kind != "unknown" and source_kind not in _SOURCE_KINDS:
        raise ApplicationSkillBindingError("invalid Application Skill binding source kind")
    if source_kind == "unknown":
        if source_id != "unknown":
            raise ApplicationSkillBindingError("invalid unknown Application Skill binding provenance")
    elif not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
        raise ApplicationSkillBindingError("invalid Application Skill binding source id")
    consumers = value.get("allowed_consumers")
    terms = value.get("trigger_terms")
    if not isinstance(consumers, Sequence) or isinstance(consumers, (str, bytes)):
        raise ApplicationSkillBindingError("Application Skill consumers must be an array")
    if not isinstance(terms, Sequence) or isinstance(terms, (str, bytes)):
        raise ApplicationSkillBindingError("Application Skill trigger terms must be an array")
    clean_consumers = sorted({_consumer(item) for item in consumers})
    clean_terms = _trigger_terms(terms)
    if not clean_consumers or list(consumers) != clean_consumers or list(terms) != clean_terms:
        raise ApplicationSkillBindingError("Application Skill binding arrays are not canonical")
    priority = value.get("priority")
    if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 1000:
        raise ApplicationSkillBindingError("invalid Application Skill priority")
    if value.get("status") not in {"active", "inactive"}:
        raise ApplicationSkillBindingError("invalid Application Skill binding status")
    for key in ("activated_at", "updated_at"):
        if not isinstance(value.get(key), str) or not str(value[key]).strip():
            raise ApplicationSkillBindingError(f"invalid Application Skill {key}")
    if value["status"] == "inactive":
        _required_reason(value.get("deactivation_reason"), "deactivation reason")
        if not isinstance(value.get("deactivated_at"), str) or not str(value["deactivated_at"]).strip():
            raise ApplicationSkillBindingError("invalid Application Skill deactivated_at")
    # Older schema-1 records did not bind a package origin.  Preserve their
    # readability, but surface provenance as explicitly unknown so callers
    # with a source-isolation policy can fail closed instead of guessing.
    return {**deepcopy(dict(value)), "source_id": source_id, "source_kind": source_kind}


def _validate_history(value: object) -> dict[str, object]:
    expected = {
        "registry_revision", "action", "binding_id", "project_id", "skill_id", "binding_revision",
        "before", "after", "reason", "recorded_at",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ApplicationSkillBindingError("invalid Application Skill history event")
    revision = value.get("registry_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ApplicationSkillBindingError("invalid Application Skill history revision")
    if value.get("action") not in {"activated", "rebound", "deactivated"}:
        raise ApplicationSkillBindingError("invalid Application Skill history action")
    project, skill = _project_id(value.get("project_id")), _skill_id(value.get("skill_id"))
    if value.get("binding_id") != _binding_id(project, skill):
        raise ApplicationSkillBindingError("Application Skill history identity drifted")
    before, after = value.get("before"), value.get("after")
    if not isinstance(before, Sequence) or isinstance(before, (str, bytes)) or len(before) > 1:
        raise ApplicationSkillBindingError("invalid Application Skill history before snapshot")
    if not isinstance(after, Sequence) or isinstance(after, (str, bytes)) or len(after) != 1:
        raise ApplicationSkillBindingError("invalid Application Skill history after snapshot")
    clean_before = [_validate_binding(item) for item in before]
    clean_after = [_validate_binding(item) for item in after]
    if any(item["binding_id"] != value["binding_id"] for item in [*clean_before, *clean_after]):
        raise ApplicationSkillBindingError("Application Skill history snapshot identity drifted")
    if value.get("binding_revision") != clean_after[0]["binding_revision"]:
        raise ApplicationSkillBindingError("Application Skill history binding revision drifted")
    _required_reason(value.get("reason"), "history reason")
    if not isinstance(value.get("recorded_at"), str) or not str(value["recorded_at"]).strip():
        raise ApplicationSkillBindingError("invalid Application Skill history timestamp")
    return {**deepcopy(dict(value)), "before": clean_before, "after": clean_after}


def _find_binding(bindings: object, project_id: str, skill_id: str) -> dict[str, object] | None:
    if not isinstance(bindings, Sequence) or isinstance(bindings, (str, bytes)):
        return None
    item = next(
        (
            value
            for value in bindings
            if isinstance(value, Mapping)
            and value.get("project_id") == project_id
            and value.get("skill_id") == skill_id
        ),
        None,
    )
    return deepcopy(dict(item)) if item is not None else None


def _binding_matches_package(
    binding: Mapping[str, object], package: ApplicationSkillPackage,
) -> bool:
    """Require immutable package bytes and persisted source provenance to agree."""
    return (
        package.fingerprint == binding["skill_fingerprint"]
        and binding["source_kind"] != "unknown"
        and package.source_id == binding["source_id"]
        and package.source_kind == binding["source_kind"]
    )


def _project_id(value: object) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not _PROJECT_ID.fullmatch(text):
        raise ApplicationSkillBindingError("invalid Application Skill project id")
    return text


def _skill_id(value: object) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not _SKILL_ID.fullmatch(text):
        raise ApplicationSkillBindingError("invalid Application Skill id")
    return text


def _consumer(value: object) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if text not in _ALLOWED_CONSUMERS:
        raise ApplicationSkillBindingError("unsupported Application Skill consumer")
    return text


def _trigger_terms(values: Sequence[object]) -> list[str]:
    if len(values) > _MAX_TRIGGER_TERMS:
        raise ApplicationSkillBindingError("too many Application Skill trigger terms")
    clean: set[str] = set()
    for value in values:
        text = " ".join(value.strip().casefold().split()) if isinstance(value, str) else ""
        if not text or len(text) > 100 or _SECRET.search(text):
            raise ApplicationSkillBindingError("invalid or sensitive Application Skill trigger term")
        clean.add(text)
    return sorted(clean)


def _confirmation(confirm: bool, reason: str, action: str) -> str:
    if confirm is not True:
        raise ApplicationSkillBindingError(f"Application Skill {action} requires explicit confirmation")
    return _required_reason(reason, f"{action} reason")


def _required_reason(value: object, label: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > 500 or _SECRET.search(text):
        raise ApplicationSkillBindingError(f"invalid or sensitive Application Skill {label}")
    return text


def _binding_id(project_id: str, skill_id: str) -> str:
    digest = hashlib.sha256(f"{project_id}\0{skill_id}".encode()).hexdigest()[:24]
    return f"skill-binding-{digest}"


def _preview_token(registry_revision: int, candidate: Mapping[str, object], current: object) -> str:
    canonical = deepcopy(dict(candidate))
    for key in ("activated_at", "updated_at"):
        canonical.pop(key, None)
    return hashlib.sha256(
        json.dumps(
            {"registry_revision": registry_revision, "candidate": canonical, "current": current},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _batch_preview_token(
    registry_revision: int,
    candidates: Sequence[Mapping[str, object]],
    current: Sequence[object],
) -> str:
    canonical_candidates = []
    for candidate in candidates:
        clean = deepcopy(dict(candidate))
        for key in ("activated_at", "updated_at"):
            clean.pop(key, None)
        canonical_candidates.append(clean)
    return hashlib.sha256(
        json.dumps(
            {
                "registry_revision": registry_revision,
                "candidates": canonical_candidates,
                "current": current,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _binding_sort_key(binding: Mapping[str, object]) -> tuple[str, int, str]:
    return (str(binding["project_id"]), -int(binding["priority"]), str(binding["skill_id"]))
