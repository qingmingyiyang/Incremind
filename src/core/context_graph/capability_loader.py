from __future__ import annotations

import json
import re
import sqlite3
import ast
import uuid
from dataclasses import asdict, dataclass, replace as dataclass_replace
from pathlib import Path
from typing import Mapping
from .capability_artifact import CapabilityArtifactError, CapabilityArtifactStore


class CapabilityPackageError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CapabilityPackageDesiredState:
    """Durable operator intent, separate from the currently materialized bundle."""

    capability_id: str
    state: str
    state_revision: int
    command_id: str
    audit_ref: str


@dataclass(frozen=True, slots=True)
class CapabilityPackageManifest:
    schema_version: str
    core_api: str
    capability_id: str
    capability_revision: str
    display_name: str
    kind: str
    execution_state_owner: str
    recovery_owner: str
    secret_access: str
    memory_write: str
    document_write: str
    project_skill_write: str
    contributions: tuple[str, ...]
    provides: Mapping[str, tuple[str, ...]]
    tools: tuple[Mapping[str, str], ...]
    workflows: tuple[Mapping[str, str], ...]
    permissions: Mapping[str, object]
    budgets: Mapping[str, int]
    effects: Mapping[str, str]
    ui: Mapping[str, object]
    tests: tuple[str, ...]
    context: tuple[Mapping[str, str], ...] = ()
    artifact_id: str = ""


