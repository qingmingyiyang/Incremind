from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .binding_registry import ApplicationSkillBindingRegistry
from .package_catalog import (
    ApplicationSkillCatalog,
    ApplicationSkillCatalogSnapshot,
    ApplicationSkillError,
    ApplicationSkillPackage,
    ApplicationSkillSource,
)
from .resolver import ApplicationSkillResolver
from .consumer_runtime import ObjectStoreApplicationSkillTraceRepository


class ApplicationSkillManagementError(ValueError):
    """Raised when a local management operation violates the Skill contract."""


class ApplicationSkillManagementConflict(ApplicationSkillManagementError):
    """Raised when a preview or local package target drifted."""


class ApplicationSkillManagementStorePort(Protocol):
    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None: ...

    def revision(self, collection: str, object_id: str) -> int: ...

    def write(
        self,
        collection: str,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int | None,
    ) -> int: ...

    def list(self, collection: str) -> Sequence[Mapping[str, object]]: ...


_SAFE_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_MAX_INVOCATIONS = 100
_PROPOSAL_COLLECTION = "application_skill_proposals"


class ApplicationSkillProposalRegistry:
    """Durable review gate for every production Skill mutation."""

    def __init__(self, store: ApplicationSkillManagementStorePort) -> None:
        self._store = store

    def propose(self, action: str, payload: Mapping[str, object]) -> dict[str, object]:
        # Proposal payloads may contain nested, evidence-bound structures.  A
        # JSON canonical form is deterministic across mapping insertion order
        # and avoids depending on Python's repr for durable identities.
        canonical = json.dumps(
            {"action": action, "payload": _canonical_payload(payload)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        proposal_id = f"skill-proposal-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:24]}"
        existing = self._store.read(_PROPOSAL_COLLECTION, proposal_id)
        if existing is not None:
            return dict(existing)
        now = datetime.now(timezone.utc).isoformat()
        record = {
            "proposal_id": proposal_id,
            "schema_version": "1.0.0",
            "action": action,
            "status": "pending_review",
            "payload": dict(payload),
            "created_at": now,
            "reviewed_at": None,
            "review_reason": None,
        }
        self._store.write(_PROPOSAL_COLLECTION, proposal_id, record, expected_revision=0)
        return record

    def require_approved(
        self,
        proposal_id: str,
        *,
        action: str,
        expected_payload: Mapping[str, object],
        confirm: bool,
        reason: str,
    ) -> dict[str, object]:
        if confirm is not True:
            raise ApplicationSkillManagementError("Application Skill Proposal requires confirmation")
        clean_reason = reason.strip() if isinstance(reason, str) else ""
        if not clean_reason or len(clean_reason) > 500:
            raise ApplicationSkillManagementError("Application Skill Proposal review reason is required")
        record = self._store.read(_PROPOSAL_COLLECTION, proposal_id)
        if record is None or record.get("action") != action:
            raise ApplicationSkillManagementConflict("Application Skill Proposal was not found")
        if record.get("payload") != dict(expected_payload):
            raise ApplicationSkillManagementConflict("Application Skill Proposal payload drifted")
        if record.get("status") == "applied":
            return dict(record)
        if record.get("status") not in {"pending_review", "approved"}:
            raise ApplicationSkillManagementConflict("Application Skill Proposal is not reviewable")
        approved = {
            **record,
            "status": "approved",
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "review_reason": clean_reason,
        }
        revision = self._store.revision(_PROPOSAL_COLLECTION, proposal_id)
        self._store.write(_PROPOSAL_COLLECTION, proposal_id, approved, expected_revision=revision)
        return approved

    def mark_applied(self, proposal_id: str) -> dict[str, object]:
        record = self._store.read(_PROPOSAL_COLLECTION, proposal_id)
        if record is None or record.get("status") not in {"approved", "applied"}:
            raise ApplicationSkillManagementConflict("Application Skill Proposal is not approved")
        if record.get("status") == "applied":
            return dict(record)
        applied = {
            **record,
            "status": "applied",
            "applied_at": datetime.now(timezone.utc).isoformat(),
        }
        revision = self._store.revision(_PROPOSAL_COLLECTION, proposal_id)
        self._store.write(_PROPOSAL_COLLECTION, proposal_id, applied, expected_revision=revision)
        return applied

    def reject(
        self,
        proposal_id: str,
        *,
        action: str,
        expected_payload: Mapping[str, object],
        confirm: bool,
        reason: str,
    ) -> dict[str, object]:
        """Record an explicit review rejection without mutating a Skill."""
        if confirm is not True:
            raise ApplicationSkillManagementError("Application Skill Proposal requires confirmation")
        clean_reason = reason.strip() if isinstance(reason, str) else ""
        if not clean_reason or len(clean_reason) > 500:
            raise ApplicationSkillManagementError("Application Skill Proposal review reason is required")
        record = self._store.read(_PROPOSAL_COLLECTION, proposal_id)
        if record is None or record.get("action") != action:
            raise ApplicationSkillManagementConflict("Application Skill Proposal was not found")
        if record.get("payload") != dict(expected_payload):
            raise ApplicationSkillManagementConflict("Application Skill Proposal payload drifted")
        if record.get("status") == "rejected":
            return dict(record)
        if record.get("status") != "pending_review":
            raise ApplicationSkillManagementConflict("Application Skill Proposal is not reviewable")
        rejected = {
            **record,
            "status": "rejected",
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "review_reason": clean_reason,
        }
        revision = self._store.revision(_PROPOSAL_COLLECTION, proposal_id)
        self._store.write(_PROPOSAL_COLLECTION, proposal_id, rejected, expected_revision=revision)
        return rejected

    def list(self) -> list[dict[str, object]]:
        return [dict(item) for item in self._store.list(_PROPOSAL_COLLECTION)]


def _canonical_payload(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_payload(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical_payload(item) for item in value]
    raise ApplicationSkillManagementError("Application Skill Proposal payload is not JSON-safe")


@dataclass(frozen=True, slots=True)
class ApplicationSkillImportService:
    catalog: ApplicationSkillCatalog
    target_root: Path

    def preview(self, source_path: str) -> dict[str, object]:
        self._require_safe_target_root()
        source = _selected_path(source_path)
        package = self._inspect(source)
        target = self.target_root / package.skill_id
        target_state = self._target_state(target, package)
        return {
            "status": "validated",
            "action": target_state,
            "package": _package_projection(package),
            "preview_token": _import_token(source, package, target_state),
            "write_effect": "none",
        }

    def confirm(
        self,
        source_path: str,
        *,
        expected_fingerprint: str,
        preview_token: str,
        confirm: bool,
        reason: str,
    ) -> dict[str, object]:
        if confirm is not True:
            raise ApplicationSkillManagementError("Application Skill import requires confirmation")
        clean_reason = reason.strip() if isinstance(reason, str) else ""
        if not clean_reason or len(clean_reason) > 500:
            raise ApplicationSkillManagementError("Application Skill import reason is required")
        preview = self.preview(source_path)
        package = preview["package"]
        if not isinstance(package, Mapping) or package.get("fingerprint") != expected_fingerprint:
            raise ApplicationSkillManagementConflict("Application Skill import fingerprint drifted")
        if preview["preview_token"] != preview_token:
            raise ApplicationSkillManagementConflict("Application Skill import preview drifted")
        if preview["action"] == "already_imported":
            return {**preview, "status": "already_imported", "replayed": True}
        if preview["action"] != "import":
            raise ApplicationSkillManagementConflict("Application Skill import target conflicts")

        source = _selected_path(source_path)
        skill_id = str(package["skill_id"])
        self.target_root.mkdir(parents=True, exist_ok=True)
        target = self.target_root / skill_id
        temporary_root = self.target_root / f".import-{secrets.token_hex(8)}"
        temporary = temporary_root / skill_id
        try:
            shutil.copytree(source, temporary, symlinks=True)
            copied = self._inspect(temporary)
            if copied.skill_id != skill_id or copied.fingerprint != expected_fingerprint:
                raise ApplicationSkillManagementConflict("Application Skill copied package drifted")
            if target.exists():
                raise ApplicationSkillManagementConflict("Application Skill import target appeared")
            os.replace(temporary, target)
        except (OSError, shutil.Error) as error:
            raise ApplicationSkillManagementError("Application Skill import copy failed") from error
        finally:
            if temporary_root.exists():
                shutil.rmtree(temporary_root, ignore_errors=True)
        installed = self._inspect(target)
        return {
            "status": "imported",
            "action": "import",
            "package": _package_projection(installed),
            "replayed": False,
        }

    def _require_safe_target_root(self) -> None:
        if self.target_root.is_symlink():
            raise ApplicationSkillManagementConflict(
                "Application Skill target root cannot be a symlink"
            )

    def _inspect(self, path: Path) -> ApplicationSkillPackage:
        try:
            return self.catalog.inspect_package(path)
        except ApplicationSkillError as error:
            raise ApplicationSkillManagementError(str(error)) from error

    def _target_state(self, target: Path, candidate: ApplicationSkillPackage) -> str:
        if target.is_symlink():
            raise ApplicationSkillManagementConflict(
                "Application Skill import target cannot be a symlink"
            )
        if not target.exists():
            return "import"
        try:
            existing = self._inspect(target)
        except ApplicationSkillManagementError as error:
            raise ApplicationSkillManagementConflict(
                "Application Skill import target is invalid"
            ) from error
        if existing.fingerprint == candidate.fingerprint:
            return "already_imported"
        raise ApplicationSkillManagementConflict(
            "Application Skill import target already contains a different package"
        )


class ApplicationSkillManagementService:
    def __init__(
        self,
        *,
        catalog: ApplicationSkillCatalog,
        sources: Sequence[ApplicationSkillSource],
        bindings: ApplicationSkillBindingRegistry,
        resolver: ApplicationSkillResolver,
        store: ApplicationSkillManagementStorePort,
    ) -> None:
        self._catalog = catalog
        self._sources = tuple(sources)
        self._bindings = bindings
        self._resolver = resolver
        self._store = store

    def status(self) -> dict[str, object]:
        snapshot = self._snapshot()
        return {
            "schema_version": "1.0.0",
            "catalog": _catalog_projection(snapshot),
            "registry": self._bindings.status(snapshot),
            "proposals": [
                {
                    key: item.get(key)
                    for key in (
                        "proposal_id", "action", "status", "created_at",
                        "reviewed_at", "review_reason",
                    )
                }
                for item in ApplicationSkillProposalRegistry(self._store).list()
            ],
        }

    def preview_binding(
        self,
        *,
        skill_id: str,
        project_id: str,
        allowed_consumers: Sequence[str],
        priority: int,
        trigger_terms: Sequence[str],
    ) -> dict[str, object]:
        package = self._package(skill_id)
        preview = self._bindings.preview_bind(
            package,
            project_id=project_id,
            allowed_consumers=allowed_consumers,
            priority=priority,
            trigger_terms=trigger_terms,
        )
        proposal = ApplicationSkillProposalRegistry(self._store).propose(
            "binding.activate",
            {
                "skill_id": skill_id,
                "project_id": project_id,
                "allowed_consumers": list(allowed_consumers),
                "priority": priority,
                "trigger_terms": list(trigger_terms),
                "registry_revision": preview["registry_revision"],
                "preview_token": preview["preview_token"],
            },
        )
        return {
            **preview,
            "proposal_id": proposal["proposal_id"],
            "proposal_status": proposal["status"],
        }

    def activate_binding(self, **values: object) -> dict[str, object]:
        package = self._package(str(values.get("skill_id") or ""))
        payload = {
            "skill_id": str(values.get("skill_id") or ""),
            "project_id": str(values.get("project_id") or ""),
            "allowed_consumers": list(
                _strings(values.get("allowed_consumers"), "allowed_consumers")
            ),
            "priority": _integer(values.get("priority"), "priority"),
            "trigger_terms": list(_strings(values.get("trigger_terms"), "trigger_terms")),
            "registry_revision": _integer(
                values.get("expected_registry_revision"), "expected_registry_revision"
            ),
            "preview_token": str(values.get("preview_token") or ""),
        }
        proposals = ApplicationSkillProposalRegistry(self._store)
        proposal_id = str(values.get("proposal_id") or "")
        approved = proposals.require_approved(
            proposal_id,
            action="binding.activate",
            expected_payload=payload,
            confirm=values.get("confirm") is True,
            reason=str(values.get("reason") or ""),
        )
        if approved.get("status") == "applied":
            return {
                **self._bindings.status(self._snapshot()),
                "status": "already_active",
                "replayed": True,
                "proposal_id": proposal_id,
                "proposal_status": "applied",
            }
        result = self._bindings.activate(
            package,
            project_id=str(payload["project_id"]),
            allowed_consumers=payload["allowed_consumers"],
            priority=int(payload["priority"]),
            trigger_terms=payload["trigger_terms"],
            expected_registry_revision=int(payload["registry_revision"]),
            preview_token=str(payload["preview_token"]),
            confirm=values.get("confirm") is True,
            reason=str(values.get("reason") or ""),
        )
        proposals.mark_applied(proposal_id)
        return {**result, "proposal_id": proposal_id, "proposal_status": "applied"}

    def deactivate_binding(self, **values: object) -> dict[str, object]:
        payload = {
            "project_id": str(values.get("project_id") or ""),
            "skill_id": str(values.get("skill_id") or ""),
            "registry_revision": _integer(
                values.get("expected_registry_revision"), "expected_registry_revision"
            ),
        }
        proposals = ApplicationSkillProposalRegistry(self._store)
        proposal_id = str(values.get("proposal_id") or "")
        approved = proposals.require_approved(
            proposal_id,
            action="binding.deactivate",
            expected_payload=payload,
            confirm=values.get("confirm") is True,
            reason=str(values.get("reason") or ""),
        )
        if approved.get("status") == "applied":
            return {
                **self._bindings.status(self._snapshot()),
                "status": "already_inactive",
                "replayed": True,
                "proposal_id": proposal_id,
                "proposal_status": "applied",
            }
        result = self._bindings.deactivate(
            project_id=payload["project_id"],
            skill_id=payload["skill_id"],
            expected_registry_revision=payload["registry_revision"],
            confirm=True,
            reason=str(values.get("reason") or ""),
        )
        proposals.mark_applied(proposal_id)
        return {**result, "proposal_id": proposal_id, "proposal_status": "applied"}

    def preview_deactivation(
        self, *, project_id: str, skill_id: str, expected_registry_revision: int
    ) -> dict[str, object]:
        payload = {
            "project_id": project_id,
            "skill_id": skill_id,
            "registry_revision": expected_registry_revision,
        }
        proposal = ApplicationSkillProposalRegistry(self._store).propose(
            "binding.deactivate", payload
        )
        return {
            "status": "pending_review",
            "action": "deactivate",
            "proposal_id": proposal["proposal_id"],
            "proposal_status": proposal["status"],
            **payload,
            "write_effect": "proposal_only",
        }

    def preview_resolution(
        self,
        *,
        project_id: str,
        consumer: str,
        task_kind: str,
        task_text: str,
        project_summary: str = "",
    ) -> dict[str, object]:
        preview = self._resolver.preview(
            self._snapshot(),
            project_id=project_id,
            consumer=consumer,
            task_kind=task_kind,
            task_text=task_text,
            project_summary=project_summary,
        )
        return {
            "preview_id": preview.preview_id,
            "project_id": preview.project_id,
            "consumer": preview.consumer,
            "task_kind": preview.task_kind,
            "matched": [_match_projection(item) for item in preview.matched],
            "selected": [_match_projection(item) for item in preview.selected_matches],
            "budget_excluded_skill_ids": list(preview.budget_excluded_skill_ids),
            "estimated_instruction_bytes": preview.estimated_instruction_bytes,
            "task_fingerprint": preview.task_fingerprint,
            "write_effect": "none",
        }

    def invocations(self, project_id: str) -> dict[str, object]:
        clean_project = _project_id(project_id)
        repository = ObjectStoreApplicationSkillTraceRepository(self._store)
        items = []
        for raw in self._store.list("application_skill_resolution_traces"):
            resolution_id = raw.get("resolution_id")
            if not isinstance(resolution_id, str):
                raise ApplicationSkillManagementError(
                    "Application Skill invocation identity is invalid"
                )
            trace = repository.get_trace(resolution_id)
            if trace is not None and trace.get("project_id") == clean_project:
                items.append(_trace_projection(trace))
        items.sort(key=lambda item: str(item["recorded_at"]), reverse=True)
        return {"project_id": clean_project, "invocations": items[:_MAX_INVOCATIONS]}

    def project_summary(self, project_id: str) -> dict[str, object]:
        clean_project = _project_id(project_id)
        snapshot = self._snapshot()
        bindings = [
            item
            for item in self._bindings.status(snapshot)["bindings"]
            if isinstance(item, Mapping) and item.get("project_id") == clean_project
        ]
        invocations = self.invocations(clean_project)["invocations"]
        last_by_skill: dict[str, str] = {}
        for trace in invocations:
            for selected in trace["selected"]:
                last_by_skill.setdefault(str(selected["skill_id"]), str(trace["recorded_at"]))
        packages = {item.skill_id: item for item in snapshot.packages}
        methods = []
        for binding in bindings:
            package = packages.get(str(binding.get("skill_id") or ""))
            methods.append(
                {
                    "skill_id": binding.get("skill_id"),
                    "name": package.name if package is not None else binding.get("skill_id"),
                    "description": package.description if package is not None else "方法包当前不可用。",
                    "trigger_boundary": package.trigger_boundary if package is not None else None,
                    "validation": package.validation if package is not None else None,
                    "maturity": package.maturity if package is not None else None,
                    "status": binding.get("effective_status"),
                    "consumers": list(binding.get("allowed_consumers") or []),
                    "last_used_at": last_by_skill.get(str(binding.get("skill_id") or "")),
                }
            )
        return {"project_id": clean_project, "methods": methods}

    def _snapshot(self) -> ApplicationSkillCatalogSnapshot:
        return self._catalog.discover(self._sources)

    def _package(self, skill_id: str) -> ApplicationSkillPackage:
        package = self._snapshot().get(skill_id)
        if package is None:
            raise ApplicationSkillManagementError("Application Skill package is unavailable")
        return package


def _catalog_projection(snapshot: ApplicationSkillCatalogSnapshot) -> dict[str, object]:
    return {
        "scanned_source_count": snapshot.scanned_source_count,
        "packages": [_package_projection(item) for item in snapshot.packages],
        "issues": [
            {
                "source_id": item.source_id,
                "package_name": item.package_name,
                "code": item.code,
                "detail": item.detail,
            }
            for item in snapshot.issues
        ],
    }


def _package_projection(package: ApplicationSkillPackage) -> dict[str, object]:
    return {
        "skill_id": package.skill_id,
        "name": package.name,
        "description": package.description,
        "trigger_boundary": package.trigger_boundary,
        "validation": package.validation,
        "maturity": package.maturity,
        "source_id": package.source_id,
        "source_kind": package.source_kind,
        "fingerprint": package.fingerprint,
        "instruction_size_bytes": package.instruction_size_bytes,
        "package_size_bytes": package.package_size_bytes,
        "resources": [
            {
                "relative_path": item.relative_path,
                "resource_kind": item.resource_kind,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
            }
            for item in package.resources
        ],
    }


def _match_projection(item: object) -> dict[str, object]:
    return {
        "skill_id": getattr(item, "skill_id"),
        "skill_fingerprint": getattr(item, "skill_fingerprint"),
        "binding_id": getattr(item, "binding_id"),
        "binding_revision": getattr(item, "binding_revision"),
        "score": getattr(item, "score"),
        "priority": getattr(item, "priority"),
        "reasons": list(getattr(item, "reasons")),
    }


def _trace_projection(trace: Mapping[str, object]) -> dict[str, object]:
    return {
        "turn_id": trace.get("invocation_id") if trace.get("consumer") == "turn.workbench-question" else None,
        "resolution_id": trace.get("resolution_id"),
        "consumer": trace.get("consumer"),
        "task_kind": trace.get("task_kind"),
        "selected": [
            {
                "skill_id": item.get("skill_id"),
                "skill_fingerprint": item.get("skill_fingerprint"),
                "binding_revision": item.get("binding_revision"),
            }
            for item in trace.get("selected", [])
            if isinstance(item, Mapping)
        ],
        "fallback": trace.get("fallback"),
        "loaded_instruction_bytes": trace.get("loaded_instruction_bytes"),
        "context_size_bytes": trace.get("context_size_bytes"),
        "recorded_at": trace.get("recorded_at"),
    }


def _selected_path(value: object) -> Path:
    text = value.strip() if isinstance(value, str) else ""
    if not text or "\x00" in text:
        raise ApplicationSkillManagementError("Application Skill source path is required")
    return Path(text).expanduser()


def _import_token(source: Path, package: ApplicationSkillPackage, action: str) -> str:
    resolved = str(source.resolve(strict=True))
    digest = hashlib.sha256(
        "\n".join((resolved, package.skill_id, package.fingerprint, action)).encode("utf-8")
    ).hexdigest()
    return f"skill-import-preview-{digest}"


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ApplicationSkillManagementError(f"{label} must be an array")
    return tuple(str(item) for item in value)


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ApplicationSkillManagementError(f"{label} must be an integer")
    return value


def _project_id(value: object) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not _SAFE_PROJECT_ID.fullmatch(text):
        raise ApplicationSkillManagementError("invalid project_id")
    return text