class CapabilityPackageLoader:
    """Core-owned package catalog; packages contribute declarations, never runtime state."""

    _FIELDS = {
        "schema_version", "core_api", "capability_id", "capability_revision", "display_name", "kind",
        "execution_state_owner", "recovery_owner", "secret_access", "memory_write",
        "document_write", "project_skill_write", "contributions", "provides", "tools",
        "workflows", "permissions", "budgets", "effects", "ui", "tests",
    }
    _CONTEXT_FIELD = "context"
    _LEGACY_FIELDS = {
        "schema_version", "capability_id", "capability_revision", "display_name",
        "execution_state_owner", "recovery_owner", "secret_access", "memory_write",
        "document_write", "project_skill_write", "contributions",
    }
    _CONTRIBUTIONS = {
        "importer", "exporter", "renderer", "context_compiler_extension",
        "proposal_adapter", "validation_schema", "ui_contribution",
        "evaluation_fixture", "read_only_preview", "migration_adapter",
    }
    _EXPECTED_OWNERS = {
        "execution_state_owner": "core_effect_log",
        "recovery_owner": "core_reaper",
        "secret_access": "lease_reference_only",
        "memory_write": "proposal_only",
        "document_write": "draft_only",
        "project_skill_write": "proposal_only",
    }
    _CAPABILITY_ID = re.compile(r"[a-z][a-z0-9_]{2,127}\Z")
    _REVISION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
    _ENTRYPOINT = re.compile(r"[a-zA-Z0-9_./-]+\.py:[a-zA-Z_][a-zA-Z0-9_]*\Z")
    _SOURCE_TYPE = re.compile(r"[a-z][a-z0-9_]{1,63}\Z")
    _EFFECT_CLASSES = frozenset({"PURE", "IDEMPOTENT", "QUERYABLE", "AT_MOST_ONCE", "NEEDS_REAUTH"})
    _KINDS = frozenset({"platform", "context_extension", "agent_client", "read_only_view"})

    def __init__(
        self, database_path: Path | None = None, *, trusted_packages_root: Path | None = None,
    ) -> None:
        self._database_path = (
            Path(database_path).resolve(strict=False) if database_path is not None else None
        )
        self._active: dict[str, CapabilityPackageManifest] = {}
        self._history: dict[str, list[CapabilityPackageManifest]] = {}
        self._desired: dict[str, CapabilityPackageDesiredState] = {}
        self._manifest_sources: dict[tuple[str, str], Path] = {}
        self._trusted_packages_root = (
            Path(trusted_packages_root).resolve(strict=False)
            if trusted_packages_root is not None else None
        )
        self._artifact_store = (
            CapabilityArtifactStore(self._database_path.parent / "capability-artifacts")
            if self._database_path is not None else None
        )
        if self._database_path is not None:
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize_schema()
            self._reload()

    @property
    def database_path(self) -> Path | None:
        return self._database_path

    def load_manifest(self, authorized_manifest_path: Path) -> CapabilityPackageManifest:
        path = Path(authorized_manifest_path)
        if not path.is_absolute() or not path.is_file():
            raise CapabilityPackageError("authorized_manifest_required")
        source_root = path.parent
        if self._trusted_packages_root is not None:
            try:
                source_root.resolve(strict=True).relative_to(self._trusted_packages_root.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise CapabilityPackageError("capability_package_source_outside_trusted_root") from exc
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise CapabilityPackageError("invalid_manifest_json") from exc
        manifest = self._manifest_from_payload(payload)
        self._validate_package_files(source_root, manifest)
        self._manifest_sources[(manifest.capability_id, manifest.capability_revision)] = source_root
        return manifest

    def artifact_root(self, manifest: CapabilityPackageManifest) -> Path:
        if self._artifact_store is None or not manifest.artifact_id:
            raise CapabilityPackageError("capability_artifact_unavailable")
        try:
            return self._artifact_store.verify(
                manifest.capability_id, manifest.capability_revision, manifest.artifact_id,
            ).root
        except CapabilityArtifactError as exc:
            raise CapabilityPackageError(str(exc)) from exc

    def discover(self, authorized_packages_root: Path) -> tuple[CapabilityPackageManifest, ...]:
        root = Path(authorized_packages_root)
        if not root.is_absolute() or not root.is_dir():
            raise CapabilityPackageError("authorized_packages_root_required")
        if self._trusted_packages_root is not None and root.resolve(strict=True) != self._trusted_packages_root.resolve(strict=True):
            raise CapabilityPackageError("capability_package_source_outside_trusted_root")
        manifests: list[CapabilityPackageManifest] = []
        identities: set[str] = set()
        for path in sorted(root.glob("*/manifest.json")):
            manifest = self.load_manifest(path)
            if manifest.capability_id in identities:
                raise CapabilityPackageError("duplicate_capability_identity")
            identities.add(manifest.capability_id)
            manifests.append(manifest)
        return tuple(manifests)

    def install(
        self,
        manifest: CapabilityPackageManifest,
        *,
        command_id: str | None = None,
        audit_ref: str | None = None,
    ) -> CapabilityPackageManifest:
        self._validate_manifest(manifest)
        manifest = self._capture_artifact(manifest)
        if manifest.capability_id in self._active:
            raise CapabilityPackageError("capability_already_installed")
        command_id, audit_ref = self._command_identity(
            "install", manifest.capability_id, manifest.capability_revision, command_id, audit_ref,
        )
        if self._database_path is None:
            self._active[manifest.capability_id] = manifest
            history = self._history.setdefault(manifest.capability_id, [])
            if manifest not in history:
                history.append(manifest)
            prior = self._desired.get(manifest.capability_id)
            self._desired[manifest.capability_id] = CapabilityPackageDesiredState(
                manifest.capability_id, "enabled",
                (prior.state_revision if prior is not None else 0) + 1,
                command_id, audit_ref,
            )
            return manifest
        with self._connect() as connection:
            restored_legacy_pointer = False
            recorded = next(
                (
                    item for item in self._history.get(manifest.capability_id, ())
                    if item.capability_revision == manifest.capability_revision
                ),
                None,
            )
            if recorded is not None and recorded != manifest:
                if dataclass_replace(recorded, artifact_id="") != dataclass_replace(manifest, artifact_id="") or recorded.artifact_id:
                    raise CapabilityPackageError("capability_revision_manifest_drift")
                changed = connection.execute(
                    "UPDATE capability_package_revisions SET artifact_id = ? "
                    "WHERE capability_id = ? AND capability_revision = ? AND artifact_id = ''",
                    (manifest.artifact_id, manifest.capability_id, manifest.capability_revision),
                ).rowcount
                if changed != 1:
                    raise CapabilityPackageError("capability_revision_manifest_drift")
                restored_legacy_pointer = connection.execute(
                    "SELECT 1 FROM capability_package_active WHERE capability_id = ? AND capability_revision = ?",
                    (manifest.capability_id, manifest.capability_revision),
                ).fetchone() is not None
            if recorded is None:
                self._append_revision(connection, manifest, operation="install")
            else:
                self._record_event(connection, manifest, operation="install")
            if not restored_legacy_pointer:
                prior_pointer = connection.execute(
                    "SELECT capability_revision FROM capability_package_active WHERE capability_id = ?",
                    (manifest.capability_id,),
                ).fetchone()
                if prior_pointer is None:
                    connection.execute(
                        "INSERT INTO capability_package_active(capability_id, capability_revision) VALUES (?, ?)",
                        (manifest.capability_id, manifest.capability_revision),
                    )
                else:
                    previous_artifact = connection.execute(
                        "SELECT artifact_id FROM capability_package_revisions WHERE capability_id = ? AND capability_revision = ?",
                        (manifest.capability_id, prior_pointer["capability_revision"]),
                    ).fetchone()
                    if previous_artifact is None or previous_artifact["artifact_id"]:
                        raise CapabilityPackageError("capability_revision_manifest_drift")
                    connection.execute(
                        "UPDATE capability_package_active SET capability_revision = ? WHERE capability_id = ?",
                        (manifest.capability_revision, manifest.capability_id),
                    )
            self._set_desired_state(
                connection, capability_id=manifest.capability_id, state="enabled",
                command_id=command_id, audit_ref=audit_ref,
            )
        self._reload()
        return manifest

    def upgrade(
        self, manifest: CapabilityPackageManifest, *, expected_revision: str,
    ) -> CapabilityPackageManifest:
        self._validate_manifest(manifest)
        manifest = self._capture_artifact(manifest)
        current = self._active.get(manifest.capability_id)
        if current is None:
            raise CapabilityPackageError("capability_not_installed")
        if current.capability_revision != expected_revision:
            raise CapabilityPackageError("capability_revision_drift")
        if manifest.capability_revision == current.capability_revision:
            raise CapabilityPackageError("capability_revision_unchanged")
        if _revision_key(manifest.capability_revision) <= _revision_key(current.capability_revision):
            raise CapabilityPackageError("capability_revision_not_newer")
        if self._database_path is None:
            self._active[manifest.capability_id] = manifest
            self._history[manifest.capability_id].append(manifest)
            return manifest
        with self._connect() as connection:
            self._append_revision(connection, manifest, operation="upgrade")
            changed = connection.execute(
                "UPDATE capability_package_active SET capability_revision = ? "
                "WHERE capability_id = ? AND capability_revision = ?",
                (manifest.capability_revision, manifest.capability_id, expected_revision),
            ).rowcount
            if changed != 1:
                raise CapabilityPackageError("capability_revision_drift")
        self._reload()
        return manifest

    def uninstall(
        self,
        capability_id: str,
        *,
        expected_revision: str,
        command_id: str | None = None,
        audit_ref: str | None = None,
    ) -> CapabilityPackageManifest:
        command_id, audit_ref = self._command_identity(
            "uninstall", capability_id, expected_revision, command_id, audit_ref,
        )
        current = self._active.get(capability_id)
        if current is None:
            desired = self._desired.get(capability_id)
            if desired is not None and desired.state == "disabled" and desired.command_id == command_id:
                if desired.audit_ref != audit_ref:
                    raise CapabilityPackageError("capability_command_identity_drift")
                historical = next(
                    (item for item in self._history.get(capability_id, ())
                     if item.capability_revision == expected_revision),
                    None,
                )
                if historical is not None:
                    return historical
            raise CapabilityPackageError("capability_not_installed")
        if current.capability_revision != expected_revision:
            raise CapabilityPackageError("capability_revision_drift")
        if self._database_path is None:
            removed = self._active.pop(capability_id)
            prior = self._desired.get(capability_id)
            self._desired[capability_id] = CapabilityPackageDesiredState(
                capability_id, "disabled", (prior.state_revision if prior else 0) + 1,
                command_id, audit_ref,
            )
            return removed
        with self._connect() as connection:
            changed = connection.execute(
                "DELETE FROM capability_package_active "
                "WHERE capability_id = ? AND capability_revision = ?",
                (capability_id, expected_revision),
            ).rowcount
            if changed != 1:
                raise CapabilityPackageError("capability_revision_drift")
            self._record_event(connection, current, operation="uninstall")
            self._set_desired_state(
                connection, capability_id=capability_id, state="disabled",
                command_id=command_id, audit_ref=audit_ref,
            )
        self._reload()
        return current

    def rollback(self, capability_id: str, *, expected_revision: str) -> CapabilityPackageManifest:
        current = self._active.get(capability_id)
        history = self._history.get(capability_id, [])
        if current is None or current.capability_revision != expected_revision:
            raise CapabilityPackageError("capability_revision_drift")
        current_index = next(
            (
                index for index, item in enumerate(history)
                if item.capability_revision == expected_revision
            ),
            -1,
        )
        if current_index < 1:
            raise CapabilityPackageError("rollback_revision_unavailable")
        previous = history[current_index - 1]
        if self._database_path is None:
            self._active[capability_id] = previous
            return previous
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE capability_package_active SET capability_revision = ? "
                "WHERE capability_id = ? AND capability_revision = ?",
                (previous.capability_revision, capability_id, expected_revision),
            ).rowcount
            if changed != 1:
                raise CapabilityPackageError("capability_revision_drift")
            self._record_event(connection, previous, operation="rollback")
        self._reload()
        return previous

    def active(self) -> tuple[CapabilityPackageManifest, ...]:
        return tuple(self._active[key] for key in sorted(self._active))

    def history(self, capability_id: str) -> tuple[CapabilityPackageManifest, ...]:
        return tuple(self._history.get(capability_id, ()))

    def desired_state(self, capability_id: str) -> CapabilityPackageDesiredState | None:
        return self._desired.get(capability_id)

    def is_disabled(self, capability_id: str) -> bool:
        desired = self._desired.get(capability_id)
        return desired is not None and desired.state == "disabled"

    def detach_missing_bundle(
        self, capability_id: str, *, expected_revision: str,
    ) -> CapabilityPackageManifest:
        """Remove an unavailable bundle without treating it as an operator uninstall."""
        current = self._active.get(capability_id)
        if current is None or current.capability_revision != expected_revision:
            raise CapabilityPackageError("capability_revision_drift")
        if self._database_path is None:
            return self._active.pop(capability_id)
        with self._connect() as connection:
            changed = connection.execute(
                "DELETE FROM capability_package_active "
                "WHERE capability_id = ? AND capability_revision = ?",
                (capability_id, expected_revision),
            ).rowcount
            if changed != 1:
                raise CapabilityPackageError("capability_revision_drift")
            self._record_event(connection, current, operation="uninstall")
        self._reload()
        return current

    def refresh(self) -> tuple[CapabilityPackageManifest, ...]:
        if self._database_path is not None:
            self._reload()
        return self.active()

    def _manifest_from_payload(
        self,
        payload: object,
        *,
        allow_legacy_importer_source_type: bool = False,
    ) -> CapabilityPackageManifest:
        if isinstance(payload, Mapping) and "artifact_id" in payload:
            payload = {key: value for key, value in payload.items() if key != "artifact_id"}
        if isinstance(payload, Mapping) and set(payload) == self._LEGACY_FIELDS:
            payload = {
                **dict(payload), "core_api": "1", "kind": "context_extension",
                "provides": {}, "tools": [], "workflows": [],
                "context": [],
                "permissions": {"net": [], "fs": [], "secrets": []},
                "budgets": {"max_bytes": 1, "max_seconds": 1}, "effects": {},
                "ui": {
                    "label": str(payload.get("display_name") or "Legacy capability"),
                    "icon": "legacy", "progress_steps": [],
                },
                "tests": [],
            }
        if isinstance(payload, Mapping) and set(payload) == self._FIELDS:
            payload = {**dict(payload), "context": []}
        if not isinstance(payload, Mapping) or set(payload) != self._FIELDS | {self._CONTEXT_FIELD}:
            raise CapabilityPackageError("invalid_manifest_fields")
        declaration_fields = {
            "contributions", "provides", "tools", "workflows", "permissions",
            "budgets", "effects", "ui", "tests", "context",
        }
        values = {name: payload[name] for name in self._FIELDS - declaration_fields}
        if not all(isinstance(value, str) and value.strip() for value in values.values()):
            raise CapabilityPackageError("invalid_manifest_identity")
        contributions = payload["contributions"]
        if (
            not isinstance(contributions, list) or not contributions
            or not all(isinstance(item, str) for item in contributions)
        ):
            raise CapabilityPackageError("invalid_contributions")
        provides = payload["provides"]
        tools = payload["tools"]
        workflows = payload["workflows"]
        context = payload["context"]
        permissions = payload["permissions"]
        budgets = payload["budgets"]
        effects = payload["effects"]
        ui = payload["ui"]
        tests = payload["tests"]
        if not isinstance(provides, Mapping) or not all(
            isinstance(key, str) and isinstance(items, list)
            and all(isinstance(item, str) and item for item in items)
            for key, items in provides.items()
        ):
            raise CapabilityPackageError("invalid_provides")
        if not isinstance(tools, list) or not all(isinstance(item, Mapping) for item in tools):
            raise CapabilityPackageError("invalid_tools")
        if not isinstance(workflows, list) or not all(isinstance(item, Mapping) for item in workflows):
            raise CapabilityPackageError("invalid_workflows")
        if not isinstance(context, list) or not all(isinstance(item, Mapping) for item in context):
            raise CapabilityPackageError("invalid_context_contributions")
        if not isinstance(permissions, Mapping) or set(permissions) != {"net", "fs", "secrets"}:
            raise CapabilityPackageError("invalid_permissions")
        if not isinstance(budgets, Mapping) or set(budgets) != {"max_bytes", "max_seconds"}:
            raise CapabilityPackageError("invalid_budgets")
        if not isinstance(effects, Mapping):
            raise CapabilityPackageError("invalid_effects")
        if not isinstance(ui, Mapping) or set(ui) != {"label", "icon", "progress_steps"}:
            raise CapabilityPackageError("invalid_ui_metadata")
        if not isinstance(tests, list) or not all(isinstance(item, str) and item for item in tests):
            raise CapabilityPackageError("invalid_contract_tests")
        manifest = CapabilityPackageManifest(
            **values,
            contributions=tuple(sorted(set(contributions))),
            provides={str(key): tuple(sorted(set(items))) for key, items in provides.items()},
            tools=tuple(dict(item) for item in tools),
            workflows=tuple(dict(item) for item in workflows),
            context=tuple(dict(item) for item in context),
            permissions=dict(permissions), budgets=dict(budgets), effects=dict(effects),
            ui=dict(ui), tests=tuple(tests),
        )
        self._validate_manifest(
            manifest,
            allow_legacy_importer_source_type=allow_legacy_importer_source_type,
        )
        return manifest

    def _validate_manifest(
        self,
        manifest: CapabilityPackageManifest,
        *,
        allow_legacy_importer_source_type: bool = False,
    ) -> None:
        if any(getattr(manifest, name) != value for name, value in self._EXPECTED_OWNERS.items()):
            raise CapabilityPackageError("forbidden_capability_authority")
        if manifest.schema_version.split(".", 1)[0] not in {"1", "2"}:
            raise CapabilityPackageError("unsupported_manifest_version")
        if manifest.core_api not in {"1", "2"}:
            raise CapabilityPackageError("unsupported_core_api")
        if manifest.kind not in self._KINDS:
            raise CapabilityPackageError("unknown_capability_kind")
        if self._CAPABILITY_ID.fullmatch(manifest.capability_id) is None:
            raise CapabilityPackageError("invalid_capability_id")
        if self._REVISION.fullmatch(manifest.capability_revision) is None:
            raise CapabilityPackageError("invalid_capability_revision")
        if not manifest.contributions or set(manifest.contributions) - self._CONTRIBUTIONS:
            raise CapabilityPackageError("unknown_contribution")
        for tool in manifest.tools:
            if set(tool) != {"id", "exposure", "contributes", "handler"}:
                raise CapabilityPackageError("invalid_tool_declaration")
            if tool["exposure"] not in {"model", "internal"}:
                raise CapabilityPackageError("invalid_tool_exposure")
            if tool["contributes"] not in manifest.contributions:
                raise CapabilityPackageError("undeclared_tool_contribution")
            self._validate_entrypoint(tool["handler"])
        for workflow in manifest.workflows:
            if set(workflow) != {"id", "decider"}:
                raise CapabilityPackageError("invalid_workflow_declaration")
            self._validate_entrypoint(workflow["decider"])
        context_ids: set[str] = set()
        importer_source_types: set[str] = set()
        for declaration in manifest.context:
            contribution_id = declaration.get("id")
            kind = declaration.get("kind")
            expected_fields = {"id", "kind", "entrypoint"}
            if kind == "importer":
                expected_fields.add("source_type")
            legacy_importer = (
                allow_legacy_importer_source_type
                and kind == "importer"
                and set(declaration) == {"id", "kind", "entrypoint"}
            )
            if not legacy_importer and set(declaration) != expected_fields:
                raise CapabilityPackageError("invalid_context_contribution")
            if not isinstance(contribution_id, str) or not contribution_id or contribution_id in context_ids:
                raise CapabilityPackageError("duplicate_context_contribution")
            if kind not in self._CONTRIBUTIONS or kind not in manifest.contributions:
                raise CapabilityPackageError("undeclared_context_contribution")
            if kind == "importer":
                if legacy_importer:
                    context_ids.add(contribution_id)
                    self._validate_entrypoint(declaration["entrypoint"])
                    continue
                source_type = declaration["source_type"]
                if (
                    not isinstance(source_type, str)
                    or self._SOURCE_TYPE.fullmatch(source_type) is None
                ):
                    raise CapabilityPackageError("invalid_context_importer_source_type")
                if source_type in importer_source_types:
                    raise CapabilityPackageError("duplicate_context_importer_source_type")
                importer_source_types.add(source_type)
            context_ids.add(contribution_id)
            self._validate_entrypoint(declaration["entrypoint"])
        net = manifest.permissions.get("net")
        fs = manifest.permissions.get("fs")
        secrets = manifest.permissions.get("secrets")
        if not isinstance(net, list) or not isinstance(fs, list) or not isinstance(secrets, list):
            raise CapabilityPackageError("invalid_permissions")
        if not all(isinstance(item, str) and item for item in (*net, *fs)):
            raise CapabilityPackageError("invalid_permissions")
        for secret in secrets:
            if (
                not isinstance(secret, Mapping) or set(secret) != {"name", "purpose", "ttl"}
                or not isinstance(secret.get("name"), str)
                or not isinstance(secret.get("purpose"), str)
                or not isinstance(secret.get("ttl"), int)
                or isinstance(secret.get("ttl"), bool)
                or not 1 <= secret["ttl"] <= 300
            ):
                raise CapabilityPackageError("invalid_secret_lease_declaration")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in manifest.budgets.values()
        ):
            raise CapabilityPackageError("invalid_budgets")
        if any(
            not isinstance(kind, str) or not kind
            or effect_class not in self._EFFECT_CLASSES
            for kind, effect_class in manifest.effects.items()
        ):
            raise CapabilityPackageError("invalid_effect_declaration")
        if (
            not isinstance(manifest.ui.get("label"), str)
            or not isinstance(manifest.ui.get("icon"), str)
            or not isinstance(manifest.ui.get("progress_steps"), list)
            or not all(isinstance(item, str) for item in manifest.ui["progress_steps"])
        ):
            raise CapabilityPackageError("invalid_ui_metadata")
        for path in manifest.tests:
            if path.startswith(("/", "\\")) or ".." in Path(path).parts:
                raise CapabilityPackageError("invalid_contract_test_path")

    def _validate_entrypoint(self, entrypoint: object) -> None:
        if (
            not isinstance(entrypoint, str)
            or self._ENTRYPOINT.fullmatch(entrypoint) is None
            or entrypoint.startswith(("/", "\\"))
            or ".." in Path(entrypoint.split(":", 1)[0]).parts
        ):
            raise CapabilityPackageError("invalid_package_entrypoint")

    def _validate_package_files(
        self, package_root: Path, manifest: CapabilityPackageManifest,
    ) -> None:
        for declaration in (*manifest.tools, *manifest.workflows, *manifest.context):
            entrypoint = (
                declaration.get("handler") or declaration.get("decider")
                or declaration.get("entrypoint")
            )
            assert isinstance(entrypoint, str)
            relative_path, function_name = entrypoint.split(":", 1)
            source_path = (package_root / relative_path).resolve(strict=False)
            try:
                source_path.relative_to(package_root.resolve(strict=True))
            except ValueError as exc:
                raise CapabilityPackageError("package_entrypoint_outside_root") from exc
            if not source_path.is_file():
                raise CapabilityPackageError("package_entrypoint_missing")
            try:
                tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
            except (SyntaxError, UnicodeError) as exc:
                raise CapabilityPackageError("package_entrypoint_invalid") from exc
            if not any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == function_name
                for node in tree.body
            ):
                raise CapabilityPackageError("package_entrypoint_symbol_missing")
        for test_path in manifest.tests:
            resolved = (package_root / test_path).resolve(strict=False)
            try:
                resolved.relative_to(package_root.resolve(strict=True))
            except ValueError as exc:
                raise CapabilityPackageError("contract_test_outside_root") from exc
            if not resolved.is_file():
                raise CapabilityPackageError("contract_test_missing")

    def _connect(self) -> sqlite3.Connection:
        if self._database_path is None:
            raise CapabilityPackageError("durable_catalog_unavailable")
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS capability_package_revisions (
                    capability_id TEXT NOT NULL,
                    capability_revision TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    artifact_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(capability_id, capability_revision),
                    UNIQUE(capability_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS capability_package_active (
                    capability_id TEXT PRIMARY KEY,
                    capability_revision TEXT NOT NULL,
                    FOREIGN KEY(capability_id, capability_revision)
                        REFERENCES capability_package_revisions(capability_id, capability_revision)
                );
                CREATE TABLE IF NOT EXISTS capability_package_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    capability_id TEXT NOT NULL,
                    capability_revision TEXT NOT NULL,
                    operation TEXT NOT NULL CHECK(operation IN ('install','upgrade','rollback','uninstall'))
                );
                CREATE TABLE IF NOT EXISTS capability_package_desired_state (
                    capability_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL CHECK(state IN ('enabled','disabled')),
                    state_revision INTEGER NOT NULL CHECK(state_revision >= 1),
                    command_id TEXT NOT NULL UNIQUE,
                    audit_ref TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capability_package_state_commands (
                    command_id TEXT PRIMARY KEY,
                    capability_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('enabled','disabled')),
                    audit_ref TEXT NOT NULL,
                    state_revision INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO capability_package_desired_state
                    (capability_id, state, state_revision, command_id, audit_ref)
                SELECT capability_id, 'enabled', 1,
                    'legacy-active:' || capability_id || ':' || capability_revision,
                    'legacy-active:' || capability_id || ':' || capability_revision
                FROM capability_package_active;
                INSERT OR IGNORE INTO capability_package_state_commands
                    (command_id, capability_id, state, audit_ref, state_revision)
                SELECT command_id, capability_id, state, audit_ref, state_revision
                FROM capability_package_desired_state;
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(capability_package_revisions)")}
            if "artifact_id" not in columns:
                connection.execute("ALTER TABLE capability_package_revisions ADD COLUMN artifact_id TEXT NOT NULL DEFAULT ''")

    def _reload(self) -> None:
        active: dict[str, CapabilityPackageManifest] = {}
        history: dict[str, list[CapabilityPackageManifest]] = {}
        desired: dict[str, CapabilityPackageDesiredState] = {}
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT manifest_json, artifact_id FROM capability_package_revisions "
                "ORDER BY capability_id, sequence"
            ).fetchall()
            for row in rows:
                manifest = self._manifest_from_payload(
                    json.loads(row["manifest_json"]),
                    allow_legacy_importer_source_type=True,
                )
                manifest = dataclass_replace(manifest, artifact_id=row["artifact_id"])
                history.setdefault(manifest.capability_id, []).append(manifest)
            rows = connection.execute(
                "SELECT r.manifest_json, r.artifact_id FROM capability_package_active a "
                "JOIN capability_package_revisions r USING(capability_id, capability_revision) "
                "ORDER BY a.capability_id"
            ).fetchall()
            active_count = connection.execute(
                "SELECT COUNT(*) FROM capability_package_active"
            ).fetchone()[0]
            if active_count != len(rows):
                raise CapabilityPackageError("capability_active_pointer_corrupt")
            for row in rows:
                manifest = self._manifest_from_payload(
                    json.loads(row["manifest_json"]),
                    allow_legacy_importer_source_type=True,
                )
                manifest = dataclass_replace(manifest, artifact_id=row["artifact_id"])
                # Pre-artifact records are readable history, but never executable
                # until a trusted source exactly re-materializes their artifact.
                if manifest.artifact_id:
                    active[manifest.capability_id] = manifest
            for row in connection.execute(
                "SELECT capability_id, state, state_revision, command_id, audit_ref "
                "FROM capability_package_desired_state ORDER BY capability_id"
            ).fetchall():
                desired[row["capability_id"]] = CapabilityPackageDesiredState(
                    row["capability_id"], row["state"], row["state_revision"],
                    row["command_id"], row["audit_ref"],
                )
        self._active = active
        self._history = history
        self._desired = desired

    def _append_revision(
        self, connection: sqlite3.Connection, manifest: CapabilityPackageManifest, *, operation: str,
    ) -> None:
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM capability_package_revisions "
            "WHERE capability_id = ?", (manifest.capability_id,),
        ).fetchone()[0]
        try:
            connection.execute(
                "INSERT INTO capability_package_revisions"
                "(capability_id, capability_revision, manifest_json, sequence, artifact_id) VALUES (?, ?, ?, ?, ?)",
                (
                    manifest.capability_id, manifest.capability_revision,
                    json.dumps({key: value for key, value in asdict(manifest).items() if key != "artifact_id"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    sequence, manifest.artifact_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise CapabilityPackageError("capability_revision_already_recorded") from exc
        self._record_event(connection, manifest, operation=operation)

    def _capture_artifact(self, manifest: CapabilityPackageManifest) -> CapabilityPackageManifest:
        if self._database_path is None:
            return manifest
        source = self._manifest_sources.get((manifest.capability_id, manifest.capability_revision))
        if source is None:
            # Persisted records cannot be rebound to mutable source trees.
            if manifest.artifact_id:
                self.artifact_root(manifest)
                return manifest
            raise CapabilityPackageError("capability_artifact_source_required")
        try:
            artifact = self._artifact_store.capture(source, capability_id=manifest.capability_id, capability_revision=manifest.capability_revision)
        except CapabilityArtifactError as exc:
            raise CapabilityPackageError(str(exc)) from exc
        return dataclass_replace(manifest, artifact_id=artifact.artifact_id)

    @staticmethod
    def _record_event(
        connection: sqlite3.Connection, manifest: CapabilityPackageManifest, *, operation: str,
    ) -> None:
        connection.execute(
            "INSERT INTO capability_package_events(capability_id, capability_revision, operation) "
            "VALUES (?, ?, ?)",
            (manifest.capability_id, manifest.capability_revision, operation),
        )

    @staticmethod
    def _command_identity(
        operation: str,
        capability_id: str,
        capability_revision: str,
        command_id: str | None,
        audit_ref: str | None,
    ) -> tuple[str, str]:
        # Callers that need replay semantics provide a stable command id. Legacy
        # direct callers get a fresh Core command so an explicit reinstall cannot
        # collide with its earlier install record.
        command = command_id or f"{operation}:{capability_id}:{capability_revision}:{uuid.uuid4().hex}"
        audit = audit_ref or f"core_catalog:{command}"
        if not isinstance(command, str) or not command.strip():
            raise CapabilityPackageError("invalid_capability_command")
        if not isinstance(audit, str) or not audit.strip():
            raise CapabilityPackageError("invalid_capability_audit_ref")
        return command, audit

    @staticmethod
    def _set_desired_state(
        connection: sqlite3.Connection,
        *,
        capability_id: str,
        state: str,
        command_id: str,
        audit_ref: str,
    ) -> None:
        existing_command = connection.execute(
            "SELECT capability_id, state, audit_ref, state_revision "
            "FROM capability_package_state_commands WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        if existing_command is not None:
            if (
                existing_command["capability_id"] != capability_id
                or existing_command["state"] != state
                or existing_command["audit_ref"] != audit_ref
            ):
                raise CapabilityPackageError("capability_command_identity_drift")
            return
        current = connection.execute(
            "SELECT state_revision FROM capability_package_desired_state WHERE capability_id = ?",
            (capability_id,),
        ).fetchone()
        next_revision = (current["state_revision"] if current is not None else 0) + 1
        connection.execute(
            "INSERT INTO capability_package_state_commands "
            "(command_id, capability_id, state, audit_ref, state_revision) VALUES (?, ?, ?, ?, ?)",
            (command_id, capability_id, state, audit_ref, next_revision),
        )
        if current is None:
            connection.execute(
                "INSERT INTO capability_package_desired_state "
                "(capability_id, state, state_revision, command_id, audit_ref) VALUES (?, ?, ?, ?, ?)",
                (capability_id, state, next_revision, command_id, audit_ref),
            )
            return
        changed = connection.execute(
            "UPDATE capability_package_desired_state "
            "SET state = ?, state_revision = ?, command_id = ?, audit_ref = ? "
            "WHERE capability_id = ? AND state_revision = ?",
            (state, next_revision, command_id, audit_ref, capability_id, current["state_revision"]),
        ).rowcount
        if changed != 1:
            raise CapabilityPackageError("capability_desired_state_drift")


def _revision_key(revision: str) -> tuple[int, int, int]:
    try:
        parts = tuple(int(part) for part in revision.split("."))
    except (TypeError, ValueError) as exc:
        raise CapabilityPackageError("invalid_capability_revision") from exc
    if len(parts) != 3:
        raise CapabilityPackageError("invalid_capability_revision")
    return parts
